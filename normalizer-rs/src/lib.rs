//! `normalize_part_number`, ported from `catalog/normalize.py`.
//!
//! The Python function stays the reference. The port is kept honest by `tests/test_normalize_rust.py`, which feeds
//! both the same generated inputs and compares every answer: the canonical number, the rules that fired in order,
//! and the error. A rule here is the same rule there (R1 to R7, see docs/normalization.md).

mod tables;

use tables::{LETTERS, UPPER};
use unicode_normalization::char::canonical_combining_class;
use unicode_normalization::UnicodeNormalization;

/// The Unicode version of the Python these tables were generated from.
pub use tables::UNICODE_VERSION;

pub const MIN_LEN: usize = 3;
pub const MAX_LEN: usize = 32;
pub const ZEROPAD_MIN_DIGITS: usize = 8;
pub const ZEROPAD_KEEP_DIGITS: usize = 4;

/// Brand -> the prefixes a supplier glues on the front of a number (the table in `normalize.py`).
const BRAND_PREFIXES: &[(&str, &[&str])] = &[
    ("KESTREL", &["KB", "KST"]),
    ("MARLOW", &["MF", "MRL"]),
    ("DUNMORE", &["DU", "DNM"]),
    ("TARN", &["TC", "TRN"]),
    ("HALDEN", &["HS", "HLD"]),
    ("ORSETT", &["OE", "ORS"]),
];

/// Brands whose catalog numbers start with a zero that is part of the number.
const ZERO_SIGNIFICANT: &[&str] = &["KESTREL"];

#[derive(Debug, PartialEq, Eq, Clone)]
pub struct NormalizedNumber {
    pub value: String,
    pub rules: Vec<&'static str>,
    pub error: &'static str,
}

