"""A secret an adapter was configured with is stored nowhere: not in a run's errors or notes, not in its source_ref,
not in the checkpoint, not in the log and not in what /api/feed-runs/ answers without a login.

The leak this class started from: a proxy URL such as http://user:password@127.0.0.1:99999 makes `requests` raise
InvalidURL carrying the WHOLE URL, and the runner stores an exception's text. The tests plant a distinctive secret in
every configurable place of every adapter, make the adapter raise an exception that repeats it, and search everything
the database holds. The class is "any exception text", so the planting does not depend on what one library happens to say.
"""
import json
import logging
import random
import string

import pytest
from django.test import Client

from catalog.ingest.rest import RestAdapter
from catalog.ingest.runner import ingest
from catalog.ingest.scrape import ScrapeAdapter, ScrapeConfig, ScrapeConfigError
from catalog.ingest.sftp import SftpAdapter
from catalog.ingest.soap import SoapAdapter
from catalog.models import FeedCheckpoint, PipelineRun, RawPart, RejectedRow

from .fakes import Behaviour, ScrapeSite, shop_products
from .helpers import get_supplier, row, write_csv
from .test_remote_adapters import NoSleep, catalog_items, rest, sftp, soap  # noqa: F401  (fixtures)

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "Pw-Planted-7f3a91"
PROXY_USER, PROXY_PASSWORD = "pxuser-Planted-4c2e", "Px-Planted-9d1b07"
API_KEY = "Qk-Planted-5e8c33"
TOKEN = "Tok-Planted-2b6d58"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def site():
    made = ScrapeSite(shop_products(4), password=PASSWORD)
    yield made
    made.close()


def scrape(site, environ=None, **changes):
    clock = Clock()
    base = dict(
        start_url=f"{site.url}/catalog?page=1", login_url=f"{site.url}/login", username_env="SHOP_USER", password_env="SHOP_PASS",
        session_cookie="sid", delay_seconds=0.0, user_agent="camlobe-test/1.0 (a test)",
    )
    base.update(changes)
    env = {"SHOP_USER": "buyer", "SHOP_PASS": PASSWORD} if environ is None else environ
    return ScrapeAdapter(ScrapeConfig(**base), environ=env, sleep=clock.sleep, clock=clock.time, jitter=lambda d: d)


def everything_stored(caplog) -> str:
    """Every place a run or its checkpoint is kept, plus what the open API says about runs and the log."""
    stored = [
        list(PipelineRun.objects.values("source_ref", "errors", "notes", "status")),
        list(FeedCheckpoint.objects.values("source_ref", "fingerprint")),
        list(RejectedRow.objects.values("reason", "raw")),
        list(RawPart.objects.values("payload", "part_number_raw", "brand_raw", "description_raw")),
    ]
    client = Client()
    api = [client.get("/api/feed-runs/").content.decode()]
    for run in PipelineRun.objects.all():
        api.append(client.get(f"/api/feed-runs/{run.pk}/").content.decode())
    return json.dumps(stored, default=str) + "".join(api) + caplog.text


def fail_with(real, text_of, only_when=lambda args, kwargs: True):
    """A request that raises an exception repeating whatever the caller handed it, as a library may; others go through."""

    def raiser(*args, **kwargs):
        if only_when(args, kwargs):
            raise RuntimeError(f"upstream refused: {text_of(args, kwargs)}")
        return real(*args, **kwargs)

    return raiser


# ---- the scraping adapter -----------------------------------------------------------------------------------------


def test_a_proxy_url_that_requests_cannot_parse_leaves_its_password_in_no_stored_run(site, caplog):
    caplog.set_level(logging.DEBUG)
    env = {"SHOP_USER": "buyer", "SHOP_PASS": PASSWORD, "SHOP_PROXY": f"http://{PROXY_USER}:{PROXY_PASSWORD}@127.0.0.1:99999"}
    run = ingest(get_supplier("s"), scrape(site, env, proxy_envs=["SHOP_PROXY"]))
    assert run.status == PipelineRun.Status.FAILED and PipelineRun.objects.count() == 1
    seen = everything_stored(caplog)
    assert PROXY_PASSWORD not in seen and PROXY_USER not in seen


def test_the_login_password_is_stored_nowhere_even_when_a_library_repeats_it_in_an_exception(site, caplog):
    caplog.set_level(logging.DEBUG)
    adapter = scrape(site)
    adapter.session.request = fail_with(adapter.session.request, lambda a, k: f"{a} {k.get('data')}", lambda a, k: k.get("data") is not None)
    run = ingest(get_supplier("s"), adapter)
    assert run.status == PipelineRun.Status.FAILED
    assert "upstream refused" in json.dumps(run.errors)  # the message is kept, only the secret is taken out
    assert PASSWORD not in everything_stored(caplog)


