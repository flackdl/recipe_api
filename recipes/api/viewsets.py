from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.gzip import gzip_page
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets
from rest_framework.filters import OrderingFilter

from recipes.api.filters import SearchVectorFilter, RecipeFilter
from recipes.api.serializers import CategorySerializer, RecipeSerializer
from recipes.models import Category, Recipe


CACHE_HOUR = 60 * 60
CACHE_DAY = CACHE_HOUR * 24


@method_decorator(gzip_page, name='dispatch')
@method_decorator(cache_page(timeout=CACHE_DAY), name='dispatch')
class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    filterset_fields = ['name']
    search_fields = ['name']
    pagination_class = None


@method_decorator(gzip_page, name='dispatch')
@method_decorator(cache_page(timeout=CACHE_DAY), name='dispatch')
class RecipeViewSet(viewsets.ModelViewSet):
    queryset = Recipe.objects.all()
    serializer_class = RecipeSerializer
    filterset_class = RecipeFilter
    filter_backends = (SearchVectorFilter, DjangoFilterBackend, OrderingFilter)
    search_fields = ['search_vector']
    ordering_fields = ['rating_value', 'date_added']
