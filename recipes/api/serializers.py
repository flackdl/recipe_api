from rest_framework import serializers
from recipes.models import Category, Recipe


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = '__all__'


class RecipeSerializer(serializers.ModelSerializer):
    search_rank = serializers.FloatField(read_only=True)

    class Meta:
        model = Recipe
        exclude = ('search_vector',)
