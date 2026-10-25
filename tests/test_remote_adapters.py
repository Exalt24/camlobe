"""REST, SOAP and SFTP adapters against local fake servers, all the way through the ingest runner."""
import socket

import paramiko
import pytest

from catalog.ingest.adapters import FileAdapter
from catalog.ingest.rest import RestAdapter
from catalog.ingest.runner import ingest
from catalog.ingest.sftp import HostKeyMismatch, SftpAdapter, SftpError
from catalog.ingest.soap import SoapAdapter
from catalog.models import FeedCheckpoint, PipelineRun, RawPart

from .fakes import Behaviour, SftpServer, rest_server, soap_server
from .helpers import get_supplier, row, write_csv

pytestmark = pytest.mark.django_db(transaction=True)


def catalog_items(n):
    return [
        {"sku": f"MF-H{i:04d}A", "manufacturer": "Marlow", "title": "Oil filter", "price": f"{i % 90 + 1}.00", "gtin": ""}
        for i in range(n)
    ]


class NoSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


@pytest.fixture
def rest():
    servers = []

    def make(behaviour):
        server = rest_server(behaviour)
        servers.append(server)
        return server, f"http://127.0.0.1:{server.port}/catalog"

    yield make
    for s in servers:
        s.close()


@pytest.fixture
def soap():
    servers = []

    def make(behaviour):
        server = soap_server(behaviour)
        servers.append(server)
        return server, f"http://127.0.0.1:{server.port}/catalog?wsdl"

    yield make
    for s in servers:
        s.close()


# ---- REST ------------------------------------------------------------------------------------------


def test_rest_reads_every_page_through_the_runner(rest):
    server, url = rest(Behaviour(catalog_items(250)))
    run = ingest(get_supplier(), RestAdapter(url, page_size=100), batch_size=60)
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 250
    assert [r for r, _ in server.behaviour.requests] == [0, 100, 200]
    assert RawPart.objects.filter(part_number_raw="MF-H0007A").get().cost_cents == 800


def test_rest_a_second_run_reads_again_but_stores_nothing_new(rest):
    _, url = rest(Behaviour(catalog_items(120)))
    s = get_supplier()
    ingest(s, RestAdapter(url, page_size=50))
    again = ingest(s, RestAdapter(url, page_size=50))
    assert (again.rows_read, again.rows_written, again.rows_unchanged) == (120, 0, 120)


def test_rest_a_503_twice_is_retried_and_counted(rest):
    sleep = NoSleep()
    server, url = rest(Behaviour(catalog_items(150), script={100: [503, 503]}))
    run = ingest(get_supplier(), RestAdapter(url, page_size=100, sleep=sleep, backoff_base=0.5, jitter=lambda d: d))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 150
    assert run.notes["retries"] == 2 and sleep.calls == [0.5, 1.0]
    assert server.behaviour.requests.count((100, 100)) == 3


def test_rest_429_with_retry_after_waits_as_told(rest):
    sleep = NoSleep()
    _, url = rest(Behaviour(catalog_items(50), script={0: [429]}, retry_after=3))
    ingest(get_supplier(), RestAdapter(url, page_size=100, sleep=sleep, backoff_base=0.5))
    assert sleep.calls == [3.0]


def test_rest_a_page_that_keeps_failing_gives_a_partial_run_and_the_rerun_adds_only_that_page(rest):
    sleep = NoSleep()
    behaviour = Behaviour(catalog_items(300), always={100: 500})
    _, url = rest(behaviour)
    s = get_supplier()
    first = ingest(s, RestAdapter(url, page_size=100, sleep=sleep, max_retries=2), batch_size=50)
    assert first.status == PipelineRun.Status.PARTIAL
    assert first.rows_written == 200 and first.errors[0]["where"] == "positions 101-200"
    cp = FeedCheckpoint.objects.get()
    assert cp.position == 100 and not cp.completed
    behaviour.always.clear()  # the supplier fixed page 2
    second = ingest(s, RestAdapter(url, page_size=100, sleep=sleep), batch_size=50)
    assert second.status == PipelineRun.Status.SUCCEEDED
    assert second.notes["resumed_from"] == 100 and second.rows_written == 100 and second.rows_unchanged == 100
    assert RawPart.objects.count() == 300
    assert FeedCheckpoint.objects.get().completed


def test_rest_a_wrong_token_stops_the_run_and_stores_nothing(rest):
    server, url = rest(Behaviour(catalog_items(50), token="s3cret-token"))
    run = ingest(get_supplier(), RestAdapter(url, token="not-it"))
    assert run.status == PipelineRun.Status.FAILED and "refused our credentials" in run.errors[0]["message"]
    assert RawPart.objects.count() == 0 and len(server.behaviour.requests) == 1  # no retry storm on a 401


def test_rest_the_token_is_sent_and_never_stored(rest):
    _, url = rest(Behaviour(catalog_items(20), token="s3cret-token"))
    run = ingest(get_supplier(), RestAdapter(url, token="s3cret-token"))
    assert run.status == PipelineRun.Status.SUCCEEDED
    stored = repr([run.source_ref, run.notes, run.errors, list(RawPart.objects.values_list("payload", flat=True))])
    assert "s3cret-token" not in stored