/// Python's `str.isalpha`, from the table `gen_tables.py` wrote out of the reference interpreter. Rust's own letter and case
/// tables are on a newer Unicode version than Python 3.13 (15.1.0) and the sweep in `tests/test_normalize_rust.py` found
/// thousands of code points where they disagree, so neither is used here.
fn is_letter(c: char) -> bool {
    let cp = c as u32;
    LETTERS
        .binary_search_by(|&(lo, hi)| {
            if cp < lo {
                std::cmp::Ordering::Greater
            } else if cp > hi {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// Python's `str.upper()` for one character: the full mapping, which can be longer than one character (the sharp s is SS).
fn push_upper(c: char, out: &mut String) {
    match UPPER.binary_search_by_key(&(c as u32), |&(cp, _)| cp) {
        Ok(i) => out.push_str(UPPER[i].1),
        Err(_) => out.push(c),
    }
}

/// R1 fold: NFKC, then NFD with every combining mark dropped (`unicodedata.combining(c) != 0`).
fn fold(text: &str) -> String {
    text.nfkc().nfd().filter(|c| canonical_combining_class(*c) == 0).collect()
}

fn prefixes_for(brand: &str) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    if !brand.is_empty() {
        names.push(brand.to_string());
    }
    if let Some((_, prefixes)) = BRAND_PREFIXES.iter().find(|(b, _)| *b == brand) {
        for p in prefixes.iter() {
            if !names.iter().any(|n| n == p) {
                names.push((*p).to_string());
            }
        }
    }
    names.sort_by(|a, b| b.chars().count().cmp(&a.chars().count()));
    names
}

fn strip_prefix(value: &str, brand: &str) -> Option<String> {
    for prefix in prefixes_for(brand) {
        if let Some(rest) = value.strip_prefix(prefix.as_str()) {
            if rest.chars().count() >= MIN_LEN && rest.chars().any(|c| c.is_ascii_digit()) {
                return Some(rest.to_string());
            }
        }
    }
    None
}

fn strip_zeros(value: &str, brand: &str) -> Option<String> {
    if ZERO_SIGNIFICANT.contains(&brand) || value.is_empty() || !value.chars().all(|c| c.is_ascii_digit()) || value.len() < ZEROPAD_MIN_DIGITS {
        return None;
    }
    let stripped = value.trim_start_matches('0');
    if stripped == value {
        return None;
    }
    let stripped = if stripped.len() < ZEROPAD_KEEP_DIGITS { &value[value.len() - ZEROPAD_KEEP_DIGITS..] } else { stripped };
    if stripped != value {
        Some(stripped.to_string())
    } else {
        None
    }
}

pub fn normalize_part_number(raw: Option<&str>, brand: Option<&str>) -> NormalizedNumber {
    let raw = raw.unwrap_or("");
    let brand = brand.unwrap_or("");
    let mut rules: Vec<&'static str> = Vec::new();
    let folded = fold(raw);
    if folded != raw {
        rules.push("R1");
    }
    let mut upper = String::with_capacity(folded.len());
    for c in folded.chars() {
        push_upper(c, &mut upper);
    }
    if upper != folded {
        rules.push("R2");
    }
    // R6, before the separators go: a non ASCII letter is not something to guess a mapping for.
    if upper.chars().any(|c| is_letter(c) && !c.is_ascii_uppercase()) {
        rules.push("R6");
        return NormalizedNumber { value: String::new(), rules, error: "non_ascii_letters" };
    }
    let stripped: String = upper.chars().filter(|c| c.is_ascii_uppercase() || c.is_ascii_digit()).collect();
    if stripped != upper {
        rules.push("R3");
    }
    let mut value = stripped;
    loop {
        // every pass removes characters, so it ends; a canonical number is a fixed point
        let mut changed = false;
        if let Some(rest) = strip_prefix(&value, brand) {
            value = rest;
            changed = true;
            rules.push("R4");
        }
        if let Some(rest) = strip_zeros(&value, brand) {
            value = rest;
            changed = true;
            rules.push("R5");
        }
        if !changed {
            break;
        }
    }
    if value.len() < MIN_LEN {
        rules.push("R7");
        return NormalizedNumber { value: String::new(), rules, error: "too_short" };
    }
    if value.len() > MAX_LEN {
        rules.push("R7");
        return NormalizedNumber { value: String::new(), rules, error: "too_long" };
    }
    let mut seen: Vec<&'static str> = Vec::new();
    for r in rules {
        if !seen.contains(&r) {
            seen.push(r);
        }
    }
    NormalizedNumber { value, rules: seen, error: "" }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn n(raw: &str, brand: &str) -> NormalizedNumber {
        normalize_part_number(Some(raw), Some(brand))
    }

    #[test]
    fn a_prefix_and_separators_are_removed_and_the_rules_are_named() {
        let got = n("MF-H4123A", "MARLOW");
        assert_eq!((got.value.as_str(), got.rules.as_slice(), got.error), ("H4123A", ["R3", "R4"].as_slice(), ""));
    }

    #[test]
    fn lower_case_and_spaces_fire_r2_and_r3() {
        let got = n("h 4123 a", "MARLOW");
        assert_eq!((got.value.as_str(), got.rules.as_slice()), ("H4123A", ["R2", "R3"].as_slice()));
    }

    #[test]
    fn leading_zeros_of_a_fixed_width_number_go_unless_the_brand_keeps_them() {
        assert_eq!(n("0000451233", "MARLOW").value, "451233");
        assert_eq!(n("0000451233", "KESTREL").value, "0000451233");
    }

    #[test]
    fn a_prefix_is_kept_when_too_little_would_remain() {
        assert_eq!(n("MFAB", "MARLOW").value, "MFAB");
    }

    #[test]
    fn a_look_alike_letter_rejects_the_number() {
        let got = n("\u{0410}B123", "");
        assert_eq!((got.value.as_str(), got.error), ("", "non_ascii_letters"));
        assert_eq!(got.rules.last(), Some(&"R6"));
    }

    #[test]
    fn full_width_digits_and_accents_fold_to_ascii() {
        let got = n("\u{FF21}\u{FF22}\u{FF11}\u{FF12}\u{FF13}", "");
        assert_eq!(got.value, "AB123");
        assert_eq!(n("\u{00C9}12", "").value, "E12");
    }

    #[test]
    fn length_bounds() {
        assert_eq!(n("A1", "").error, "too_short");
        assert_eq!(n(&"A1".repeat(17), "").error, "too_long");
    }

    #[test]
    fn a_canonical_number_is_a_fixed_point() {
        let once = n("kb-0000451233", "KESTREL");
        let twice = n(&once.value, "KESTREL");
        assert_eq!(once.value, twice.value);
    }
}
