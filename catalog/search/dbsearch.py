"""The same search over PostgreSQL, for a machine without Meilisearch. Same query legs, same answer shape."""
from __future__ import annotations

from functools import reduce
from operator import or_

from ..models import Part
from .documents import documents_for, leg_filter, search_legs


def search(q: str, limit: int = 20, offset: int = 0, brand: str = "") -> dict:
    legs = search_legs(q, brand)
    if not legs:
        return {"count": 0, "results": []}
    qs = Part.objects.filter(reduce(or_, (leg_filter(leg) for leg in legs)))
    count = qs.count()
    ids = list(qs.order_by("number_norm", "id").values_list("id", flat=True)[offset : offset + limit])
    by_id = {d["id"]: d for d in documents_for(Part.objects.filter(id__in=ids))}
    results = []
    for pk in ids:
        doc = by_id[pk]
        doc.pop("version", None)
        results.append(doc)
    return {"count": count, "results": results}
