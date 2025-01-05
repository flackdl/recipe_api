from django.contrib.postgres import fields
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import Index


class Recipe(models.Model):
    name = models.CharField(max_length=500)
    slug = models.SlugField(max_length=200, unique=True)
    image_path = models.CharField(max_length=210, null=True, blank=True)
    description = models.TextField()
    total_time_string = models.CharField(null=True, blank=True, max_length=100)  # human readable, e.g "1 hour"
    servings = models.CharField(max_length=100)
    rating_value = models.IntegerField(null=True, blank=True)
    rating_count = models.IntegerField(null=True, blank=True)
    ingredients = fields.ArrayField(base_field=models.CharField(max_length=1500))
    instructions = fields.ArrayField(base_field=models.CharField(max_length=3000))
    categories = models.ManyToManyField('Category')
    author = models.CharField(max_length=100)
    search_vector = SearchVectorField(null=True)  # postgres search vector populated after creation
    date_added = models.DateField(auto_now_add=True)

    class Meta:
        indexes = [
            GinIndex(fields=['search_vector']),
            Index(fields=['slug']),
            Index(fields=['rating_value']),
            Index(fields=['rating_count']),
        ]

    def __str__(self):
        return self.name


class Category(models.Model):

    TYPE_SPECIAL_DIET = 'special_diets'
    TYPE_CUISINES = 'cuisines'
    TYPE_MEAL_TYPES = 'meal_types'
    TYPE_DISH_TYPES = 'dish_types'
    TYPE_UNKNOWN = '_UNKNOWN_'
    TYPES = [TYPE_SPECIAL_DIET, TYPE_CUISINES, TYPE_MEAL_TYPES, TYPE_DISH_TYPES, TYPE_UNKNOWN]

    name = models.CharField(max_length=100, unique=True)
    type = models.CharField(max_length=50, choices=zip(TYPES, TYPES))

    class Meta:
        verbose_name_plural = 'categories'
        ordering = ('name',)

    def __str__(self):
        return self.name
