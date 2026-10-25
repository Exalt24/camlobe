"""From the catalog to an 850, and from the answers back to a list of differences."""
import dataclasses
import datetime as dt

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from catalog.edi.orders import OrderError, purchase_order_for, reconcile
from catalog.edi.x12 import Asn, AsnItem, AsnOrder, Invoice, PoAck, AckLine, build_850, parse
from catalog.models import Part

from .helpers import get_supplier, row, run_all, write_csv

DATE = dt.date(2026, 10, 25)


def codes(found):
    return sorted(d.code for d in found)


@pytest.fixture
def catalog(tmp_path, db):
    run_all(
        get_supplier("redline"),
        write_csv(tmp_path / "r.csv", [row("MF-H4123A", "Marlow", "Oil filter", "12.50"), row("ZR 4519", "Dunmore", "Plug", "7.99"), row("K80123", "Halden", "Strut", "")]),
    )
    run_all(get_supplier("delta"), write_csv(tmp_path / "d.csv", [row("H4123A", "Marlow", "Oil filter", "11.00")]))
    return {p.number_norm: p.pk for p in Part.objects.all()}


def test_the_order_uses_the_suppliers_own_number_and_cost(catalog):
    po = purchase_order_for("redline", [(catalog["H4123A"], 4), (catalog["ZR4519"], 10)], "PO-1", DATE, "Dock 4")
    assert [(ln.part_number, ln.qty, ln.unit_price_cents, ln.part_qualifier) for ln in po.lines] == [
        ("MF-H4123A", 4, 1250, "VP"), ("ZR 4519", 10, 799, "VP"),
    ]
    other = purchase_order_for("delta", [(catalog["H4123A"], 1)], "PO-2", DATE)
    assert other.lines[0].part_number == "H4123A" and other.lines[0].unit_price_cents == 1100
    assert parse(build_850(po, "CAMLOBE LAB", "REDLINE", 1)).documents == [po]


@pytest.mark.parametrize(
    "supplier,items,message",
    [
        ("redline", [], "at least one line"),
        ("nobody", [(1, 1)], "no supplier"),
        ("redline", [(999999, 1)], "no part"),
        ("redline", [("K80123", 0)], "at least 1"),
        ("delta", [("ZR4519", 1)], "does not list"),  # delta never listed that part
        ("redline", [("K80123", 1)], "no cost"),  # redline lists it without a price
    ],
)
def test_orders_that_cannot_be_placed_are_refused(catalog, supplier, items, message):
    items = [(catalog.get(p, p) if isinstance(p, str) else p, q) for p, q in items]
    with pytest.raises(OrderError, match=message):
        purchase_order_for(supplier, items, "PO-9", DATE)


# ---- reconcile -------------------------------------------------------------------------------------


@pytest.fixture
def po(catalog):
    return purchase_order_for("redline", [(catalog["H4123A"], 4), (catalog["ZR4519"], 10)], "PO-1", DATE)


def ack_for(po, **changes):
    lines = tuple(AckLine(ln.line_id, "IA", ln.qty, ln.uom, ln.part_qualifier, ln.part_number, ln.unit_price_cents, ln.qty) for ln in po.lines)
    return PoAck(po.po_number, DATE, lines)


def test_a_clean_run_of_documents_reconciles_to_nothing(po):
    ack = ack_for(po)
    asn = Asn("S1", DATE, (AsnOrder("PO-1", tuple(AsnItem("VP", ln.part_number, ln.qty, ln.uom) for ln in po.lines)),))
    inv = Invoice("I1", DATE, "PO-1", po.lines, sum(ln.qty * ln.unit_price_cents for ln in po.lines))
    assert reconcile(po, ack, asn, inv) == []
    assert reconcile(po) == []


def test_an_acknowledgement_that_changes_the_order(po):
    first, second = po.lines
    ack = PoAck(
        "PO-1", DATE,
        (
            AckLine("1", "IQ", 3, "EA", "VP", first.part_number, first.unit_price_cents, 4),
            AckLine("2", "IR", None, "BX", "VP", second.part_number, second.unit_price_cents, 10),
            AckLine("9", "IA", 1, "EA", "VP", "ZZ9", 100, 1),
        ),
    )
    assert codes(reconcile(po, ack)) == ["ACK_QTY_CHANGED", "ACK_UNKNOWN_LINE", "LINE_REJECTED"]
    backordered = PoAck("PO-1", DATE, (dataclasses.replace(ack_for(po).lines[0], status="IB", qty=1), ack_for(po).lines[1]))
    assert codes(reconcile(po, backordered)) == ["LINE_BACKORDERED"]
    assert codes(reconcile(po, PoAck("PO-1", DATE, ack_for(po).lines[:1]))) == ["LINE_NOT_ACKNOWLEDGED"]
    assert codes(reconcile(po, PoAck("PO-OTHER", DATE, ack_for(po).lines))) == ["ACK_WRONG_PO"]
    renamed = PoAck("PO-1", DATE, (dataclasses.replace(ack_for(po).lines[0], part_number="OTHER1"), ack_for(po).lines[1]))
    assert codes(reconcile(po, renamed)) == ["ACK_PART_CHANGED"]
    repriced = PoAck("PO-1", DATE, (dataclasses.replace(ack_for(po).lines[0], unit_price_cents=9999), ack_for(po).lines[1]))
    assert codes(reconcile(po, repriced)) == ["ACK_PRICE_CHANGED"]


