FROM python:3.11-alpine
ADD . /app
WORKDIR /app
RUN pip install -U pip
RUN pip install -r requirements.txt
RUN python manage.py migrate
RUN python manage.py collectstatic --no-input
RUN chmod +x docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
