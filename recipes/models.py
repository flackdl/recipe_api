from django.contrib.postgres import fields
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import Index


class Recipe(models.Model):
    name = models.CharField(max_length=500)
    # TODO - pointless if not unique (use their slug vs generating one and can link correctly)
    slug = models.SlugField(max_length=200, unique=True)
    image_path = models.CharField(max_length=210, null=True, blank=True)
    description = models.TextField()
    total_time = models.IntegerField(null=True, blank=True)  # minutes
    servings = models.CharField(max_length=100)
    rating_value = models.IntegerField(null=True, blank=True)
    rating_count = models.IntegerField(null=True, blank=True)
    ingredients = fields.ArrayField(base_field=models.CharField(max_length=1500))
    instructions = fields.ArrayField(base_field=models.CharField(max_length=3000))
    categories = models.ManyToManyField('Category')
    cuisines = models.ManyToManyField('Cuisine')
    author = models.CharField(max_length=100)
    search_vector = SearchVectorField(null=True)  # postgres search vector populated after creation

    class Meta:
        indexes = [
            GinIndex(fields=['search_vector']),
            Index(fields=['slug']),
            Index(fields=['rating_value']),
            Index(fields=['rating_count']),
        ]

    def __str__(self):
        return self.name


class Cuisine(models.Model):
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        verbose_name_plural = 'categories'

    def __str__(self):
        return self.name
