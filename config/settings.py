import json
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
    "DEFAULT_PAGINATION_CLASS": "catalog.api.pagination.CappedLimitOffsetPagination",
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

# Background work (Celery on Redis). A feed run is long and must not die with a web request.
CELERY_BROKER_URL = os.environ.get("CAMLOBE_REDIS_URL", "redis://localhost:6381/0")
CELERY_RESULT_BACKEND = os.environ.get("CAMLOBE_REDIS_URL", "redis://localhost:6381/0")
CELERY_TASK_ACKS_LATE = True            # a worker that dies mid run hands the job back to the broker
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1   # one long feed per worker slot, not a queue of them
CELERY_TASK_SOFT_TIME_LIMIT = 60 * 50
CELERY_TASK_TIME_LIMIT = 60 * 60
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# {"<name>": {"supplier": "<code>", "source": {"file": "<path>"}, "crontab": "0 3 * * *"}}: set from the environment as JSON.
CAMLOBE_FEED_SCHEDULE = json.loads(os.environ.get("CAMLOBE_FEED_SCHEDULE", "{}"))
