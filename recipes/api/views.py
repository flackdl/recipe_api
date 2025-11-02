import requests
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from recipe_scrapers import scrape_html

from recipes.api.serializers import JustTheRecipeSerializer

CACHE_MINUTE = 60
CACHE_HOUR = CACHE_MINUTE * 60
CACHE_HALF_DAY = CACHE_HOUR * 12
CACHE_DAY = CACHE_HALF_DAY * 2

# TODO - rate limit
# https://django-ratelimit.readthedocs.io/en/stable/

class JustTheRecipeView(APIView):
    permission_classes = (AllowAny,)

    # try and mimic a browser
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    }

    @method_decorator(cache_page(CACHE_DAY))
    def get(self, request):
        # validate url exists
        if 'url' not in request.GET:
            raise ValidationError({'url': "Missing 'url' parameter"})
        url = request.GET['url']
        # validate url looks correct
        if not url.startswith('http'):
            raise ValidationError({'url': "Invalid 'url' parameter"})
        # retrieve the recipe webpage HTML
        req = requests.get(url, headers=self.headers, timeout=20)
        if req.status_code < 200 or req.status_code >= 300:
            raise ValidationError({
                'url': 'URL returned an error',
                'status_code': req.status_code,
                'content': req.content,
            })
        html = req.content
        # pass the html alongside the url to our scrape_html function
        try:
            scraper = scrape_html(html, org_url=url, wild_mode=True)
        except Exception:
            raise ValidationError({'url': 'Could not parse recipe from url'})
        # return original format
        if 'original' in request.GET:
            return Response(scraper.to_json())
        # return serialized version
        recipe = JustTheRecipeSerializer(scraper.to_json(), many=False).data
        return Response(recipe)
