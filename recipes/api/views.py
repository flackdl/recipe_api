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


class JustTheRecipeView(APIView):
    permission_classes = (AllowAny,)

    @method_decorator(cache_page(CACHE_DAY))
    def get(self, request):
        if 'url' not in request.GET:
            raise ValidationError({'url': 'Missing "url" parameter'})
        url = request.GET['url']
        # retrieve the recipe webpage HTML
        req = requests.get(url)
        req.raise_for_status()
        html = req.content
        # pass the html alongside the url to our scrape_html function
        scraper = scrape_html(html, org_url=url, wild_mode=True)
        # return original format
        if 'original' in request.GET:
            return Response(scraper.to_json())
        # return serialized version
        recipe = JustTheRecipeSerializer(scraper.to_json(), many=False).data
        return Response(recipe)
