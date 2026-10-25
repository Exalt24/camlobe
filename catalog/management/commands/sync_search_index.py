from django.core.management.base import BaseCommand, CommandError

from catalog.models import PipelineRun
from catalog.runs import AlreadyRunning
from catalog.search.index import sync


class Command(BaseCommand):
    help = "Send the parts that changed since they were last indexed to Meilisearch. Safe to run as often as you like."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=1000)

    def handle(self, *args, **opts):
        try:
            run = sync(batch_size=opts["batch_size"])
        except AlreadyRunning:
            raise CommandError("another index run holds the lock")
        self.stdout.write(f"index sync run {run.pk} {run.status}: parts_read={run.rows_read} documents_sent={run.rows_written}")
        if run.status != PipelineRun.Status.SUCCEEDED:
            raise CommandError(f"run {run.pk} {run.status}: {run.errors[:1]}")
