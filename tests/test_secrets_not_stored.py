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


# ---- second output round: the pieces a library cuts a secret into at URL delimiters --------------------------------------------


import re  # noqa: E402

import requests  # noqa: E402
from hypothesis import assume, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from catalog.ingest.redact import REDACTED, Redactor, userinfo_secrets  # noqa: E402

BACKSLASH = chr(92)
PUNCTUATION = string.punctuation  # every printable punctuation character
SECRET_TEXT = st.text(alphabet=string.ascii_letters + string.digits + PUNCTUATION, min_size=1, max_size=30)


def pieces_of(secret: str) -> set[str]:
    """What a URL parser can cut a secret into: the parts between URL delimiters and percent forms, 4 characters or longer."""
    return {piece for piece in re.split(r"[\\/?#@:%]", secret) if len(piece) >= 4}


def real_error_text(url: str) -> str:
    """The text requests REALLY produces for this proxy URL (a proxy that cannot be parsed, or one that refuses)."""
    try:
        requests.Session().get("http://127.0.0.1:9/", proxies={"http": url}, timeout=1)
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return ""


def test_the_first_half_of_a_proxy_password_with_a_backslash_is_not_left_in_the_error_requests_raises():
    password = "Zq9pass" + BACKSLASH + "word77"
    url = f"http://camuser:{password}@127.0.0.1:3128"
    raw = real_error_text(url)
    assert "Zq9pass" in raw  # requests and urllib3 split the URL at the backslash: this is the text the runner would store
    out = Redactor(userinfo_secrets(url)).text(raw)
    assert "Zq9pass" not in out and "word77" not in out and "camuser" not in out and "InvalidURL" in out


def test_a_proxy_password_that_holds_an_at_sign_is_read_up_to_the_last_at_sign():
    """Pinned by hand: the property tests below only find an at sign in a password when Hypothesis happens to draw one, and the guard for this line survived one full run that way."""
    url = "http://camuser:Zq9@pass77@127.0.0.1:99999"
    secrets = userinfo_secrets(url)
    assert secrets[1] == "Zq9@pass77" and "camuser" in secrets
    out = Redactor(secrets).text(real_error_text(url))
    assert "pass77" not in out and "Zq9" not in out and "camuser" not in out


@given(SECRET_TEXT)
@settings(max_examples=60, deadline=None)
def test_no_piece_of_a_secret_a_library_cuts_it_into_survives_in_the_real_error_text_of_a_malformed_proxy(secret):
    """The class: credential text reaches stored run errors by a route the whole-secret match does not cover."""
    url = f"http://camuser:{secret}@127.0.0.1:3128"
    out = Redactor(userinfo_secrets(url)).text(real_error_text(url))
    clean = out.replace(REDACTED, "")
    for piece in pieces_of(secret) | ({secret} if len(secret) >= 4 else set()):
        assert piece not in clean, (secret, piece, out)


@given(SECRET_TEXT, st.sampled_from([BACKSLASH, "/", "?", "#", "@", ":", "%5C", "%40", "%3A", "%2f"]))
@settings(max_examples=200, deadline=None)
def test_a_secret_cut_at_any_url_delimiter_is_removed_piece_by_piece_from_any_text(secret, cut):
    joined = secret + cut + secret[::-1] + "k9x2"  # a password that holds the delimiter
    out = Redactor([joined]).text(f"boom {joined[: len(secret)]} | {joined[len(secret) + len(cut):]} | {joined} end").replace(REDACTED, "")
    for piece in pieces_of(joined):
        assert piece not in out


def test_a_password_half_that_holds_a_percent_form_is_removed_whole_when_a_library_cuts_at_the_backslash():
    """Found by a random search on 2026-10-26 (it fails the same way on the commit before): `%000;` was split at its own `%00`, so the half a library shows was never a piece."""
    password = "%000;" + BACKSLASH + ";000%k9x2"
    out = Redactor([password]).text(f"boom %000; | ;000%k9x2 | {password} end")
    assert "000;" not in out.replace(REDACTED, "") and "k9x2" not in out
    plain = Redactor(["abcd" + BACKSLASH + "wxyz"]).text("boom abcd | wxyz end")  # CONTROL: an ordinary cut half is still removed
    assert "abcd" not in plain and "wxyz" not in plain


def test_a_secret_that_decodes_to_whitespace_does_not_turn_every_space_of_a_message_into_a_placeholder():
    """Found the same way: `+` is a space in a form encoded string, and a form of only whitespace matched every gap between two words."""
    message = "InvalidURL: Failed to parse: 'http://127.0.0.1:99999' is not a valid host or port"
    assert Redactor(["+", "http://camuser:+@127.0.0.1:3128"]).text(message) == message
    assert Redactor(["pass+word"]).text("the pass+word failed") == f"the {REDACTED} failed"  # CONTROL: a real secret that holds a plus is still removed


ORDINARY_ERRORS = [
    "InvalidURL: Failed to parse: 'http://127.0.0.1:99999' is not a valid host or port",
    "ConnectionError", "ReadTimeout", "ChunkedEncodingError", "HTTP 500", "HTTP 429 on every one of 4 attempts",
    "E_PAGE_TRUNCATED: the page ends inside its product table, so nothing is read from it",
    "E_LOGIN_REQUIRED: the site wants a login and the config has no login_url",
    "E_PROXY_UNAVAILABLE: every proxy refused the connection", "E_REQUEST_INVALID: the request cannot be made as configured (an invalid URL, header or proxy setting, or a missing dependency); see the config",
    "E_RATE_LIMITED: the site asked for a wait of 3600 s, longer than the 30 s allowed", "positions 11-20", "the page at offset 100 had 40 items",
    "SOAP fault Server.Busy", "the answer was not the expected JSON", "no column maps to cost; the feed has Colour, Size",
]


@given(SECRET_TEXT)
@settings(max_examples=200, deadline=None)
def test_the_fixed_phrases_of_ordinary_errors_are_unchanged_by_the_pieces_of_any_secret(secret):
    redactor = Redactor([secret, f"http://camuser:{secret}@127.0.0.1:3128"])
    for text in ORDINARY_ERRORS:
        assume(not any(piece in text for piece in pieces_of(secret)) and secret not in text)
        assert redactor.text(text) == text


def test_the_url_of_a_proxy_is_one_secret_and_its_host_and_scheme_are_not():
    redactor = Redactor(userinfo_secrets("http://pxuser:Pw99xx@proxy.example:8080"))
    assert redactor.text("connecting to proxy.example port 8080 over http") == "connecting to proxy.example port 8080 over http"
    assert REDACTED in redactor.text("bad: http://pxuser:Pw99xx@proxy.example:8080")
