FROM python:3.8-buster
ADD . /app
WORKDIR /app
RUN pip install -r requirements.txt
RUN python manage.py collectstatic --no-input
ENTRYPOINT /app/docker-entrypoint.sh
