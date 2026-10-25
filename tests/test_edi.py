"""X12 850, 855, 856 and 810: round trips, and one test per way a document can be refused.

All documents are invented. Nothing here has been checked against a trading partner.
"""
import datetime as dt
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from catalog.edi import x12
from catalog.edi.x12 import (
    AckLine,
    Asn,
    AsnItem,
    AsnOrder,
    Invoice,
    Line,
    PoAck,
    PurchaseOrder,
    X12Error,
    build_810,
    build_850,
    build_855,
    build_856,
    parse,
)

WHEN = dt.datetime(2026, 10, 25, 9, 30)
SENDER, RECEIVER = "CAMLOBE LAB", "REDLINE"
LINES = (
    Line("1", 4, "EA", 1250, "VP", "MF-H4123A"),
    Line("2", 10, "BX", 799, "VP", "ZR 4519"),
    Line("3", 1, "EA", 14000, "BP", "K80123"),
)
PO = PurchaseOrder("PO-10001", dt.date(2026, 10, 25), LINES, "SA", "Dock 4")
ACK = PoAck(
    "PO-10001", dt.date(2026, 10, 25),
    (
        AckLine("1", "IA", 4, "EA", "VP", "MF-H4123A", 1250, 4),
        AckLine("2", "IQ", 8, "BX", "VP", "ZR 4519", 799, 10),
        AckLine("3", "IR", None, "EA", "BP", "K80123", 14000, 1),
    ),
)
ASN = Asn(
    "SHIP-77", dt.date(2026, 10, 26),
    (AsnOrder("PO-10001", (AsnItem("VP", "MF-H4123A", 4, "EA"), AsnItem("VP", "ZR 4519", 8, "BX"))),),
)
INV = Invoice("INV-5001", dt.date(2026, 10, 27), "PO-10001", LINES[:2], 4 * 1250 + 10 * 799)


def doc850(po=PO):
    return build_850(po, SENDER, RECEIVER, 1, WHEN)


def wrap(set_id, body, control=7):
    return x12._wrap(set_id, body, SENDER, RECEIVER, control, WHEN, "T")


def refused(text, code):
    with pytest.raises(X12Error) as err:
        parse(text)
    assert err.value.code == code, f"expected {code}, got {err.value}"
    return err.value


# ---- round trips -----------------------------------------------------------------------------------


def test_850_round_trip_and_envelope_shape():
    text = doc850()
    assert text.startswith("ISA*00*") and len(text.split("~")[0]) + 1 == 106  # 106 with its terminator
    assert text.splitlines()[-1].startswith("IEA*1*000000001")
    env = parse(text)
    assert (env.sender, env.receiver, env.control_number, env.usage) == (SENDER, RECEIVER, "000000001", "T")
    assert env.documents == [PO]


@pytest.mark.parametrize(
    "build,doc",
    [(build_855, ACK), (build_856, ASN), (build_810, INV)],
)
def test_other_documents_round_trip(build, doc):
    env = parse(build(doc, SENDER, RECEIVER, 5, WHEN))
    assert env.documents == [doc]


def test_a_rejected_line_may_carry_no_quantity_and_survives_the_round_trip():
    parsed = parse(build_855(ACK, SENDER, RECEIVER, 1, WHEN)).documents[0]
    assert parsed.lines[2].status == "IR" and parsed.lines[2].qty is None


def test_the_writer_refuses_a_value_that_contains_a_separator():
    bad = PurchaseOrder("PO*1", PO.date, LINES)
    with pytest.raises(ValueError, match="separator"):
        build_850(bad, SENDER, RECEIVER)
    with pytest.raises(ValueError, match="interchange id"):
        build_850(PO, "A" * 16, RECEIVER)


def test_money_is_integer_cents_in_and_out():
    po = PurchaseOrder("PO-1", PO.date, (Line("1", 3, "EA", 1999, "VP", "AB123"),))
    assert "PO1*1*3*EA*19.99*PE*VP*AB123~" in build_850(po, SENDER, RECEIVER)
    assert parse(build_850(po, SENDER, RECEIVER)).documents[0].lines[0].unit_price_cents == 1999