def test_a_shipment_that_does_not_match_the_order(po):
    first = po.lines[0]
    over = Asn("S1", DATE, (AsnOrder("PO-1", (AsnItem("VP", first.part_number, 5, "EA"),)),))
    assert codes(reconcile(po, asn=over)) == ["ASN_OVERSHIP"]
    split = Asn("S1", DATE, (AsnOrder("PO-1", (AsnItem("VP", first.part_number, 2, "EA"), AsnItem("VP", first.part_number, 3, "EA"))),))
    assert codes(reconcile(po, asn=split)) == ["ASN_OVERSHIP"]  # two boxes add up to more than ordered
    stranger = Asn("S1", DATE, (AsnOrder("PO-1", (AsnItem("VP", "NOT-ORDERED", 1, "EA"),)),))
    assert codes(reconcile(po, asn=stranger)) == ["ASN_UNKNOWN_PART"]
    elsewhere = Asn("S1", DATE, (AsnOrder("PO-77", (AsnItem("VP", first.part_number, 1, "EA"),)),))
    assert codes(reconcile(po, asn=elsewhere)) == ["ASN_WRONG_PO"]


def test_an_invoice_that_does_not_match(po):
    first, second = po.lines
    repriced = dataclasses.replace(first, unit_price_cents=first.unit_price_cents + 100)
    inv = Invoice("I1", DATE, "PO-1", (repriced, second), repriced.qty * repriced.unit_price_cents + second.qty * second.unit_price_cents)
    assert codes(reconcile(po, invoice=inv)) == ["INVOICE_PRICE"]
    more = dataclasses.replace(first, qty=first.qty + 1)
    inv2 = Invoice("I1", DATE, "PO-1", (more,), more.qty * more.unit_price_cents)
    assert codes(reconcile(po, invoice=inv2)) == ["INVOICE_QTY_OVER_ORDER"]
    asn = Asn("S1", DATE, (AsnOrder("PO-1", (AsnItem("VP", first.part_number, 2, "EA"),)),))
    inv3 = Invoice("I1", DATE, "PO-1", (first,), first.qty * first.unit_price_cents)
    assert codes(reconcile(po, asn=asn, invoice=inv3)) == ["INVOICE_QTY_OVER_SHIPPED"]
    unknown = dataclasses.replace(first, part_number="NOT-ORDERED")
    assert codes(reconcile(po, invoice=Invoice("I1", DATE, "PO-1", (unknown,), unknown.qty * unknown.unit_price_cents))) == ["INVOICE_UNKNOWN_PART"]
    assert codes(reconcile(po, invoice=Invoice("I1", DATE, "PO-9", (first,), first.qty * first.unit_price_cents))) == ["INVOICE_WRONG_PO"]


# ---- commands --------------------------------------------------------------------------------------


def test_edi_po_and_edi_parse_commands(catalog, tmp_path, capsys):
    out = tmp_path / "po.edi"
    call_command("edi_po", "redline", f"{catalog['H4123A']}:4", f"{catalog['ZR4519']}:2", "--po", "PO-55", "--out", str(out))
    assert "wrote" in capsys.readouterr().out
    text = out.read_text(encoding="ascii")
    assert text.startswith("ISA*") and "PO1*1*4*EA*12.50*PE*VP*MF-H4123A~" in text
    call_command("edi_parse", str(out))
    shown = capsys.readouterr().out
    assert "PurchaseOrder" in shown and "PO-55" in shown
    call_command("edi_po", "redline", f"{catalog['H4123A']}:1", "--po", "PO-56")
    assert "ISA*" in capsys.readouterr().out


def test_edi_commands_refuse_bad_input(catalog, tmp_path):
    with pytest.raises(CommandError, match="PART_ID:QTY"):
        call_command("edi_po", "redline", "abc", "--po", "PO-1")
    with pytest.raises(CommandError, match="does not list"):
        call_command("edi_po", "delta", f"{catalog['ZR4519']}:1", "--po", "PO-1")
    with pytest.raises(CommandError, match="separator"):
        call_command("edi_po", "redline", f"{catalog['H4123A']}:1", "--po", "PO*1")
    bad = tmp_path / "bad.edi"
    bad.write_text("GS*PO*A~", encoding="ascii")
    with pytest.raises(CommandError, match="E_ISA_MISSING"):
        call_command("edi_parse", str(bad))
    with pytest.raises(CommandError, match="cannot read"):
        call_command("edi_parse", str(tmp_path / "missing.edi"))
