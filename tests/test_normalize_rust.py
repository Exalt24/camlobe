"""The Rust port of the part number normaliser must answer exactly as `catalog/normalize.py` does.

`normalizer-rs/` is a port of `normalize_part_number`. The Python function is the reference, and this file is what makes the
port safe to trust: it feeds both the same inputs (generated text over the whole of Unicode, and structured numbers that
carry prefixes, separators, zero padding and look alike letters) and compares the canonical number, the rules that fired in
order, and the error, character for character.

The binary is built with `cargo build --release` in `normalizer-rs/`. Without it these tests skip on a laptop and fail in CI,
where CAMLOBE_REQUIRE_RUST is set, so a missing build can never pass for a green run.
"""
import json
import os
import subprocess
import unicodedata
from pathlib import Path

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from catalog.normalize import BRAND_PREFIXES, normalize_part_number

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "normalizer-rs" / "target" / "release" / ("camlobe-normalizer.exe" if os.name == "nt" else "camlobe-normalizer")

pytestmark = pytest.mark.skipif(not EXE.exists() and not os.environ.get("CAMLOBE_REQUIRE_RUST"), reason="build it: cd normalizer-rs && cargo build --release")

BRANDS = [None, "", *BRAND_PREFIXES, "UNKNOWNCO", "MF", "A1"]


def rust(cases):
    """One process, one line per case in, one line per case out."""
    assert EXE.exists(), "CAMLOBE_REQUIRE_RUST is set and normalizer-rs is not built"
    lines = "\n".join(json.dumps({"raw": raw, "brand": brand}) for raw, brand in cases) + "\n"
    done = subprocess.run([str(EXE)], input=lines, capture_output=True, text=True, encoding="utf-8", check=True, timeout=120)
    return [json.loads(line) for line in done.stdout.splitlines()]


def python(cases):
    out = []
    for raw, brand in cases:
        got = normalize_part_number(raw, brand)
        out.append({"value": got.value, "rules": list(got.rules), "error": got.error})
    return out


def assert_same(cases):
    want, got = python(cases), rust(cases)
    assert len(got) == len(want)
    for case, w, g in zip(cases, want, got):
        assert g == w, f"input {case!r}: python {w} rust {g}"


# Characters that have tripped normalisers elsewhere: sharp s, a titlecase digraph, a ligature, a dotted capital I, a Kelvin
# sign, a roman numeral, a circled digit, superscripts, full width forms, Cyrillic and Greek look alikes, a combining mark.
TRICKY = ["ß", "ǅ", "ﬁ", "İ", "K", "Ⅻ", "①", "²", "ＡＢ１", "А", "Ω", "́", "É", "ı", "Ω", "ẞ", "ͅ", "　"]

TRICKY_CHARS = sorted(set("".join(TRICKY)))

STRUCTURED = st.builds(
    lambda prefix, sep, digits, tail, pad: f"{prefix}{sep}{'0' * pad}{digits}{tail}",
    st.sampled_from(["", "MF", "mf", "KB", "KST", "DU", "OE", "HLD", "TC", "XY"]),
    st.sampled_from(["", "-", " ", ".", "/", "_", "–", " "]),
    st.text(alphabet="0123456789", min_size=0, max_size=14),
    st.text(alphabet=st.sampled_from(list("ABCabc1-. ") + TRICKY_CHARS), max_size=6),
    st.integers(min_value=0, max_value=10),
)
ANYTHING = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=40)
MIXED = st.text(alphabet=st.sampled_from(list("AaZz09-. /_") + TRICKY_CHARS), max_size=40)
CASES = st.lists(st.tuples(st.one_of(STRUCTURED, ANYTHING, MIXED, st.none()), st.sampled_from(BRANDS)), min_size=1, max_size=120)

SETTINGS = hyp_settings(max_examples=120, deadline=None)


@given(CASES)
@SETTINGS
def test_rust_and_python_answer_the_same_on_generated_inputs(cases):
    assert_same(cases)