@pytest.mark.parametrize("url", ["http://user:pw@127.0.0.1:1/catalog", "http://127.0.0.1:1/catalog?api_key=abc"])
def test_rest_refuses_credentials_inside_the_url(url):
    with pytest.raises(ValueError, match="token"):
        RestAdapter(url)


def test_rest_a_server_that_caps_the_page_size_stops_the_run(rest):
    _, url = rest(Behaviour(catalog_items(300), cap=50))
    run = ingest(get_supplier(), RestAdapter(url, page_size=100))
    assert run.status == PipelineRun.Status.FAILED and "caps the page size" in run.errors[0]["message"]
    assert RawPart.objects.count() == 0 and FeedCheckpoint.objects.get().position == 0  # nothing from the bad page


def test_rest_works_when_the_server_gives_no_total(rest):
    _, url = rest(Behaviour(catalog_items(230), hide_total=True))
    assert ingest(get_supplier(), RestAdapter(url, page_size=100)).rows_written == 230


def test_rest_a_dead_endpoint_fails_the_run_after_three_pages():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here now
    run = ingest(
        get_supplier(), RestAdapter(f"http://127.0.0.1:{port}/catalog", page_size=10, sleep=NoSleep(), max_retries=1)
    )
    assert run.status == PipelineRun.Status.FAILED and "looks down" in run.errors[-1]["message"]


def test_rest_rows_with_a_bad_cost_are_rejected_not_stored(rest):
    items = catalog_items(5)
    items[2]["price"] = "call us"
    _, url = rest(Behaviour(items))
    run = ingest(get_supplier(), RestAdapter(url))
    assert (run.rows_written, run.rows_rejected) == (4, 1)


# ---- SOAP ------------------------------------------------------------------------------------------


def test_soap_reads_every_page(soap):
    server, wsdl = soap(Behaviour(catalog_items(130)))
    run = ingest(get_supplier(), SoapAdapter(wsdl, page_size=50))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 130
    assert [r for r, _ in server.behaviour.requests] == [0, 50, 100]
    raw = RawPart.objects.get(part_number_raw="MF-H0007A")
    assert (raw.cost_cents, raw.brand_raw) == (800, "Marlow")


def test_soap_a_transient_fault_is_retried(soap):
    sleep = NoSleep()
    server, wsdl = soap(Behaviour(catalog_items(60), script={50: [-1, -1]}))
    run = ingest(get_supplier(), SoapAdapter(wsdl, page_size=50, sleep=sleep, backoff_base=0.25, jitter=lambda d: d))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 60
    assert sleep.calls == [0.25, 0.5]


def test_soap_a_page_that_keeps_returning_500_is_a_gap(soap):
    _, wsdl = soap(Behaviour(catalog_items(150), always={50: 500}))
    run = ingest(get_supplier(), SoapAdapter(wsdl, page_size=50, sleep=NoSleep(), max_retries=1))
    assert run.status == PipelineRun.Status.PARTIAL and run.rows_written == 100
    assert FeedCheckpoint.objects.get().position == 50


def test_soap_a_401_stops_the_run_without_retrying(soap):
    server, wsdl = soap(Behaviour(catalog_items(60), always={0: 401}))
    run = ingest(get_supplier(), SoapAdapter(wsdl, page_size=50, sleep=NoSleep()))
    assert run.status == PipelineRun.Status.FAILED and "refused our credentials" in run.errors[0]["message"]
    assert len(server.behaviour.requests) == 1 and RawPart.objects.count() == 0


def test_soap_refuses_credentials_in_the_wsdl_url():
    with pytest.raises(ValueError, match="separately"):
        SoapAdapter("http://user:pw@127.0.0.1:1/catalog?wsdl")


# ---- SFTP ------------------------------------------------------------------------------------------


