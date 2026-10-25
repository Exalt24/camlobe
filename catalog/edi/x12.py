"""A small, hand-written X12 reader and writer for four purchase order documents: 850, 855, 856 and 810.

WHAT THIS IS NOT. It has not been tested against any trading partner, VAN or EDI translator, and it is
not a complete X12 implementation. It reads and writes a stated subset of each document (the segments and
elements in the parsers below), for the version 005010 envelope, and refuses everything else with a coded
error. There is no 997/999 acknowledgement, no ISA authentication, no repeating element support beyond
what the subset names, and a real partner's implementation guide will differ in the details. The point is
the discipline: fixed width envelope, control totals that must agree, every element checked for presence,
length and type, and a malformed document refused with the reason and the segment.

Errors are raised as X12Error(code, message, segment_number). One error per document: the first one found.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# ----------------------------------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------------------------------


class X12Error(Exception):
    def __init__(self, code: str, message: str, segment_number: int | None = None):
        where = f" (segment {segment_number})" if segment_number else ""
        super().__init__(f"{code}: {message}{where}")
        self.code = code
        self.message = message
        self.segment_number = segment_number


# ----------------------------------------------------------------------------------------------------
# Typed documents
# ----------------------------------------------------------------------------------------------------

UOM_CODES = frozenset({"EA", "CA", "BX", "PK", "DZ", "PR", "ST", "SE"})
PART_QUALIFIERS = frozenset({"BP", "VP", "MG", "UP"})  # buyer's, vendor's, manufacturer's number, UPC
ACK_STATUS = frozenset({"IA", "IR", "IB", "IQ"})  # accepted, rejected, backordered, accepted with a new quantity
ACK_TYPES = frozenset({"AC", "AD", "AE", "RD"})
PO_TYPES = frozenset({"SA", "NE", "RL"})
SET_TO_GROUP = {"850": "PO", "855": "PR", "856": "SH", "810": "IN"}


@dataclass(frozen=True)
class Line:
    line_id: str
    qty: int
    uom: str
    unit_price_cents: int
    part_qualifier: str
    part_number: str


@dataclass(frozen=True)
class PurchaseOrder:
    po_number: str
    date: dt.date
    lines: tuple[Line, ...]
    po_type: str = "SA"
    ship_to: str = ""


@dataclass(frozen=True)
class AckLine:
    line_id: str
    status: str
    qty: int | None
    uom: str
    part_qualifier: str
    part_number: str
    unit_price_cents: int = 0
    ordered_qty: int = 0


@dataclass(frozen=True)
class PoAck:
    po_number: str
    date: dt.date
    lines: tuple[AckLine, ...]
    ack_type: str = "AC"


@dataclass(frozen=True)
class AsnItem:
    part_qualifier: str
    part_number: str
    qty: int
    uom: str


@dataclass(frozen=True)
class AsnOrder:
    po_number: str
    items: tuple[AsnItem, ...]


@dataclass(frozen=True)
class Asn:
    shipment_id: str
    date: dt.date
    orders: tuple[AsnOrder, ...]


@dataclass(frozen=True)
class Invoice:
    invoice_number: str
    date: dt.date
    po_number: str
    lines: tuple[Line, ...]
    total_cents: int


@dataclass
class Envelope:
    sender: str
    receiver: str
    control_number: str
    usage: str
    documents: list = field(default_factory=list)  # PurchaseOrder | PoAck | Asn | Invoice, in order


# ----------------------------------------------------------------------------------------------------
# Segments and element checks
# ----------------------------------------------------------------------------------------------------


@dataclass
class Segment:
    tag: str
    elements: list[str]  # elements[0] is element 01
    number: int  # 1 based position in the interchange, for error messages

    def el(self, n: int) -> str:
        return self.elements[n - 1] if n <= len(self.elements) else ""


def need(seg: Segment, n: int, name: str) -> str:
    value = seg.el(n)
    if value == "":
        raise X12Error("E_ELEMENT_REQUIRED", f"{seg.tag}{n:02d} {name} is required", seg.number)
    return value


def an(seg: Segment, n: int, name: str, lo: int, hi: int, required: bool = True) -> str:
    value = need(seg, n, name) if required else seg.el(n)
    if value == "" and not required:
        return ""
    if not lo <= len(value) <= hi:
        raise X12Error("E_ELEMENT_LENGTH", f"{seg.tag}{n:02d} {name} must be {lo} to {hi} characters, got {len(value)}", seg.number)
    if not re.fullmatch(r"[ -~]+", value):
        raise X12Error("E_ELEMENT_TYPE", f"{seg.tag}{n:02d} {name} has characters outside the basic set", seg.number)
    return value


def code(seg: Segment, n: int, name: str, allowed: frozenset[str], required: bool = True) -> str:
    value = need(seg, n, name) if required else seg.el(n)
    if value == "" and not required:
        return ""
    if value not in allowed:
        raise X12Error("E_CODE", f"{seg.tag}{n:02d} {name} {value!r} is not one of {', '.join(sorted(allowed))}", seg.number)
    return value


def whole(seg: Segment, n: int, name: str, minimum: int = 1, maximum: int = 999_999) -> int:
    value = need(seg, n, name)
    if not re.fullmatch(r"\d{1,9}", value):
        raise X12Error("E_QTY" if "qty" in name.lower() or "quantity" in name.lower() else "E_ELEMENT_TYPE", f"{seg.tag}{n:02d} {name} must be a whole number, got {value!r}", seg.number)
    number = int(value)
    if not minimum <= number <= maximum:
        raise X12Error("E_QTY" if "qty" in name.lower() or "quantity" in name.lower() else "E_ELEMENT_RANGE", f"{seg.tag}{n:02d} {name} must be {minimum} to {maximum}, got {number}", seg.number)
    return number


def money(seg: Segment, n: int, name: str) -> int:
    """A decimal dollar amount (X12 type R) as integer cents. More than two decimals is refused, never rounded."""
    value = need(seg, n, name)
    if not re.fullmatch(r"\d{1,9}(\.\d+)?", value):
        raise X12Error("E_PRICE", f"{seg.tag}{n:02d} {name} must be a positive decimal, got {value!r}", seg.number)
    try:
        cents = Decimal(value) * 100
    except InvalidOperation:  # pragma: no cover - the pattern guarantees a number
        raise X12Error("E_PRICE", f"{seg.tag}{n:02d} {name} is not a number", seg.number)
    if cents != cents.to_integral_value():
        raise X12Error("E_PRICE", f"{seg.tag}{n:02d} {name} has more than two decimals: {value}", seg.number)
    return int(cents)


def date8(seg: Segment, n: int, name: str) -> dt.date:
    value = need(seg, n, name)
    if not re.fullmatch(r"\d{8}", value):
        raise X12Error("E_DATE", f"{seg.tag}{n:02d} {name} must be CCYYMMDD, got {value!r}", seg.number)
    try:
        return dt.date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        raise X12Error("E_DATE", f"{seg.tag}{n:02d} {name} {value} is not a calendar date", seg.number)


def fmt_date(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def fmt_price(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


# ----------------------------------------------------------------------------------------------------
# Envelope
# ----------------------------------------------------------------------------------------------------

ISA_WIDTHS = [2, 10, 2, 10, 2, 15, 2, 15, 6, 4, 1, 5, 9, 1, 1, 1]
ISA_LENGTH = 106


def split_segments(text: str) -> tuple[list[Segment], str, str]:
    text = text.lstrip("﻿ \t\r\n")
    if not text.startswith("ISA"):
        raise X12Error("E_ISA_MISSING", "the document does not start with an ISA segment")
    if len(text) < ISA_LENGTH:
        raise X12Error("E_ISA_SHORT", f"the ISA segment is {len(text)} characters, it must be {ISA_LENGTH}")
    es, ss, term = text[3], text[104], text[105]
    if es.isalnum() or ss.isalnum() or term.isalnum() or len({es, ss, term}) != 3 or es in " \r\n" or term in " \r\n":
        raise X12Error("E_ISA_SEPARATORS", f"unusable separators {es!r} {ss!r} {term!r}", 1)
    pieces = text.split(term)
    if pieces[-1].strip("\r\n \t") != "":
        raise X12Error("E_UNTERMINATED", "the last segment has no terminator")
    segments = []
    for n, raw in enumerate(pieces[:-1], start=1):
        raw = raw.strip("\r\n")
        parts = raw.split(es)
        tag = parts[0]
        if not re.fullmatch(r"[A-Z0-9]{2,3}", tag):
            raise X12Error("E_BAD_TAG", f"{tag!r} is not a segment tag", n)
        segments.append(Segment(tag, parts[1:], n))
    isa = segments[0]
    if isa.tag != "ISA":
        raise X12Error("E_ISA_MISSING", "the document does not start with an ISA segment", 1)
    if len(isa.elements) != 16 or any(len(e) != w for e, w in zip(isa.elements, ISA_WIDTHS)):
        raise X12Error("E_ISA_FIXED_WIDTH", "an ISA element is not its fixed width", 1)
    return segments, es, ss


def _check_isa_values(isa: Segment) -> None:
    if isa.el(12) != "00501":
        raise X12Error("E_VERSION", f"ISA12 {isa.el(12)!r}: only 00501 is supported", 1)
    if not re.fullmatch(r"\d{9}", isa.el(13)):
        raise X12Error("E_ISA_CONTROL", "ISA13 must be nine digits", 1)
    if isa.el(15) not in ("T", "P"):
        raise X12Error("E_CODE", "ISA15 must be T or P", 1)
    if not re.fullmatch(r"\d{6}", isa.el(9)) or not re.fullmatch(r"\d{4}", isa.el(10)):
        raise X12Error("E_DATE", "ISA09 must be YYMMDD and ISA10 HHMM", 1)


def parse(text: str) -> Envelope:
    """Parse and validate one interchange. Returns the typed documents, or raises X12Error."""
    segments, _, _ = split_segments(text)
    isa = segments[0]
    _check_isa_values(isa)
    envelope = Envelope(isa.el(6).strip(), isa.el(8).strip(), isa.el(13), isa.el(15))
    i = 1
    groups = 0
    while True:
        if i >= len(segments):
            raise X12Error("E_NO_IEA", "the interchange has no IEA segment")
        seg = segments[i]
        if seg.tag == "IEA":
            break
        if seg.tag != "GS":
            raise X12Error("E_GS_MISSING", f"expected GS, found {seg.tag}", seg.number)
        i, docs = _parse_group(segments, i)
        envelope.documents.extend(docs)
        groups += 1
    iea = segments[i]
    if iea.el(1) != str(groups):
        raise X12Error("E_IEA_COUNT", f"IEA01 says {iea.el(1)!r} groups, the interchange holds {groups}", iea.number)
    if iea.el(2) != isa.el(13):
        raise X12Error("E_IEA_CONTROL", f"IEA02 {iea.el(2)!r} does not match ISA13 {isa.el(13)!r}", iea.number)
    if i != len(segments) - 1:
        raise X12Error("E_TRAILING_DATA", "segments follow the IEA", segments[i + 1].number)
    if groups == 0:
        raise X12Error("E_GS_MISSING", "the interchange holds no functional group", iea.number)
    return envelope


def _parse_group(segments: list[Segment], i: int) -> tuple[int, list]:
    gs = segments[i]
    if gs.el(1) not in SET_TO_GROUP.values():
        raise X12Error("E_UNSUPPORTED_SET", f"GS01 {gs.el(1)!r} is not a supported functional identifier", gs.number)
    if not gs.el(6):
        raise X12Error("E_ELEMENT_REQUIRED", "GS06 group control number is required", gs.number)
    if not gs.el(8).startswith("00501"):
        raise X12Error("E_VERSION", f"GS08 {gs.el(8)!r}: only 00501x is supported", gs.number)
    docs: list = []
    sets = 0
    i += 1
    while True:
        if i >= len(segments):
            raise X12Error("E_GE_MISSING", "the functional group has no GE segment")
        seg = segments[i]
        if seg.tag == "GE":
            break
        if seg.tag != "ST":
            raise X12Error("E_ST_MISSING", f"expected ST, found {seg.tag}", seg.number)
        i, doc = _parse_set(segments, i, gs)
        docs.append(doc)
        sets += 1
    ge = segments[i]
    if ge.el(1) != str(sets):
        raise X12Error("E_GE_COUNT", f"GE01 says {ge.el(1)!r} transaction sets, the group holds {sets}", ge.number)
    if ge.el(2) != gs.el(6):
        raise X12Error("E_GE_CONTROL", f"GE02 {ge.el(2)!r} does not match GS06 {gs.el(6)!r}", ge.number)
    if sets == 0:
        raise X12Error("E_ST_MISSING", "the functional group holds no transaction set", ge.number)
    return i + 1, docs


def _parse_set(segments: list[Segment], i: int, gs: Segment) -> tuple[int, object]:
    st = segments[i]
    set_id = st.el(1)
    if set_id not in SET_TO_GROUP:
        raise X12Error("E_UNSUPPORTED_SET", f"ST01 {set_id!r} is not one of {', '.join(sorted(SET_TO_GROUP))}", st.number)
    if SET_TO_GROUP[set_id] != gs.el(1):
        raise X12Error("E_GS_MISMATCH", f"transaction set {set_id} inside a {gs.el(1)} group", st.number)
    an(st, 2, "transaction set control number", 4, 9)
    j = i + 1
    while j < len(segments) and segments[j].tag not in ("SE", "ST", "GE", "IEA"):
        j += 1
    if j >= len(segments) or segments[j].tag != "SE":
        raise X12Error("E_SE_MISSING", f"transaction set {set_id} has no SE segment", st.number)
    se = segments[j]
    count = j - i + 1
    if se.el(1) != str(count):
        raise X12Error("E_SE_COUNT", f"SE01 says {se.el(1)!r} segments, the set holds {count}", se.number)
    if se.el(2) != st.el(2):
        raise X12Error("E_SE_CONTROL", f"SE02 {se.el(2)!r} does not match ST02 {st.el(2)!r}", se.number)
    body = segments[i + 1 : j]
    parsers = {"850": _parse_850, "855": _parse_855, "856": _parse_856, "810": _parse_810}
    return j + 1, parsers[set_id](body, st)


def _first(body: list[Segment], tag: str, st: Segment) -> Segment:
    if not body or body[0].tag != tag:
        found = body[0].tag if body else "nothing"
        raise X12Error("E_MISSING_SEGMENT", f"the set must start with {tag} after ST, found {found}", (body[0].number if body else st.number))
    return body[0]


def _po1_line(seg: Segment) -> Line:
    return Line(
        line_id=an(seg, 1, "line number", 1, 20),
        qty=whole(seg, 2, "quantity ordered"),
        uom=code(seg, 3, "unit of measure", UOM_CODES),
        unit_price_cents=money(seg, 4, "unit price"),
        part_qualifier=code(seg, 6, "part number qualifier", PART_QUALIFIERS),
        part_number=an(seg, 7, "part number", 1, 48),
    )


def _ctt(body: list[Segment], expected: int, what: str, st: Segment) -> None:
    ctt = [s for s in body if s.tag == "CTT"]
    if len(ctt) != 1:
        raise X12Error("E_MISSING_SEGMENT", "exactly one CTT segment is required", body[-1].number if body else st.number)
    if ctt[0] is not body[-1]:
        raise X12Error("E_SEGMENT_ORDER", "CTT must be the last segment before SE", ctt[0].number)
    if whole(ctt[0], 1, "number of line items", 0) != expected:
        raise X12Error("E_CTT_COUNT", f"CTT01 says {ctt[0].el(1)}, the set holds {expected} {what}", ctt[0].number)


def _parse_850(body: list[Segment], st: Segment) -> PurchaseOrder:
    beg = _first(body, "BEG", st)
    code(beg, 1, "transaction set purpose", frozenset({"00"}))
    po_type = code(beg, 2, "purchase order type", PO_TYPES)
    po_number = an(beg, 3, "purchase order number", 1, 22)
    date = date8(beg, 5, "purchase order date")
    ship_to = ""
    lines: list[Line] = []
    for seg in body[1:]:
        if seg.tag == "N1":
            if lines:
                raise X12Error("E_SEGMENT_ORDER", "N1 must come before the PO1 lines", seg.number)
            code(seg, 1, "entity identifier", frozenset({"ST"}))
            ship_to = an(seg, 2, "ship to name", 1, 60)
        elif seg.tag == "PO1":
            lines.append(_po1_line(seg))
        elif seg.tag == "CTT":
            pass
        else:
            raise X12Error("E_UNEXPECTED_SEGMENT", f"{seg.tag} is not part of the supported 850 subset", seg.number)
    if not lines:
        raise X12Error("E_MISSING_SEGMENT", "an 850 needs at least one PO1 line", st.number)
    if len({ln.line_id for ln in lines}) != len(lines):
        raise X12Error("E_DUPLICATE_LINE", "two PO1 segments use the same line number", st.number)
    _ctt(body, len(lines), "PO1 lines", st)
    return PurchaseOrder(po_number, date, tuple(lines), po_type, ship_to)


def _parse_855(body: list[Segment], st: Segment) -> PoAck:
    bak = _first(body, "BAK", st)
    code(bak, 1, "transaction set purpose", frozenset({"00"}))
    ack_type = code(bak, 2, "acknowledgment type", ACK_TYPES)
    po_number = an(bak, 3, "purchase order number", 1, 22)
    date = date8(bak, 4, "purchase order date")
    lines: list[AckLine] = []
    current: Line | None = None
    acks_for_current = 0
    for seg in body[1:]:
        if seg.tag == "PO1":
            if current is not None and acks_for_current == 0:
                raise X12Error("E_MISSING_SEGMENT", f"PO1 line {current.line_id} has no ACK", seg.number)
            current, acks_for_current = _po1_line(seg), 0
        elif seg.tag == "ACK":
            if current is None:
                raise X12Error("E_SEGMENT_ORDER", "ACK before any PO1", seg.number)
            status = code(seg, 1, "line item status", ACK_STATUS)
            qty = whole(seg, 2, "quantity", 0) if (status != "IR" or seg.el(2)) else None
            if status in ("IA", "IB", "IQ") and not qty:
                raise X12Error("E_QTY", f"ACK02 quantity must be above zero for status {status}", seg.number)
            uom = code(seg, 3, "unit of measure", UOM_CODES) if seg.el(3) or status != "IR" else current.uom
            lines.append(
                AckLine(current.line_id, status, qty, uom, current.part_qualifier, current.part_number, current.unit_price_cents, current.qty)
            )
            acks_for_current += 1
        elif seg.tag == "CTT":
            pass
        else:
            raise X12Error("E_UNEXPECTED_SEGMENT", f"{seg.tag} is not part of the supported 855 subset", seg.number)
    if current is None:
        raise X12Error("E_MISSING_SEGMENT", "an 855 needs at least one PO1 line", st.number)
    if acks_for_current == 0:
        raise X12Error("E_MISSING_SEGMENT", f"PO1 line {current.line_id} has no ACK", body[-1].number)
    po1_count = len([s for s in body if s.tag == "PO1"])
    _ctt(body, po1_count, "PO1 lines", st)
    return PoAck(po_number, date, tuple(lines), ack_type)


def _parse_856(body: list[Segment], st: Segment) -> Asn:
    bsn = _first(body, "BSN", st)
    code(bsn, 1, "transaction set purpose", frozenset({"00"}))
    shipment_id = an(bsn, 2, "shipment identification", 1, 30)
    date = date8(bsn, 3, "shipment date")
    if not re.fullmatch(r"\d{4}(\d{2})?", need(bsn, 4, "shipment time")):
        raise X12Error("E_TIME", f"BSN04 shipment time must be HHMM or HHMMSS, got {bsn.el(4)!r}", bsn.number)
    hl_total = 0
    levels: dict[str, str] = {}  # hl id -> level code
    orders: list[tuple[str, list[AsnItem]]] = []
    pending_part: tuple[str, str] | None = None
    for seg in body[1:]:
        if seg.tag == "HL":
            if pending_part is not None:
                raise X12Error("E_MISSING_SEGMENT", "an item HL needs a LIN and an SN1", seg.number)
            hl_total += 1
            hid = an(seg, 1, "hierarchical id", 1, 12)
            if hid != str(hl_total):
                raise X12Error("E_HL_ID", f"HL01 must count up from 1: expected {hl_total}, got {hid!r}", seg.number)
            parent = seg.el(2)
            level = code(seg, 3, "hierarchical level", frozenset({"S", "O", "I"}))
            expected_parent = {"S": "", "O": "S", "I": "O"}[level]
            if level == "S":
                if parent or hl_total != 1:
                    raise X12Error("E_HL_PARENT", "the shipment level must be first and have no parent", seg.number)
            else:
                if parent not in levels:
                    raise X12Error("E_HL_PARENT", f"HL02 parent {parent!r} does not name an earlier HL", seg.number)
                if levels[parent] != expected_parent:
                    raise X12Error("E_HL_LEVEL", f"a {level} level cannot sit under a {levels[parent]} level", seg.number)
            levels[hid] = level
            if level == "I":
                pending_part = ("", "")
        elif seg.tag == "PRF":
            if not levels or list(levels.values())[-1] != "O":
                raise X12Error("E_SEGMENT_ORDER", "PRF belongs directly under an order level HL", seg.number)
            orders.append((an(seg, 1, "purchase order number", 1, 22), []))
        elif seg.tag == "LIN":
            if pending_part is None or pending_part != ("", ""):
                raise X12Error("E_SEGMENT_ORDER", "LIN belongs directly under an item level HL", seg.number)
            pending_part = (code(seg, 2, "part number qualifier", PART_QUALIFIERS), an(seg, 3, "part number", 1, 48))
        elif seg.tag == "SN1":
            if pending_part is None or pending_part == ("", ""):
                raise X12Error("E_SEGMENT_ORDER", "SN1 must follow the LIN of an item", seg.number)
            qty = whole(seg, 2, "quantity shipped")
            uom = code(seg, 3, "unit of measure", UOM_CODES)
            orders[-1][1].append(AsnItem(pending_part[0], pending_part[1], qty, uom))
            pending_part = None
        elif seg.tag in ("TD1", "REF", "DTM"):
            pass  # accepted and ignored: packaging, references and dates are outside the subset
        elif seg.tag == "CTT":
            pass
        else:
            raise X12Error("E_UNEXPECTED_SEGMENT", f"{seg.tag} is not part of the supported 856 subset", seg.number)
    if pending_part is not None:
        raise X12Error("E_MISSING_SEGMENT", "the last item HL has no LIN and SN1", body[-1].number)
    if not orders or any(not items for _, items in orders):
        raise X12Error("E_MISSING_SEGMENT", "every order level needs at least one item", st.number)
    _ctt(body, hl_total, "HL segments", st)
    return Asn(shipment_id, date, tuple(AsnOrder(po, tuple(items)) for po, items in orders))


def _parse_810(body: list[Segment], st: Segment) -> Invoice:
    big = _first(body, "BIG", st)
    date = date8(big, 1, "invoice date")
    invoice_number = an(big, 2, "invoice number", 1, 22)
    po_number = an(big, 4, "purchase order number", 1, 22)
    lines: list[Line] = []
    total: int | None = None
    for seg in body[1:]:
        if seg.tag == "IT1":
            lines.append(
                Line(
                    line_id=an(seg, 1, "line number", 1, 20),
                    qty=whole(seg, 2, "quantity invoiced"),
                    uom=code(seg, 3, "unit of measure", UOM_CODES),
                    unit_price_cents=money(seg, 4, "unit price"),
                    part_qualifier=code(seg, 6, "part number qualifier", PART_QUALIFIERS),
                    part_number=an(seg, 7, "part number", 1, 48),
                )
            )
        elif seg.tag == "TDS":
            if total is not None:
                raise X12Error("E_SEGMENT_ORDER", "only one TDS segment is allowed", seg.number)
            value = need(seg, 1, "total invoice amount")
            if not re.fullmatch(r"\d{1,12}", value):
                raise X12Error("E_TOTAL", f"TDS01 is an amount in cents with no decimal point, got {value!r}", seg.number)
            total = int(value)
        elif seg.tag == "CTT":
            pass
        else:
            raise X12Error("E_UNEXPECTED_SEGMENT", f"{seg.tag} is not part of the supported 810 subset", seg.number)
    if not lines:
        raise X12Error("E_MISSING_SEGMENT", "an 810 needs at least one IT1 line", st.number)
    if total is None:
        raise X12Error("E_MISSING_SEGMENT", "an 810 needs a TDS total", st.number)
    expected = sum(ln.qty * ln.unit_price_cents for ln in lines)
    if total != expected:
        raise X12Error("E_TOTAL_MISMATCH", f"TDS01 {total} is not the sum of the lines, {expected}", body[-1].number)
    _ctt(body, len(lines), "IT1 lines", st)
    return Invoice(invoice_number, date, po_number, tuple(lines), total)


# ----------------------------------------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------------------------------------

ES, SS, TERM = "*", ":", "~"


def _isa_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9 ]{1,15}", value):
        raise ValueError(f"interchange id {value!r}: 1 to 15 letters, digits or spaces")
    return value.ljust(15)


def _wrap(set_id: str, body: list[str], sender: str, receiver: str, control: int, when: dt.datetime, usage: str) -> str:
    st_control = f"{control:04d}"
    set_segments = [f"ST{ES}{set_id}{ES}{st_control}", *body]
    set_segments.append(f"SE{ES}{len(set_segments) + 1}{ES}{st_control}")
    gs = f"GS{ES}{SET_TO_GROUP[set_id]}{ES}{sender.strip()}{ES}{receiver.strip()}{ES}{when:%Y%m%d}{ES}{when:%H%M}{ES}{control}{ES}X{ES}005010"
    isa = ES.join(
        [
            "ISA", "00", " " * 10, "00", " " * 10, "ZZ", _isa_id(sender), "ZZ", _isa_id(receiver),
            f"{when:%y%m%d}", f"{when:%H%M}", "^", "00501", f"{control:09d}", "0", usage, SS,
        ]
    )
    segments = [isa, gs, *set_segments, f"GE{ES}1{ES}{control}", f"IEA{ES}1{ES}{control:09d}"]
    return "\n".join(s + TERM for s in segments) + "\n"


def _clean(value: str) -> str:
    if any(c in value for c in (ES, SS, TERM, "\n", "\r")):
        raise ValueError(f"{value!r} contains an X12 separator")
    return value


def build_850(po: PurchaseOrder, sender: str, receiver: str, control: int = 1, when: dt.datetime | None = None, usage: str = "T") -> str:
    body = [f"BEG{ES}00{ES}{po.po_type}{ES}{_clean(po.po_number)}{ES}{ES}{fmt_date(po.date)}"]
    if po.ship_to:
        body.append(f"N1{ES}ST{ES}{_clean(po.ship_to)}")
    for ln in po.lines:
        body.append(_po1(ln))
    body.append(f"CTT{ES}{len(po.lines)}")
    return _wrap("850", body, sender, receiver, control, when or dt.datetime(2026, 10, 25, 9, 0), usage)


def _po1(ln: Line, tag: str = "PO1") -> str:
    return ES.join([tag, _clean(ln.line_id), str(ln.qty), ln.uom, fmt_price(ln.unit_price_cents), "PE", ln.part_qualifier, _clean(ln.part_number)])


def build_855(ack: PoAck, sender: str, receiver: str, control: int = 1, when: dt.datetime | None = None, usage: str = "T") -> str:
    body = [f"BAK{ES}00{ES}{ack.ack_type}{ES}{_clean(ack.po_number)}{ES}{fmt_date(ack.date)}"]
    seen: dict[str, AckLine] = {}
    for a in ack.lines:
        if a.line_id not in seen:
            body.append(_po1(Line(a.line_id, a.ordered_qty or a.qty or 1, a.uom, a.unit_price_cents, a.part_qualifier, a.part_number)))
            seen[a.line_id] = a
        qty = "" if a.qty is None else str(a.qty)
        body.append(f"ACK{ES}{a.status}{ES}{qty}{ES}{a.uom}")
    body.append(f"CTT{ES}{len(seen)}")
    return _wrap("855", body, sender, receiver, control, when or dt.datetime(2026, 10, 25, 9, 0), usage)


def build_856(asn: Asn, sender: str, receiver: str, control: int = 1, when: dt.datetime | None = None, usage: str = "T") -> str:
    body = [f"BSN{ES}00{ES}{_clean(asn.shipment_id)}{ES}{fmt_date(asn.date)}{ES}0930", f"HL{ES}1{ES}{ES}S"]
    hl = 1
    for order in asn.orders:
        hl += 1
        order_id = hl
        body += [f"HL{ES}{hl}{ES}1{ES}O", f"PRF{ES}{_clean(order.po_number)}"]
        for item in order.items:
            hl += 1
            body += [
                f"HL{ES}{hl}{ES}{order_id}{ES}I",
                f"LIN{ES}{ES}{item.part_qualifier}{ES}{_clean(item.part_number)}",
                f"SN1{ES}{ES}{item.qty}{ES}{item.uom}",
            ]
    body.append(f"CTT{ES}{hl}")
    return _wrap("856", body, sender, receiver, control, when or dt.datetime(2026, 10, 25, 9, 0), usage)


def build_810(inv: Invoice, sender: str, receiver: str, control: int = 1, when: dt.datetime | None = None, usage: str = "T") -> str:
    body = [f"BIG{ES}{fmt_date(inv.date)}{ES}{_clean(inv.invoice_number)}{ES}{ES}{_clean(inv.po_number)}"]
    for ln in inv.lines:
        body.append(_po1(ln, "IT1"))
    body += [f"TDS{ES}{inv.total_cents}", f"CTT{ES}{len(inv.lines)}"]
    return _wrap("810", body, sender, receiver, control, when or dt.datetime(2026, 10, 25, 9, 0), usage)
