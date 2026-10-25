"""Read-only REST endpoints. No writes, no authentication: a lab API on made-up data, never exposed."""
from __future__ import annotations

from django.conf import settings
from django.db.models import Prefetch
from rest_framework import generics, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from ..fitment import canon_vehicle_text
from ..models import Fitment, Part, PartSource, PipelineRun, ReviewItem
from ..search import dbsearch, index
from .serializers import (
    FeedRunSerializer,
    FitmentPartSerializer,
    PartDetailSerializer,
    ReviewItemSerializer,
)

MAX_LIMIT = 100


def _int_param(request, name: str, default: int, low: int, high: int) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValidationError({name: "must be a whole number"})
    if not low <= value <= high:
        raise ValidationError({name: f"must be between {low} and {high}"})
    return value


class PartSearchView(APIView):
    """GET /api/parts/search/?q=...&brand=...&limit=...&offset=..."""

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        if not q:
            raise ValidationError({"q": "give something to search for"})
        if len(q) > 100:
            raise ValidationError({"q": "at most 100 characters"})
        limit = _int_param(request, "limit", 20, 1, MAX_LIMIT)
        offset = _int_param(request, "offset", 0, 0, 5000)
        brand = (request.query_params.get("brand") or "").strip()
        backend = index if settings.SEARCH_BACKEND == "meili" else dbsearch
        try:
            found = backend.search(q, limit=limit, offset=offset, brand=brand)
        except index.SearchUnavailable as exc:
            return Response({"detail": f"search is unavailable: {exc}"}, status=503)
        return Response({**found, "limit": limit, "offset": offset})


class PartDetailView(generics.RetrieveAPIView):
    serializer_class = PartDetailSerializer
    queryset = Part.objects.prefetch_related(
        Prefetch("sources", queryset=PartSource.objects.select_related("supplier").order_by("supplier__code", "id")),
        Prefetch("fitments", queryset=Fitment.objects.select_related("vehicle").order_by("vehicle__year", "vehicle__make", "vehicle__model", "id")),
    )


class FeedRunViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/feed-runs/ (filter: kind, status, supplier) and /api/feed-runs/<id>/."""

    serializer_class = FeedRunSerializer

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "detail": self.action == "retrieve"}

    def get_queryset(self):
        qs = PipelineRun.objects.select_related("supplier").order_by("-id")
        params = self.request.query_params
        for name, valid in (("kind", PipelineRun.Kind.values), ("status", PipelineRun.Status.values)):
            value = params.get(name)
            if value:
                if value not in valid:
                    raise ValidationError({name: f"one of {', '.join(valid)}"})
                qs = qs.filter(**{name: value})
        if params.get("supplier"):
            qs = qs.filter(supplier__code=params["supplier"])
        return qs


class FitmentView(generics.ListAPIView):
    """GET /api/fitment/?year=2019&make=ALTAIR&model=T200[&submodel=...&engine=...]: the parts that fit a vehicle."""

    serializer_class = FitmentPartSerializer

    def get_queryset(self):
        params = self.request.query_params
        errors = {}
        try:
            year = int(params.get("year", ""))
        except ValueError:
            errors["year"] = "a whole number, for example 2019"
            year = 0
        make, model = canon_vehicle_text(params.get("make")), canon_vehicle_text(params.get("model"))
        if not make:
            errors["make"] = "required"
        if not model:
            errors["model"] = "required"
        if errors:
            raise ValidationError(errors)
        qs = Fitment.objects.filter(vehicle__year=year, vehicle__make=make, vehicle__model=model)
        for name in ("submodel", "engine"):
            if params.get(name):
                qs = qs.filter(**{f"vehicle__{name}": canon_vehicle_text(params[name])})
        return qs.select_related("part", "vehicle").order_by("part__brand", "part__number_norm", "id")


class ReviewQueueView(generics.ListAPIView):
    """GET /api/review/: listings the match rule would not decide alone."""

    serializer_class = ReviewItemSerializer

    def get_queryset(self):
        status = self.request.query_params.get("status", ReviewItem.Status.PENDING)
        if status not in ReviewItem.Status.values:
            raise ValidationError({"status": f"one of {', '.join(ReviewItem.Status.values)}"})
        return ReviewItem.objects.filter(status=status).select_related("supplier", "norm_row__raw").order_by("id")


__all__ = ["PartSearchView", "PartDetailView", "FeedRunViewSet", "FitmentView", "ReviewQueueView"]