@pytest.fixture
def sftp(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    servers = []

    def make(**kw):
        server = SftpServer(root, **kw)
        servers.append(server)
        return server

    make.root = root
    yield make
    for s in servers:
        s.close()


def adapter_for(server, path, **kw):
    kw.setdefault("host_key_sha256", server.fingerprint())
    kw.setdefault("password", server.password)
    kw.setdefault("sleep", NoSleep())
    return SftpAdapter("127.0.0.1", path, server.user, port=server.port, **kw)


def test_sftp_csv_and_xml_through_the_runner(sftp, tmp_path):
    write_csv(sftp.root / "feed.csv", [row(f"AB{i:03d}") for i in range(40)])
    (sftp.root / "feed.xml").write_text(
        '<?xml version="1.0"?><catalog><item><sku>MF-H1A</sku><manufacturer>Marlow</manufacturer><price>5.00</price></item></catalog>',
        encoding="utf-8",
    )
    server = sftp()
    s = get_supplier()
    a = adapter_for(server, "/feed.csv")
    csv_run = ingest(s, a, batch_size=15)
    a.cleanup()
    xml_adapter = adapter_for(server, "/feed.xml")
    xml_run = ingest(get_supplier("s2"), xml_adapter)
    xml_adapter.cleanup()
    assert csv_run.status == xml_run.status == PipelineRun.Status.SUCCEEDED
    assert (csv_run.rows_written, xml_run.rows_written) == (40, 1)
    assert csv_run.notes["host_key_verified"] and csv_run.notes["remote_bytes"] > 100
    assert csv_run.source_ref == f"sftp://127.0.0.1:{server.port}/feed.csv"


def test_sftp_a_second_run_over_an_unchanged_file_is_skipped(sftp):
    write_csv(sftp.root / "feed.csv", [row(f"AB{i:03d}") for i in range(10)])
    server = sftp()
    s = get_supplier()
    first = ingest(s, adapter_for(server, "/feed.csv"))
    second = ingest(s, adapter_for(server, "/feed.csv"))
    assert first.rows_written == 10 and second.rows_written == 0 and "unchanged" in second.notes["skipped"]


def test_sftp_a_host_key_that_is_not_the_expected_one_is_refused(sftp):
    write_csv(sftp.root / "feed.csv", [row("AB001")])
    server = sftp()
    other = paramiko.RSAKey.generate(2048)
    from catalog.ingest.sftp import key_fingerprint

    bad = adapter_for(server, "/feed.csv", host_key_sha256=key_fingerprint(other))
    with pytest.raises(HostKeyMismatch):
        bad.fetch()
    run = ingest(get_supplier(), adapter_for(server, "/feed.csv", host_key_sha256=key_fingerprint(other)))
    assert run.status == PipelineRun.Status.FAILED and "not the expected" in run.errors[0]["message"]
    assert RawPart.objects.count() == 0 and server.connections == 2  # no retry on a key mismatch


def test_sftp_a_wrong_password_fails_without_retrying(sftp):
    write_csv(sftp.root / "feed.csv", [row("AB001")])
    server = sftp()
    run = ingest(get_supplier(), adapter_for(server, "/feed.csv", password="wrong"))
    assert run.status == PipelineRun.Status.FAILED and "Authentication" in run.errors[0]["message"]
    assert server.connections == 1


def test_sftp_a_missing_file_is_a_clear_error_and_not_retried(sftp):
    server = sftp()
    run = ingest(get_supplier(), adapter_for(server, "/nope.csv"))
    assert run.status == PipelineRun.Status.FAILED and "no such file" in run.errors[0]["message"]
    assert server.connections == 1


def test_sftp_a_truncated_transfer_is_caught_before_anything_is_parsed(sftp):
    write_csv(sftp.root / "feed.csv", [row(f"AB{i:03d}") for i in range(200)])
    server = sftp(truncate_at=300)
    run = ingest(get_supplier(), adapter_for(server, "/feed.csv", attempts=2))
    assert run.status == PipelineRun.Status.FAILED and "transfer incomplete" in run.errors[0]["message"]
    assert RawPart.objects.count() == 0 and server.connections == 2  # it did retry a transfer problem


def test_sftp_the_password_never_reaches_the_stored_run(sftp):
    write_csv(sftp.root / "feed.csv", [row("AB001")])
    server = sftp()
    run = ingest(get_supplier(), adapter_for(server, "/feed.csv"))
    stored = repr([run.source_ref, run.notes, run.errors, list(RawPart.objects.values_list("payload", flat=True))])
    assert server.password not in stored and server.user not in stored


def test_sftp_connection_refused_is_retried_with_backoff_then_reported():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    sleep = NoSleep()
    a = SftpAdapter(
        "127.0.0.1", "/feed.csv", "u", host_key_sha256="SHA256:abc", password="p", port=port,
        attempts=3, retry_delay=1.0, sleep=sleep,
    )
    with pytest.raises(SftpError, match="after 3 attempts"):
        a.fetch()
    assert sleep.calls == [1.0, 2.0]


def test_sftp_arguments_are_checked_up_front():
    with pytest.raises(ValueError, match="SHA256"):
        SftpAdapter("h", "/f.csv", "u", host_key_sha256="abc", password="p")
    with pytest.raises(ValueError, match="password or a key"):
        SftpAdapter("h", "/f.csv", "u", host_key_sha256="SHA256:abc")


def test_a_downloaded_file_reads_exactly_like_the_same_local_file(sftp, tmp_path):
    local = write_csv(tmp_path / "feed.csv", [row(f"AB{i:03d}", cost=f"{i}.50") for i in range(30)])
    write_csv(sftp.root / "feed.csv", [row(f"AB{i:03d}", cost=f"{i}.50") for i in range(30)])
    server = sftp()
    remote = adapter_for(server, "/feed.csv")
    assert remote.fingerprint() == FileAdapter(local).fingerprint()
    assert [r.fields for r in remote.records()] == [r.fields for r in FileAdapter(local).records()]
    remote.cleanup()
