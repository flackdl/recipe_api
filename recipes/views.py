import os
from django.conf import settings
from django.shortcuts import HttpResponse


def main(request):
    # return the raw file vs rendering it since it's all vue/javascript
    return HttpResponse(open(os.path.join(settings.BASE_DIR, 'static', 'index.html')).read())