def test_several_documents_in_one_interchange_are_all_read():
    second = ["ST*850*0002", "BEG*00*SA*PO-2**20261025", "PO1*1*4*EA*12.50*PE*VP*MF-H4123A", "CTT*1", "SE*5*0002"]
    text = doc850().replace("GE*1*1~", "~\n".join(second) + "~\nGE*2*1~")
    assert [d.po_number for d in parse(text).documents] == ["PO-10001", "PO-2"]


# ---- the envelope ----------------------------------------------------------------------------------


def swap(text, old, new, count=1):
    assert old in text, f"{old!r} not in the document"
    return text.replace(old, new, count)


def drop(text, prefix):
    lines = [ln for ln in text.splitlines() if not ln.startswith(prefix)]
    assert len(lines) < len(text.splitlines()), f"no {prefix} segment to drop"
    return "\n".join(lines) + "\n"


T = doc850()
ENVELOPE_CASES = [
    ("empty", "", "E_ISA_MISSING"),
    ("not an ISA", "GS*PO*A*B~", "E_ISA_MISSING"),
    ("short ISA", T[:60], "E_ISA_SHORT"),
    ("fixed width broken", swap(swap(T, "ISA*00*          *00*          *ZZ*", "ISA*00*          *00*          *ZZZ*"), "*ZZ*REDLINE        *", "*Z*REDLINE        *"), "E_ISA_FIXED_WIDTH"),
    ("repeated separator", T[:104] + "*" + T[105:], "E_ISA_SEPARATORS"),
    ("alphanumeric terminator", T[:105] + "A" + T[106:], "E_ISA_SEPARATORS"),
    ("wrong version", swap(T, "*00501*", "*00401*"), "E_VERSION"),
    ("ISA13 not numeric", swap(T, "*000000001*0*T*", "*00000000A*0*T*"), "E_ISA_CONTROL"),
    ("usage code", swap(T, "*0*T*:", "*0*X*:"), "E_CODE"),
    ("ISA date", swap(T, "*261025*0930*", "*26AB25*0930*"), "E_DATE"),
    ("no IEA", drop(T, "IEA"), "E_NO_IEA"),
    ("IEA count", swap(T, "IEA*1*", "IEA*2*"), "E_IEA_COUNT"),
    ("IEA control", swap(T, "IEA*1*000000001", "IEA*1*000000002"), "E_IEA_CONTROL"),
    ("segment after IEA", T + "ST*850*0009~\n", "E_TRAILING_DATA"),
    ("unterminated", T + "GARBAGE", "E_UNTERMINATED"),
    ("no GS", drop(drop(T, "GS"), "GE"), "E_GS_MISSING"),
    ("GS identifier unknown", swap(T, "GS*PO*", "GS*XX*"), "E_UNSUPPORTED_SET"),
    ("GS does not match ST", swap(T, "GS*PO*", "GS*SH*"), "E_GS_MISMATCH"),
    ("GS version", swap(T, "*X*005010~", "*X*004010~"), "E_VERSION"),
    ("GS06 empty", swap(T, "*1*X*005010~", "**X*005010~"), "E_ELEMENT_REQUIRED"),
    ("no GE", drop(T, "GE"), "E_ST_MISSING"),
    ("nothing after the last SE", drop(drop(T, "GE"), "IEA"), "E_GE_MISSING"),
    ("GE count", swap(T, "GE*1*1~", "GE*3*1~"), "E_GE_COUNT"),
    ("GE control", swap(T, "GE*1*1~", "GE*1*2~"), "E_GE_CONTROL"),
    ("bad tag", swap(T, "CTT*3~", "ctt*3~"), "E_BAD_TAG"),
    ("ST unsupported set", swap(T, "ST*850*", "ST*997*"), "E_UNSUPPORTED_SET"),
    ("no SE", drop(T, "SE"), "E_SE_MISSING"),
    ("SE count", swap(T, "SE*8*", "SE*9*"), "E_SE_COUNT"),
    ("SE control", swap(T, "*0001~\nGE", "*0002~\nGE"), "E_SE_CONTROL"),
    ("ST02 too short", swap(swap(T, "ST*850*0001", "ST*850*01"), "*0001~\nGE", "*01~\nGE"), "E_ELEMENT_LENGTH"),
]


