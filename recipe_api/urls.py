"""recipe_api URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/3.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include, re_path
from recipes import views
import recipes.api.urls

urlpatterns = [
    # admin
    path('admin/', admin.site.urls),
    # api auth
    path('api-auth/', include('rest_framework.urls')),
    # api
    path('api/', include(recipes.api.urls)),
    # just the recipe - redirect
    re_path('^http.*', views.just_the_recipe),
    # main
    path('', views.main, name='main')
]
