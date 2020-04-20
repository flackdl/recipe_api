from django.contrib import admin
from recipes.models import Category, Recipe, Cuisine


class RecipeInlineAdmin(admin.TabularInline):
    model = Recipe
    extra = 0


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    pass


@admin.register(Cuisine)
class CuisineAdmin(admin.ModelAdmin):
    inlines = (RecipeInlineAdmin,)


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    pass