@pytest.mark.parametrize("name,text,code", ENVELOPE_CASES, ids=[c[0] for c in ENVELOPE_CASES])
def test_envelope_refusals(name, text, code):
    refused(text, code)


def test_the_valid_baseline_really_is_valid():
    assert len(parse(T).documents) == 1  # every refusal above is one edit away from this


def test_an_error_names_the_segment():
    err = refused(swap(T, "GE*1*1~", "GE*3*1~"), "E_GE_COUNT")
    assert err.segment_number == 11 and "segment 11" in str(err)


# ---- 850 -------------------------------------------------------------------------------------------

BEG = "BEG*00*SA*PO-10001**20261025"
P1 = "PO1*1*4*EA*12.50*PE*VP*MF-H4123A"
P2 = "PO1*2*10*BX*7.99*PE*VP*ZR4519"
CTT2 = "CTT*2"


def body850(*parts):
    return wrap("850", list(parts))


C850 = [
    ("valid", [BEG, P1, P2, CTT2], None),
    ("no BEG", [P1, P2, CTT2], "E_MISSING_SEGMENT"),
    ("BEG01 not 00", ["BEG*05*SA*PO-10001**20261025", P1, P2, CTT2], "E_CODE"),
    ("PO type", ["BEG*00*ZZ*PO-10001**20261025", P1, P2, CTT2], "E_CODE"),
    ("PO number missing", ["BEG*00*SA***20261025", P1, P2, CTT2], "E_ELEMENT_REQUIRED"),
    ("PO number too long", ["BEG*00*SA*" + "9" * 23 + "**20261025", P1, P2, CTT2], "E_ELEMENT_LENGTH"),
    ("date not a date", ["BEG*00*SA*PO-1**20261301", P1, P2, CTT2], "E_DATE"),
    ("date with dashes", ["BEG*00*SA*PO-1**2026-10-25", P1, P2, CTT2], "E_DATE"),
    ("qty zero", [BEG, "PO1*1*0*EA*12.50*PE*VP*A1B", P2, CTT2], "E_QTY"),
    ("qty fraction", [BEG, "PO1*1*1.5*EA*12.50*PE*VP*A1B", P2, CTT2], "E_QTY"),
    ("qty negative", [BEG, "PO1*1*-2*EA*12.50*PE*VP*A1B", P2, CTT2], "E_QTY"),
    ("qty missing", [BEG, "PO1*1**EA*12.50*PE*VP*A1B", P2, CTT2], "E_ELEMENT_REQUIRED"),
    ("unit of measure", [BEG, "PO1*1*1*XX*12.50*PE*VP*A1B", P2, CTT2], "E_CODE"),
    ("price three decimals", [BEG, "PO1*1*1*EA*12.505*PE*VP*A1B", P2, CTT2], "E_PRICE"),
    ("price not a number", [BEG, "PO1*1*1*EA*abc*PE*VP*A1B", P2, CTT2], "E_PRICE"),
    ("price negative", [BEG, "PO1*1*1*EA*-5*PE*VP*A1B", P2, CTT2], "E_PRICE"),
    ("qualifier", [BEG, "PO1*1*1*EA*12.50*PE*ZZ*A1B", P2, CTT2], "E_CODE"),
    ("part number missing", [BEG, "PO1*1*1*EA*12.50*PE*VP*", P2, CTT2], "E_ELEMENT_REQUIRED"),
    ("part number too long", [BEG, "PO1*1*1*EA*12.50*PE*VP*" + "A" * 49, P2, CTT2], "E_ELEMENT_LENGTH"),
    ("part number not basic characters", [BEG, "PO1*1*1*EA*12.50*PE*VP*Aé1", P2, CTT2], "E_ELEMENT_TYPE"),
    ("duplicate line", [BEG, P1, "PO1*1*2*EA*1.00*PE*VP*XX9", CTT2], "E_DUPLICATE_LINE"),
    ("no lines", [BEG, "CTT*0"], "E_MISSING_SEGMENT"),
    ("CTT count", [BEG, P1, P2, "CTT*3"], "E_CTT_COUNT"),
    ("no CTT", [BEG, P1, P2], "E_MISSING_SEGMENT"),
    ("CTT not last", [BEG, P1, CTT2, P2], "E_SEGMENT_ORDER"),
    ("segment outside the subset", [BEG, P1, "XYZ*1", P2, CTT2], "E_UNEXPECTED_SEGMENT"),
    ("N1 after a line", [BEG, P1, "N1*ST*Dock 4", P2, CTT2], "E_SEGMENT_ORDER"),
    ("N1 qualifier", [BEG, "N1*BY*Dock 4", P1, P2, CTT2], "E_CODE"),
]


