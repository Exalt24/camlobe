# LIFESPAN: permanent
"""One name for one page: the canonical form of a catalog page address.

A supplier's site names the same page many ways: `HTTP://Shop.Test:80/Parts?b=2&a=1#top`, the same with `a` and `b` swapped,
and the same again with a `utm_source` the pager copied from a campaign link. The scraper reads a page once and calls a link
back to a page already read a loop, so it compares addresses by this form and never by the raw text.

What is made equal (RFC 3986, section 6.2.2 and 6.2.3, which say these changes never change the resource):
  - the scheme and the host are lower case, and a default port (80 for http, 443 for https) is dropped
  - dot segments are resolved (`/a/./b/../c` is `/a/c`) and a doubled slash stays (a path is not guessed at)
  - percent escapes are upper case (`%e2` is `%E2`) and an escaped unreserved character is the character (`%41` is `A`)
  - the fragment is dropped: it is never sent to the server
What is made equal by a rule of THIS tool, not by the RFC (a site could in theory treat these as different pages; see docs/feeds.md):
  - query parameters are sorted by name and then by value, and a parameter that only tracks the visitor
    (utm_*, gclid, fbclid, msclkid, mc_cid, mc_eid, _ga, igshid, yclid) is dropped
What is NEVER touched: the path's case (a path is case sensitive), a trailing slash (`/parts` and `/parts/` stay two names:
the walk treats a redirect between them as the one page itself), and any parameter that is not on the tracking list.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}
TRACKING_NAMES = frozenset({"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "_ga", "igshid", "yclid"})
TRACKING_PREFIX = "utm_"
UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")


def is_tracking(name: str) -> bool:
    """A query parameter that names the visitor or the campaign and never the page."""
    lowered = unquote(name).lower()
    return lowered.startswith(TRACKING_PREFIX) or lowered in TRACKING_NAMES


def _escapes(text: str) -> str:
    """Upper case every percent escape, and turn an escaped unreserved character back into the character."""

    def one(match: re.Match) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in UNRESERVED else "%" + match.group(1).upper()

    return _ESCAPE.sub(one, text)


def _dot_segments(path: str) -> str:
    """RFC 3986 section 5.2.4: remove `.` and `..` segments from an absolute path."""
    out: list[str] = []
    segments = path.split("/")
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == ".":
            if last:
                out.append("")
        elif segment == "..":
            if len(out) > 1:
                out.pop()
            if last:
                out.append("")
        else:
            out.append(segment)
    joined = "/".join(out)
    return joined if joined.startswith("/") else "/" + joined


def _query(query: str) -> str:
    pairs = []
    for part in query.split("&"):
        if not part:
            continue
        name, eq, value = part.partition("=")
        if is_tracking(name):
            continue
        pairs.append((_escapes(name), eq, _escapes(value)))
    pairs.sort()
    return "&".join(f"{name}{eq}{value}" for name, eq, value in pairs)


def canonical_url(url: str) -> str:
    """The one name of the page `url` points at (see the module text for what is and is not made equal).

    An address that is not an http or https URL with a host is returned stripped and otherwise as it came, because a
    string this function cannot read is never merged with another. It is idempotent: canonical_url(canonical_url(u)) is canonical_url(u).
    """
    text = (url or "").strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return text
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        return text
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"  # an IPv6 literal keeps its brackets
    userinfo = parts.netloc.rpartition("@")[0]
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    if userinfo:
        netloc = f"{userinfo}@{netloc}"
    path = _escapes(_dot_segments(parts.path or "/"))
    return urlunsplit((scheme, netloc, path, _query(parts.query), ""))


def same_page(a: str, b: str) -> bool:
    """Do two addresses name the same page, by canonical_url."""
    return canonical_url(a) == canonical_url(b)
