"""Tables of the catalog pipeline.

Layering, bottom to top: RawPart is what a supplier sent and is never changed (a database
trigger refuses UPDATE and DELETE). NormalizedRow is derived from it and can be recomputed.
Part is the canonical product; PartSource links one supplier's listing to it. ReviewItem holds
every match the rules refuse to decide alone.
"""
from django.db import models
from django.db.models import F, Func, Q, Value


class Supplier(models.Model):
    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)
    # True when the supplier's feed lists everything it offers: a listing missing from a complete run is delisted
    full_snapshot = models.BooleanField(default=False, db_default=False)

    def __str__(self) -> str:
        return self.code


class PipelineRun(models.Model):
    """The run table: one row per command run, with counts and the errors it hit."""

    class Kind(models.TextChoices):
        INGEST = "ingest"
        NORMALIZE = "normalize"
        MATCH = "match"
        INDEX = "index"
        FITMENT = "fitment"

    class Status(models.TextChoices):
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        PARTIAL = "partial"  # finished, but a part of the source could not be read
        FAILED = "failed"

    kind = models.CharField(max_length=12, choices=Kind.choices)
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.PROTECT, related_name="runs")
    source_ref = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    rows_read = models.IntegerField(default=0)
    rows_written = models.IntegerField(default=0)
    rows_unchanged = models.IntegerField(default=0)
    rows_rejected = models.IntegerField(default=0)
    errors = models.JSONField(default=list)  # capped list of {"where", "message"}
    notes = models.JSONField(default=dict)  # encoding, fingerprint, resumed_from, ...

    class Meta:
        indexes = [models.Index(fields=["kind", "-started_at"])]
        ordering = ["-id"]


class FeedCheckpoint(models.Model):
    """Where a source was read up to. Advanced in the same transaction as the rows it covers."""

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    source_ref = models.CharField(max_length=500)
    fingerprint = models.CharField(max_length=64, blank=True)
    position = models.BigIntegerField(default=0)
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)  # when the pass over the whole source finished
    updated_at = models.DateTimeField(auto_now=True)
    last_run = models.ForeignKey(PipelineRun, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["supplier", "source_ref"], name="one_checkpoint_per_source")]


class SnapshotSeen(models.Model):
    """A supplier part the pass over a snapshot feed read (kept for suppliers flagged full_snapshot only).

    Rows that did not change store no RawPart, so what a full feed still lists can only be known from this table.
    An empty part number is a marker: a row the pass could not read, so the pass cannot prove what is missing.
    """

    checkpoint = models.ForeignKey(FeedCheckpoint, on_delete=models.CASCADE, related_name="seen")
    part_number_raw = models.CharField(max_length=200, blank=True)
    brand_raw = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["checkpoint", "part_number_raw", "brand_raw"], name="one_seen_per_pass")]


class RejectedRow(models.Model):
    run = models.ForeignKey(PipelineRun, on_delete=models.CASCADE, related_name="rejected")
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.PROTECT)
    position = models.BigIntegerField()
    reason = models.CharField(max_length=60)
    raw = models.TextField(blank=True)


class RawPart(models.Model):
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="raw_parts")
    run = models.ForeignKey(PipelineRun, on_delete=models.PROTECT, related_name="raw_parts")
    position = models.BigIntegerField()
    # sha256(supplier | source | row position | content): the same source row can only land once.
    idempotency_key = models.CharField(max_length=64, unique=True)
    # sha256 of the row's content alone: a later file that repeats the row stores nothing new.
    content_hash = models.CharField(max_length=64)
    part_number_raw = models.CharField(max_length=200)
    brand_raw = models.CharField(max_length=200, blank=True)
    description_raw = models.TextField(blank=True)
    gtin_raw = models.CharField(max_length=60, blank=True)
    cost_cents = models.BigIntegerField(null=True, blank=True)
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["supplier", "part_number_raw", "-id"], name="rawpart_latest_idx")]


class NormalizedRow(models.Model):
    class Status(models.TextChoices):
        OK = "ok"
        REJECTED = "rejected"

    class MatchState(models.TextChoices):
        PENDING = "pending"
        MATCHED = "matched"
        REVIEW = "review"
        SKIPPED = "skipped"

    raw = models.OneToOneField(RawPart, on_delete=models.PROTECT, related_name="normalized")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    status = models.CharField(max_length=10, choices=Status.choices)
    reject_reason = models.CharField(max_length=60, blank=True)
    brand = models.CharField(max_length=80, blank=True)
    number_norm = models.CharField(max_length=40, blank=True)
    gtin = models.CharField(max_length=14, blank=True)
    rules = models.JSONField(default=list)
    version = models.IntegerField()
    match_state = models.CharField(max_length=8, choices=MatchState.choices, default=MatchState.PENDING)
    normalized_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["match_state", "id"], name="norm_pending_idx"),
            models.Index(fields=["version"], name="norm_version_idx"),
        ]