def test_every_tricky_character_alone_and_inside_a_number():
    cases = []
    for ch in TRICKY:
        for brand in (None, "MARLOW", "KESTREL"):
            cases += [(ch, brand), (f"MF-{ch}1234", brand), (f"{ch}0000451233", brand), (f"AB{ch}", brand), (f"1234{ch}5678", brand)]
    assert_same(cases)


def test_the_documented_examples_agree_with_the_rules_table():
    cases = [("MF-H4123A", "MARLOW"), ("h 4123 a", "MARLOW"), ("0000451233", "MARLOW"), ("0000451233", "KESTREL"), ("MFAB", "MARLOW"), ("A1", None), ("A1" * 17, None), (None, None), ("", "")]
    assert_same(cases)
    assert rust(cases)[0] == {"value": "H4123A", "rules": ["R3", "R4"], "error": ""}
    assert rust(cases)[1] == {"value": "H4123A", "rules": ["R2", "R3"], "error": ""}


def test_a_known_wrong_port_is_caught():
    """The control for this file: a comparison that cannot fail proves nothing. Feeding it a wrong answer must raise."""
    cases = [("MF-H4123A", "MARLOW")]
    wrong = rust(cases)
    wrong[0]["value"] = "H4123"
    assert python(cases) != wrong


def test_zero_padding_at_every_length_around_the_threshold():
    """R5 starts at 8 digits and keeps at least 4: every length from 1 to 14, zeros first or inside, for a brand that strips and one that keeps."""
    cases = []
    for length in range(1, 15):
        for zeros in range(0, length + 1):
            number = "0" * zeros + "7" * (length - zeros)
            for brand in (None, "MARLOW", "KESTREL"):
                cases += [(number, brand), (f"MF{number}", brand), (f"KB-{number}", brand)]
    assert_same(cases)


def test_every_code_point_alone_and_inside_a_number_agrees():
    """The sweep that pins the Unicode versions: one case per code point of the whole range (a surrogate cannot be sent as text).

    Python 3.13 carries Unicode 15.1.0 and the Rust crates are pinned to match (see `is_letter` in normalizer-rs/src/lib.rs).
    Upgrading either side without the other shows up here as the code points they disagree on, which is the point of it.
    """
    points = [cp for cp in range(0x110000) if not 0xD800 <= cp <= 0xDFFF]
    cases = [(chr(cp), None) for cp in points]
    cases += [("MF-" + chr(cp) + "123", "MARLOW") for cp in points if chr(cp).isalpha()]
    want = python(cases)
    got = rust(cases)
    assert len(got) == len(want)
    bad = [(c, w, g) for c, w, g in zip(cases, want, got) if w != g]
    assert not bad, f"{len(bad)} disagreements, the first five: {bad[:5]}"


def test_the_generated_tables_are_from_this_pythons_unicode_version():
    header = (ROOT / "normalizer-rs" / "src" / "tables.rs").read_text(encoding="utf-8").splitlines()[0]
    assert f"Unicode {unicodedata.unidata_version}." in header, f"run `python normalizer-rs/gen_tables.py`: {header}"


def test_every_brand_prefix_and_the_brand_name_itself_is_stripped_the_same_way():
    """R4 for each brand's own prefixes, the brand name, another brand's prefix, and shapes where too little or no digit would remain."""
    others = sorted({p for prefixes in BRAND_PREFIXES.values() for p in prefixes})
    cases = []
    for brand, prefixes in BRAND_PREFIXES.items():
        for prefix in (*prefixes, brand, *others):
            for rest in ("H4123A", "4123", "123", "12", "ABCD", "0000451233", "A1B2C3"):
                for sep in ("", "-", " "):
                    cases.append((f"{prefix}{sep}{rest}", brand))
                    cases.append((f"{prefix}{prefix}{sep}{rest}", brand))  # R4 repeats until nothing changes
    assert_same(cases)
