import os
from django.conf import settings
from django.shortcuts import HttpResponse, redirect, resolve_url


def main(request):
    # return the raw file vs rendering it since it's all vue/javascript
    with open(os.path.join(settings.BASE_DIR, 'static', 'index.html')) as fp:
        return HttpResponse(fp.read())


def just_the_recipe(request):
    # redirect to "just the recipe" vue page when a url path is provided as the main path
    # https://example.com/http://recipes.com/recipe/123 => https://example.com/#/just-the-recipe?url=http://recipes.com/recipe/123
    recipe_url = request.path.lstrip('/')
    redirect_url =  f'{resolve_url("main")}#/just-the-recipe?url={recipe_url}'
    return redirect(redirect_url)