class Part(models.Model):
    brand = models.CharField(max_length=80)
    number_norm = models.CharField(max_length=40)
    # the number without its leading zeros, kept by the database: two parts of one brand that share it are zero variants of each other
    number_core = models.GeneratedField(
        expression=Func(F("number_norm"), Value("0"), function="LTRIM"), output_field=models.CharField(max_length=40), db_persist=True
    )
    description = models.TextField(blank=True)
    gtin = models.CharField(max_length=14, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField()
    # bumped on every change that shows in search; the sync sends parts where indexed_version < search_version
    search_version = models.IntegerField(default=1)
    indexed_version = models.IntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["brand", "number_norm"], name="one_part_per_brand_number")]
        indexes = [
            # varchar_pattern_ops serves both "= value" and "LIKE 'prefix%'", so one index covers match and search
            models.Index(fields=["number_norm"], name="part_number_idx", opclasses=["varchar_pattern_ops"]),
            models.Index(fields=["gtin"], name="part_gtin_idx"),
            models.Index(fields=["number_core"], name="part_core_idx"),
            models.Index(fields=["id"], name="part_search_dirty_idx", condition=Q(indexed_version__lt=F("search_version"))),
        ]


class PartSource(models.Model):
    """One supplier's listing of a part. A listing links to exactly one Part."""

    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="sources")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="listings")
    listing_key = models.CharField(max_length=130)  # "<brand>|<number_norm>"
    raw_part = models.ForeignKey(RawPart, on_delete=models.PROTECT, related_name="+")  # newest version seen
    supplier_part_number = models.CharField(max_length=200)
    cost_cents = models.BigIntegerField(null=True, blank=True)
    linked_by = models.CharField(max_length=20)  # rule name, or "review"
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField()
    # set when the supplier's latest complete snapshot no longer lists it; cleared when it lists it again
    delisted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["supplier", "listing_key"], name="one_listing_per_supplier")]


class ReviewItem(models.Model):
    class Reason(models.TextChoices):
        NO_BRAND = "no_brand"
        GTIN_CONFLICT = "gtin_conflict"
        GTIN_CROSS = "gtin_cross"
        ZERO_VARIANT = "zero_variant"

    class Status(models.TextChoices):
        PENDING = "pending"
        LINKED = "linked"
        NEW_PART = "new_part"
        IGNORED = "ignored"

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    listing_key = models.CharField(max_length=130)
    norm_row = models.ForeignKey(NormalizedRow, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=20, choices=Reason.choices)
    candidate_part_ids = models.JSONField(default=list)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    resolved_part = models.ForeignKey(Part, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["supplier", "listing_key"], name="one_review_per_listing")]
        indexes = [models.Index(fields=["status", "id"], name="review_status_idx")]


class Vehicle(models.Model):
    """A VCdb style vehicle: year, make, model, submodel and engine are all part of its identity."""

    year = models.SmallIntegerField()
    make = models.CharField(max_length=40)
    model = models.CharField(max_length=60)
    submodel = models.CharField(max_length=60, blank=True)
    engine = models.CharField(max_length=60, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["year", "make", "model", "submodel", "engine"], name="one_vehicle_row")
        ]


class Fitment(models.Model):
    """An ACES style application: this part fits this vehicle, at this position."""

    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="fitments")
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name="fitments")
    position = models.CharField(max_length=30, blank=True)  # "front", "rear", ...
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["part", "vehicle", "position"], name="one_fitment_row")]
        indexes = [models.Index(fields=["vehicle"], name="fitment_vehicle_idx")]


class SearchIndexState(models.Model):
    """Progress of a Meilisearch rebuild. The incremental sync needs no cursor: it reads Part.search_version."""

    index_uid = models.CharField(max_length=60, unique=True)
    status = models.CharField(max_length=12, default="idle")  # idle, building, ready
    rebuild_after_id = models.BigIntegerField(default=0)  # last Part id the rebuild has sent
    docs_sent = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)
