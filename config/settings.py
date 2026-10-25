"""Settings for the local lab. Every value that differs between machines comes from the environment."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# A lab value, never a production one: the stack only listens on localhost.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "camlobe-local-lab-key-not-a-secret")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.staticfiles",
    "rest_framework",
    "catalog",
]
MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
ROOT_URLCONF = "config.urls"
TEMPLATES = []
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("CAMLOBE_DB_NAME", "camlobe"),
        "USER": os.environ.get("CAMLOBE_DB_USER", "camlobe"),
        "PASSWORD": os.environ.get("CAMLOBE_DB_PASSWORD", "camlobe"),
        "HOST": os.environ.get("CAMLOBE_DB_HOST", "localhost"),
        "PORT": os.environ.get("CAMLOBE_DB_PORT", "5434"),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
STATIC_URL = "static/"

REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PERMISSION_CLASSES": [],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
}

# Search: "meili" (the Meilisearch index) or "db" (a plain SQL lookup on the same normalised columns).
SEARCH_BACKEND = os.environ.get("CAMLOBE_SEARCH_BACKEND", "meili")
MEILI_URL = os.environ.get("CAMLOBE_MEILI_URL", "http://localhost:7701")
MEILI_KEY = os.environ.get("CAMLOBE_MEILI_KEY", "camlobe-local-master-key")
MEILI_INDEX = os.environ.get("CAMLOBE_MEILI_INDEX", "parts")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"catalog": {"handlers": ["console"], "level": "WARNING"}},
}
