import errno
import os
import re
from glob import glob
import shutil
import sys
import json
import logging
from typing import List, Union
from urllib.parse import urlparse
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
        parser.add_argument('--urls', action='store_true', help='Captures all recipe urls')
        parser.add_argument('--recipes', action='store_true', help='Captures all recipes')
        parser.add_argument('--images', action='store_true', help='Downloads all recipe images')
        parser.add_argument('--ingest', action='store_true', help='Ingests recipes into db')
        parser.add_argument('--force', action='store_true', help='Forces an update')
        parser.add_argument('--scrape_and_ingest_recipe_slug', help="Manually scrape and ingest a specific recipe using it's slug")

    def handle(self, *args, **options):

        self.force = options['force']

        # validate required args
        if not any([options['urls'], options['recipes'], options['images'], options['ingest'], options['scrape_and_ingest_recipe_slug']]):
            raise CommandError('Missing argument.  Run with -h to see options')

        self._validate_cache_path()

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
        if options['scrape_and_ingest_recipe_slug']:
            self._scrape_and_ingest_specific_recipe(options['scrape_and_ingest_recipe_slug'])

        # clear cache
        cache.clear()

    def _fetch_page_props(self, url: str) -> dict:
        content = self._fetch_url_content(url)
        return self._parse_page_props(content)

    def _parse_page_props(self, content) -> dict:
        empty = {}
        html = etree.HTML(content)
        script_json = html.xpath('//script[@id="__NEXT_DATA__"]')
        # return empty if script is absent
        if not script_json:
            return empty
        data = json.loads(script_json[0].text)
        return data.get('props', empty).get('pageProps', empty)

    def _scrape_urls(self):
        recipe_urls = set()
        page = 1
        identical_page_failures = 0
        sequential_failures = 0

        while True:
            page_recipe_urls = set()
            url = '{url}/search?page={page}'.format(url=URL_NYT, page=page)
            try:
                props = self._fetch_page_props(url)
            except requests.exceptions.RequestException as e:
                logging.warning(e)
                # repeat requests few times on failures
                self.stdout.write(self.style.WARNING('Bad sequential response #{} for {}'.format(sequential_failures, url)))
                sequential_failures += 1
                # too many consecutive errors for this page - go to next page
                if sequential_failures > 5:
                    self.stdout.write(self.style.WARNING('Too many failures for {}, continuing'.format(url)))
                    page += 1
                continue

            sequential_failures = 0
            results = props.get('results', [])
            # filter to type: recipe (e.g. not "collection")
            recipes = [r for r in results if r.get('type') == 'recipe']

            self.stdout.write(self.style.SUCCESS('Fetched {} with {} recipes'.format(url, len(recipes))))
            if recipes:
                for recipe in recipes:
                    recipe_url = recipe.get('url')
                    if not recipe_url:
                        logging.warning(f'${url} is missing "url" in an article, skipping')
                    # verify it matches a recipe url pattern
                    if not re.search('^/recipes/', recipe_url):
                        continue
                    page_recipe_urls.add(recipe_url)
            else:  # no more pages
                self.stdout.write(self.style.SUCCESS('No recipes found on URl {}'.format(url)))

            # validate urls exist in page
            if not page_recipe_urls:
                logging.warning(f'no recipe urls found for {url}')
                page += 1
                continue
            # validate identical page
            if page_recipe_urls.issubset(recipe_urls):
                identical_page_failures += 1
                self.stdout.write(self.style.WARNING(f'identical page response #{identical_page_failures} for {url}'))
                if identical_page_failures >= 20:
                    self.stdout.write(
                        self.style.SUCCESS(f'Pages have been identical the last {identical_page_failures} times so stopping scrape on page {page}'))
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
                    content = self._fetch_url_content('{base_url}{url}'.format(base_url=URL_NYT, url=url))
                except requests.exceptions.RequestException as e:
                    logging.exception(e)
                    logging.warning('ERROR scraping url {}'.format(url))
                    continue
                # store cached version
                with open(cache_path, 'w') as fp:
                    fp.write(content)

            # parse recipe content
            recipe_data = self._parse_page_props(content)
            if not recipe_data or 'recipe' not in recipe_data:
                logging.warning('ERROR parsing {}'.format(url))
                continue

            if i != 0 and i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Scraped {} recipes'.format(i)))

            # write recipe json file
            recipe_file = os.path.join(CACHE_DIR, f'{slug}.json')
            json.dump(recipe_data, open(recipe_file, 'w'), ensure_ascii=False, indent=2)

            recipes_scraped += 1

        self.stdout.write(self.style.SUCCESS('Scraped {} recipes total'.format(recipes_scraped)))

    def _fetch_url_content(self, url) -> str:
        response = requests.get(url, timeout=30)
        return response.content.decode()

    def _scrape_images(self):
        # loop through all recipe json files
        recipe_files = self._get_recipe_json_files()
        for i, recipe_file in enumerate(recipe_files):
            recipe = json.load(open(recipe_file))
            if not recipe or not recipe.get('recipe'):
                logging.warning(f'skipping absent recipe record {recipe_file}')
                continue
            recipe = recipe['recipe']
            slug = os.path.basename(recipe['url'])

            recipe_exists = self._recipe_exists(slug=slug)

            image_url = self._get_image_url_from_recipe(recipe)

            # skip empty/placeholder images
            if not image_url or self._is_placeholder_image(image_url):
                logging.warning(f'skipping absent image for {recipe_file}')
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
            # write to static "output" directory
            with open(os.path.join('staticfiles/recipes', image_name), 'wb') as out_file:
                shutil.copyfileobj(response.raw, out_file)
            if i != 0 and i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Downloaded {} images'.format(i)))

        self.stdout.write(self.style.SUCCESS('Completed images'))

    def _ingest_recipes(self):
        num_ingested = 0

        # loop through all recipe json files
        recipe_files = self._get_recipe_json_files()
        for i, recipe_file in enumerate(recipe_files):
            if self._ingest_recipe(recipe_file):
                num_ingested += 1
            if i != 0 and i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Ingested {} recipes'.format(num_ingested)))

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

    def _ingest_recipe(self, recipe_file: str) -> Union[Recipe, None]:

        recipe = json.load(open(recipe_file))
        if 'recipe' not in recipe:
            logging.warning(f'skipping absent recipe in file {recipe_file}')
            return None
        recipe = recipe['recipe']

        slug = os.path.basename(recipe['url'])

        # set image path if it exists
        rel_image_path = os.path.join('staticfiles', 'recipes', '{}.jpg'.format(slug))
        rel_image_path_href = os.path.join('static', 'recipes', '{}.jpg'.format(slug))
        abs_image_path = os.path.join(settings.BASE_DIR, rel_image_path)
        image_url = '/' + rel_image_path_href if os.path.exists(abs_image_path) else None

        # description, ingredients, & instructions
        description = self._replace_recipe_links_to_internal(recipe.get('topnote') or '')
        ingredients = self._replace_recipe_links_to_internal(self._get_ingredients_from_recipe(recipe))
        instructions = self._replace_recipe_links_to_internal(self._get_instructions_from_recipe(recipe))
        if not instructions or not ingredients:
            logging.warning(f'skipping recipe {recipe_file} without ingredients or instructions')
            return None

        # author
        author = recipe.get('contentAttribution', {}).get('cardByline')

        # create recipe
        recipe_obj, _ = Recipe.objects.update_or_create(
            slug=slug,
            defaults=dict(
                name=recipe['title'],
                image_path=image_url,
                description=description,
                total_time_string=recipe.get('totalTime') or recipe.get('time'),
                servings=recipe['recipeYield'],
                rating_value=recipe.get('ratings', {}).get('avgRating'),
                rating_count=recipe.get('ratings', {}).get('numRatings'),
                ingredients=ingredients,
                instructions=instructions,
                author=author[:100],
            )
        )

        # assign categories
        categories = [t['name'] for t in recipe.get('tags', []) if t.get('name')]
        for category in categories:
            cat_obj, cat_created = Category.objects.get_or_create(
                name=category.lower(),
                # TODO - NYT recently removed the types of tags (e.g. diet, cuisine, meal-type, dish-type) so using "unknown" for now
                # TODO - will eventually need to either manually assign categories' types or get ride of the UI that offers those filters
                defaults=dict(
                    type='_UNKNOWN_',
                )
            )
            recipe_obj.categories.add(cat_obj)
            recipe_obj.save()

        return recipe_obj

    def _replace_recipe_links_to_internal(self, value: Union[str, list]) -> Union[str, list]:
        domain_parsed = urlparse(URL_NYT)
        re_search = r'https?://{base_url}/recipes/'.format(base_url=domain_parsed.hostname)
        re_replace = '/#/recipe/'
        if isinstance(value, list):
            return [re.sub(re_search, re_replace, x) for x in value]
        return re.sub(re_search, re_replace, value)

    def _scrape_and_ingest_specific_recipe(self, slug: str):
        recipe_data = self._fetch_page_props(f'{URL_NYT}/recipes/{slug}')
        recipe_file = os.path.join(CACHE_DIR, f'{slug}.json')
        json.dump(recipe_data, open(recipe_file, 'w'), ensure_ascii=False, indent=2)
        self._ingest_recipe(recipe_file)
        self.stdout.write(self.style.SUCCESS('Scraped & ingested recipe: {} to {}'.format(slug, recipe_file)))

    def _get_ingredients_from_recipe(self, recipe: dict) -> List:
        ingredients = []
        for ingredient in recipe.get('ingredients', []):
            if 'ingredients' in ingredient:
                # section name
                if ingredient.get('name'):
                    ingredients.append(f'@@{ingredient["name"]}@@')
                for sub_ingredient in ingredient['ingredients']:
                    ingredients.append(f"{sub_ingredient['quantity']} {sub_ingredient['text']}")
            else:
                ingredients.append(f"{ingredient['quantity']} {ingredient['text']}")
        return ingredients

    def _get_instructions_from_recipe(self, recipe: dict) -> List:
        instructions = []
        for instruction in recipe.get('steps', []):
            # section with nested steps
            if 'name' in instruction:
                if instruction.get('name'):
                    instructions.append(f'@@{instruction["name"]}@@')
                for inner_step in instruction.get('steps', []):
                    instructions.append(inner_step['description'])
            else:  # single section steps
                instructions.append(instruction['description'])
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
