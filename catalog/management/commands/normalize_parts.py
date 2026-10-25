from django.core.management.base import BaseCommand, CommandError

from catalog.models import PipelineRun
from catalog.normalize_step import normalize_pending
from catalog.runs import AlreadyRunning


class Command(BaseCommand):
    help = "Normalize raw rows that have no normalized row yet. Restartable; a second run finds nothing to do."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=2000)
        parser.add_argument("--limit", type=int, default=None, help="stop after this many raw rows")

    def handle(self, *args, **opts):
        try:
            run = normalize_pending(batch_size=opts["batch_size"], limit=opts["limit"])
        except AlreadyRunning:
            raise CommandError("another normalize run holds the lock")
        self.stdout.write(
            f"normalize run {run.pk} {run.status}: read={run.rows_read} normalized={run.rows_written} "
            f"rejected={run.rows_rejected}"
        )
        if run.status != PipelineRun.Status.SUCCEEDED:
            raise CommandError(f"run {run.pk} {run.status}: {run.errors[:1]}")
