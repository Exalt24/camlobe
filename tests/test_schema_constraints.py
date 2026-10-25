"""The database refuses what the code is also written never to do. Each constraint is tested by breaking it on purpose."""
import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from catalog.models import (
    FeedCheckpoint,
    Fitment,
    NormalizedRow,
    Part,
    PartSource,
    RawPart,
    ReviewItem,
    Vehicle,
)

from .helpers import get_supplier, row, run_all, write_csv

pytestmark = pytest.mark.django_db


@pytest.fixture
def loaded(tmp_path):
    s = get_supplier()
    run_all(s, write_csv(tmp_path / "f.csv", [row("H4123A"), row("H4124A", "")]))
    return s


def refuses(fn):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            fn()


def test_one_part_per_brand_and_number(loaded):
    part = Part.objects.get()
    refuses(lambda: Part.objects.create(brand=part.brand, number_norm=part.number_norm, updated_at=timezone.now()))
    Part.objects.create(brand="HALDEN", number_norm=part.number_norm, updated_at=timezone.now())  # another brand is fine


def test_one_listing_per_supplier(loaded):
    source = PartSource.objects.get()
    refuses(
        lambda: PartSource.objects.create(
            part=source.part, supplier=source.supplier, listing_key=source.listing_key, raw_part=source.raw_part,
            supplier_part_number="x", linked_by="exact", last_seen=timezone.now(),
        )
    )


def test_one_review_item_per_listing(loaded):
    item = ReviewItem.objects.get()
    refuses(
        lambda: ReviewItem.objects.create(
            supplier=item.supplier, listing_key=item.listing_key, norm_row=item.norm_row, reason="no_brand"
        )
    )


def test_one_checkpoint_per_source(loaded):
    refuses(lambda: FeedCheckpoint.objects.create(supplier=loaded, source_ref="f.csv"))


def test_one_normalized_row_per_raw_row(loaded):
    existing = NormalizedRow.objects.first()
    refuses(
        lambda: NormalizedRow.objects.create(
            raw=existing.raw, supplier=loaded, status="ok", brand="X", number_norm="Y", version=1
        )
    )


def test_one_fitment_row_and_one_vehicle_row(loaded):
    part = Part.objects.get()
    vehicle = Vehicle.objects.create(year=2019, make="ALTAIR", model="T200", submodel="", engine="")
    refuses(lambda: Vehicle.objects.create(year=2019, make="ALTAIR", model="T200", submodel="", engine=""))
    Fitment.objects.create(part=part, vehicle=vehicle, position="front")
    refuses(lambda: Fitment.objects.create(part=part, vehicle=vehicle, position="front"))
    Fitment.objects.create(part=part, vehicle=vehicle, position="rear")  # a different position is a different fit


def test_a_part_with_sources_cannot_be_deleted_by_accident(loaded):
    with pytest.raises(Exception, match="protected"):
        Part.objects.get().delete()


def test_raw_rows_keep_their_run(loaded):
    raw = RawPart.objects.first()
    with pytest.raises(Exception, match="protected|append only"):
        raw.run.delete()
