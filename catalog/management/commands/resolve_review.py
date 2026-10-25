from django.core.management.base import BaseCommand, CommandError

from catalog.matching import resolve_review
from catalog.models import Part, ReviewItem


class Command(BaseCommand):
    help = "List the review queue, or resolve one item: --part ID, --new-part [--brand X], or --ignore."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true")
        parser.add_argument("--item", type=int)
        parser.add_argument("--part", type=int)
        parser.add_argument("--new-part", action="store_true")
        parser.add_argument("--brand", default="")
        parser.add_argument("--ignore", action="store_true")

    def handle(self, *args, **opts):
        if opts["list"] or not opts["item"]:
            for item in ReviewItem.objects.filter(status=ReviewItem.Status.PENDING).order_by("id")[:50]:
                self.stdout.write(
                    f"{item.pk} {item.reason} supplier={item.supplier_id} listing={item.listing_key} "
                    f"candidates={item.candidate_part_ids}"
                )
            return
        try:
            item = ReviewItem.objects.select_related("norm_row__raw").get(pk=opts["item"])
            part = Part.objects.get(pk=opts["part"]) if opts["part"] else None
            resolve_review(item, part=part, new_part=opts["new_part"], brand=opts["brand"], ignore=opts["ignore"])
        except (ReviewItem.DoesNotExist, Part.DoesNotExist, ValueError) as exc:
            raise CommandError(str(exc))
        self.stdout.write(f"item {item.pk} is now {item.status}")