@pytest.mark.parametrize("name,body,code", C850, ids=[c[0] for c in C850])
def test_850_content(name, body, code):
    text = body850(*body)
    if code is None:
        assert parse(text).documents[0].lines[1].unit_price_cents == 799
    else:
        refused(text, code)


# ---- 855 -------------------------------------------------------------------------------------------

BAK = "BAK*00*AC*PO-10001*20261025"
A1 = "PO1*1*4*EA*12.50*PE*VP*MF-H4123A"
K1 = "ACK*IA*4*EA"
A2 = "PO1*2*10*BX*7.99*PE*VP*ZR4519"
K2 = "ACK*IB*10*BX"
C855 = [
    ("valid", [BAK, A1, K1, A2, K2, "CTT*2"], None),
    ("ack type", ["BAK*00*ZZ*PO-10001*20261025", A1, K1, "CTT*1"], "E_CODE"),
    ("no BAK", [A1, K1, "CTT*1"], "E_MISSING_SEGMENT"),
    ("PO1 without ACK in the middle", [BAK, A1, A2, K2, "CTT*2"], "E_MISSING_SEGMENT"),
    ("PO1 without ACK at the end", [BAK, A1, K1, A2, "CTT*2"], "E_MISSING_SEGMENT"),
    ("ACK before any PO1", [BAK, K1, A1, "CTT*1"], "E_SEGMENT_ORDER"),
    ("status", [BAK, A1, "ACK*XX*4*EA", "CTT*1"], "E_CODE"),
    ("accepted with zero", [BAK, A1, "ACK*IA*0*EA", "CTT*1"], "E_QTY"),
    ("accepted without a quantity", [BAK, A1, "ACK*IA**EA", "CTT*1"], "E_ELEMENT_REQUIRED"),
    ("CTT count", [BAK, A1, K1, "CTT*2"], "E_CTT_COUNT"),
    ("no lines at all", [BAK, "CTT*0"], "E_MISSING_SEGMENT"),
    ("segment outside the subset", [BAK, A1, K1, "XYZ*1", "CTT*1"], "E_UNEXPECTED_SEGMENT"),
]


@pytest.mark.parametrize("name,body,code", C855, ids=[c[0] for c in C855])
def test_855_content(name, body, code):
    text = wrap("855", body)
    if code is None:
        lines = parse(text).documents[0].lines
        assert [(ln.status, ln.qty, ln.ordered_qty) for ln in lines] == [("IA", 4, 4), ("IB", 10, 10)]
    else:
        refused(text, code)


# ---- 856 -------------------------------------------------------------------------------------------

