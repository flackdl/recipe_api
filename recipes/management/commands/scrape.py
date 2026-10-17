import errno
import gzip
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
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}

SITEMAP_LEGACY_RECIPES = 'https://www.nytimes.com/sitemaps/www.nytimes.com/recipes_00000.xml.gz'
SITEMAP_COOKING_INDEX = 'https://www.nytimes.com/sitemaps/new/cooking.xml.gz'

# NYT recipe-page tag types -> our Category types (see scoopRecipe.tags in __NEXT_DATA__)
TAG_TYPE_TO_CATEGORY_TYPE = {
    'CUISINE': Category.TYPE_CUISINES,
    'MEAL': Category.TYPE_MEAL_TYPES,
    'COURSE': Category.TYPE_DISH_TYPES,
    'DISH': Category.TYPE_DISH_TYPES,
    'DIET': Category.TYPE_SPECIAL_DIET,
    'INVESTMENT': Category.TYPE_EASY_QUICK,
}


class Command(BaseCommand):
    help = 'Scrape NYT recipes'
    force = False

    def add_arguments(self, parser):
        parser.add_argument('--urls', action='store_true', help='Scrapes all recipe urls')
        parser.add_argument('--recipes', action='store_true', help='Scrapes all recipes')
        parser.add_argument('--specific-recipe-slug', help='Scrapes a specific recipe')
        parser.add_argument('--force', action='store_true', help='Forces an update')
        parser.add_argument('--categories', action='store_true', help='Backfill typed categories for all recipes')
        parser.add_argument('--clear-unknown-categories', action='store_true', help='Delete all _UNKNOWN_ categories')

    def handle(self, *args, **options):

        self.force = options['force']

        # validate required args
        if not any([options['urls'], options['recipes'], options['specific_recipe_slug'],
                    options['categories'], options['clear_unknown_categories']]):
            raise CommandError('Missing argument.  Run with -h to see options')

        self._validate_cache_path()

        if options['urls']:
            self._scrape_urls()
        if options['recipes']:
            self.scrape_recipes()
        if options['specific_recipe_slug']:
            self.scrape_specific_recipe(options['specific_recipe_slug'])
        if options['categories']:
            self.backfill_categories()
        if options['clear_unknown_categories']:
            self.clear_unknown_categories()

        # clear cache
        cache.clear()

    def scrape_specific_recipe(self, slug: str):
        recipe, image_url = self._scrape_recipe_url('{base_url}/recipes/{slug}'.format(base_url=URL_NYT, slug=slug.strip()))
        # save search vector
        vector = self._get_search_vector()
        recipe = Recipe.objects.filter(slug=recipe.slug).annotate(vector=vector).first() or recipe
        recipe.search_vector = vector
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

    def backfill_categories(self):
        # re-fetch each recipe page and update only its typed categories (no image downloads)
        recipes = list(Recipe.objects.all())
        for i, recipe in enumerate(recipes):
            url = '{base_url}/recipes/{slug}'.format(base_url=URL_NYT, slug=recipe.slug)
            try:
                response = requests.get(url, timeout=20, headers=REQUEST_HEADERS)
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                logging.warning('failed to fetch %s: %s', url, e)
                continue
            recipe.categories.set(self._categories_from_tags(response.content))
            if i != 0 and i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Backfilled {} recipes so far'.format(i)))
        self.stdout.write(self.style.SUCCESS('Completed category backfill for {} recipes'.format(len(recipes))))

        # rebuild search vectors now that categories have changed
        vector = self._get_search_vector()
        self.stdout.write(self.style.SUCCESS('Rebuilding search vectors'))
        for recipe in Recipe.objects.annotate(vector=vector):
            recipe.search_vector = recipe.vector
            recipe.save()
        self.stdout.write(self.style.SUCCESS('Complete'))

    def clear_unknown_categories(self):
        deleted, _ = Category.objects.filter(type=Category.TYPE_UNKNOWN).delete()
        self.stdout.write(self.style.SUCCESS('Deleted {} unknown categories'.format(deleted)))

    def _get_search_vector(self):
        return (
                SearchVector('name', weight='A', config=POSTGRES_LANGUAGE_UNACCENT) +
                SearchVector(StringAgg('categories__name', ' '), weight='B', config=POSTGRES_LANGUAGE_UNACCENT) +
                SearchVector('ingredients', weight='C', config=POSTGRES_LANGUAGE_UNACCENT)
        )

    def _extract_recipe_tags(self, content):
        # parse __NEXT_DATA__ -> pageProps.scoopRecipe.tags
        html = etree.HTML(content)
        script = html.xpath('//script[@id="__NEXT_DATA__"]')
        if not script or not script[0].text:
            return []
        try:
            data = json.loads(script[0].text)
        except (ValueError, TypeError):
            return []
        scoop = data.get('props', {}).get('pageProps', {}).get('scoopRecipe') or {}
        return scoop.get('tags') or []

    def _categories_from_tags(self, content):
        # map NYT tag types to typed Category rows
        categories = []
        for tag in self._extract_recipe_tags(content):
            category_type = TAG_TYPE_TO_CATEGORY_TYPE.get(tag.get('type'))
            name = (tag.get('normalizedName') or (tag.get('name') or '').lower()).strip()
            if not category_type or not name:
                continue
            category, _ = Category.objects.update_or_create(
                name=name,
                defaults=dict(type=category_type),
            )
            categories.append(category)
        return categories

    def _fetch_sitemap(self, url):
        # fetch a sitemap (gzip or plain XML) and return its <loc> urls
        try:
            response = requests.get(url, timeout=30, headers=REQUEST_HEADERS)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logging.warning('failed to fetch sitemap %s: %s', url, e)
            self.stdout.write(self.style.ERROR('Failed to fetch sitemap {}'.format(url)))
            return []
        content = response.content
        # NYT serves .xml.gz paths as plain XML; sniff the gzip magic bytes
        if content[:2] == b'\x1f\x8b':
            content = gzip.decompress(content)
        text = content.decode('utf-8', 'ignore')
        return [loc.strip() for loc in re.findall(r'<loc>([^<]+)</loc>', text) if loc.strip()]

    def _slug_from_loc(self, loc):
        # convert a sitemap <loc> into a cooking slug, or None if not a recipe
        path = urlparse(loc).path
        # cooking format: /recipes/<id>-<slug>
        match = re.search(r'/recipes/(\d+)-([A-Za-z0-9\-]+)$', path)
        if match:
            return '{}-{}'.format(match.group(1), match.group(2).lower())
        # legacy www format: /recipes/<id>/<Slug>.html
        match = re.search(r'/recipes/(\d+)/([^/]+)\.html$', path)
        if match:
            return '{}-{}'.format(match.group(1), match.group(2).lower())
        return None

    def _scrape_urls(self):
        slugs = set()

        # legacy www.nytimes.com recipes (single sitemap)
        for loc in self._fetch_sitemap(SITEMAP_LEGACY_RECIPES):
            slug = self._slug_from_loc(loc)
            if slug:
                slugs.add(slug)

        # newer cooking.nytimes.com recipes (index -> monthly sitemaps)
        for index_loc in self._fetch_sitemap(SITEMAP_COOKING_INDEX):
            for loc in self._fetch_sitemap(index_loc):
                slug = self._slug_from_loc(loc)
                if slug:
                    slugs.add(slug)

        slugs = sorted(slugs)

        # save output as json file (url paths, matching what --recipes expects)
        json.dump({'urls': ['/recipes/{}'.format(slug) for slug in slugs]},
                  open(os.path.join(CACHE_DIR, 'urls.json'), 'w'), indent=2)
        self.stdout.write(self.style.SUCCESS('Completed {} urls'.format(len(slugs))))

    def _scrape_recipes(self):
        recipes_scraped = 0
        urls_file = os.path.join(CACHE_DIR, 'urls.json')

        # validate the input urls file
        if not os.path.exists(urls_file):
            self.stdout.write(self.style.ERROR(f'"{urls_file}" does not exist.  Run with --urls first'))
            sys.exit(1)

        # build a list of all recipe urls: existing and newly scraped
        urls_existing = [f'/recipes/{slug}' for slug in list(Recipe.objects.values_list('slug', flat=True))]
        urls_scraped = json.load(open(urls_file, 'r'))['urls']
        urls_all = set(urls_existing + urls_scraped)

        self.stdout.write(self.style.SUCCESS(f'Scraping {len(urls_all)} recipes'))

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
            ingredients.extend(group.get('ingredients') or [])
        return ingredients

    def _scrape_recipe_url(self, url: str) -> Tuple[Recipe, str]:
        # return Recipe and external image url

        # fetch url
        response = requests.get(url, timeout=20, headers=REQUEST_HEADERS)
        # resolve the canonical slug from the final url (old slugs redirect to the current one)
        final_url = response.url
        slug = os.path.basename(urlparse(final_url).path)
        # parse recipe
        scraper = scrape_html(response.content, org_url=final_url, wild_mode=True)
        recipe_data = scraper.to_json()
        # validate it has ingredients
        if not recipe_data.get('ingredients'):
            raise Exception(f'skipping recipe {url} without ingredients or instructions')
        # handle ingredient groups by flattening them with special characters defining the group name
        if len(recipe_data.get('ingredient_groups', [])) > 1:
            # convert ingredient groups into a custom/flat list
            recipe_data['ingredients'] = self._convert_ingredient_groups(recipe_data)
        # save the recipe
        recipe, _ = Recipe.objects.update_or_create(
            slug=slug,
            defaults=dict(
                name=recipe_data.get('title'),
                description=self._replace_recipe_links_to_internal(recipe_data.get('description') or ''),
                total_time_string=(f"{recipe_data.get('total_time')} min" if recipe_data.get('total_time') else ''),
                servings=recipe_data.get('yields') or '',
                rating_value=recipe_data.get('ratings'),
                rating_count=recipe_data.get('ratings_count'),
                ingredients=self._replace_recipe_links_to_internal(self._clean_list(recipe_data.get('ingredients'))),
                instructions=self._replace_recipe_links_to_internal(self._clean_list(recipe_data.get('instructions_list'))),
                author=recipe_data.get('author'),
            ),
        )
        # set typed categories from __NEXT_DATA__ tags
        recipe.categories.set(self._categories_from_tags(response.content))

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

    def _clean_list(self, value):
        if value is None:
            return []
        if isinstance(value, list):
            return [x for x in value if x is not None and x != 'None']
        return value

    def _replace_recipe_links_to_internal(self, value: Union[str, list]) -> Union[str, list]:
        if value is None:
            return value
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
