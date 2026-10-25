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

## How it is tested

`tests/test_normalize.py`: a table of every rule with the rules that must fire, a list of numbers that must stay different, and Hypothesis properties: normalising twice equals normalising once, spacing and case never change the result, a brand prefix (one to three times) never changes the result, the output is only upper case letters and digits or a rejection. Each guard in the module has a mutant in `tools/mutation_check.py`.
