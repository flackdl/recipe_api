FROM python:3.11-bullseye
ADD . /app
WORKDIR /app
RUN pip install -U pip
RUN pip install -r requirements.txt
RUN python manage.py collectstatic --no-input
ENTRYPOINT /app/docker-entrypoint.sh
