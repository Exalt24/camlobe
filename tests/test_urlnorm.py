"""canonical_url: one name for one page, and never one name for two pages.

The equal side and the different side are tested apart, because a normaliser that merges too much loses a page of the
catalog and one that merges too little reads a page twice. Properties are generated; the named cases pin the rules in
the module text one by one.
"""
import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from catalog.ingest.urlnorm import TRACKING_NAMES, canonical_url, is_tracking, same_page

label = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=8)
hosts = st.builds(lambda a, b: f"{a}.{b}.test", label, label)
segments = st.lists(st.text(alphabet="abcXYZ019-_", min_size=1, max_size=6), min_size=0, max_size=4)
names = st.text(alphabet="abcdefghij", min_size=1, max_size=5)  # never starts with utm_ (no underscore) and is no tracking name
values = st.text(alphabet="abc123", max_size=4)
params = st.lists(st.tuples(names, values), min_size=0, max_size=5, unique_by=lambda p: p[0])
tracking = st.lists(st.sampled_from(["utm_source=x", "utm_campaign=y", "gclid=1", "fbclid=2", "_ga=3", "mc_cid=4"]), max_size=3, unique=True)

SETTINGS = hyp_settings(max_examples=150, deadline=None)


def build(scheme, host, path, pairs, port="", fragment=""):
    query = "&".join(f"{n}={v}" for n, v in pairs)
    return f"{scheme}://{host}{port}/{'/'.join(path)}{'?' + query if query else ''}{'#' + fragment if fragment else ''}"


@pytest.mark.parametrize(
    "raw, want",
    [
        ("HTTP://Shop.Test:80/Parts?b=2&a=1#top", "http://shop.test/Parts?a=1&b=2"),
        ("https://shop.test:443/p", "https://shop.test/p"),
        ("https://shop.test:8443/p", "https://shop.test:8443/p"),
        ("http://shop.test:443/p", "http://shop.test:443/p"),  # 443 is only the default of https
        ("http://shop.test", "http://shop.test/"),
        ("http://shop.test/a/./b/../c", "http://shop.test/a/c"),
        ("http://shop.test/a/b/..", "http://shop.test/a/"),
        ("http://shop.test/a/../..", "http://shop.test/"),
        ("http://shop.test/%e2%82%ac/%41%7e", "http://shop.test/%E2%82%AC/A~"),
        ("http://shop.test/p?utm_source=a&page=2&gclid=z", "http://shop.test/p?page=2"),
        ("http://shop.test/p?UTM_Medium=a&page=2", "http://shop.test/p?page=2"),
        ("http://shop.test/p?b=2&a=2&a=1", "http://shop.test/p?a=1&a=2&b=2"),
        ("http://shop.test/p?", "http://shop.test/p"),
        ("http://[::1]:80/a", "http://[::1]/a"),
    ],
)
def test_the_rules_one_by_one(raw, want):
    assert canonical_url(raw) == want


@pytest.mark.parametrize(
    "a, b",
    [
        ("http://shop.test/Parts", "http://shop.test/parts"),  # a path is case sensitive
        ("http://shop.test/parts", "http://shop.test/parts/"),  # the walk handles a redirect between these, the name does not
        ("http://shop.test/p?page=1", "http://shop.test/p?page=2"),
        ("http://shop.test/p?q=a", "http://shop.test/p?q=A"),
        ("http://shop.test/p?q", "http://shop.test/p?q="),  # a bare name and an empty value are not claimed equal
        ("http://shop.test/p?sort=asc", "http://shop.test/p"),
        ("http://shop.test/a%2Fb", "http://shop.test/a/b"),  # an escaped slash is not a separator
        ("http://shop.test/p?ref=1", "http://shop.test/p?ref=2"),  # `ref` is not on the tracking list, so it names a page
        ("http://a.test/p", "http://b.test/p"),
        ("http://shop.test/p", "https://shop.test/p"),
    ],
)
def test_two_pages_keep_two_names(a, b):
    assert not same_page(a, b)


@pytest.mark.parametrize("raw", ["", "   ", "not a url", "mailto:a@b.c", "ftp://x.test/a", "//x.test/a", "http:///nohost", "http://x.test:99999/a"])
def test_a_string_that_is_not_an_http_address_is_never_merged_with_another(raw):
    assert canonical_url(raw) == raw.strip()


def test_the_tracking_names_are_the_ones_the_docs_list():
    assert TRACKING_NAMES == {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "_ga", "igshid", "yclid"}
    assert is_tracking("utm_anything") and is_tracking("UTM_Source") and is_tracking("gclid") and not is_tracking("page") and not is_tracking("utm")


@given(st.sampled_from(["http", "https"]), hosts, segments, params, tracking)
@SETTINGS
def test_it_is_idempotent(scheme, host, path, pairs, track):
    url = build(scheme, host, path, pairs) + ("&" if pairs else "?") * bool(track) + "&".join(track)
    once = canonical_url(url)
    assert canonical_url(once) == once


@given(st.sampled_from(["http", "https"]), hosts, segments, params, tracking, st.randoms(use_true_random=False), st.booleans(), st.text(alphabet="abc", max_size=4))
@SETTINGS
def test_every_way_of_writing_one_page_has_one_name(scheme, host, path, pairs, track, rnd, with_port, fragment):
    plain = build(scheme, host, path, pairs)
    shuffled = list(pairs)
    rnd.shuffle(shuffled)
    extras = list(track)
    port = ":80" if scheme == "http" else ":443"
    messy = build(scheme.upper(), host.upper(), path, shuffled, port=port if with_port else "", fragment=fragment)
    if extras:
        messy = messy.split("#")[0] + ("&" if shuffled else "?") + "&".join(extras) + (f"#{fragment}" if fragment else "")
    assert canonical_url(messy) == canonical_url(plain)


@given(st.sampled_from(["http", "https"]), hosts, segments, params.filter(bool), values)
@SETTINGS
def test_a_changed_value_or_path_or_parameter_is_another_name(scheme, host, path, pairs, extra):
    base = canonical_url(build(scheme, host, path, pairs))
    first_name, first_value = pairs[0]
    changed_value = [(first_name, first_value + "z")] + pairs[1:]
    assert canonical_url(build(scheme, host, path, changed_value)) != base
    assert canonical_url(build(scheme, host, path, pairs + [("k" * 9, extra)])) != base  # a name outside names, so no clash with a generated one
    assert canonical_url(build(scheme, host, path + ["extra"], pairs)) != base
