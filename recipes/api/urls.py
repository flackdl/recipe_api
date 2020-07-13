from django.urls import include, path
from rest_framework import routers
from recipes.api import viewsets

router = routers.DefaultRouter()
router.register('category', viewsets.CategoryViewSet)
router.register('recipe', viewsets.RecipeViewSet)

urlpatterns = [
    path('', include(router.urls)),
]
