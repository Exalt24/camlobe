from django.core.management.base import BaseCommand, CommandError

from catalog.matching import match_pending
from catalog.models import PipelineRun
from catalog.runs import AlreadyRunning


class Command(BaseCommand):
    help = "Group normalized rows into canonical parts. Ambiguous ones go to the review queue. Restartable."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=2000)
        parser.add_argument("--limit", type=int, default=None, help="stop after this many rows")

    def handle(self, *args, **opts):
        try:
            run = match_pending(batch_size=opts["batch_size"], limit=opts["limit"])
        except AlreadyRunning:
            raise CommandError("another match run holds the lock")
        self.stdout.write(
            f"match run {run.pk} {run.status}: read={run.rows_read} linked={run.rows_written} "
            f"refreshed={run.rows_unchanged} queued_for_review={run.rows_rejected} "
            f"new_parts={run.notes.get('created', 0)}"
        )
        if run.status != PipelineRun.Status.SUCCEEDED:
            raise CommandError(f"run {run.pk} {run.status}: {run.errors[:1]}")
