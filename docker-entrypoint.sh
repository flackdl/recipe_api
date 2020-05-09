python manage.py migrate
python manage.py createcachetable
gunicorn recipe_api.wsgi:application -b :80 --workers=4
