import io
import time
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from catalog.demo.generator import write_feeds
from catalog.ingest.adapters import FileAdapter
from catalog.ingest.runner import ingest
from catalog.matching import match_pending
from catalog.fitment import load_fitment
from catalog.models import Fitment, NormalizedRow, Part, PartSource, PipelineRun, RawPart, RejectedRow, ReviewItem, Supplier
from catalog.normalize_step import normalize_pending
from catalog.plans import check_plans


class Command(BaseCommand):
    help = "End to end on invented data: generate three messy feeds, ingest, normalize, match, load fitment, check query plans, index. Then run it all again."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="demo-data")
        parser.add_argument("--size", type=int, default=20000)
        parser.add_argument("--seed", type=int, default=7)
        parser.add_argument("--no-search", action="store_true", help="skip the Meilisearch rebuild")
        parser.add_argument("--brief", action="store_true", help="print one line per stage (this is what the README GIF shows)")

    def say(self, text):
        if not self.brief:
            self.stdout.write(text)

    def step(self, label, fn):
        started = time.time()
        result = fn()
        self.say(f"  {time.time() - started:6.1f} s  {label}")
        return result

    def handle(self, *args, **opts):
        if RawPart.objects.exists():
            raise CommandError("the database already holds rows: use an empty one (docker compose down -v, up -d, migrate)")
        self.brief = opts["brief"]
        out = Path(opts["out"])
        first_pass: list = []
        second_pass: list = []
        self.say(f"1. invented feeds, {opts['size']} canonical parts, seed {opts['seed']}")
        counts = self.step("write feeds", lambda: write_feeds(out, size=opts["size"], seed=opts["seed"]))
        for name, n in counts.items():
            self.say(f"         {name}: {n} rows")
        feeds = [
            ("redline", "Redline Distribution", "redline_catalog.csv"),
            ("harbor", "Harbor Parts Co", "harbor_catalog.xml"),
            ("delta", "Delta Wholesale", "delta_2026-10-01.csv"),
            ("delta", "Delta Wholesale", "delta_2026-10-15.csv"),
            ("delta", "Delta Wholesale", "delta_2026-10-22.csv"),
            ("delta", "Delta Wholesale", "delta_2026-10-29_unmapped.csv"),
        ]
        for pass_no in (1, 2):
            self.say(f"\n{pass_no + 1}. pass {pass_no}: ingest, normalize, match" + ("  (everything again: nothing should change)" if pass_no == 2 else ""))
            for code, name, filename in feeds:
                supplier, _ = Supplier.objects.get_or_create(code=code, defaults={"name": name})
                run = self.step(f"ingest {filename}", lambda: ingest(supplier, FileAdapter(out / filename)))
                (first_pass if pass_no == 1 else second_pass).append((filename, run))
                line = f"         {run.status}: read={run.rows_read} stored={run.rows_written} unchanged={run.rows_unchanged} rejected={run.rows_rejected}"
                if run.errors:
                    line += f"  error: {run.errors[0]['message'][:90]}"
                if "skipped" in run.notes:
                    line += "  (source unchanged)"
                self.say(line)
            norm = self.step("normalize", normalize_pending)
            match = self.step("match", match_pending)
            if pass_no == 1:
                first_match = match
            else:
                second_match = match
            self.say(f"         normalized {norm.rows_written}, matched or created {match.rows_written}, refreshed {match.rows_unchanged}, queued for review {match.rows_rejected}")
        self.say("\n4. fitment")
        fit = self.step("load fitment_aces.csv", lambda: load_fitment(out / "fitment_aces.csv"))
        self.say(f"         read={fit.rows_read} new={fit.rows_written} rejected={fit.rows_rejected}")
        again = load_fitment(out / "fitment_aces.csv")
        fit_again = again
        self.say(f"         the same file again: new={again.rows_written} already_there={again.rows_unchanged}")
        self.say("\n5. query plans")
        results = self.step("EXPLAIN ANALYZE on the hot queries", lambda: check_plans(300))
        for r in results:
            self.say(f"         {'ok  ' if r.ok else 'FAIL'} {r.ms:7.2f} ms  {r.name}")
        if not opts["no_search"]:
            self.say("\n6. search index")
            try:
                sink = io.StringIO() if self.brief else self.stdout
                call_command("rebuild_search_index", stdout=sink)
                call_command("sync_search_index", stdout=sink)
            except CommandError as exc:
                self.say(f"         skipped: {exc}")
        self.say("\nSUMMARY")
        self.say(f"  raw rows (as received, append only): {RawPart.objects.count()}")
        self.say(f"  rejected rows with a reason:         {RejectedRow.objects.count()}")
        self.say(f"  canonical parts:                     {Part.objects.count()}")
        self.say(f"  supplier listings linked:            {PartSource.objects.count()}")
        self.say(f"  parts listed by 2 or more suppliers: {self.multi()}")
        self.say(f"  waiting in the review queue:         {ReviewItem.objects.filter(status='pending').count()}")
        self.say(f"  fitment rows:                        {Fitment.objects.count()}")
        self.say(f"  pipeline runs recorded:              {PipelineRun.objects.count()}")
        self.say(f"  normalized rows still pending match: {NormalizedRow.objects.filter(match_state='pending').count()}")

        if self.brief:
            self.stdout.write(self.brief_text(first_pass, second_pass, first_match, second_match, fit, fit_again, results))

    def brief_text(self, first, second, first_match, second_match, fit, fit_again, results) -> str:
        lines = ["invented data: 3 suppliers, 6 files, Postgres 17"]
        for filename, run in first:
            if run.status == "failed":
                lines.append(f"ingest {filename:29} REFUSED: column renamed to 'producer'")
            else:
                lines.append(f"ingest {filename:29} stored {run.rows_written:>5}, same {run.rows_unchanged:>5}, bad {run.rows_rejected:>3}")
        lines.append(f"match  {Part.objects.count()} parts from {PartSource.objects.count()} listings, {first_match.rows_rejected} queued")
        lines.append(f"again  stored {sum(r.rows_written for _, r in second)}, matched {second_match.rows_written}: nothing changed")
        lines.append(f"fitment {fit.rows_written} rows, again {fit_again.rows_written} new")
        lines.append(f"plans  {sum(r.ok for r in results)} of {len(results)} queries use their indexes")
        return chr(10).join(lines)

    def multi(self) -> int:
        from django.db.models import Count

        return Part.objects.annotate(n=Count("sources__supplier", distinct=True)).filter(n__gte=2).count()