BSN = "BSN*00*SHIP-77*20261026*0930"
H1, H2, H3 = "HL*1**S", "HL*2*1*O", "HL*3*2*I"
PRF = "PRF*PO-10001"
LIN, SN1 = "LIN**VP*MF-H4123A", "SN1**4*EA"
GOOD856 = [BSN, H1, H2, PRF, H3, LIN, SN1, "CTT*3"]
C856 = [
    ("valid", GOOD856, None),
    ("no BSN", GOOD856[1:], "E_MISSING_SEGMENT"),
    ("time", ["BSN*00*SHIP-77*20261026*9:30", *GOOD856[1:]], "E_TIME"),
    ("date", ["BSN*00*SHIP-77*20260230*0930", *GOOD856[1:]], "E_DATE"),
    ("HL id out of sequence", [BSN, H1, "HL*3*1*O", PRF, "HL*4*3*I", LIN, SN1, "CTT*3"], "E_HL_ID"),
    ("parent does not exist", [BSN, H1, "HL*2*9*O", PRF, H3, LIN, SN1, "CTT*3"], "E_HL_PARENT"),
    ("item directly under the shipment", [BSN, H1, "HL*2*1*I", LIN, SN1, "CTT*2"], "E_HL_LEVEL"),
    ("shipment has a parent", [BSN, "HL*1*7*S", H2, PRF, H3, LIN, SN1, "CTT*3"], "E_HL_PARENT"),
    ("second shipment level", [BSN, H1, "HL*2*1*S", "CTT*2"], "E_HL_PARENT"),
    ("level code", [BSN, H1, "HL*2*1*X", "CTT*2"], "E_CODE"),
    ("PRF under the shipment", [BSN, H1, PRF, "CTT*1"], "E_SEGMENT_ORDER"),
    ("item with no LIN", [BSN, H1, H2, PRF, H3, "HL*4*2*I", LIN, SN1, "CTT*4"], "E_MISSING_SEGMENT"),
    ("SN1 before LIN", [BSN, H1, H2, PRF, H3, SN1, LIN, "CTT*3"], "E_SEGMENT_ORDER"),
    ("LIN outside an item", [BSN, H1, H2, PRF, LIN, "CTT*2"], "E_SEGMENT_ORDER"),
    ("shipped zero", [BSN, H1, H2, PRF, H3, LIN, "SN1**0*EA", "CTT*3"], "E_QTY"),
    ("shipped unit of measure", [BSN, H1, H2, PRF, H3, LIN, "SN1**4*XX", "CTT*3"], "E_CODE"),
    ("part qualifier", [BSN, H1, H2, PRF, H3, "LIN**ZZ*MF-H4123A", SN1, "CTT*3"], "E_CODE"),
    ("CTT count", [BSN, H1, H2, PRF, H3, LIN, SN1, "CTT*2"], "E_CTT_COUNT"),
    ("order with no items", [BSN, H1, H2, PRF, "CTT*2"], "E_MISSING_SEGMENT"),
    ("last item has no SN1", [BSN, H1, H2, PRF, H3, LIN, "CTT*3"], "E_MISSING_SEGMENT"),
    ("ignored segments are tolerated", [BSN, H1, "TD1*CTN25*2", "REF*BM*B1", H2, PRF, H3, LIN, SN1, "CTT*3"], None),
    ("segment outside the subset", [BSN, H1, "XYZ*1", H2, PRF, H3, LIN, SN1, "CTT*3"], "E_UNEXPECTED_SEGMENT"),
]


@pytest.mark.parametrize("name,body,code", C856, ids=[c[0] for c in C856])
def test_856_content(name, body, code):
    text = wrap("856", body)
    if code is None:
        asn = parse(text).documents[0]
        assert asn.orders[0].items == (AsnItem("VP", "MF-H4123A", 4, "EA"),)
    else:
        refused(text, code)


# ---- 810 -------------------------------------------------------------------------------------------

