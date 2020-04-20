import os
import shutil
import sys
import json
import logging
import requests
from lxml import etree
from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.utils.text import slugify
from django.core.management.base import BaseCommand, CommandError

from recipes.models import Recipe, Category, Cuisine


class Command(BaseCommand):
    help = 'Scrape NYT recipes'
    pages = 422

    def add_arguments(self, parser):
        parser.add_argument('--urls', action='store_true', help='Captures all recipe urls')
        parser.add_argument('--recipes', action='store_true', help='Captures all recipes')
        parser.add_argument('--images', action='store_true', help='Downloads all recipe images')
        parser.add_argument('--ingest', action='store_true', help='Ingests recipes into db')

    def handle(self, *args, **options):

        # validate args
        if not any([options['urls'], options['recipes'], options['images'], options['ingest']]):
            raise CommandError('Missing argument')

        if options['urls']:
            self._scrape_urls()
        elif options['recipes']:
            self._scrape_recipes()
        elif options['images']:
            self._scrape_images()
        elif options['ingest']:
            self._ingest_recipes()

    def _ingest_recipes(self):

        # retrieve and validate recipe file
        recipe_file = self._validate_recipes_json_file()
        recipes = json.load(open(recipe_file))

        # delete existing records
        Recipe.objects.all().delete()
        Category.objects.all().delete()
        Cuisine.objects.all().delete()

        # import
        for i, recipe in enumerate(recipes['recipes']):
            # set recipe slug
            slug = slugify(recipe['name'])

            # set image path if it exists
            rel_image_path = os.path.join('static', 'recipes', '{}.jpg'.format(slug))
            abs_image_path = os.path.join(settings.BASE_DIR, rel_image_path)

            # set cuisine
            cuisine, _ = Cuisine.objects.get_or_create(name=recipe['recipeCuisine']) if recipe['recipeCuisine'] else (None, False)

            # create recipe
            recipe_obj = Recipe.objects.create(
                name=recipe['name'],
                slug=slug,
                image_path=rel_image_path if os.path.exists(abs_image_path) else None,
                description=recipe['description'],
                total_time=0,  # TODO - parse "PT45M" for 45 minutes
                servings=recipe['recipeYield'],
                rating=recipe['aggregateRating']['ratingValue'] if recipe['aggregateRating'] else None,
                ingredients=recipe['recipeIngredient'],
                instructions=[x['text'] for x in recipe['recipeInstructions']],
                cuisine=cuisine,
            )

            # assign categories (they're csv strings)
            categories = recipe['recipeCategory'].split(',')
            for category in categories:
                if not category:
                    continue
                cat_obj, _ = Category.objects.get_or_create(name=category)
                recipe_obj.categories.add(cat_obj)
                recipe_obj.save()

            if i != 0 and i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Ingested {} recipes'.format(i)))

        # add search vector
        vector = SearchVector('name', weight='A') + SearchVector('ingredients', weight='B')
        Recipe.objects.update(search_vector=vector)

        self.stdout.write(self.style.SUCCESS('Complete'))

    def _scrape_images(self):
        recipe_file = self._validate_recipes_json_file()

        recipes = json.load(open(recipe_file))
        for i, recipe in enumerate(recipes['recipes']):
            try:
                response = requests.get(recipe['image'], stream=True)
                response.raise_for_status()
            except Exception as e:
                logging.exception(e)
                self.stdout.write(self.style.ERROR('Could not download image {} for {}'.format(recipe['image'], recipe['name'])))
                continue
            image_name = '{}.jpg'.format(slugify(recipe['name']))
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

        self.stdout.write(self.style.SUCCESS('Scraping recipes'))

        recipes = []

        for i, url in enumerate(json.load(open(urls_file, 'r'))['urls']):

            response = requests.get('https://cooking.nytimes.com{url}'.format(url=url))
            try:
                html = etree.HTML(response.content)
                recipe_json = html.xpath('//script[@type="application/ld+json"]')[0]
            except Exception as e:
                logging.warning('ERROR parsing #{} of url {}'.format(i, url))
                logging.exception(e)
                continue

            if i != 0 and i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Scraped {} recipes'.format(i)))

            recipes.append(json.loads(recipe_json.text))

        json.dump({'recipes': recipes}, open('recipes.json', 'w'))
        self.stdout.write(self.style.SUCCESS('Complete'))

    def _scrape_urls(self):

        recipe_urls = []

        for page in range(1, self.pages + 1):
            url = 'https://cooking.nytimes.com/search?page={page}'.format(page=page)
            response = requests.get(url)
            data = response.content
            html = etree.HTML(data)
            articles = html.xpath('//article')
            self.stdout.write(self.style.SUCCESS('Fetched {} with {} recipes'.format(url, len(articles))))
            for article in articles:
                url = article.attrib['data-url']
                recipe_urls.append(url)

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
