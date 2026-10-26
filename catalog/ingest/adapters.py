"""Feed adapters: one interface, one implementation per transport or format.

An adapter turns a source into SourceRecord values with a 1-based `position` that never changes for
the same source content, so a checkpoint can say "everything up to position N is stored". It yields a
SourceGap instead of raising when a part of the source cannot be read but later parts still can (one
REST page after its retries), so the runner can keep going and report the run as partial.
"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from defusedxml.ElementTree import iterparse


@dataclass(frozen=True)
class SourceRecord:
    position: int
    fields: dict[str, str]
    raw_text: str = ""
    reject: str = ""  # set by the adapter when the record itself is unusable (undecodable bytes, split row)


@dataclass(frozen=True)
class SourceGap:
    first_position: int
    last_position: int
    message: str
    # True when the rows already read cannot be trusted to line up with the rest of this pass (the source's own total moved
    # while it was walked): the next run then starts over from the first row as a new pass instead of resuming at the checkpoint
    restart: bool = False


class FeedAdapter(Protocol):
    source_ref: str

    def fingerprint(self) -> str:
        """Identity of the source's content, or "" when the source has none (a live API)."""

    def records(self, start_after: int = 0) -> Iterator[SourceRecord | SourceGap]:
        """Records with position > start_after, in order."""

    def notes(self) -> dict:
        """What the adapter learned while reading (encoding, delimiter, retries). Read after iterating."""

    # Optional: `secret_values() -> list[str]` names every secret the adapter was configured with (a password, a token,
    # proxy credentials, query values of its URLs). The runner removes them from every message it stores or logs
    # (catalog/ingest/redact.py). `source_ref` is stored as it is, so it must never hold one.


def decode_line(raw: bytes) -> tuple[str, bool]:
    """One physical line to text. UTF-8 first, windows-1252 when that fails; (text, used_fallback).

    A file that mixes both (a supplier's export tool pasting latin text into a UTF-8 file) is decoded
    line by line, so one bad line does not turn every accented letter in the file into mojibake. A byte
    cp1252 does not define becomes U+FFFD, and the record holding it is rejected, not repaired.
    """
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), True


def wide_encoding(data: bytes) -> str | None:
    """The codec of a UTF-16 or UTF-32 file, or None for the usual byte oriented text.

    Excel's "Unicode Text" export is UTF-16 with a byte order mark. A file without one is recognised by its NULs:
    in text that is mostly ASCII every second byte (UTF-16) is zero, and which of the two says the byte order.
    One stray NUL in an ordinary file is nowhere near that, so it is not mistaken for UTF-16.
    """
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")) and len(data) % 4 == 0:
        return "utf-32"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    sample = data[:2048]
    pairs = len(sample) // 2
    if pairs < 4:
        return None
    even, odd = sample[0 : pairs * 2 : 2].count(0), sample[1 : pairs * 2 : 2].count(0)
    if odd >= 0.8 * pairs and even <= 0.1 * pairs:
        return "utf-16-le"
    if even >= 0.8 * pairs and odd <= 0.1 * pairs:
        return "utf-16-be"
    return None


def local_name(name: str) -> str:
    """`{urn:example}sku` is `sku`: the part after the namespace in ElementTree's notation."""
    return name.rsplit("}", 1)[-1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


_DELIMITERS = ",;\t|"


def detect_delimiter(header_line: str) -> str:
    counts = {d: header_line.count(d) for d in _DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


class FileAdapter:
    """A CSV or XML file on disk. Also the parser behind the SFTP adapter once the file is downloaded."""

    def __init__(self, path: str | Path, fmt: str | None = None, source_ref: str | None = None):
        self.path = Path(path)
        self.fmt = fmt or ("xml" if self.path.suffix.lower() == ".xml" else "csv")
        if self.fmt not in ("csv", "xml"):
            raise ValueError(f"unknown feed format {self.fmt!r}")
        self.source_ref = source_ref or self.path.name
        self._notes: dict = {}

    def fingerprint(self) -> str:
        return sha256_file(self.path)

    def notes(self) -> dict:
        return dict(self._notes)

    def records(self, start_after: int = 0) -> Iterator[SourceRecord | SourceGap]:
        if self.fmt == "xml":
            yield from self._xml(start_after)
        else:
            yield from self._csv(start_after)

    def _csv(self, start_after: int) -> Iterator[SourceRecord | SourceGap]:
        data = self.path.read_bytes()
        lines: list[str] = []
        fallback = 0
        wide = wide_encoding(data)
        if wide:
            # a wide export is decoded whole: its line breaks are two or four bytes, so bytes cannot be split on them
            self._notes["encoding"] = wide
            lines = data.decode(wide, errors="replace").lstrip("﻿").splitlines(keepends=True)
        else:
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
                self._notes["bom"] = True
            for raw in data.splitlines(keepends=True):
                text, used_fallback = decode_line(raw)
                fallback += used_fallback
                lines.append(text)
        self._notes["fallback_lines"] = fallback
        if not lines:
            return
        delimiter = detect_delimiter(lines[0])
        self._notes["delimiter"] = delimiter
        reader = csv.reader(io.StringIO("".join(lines), newline=""), delimiter=delimiter)
        header = next(reader, None)
        if header is None:
            return
        header = [h.strip() for h in header]
        position = 0
        for cells in reader:
            if not cells or all(c == "" for c in cells):
                continue
            position += 1
            if position <= start_after:
                continue
            raw_text = delimiter.join(cells)[:2000]
            if len(cells) != len(header):
                yield SourceRecord(position, {}, raw_text, reject="column_count")
            elif any("�" in c for c in cells):
                yield SourceRecord(position, {}, raw_text, reject="undecodable_bytes")
            else:
                yield SourceRecord(position, dict(zip(header, cells)), raw_text)

    def _xml(self, start_after: int) -> Iterator[SourceRecord | SourceGap]:
        depth = 0
        position = 0
        with self.path.open("rb") as handle:
            for event, element in iterparse(handle, events=("start", "end"), forbid_dtd=True):
                if event == "start":
                    depth += 1
                    continue
                depth -= 1
                if depth != 1:  # only direct children of the root are records
                    continue
                position += 1
                if position > start_after:
                    # the record's own attributes, then its children; a namespace is markup, not part of a field name
                    fields: dict[str, str] = {local_name(a): v for a, v in element.attrib.items()}
                    for child in element:
                        name = local_name(child.tag)
                        fields[name] = (child.text or "").strip()
                        for attr, value in child.attrib.items():
                            fields[f"{name}@{local_name(attr)}"] = value
                    yield SourceRecord(position, fields, local_name(element.tag or "")[:200])
                element.clear()
