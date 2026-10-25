from rest_framework import serializers

from ..models import Fitment, Part, PartSource, PipelineRun, ReviewItem

CURRENCY = "USD"  # every cost in this lab is an integer number of US cents


class SourceSerializer(serializers.ModelSerializer):
    supplier = serializers.CharField(source="supplier.code")
    currency = serializers.SerializerMethodField()

    class Meta:
        model = PartSource
        fields = ["supplier", "supplier_part_number", "cost_cents", "currency", "linked_by", "last_seen"]

    def get_currency(self, obj) -> str:
        return CURRENCY


class FitmentSerializer(serializers.ModelSerializer):
    year = serializers.IntegerField(source="vehicle.year")
    make = serializers.CharField(source="vehicle.make")
    model = serializers.CharField(source="vehicle.model")
    submodel = serializers.CharField(source="vehicle.submodel")
    engine = serializers.CharField(source="vehicle.engine")

    class Meta:
        model = Fitment
        fields = ["year", "make", "model", "submodel", "engine", "position", "note"]


class PartDetailSerializer(serializers.ModelSerializer):
    number = serializers.CharField(source="number_norm")
    sources = SourceSerializer(many=True, read_only=True)
    fitment = FitmentSerializer(source="fitments", many=True, read_only=True)

    class Meta:
        model = Part
        fields = ["id", "brand", "number", "description", "gtin", "sources", "fitment"]


class FitmentPartSerializer(serializers.ModelSerializer):
    part_id = serializers.IntegerField(source="part.id")
    brand = serializers.CharField(source="part.brand")
    number = serializers.CharField(source="part.number_norm")
    description = serializers.CharField(source="part.description")
    year = serializers.IntegerField(source="vehicle.year")
    submodel = serializers.CharField(source="vehicle.submodel")
    engine = serializers.CharField(source="vehicle.engine")

    class Meta:
        model = Fitment
        fields = ["part_id", "brand", "number", "description", "position", "note", "year", "submodel", "engine"]


class FeedRunSerializer(serializers.ModelSerializer):
    supplier = serializers.CharField(source="supplier.code", default=None, read_only=True)
    counts = serializers.SerializerMethodField()
    rejected_rows = serializers.SerializerMethodField()

    class Meta:
        model = PipelineRun
        fields = ["id", "kind", "supplier", "source_ref", "status", "started_at", "finished_at", "counts", "rejected_rows", "errors", "notes"]

    def get_counts(self, obj) -> dict:
        return {
            "read": obj.rows_read,
            "written": obj.rows_written,
            "unchanged": obj.rows_unchanged,
            "rejected": obj.rows_rejected,
        }

    def get_rejected_rows(self, obj) -> dict:
        """How many rows were refused, by reason (a run's detail; the list view skips the extra query)."""
        if not self.context.get("detail"):
            return {}
        out: dict[str, int] = {}
        for reason in obj.rejected.values_list("reason", flat=True):
            out[reason] = out.get(reason, 0) + 1
        return out


class ReviewItemSerializer(serializers.ModelSerializer):
    supplier = serializers.CharField(source="supplier.code")
    supplier_part_number = serializers.CharField(source="norm_row.raw.part_number_raw")
    brand_as_received = serializers.CharField(source="norm_row.raw.brand_raw")

    class Meta:
        model = ReviewItem
        fields = ["id", "reason", "supplier", "supplier_part_number", "brand_as_received", "candidate_part_ids", "status", "created_at"]
