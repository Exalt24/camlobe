import datetime as dt
import re

from django.core.management.base import BaseCommand, CommandError

from catalog.edi.orders import OrderError, purchase_order_for
from catalog.edi.x12 import X12Error, build_850


class Command(BaseCommand):
    help = "Print an X12 850 purchase order for parts one supplier lists. Made-up orders only: not tested against any trading partner."

    def add_arguments(self, parser):
        parser.add_argument("supplier")
        parser.add_argument("item", nargs="+", help="PART_ID:QTY")
        parser.add_argument("--po", required=True, help="purchase order number")
        parser.add_argument("--ship-to", default="")
        parser.add_argument("--out", help="write to a file instead of the screen")

    def handle(self, *args, **opts):
        items = []
        for raw in opts["item"]:
            try:
                part_id, qty = raw.split(":")
                items.append((int(part_id), int(qty)))
            except ValueError:
                raise CommandError(f"{raw!r} is not PART_ID:QTY")
        try:
            po = purchase_order_for(opts["supplier"], items, opts["po"], dt.date.today(), opts["ship_to"])
            # a supplier code is a slug (hyphens, underscores), an interchange id is letters, digits and spaces
            receiver = re.sub(r"[^A-Z0-9]", "", opts["supplier"].upper())[:15] or "SUPPLIER"
            # build_850 reads the document back before returning it, so what is printed reads clean or this is an X12Error
            text = build_850(po, sender="CAMLOBE LAB", receiver=receiver, when=dt.datetime.now())
        except (OrderError, X12Error, ValueError) as exc:
            raise CommandError(str(exc))
        if opts["out"]:
            with open(opts["out"], "w", encoding="ascii", newline="\n") as handle:
                handle.write(text)
            self.stdout.write(f"wrote {opts['out']}")
        else:
            self.stdout.write(text)
