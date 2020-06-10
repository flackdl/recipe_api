import errno
import os
import re
import shutil
import sys
import json
import logging
import requests
from django.core.management import call_command
from django.utils.dateparse import parse_duration
from lxml import etree
from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.core.management.base import BaseCommand, CommandError
from django.core.cache import cache

from recipe_api.settings import POSTGRES_LANGUAGE_UNACCENT
from recipes.models import Recipe, Category, Cuisine

CACHE_DIR = '/tmp/recipes'


class Command(BaseCommand):
    help = 'Scrape NYT recipes'

    def add_arguments(self, parser):
        parser.add_argument('--urls', action='store_true', help='Captures all recipe urls')
        parser.add_argument('--recipes', action='store_true', help='Captures all recipes')
        parser.add_argument('--images', action='store_true', help='Downloads all recipe images')
        parser.add_argument('--ingest', action='store_true', help='Ingests recipes into db')
        parser.add_argument('--all', action='store_true', help='Scrapes and ingests everything')

    def handle(self, *args, **options):

        # validate args
        if not any([options['urls'], options['recipes'], options['images'], options['ingest'], options['all']]):
            raise CommandError('Missing argument')

        if options['urls']:
            self._scrape_urls()
        elif options['recipes']:
            self._scrape_recipes()
        elif options['images']:
            self._scrape_images()
        elif options['ingest']:
            self._ingest_recipes()
        elif options['all']:
            self._scrape_urls()
            self._scrape_recipes()
            self._scrape_images()
            self._ingest_recipes()
            # clear cache and collect static files for new recipe images
            cache.clear()
            call_command('collectstatic', verbosity=0, interactive=False)

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

            # TODO - update external recipe urls to internal ones
            #description = re.sub(r'https://cooking.nytimes.com/recipes/', '#/recipe/', recipe['description'] or '')
            description = recipe['description']

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
                    ingredients=recipe['recipeIngredient'],
                    instructions=[x['text'] for x in recipe['recipeInstructions'] or [] if 'text' in x],
                    author=recipe['author']['name'],
                )
            )

            # assign categories (they're csv strings)
            categories = recipe['recipeCategory'].split(',')
            for category in categories:
                if not category:
                    continue
                cat_obj, _ = Category.objects.get_or_create(name=category.strip())
                recipe_obj.categories.add(cat_obj)
                recipe_obj.save()

            # assign cuisines (they're csv strings)
            cuisines = recipe['recipeCuisine'].split(',')
            for cuisine in cuisines:
                if not cuisine:
                    continue
                cuisine_obj, _ = Cuisine.objects.get_or_create(name=cuisine.strip())
                recipe_obj.cuisines.add(cuisine_obj)
                recipe_obj.save()

            if i != 0 and i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Ingested {} recipes'.format(i)))

        # add search vector
        vector = SearchVector('name', weight='A', config=POSTGRES_LANGUAGE_UNACCENT) + SearchVector('ingredients', weight='B', config=POSTGRES_LANGUAGE_UNACCENT)
        Recipe.objects.update(search_vector=vector)

        self.stdout.write(self.style.SUCCESS('Complete'))

    def _scrape_images(self):
        recipe_file = self._validate_recipes_json_file()

        recipes = json.load(open(recipe_file))
        for i, recipe in enumerate(recipes['recipes']):

            # skip if we already have this recipe imported
            if self._recipe_exists(slug=recipe['slug']):
                continue

            try:
                response = requests.get(recipe['image'], stream=True)
                response.raise_for_status()
            except Exception as e:
                logging.exception(e)
                self.stdout.write(self.style.ERROR('Could not download image {} for {}'.format(recipe['image'], recipe['name'])))
                continue
            # use image extension for new name based on slug
            extension_match = re.match(r'.*(\.\w{3})$', os.path.basename(recipe['image']))
            if extension_match:
                extension = extension_match.groups()[0]
            else:
                extension = '.jpg'
            image_name = '{}{}'.format(recipe['slug'], extension)
            with open(os.path.join('static/recipes', image_name), 'wb') as out_file:
                shutil.copyfileobj(response.raw, out_file)
            if i != 0 and i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Downloaded {} images'.format(i)))

        self.stdout.write(self.style.SUCCESS('Complete'))

    def _scrape_recipes(self):

        urls_file = os.path.join(settings.BASE_DIR, 'urls.json')

        # validate the input urls file
        if not os.path.exists(urls_file):
            self.stdout.write(self.style.ERROR('"urls.json" does not exist.  Run with --urls first'))
            sys.exit(1)

        # create cache dir if it doesn't exist
        try:
            os.makedirs(CACHE_DIR)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise

        self.stdout.write(self.style.SUCCESS('Scraping recipes'))

        recipes = []
        using_cached = 0

        for i, url in enumerate(json.load(open(urls_file, 'r'))['urls']):

            # skip if we already have this recipe imported
            slug = os.path.basename(url)
            if self._recipe_exists(slug=slug):
                continue

            cache_path = os.path.join(CACHE_DIR, os.path.basename(url))

            # use cache if it exists
            if os.path.exists(cache_path):
                content = open(cache_path, 'r').read()
                if using_cached != 0 and using_cached % 100 == 0:
                    logging.warning('Used {} cached pages'.format(using_cached))
            # scrape url
            else:
                try:
                    response = requests.get('https://cooking.nytimes.com{url}'.format(url=url))
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

        json.dump({'recipes': recipes}, open('recipes.json', 'w'), ensure_ascii=False)
        self.stdout.write(self.style.SUCCESS('Collected {} recipes'.format(len(recipes))))

    def _scrape_urls(self):

        recipe_urls = []

        page = 1

        while True:
            url = 'https://cooking.nytimes.com/search?page={page}'.format(page=page)
            response = requests.get(url)
            data = response.content
            html = etree.HTML(data)
            articles = html.xpath('//article')
            self.stdout.write(self.style.SUCCESS('Fetched {} with {} recipes'.format(url, len(articles))))
            if articles:
                for article in articles:
                    url = article.attrib['data-url']
                    recipe_urls.append(url)
            else:  # no more pages
                break
            page += 1

        # save output as json file
        json.dump({'urls': recipe_urls}, open('urls.json', 'w'))
        self.stdout.write(self.style.SUCCESS('Complete'))

    def _validate_recipes_json_file(self):
        urls_file = os.path.join(settings.BASE_DIR, 'recipes.json')

        # validate the input urls file
        if not os.path.exists(urls_file):
            self.stdout.write(self.style.ERROR('"recipes.json" does not exist.  Run with --recipes first'))
            sys.exit(1)

        return urls_file

    def _recipe_exists(self, slug):
        return Recipe.objects.filter(slug=slug).exists()
