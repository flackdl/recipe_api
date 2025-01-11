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


class JustTheRecipeSerializer(serializers.Serializer):
    name = serializers.CharField(source='title')
    image_path = serializers.CharField(source='image')
    description = serializers.CharField()
    total_time_string = serializers.CharField(source='total_time')
    rating_value = serializers.IntegerField(source='ratings')
    rating_count = serializers.IntegerField(source='ratings_count')
    ingredients = serializers.ListField()
    instructions = serializers.ListField(source='instructions_list')
