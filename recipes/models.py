from django.contrib.postgres import fields
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models


class Recipe(models.Model):
    name = models.CharField(max_length=500, unique=True)
    slug = models.SlugField(max_length=200, unique=True)
    total_time = models.IntegerField()  # minutes
    servings = models.IntegerField()
    rating = models.IntegerField(null=True, blank=True)
    description = models.TextField()
    ingredients = fields.ArrayField(base_field=models.CharField(max_length=1500))
    search_vector = SearchVectorField(null=True)  # postgres search vector populated after creation

    class Meta:
        indexes = [
            GinIndex(fields=['search_vector']),
        ]

    def __str__(self):
        return self.name


class Cuisine(models.Model):
    name = models.CharField(max_length=100)
    recipes = models.ManyToManyField(Recipe)


class Category(models.Model):
    name = models.CharField(max_length=100)
    recipes = models.ManyToManyField(Recipe)
