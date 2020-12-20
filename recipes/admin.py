from django.contrib import admin
from recipes.models import Category, Recipe


class RecipeInlineAdmin(admin.TabularInline):
    model = Recipe
    extra = 0


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'type',)


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display = ('name', 'date_added',)
    list_filter = ('categories',)
