#!/bin/sh
python manage.py migrate
python manage.py createcachetable
gunicorn recipe_api.wsgi:application -b :80 --workers=3 --threads=2 --keep-alive=2
