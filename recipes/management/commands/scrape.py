import errno
import os
import re
from glob import glob
import shutil
import sys
import json
import logging
from typing import List

import requests
from django.contrib.postgres.aggregates import StringAgg
from django.core.management import call_command
from lxml import etree
from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.core.management.base import BaseCommand, CommandError
from django.core.cache import cache

from recipe_api.settings import POSTGRES_LANGUAGE_UNACCENT
from recipes.models import Recipe, Category

CACHE_DIR = '/tmp/recipes'
URL_NYT = 'https://cooking.nytimes.com'
CATEGORY_TYPES = ['special_diets', 'cuisines', 'meal_types', 'dish_types']


class Command(BaseCommand):
    help = 'Scrape NYT recipes'
    force = False

    def add_arguments(self, parser):
        parser.add_argument('--categories', action='store_true', help='Captures all categories')
        parser.add_argument('--urls', action='store_true', help='Captures all recipe urls')
        parser.add_argument('--recipes', action='store_true', help='Captures all recipes')
        parser.add_argument('--images', action='store_true', help='Downloads all recipe images')
        parser.add_argument('--ingest', action='store_true', help='Ingests recipes into db')
        parser.add_argument('--force', action='store_true', help='Forces an update')

    def handle(self, *args, **options):

        self.force = options['force']

        # validate required args
        if not any([options['categories'], options['urls'], options['recipes'], options['images'], options['ingest']]):
            raise CommandError('Missing argument')

        self._validate_cache_path()

        if options['categories']:
            self._scrape_categories()
        if options['urls']:
            self._scrape_urls()
        if options['recipes']:
            self._scrape_recipes()
        if options['images']:
            self._scrape_images()
            # collect static files since we scraped new images
            call_command('collectstatic', interactive=False)
        if options['ingest']:
            self._ingest_recipes()

        # clear cache
        cache.clear()

    def _scrape_categories(self):
        url = '{base_url}/search'.format(base_url=URL_NYT)
        response = requests.get(url)
        data = response.content
        html = etree.HTML(data)
        categories_processed = 0
        for category_type in CATEGORY_TYPES:
            categories = html.xpath('//div[@facet-type="{}"]//label[@class="general-facet"]'.format(category_type))
            for category in categories:
                Category.objects.update_or_create(
                    name=category.text,
                    defaults=dict(
                        type=category_type,
                    )
                )
                categories_processed += 1
        self.stdout.write(self.style.SUCCESS('Collected {} categories'.format(categories_processed)))

    def _scrape_urls(self):
        recipe_urls = set()
        page = 1
        identical_page_failures = 0
        sequential_failures = 0

        while True:
            page_recipe_urls = set()
            url = '{url}/search?page={page}'.format(url=URL_NYT, page=page)
            response = requests.get(url)

            # repeat requests few times on failures
            if not response.ok:
                self.stdout.write(self.style.WARNING('Bad sequential response #{} for {}'.format(sequential_failures, url)))
                sequential_failures += 1
                # too many consecutive errors for this page - go to next page
                if sequential_failures > 5:
                    self.stdout.write(self.style.WARNING('Too many failures for {}, continuing'.format(url)))
                    page += 1
                continue

            sequential_failures = 0

            data = response.content
            html = etree.HTML(data)
            articles = html.xpath('//article')
            self.stdout.write(self.style.SUCCESS('Fetched {} with {} recipes'.format(url, len(articles))))
            if articles:
                for article in articles:
                    recipe_url = article.attrib['data-url']
                    # verify it matches a recipe url pattern
                    if not re.search('^/recipes/', recipe_url):
                        continue
                    page_recipe_urls.add(article.attrib['data-url'])
            else:  # no more pages
                break

            # validate urls in page
            if not page_recipe_urls:
                logging.warning(f'no recipe urls for {url}')
                page += 1
                continue
            # validate identical page
            if page_recipe_urls.issubset(recipe_urls):
                identical_page_failures += 1
                self.stdout.write(self.style.WARNING(f'identical page response #{identical_page_failures} for {url}'))
                if identical_page_failures >= 20:
                    self.stdout.write(self.style.SUCCESS(f'Pages have been identical the last {identical_page_failures} times so stopping scrape on page {page}'))
                    break
            else:
                identical_page_failures = 0
                recipe_urls.update(page_recipe_urls)

            page += 1

        # save output as json file
        json.dump({'urls': list(recipe_urls)}, open(os.path.join(CACHE_DIR, 'urls.json'), 'w'), indent=2)
        self.stdout.write(self.style.SUCCESS('Completed {} urls'.format(len(recipe_urls))))

    def _scrape_recipes(self):
        recipes_scraped = 0
        urls_file = os.path.join(CACHE_DIR, 'urls.json')

        # validate the input urls file
        if not os.path.exists(urls_file):
            self.stdout.write(self.style.ERROR('"urls.json" does not exist.  Run with --urls first'))
            sys.exit(1)

        self.stdout.write(self.style.SUCCESS('Scraping recipes'))

        using_cached = 0

        for i, url in enumerate(json.load(open(urls_file, 'r'))['urls']):
            slug = os.path.basename(url)

            # skip if we already have this recipe imported
            if not self.force and self._recipe_exists(slug=slug):
                continue

            cache_path = os.path.join(CACHE_DIR, slug)

            # use cache if it exists
            if not self.force and os.path.exists(cache_path):
                content = open(cache_path, 'r').read()
                if using_cached != 0 and using_cached % 100 == 0:
                    logging.warning('Used {} cached pages'.format(using_cached))
            # scrape url
            else:
                try:
                    response = requests.get('{base_url}{url}'.format(base_url=URL_NYT, url=url))
                except Exception as e:
                    logging.exception(e)
                    logging.warning('ERROR scraping url {}'.format(url))
                    continue
                content = response.content.decode()
                # store cached version
                with open(cache_path, 'w') as fp:
                    fp.write(content)

            # parse recipe content
            try:
                html = etree.HTML(content)
                recipe_json = html.xpath('//script[@id="__NEXT_DATA__"]')[0]
            except Exception as e:
                logging.warning('ERROR parsing {}'.format(url))
                logging.exception(e)
                continue

            if i != 0 and i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Scraped {} recipes'.format(i)))

            page_data = json.loads(recipe_json.text)

            # validate recipe data
            if 'props' not in page_data or 'pageProps' not in page_data['props']:
                logging.warning(f'recipe {url} has no expected props data')
                continue

            # write recipe json file
            recipe_data = page_data['props']['pageProps']
            recipe_file = os.path.join(CACHE_DIR, f'{slug}.json')
            json.dump(recipe_data, open(recipe_file, 'w'), ensure_ascii=False, indent=2)

            recipes_scraped += 1

        self.stdout.write(self.style.SUCCESS('Scraped {} recipes total'.format(recipes_scraped)))

    def _scrape_images(self):
        # loop through all recipe json files
        recipe_files = self._get_recipe_json_files()
        for i, recipe_file in enumerate(recipe_files):
            recipe = json.load(open(recipe_file))
            if not recipe or not recipe.get('recipe'):
                logging.warning(f'skipping absent recipe record {recipe_file}')
                continue
            recipe = recipe['recipe']

            recipe_exists = self._recipe_exists(slug=os.path.basename(recipe['url']))

            image_url = self._get_image_url_from_recipe(recipe)

            # skip empty/placeholder images
            if not image_url or self._is_placeholder_image(image_url):
                continue

            # skip recipes we've already imported
            if not self.force and recipe_exists:
                continue

            try:
                response = requests.get(image_url, stream=True)
                response.raise_for_status()
            except Exception as e:
                logging.exception(e)
                self.stdout.write(self.style.ERROR('Could not download image {} for {}'.format(image_url, recipe['name'])))
                continue
            # use image extension for new name based on slug
            extension_match = re.match(r'.*(\.\w{3})$', os.path.basename(image_url))
            if extension_match:
                extension = extension_match.groups()[0]
            else:
                extension = '.jpg'
            image_name = '{}{}'.format(os.path.basename(recipe['url']), extension)
            with open(os.path.join('static/recipes', image_name), 'wb') as out_file:
                shutil.copyfileobj(response.raw, out_file)
            if i != 0 and i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Downloaded {} images'.format(i)))

        self.stdout.write(self.style.SUCCESS('Completed images'))

    def _ingest_recipes(self):
        num_ingested = 0

        # loop through all recipe json files
        recipe_files = self._get_recipe_json_files()
        for i, recipe_file in enumerate(recipe_files):
            recipe = json.load(open(recipe_file))
            if 'recipe' not in recipe:
                logging.warning(f'skipping absent recipe in file {recipe_file}')
                continue
            recipe = recipe['recipe']

            slug = os.path.basename(recipe['url'])

            # set image path if it exists
            rel_image_path = os.path.join('static', 'recipes', '{}.jpg'.format(slug))
            abs_image_path = os.path.join(settings.BASE_DIR, rel_image_path)
            image_url = '/' + rel_image_path if os.path.exists(abs_image_path) else None

            # update external recipe urls to internal ones
            description = re.sub(r'{base_url}/recipes/'.format(base_url=URL_NYT), '/#/recipe/', recipe.get('topnote') or '')

            ingredients = self._get_ingredients_from_recipe(recipe)
            instructions = self._get_instructions_from_recipe(recipe)
            if not instructions or not ingredients:
                continue

            author = recipe.get('contentAttribution', {}).get('cardByline')

            # create recipe
            recipe_obj, _ = Recipe.objects.update_or_create(
                slug=slug,
                defaults=dict(
                    name=recipe['title'],
                    image_path=image_url,
                    description=description,
                    total_time_string=recipe['time'],
                    servings=recipe['recipeYield'],
                    rating_value=recipe.get('ratings', {}).get('avgRating'),
                    rating_count=recipe.get('ratings', {}).get('numRatings'),
                    ingredients=ingredients,
                    instructions=instructions,
                    author=author[:100],
                )
            )

            num_ingested += 1

            # combine and assign categories and keywords (they're csv strings)
            categories = [t['name'] for t in recipe.get('tags', []) if t.get('name')]
            for category in categories:
                cat_obj = Category.objects.filter(name=category).first()
                # only assign categories that already exist
                if cat_obj:
                    recipe_obj.categories.add(cat_obj)
                    recipe_obj.save()

            if i != 0 and i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Ingested {} recipes'.format(i)))

        # define search vector
        vector = (
            SearchVector('name', weight='A', config=POSTGRES_LANGUAGE_UNACCENT) +
            SearchVector(StringAgg('categories__name', ' '), weight='B', config=POSTGRES_LANGUAGE_UNACCENT) +
            SearchVector('ingredients', weight='C', config=POSTGRES_LANGUAGE_UNACCENT)
        )

        # add search vector to all recipes
        # NOTE: it's necessary to do it one by one since django doesn't support updates on aggregates (e.g. categories)
        for recipe in Recipe.objects.annotate(vector=vector):
            recipe.search_vector = recipe.vector
            recipe.save()

        self.stdout.write(self.style.SUCCESS(f'Complete: ingested {num_ingested} recipes'))

    def _get_ingredients_from_recipe(self, recipe: dict) -> List:
        ingredients = []
        for ingredient in recipe.get('ingredients', []):
            # section
            if 'ingredients' in ingredient:
                ingredients.append(f'@@{ingredient["name"]}@@')
                for sub_ingredient in ingredient['ingredients']:
                    ingredients.append(f"{sub_ingredient['quantity']} {sub_ingredient['text']}")
            else:
                ingredients.append(f"{ingredient['quantity']} {ingredient['text']}")
        return ingredients

    def _get_instructions_from_recipe(self, recipe: dict) -> List:
        instructions = []
        for step in recipe.get('steps', []):
            # section with nested steps
            if 'name' in step:
                instructions.append(f'@@{step["name"]}@@')
                for inner_step in step.get('steps', []):
                    instructions.append(inner_step['description'])
            else:  # single section steps
                instructions.append(step['description'])
        return instructions

    def _get_recipe_json_files(self) -> List[str]:
        return glob(os.path.join(CACHE_DIR, '*.json'))

    def _recipe_exists(self, slug):
        return Recipe.objects.filter(slug=slug).exists()

    def _is_placeholder_image(self, image_path: str):
        # placeholder images are like https://static01.nyt.com/applications/cooking/5b227f9/assets/15.png
        return re.search('/assets/\d+\.(png|jpg|jpeg)', image_path, re.I)

    def _get_image_url_from_recipe(self, recipe: dict):
        if not recipe:
            return
        if recipe.get('image'):
            if recipe['image'].get('src'):
                return recipe['image']['src'].get('article')

    def _validate_cache_path(self):
        # create cache dir if it doesn't exist
        try:
            os.makedirs(CACHE_DIR)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
