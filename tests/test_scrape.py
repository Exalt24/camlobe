"""The scraping adapter, against a local fake shop site and a local forward proxy. It has never met a real site.

Every guard has a test in both directions: the case it must stop and the near miss it must let through.
"""
import json
import logging
import random

import pytest
from bs4 import BeautifulSoup
from django.core.management import call_command
from django.core.management.base import CommandError

from catalog.ingest.adapters import SourceGap, SourceRecord
from catalog.ingest.runner import ingest
from catalog.ingest.scrape import PARSER, ScrapeAdapter, ScrapeConfig, ScrapeConfigError
from catalog.models import PipelineRun, RawPart, RejectedRow

from .fakes import DroppingProxy, ForwardProxy, ScrapeSite, dead_proxy_url, shop_products
from .helpers import get_supplier

pytestmark = pytest.mark.django_db(transaction=True)

USER, PASSWORD = "buyer", "s3cret pass 42"
ENV = {"SHOP_USER": USER, "SHOP_PASS": PASSWORD}


class Clock:
    """A clock that only moves when the adapter sleeps, so waiting costs no time and is still measured."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def sites():
    made = []

    def make(n=7, **knobs):
        site = ScrapeSite(shop_products(n), **knobs)
        made.append(site)
        return site

    yield make
    for site in made:
        site.close()


@pytest.fixture
def proxies():
    made = []

    def make(kind=ForwardProxy):
        proxy = kind()
        made.append(proxy)
        return proxy

    yield make
    for proxy in made:
        proxy.close()


def config(site, **changes):
    base = dict(
        start_url=f"{site.url}/catalog?page=1", login_url=f"{site.url}/login", username_env="SHOP_USER", password_env="SHOP_PASS",
        session_cookie="sid", delay_seconds=0.5, backoff_base=1.0, backoff_cap=30.0, user_agent="camlobe-test/1.0 (a test)",
    )
    base.update(changes)
    return ScrapeConfig(**base)


def make_adapter(site, clock=None, environ=None, **changes):
    clock = clock or Clock()
    adapter = ScrapeAdapter(
        config(site, **changes), environ=ENV if environ is None else environ, sleep=clock.sleep, clock=clock.time, jitter=lambda d: d
    )
    adapter.clock = clock
    return adapter


def read(adapter, start=0):
    return list(adapter.records(start_after=start))


def rows(out):
    return [r for r in out if isinstance(r, SourceRecord)]


def gaps(out):
    return [g for g in out if isinstance(g, SourceGap)]


def numbers(out):
    return [r.fields["Part No"] for r in rows(out)]


EVERY_PART = [p["Part No"] for p in shop_products(7)]


# ---- the happy path: login with a CSRF token, then every page by its next link ----------------------------


def test_it_logs_in_with_the_csrf_token_and_reads_every_page_by_its_next_link(sites):
    site = sites()
    adapter = make_adapter(site)
    out = read(adapter)
    assert numbers(out) == EVERY_PART and [r.position for r in rows(out)] == list(range(1, 8)) and gaps(out) == []
    assert rows(out)[0].fields == {"Part No": "MF-H0001A", "Brand": "Marlow", "Description": "Oil filter 1", "Price": "2.00"}
    login_post = [e for e in site.log if e["method"] == "POST"]
    assert len(login_post) == 1 and login_post[0]["form_keys"] == ["csrf_token", "password", "username"]  # the token came from the form
    assert site.paths() == [  # the first page sends us to the login form, the form is read and posted, the page is asked for again
        "/robots.txt", "/catalog?page=1", "/login", "/login", "/catalog?page=1", "/catalog?page=2", "/catalog?page=3"
    ]
    assert [e["sid"] != "" for e in site.log if e["path"] == "/catalog"][-1]  # the session cookie went with the page requests
    assert adapter.notes()["pages"] == 3 and adapter.notes()["logins"] == 1 and adapter.fingerprint() == ""


def test_the_rows_go_through_the_runner_and_the_header_mapping(sites):
    site = sites()
    run = ingest(get_supplier("shop"), make_adapter(site))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 7
    raw = RawPart.objects.get(part_number_raw="MF-H0003A")
    assert (raw.brand_raw, raw.cost_cents, raw.description_raw) == ("Marlow", 400, "Oil filter 3")


def test_a_resume_reads_every_page_again_from_the_first_and_never_skips_rows_by_position(sites):
    out = read(make_adapter(sites()), start=4)
    assert [r.position for r in rows(out)] == [1, 2, 3, 4, 5, 6, 7] and numbers(out) == EVERY_PART


def test_a_site_that_needs_no_login_is_read_without_one(sites):
    site = sites(no_login=True)
    out = read(make_adapter(site, login_url="", username_env="", password_env=""))
    assert numbers(out) == EVERY_PART and "/login" not in site.paths()


# ---- credentials come from the environment and are never shown --------------------------------------------


def test_credentials_are_read_from_the_named_variables_and_the_config_never_holds_them(sites):
    site = sites()
    assert PASSWORD not in json.dumps(config(site).__dict__) and USER not in json.dumps(config(site).__dict__)
    with pytest.raises(ScrapeConfigError, match="SHOP_PASS is empty or not set") as err:
        make_adapter(site, environ={"SHOP_USER": USER})
    assert PASSWORD not in str(err.value) and USER not in str(err.value)
    with pytest.raises(ScrapeConfigError, match="password_env is required"):
        make_adapter(site, password_env="")


def test_a_wrong_password_ends_the_run_and_the_password_is_nowhere(sites, caplog):
    site = sites()
    caplog.set_level(logging.DEBUG)
    adapter = make_adapter(site, environ={"SHOP_USER": USER, "SHOP_PASS": "not the password 9"})
    run = ingest(get_supplier("shop"), adapter)
    assert run.status == PipelineRun.Status.PARTIAL and run.errors[0]["message"].startswith("E_LOGIN_FAILED")
    assert RawPart.objects.count() == 0
    everything = json.dumps([run.errors, run.notes]) + caplog.text + repr(adapter) + str(site.log)
    assert "not the password 9" not in everything and USER not in everything


def test_a_successful_run_leaves_no_credential_in_the_run_the_notes_or_the_stored_rows(sites, caplog):
    site = sites()
    caplog.set_level(logging.DEBUG)
    adapter = make_adapter(site)
    run = ingest(get_supplier("shop"), adapter)
    stored = json.dumps([r.payload for r in RawPart.objects.all()]) + json.dumps(run.notes) + json.dumps(run.errors) + run.source_ref
    assert PASSWORD not in stored + caplog.text + repr(adapter) and "s3cret" not in stored
    assert PASSWORD not in json.dumps(site.log)  # the site's own log holds field names, never the value


def test_the_login_form_without_the_csrf_field_is_refused_before_anything_is_posted(sites):
    site = sites(csrf_field="")
    out = read(make_adapter(site))
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_LOGIN_FORM"] and rows(out) == []
    assert [e for e in site.log if e["method"] == "POST"] == []


def test_every_hidden_field_of_the_form_is_posted_not_only_the_one_the_config_names(sites):
    site = sites()  # the config names no CSRF field, the form has one: it is still posted, as a browser would
    out = read(make_adapter(site, csrf_field=""))
    assert numbers(out) == EVERY_PART and [e["form_keys"] for e in site.log if e["method"] == "POST"] == [["csrf_token", "password", "username"]]


def test_a_login_the_site_does_not_accept_is_not_taken_for_one(sites):
    for knobs, changes in (
        # sent back to the form, and with no session cookie named in the config that is the only thing that says no
        ({"redirect_on_bad_login": True}, {"environ": {"SHOP_USER": USER, "SHOP_PASS": "wrong one 1"}, "session_cookie": ""}),
        ({}, {"session_cookie": "never-set"}),  # redirected on as if it worked, but the cookie the config expects is not there
    ):
        site = sites(**knobs)
        out = read(make_adapter(site, **changes))
        assert [g.message.split(":")[0] for g in gaps(out)] == ["E_LOGIN_FAILED"] and rows(out) == []
        assert site.log[-1]["method"] == "POST"  # it stopped at the answer to the post: no page was asked for on a login that failed


def test_a_site_that_accepts_the_login_and_never_a_session_ends_the_run_after_one_login(sites):
    site = sites(broken_session=True)
    adapter = make_adapter(site)
    out = read(adapter)
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_LOGIN_FAILED"] and rows(out) == [] and site.logins == 1


def test_a_challenge_page_in_answer_to_the_login_post_is_not_read_as_a_login(sites):
    out = read(make_adapter(sites(login_post_challenge=True)))
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_CHALLENGE"] and rows(out) == []


def test_a_redirect_to_another_site_is_not_followed(sites):
    site = sites(offsite_redirect=2)
    out = read(make_adapter(site))
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_OFFSITE"] and numbers(out) == EVERY_PART[:3]


# ---- the session: a cookie jar on disk, and one re-login ---------------------------------------------------


@pytest.mark.parametrize("expire_as", ["redirect", "form", "401"])
def test_a_session_that_expires_mid_run_is_logged_in_again_once_and_the_run_finishes(sites, expire_as):
    # however the site says "log in again": a redirect to the form, the form itself in a 200, or a 401
    site = sites(n=12, expire_after=2, expire_as=expire_as)  # two catalog pages per session, four pages in all
    adapter = make_adapter(site)
    out = read(adapter)
    assert numbers(out) == [p["Part No"] for p in shop_products(12)] and gaps(out) == []
    assert site.logins == 2 and adapter.notes()["relogins"] == 1
    assert site.paths().count("/catalog?page=3") == 2  # the page that found the session dead is asked for again


def test_a_second_expiry_ends_the_run_and_never_logs_in_a_third_time(sites):
    site = sites(n=12, expire_after=1)
    adapter = make_adapter(site)
    out = read(adapter)
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_SESSION_EXPIRED"]
    assert numbers(out) == [p["Part No"] for p in shop_products(12)][:6]  # two sessions of one page each... and nothing past the second
    assert site.logins == 2 and adapter.notes()["relogins"] == 1


def test_the_cookie_jar_keeps_the_session_between_runs_and_a_dead_jar_just_logs_in_again(sites, tmp_path):
    site = sites()
    jar = str(tmp_path / "shop.cookies")
    first = make_adapter(site, cookie_jar=jar)
    assert numbers(read(first)) == EVERY_PART and site.logins == 1
    before = len(site.log)
    second = make_adapter(site, cookie_jar=jar)  # a new process with the same jar
    assert numbers(read(second)) == EVERY_PART and site.logins == 1  # no second login
    assert "/login" not in [e["path"] for e in site.log[before:]]
    site.sessions.clear()  # the site forgot every session
    third = make_adapter(site, cookie_jar=jar)
    assert numbers(read(third)) == EVERY_PART and site.logins == 2
    assert "sid=" in (tmp_path / "shop.cookies").read_text(encoding="utf-8")  # it is a live session on disk: a secret


def test_a_run_without_a_jar_leaves_no_file_behind(sites, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    read(make_adapter(sites()))
    assert list(tmp_path.iterdir()) == []


# ---- politeness: the pause, Retry-After, backoff ------------------------------------------------------------


def test_requests_are_spaced_by_the_pause_and_a_longer_crawl_delay_wins(sites):
    for robots, pause in (("User-agent: *\nDisallow: /private/\n", 0.5), ("User-agent: *\nCrawl-delay: 3\n", 3.0)):
        site = sites()
        site.robots = robots
        adapter = make_adapter(site)
        stamps = []
        original = adapter.session.request

        def stamped(*args, _original=original, _adapter=adapter, **kwargs):
            stamps.append(_adapter.clock.now)
            return _original(*args, **kwargs)

        adapter.session.request = stamped
        read(adapter)
        gaps_between = [round(b - a, 6) for a, b in zip(stamps, stamps[1:])]
        assert len(stamps) >= 6 and min(gaps_between) >= pause


def test_a_429_is_waited_out_as_retry_after_says_and_the_run_finishes(sites):
    site = sites(throttle={"after": 1, "times": 2, "retry_after": 2})
    adapter = make_adapter(site)
    out = read(adapter)
    assert numbers(out) == EVERY_PART and gaps(out) == []
    assert adapter.notes()["rate_limited"] == 2 and adapter.clock.sleeps.count(2.0) == 2  # it waited what it was told, not less


def test_a_retry_after_longer_than_the_cap_is_not_waited_and_ends_the_run(sites):
    site = sites(throttle_forever=3600)
    adapter = make_adapter(site)
    out = read(adapter)
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_RATE_LIMITED"] and numbers(out) == EVERY_PART[:3]
    assert max(adapter.clock.sleeps) < 30 and "3600" in gaps(out)[0].message  # no hour long sleep, and the run says why


def test_a_429_that_never_clears_gives_up_after_the_retries_with_the_waits_growing(sites):
    site = sites(throttle_forever=1)
    adapter = make_adapter(site, max_retries=3)
    out = read(adapter)
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_RATE_LIMITED"] and "4 attempts" in gaps(out)[0].message
    waits = [s for s in adapter.clock.sleeps if s >= 1.0]
    assert waits == sorted(waits) and waits[-1] >= 4.0  # 1, 2, 4: exponential, and never faster than the server asked


def test_a_503_is_retried_with_backoff_and_a_site_that_stays_down_is_a_coded_gap(sites):
    site = sites(fail_times=2)
    adapter = make_adapter(site)
    assert numbers(read(adapter)) == EVERY_PART and adapter.notes()["retries"] == 2
    down = sites(fail_times=99)
    out = read(make_adapter(down, max_retries=2))
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_SERVER_ERROR"] and rows(out) == []


def test_a_missing_page_is_a_coded_gap(sites):
    site = sites()
    out = read(make_adapter(site, start_url=f"{site.url}/missing?x=1"))
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_HTTP_404"]


# ---- robots.txt and the user agent --------------------------------------------------------------------------


def test_robots_txt_that_disallows_the_catalog_stops_the_run_before_the_catalog_is_asked_for(sites):
    site = sites()
    site.robots = "User-agent: *\nDisallow: /catalog\n"
    out = read(make_adapter(site))
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_ROBOTS"] and rows(out) == []
    assert site.paths() == ["/robots.txt"]


def test_robots_txt_that_disallows_something_else_does_not_stop_it(sites):
    site = sites()
    assert numbers(read(make_adapter(site))) == EVERY_PART and "/robots.txt" in site.paths()


def test_a_disallow_for_this_user_agent_stops_it_and_one_for_another_bot_does_not(sites):
    site = sites()
    site.robots = "User-agent: camlobe-test\nDisallow: /\n"
    assert [g.message.split(":")[0] for g in gaps(read(make_adapter(site)))] == ["E_ROBOTS"]
    other = sites()
    other.robots = "User-agent: someotherbot\nDisallow: /\n"
    assert numbers(read(make_adapter(other))) == EVERY_PART


def test_the_robots_check_can_be_turned_off_and_then_robots_txt_is_not_even_fetched(sites):
    site = sites()
    site.robots = "User-agent: *\nDisallow: /\n"
    out = read(make_adapter(site, respect_robots=False))
    assert numbers(out) == EVERY_PART and "/robots.txt" not in site.paths()


def test_a_missing_robots_txt_means_no_rules_and_a_broken_one_means_everything_is_disallowed(sites):
    gone = sites()
    gone.robots = 404
    assert numbers(read(make_adapter(gone))) == EVERY_PART
    broken = sites()
    broken.robots = 500
    out = read(make_adapter(broken))
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_ROBOTS_UNAVAILABLE"] and set(broken.paths()) == {"/robots.txt"}


def test_every_request_carries_the_configured_user_agent_and_not_the_library_default(sites):
    site = sites()
    read(make_adapter(site))
    assert {e["ua"] for e in site.log} == {"camlobe-test/1.0 (a test)"}
    default = sites()
    adapter = ScrapeAdapter(ScrapeConfig(start_url=f"{default.url}/catalog?page=1", login_url=f"{default.url}/login", username_env="SHOP_USER", password_env="SHOP_PASS", delay_seconds=0), environ=ENV)
    list(adapter.records())
    assert {e["ua"] for e in default.log} == {"camlobe-feed/1.0 (supplier catalog ingest)"}


# ---- proxies ------------------------------------------------------------------------------------------------


def test_the_proxy_carries_every_request_and_without_one_nothing_does(sites, proxies):
    site, proxy = sites(), proxies()
    out = read(make_adapter(site, proxies=[proxy.url]))
    assert numbers(out) == EVERY_PART
    assert len(proxy.passed) == len(site.log) and all(e["via"] == "1.1 camlobe-test-proxy" for e in site.log)  # proved from both ends
    assert ("POST", f"{site.url}/login") in proxy.passed  # the login went through it too
    direct = sites()
    read(make_adapter(direct))
    assert all(e["via"] is None for e in direct.log)


def test_a_proxy_that_refuses_the_connection_is_skipped_for_the_next_one(sites, proxies):
    site, live = sites(), proxies()
    adapter = make_adapter(site, proxies=[dead_proxy_url(), live.url])
    out = read(adapter)
    assert numbers(out) == EVERY_PART and gaps(out) == []
    assert adapter.notes()["proxy_failovers"] == 1 and adapter.notes()["proxy"] == 1
    assert len(live.passed) == len(site.log) and all(e["via"] for e in site.log)


def test_a_working_first_proxy_is_used_and_the_dead_second_one_is_never_tried(sites, proxies):
    site, live = sites(), proxies()
    adapter = make_adapter(site, proxies=[live.url, dead_proxy_url()])
    assert numbers(read(adapter)) == EVERY_PART and adapter.notes()["proxy_failovers"] == 0


def test_when_every_proxy_refuses_the_run_ends_as_a_coded_gap_and_nothing_goes_direct(sites, proxies):
    site, hangs_up = sites(), proxies(DroppingProxy)
    adapter = make_adapter(site, proxies=[hangs_up.url, dead_proxy_url()], max_retries=1)
    out = read(adapter)
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_PROXY_UNAVAILABLE"] and rows(out) == []
    assert site.log == [] and hangs_up.accepted >= 1  # a proxy that fails is never bypassed, and it really was tried


def test_a_proxy_url_comes_from_an_environment_variable_and_credentials_are_not_allowed_in_the_config(sites, proxies):
    site, proxy = sites(), proxies()
    env = {**ENV, "SHOP_PROXY": proxy.url}
    assert numbers(read(make_adapter(site, environ=env, proxy_envs=["SHOP_PROXY"]))) == EVERY_PART and proxy.passed
    with pytest.raises(ScrapeConfigError, match="proxy_envs"):
        make_adapter(site, proxies=["http://user:pw@127.0.0.1:3128"])
    with pytest.raises(ScrapeConfigError, match="NOPE is empty or not set"):
        make_adapter(site, proxy_envs=["NOPE"])


# ---- a challenge or a block is never data -------------------------------------------------------------------


def test_a_bot_challenge_page_ends_the_run_as_a_gap_and_is_never_parsed(sites):
    site = sites(challenge_page=2)
    run = ingest(get_supplier("shop"), make_adapter(site))
    assert run.status == PipelineRun.Status.PARTIAL and run.errors[0]["message"].startswith("E_CHALLENGE")
    assert RawPart.objects.count() == 3 and run.errors[0]["where"] == "positions 4-4"  # page 1 only; the challenge stored nothing
    assert site.paths().count("/catalog?page=3") == 0  # and it did not carry on to the next page


def test_a_product_that_mentions_a_captcha_is_a_product_and_not_a_challenge(sites):
    products = shop_products(4)
    products[1]["Description"] = "Captcha reader module, access denied sensor"
    site = ScrapeSite(products)
    try:
        out = read(make_adapter(site))
        assert numbers(out) == [p["Part No"] for p in products] and gaps(out) == []
    finally:
        site.close()


def test_a_403_is_a_block_that_is_not_retried(sites):
    site = sites(block_page=2)
    out = read(make_adapter(site))
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_BLOCKED"] and numbers(out) == EVERY_PART[:3]
    assert site.paths().count("/catalog?page=2") == 1


def test_a_login_page_that_is_itself_a_challenge_stops_the_run(sites):
    site = sites(login_form_challenge=True)
    out = read(make_adapter(site))
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_CHALLENGE"] and [e for e in site.log if e["method"] == "POST"] == []


def test_a_page_with_no_product_table_and_no_marker_is_an_unexpected_page(sites):
    out = read(make_adapter(sites(), table_selector="table.nothing-like-this"))
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_UNEXPECTED_PAGE"] and rows(out) == []


def test_a_custom_challenge_marker_is_honoured(sites):
    site = sites()
    out = read(make_adapter(site, challenge_markers=["oil filter 5"]))  # a marker outside the table would stop it; inside the table it is data
    assert numbers(out) == EVERY_PART and gaps(out) == []
    out = read(make_adapter(site, challenge_markers=["next"]))  # the Next link is outside the table
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_CHALLENGE"]


# ---- next links ---------------------------------------------------------------------------------------------


def test_a_next_link_that_loops_back_ends_the_run_instead_of_reading_for_ever(sites):
    out = read(make_adapter(sites(loop_back=True)))
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_PAGE_LOOP"] and numbers(out) == EVERY_PART


def test_a_next_link_to_another_site_is_not_followed(sites):
    site = sites(offsite_next=True)
    out = read(make_adapter(site))
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_OFFSITE"] and numbers(out) == EVERY_PART[:6]


def test_the_page_limit_stops_a_site_with_more_pages_than_allowed(sites):
    out = read(make_adapter(sites(n=12), max_pages=2))
    assert [g.message.split(":")[0] for g in gaps(out)] == ["E_TOO_MANY_PAGES"] and len(rows(out)) == 6
    assert gaps(read(make_adapter(sites(n=12), max_pages=4))) == []  # exactly enough pages is fine


# ---- reading the table --------------------------------------------------------------------------------------


def table(html):
    return ScrapeAdapter._table(BeautifulSoup(html, PARSER).select_one("table"))


def test_a_table_without_a_thead_or_a_tbody_and_with_loose_markup_is_read():
    header, body = table(
        "<table class=products><tr><th>Part No<th>Brand<th>Price"
        "<tr><td>AB&amp;1<td><b>Marlow</b><td> $5.00 "
        "<tr><td>AB2<td>Dunmore<td>6.00</table>"
    )
    assert header == ["Part No", "Brand", "Price"] and body == [["AB&1", "Marlow", "$5.00"], ["AB2", "Dunmore", "6.00"]]


def test_a_row_with_the_wrong_number_of_cells_is_rejected_not_padded(sites):
    site = sites()
    adapter = make_adapter(site)
    original = adapter._table
    adapter._table = lambda t: (lambda h, r: (h, [r[0][:3]] + r[1:]))(*original(t))
    out = read(adapter)
    assert rows(out)[0].reject == "column_count" and rows(out)[1].reject == ""
    run = ingest(get_supplier("shop2"), make_adapter(site))
    assert RejectedRow.objects.count() == 0 and run.rows_written == 7  # the unbroken site has none


# ---- the command --------------------------------------------------------------------------------------------


def test_the_ingest_command_takes_a_scrape_config_and_the_credentials_from_the_environment(sites, tmp_path, monkeypatch, capsys):
    site = sites()
    cfg = tmp_path / "shop.json"
    cfg.write_text(json.dumps({
        "start_url": f"{site.url}/catalog?page=1", "login_url": f"{site.url}/login", "username_env": "SHOP_USER",
        "password_env": "SHOP_PASS", "delay_seconds": 0, "user_agent": "camlobe-test/1.0 (a test)",
    }), encoding="utf-8")
    with pytest.raises(CommandError, match="SHOP_USER is empty or not set"):
        call_command("ingest_feed", "shop", "--scrape-config", str(cfg))
    monkeypatch.setenv("SHOP_USER", USER)
    monkeypatch.setenv("SHOP_PASS", PASSWORD)
    call_command("ingest_feed", "shop", "--scrape-config", str(cfg))
    assert "succeeded: read=7 stored=7" in capsys.readouterr().out and RawPart.objects.count() == 7
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"start_url": site.url, "colour": "red"}), encoding="utf-8")
    with pytest.raises(CommandError, match="unknown setting"):
        call_command("ingest_feed", "shop", "--scrape-config", str(bad))
    with pytest.raises(CommandError, match="exactly one source"):
        call_command("ingest_feed", "shop", "--scrape-config", str(cfg), "--rest-url", "http://127.0.0.1:1/x")
    cfg.write_text(json.dumps({"start_url": f"{site.url}/catalog?page=1", "robots_txt": True}), encoding="utf-8")
    with pytest.raises(CommandError, match="unknown setting"):
        call_command("ingest_feed", "shop", "--scrape-config", str(cfg))


def test_a_scrape_run_that_ends_as_a_gap_is_a_partial_run_and_a_failed_command(sites, tmp_path, monkeypatch):
    site = sites(challenge_page=2)
    cfg = tmp_path / "shop.json"
    cfg.write_text(json.dumps({"start_url": f"{site.url}/catalog?page=1", "login_url": f"{site.url}/login", "username_env": "SHOP_USER", "password_env": "SHOP_PASS", "delay_seconds": 0}), encoding="utf-8")
    monkeypatch.setenv("SHOP_USER", USER)
    monkeypatch.setenv("SHOP_PASS", PASSWORD)
    with pytest.raises(CommandError, match="ended partial"):
        call_command("ingest_feed", "shop", "--scrape-config", str(cfg))


# ---- a seeded random world: every guard together --------------------------------------------------------------


@pytest.mark.parametrize("seed", range(24))
def test_random_sites_are_read_exactly_or_stopped_with_a_prefix_and_a_code(seed, sites, proxies, tmp_path):
    rng = random.Random(seed)
    n = rng.randint(0, 30)
    per_page = rng.randint(1, 7)
    expire = rng.choice([None, None, 1, 2, 3, 6])
    products = shop_products(n)
    site = ScrapeSite(products, per_page=per_page, expire_after=expire)
    if expire is None and rng.random() < 0.5:
        site.throttle = {"after": rng.randint(0, 3), "times": rng.randint(1, 3), "retry_after": rng.choice([0, 1, 2])}
    if rng.random() < 0.3:
        site.fail_times = rng.randint(1, 3) if expire is None else 0
    if rng.random() < 0.3:
        site.robots = rng.choice(["User-agent: *\nCrawl-delay: 2\n", 404, "User-agent: other\nDisallow: /\n"])
    live = proxies() if rng.random() < 0.5 else None
    chosen = ([proxies(DroppingProxy).url] if live and rng.random() < 0.5 else []) + ([live.url] if live else [])  # all dead is its own test
    jar = str(tmp_path / f"{seed}.cookies") if rng.random() < 0.5 else ""
    try:
        for _ in range(rng.randint(1, 2)):  # a second run may reuse the jar
            adapter = make_adapter(site, proxies=chosen, cookie_jar=jar, max_retries=4)
            out = read(adapter)
            got = numbers(out)
            want = [p["Part No"] for p in products]
            code = gaps(out)[0].message.split(":")[0] if gaps(out) else ""
            assert got == want[: len(got)] and len(gaps(out)) <= 1  # always a prefix of the truth: nothing twice, nothing out of order
            if code == "":
                assert got == want  # no gap means the whole site
            else:
                assert code in {"E_SESSION_EXPIRED", "E_SERVER_ERROR", "E_RATE_LIMITED"}
            assert adapter.notes()["logins"] <= 2  # one login, and one more only when the session died mid run
            assert adapter.notes()["relogins"] <= 1
            assert PASSWORD not in json.dumps(adapter.notes()) + repr(adapter)
        assert {e["ua"] for e in site.log} <= {"camlobe-test/1.0 (a test)"}
        if chosen:  # with a live proxy in the list, every request the site saw came through a proxy: proved from the site's side
            assert all(e["via"] for e in site.log)
        if not chosen:
            assert all(e["via"] is None for e in site.log)
    finally:
        site.close()
