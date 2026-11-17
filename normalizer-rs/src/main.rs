//! Reads JSON lines `{"raw": ..., "brand": ...}` on stdin and writes one JSON line per input:
//! `{"value": ..., "rules": [...], "error": ...}`. `tests/test_normalize_rust.py` drives it.
//! `--bench N` normalises N generated numbers and prints the time, for docs/normalization.md.

use camlobe_normalizer::normalize_part_number;
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() == 3 && args[1] == "--bench" {
        let n: usize = args[2].parse().expect("--bench takes a count");
        let started = std::time::Instant::now();
        let mut kept = 0usize;
        for i in 0..n {
            let raw = format!("MF-{:010}", i);
            kept += normalize_part_number(Some(&raw), Some("MARLOW")).value.len();
        }
        println!("{} numbers in {:?} ({} characters kept)", n, started.elapsed(), kept);
        return;
    }
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut out = io::BufWriter::new(stdout.lock());
    for line in stdin.lock().lines() {
        let line = line.expect("stdin is utf-8");
        if line.trim().is_empty() {
            continue;
        }
        let v: Value = serde_json::from_str(&line).expect("one JSON object per line");
        let raw = v.get("raw").and_then(Value::as_str);
        let brand = v.get("brand").and_then(Value::as_str);
        let got = normalize_part_number(raw, brand);
        writeln!(out, "{}", json!({"value": got.value, "rules": got.rules, "error": got.error})).unwrap();
    }
}
