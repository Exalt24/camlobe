from django.core.management.base import BaseCommand, CommandError

from catalog.fitment import load_fitment
from catalog.models import PipelineRun
from catalog.runs import AlreadyRunning


class Command(BaseCommand):
    help = "Load vehicle fitment (ACES style CSV). Parts must already exist. Loading the same file again changes nothing."

    def add_arguments(self, parser):
        parser.add_argument("file")
        parser.add_argument("--batch-size", type=int, default=2000)

    def handle(self, *args, **opts):
        try:
            run = load_fitment(opts["file"], batch_size=opts["batch_size"])
        except AlreadyRunning:
            raise CommandError("this file is already being loaded")
        self.stdout.write(
            f"fitment run {run.pk} {run.status}: read={run.rows_read} new={run.rows_written} "
            f"already_there={run.rows_unchanged} rejected={run.rows_rejected}"
        )
        if run.status != PipelineRun.Status.SUCCEEDED:
            raise CommandError(f"run {run.pk} {run.status}: {run.errors[:1]}")
