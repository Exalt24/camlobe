"""The same search over PostgreSQL, for a machine without Meilisearch. Same query rules, same answer shape."""
from __future__ import annotations

from django.db.models import Q

from ..models import Part
from ..normalize import canonical_brand
from .documents import documents_for, interpret_query


def search(q: str, limit: int = 20, offset: int = 0, brand: str = "") -> dict:
    query = interpret_query(q)
    qs = Part.objects.all()
    if brand:
        qs = qs.filter(brand=canonical_brand(brand))
    if query["brand"]:
        qs = qs.filter(brand=query["brand"])
    if query["kind"] == "number":
        qs = qs.filter(number_norm__startswith=query["value"])
    elif query["kind"] == "gtin":
        qs = qs.filter(gtin=query["value"])
    else:
        words = [w for w in query["value"].split() if w]
        if not words:
            return {"count": 0, "results": []}
        for word in words:
            qs = qs.filter(Q(description__icontains=word) | Q(brand__icontains=word))
    count = qs.count()
    ids = list(qs.order_by("number_norm", "id").values_list("id", flat=True)[offset : offset + limit])
    by_id = {d["id"]: d for d in documents_for(Part.objects.filter(id__in=ids))}
    results = []
    for pk in ids:
        doc = by_id[pk]
        doc.pop("version", None)
        results.append(doc)
    return {"count": count, "results": results}
