# The match rule and the review queue

`catalog/matching_batch.py`. A normalised row is a supplier listing with a canonical brand, a canonical number and maybe a GTIN. The rule decides in this order, and never guesses:

1. The supplier's listing is already linked: refresh its newest version and cost.
2. The listing is already in the review queue: refresh the queued row, or skip it if it was ignored.
3. No brand: queue it as `no_brand`, with the parts that carry the number as candidates.
4. A part with the same brand and number exists:
   - both sides have a GTIN and they differ: queue it as `gtin_conflict`.
   - the row's GTIN is held by a different part: queue it as `gtin_cross`.
   - otherwise link it (rule `exact`).
5. No such part:
   - the row's GTIN is held by another part (a typo, a renumbering or a rebrand): queue it as `gtin_cross`.
   - the brand already has a part with the same digits and a different number of leading zeros (`0045678` against `00045678`): queue it as `zero_variant`.
   - otherwise create the part (rule `new`) and link it.

The same number under a different brand is not a match. Two manufacturers use the same number all the time.

## Why these queue reasons

| Reason | What it usually is | Why it is not decided by the code |
|---|---|---|
| `no_brand` | a feed that leaves the manufacturer out | the number alone fits several parts, or none |
| `gtin_conflict` | two suppliers disagree on the barcode of one brand and number | one of them is wrong, and which one is not in the data |
| `gtin_cross` | a part renumbered or superseded, a typo in a number, a rebrand | merging would hide a real difference, creating would duplicate a barcode |
| `zero_variant` | a fixed width export that pads a number the other supplier sends without the padding | the number alone cannot say whether the zeros are padding or part of the number, so one part or two is a person's call |

A person resolves an item with `python manage.py resolve_review --item ID --part PART_ID`, `--new-part [--brand X]` or `--ignore`. Resolving to a part creates the listing's link, and later rows for that listing flow through the link. An ignored listing stays out of the catalog.

## What makes it restartable

The queue is the set of normalised rows whose `match_state` is `pending`. Each batch of 2000 rows is one transaction: the new parts, the links, the review items and the state changes commit together. A run killed between batches resumes with the remaining rows, and a second run finds none. Two runs cannot overlap: each takes a Postgres advisory lock.

Rows are decided in id order, so a run is deterministic. When several versions of one listing arrive in a batch, the newest one carries the data.

## Known limits

- The order feeds arrive in can change which side of a GTIN disagreement is queued. The set of parts is the same either way (`test_the_parts_do_not_depend_on_the_order_the_feeds_arrive_in`).
- There is no fuzzy matching between numbers and no supersession table. A renumbered part appears as a `gtin_cross` item or as a new part.
- The zero variant check covers all digit numbers only. A zero run after letters (`AB0123` against `AB123`) is not compared, since no export rule in these notes pads that shape.
- A change to the normalisation rules does not re match old rows. Rows carry the rule version, and the normalize command reports how many are stale.
