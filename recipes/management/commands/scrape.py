import errno
import os
import re
import shutil
import sys
import json
import logging
import requests
from django.contrib.postgres.aggregates import StringAgg
from django.core.management import call_command
from django.utils.dateparse import parse_duration
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


# TODO - must scrape raw html because json snippet doesn't include multiple ingredient sections
#      - https://cooking.nytimes.com/recipes/1020480-savory-thai-noodles-with-seared-brussels-sprouts


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

        sequential_failures = 0

        while True:
            page_recipe_urls = set()
            url = '{url}/search?page={page}'.format(url=URL_NYT, page=page)
            response = requests.get(url)

            # some pages return an error so keep trying a few more times
            if not response.ok:
                self.stdout.write(self.style.WARNING('Bad sequential response #{} for {}'.format(sequential_failures, url)))
                sequential_failures += 1
                # try the next page
                if sequential_failures <= 5:
                    page += 1
                    continue
                # too many consecutive errors
                else:
                    break

            sequential_failures = 0
            data = response.content
            html = etree.HTML(data)
            articles = html.xpath('//article')
            self.stdout.write(self.style.SUCCESS('Fetched {} with {} recipes'.format(url, len(articles))))
            if articles:
                for article in articles:
                    page_recipe_urls.add(article.attrib['data-url'])
            else:  # no more pages
                break

            # validate
            if page_recipe_urls.issubset(recipe_urls):
                self.stdout.write(self.style.SUCCESS('Page {} is identical to last so stopping scrape'.format(page)))
                break
            else:
                recipe_urls.update(page_recipe_urls)

            page += 1

        # save output as json file
        json.dump({'urls': list(recipe_urls)}, open(os.path.join(CACHE_DIR, 'urls.json'), 'w'))
        self.stdout.write(self.style.SUCCESS('Completed {} urls'.format(len(recipe_urls))))

    def _scrape_recipes(self):

        urls_file = os.path.join(CACHE_DIR, 'urls.json')

        # validate the input urls file
        if not os.path.exists(urls_file):
            self.stdout.write(self.style.ERROR('"urls.json" does not exist.  Run with --urls first'))
            sys.exit(1)

        self.stdout.write(self.style.SUCCESS('Scraping recipes'))

        recipes = []
        using_cached = 0

        for i, url in enumerate(json.load(open(urls_file, 'r'))['urls']):
            slug = os.path.basename(url)

            # skip if we already have this recipe imported
            if not self.force and self._recipe_exists(slug=slug):
                continue

            cache_path = os.path.join(CACHE_DIR, os.path.basename(url))

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
                recipe_json = html.xpath('//script[@type="application/ld+json"]')[0]
            except Exception as e:
                logging.warning('ERROR parsing {}'.format(url))
                logging.exception(e)
                continue

            if i != 0 and i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Scraped {} recipes'.format(i)))

            recipe_data = json.loads(recipe_json.text)

            # include their slug
            recipe_data['slug'] = os.path.basename(url)

            recipes.append(recipe_data)

        json.dump({'recipes': recipes}, open(os.path.join(CACHE_DIR, 'recipes.json'), 'w'), ensure_ascii=False)
        self.stdout.write(self.style.SUCCESS('Scraped {} recipes total'.format(len(recipes))))

    def _scrape_images(self):
        recipe_file = self._validate_recipes_json_file()

        recipes = json.load(open(recipe_file))
        for i, recipe in enumerate(recipes['recipes']):

            recipe_exists = self._recipe_exists(slug=recipe['slug'])

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
            image_name = '{}{}'.format(recipe['slug'], extension)
            with open(os.path.join('static/recipes', image_name), 'wb') as out_file:
                shutil.copyfileobj(response.raw, out_file)
            if i != 0 and i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Downloaded {} images'.format(i)))

        self.stdout.write(self.style.SUCCESS('Completed images'))

    def _ingest_recipes(self):

        # retrieve and validate recipe file
        recipe_file = self._validate_recipes_json_file()
        recipes = json.load(open(recipe_file))

        # import
        for i, recipe in enumerate(recipes['recipes']):

            # set image path if it exists
            rel_image_path = os.path.join('static', 'recipes', '{}.jpg'.format(recipe['slug']))
            abs_image_path = os.path.join(settings.BASE_DIR, rel_image_path)
            image_url = '/' + rel_image_path if os.path.exists(abs_image_path) else None

            # update external recipe urls to internal ones
            description = re.sub(r'{base_url}/recipes/'.format(base_url=URL_NYT), '/#/recipe/', recipe.get('description') or '')

            # skip recipes without ingredients
            if 'recipeIngredient' not in recipe:
                continue
            # sanitize ingredients by removing empty items
            ingredients = [i for i in recipe['recipeIngredient'] if i]

            # create recipe
            recipe_obj, _ = Recipe.objects.update_or_create(
                slug=recipe['slug'],
                defaults=dict(
                    name=recipe['name'],
                    image_path=image_url,
                    description=description,
                    # parse duration like "PT45M" to 45 minutes
                    total_time=parse_duration(recipe['totalTime']).seconds / 60 if 'totalTime' in recipe else None,
                    servings=recipe['recipeYield'],
                    rating_value=recipe['aggregateRating']['ratingValue'] if recipe['aggregateRating'] else None,
                    rating_count=recipe['aggregateRating']['ratingCount'] if recipe['aggregateRating'] else None,
                    ingredients=ingredients,
                    instructions=[x['text'] for x in recipe['recipeInstructions'] or [] if 'text' in x],
                    author=recipe['author']['name'],
                )
            )

            # combine and assign categories and keywords (they're csv strings)
            categories = recipe['recipeCategory'].split(',') + recipe['recipeCuisine'].split(',') + recipe['keywords'].split(',')
            for category in categories:
                category_name = category.strip()
                if not category:
                    continue
                cat_obj = Category.objects.filter(name=category_name).first()
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
        # NOTE: it's necessary to do it one by one since django doesn't support updates on aggregates (i.e categories)
        for recipe in Recipe.objects.annotate(vector=vector):
            recipe.search_vector = recipe.vector
            recipe.save()

        self.stdout.write(self.style.SUCCESS('Completed recipes'))

    def _validate_recipes_json_file(self):
        urls_file = os.path.join(CACHE_DIR, 'recipes.json')

        # validate the input urls file
        if not os.path.exists(urls_file):
            self.stdout.write(self.style.ERROR('"recipes.json" does not exist.  Run with --recipes first'))
            sys.exit(1)

        return urls_file

    def _recipe_exists(self, slug):
        return Recipe.objects.filter(slug=slug).exists()

    def _is_placeholder_image(self, image_path: str):
        # placeholder images are like https://static01.nyt.com/applications/cooking/5b227f9/assets/15.png
        return re.search('/assets/\d+\.(png|jpg|jpeg)', image_path, re.I)

    def _get_image_url_from_recipe(self, recipe: dict):
        if 'image' in recipe:
            if isinstance(recipe['image'], str):
                return recipe['image']
            elif isinstance(recipe['image'], dict) and 'url' in recipe['image']:
                return recipe['image']['url']
        return None

    def _validate_cache_path(self):
        # create cache dir if it doesn't exist
        try:
            os.makedirs(CACHE_DIR)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
