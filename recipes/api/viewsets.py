from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_control
from django.views.decorators.gzip import gzip_page
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets
from recipes.api.filters import SearchVectorFilter
from recipes.api.serializers import CategorySerializer, RecipeSerializer, CuisineSerializer
from recipes.models import Category, Recipe, Cuisine


@method_decorator(gzip_page, name='dispatch')
@method_decorator(cache_control(max_age=3600), name='dispatch')
class CuisineViewSet(viewsets.ModelViewSet):
    queryset = Cuisine.objects.all()
    serializer_class = CuisineSerializer
    filterset_fields = ['name']
    search_fields = ['name']


@method_decorator(gzip_page, name='dispatch')
@method_decorator(cache_control(max_age=3600), name='dispatch')
class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    filterset_fields = ['name']
    search_fields = ['name']


@method_decorator(gzip_page, name='dispatch')
@method_decorator(cache_control(max_age=3600), name='dispatch')
class RecipeViewSet(viewsets.ModelViewSet):
    queryset = Recipe.objects.all()
    serializer_class = RecipeSerializer
    filterset_fields = ['name', 'rating_value', 'cuisines', 'categories']
    filter_backends = (SearchVectorFilter, DjangoFilterBackend)
    search_fields = ['search_vector']
