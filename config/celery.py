"""The Celery app. Workers and the scheduler start from here: celery -A config worker, celery -A config beat."""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("camlobe")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.on_after_configure.connect
def schedule_feeds(sender, **kwargs):
    """One periodic task per entry of CAMLOBE_FEED_SCHEDULE. Nothing is scheduled by default."""
    from django.conf import settings

    for name, entry in settings.CAMLOBE_FEED_SCHEDULE.items():
        minute, hour, dom, month, dow = entry["crontab"].split()
        sender.add_periodic_task(
            crontab(minute=minute, hour=hour, day_of_month=dom, month_of_year=month, day_of_week=dow),
            ingest_feed_task.s(entry["supplier"], entry["source"]),
            name=name,
        )


from catalog.tasks import ingest_feed_task  # noqa: E402  (imported last: the app must exist first)
