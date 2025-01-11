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
    # all fields are optional since we can't rely on their existence
    name = serializers.CharField(source='title', required=False)
    image_path = serializers.CharField(source='image', required=False)
    description = serializers.CharField(required=False)
    total_time_string = serializers.CharField(source='total_time', required=False)
    rating_value = serializers.IntegerField(source='ratings', required=False)
    rating_count = serializers.IntegerField(source='ratings_count', required=False)
    ingredients = serializers.ListField(required=False)
    instructions = serializers.ListField(source='instructions_list', required=False)
    servings = serializers.CharField(source='yields', required=False)
