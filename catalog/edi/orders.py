"""From the catalog to an 850, and from an 855, 856 or 810 back to what it says about that order.

`purchase_order_for` builds an 850 from the supplier's own part numbers and costs held in PartSource, so
the order uses the number the supplier will recognise, not our canonical one. `reconcile` compares what
came back with the order and lists every difference, never fixing one silently.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..models import Part, PartSource, Supplier
from .x12 import Asn, Invoice, Line, PoAck, PurchaseOrder


class OrderError(Exception):
    pass


def purchase_order_for(supplier_code: str, items: list[tuple[int, int]], po_number: str, date: dt.date, ship_to: str = "") -> PurchaseOrder:
    """items: (part id, quantity). Raises OrderError for a part the supplier does not list or has no cost for."""
    if not items:
        raise OrderError("an order needs at least one line")
    try:
        supplier = Supplier.objects.get(code=supplier_code)
    except Supplier.DoesNotExist:
        raise OrderError(f"no supplier {supplier_code!r}")
    lines = []
    for n, (part_id, qty) in enumerate(items, start=1):
        if qty < 1:
            raise OrderError(f"part {part_id}: quantity must be at least 1")
        try:
            part = Part.objects.get(pk=part_id)
        except Part.DoesNotExist:
            raise OrderError(f"no part {part_id}")
        source = PartSource.objects.filter(part=part, supplier=supplier).order_by("-last_seen").first()
        if source is None:
            raise OrderError(f"{supplier_code} does not list {part.brand} {part.number_norm}")
        if source.cost_cents is None:
            raise OrderError(f"{supplier_code} has no cost for {part.brand} {part.number_norm}: it cannot be ordered")
        lines.append(Line(str(n), qty, "EA", source.cost_cents, "VP", source.supplier_part_number))
    return PurchaseOrder(po_number, date, tuple(lines), ship_to=ship_to)


@dataclass(frozen=True)
class Discrepancy:
    code: str
    detail: str


def reconcile(po: PurchaseOrder, ack: PoAck | None = None, asn: Asn | None = None, invoice: Invoice | None = None) -> list[Discrepancy]:
    found: list[Discrepancy] = []
    by_line = {ln.line_id: ln for ln in po.lines}
    by_part = {ln.part_number: ln for ln in po.lines}
    if ack is not None:
        if ack.po_number != po.po_number:
            found.append(Discrepancy("ACK_WRONG_PO", f"the acknowledgement is for {ack.po_number}, not {po.po_number}"))
        acked = set()
        for a in ack.lines:
            line = by_line.get(a.line_id)
            if line is None:
                found.append(Discrepancy("ACK_UNKNOWN_LINE", f"line {a.line_id} is not on the order"))
                continue
            acked.add(a.line_id)
            if a.part_number != line.part_number:
                found.append(Discrepancy("ACK_PART_CHANGED", f"line {a.line_id}: ordered {line.part_number}, acknowledged {a.part_number}"))
            if a.status == "IR":
                found.append(Discrepancy("LINE_REJECTED", f"line {a.line_id} ({line.part_number}) was rejected"))
            elif a.status == "IB":
                found.append(Discrepancy("LINE_BACKORDERED", f"line {a.line_id} ({line.part_number}) is backordered: {a.qty} of {line.qty}"))
            elif a.qty != line.qty:
                found.append(Discrepancy("ACK_QTY_CHANGED", f"line {a.line_id}: ordered {line.qty}, acknowledged {a.qty}"))
            if a.unit_price_cents and a.unit_price_cents != line.unit_price_cents:
                found.append(Discrepancy("ACK_PRICE_CHANGED", f"line {a.line_id}: ordered at {line.unit_price_cents}, acknowledged at {a.unit_price_cents} cents"))
        for line in po.lines:
            if line.line_id not in acked:
                found.append(Discrepancy("LINE_NOT_ACKNOWLEDGED", f"line {line.line_id} ({line.part_number}) has no acknowledgement"))
    shipped: dict[str, int] = {}
    if asn is not None:
        for order in asn.orders:
            if order.po_number != po.po_number:
                found.append(Discrepancy("ASN_WRONG_PO", f"the shipment lists order {order.po_number}, not {po.po_number}"))
                continue
            for item in order.items:
                line = by_part.get(item.part_number)
                if line is None:
                    found.append(Discrepancy("ASN_UNKNOWN_PART", f"{item.part_number} was shipped but is not on the order"))
                    continue
                shipped[line.line_id] = shipped.get(line.line_id, 0) + item.qty
        for line_id, qty in shipped.items():
            if qty > by_line[line_id].qty:
                found.append(Discrepancy("ASN_OVERSHIP", f"line {line_id}: {qty} shipped against {by_line[line_id].qty} ordered"))
    if invoice is not None:
        if invoice.po_number != po.po_number:
            found.append(Discrepancy("INVOICE_WRONG_PO", f"the invoice is for {invoice.po_number}, not {po.po_number}"))
        for inv in invoice.lines:
            line = by_part.get(inv.part_number)
            if line is None:
                found.append(Discrepancy("INVOICE_UNKNOWN_PART", f"{inv.part_number} is invoiced but not on the order"))
                continue
            if inv.unit_price_cents != line.unit_price_cents:
                found.append(Discrepancy("INVOICE_PRICE", f"line {line.line_id}: ordered at {line.unit_price_cents}, invoiced at {inv.unit_price_cents} cents"))
            if inv.qty > line.qty:
                found.append(Discrepancy("INVOICE_QTY_OVER_ORDER", f"line {line.line_id}: invoiced {inv.qty}, ordered {line.qty}"))
            if asn is not None and inv.qty > shipped.get(line.line_id, 0):
                found.append(Discrepancy("INVOICE_QTY_OVER_SHIPPED", f"line {line.line_id}: invoiced {inv.qty}, shipped {shipped.get(line.line_id, 0)}"))
    return found