def test_a_named_query_value_of_the_start_url_is_hidden_in_the_source_ref_and_in_every_message(site, caplog):
    caplog.set_level(logging.DEBUG)
    adapter = scrape(site, start_url=f"{site.url}/catalog?page=1&api_key={API_KEY}")
    adapter.session.request = fail_with(adapter.session.request, lambda a, k: a, lambda a, k: "/catalog" in a[1])  # the URL is in the exception text
    run = ingest(get_supplier("s"), adapter)
    seen = everything_stored(caplog)
    assert API_KEY not in seen and "api_key=" in run.source_ref and "page=1" in run.source_ref


def test_two_sources_that_differ_only_in_a_filter_keep_two_identities(site):
    refs = {scrape(site, start_url=f"{site.url}/catalog?cat={cat}&token={API_KEY}").source_ref for cat in ("brakes", "wheels")}
    assert len(refs) == 2 and all(API_KEY not in ref for ref in refs)


def test_a_secret_planted_in_every_place_of_a_scrape_run_appears_in_nothing_stored(site, caplog):
    caplog.set_level(logging.DEBUG)
    planted = {PASSWORD, PROXY_PASSWORD, API_KEY}
    env = {"SHOP_USER": "buyer", "SHOP_PASS": PASSWORD, "SHOP_PROXY": f"http://{PROXY_USER}:{PROXY_PASSWORD}@127.0.0.1:{site.port}"}
    adapter = scrape(site, env, proxy_envs=["SHOP_PROXY"], start_url=f"{site.url}/catalog?page=1&session={API_KEY}")
    adapter.session.request = fail_with(adapter.session.request, lambda a, k: f"{a} {k} {adapter._password}", lambda a, k: "/catalog" in a[1])
    ingest(get_supplier("s"), adapter)
    seen = everything_stored(caplog)
    assert not [secret for secret in planted if secret in seen]


# ---- the siblings: REST, SFTP, SOAP go through the same runner -----------------------------------------------------


def test_rest_a_bearer_token_an_exception_repeats_is_stored_nowhere(rest, caplog):  # noqa: F811
    _, url = rest(Behaviour(catalog_items(5)))
    adapter = RestAdapter(url, token=TOKEN)
    adapter.session.get = fail_with(adapter.session.get, lambda a, k: k.get("headers"))
    run = ingest(get_supplier("r"), adapter)
    assert run.status == PipelineRun.Status.FAILED and TOKEN not in everything_stored(caplog)


def test_rest_a_token_with_a_line_break_is_refused_by_requests_and_still_not_stored(rest, caplog):  # noqa: F811
    _, url = rest(Behaviour(catalog_items(5)))
    token = f"{TOKEN}\nX-Injected: 1"  # requests quotes the header value in its error
    run = ingest(get_supplier("r"), RestAdapter(url, token=token))
    seen = everything_stored(caplog)
    assert TOKEN not in seen and run.status != PipelineRun.Status.SUCCEEDED


def test_sftp_a_password_an_exception_repeats_is_stored_nowhere(sftp, tmp_path, caplog):  # noqa: F811
    write_csv(sftp.root / "feed.csv", [row("AB001")])
    server = sftp()
    adapter = SftpAdapter("127.0.0.1", "/feed.csv", "feeds", server.fingerprint(), password=PASSWORD, port=server.port, sleep=NoSleep())
    adapter._download = lambda: (_ for _ in ()).throw(OSError(f"login as {adapter.username} with {adapter.password} failed"))
    run = ingest(get_supplier("f"), adapter)
    assert run.status == PipelineRun.Status.FAILED and "failed" in json.dumps(run.errors)
    assert PASSWORD not in everything_stored(caplog)


def test_soap_a_token_in_the_wsdl_query_is_hidden_in_the_source_ref_and_the_messages(soap, caplog):  # noqa: F811
    server, url = soap(Behaviour(catalog_items(5)))
    adapter = SoapAdapter(f"{url}&apikey={API_KEY}", page_size=50, sleep=NoSleep())
    adapter.fetch_page = lambda offset, limit: (_ for _ in ()).throw(RuntimeError(f"bad wsdl {url}&apikey={API_KEY}"))
    run = ingest(get_supplier("o"), adapter)
    assert API_KEY not in everything_stored(caplog) and "?wsdl" in run.source_ref


# ---- the class: any text, any secret, any of the forms a message shows it in ---------------------------------------


