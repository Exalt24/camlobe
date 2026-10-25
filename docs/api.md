# REST API

Read only (every other method is a 405) and with no authentication: a lab API on invented data that listens on localhost. Start it with `python manage.py runserver`. Costs are integer cents in US dollars.

| Endpoint | Answers |
|---|---|
| `GET /api/parts/search/?q=&brand=&limit=&offset=` | `{count, results, limit, offset}`; each result has `id, brand, number, description, gtin, suppliers, supplier_count, lowest_cost_cents` |
| `GET /api/parts/<id>/` | the part with its `sources` (supplier, the supplier's own part number, `cost_cents`, `currency`, how it was linked, last seen) and its `fitment` |
| `GET /api/fitment/?year=&make=&model=[&submodel=&engine=]` | the parts that fit one vehicle, with position and note |
| `GET /api/feed-runs/[?kind=&status=&supplier=]` and `/api/feed-runs/<id>/` | pipeline runs with counts, errors and notes; the detail adds rejected rows by reason |
| `GET /api/review/[?status=]` | listings the match rule would not decide |

## Search rules

A query with a digit in it is read as a part number and normalised like a feed's numbers, so `h-4123 a` finds `H4123A`. A query that is a valid GTIN (8, 12, 13 or 14 digits, dashes and spaces allowed) is looked up as a GTIN. Anything else is a text search over brand and description. A number matches exactly or by prefix and never by typo: `H4124A` does not find `H4123A`. A brand word in front of a number (`marlow h4123a`) filters by brand. `q` is required and at most 100 characters, `limit` is 1 to 100, `offset` is 0 to 5000. Bad input is a 400 naming the field.

`SEARCH_BACKEND=meili` (the default) answers from Meilisearch and answers 503 when it is down. `SEARCH_BACKEND=db` answers the same questions from PostgreSQL with the same rules. A test compares the two on the same queries.

## The Meilisearch index

- `python manage.py rebuild_search_index` builds `<index>_next` in batches of 1000 by part id, saves its cursor only after Meilisearch confirmed each batch, and swaps it with the live index when every part is in. Killed half way, the next run resumes after the last confirmed batch (`--restart` starts over). Searches see the old index until the swap.
- `python manage.py sync_search_index` sends only the parts whose `indexed_version` is behind their `search_version`. There is no clock and no cursor. A part edited while it is being sent keeps its newer version and is sent again next time. The sync refuses to run before a build or during one.
- Typo tolerance is off for the whole index, because a part number one character off is another part. The per attribute switch was tried first and on Meilisearch 1.12.8 it made every part number added after the setting impossible to find.
