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


# ---- ordinary feed shapes that used to fail the whole run -------------------------------------------


def test_xml_with_a_default_namespace_reads_with_plain_field_names(tmp_path):
    path = tmp_path / "ns.xml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><catalog xmlns="urn:example:catalog"><item><sku>AB123</sku>'
        '<price currency="USD">5.00</price></item></catalog>',
        encoding="utf-8",
    )
    assert records(FileAdapter(path))[0].fields == {"sku": "AB123", "price": "5.00", "price@currency": "USD"}


def test_xml_with_prefixed_namespaces_and_namespaced_attributes_reads_the_same(tmp_path):
    path = tmp_path / "prefixed.xml"
    path.write_text(
        '<c:catalog xmlns:c="urn:example:catalog" xmlns:x="urn:example:extra"><c:item x:rev="7"><c:sku>AB123</c:sku>'
        '<c:title xml:lang="en">Oil filter</c:title></c:item></c:catalog>',
        encoding="utf-8",
    )
    assert records(FileAdapter(path))[0].fields == {"rev": "7", "sku": "AB123", "title": "Oil filter", "title@lang": "en"}


def test_xml_that_puts_the_fields_in_attributes_of_the_record_reads_them(tmp_path):
    path = tmp_path / "attrs.xml"
    path.write_text('<catalog><item sku="AB123" brand="Marlow" cost="5.00"/><item sku="AB124" brand="Marlow" cost="6.00"/></catalog>', encoding="utf-8")
    recs = records(FileAdapter(path))
    assert [r.fields for r in recs] == [{"sku": "AB123", "brand": "Marlow", "cost": "5.00"}, {"sku": "AB124", "brand": "Marlow", "cost": "6.00"}]


EXCEL_TEXT = "PartNo\tMfr\tDescription\tCost\tUPC\r\nAB123\tMarlow\tPompe à eau\t1.00\t\r\nAB124\tMarlow\tÉtrier de frein\t2.00\t\r\n"


@pytest.mark.parametrize(
    "data,encoding",
    [
        (b"\xff\xfe" + EXCEL_TEXT.encode("utf-16-le"), "utf-16"),  # Excel's "Unicode Text"
        (b"\xfe\xff" + EXCEL_TEXT.encode("utf-16-be"), "utf-16"),
        (EXCEL_TEXT.encode("utf-16-le"), "utf-16-le"),  # no byte order mark: found by the NUL in every other byte
        (EXCEL_TEXT.encode("utf-16-be"), "utf-16-be"),
        (b"\xff\xfe\x00\x00" + EXCEL_TEXT.encode("utf-32-le"), "utf-32"),
    ],
    ids=["utf16 le with bom", "utf16 be with bom", "utf16 le no bom", "utf16 be no bom", "utf32 le with bom"],
)
def test_a_utf16_or_utf32_export_is_decoded_not_refused(tmp_path, data, encoding):
    path = tmp_path / "unicode.txt"
    path.write_bytes(data)
    adapter = FileAdapter(path, fmt="csv")
    recs = records(adapter)
    assert [r.fields["Description"] for r in recs] == ["Pompe à eau", "Étrier de frein"] and [r.reject for r in recs] == ["", ""]
    assert adapter.notes()["encoding"] == encoding and adapter.notes()["delimiter"] == "\t"


def test_a_stray_nul_in_an_ordinary_file_is_not_mistaken_for_utf16(tmp_path):
    path = tmp_path / "stray.csv"
    path.write_bytes(",".join(CSV_HEADER).encode() + b"\r\nAB123,Marlow,Oil\x00filter,5.00,\r\nAB124,Marlow,Fine,6.00,\r\n")
    adapter = FileAdapter(path)
    recs = records(adapter)
    assert [r.fields["PartNo"] for r in recs] == ["AB123", "AB124"] and "encoding" not in adapter.notes()


def test_utf8_with_a_bom_and_windows_1252_still_read_as_before(tmp_path):
    bom = tmp_path / "bom.csv"
    bom.write_bytes(b"\xef\xbb\xbf" + (",".join(CSV_HEADER) + "\r\nAB123,Marlow,Pompe à eau,1.00,\r\n").encode("utf-8"))
    latin = tmp_path / "latin.csv"
    latin.write_bytes((",".join(CSV_HEADER) + "\r\nAB123,Marlow,Pompe à eau,1.00,\r\n").encode("cp1252"))
    assert records(FileAdapter(bom))[0].fields["Description"] == records(FileAdapter(latin))[0].fields["Description"] == "Pompe à eau"
