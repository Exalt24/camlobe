from django.core.management.base import BaseCommand, CommandError

from catalog.models import PipelineRun
from catalog.runs import AlreadyRunning
from catalog.search.index import rebuild


class Command(BaseCommand):
    help = "Rebuild the Meilisearch index into a staging index and swap it in. Restartable: an interrupted build resumes."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=1000)
        parser.add_argument("--restart", action="store_true", help="throw away an interrupted build and start over")

    def handle(self, *args, **opts):
        try:
            run = rebuild(batch_size=opts["batch_size"], restart=opts["restart"])
        except AlreadyRunning:
            raise CommandError("another index run holds the lock")
        resumed = f" resumed_after_part_id={run.notes['resumed_after_part_id']}" if "resumed_after_part_id" in run.notes else ""
        self.stdout.write(f"index rebuild run {run.pk} {run.status}: parts_read={run.rows_read} documents_sent={run.rows_written}{resumed}")
        if run.status != PipelineRun.Status.SUCCEEDED:
            raise CommandError(f"run {run.pk} {run.status}: {run.errors[:1]}")
