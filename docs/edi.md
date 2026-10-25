# X12 documents: 850, 855, 856 and 810

`catalog/edi/x12.py`. A hand written reader and writer for the 005010 envelope and four documents. **It has not been tested against any trading partner, VAN or EDI translator**, it implements a stated subset of each document, and a real partner's implementation guide will differ in the details. All the documents in the tests are invented.

| Set | Subset read and written |
|---|---|
| 850 purchase order | `BEG` (type, PO number, date), optional `N1*ST`, `PO1` lines (line, quantity, unit of measure, unit price, part qualifier and number), `CTT` |
| 855 acknowledgement | `BAK`, `PO1` lines each followed by `ACK` (`IA` accepted, `IR` rejected, `IB` backordered, `IQ` quantity changed), `CTT` |
| 856 ship notice | `BSN`, the `HL` hierarchy shipment, order and item (`PRF`, `LIN`, `SN1`), `CTT`; `TD1`, `REF` and `DTM` are accepted and ignored |
| 810 invoice | `BIG`, `IT1` lines, `TDS` (total in cents, no decimal point), `CTT`; the total must equal the sum of the lines |

## What is checked

- The ISA segment is exactly 106 characters, every element has its fixed width, and the separators are three different non alphanumeric characters.
- Control numbers agree: `IEA02` with `ISA13`, `GE02` with `GS06`, `SE02` with `ST02`. Counts agree: `IEA01` groups, `GE01` sets, `SE01` segments, `CTT01` lines (or `HL` segments).
- Every element is present when required, inside its length, and of its type: whole quantities above zero, prices as decimals with at most two places, calendar dates (`CCYYMMDD`), codes from the stated tables.
- A set inside the wrong functional group, a segment outside the subset and a segment out of order are refused.

The first problem found raises `X12Error` with a code (`E_SE_COUNT`, `E_DATE`, `E_TOTAL_MISMATCH`, ...) and the segment number. `tests/test_edi.py` has a test for each code, every one built one edit away from a valid document, and a fuzz test that damages valid documents at random and requires that the reader either reads them or raises `X12Error`.

## Orders

`python manage.py edi_po <supplier> <part_id>:<qty> ... --po PO-1001` builds an 850 from what the supplier lists (its own part number and cost, not ours) and reads it back before printing. `python manage.py edi_parse FILE` prints what a file says or the first error. `catalog/edi/orders.py::reconcile` compares an acknowledgement, a ship notice and an invoice with the order and lists every difference (quantity changed, line rejected or backordered, price changed, more shipped than ordered, invoiced above shipped) and fixes none of them.

## Not done

No 997 or 999 acknowledgement, no ISA authentication, no repeating elements beyond what the subset names, no 5010 implementation guide for any named partner, no transport (AS2, VAN or SFTP delivery of EDI).
