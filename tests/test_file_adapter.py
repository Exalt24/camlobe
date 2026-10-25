"""The CSV and XML adapter: encodings, split rows, the BOM, positions, and hostile XML."""
import pytest
from defusedxml.common import DTDForbidden

from catalog.ingest.adapters import FileAdapter, decode_line, detect_delimiter

from .helpers import CSV_HEADER, write_csv


def records(adapter, start=0):
    return list(adapter.records(start_after=start))


def test_decode_line_prefers_utf8_and_falls_back_per_line():
    assert decode_line("Étrier".encode("utf-8")) == ("Étrier", False)
    assert decode_line("Étrier".encode("cp1252")) == ("Étrier", True)
    text, fell_back = decode_line(b"abc\x81def")  # 0x81 is undefined in windows-1252
    assert fell_back and "�" in text


def test_a_file_that_mixes_encodings_keeps_every_good_line(tmp_path):
    lines = [
        ",".join(CSV_HEADER).encode(),
        "AB123,Marlow,Étrier de frein,5.00,".encode("cp1252"),  # latin line
        "AB124,Marlow,Pompe à eau,6.00,".encode("utf-8"),  # utf-8 line in the same file
    ]
    path = tmp_path / "mixed.csv"
    path.write_bytes(b"\r\n".join(lines) + b"\r\n")
    adapter = FileAdapter(path)
    recs = records(adapter)
    assert [r.fields["Description"] for r in recs] == ["Étrier de frein", "Pompe à eau"]
    assert adapter.notes()["fallback_lines"] == 1


def test_an_undefined_byte_rejects_that_row_only(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_bytes(
        ",".join(CSV_HEADER).encode() + b"\r\nAB123,Marlow,Oil filter,5.00,\r\nAB124,Marlow,Bad \x81 byte,6.00,\r\nAB125,Marlow,Fine,7.00,\r\n"
    )
    recs = records(FileAdapter(path))
    assert [r.reject for r in recs] == ["", "undecodable_bytes", ""]
    assert [r.position for r in recs] == [1, 2, 3]


def test_a_split_row_is_rejected_not_padded(tmp_path):
    path = tmp_path / "split.csv"
    path.write_bytes(",".join(CSV_HEADER).encode() + b"\r\nAB123,Marlow,Oil filter\r\nAB124,Marlow,Fine,6.00,\r\n")
    recs = records(FileAdapter(path))
    assert [r.reject for r in recs] == ["column_count", ""]


def test_bom_blank_lines_and_semicolons(tmp_path):
    path = tmp_path / "semi.csv"
    path.write_bytes(b"\xef\xbb\xbfsku;brand;cost\r\n\r\nAB123;Marlow;5.00\r\n\r\n")
    adapter = FileAdapter(path)
    recs = records(adapter)
    assert recs[0].fields == {"sku": "AB123", "brand": "Marlow", "cost": "5.00"}
    assert len(recs) == 1 and adapter.notes()["delimiter"] == ";" and adapter.notes()["bom"]
    assert detect_delimiter("a|b|c") == "|"
    assert detect_delimiter("abc") == ","


def test_quoted_fields_with_commas_and_newlines(tmp_path):
    path = write_csv(tmp_path / "q.csv", [["AB123", "Marlow", 'Filter, "oil"\nlong', "5.00", ""]])
    recs = records(FileAdapter(path))
    assert recs[0].fields["Description"] == 'Filter, "oil"\nlong'


def test_start_after_skips_by_position(tmp_path):
    path = write_csv(tmp_path / "p.csv", [[f"AB{i:03d}", "Marlow", "x", "1", ""] for i in range(10)])
    adapter = FileAdapter(path)
    assert [r.position for r in records(adapter, start=7)] == [8, 9, 10]
    assert records(adapter, start=10) == []


def test_fingerprint_follows_the_content_not_the_name(tmp_path):
    a = write_csv(tmp_path / "a.csv", [["AB123", "Marlow", "x", "1", ""]])
    b = write_csv(tmp_path / "b.csv", [["AB123", "Marlow", "x", "1", ""]])
    c = write_csv(tmp_path / "c.csv", [["AB123", "Marlow", "x", "2", ""]])
    assert FileAdapter(a).fingerprint() == FileAdapter(b).fingerprint() != FileAdapter(c).fingerprint()


def test_xml_records_attributes_and_positions(tmp_path):
    path = tmp_path / "f.xml"
    path.write_text(
        '<?xml version="1.0"?><catalog><item><sku>AB123</sku><price currency="USD">5.00</price></item>'
        "<item><sku>AB124</sku><manufacturer>Marlow</manufacturer></item></catalog>",
        encoding="utf-8",
    )
    recs = records(FileAdapter(path))
    assert recs[0].fields == {"sku": "AB123", "price": "5.00", "price@currency": "USD"}
    assert recs[1].fields == {"sku": "AB124", "manufacturer": "Marlow"}
    assert [r.position for r in records(FileAdapter(path), start=1)] == [2]


def test_xml_with_a_doctype_is_refused(tmp_path):
    path = tmp_path / "evil.xml"
    path.write_text(
        '<?xml version="1.0"?><!DOCTYPE catalog [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        "<catalog><item><sku>&b;</sku></item></catalog>",
        encoding="utf-8",
    )
    with pytest.raises(DTDForbidden):
        records(FileAdapter(path))


def test_unknown_format_is_refused(tmp_path):
    with pytest.raises(ValueError):
        FileAdapter(tmp_path / "x.csv", fmt="json")