BIG = "BIG*20261027*INV-5001**PO-10001"
I1 = "IT1*1*4*EA*12.50*PE*VP*MF-H4123A"
I2 = "IT1*2*10*BX*7.99*PE*VP*ZR4519"
TDS = f"TDS*{4 * 1250 + 10 * 799}"
C810 = [
    ("valid", [BIG, I1, I2, TDS, "CTT*2"], None),
    ("total does not add up", [BIG, I1, I2, "TDS*12345", "CTT*2"], "E_TOTAL_MISMATCH"),
    ("total with a decimal point", [BIG, I1, I2, "TDS*129.90", "CTT*2"], "E_TOTAL"),
    ("no TDS", [BIG, I1, I2, "CTT*2"], "E_MISSING_SEGMENT"),
    ("two TDS", [BIG, I1, I2, TDS, TDS, "CTT*2"], "E_SEGMENT_ORDER"),
    ("no lines", [BIG, "TDS*0", "CTT*0"], "E_MISSING_SEGMENT"),
    ("invoice date", ["BIG*20261331*INV-5001**PO-10001", I1, I2, TDS, "CTT*2"], "E_DATE"),
    ("price over two decimals", [BIG, "IT1*1*4*EA*12.501*PE*VP*A1B", I2, TDS, "CTT*2"], "E_PRICE"),
    ("PO number missing", ["BIG*20261027*INV-5001***", I1, I2, TDS, "CTT*2"], "E_ELEMENT_REQUIRED"),
    ("CTT count", [BIG, I1, I2, TDS, "CTT*3"], "E_CTT_COUNT"),
    ("segment outside the subset", [BIG, I1, I2, "XYZ*1", TDS, "CTT*2"], "E_UNEXPECTED_SEGMENT"),
]


@pytest.mark.parametrize("name,body,code", C810, ids=[c[0] for c in C810])
def test_810_content(name, body, code):
    text = wrap("810", body)
    if code is None:
        assert parse(text).documents[0].total_cents == 12990
    else:
        refused(text, code)


# ---- properties ------------------------------------------------------------------------------------

LINE = st.builds(
    Line,
    line_id=st.text(alphabet="0123456789", min_size=1, max_size=4),
    qty=st.integers(1, 5000),
    uom=st.sampled_from(sorted(x12.UOM_CODES)),
    unit_price_cents=st.integers(1, 5_000_000),
    part_qualifier=st.sampled_from(sorted(x12.PART_QUALIFIERS)),
    part_number=st.text(alphabet="ABCDEFGHJKLMNPQRSTUVWXYZ0123456789-. ", min_size=1, max_size=30).filter(lambda s: s.strip() == s and "  " not in s),
)
POS = st.builds(
    PurchaseOrder,
    po_number=st.text(alphabet="ABCDEF0123456789-", min_size=1, max_size=22),
    date=st.dates(dt.date(2000, 1, 1), dt.date(2099, 12, 31)),
    lines=st.lists(LINE, min_size=1, max_size=6, unique_by=lambda ln: ln.line_id).map(tuple),
    po_type=st.sampled_from(sorted(x12.PO_TYPES)),
    ship_to=st.just("") | st.text(alphabet="ABCDEFG abcdefg", min_size=1, max_size=30).filter(lambda s: s.strip() == s),
)


@given(POS)
@settings(max_examples=150, deadline=None)
def test_any_purchase_order_survives_write_then_read(po):
    assert parse(build_850(po, SENDER, RECEIVER, 12, WHEN)).documents == [po]


@given(st.integers(0, 10_000), st.sampled_from(["delete", "replace", "truncate", "insert"]))
@settings(max_examples=400, deadline=None)
def test_a_damaged_document_is_read_or_refused_never_a_crash(seed, kind):
    rng = random.Random(seed)
    text = rng.choice([doc850(), build_855(ACK, SENDER, RECEIVER, 3, WHEN), build_856(ASN, SENDER, RECEIVER, 4, WHEN), build_810(INV, SENDER, RECEIVER, 5, WHEN)])
    i = rng.randrange(len(text))
    if kind == "delete":
        damaged = text[:i] + text[i + rng.randint(1, 5):]
    elif kind == "replace":
        damaged = text[:i] + rng.choice("*~:^ABZ09-.\né\x00") + text[i + 1:]
    elif kind == "truncate":
        damaged = text[:i]
    else:
        damaged = text[:i] + rng.choice(["*", "~", "::", "\r\n", "99"]) + text[i:]
    try:
        parse(damaged)
    except X12Error as exc:
        assert exc.code.startswith("E_")
