import errno
import os
import re
import shutil
import sys
import json
import logging
from typing import Union, Tuple
from urllib.parse import urlparse
import requests
from django.contrib.postgres.aggregates import StringAgg
from lxml import etree
from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.core.management.base import BaseCommand, CommandError
from django.core.cache import cache
from recipe_scrapers import scrape_html

from recipe_api.settings import POSTGRES_LANGUAGE_UNACCENT
from recipes.models import Recipe, Category

CACHE_DIR = '/tmp/recipes'
URL_NYT = 'https://cooking.nytimes.com'


class Command(BaseCommand):
    help = 'Scrape NYT recipes'
    force = False

    def add_arguments(self, parser):
        parser.add_argument('--urls', action='store_true', help='Scrapes all recipe urls')
        parser.add_argument('--recipes', action='store_true', help='Scrapes all recipes')
        parser.add_argument('--specific-recipe-slug', help='Scrapes a specific recipe')
        parser.add_argument('--force', action='store_true', help='Forces an update')

    def handle(self, *args, **options):

        self.force = options['force']

        # validate required args
        if not any([options['urls'], options['recipes'], options['specific_recipe_slug']]):
            raise CommandError('Missing argument.  Run with -h to see options')

        self._validate_cache_path()

        if options['urls']:
            self._scrape_urls()
        if options['recipes']:
            self.scrape_recipes()
        if options['specific_recipe_slug']:
            self.scrape_specific_recipe(options['specific_recipe_slug'])

        # clear cache
        cache.clear()

    def scrape_specific_recipe(self, slug: str):
        recipe, image_url = self._scrape_recipe_url('{base_url}/recipes/{slug}'.format(base_url=URL_NYT, slug=slug.strip()))
        # save search vector
        vector = self._get_search_vector()
        for recipe in Recipe.objects.filter(slug=slug).annotate(vector=vector):
            recipe.search_vector = recipe.vector
            recipe.save()
        self._scrape_recipe_image(recipe, image_url)
        self.stdout.write(self.style.SUCCESS('Completed scraping {}'.format(recipe)))

    def scrape_recipes(self):

        self._scrape_recipes()

        # define search vector
        vector = self._get_search_vector()

        self.stdout.write(self.style.SUCCESS('Creating search vectors'))

        # add search vector to all recipes
        # NOTE: it's necessary to do it one by one since django doesn't support updates on aggregates (e.g., categories)
        for recipe in Recipe.objects.annotate(vector=vector):
            recipe.search_vector = recipe.vector
            recipe.save()

        self.stdout.write(self.style.SUCCESS('Complete'))

    def _get_search_vector(self):
        return (
                SearchVector('name', weight='A', config=POSTGRES_LANGUAGE_UNACCENT) +
                SearchVector(StringAgg('categories__name', ' '), weight='B', config=POSTGRES_LANGUAGE_UNACCENT) +
                SearchVector('ingredients', weight='C', config=POSTGRES_LANGUAGE_UNACCENT)
        )

    def _fetch_url_content(self, url) -> str:
        response = requests.get(url, timeout=30)
        return response.content.decode()

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
                # repeat requests a few times for failures
                self.stdout.write(self.style.WARNING('Bad sequential response #{} for {}'.format(sequential_failures, url)))
                sequential_failures += 1
                # too many consecutive errors for this page - go to the next page
                if sequential_failures > 5:
                    self.stdout.write(self.style.WARNING('Too many failures for {}, continuing'.format(url)))
                    page += 1
                continue

            sequential_failures = 0
            results = props.get('results', [])
            # filter to type: recipe (e.g., not "collection")
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

            # validate urls exist in the page
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
            self.stdout.write(self.style.ERROR(f'"{urls_file}" does not exist.  Run with --urls first'))
            sys.exit(1)

        self.stdout.write(self.style.SUCCESS('Scraping recipes'))

        # build a list of all recipe urls: existing and newly scraped
        urls_existing = [f'/recipes/{slug}' for slug in list(Recipe.objects.values_list('slug', flat=True))]
        urls_scraped = json.load(open(urls_file, 'r'))['urls']
        urls_all = set(urls_existing + urls_scraped)

        for i, url in enumerate(urls_all):
            slug = os.path.basename(url)

            # skip if we already have this recipe imported
            if not self.force and self._recipe_exists(slug=slug):
                continue

            # scrape recipe
            try:
                recipe, image_url = self._scrape_recipe_url('{base_url}{url}'.format(base_url=URL_NYT, url=url))
            except Exception as e:
                logging.exception(e)
                logging.warning('ERROR scraping url {}'.format(url))
                continue

            # scrape image
            try:
                self._scrape_recipe_image(recipe, image_url)
            except Exception as e:
                logging.exception(e)
                self.stdout.write(self.style.ERROR('Could not download image {} for {}'.format(image_url, recipe)))

            if i != 0 and i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Scraped {} recipes so far'.format(i)))

            recipes_scraped += 1

        self.stdout.write(self.style.SUCCESS('Scraped {} recipes total'.format(recipes_scraped)))

    def _convert_ingredient_groups(self, recipe_data: dict) -> list:
        ingredients = []
        for i, group in enumerate(recipe_data.get('ingredient_groups', [])):
            # define as "@@group@@" for UI to recognize a new group
            ingredients.append('@@' + (group.get('purpose') or f'Group {i+1}') + '@@')
            ingredients.extend(group.get('ingredients'))
        return ingredients

    def _scrape_recipe_url(self, url: str) -> Tuple[Recipe, str]:
        # return Recipe and external image url

        # fetch url
        response = requests.get(url, timeout=20)
        # parse recipe
        scraper = scrape_html(response.content, org_url=url, wild_mode=True)
        recipe_data = scraper.to_json()
        # validate it has ingredients
        if not recipe_data.get('ingredients'):
            raise Exception(f'skipping recipe {url} without ingredients or instructions')
        # handle ingredient groups by flattening them with special characters defining the group name
        if len(recipe_data.get('ingredient_groups')) > 1:
            # convert ingredient groups into a custom/flat list
            recipe_data['ingredients'] = self._convert_ingredient_groups(recipe_data)
        # save the recipe
        recipe, _ = Recipe.objects.update_or_create(
            slug=os.path.basename(url),
            defaults=dict(
                name=recipe_data.get('title'),
                description=self._replace_recipe_links_to_internal(recipe_data.get('description')),
                total_time_string=f"{recipe_data.get('total_time')} min",
                servings=recipe_data.get('yields') or '',
                rating_value=recipe_data.get('ratings'),
                rating_count=recipe_data.get('ratings_count'),
                ingredients=self._replace_recipe_links_to_internal(recipe_data.get('ingredients')),
                instructions=self._replace_recipe_links_to_internal(recipe_data.get('instructions_list')),
                author=recipe_data.get('author'),
            ),
        )
        # set categories/keywords
        categories = []
        for keyword in scraper.keywords():
            category, _ = Category.objects.update_or_create(
                name=keyword.lower(),
                defaults=dict(
                    type=Category.TYPE_UNKNOWN,
                ),
            )
            categories.append(category)
        recipe.categories.set(categories)

        return recipe, scraper.image()

    def _scrape_recipe_image(self, recipe, image_url: str):

        # use image extension for the new name based on slug
        extension_match = re.match(r'.*(\.\w{3})$', os.path.basename(image_url))
        if extension_match:
            extension = extension_match.groups()[0]
        else:
            extension = '.jpg'

        # define image name and output path
        image_name = '{}{}'.format(recipe.slug, extension)
        image_path_file = os.path.join(f'{settings.STATIC_ROOT}/recipes', image_name)

        # download images we haven't already scraped
        if not os.path.exists(image_path_file):

            # fetch image
            response = requests.get(image_url, stream=True, timeout=20)
            response.raise_for_status()

            # write to the static "output" directory
            with open(image_path_file, 'wb') as out_file:
                shutil.copyfileobj(response.raw, out_file)

        # save the recipe with the new image path
        recipe.image_path = f'{settings.STATIC_URL}recipes/{image_name}'
        recipe.save()

    def _replace_recipe_links_to_internal(self, value: Union[str, list]) -> Union[str, list]:
        domain_parsed = urlparse(URL_NYT)
        re_search = r'https?://{base_url}/recipes/'.format(base_url=domain_parsed.hostname)
        re_replace = '/#/recipe/'
        if isinstance(value, list):
            return [re.sub(re_search, re_replace, x) for x in value]
        return re.sub(re_search, re_replace, value)

    def _recipe_exists(self, slug):
        return Recipe.objects.filter(slug=slug).exists()

    def _validate_cache_path(self):
        # create cache dir if it doesn't exist
        try:
            os.makedirs(CACHE_DIR)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
