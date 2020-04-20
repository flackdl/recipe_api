from django.contrib.postgres import fields
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import Index


class Recipe(models.Model):
    name = models.CharField(max_length=500)
    slug = models.SlugField(max_length=200)
    image_path = models.CharField(max_length=210, null=True, blank=True)
    description = models.TextField()
    total_time = models.IntegerField()  # minutes
    servings = models.CharField(max_length=100)
    rating = models.IntegerField(null=True, blank=True)
    ingredients = fields.ArrayField(base_field=models.CharField(max_length=1500))
    instructions = fields.ArrayField(base_field=models.CharField(max_length=3000))
    categories = models.ManyToManyField('Category')
    cuisine = models.ForeignKey('Cuisine', null=True, blank=True, on_delete=models.CASCADE)
    search_vector = SearchVectorField(null=True)  # postgres search vector populated after creation

    class Meta:
        indexes = [
            GinIndex(fields=['search_vector']),
            Index(fields=['slug']),
            Index(fields=['rating']),
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
