from django.contrib import admin
from recipes.models import Category, Recipe


class RecipeInlineAdmin(admin.TabularInline):
    model = Recipe
    extra = 0


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    search_fields = ('name', 'type',)
    list_display = ('name', 'type',)
    list_filter = ('type',)


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    search_fields = ('name',)
    list_display = ('name', 'date_added',)
    list_filter = ('categories',)
