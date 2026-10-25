from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from catalog.ingest.adapters import FileAdapter
from catalog.ingest.runner import ingest
from catalog.models import PipelineRun, Supplier
from catalog.runs import AlreadyRunning


class Command(BaseCommand):
    help = (
        "Ingest one feed for one supplier. Restartable: it resumes at its checkpoint, and running it again "
        "over the same file changes nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument("supplier", help="supplier code, created on first use")
        parser.add_argument("--name", default="", help="display name for a new supplier")
        parser.add_argument("--file", help="a local CSV or XML file")
        parser.add_argument("--format", choices=["csv", "xml"], default=None)
        parser.add_argument("--batch-size", type=int, default=1000)

    def handle(self, *args, **opts):
        if not opts["file"]:
            raise CommandError("give a source: --file")
        path = Path(opts["file"])
        if not path.is_file():
            raise CommandError(f"no such file: {path}")
        supplier, _ = Supplier.objects.get_or_create(code=opts["supplier"], defaults={"name": opts["name"] or opts["supplier"]})
        adapter = FileAdapter(path, fmt=opts["format"])
        try:
            run = ingest(supplier, adapter, batch_size=opts["batch_size"])
        except AlreadyRunning:
            raise CommandError("another ingest of this source is running")
        self.stdout.write(
            f"ingest run {run.pk} {run.status}: read={run.rows_read} stored={run.rows_written} "
            f"unchanged={run.rows_unchanged} rejected={run.rows_rejected}"
            + (f" resumed_from={run.notes['resumed_from']}" if "resumed_from" in run.notes else "")
            + (f" ({run.notes['skipped']})" if "skipped" in run.notes else "")
        )
        if run.status != PipelineRun.Status.SUCCEEDED:
            for err in run.errors[:3]:
                self.stderr.write(f"  {err['where']}: {err['message']}")
            raise CommandError(f"run {run.pk} ended {run.status}")
