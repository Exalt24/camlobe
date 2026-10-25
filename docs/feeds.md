# Feeds: sources, mapping and what a run records

`python manage.py ingest_feed <supplier> <one source>`. Secrets come from environment variables, never from the command line.

| Source | Flags | What it does |
|---|---|---|
| Local CSV or XML | `--file PATH` | reads it line by line (CSV) or as a stream (XML) |
| SFTP | `--sftp-host H --sftp-path /feed.csv --sftp-user U --sftp-password-env NAME --sftp-host-key SHA256:...` | checks the host key against the fingerprint you give, downloads to a temporary file, checks the size before and after, then reads it like a local file |
| Paged REST | `--rest-url URL [--token-env NAME] [--page-size N] [--rate-limit PER_SECOND]` | `GET url?offset=&limit=` answering `{"items": [...], "total": N}` |
| SOAP | `--soap-wsdl URL [--page-size N]` | calls `GetCatalogPage(offset, limit)` through zeep |

## Mapping

Columns are matched by name, never by position, after lower casing and dropping punctuation. `item_number`, `PartNo`, `sku` and `Item Number` all mean the part number. A reordered file loads the same rows, and a rename to a known alias loads as before. A required column (part number, brand, cost) that no header maps to fails the run before a row is written, with the headers the feed does have in the message. Two headers for one field is also refused. Columns that map to nothing are listed in the run's notes.

Costs are parsed with `Decimal` into integer cents. `12,50` (a decimal comma looks like a thousands separator), `12.505` (half a cent), `-3`, `N/A` and anything above ten million dollars are rejected rows, never guessed.

## Encodings

CSV lines are decoded one at a time: UTF-8 first, windows-1252 when that fails, so one pasted latin line does not turn every accent in the file into noise. A byte windows-1252 does not define rejects that row (`undecodable_bytes`). A UTF-8 byte order mark is skipped, and the delimiter (comma, semicolon, tab or pipe) is read from the header line. XML is parsed with DTDs and entities forbidden. A row with the wrong number of cells is rejected (`column_count`).

## What a run records

Every run is a row in `catalog_pipelinerun`: kind, supplier, source, status (`running`, `succeeded`, `partial`, `failed`), counts (read, written, unchanged, rejected), up to 50 errors and notes (encoding, resume point, retries, unmapped columns). Rejected rows are kept with their position, reason and raw text. `GET /api/feed-runs/` shows them.

## Restart and re-run

- Each batch of 1000 rows is one transaction holding its rows, its rejected rows, the run counters and the checkpoint.
- A source is skipped when its content hash equals the last complete run's. A file with the same name and new content is read from the start, and only the rows that changed are stored.
- A row is stored when its content differs from the newest stored version of the same supplier part number. A price that goes from A to B and back to A stores three versions, so the current price is the last one.
- Each source row also has an idempotency key (supplier, source, position, content hash) with a unique constraint, which is what stops rows after a gap landing twice when they are read again.
- A run left `running` by a dead process is closed as failed at the next start. Two runs of one source cannot overlap (Postgres advisory lock).

## Paged sources

Requests are spaced by the rate limit. A timeout, connection error, 429 or 5xx is retried with exponential backoff, a `Retry-After` is honoured up to the cap. A page that still fails, or a 4xx retrying cannot fix, becomes a gap: the run is `partial`, the checkpoint stops at the gap, the pages after it are still read, and the next run adds only the missing page. A 401 or 403 stops the run at once. Three failed pages in a row mean the endpoint is down. A page shorter than asked for in the middle of a known total stops the run, because the server capped the page size and carrying on would skip rows.
