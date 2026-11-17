# Part number normalisation

`catalog/normalize.py`. `normalize_part_number(raw, brand)` is a pure function. It returns the canonical number, the ids of the rules that fired, and an error when the number cannot be used. The ids are stored with each normalised row, so a surprising canonical number can be explained from the row alone.

The brands, aliases and prefixes below are invented. They follow the shapes real catalogs use, they are not any real manufacturer's rules.

## The rules, in the order they run

| Id | Rule | Example | Why |
|---|---|---|---|
| R1 | Unicode NFKC, then accents dropped | `ÀBC-123` becomes `ABC123`, full width `１２３４５` becomes `12345` | exports from different tools write the same character differently |
| R2 | Upper case | `zr 4519` becomes `ZR4519` | |
| R3 | Remove every character that is not A to Z or 0 to 9 | `AB.12/34_56` becomes `AB123456` | suppliers differ on spaces, dashes, dots and slashes. A slash or a dot that is meaningful in a real catalog would be lost: that is a stated limit |
| R4 | Remove a brand or supplier prefix when the brand is known and at least 3 characters with a digit remain | `MF-H4123A` with brand Marlow becomes `H4123A`; `MF-12` stays `MF12` | catalogs glue a house prefix on the front. The remainder guard stops a real number that happens to start with the prefix letters from being eaten |
| R5 | Remove leading zeros from an all digit number of 8 or more digits, keeping at least 4 digits, except for brands whose numbers really start with 0 | `0000200022` becomes `200022`; `0123456` stays; Kestrel `0440011187` stays | fixed width ERP exports pad numbers with zeros |
| R6 | A letter outside A to Z after R1 rejects the number | Cyrillic look alikes of `A`, `B`, `C` | no guessing which letter was meant |
| R7 | Fewer than 3 or more than 32 characters rejects the number | `AB`, a 34 character string | too short to identify a part, too long to be one |

A padded and an unpadded form of one number can still end up as two canonical numbers (`00045678` becomes `45678`, `0045678` stays), so the match step queues the pair as a `zero_variant` instead of creating a second part.

R4 and R5 repeat until nothing changes, so normalising a canonical number returns it unchanged. A prefix applied twice by a careless export (`MF-MF-H4123A`) is handled by the same loop.

## What is deliberately not done

- The letter O is never read as the digit 0, and the digit 1 is never read as the letter I.
- No fuzzy matching of numbers: `H4123A` and `H4124A` are two parts.
- Unbranded numbers get the zero rule (R5) too. That is why a row with no brand never auto matches: see [matching.md](matching.md).

## Brands and GTINs

`canonical_brand` compares letters and digits only against a table of spellings (`Marlow`, `Marlow Filters`, `MARLOW FILTRATION`, ...). An unknown brand keeps its own letters and digits in upper case. A near spelling (`Marlowe`) is not an alias.

`normalize_gtin` accepts 8, 12, 13 or 14 digits with a valid check digit and stores them left padded to 14. Anything else becomes empty and the row carries the rule id `gtin_invalid`, so a bad GTIN never causes a false conflict.

## Page addresses

`catalog/ingest/urlnorm.py`, `canonical_url`: one name for one page. The scraper reads a page once and calls a link back to a page already read a loop, so it compares addresses by this form and never by the raw text. Equal by RFC 3986 section 6.2.2 and 6.2.3: the scheme and host in lower case, a default port dropped (80 for http, 443 for https), dot segments resolved, percent escapes in upper case with an escaped unreserved character turned back into the character, the fragment dropped. Equal by a rule of this tool, which a site could in theory read differently: query parameters sorted by name and value, and a parameter that only names the visitor (`utm_*`, `gclid`, `fbclid`, `msclkid`, `mc_cid`, `mc_eid`, `_ga`, `igshid`, `yclid`) dropped. Never touched: the case of the path, a trailing slash (`/parts` and `/parts/` are two names, and the walk treats a redirect between them as the one page itself), and any parameter that is not on the tracking list (`ref` and `sort` name pages). A string that is not an http or https address with a host is returned as it came, so it is never merged with another. Known limit: a site whose pages differ only by a parameter on the tracking list (a `utm_page` pager) would read as one page, and a site that treats the order of its query as meaning would too; the list is the constant `TRACKING_NAMES` and the prefix `TRACKING_PREFIX`.

## The Rust port

`normalizer-rs/` is `normalize_part_number` written again in Rust (`cargo build --release` there). The Python function stays the reference and `tests/test_normalize_rust.py` is what makes the port safe to trust: it gives both the same inputs and compares the canonical number, the rules that fired in order and the error, character for character. The inputs are generated text over the whole of Unicode, structured numbers (every brand prefix, separators, zero padding at every length from 1 to 14, look alike letters, full width forms), and one case for every code point of the range, alone and inside a number.

That sweep found what a hand written list would not have. Rust's standard library and the crates that carry the same tables are on a newer Unicode version than Python 3.13 (15.1.0), and the first run of the sweep disagreed on 4,308 of its cases: a letter Python does not know yet is a letter to Rust, and a character whose upper case was added in Unicode 16 changes under `to_uppercase` and not under `str.upper()`. So the port uses neither. `normalizer-rs/gen_tables.py` writes `src/tables.rs` (the ranges where `str.isalpha()` is true and every non identity `str.upper()` mapping) out of the reference interpreter, and `unicode-normalization` is pinned to 0.1.23, which is 15.1.0, for NFKC and NFD. After a Python upgrade, run `python normalizer-rs/gen_tables.py`; the version test and the sweep fail until you do.

Measured on an Intel Core i5-11400H laptop, Windows 11, one process, the function only (no database, no file, no process start): 200,000 numbers in about 1.4 seconds in Python and about 0.3 seconds in Rust, so about four times faster ([output](test-runs/2026-11-17-rust-normalizer-bench.txt), `python tools/bench_normalizer.py 200000`). The port is not wired into the pipeline: `normalize_parts` still calls the Python function, and the port is the proof that a rewrite of this hot path would keep every answer.

Checked by hand as well: four edits to the Rust (the zero padding threshold 8 to 9, the brand prefix `TRN` of TARN removed, the zero significant brand list emptied, and the letter test swapped for Rust's own `is_alphabetic`) each turned `tests/test_normalize_rust.py` red, and two of them only after a test for that case was added, because the first version of the file let them through.

## How it is tested

`tests/test_normalize.py`: a table of every rule with the rules that must fire, a list of numbers that must stay different, and Hypothesis properties: normalising twice equals normalising once, spacing and case never change the result, a brand prefix (one to three times) never changes the result, the output is only upper case letters and digits or a rejection. Each guard in the module has a mutant in `tools/mutation_check.py`. `tests/test_urlnorm.py` covers the page address rules one by one, two lists that must stay two names, and Hypothesis properties (idempotent, every spelling of one page has one name, a changed value or path is another name).
