"""The management commands: arguments, exit behaviour and output, and that secrets only come from the environment."""
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from catalog.models import Part, PartSource, PipelineRun, RawPart, ReviewItem

from .fakes import Behaviour, SftpServer, rest_server
from .helpers import row, write_csv

pytestmark = pytest.mark.django_db(transaction=True)


def run_cmd(*args):
    call_command(*args)


def test_the_whole_chain_through_the_commands(tmp_path, capsys):
    path = write_csv(tmp_path / "f.csv", [row("MF-H1001A"), row("H1001A", "Marlow Filters"), row("H1002A", "")])
    call_command("ingest_feed", "s1", "--name", "Supplier One", "--file", str(path))
    call_command("normalize_parts")
    call_command("match_parts")
    out = capsys.readouterr().out
    assert "ingest run" in out and "normalize run" in out and "match run" in out and "queued_for_review=1" in out
    assert Part.objects.count() == 1 and PartSource.objects.count() == 1 and ReviewItem.objects.count() == 1


def test_a_second_ingest_says_so(tmp_path, capsys):
    path = write_csv(tmp_path / "f.csv", [row("H1001A")])
    call_command("ingest_feed", "s1", "--file", str(path))
    call_command("ingest_feed", "s1", "--file", str(path))
    assert "source unchanged since the last complete run" in capsys.readouterr().out


def test_exactly_one_source_is_required(tmp_path):
    with pytest.raises(CommandError, match="exactly one source"):
        call_command("ingest_feed", "s1")
    path = write_csv(tmp_path / "f.csv", [row("H1001A")])
    with pytest.raises(CommandError, match="exactly one source"):
        call_command("ingest_feed", "s1", "--file", str(path), "--rest-url", "http://127.0.0.1:1/x")
    with pytest.raises(CommandError, match="no such file"):
        call_command("ingest_feed", "s1", "--file", str(tmp_path / "missing.csv"))


def test_a_failed_run_is_a_failed_command(tmp_path):
    path = write_csv(tmp_path / "f.csv", [row("H1001A")], header=["PartNo", "Producer", "Description", "Cost", "UPC"])
    with pytest.raises(CommandError, match="ended failed"):
        call_command("ingest_feed", "s1", "--file", str(path))
    assert PipelineRun.objects.get().status == "failed"


def test_rest_through_the_command_takes_its_token_from_the_environment(monkeypatch):
    items = [{"sku": f"H{i:04d}A", "manufacturer": "Marlow", "price": "5.00"} for i in range(12)]
    server = rest_server(Behaviour(items, token="tok-123"))
    try:
        url = f"http://127.0.0.1:{server.port}/catalog"
        with pytest.raises(CommandError, match="not set"):
            call_command("ingest_feed", "api", "--rest-url", url, "--token-env", "CAMLOBE_TEST_TOKEN")
        monkeypatch.setenv("CAMLOBE_TEST_TOKEN", "tok-123")
        call_command("ingest_feed", "api", "--rest-url", url, "--token-env", "CAMLOBE_TEST_TOKEN", "--page-size", "5")
        assert RawPart.objects.count() == 12
        with pytest.raises(CommandError, match="token"):
            call_command("ingest_feed", "api2", "--rest-url", url + "?key=abc")
    finally:
        server.close()


def test_sftp_through_the_command(tmp_path, monkeypatch):
    root = tmp_path / "remote"
    root.mkdir()
    write_csv(root / "feed.csv", [row(f"H{i:04d}A") for i in range(8)])
    server = SftpServer(root)
    try:
        args = [
            "ingest_feed", "sftp1", "--sftp-host", "127.0.0.1", "--sftp-port", str(server.port),
            "--sftp-path", "/feed.csv", "--sftp-user", server.user, "--sftp-password-env", "CAMLOBE_TEST_SFTP",
        ]
        monkeypatch.setenv("CAMLOBE_TEST_SFTP", server.password)
        with pytest.raises(CommandError, match="--sftp-host-key"):
            call_command(*args)  # no host key given: refused before connecting
        call_command(*args, "--sftp-host-key", server.fingerprint())
        assert RawPart.objects.count() == 8
        with pytest.raises(CommandError, match="ended failed"):
            call_command("ingest_feed", "sftp2", *args[2:], "--sftp-host-key", "SHA256:" + "A" * 43)
    finally:
        server.close()


def test_review_commands(tmp_path, capsys):
    path = write_csv(tmp_path / "f.csv", [row("H1001A"), row("H1001A", "")])
    call_command("ingest_feed", "s1", "--file", str(path))
    call_command("normalize_parts")
    call_command("match_parts")
    call_command("resolve_review", "--list")
    assert "no_brand" in capsys.readouterr().out
    item = ReviewItem.objects.get()
    call_command("resolve_review", "--item", str(item.pk), "--part", str(Part.objects.get().pk))
    assert "is now linked" in capsys.readouterr().out
    with pytest.raises(CommandError, match="already"):
        call_command("resolve_review", "--item", str(item.pk), "--ignore")
    with pytest.raises(CommandError):
        call_command("resolve_review", "--item", "99999", "--ignore")


def test_the_generator_command_writes_feeds_and_a_truth_file(tmp_path, capsys):
    call_command("gen_demo_feeds", "--out", str(tmp_path / "d"), "--size", "300", "--seed", "3")
    names = {p.name for p in (tmp_path / "d").iterdir()}
    assert {"redline_catalog.csv", "harbor_catalog.xml", "delta_2026-10-01.csv", "truth.csv", "manifest.json"} <= names
    assert "redline_catalog.csv:" in capsys.readouterr().out


def test_the_generator_is_deterministic_for_a_seed(tmp_path):
    call_command("gen_demo_feeds", "--out", str(tmp_path / "a"), "--size", "200", "--seed", "5")
    call_command("gen_demo_feeds", "--out", str(tmp_path / "b"), "--size", "200", "--seed", "5")
    for name in ("redline_catalog.csv", "harbor_catalog.xml", "truth.csv"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
