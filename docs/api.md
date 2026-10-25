# REST API

Read only (every other method is a 405) and with no authentication: a lab API on invented data that listens on localhost. Start it with `python manage.py runserver`. Costs are integer cents in US dollars.

| Endpoint | Answers |
|---|---|
| `GET /api/parts/search/?q=&brand=&limit=&offset=` | `{count, results, limit, offset}`; each result has `id, brand, number, description, gtin, suppliers, supplier_count, lowest_cost_cents` (suppliers and the lowest cost count only listings the supplier has not dropped from its full feed) |
| `GET /api/parts/<id>/` | the part with its `sources` (supplier, the supplier's own part number, `cost_cents`, `currency`, how it was linked, last seen), its `delisted_sources` (listings the supplier dropped from a full feed: supplier, its part number, when) and its `fitment` |
| `GET /api/fitment/?year=&make=&model=[&submodel=&engine=]` | the parts that fit one vehicle, with position and note |
| `GET /api/feed-runs/[?kind=&status=&supplier=]` and `/api/feed-runs/<id>/` | pipeline runs with counts, errors and notes; the detail adds rejected rows by reason |
| `GET /api/review/[?status=]` | listings the match rule would not decide |

## Search rules

One function reads the query (`search_legs`) and both backends answer from its legs, so they cannot drift apart.

- **A query with no letter or digit** (`---`, `#`) is an empty answer, not the whole catalog.
- **A query with a digit is a part number**, tried in every form a supplier might have printed it, each matched against the canonical numbers exactly or by prefix: as typed (letters and digits only), normalised with no brand, and normalised with each known brand's own rules. So `DU-ZR7208` finds `ZR7208` through the Dunmore prefix rule, `0766611428` finds the Kestrel number with its real leading zero (and the unpadded `766611428` of another brand), `h-4123 a` finds `H4123A`. A form that only one brand's rules produce matches that brand's parts only, so `MF-ZR7208` does not find the Dunmore part; when two brands both fit, both answer and the code never picks one. A number is never fuzzy: `H4124A` does not find `H4123A`.
- **A query that is a valid GTIN** (8, 12, 13 or 14 digits with a check digit, dashes and spaces allowed) is looked up as a GTIN and also tried as a number.
- **Anything else is text**: every word must start a word of the brand or the description (`mar` finds `MARLOW`, `ilter` does not find `filter`, `brake zzz` finds nothing). The part number is not searched by a text query: `ZR` finds only what has a word starting with `zr` in its brand or description.
- A brand word in front of a number (`marlow h4123a`) or the `brand` parameter narrows every form to that brand; two different brands at once give nothing.

`q` is required and at most 100 characters, `limit` is 1 to 100, `offset` is 0 to 5000. Bad input is a 400 naming the field. Every other list endpoint (`/api/review/`, `/api/feed-runs/`, `/api/fitment/`) pages 25 rows by default and never more than 100: a larger `limit` is lowered to 100.

`SEARCH_BACKEND=meili` (the default) answers from Meilisearch and answers 503 when it is down. `SEARCH_BACKEND=db` answers the same questions from PostgreSQL with the same rules. A test compares the two on 47 queries, with and without a brand. One difference is stated: Meilisearch folds accents (`eau` finds `éau`), PostgreSQL here does not.

## The Meilisearch index

- `python manage.py rebuild_search_index` builds `<index>_next` in batches of 1000 by part id, saves its cursor only after Meilisearch confirmed each batch, and swaps it with the live index when every part is in. Killed half way, the next run resumes after the last confirmed batch (`--restart` starts over). Searches see the old index until the swap.
- `python manage.py sync_search_index` sends only the parts whose `indexed_version` is behind their `search_version`. There is no clock and no cursor. A part edited while it is being sent keeps its newer version and is sent again next time. The sync refuses to run before a build or during one.
- Typo tolerance is off for the whole index, because a part number one character off is another part. The per attribute switch was tried first and on Meilisearch 1.12.8 it made every part number added after the setting impossible to find.
