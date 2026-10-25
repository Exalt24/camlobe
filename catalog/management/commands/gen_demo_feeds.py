from pathlib import Path

from django.core.management.base import BaseCommand

from catalog.demo.generator import write_feeds


class Command(BaseCommand):
    help = "Write invented, messy supplier feeds (and a truth file) for three made-up suppliers."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="demo-data")
        parser.add_argument("--size", type=int, default=20000, help="canonical parts in the invented universe")
        parser.add_argument("--seed", type=int, default=7)

    def handle(self, *args, **opts):
        counts = write_feeds(Path(opts["out"]), size=opts["size"], seed=opts["seed"])
        for name, n in counts.items():
            self.stdout.write(f"{name}: {n} rows")
