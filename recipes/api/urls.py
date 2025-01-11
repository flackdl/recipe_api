from django.urls import include, path
from rest_framework import routers
from recipes.api import viewsets, views

router = routers.DefaultRouter()
router.register('category', viewsets.CategoryViewSet)
router.register('recipe', viewsets.RecipeViewSet)

urlpatterns = [
    path('', include(router.urls)),
    path(r'just-the-recipe/', views.JustTheRecipeView.as_view(), name='just-the-recipe'),
]
