from django.core.management.base import BaseCommand, CommandError

from catalog.models import Part
from catalog.plans import check_plans


class Command(BaseCommand):
    help = "EXPLAIN the queries the pipeline and API depend on and fail if one stops using its index."

    def add_arguments(self, parser):
        parser.add_argument("--sample", type=int, default=2000, help="how many keys to look up per query")

    def handle(self, *args, **opts):
        if not Part.objects.exists():
            raise CommandError("no parts yet: ingest, normalize and match first")
        results = check_plans(opts["sample"])
        for r in results:
            self.stdout.write(f"{'ok  ' if r.ok else 'FAIL'} {r.ms:8.2f} ms  {r.name}  [{' > '.join(r.nodes[:3])}]")
            for problem in r.problems:
                self.stdout.write(f"       {problem}")
        failed = [r for r in results if not r.ok]
        if failed:
            raise CommandError(f"{len(failed)} of {len(results)} query plans failed")
        self.stdout.write(f"{len(results)} of {len(results)} query plans use their indexes")
