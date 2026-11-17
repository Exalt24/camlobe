# LIFESPAN: permanent
"""Time the Python normaliser and the Rust port on the same numbers, in process, and print both.

    python tools/bench_normalizer.py 200000

The Rust side is `camlobe-normalizer --bench N` (build it with `cargo build --release` in normalizer-rs/). Both normalise
`MF-0000000000` .. `MF-<N-1>` for the brand MARLOW, which fires R3, R4 and R5 on most of them. This times the function only:
no database, no file read, no process start, which is the whole of what a port of that function can speed up.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from catalog.normalize import normalize_part_number  # noqa: E402

EXE = ROOT / "normalizer-rs" / "target" / "release" / ("camlobe-normalizer.exe" if os.name == "nt" else "camlobe-normalizer")


def main(n: int) -> None:
    started = time.perf_counter()
    kept = 0
    for i in range(n):
        kept += len(normalize_part_number(f"MF-{i:010}", "MARLOW").value)
    python_s = time.perf_counter() - started
    print(f"python {n} numbers in {python_s:.3f} s ({kept} characters kept)")
    done = subprocess.run([str(EXE), "--bench", str(n)], capture_output=True, text=True, check=True, timeout=300)
    print("rust  ", done.stdout.strip())


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 200000)
