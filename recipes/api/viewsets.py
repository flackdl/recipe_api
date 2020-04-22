from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets
from recipes.api.filters import SearchVectorFilter
from recipes.api.serializers import CategorySerializer, RecipeSerializer, CuisineSerializer
from recipes.models import Category, Recipe, Cuisine


class CuisineViewSet(viewsets.ModelViewSet):
    queryset = Cuisine.objects.all()
    serializer_class = CuisineSerializer
    filterset_fields = ['name']
    search_fields = ['name']


class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    filterset_fields = ['name']
    search_fields = ['name']


class RecipeViewSet(viewsets.ModelViewSet):
    queryset = Recipe.objects.all()
    serializer_class = RecipeSerializer
    filterset_fields = ['name', 'rating_value', 'cuisines', 'categories']
    filter_backends = (SearchVectorFilter, DjangoFilterBackend)
    search_fields = ['search_vector']
