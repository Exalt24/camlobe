"""Every relative link in the README and the markdown files under docs/ points at a file that exists, resolved from the folder of the file that holds the link."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")
SKIP_DIRS = {".git", "node_modules", "test-runs"}


def markdown_files():
    yield ROOT / "README.md"
    for path in sorted((ROOT / "docs").rglob("*.md")):
        if not SKIP_DIRS.intersection(path.parts):
            yield path


def test_every_relative_link_in_the_readme_and_docs_resolves():
    broken = []
    for md in markdown_files():
        if not md.exists():
            continue
        for target in LINK.findall(md.read_text(encoding="utf-8", errors="replace")):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (md.parent / target).resolve().exists():
                broken.append("%s -> %s" % (md.relative_to(ROOT), target))
    assert not broken, "relative links that lead nowhere: " + "; ".join(broken)


def test_the_guard_fails_on_a_link_written_from_the_wrong_folder(tmp_path):
    """The defect this guards: a link inside docs/ that names docs/ again resolves to docs/docs/x and leads nowhere."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "x.txt").write_text("x", encoding="utf-8")
    md = docs / "PROOF.md"
    md.write_text("[run](docs/x.txt) and [ok](x.txt)", encoding="utf-8")
    targets = LINK.findall(md.read_text(encoding="utf-8"))
    missing = [t for t in targets if not (md.parent / t).exists()]
    assert missing == ["docs/x.txt"]