def test_a_secret_is_removed_from_any_text_in_every_form_it_may_be_shown_in():
    from urllib.parse import quote, quote_plus

    from catalog.ingest.redact import REDACTED, Redactor

    rng = random.Random(7)
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()_+-=[]{};:,.<>/? \n\t'\""
    for _ in range(300):
        secret = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 24)))
        shown = [secret, quote(secret, safe=""), quote_plus(secret), repr(secret)[1:-1], repr(secret)]
        text = "boom " + " | ".join(f"<{form}>" for form in shown) + " end"
        out = Redactor([secret]).text(text)
        assert secret not in out.replace(REDACTED, "") or secret in REDACTED  # never survives, unless it is the placeholder itself
    nested = Redactor(["hunter22"]).data({"a": ["x hunter22", {"hunter22": 1}], "n": 3})
    assert "hunter22" not in json.dumps(nested) and nested["n"] == 3


def test_a_short_query_value_is_not_taken_out_of_ordinary_text():
    from catalog.ingest.redact import Redactor, query_values

    redactor = Redactor(query_values("https://shop.example/c?page=1&cat=brakes&token=ab"))
    assert redactor.text("page 1 of 11 failed") == "page 1 of 11 failed"
    assert "brakes" not in redactor.text("HTTP 500 for brakes") and "ab" not in redactor.text("sent ab") .replace("[redacted]", "")


# ---- a proxy that cannot be used is refused with a code and no secret ----------------------------------------------

BAD_PROXIES = [
    f"http://{PROXY_USER}:{PROXY_PASSWORD}@127.0.0.1:99999",
    f"http://{PROXY_USER}:{PROXY_PASSWORD}@",
    f"{PROXY_USER}:{PROXY_PASSWORD}@127.0.0.1:8080",
    f"ftp://{PROXY_USER}:{PROXY_PASSWORD}@127.0.0.1:21",
    f"http://{PROXY_USER}:{PROXY_PASSWORD}@[::1",
    f"http://{PROXY_USER}:{PROXY_PASSWORD}@bad host:8080",
    f"http://{PROXY_USER}:{PROXY_PASSWORD}@127.0.0.1:8080:9",
    f"http://{PROXY_USER}:{PROXY_PASSWORD}@127.0.0.1:0",
    f"http://{PROXY_USER}:{PROXY_PASSWORD}@127.0.0.1:port",
]
GOOD_PROXIES = ["http://127.0.0.1:8080", "https://proxy.example:3128", "http://u:p@proxy.example:8080", "http://[::1]:8080", "socks5://127.0.0.1:1080"]


@pytest.mark.parametrize("proxy", BAD_PROXIES)
def test_a_malformed_proxy_in_the_config_is_refused_when_the_config_loads_with_a_code_and_no_secret(proxy, site):
    with pytest.raises(ScrapeConfigError) as err:
        ScrapeConfig.from_dict({"start_url": f"{site.url}/catalog", "proxies": [proxy]})
    assert str(err.value).startswith("E_PROXY_URL") and PROXY_PASSWORD not in str(err.value) and PROXY_USER not in str(err.value)


@pytest.mark.parametrize("proxy", BAD_PROXIES)
def test_a_malformed_proxy_from_the_environment_ends_the_run_as_a_failed_run_with_a_code_before_any_request(proxy, site, caplog):
    env = {"SHOP_USER": "buyer", "SHOP_PASS": PASSWORD, "SHOP_PROXY": proxy}
    run = ingest(get_supplier("s"), scrape(site, env, proxy_envs=["SHOP_PROXY"]))
    assert run.status == PipelineRun.Status.FAILED and "E_PROXY_URL" in run.errors[0]["message"] and "SHOP_PROXY" in run.errors[0]["message"]
    assert site.log == [] and PROXY_PASSWORD not in everything_stored(caplog)


@pytest.mark.parametrize("proxy", GOOD_PROXIES)
def test_an_ordinary_proxy_url_is_accepted(proxy, site):
    env = {"SHOP_USER": "buyer", "SHOP_PASS": PASSWORD, "SHOP_PROXY": proxy}
    scrape(site, env, proxy_envs=["SHOP_PROXY"])  # constructing it is the check: it must not refuse
    ScrapeConfig.from_dict({"start_url": f"{site.url}/catalog", "proxies": [p for p in [proxy] if "@" not in p]})


def test_a_request_that_succeeds_with_a_secret_in_its_url_is_logged_without_it(site, caplog):
    caplog.set_level(logging.DEBUG)  # urllib3 logs the path and query of every request it makes
    run = ingest(get_supplier("s"), scrape(site, start_url=f"{site.url}/catalog?page=1&token={API_KEY}"))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 4
    assert "GET /catalog" in caplog.text and API_KEY not in everything_stored(caplog)
