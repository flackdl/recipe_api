import os
import sys
import json
import logging
import requests
from django.conf import settings
from lxml import etree
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Crawls recipes'
    pages = 422

    def add_arguments(self, parser):
        parser.add_argument('--urls', action='store_true')
        parser.add_argument('--recipes', action='store_true')

    def handle(self, *args, **options):

        # validate args
        if not any([options['urls'], options['recipes']]):
            raise CommandError('--urls or --recipes must be set')

        if options['urls']:
            self._scrape_urls()
        elif options['recipes']:
            self._scrape_recipes()

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

            if i % 100 == 0:
                self.stdout.write(self.style.SUCCESS('Scraped {} recipes'.format(i)))

            recipes.append(json.loads(recipe_json.text))

            ## TODO - REMOVE
            #if i > 100:
            #    break

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
