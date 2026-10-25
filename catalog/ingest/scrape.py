"""A supplier site that has no feed: log in, walk the product table page by page, and hand the rows over as records.

READ THIS FIRST. This adapter was built and tested against a local fake site only. It has never been run against a
real site. Scraping a site is for sites whose owner has said you may, under terms you have read: robots.txt is
checked by default and a bot challenge or block page ends the run, but neither is permission.

What it does, in the order a run does it:

  1. robots.txt is fetched once (unless the config turns it off) and every URL after that is checked against it,
     with the site's Crawl-delay raised into the pause if it is longer than the configured one. A robots.txt that
     cannot be read because the server failed (5xx) is read as "everything disallowed", as RFC 9309 says.
  2. A login form is read for its hidden fields (the CSRF token among them), filled with the credentials and posted.
     The credentials are read from the environment variables the config NAMES, in the constructor, and kept in private
     attributes. The adapter names them (`secret_values`) and the runner takes them out of every message, note, run record
     and log record before it is stored, because an exception's text (requests quotes a whole proxy URL) is not ours.
  3. The session cookie jar can be saved to a file and loaded on the next run, so a valid session is reused. A
     session that expires mid run is logged in again ONCE; a second expiry ends the run.
  4. Requests are spaced by a pause, a 429 is waited out as its Retry-After says (a wait longer than the cap is not
     waited, the run stops), a 5xx or a timeout is retried with backoff, and with several proxies a proxy that
     refuses the connection is skipped for the next one.
  5. Pages are followed by their `next` link, on the same site only, never twice, up to a page limit. A next link that
     is not a page (`#`, empty, `javascript:`, marked disabled, the page itself) means there is none.
  6. The product table of each page becomes records keyed by its header text, which the usual header mapping reads.

The walk ends as a success only when the SITE said it ended: every page was read whole (its table and html closed, its
body as long as announced, its bytes decodable with the charset it declares) and the first page had rows. Anything else is
a coded gap, so the run is partial: it delists nothing and never completes the checkpoint. A run never resumes by row
position (the order of a site changes), it walks again from the first page.

A page that is not data is never parsed as data: HTTP 403, a 429 that will not clear, a robots.txt refusal, a 200 page
carrying a bot challenge marker outside the product table, a page with no product table. Each ends the run as a
SourceGap with a code (E_BLOCKED, E_RATE_LIMITED, E_ROBOTS, E_CHALLENGE, E_UNEXPECTED_PAGE, ...), so the run is
partial and nothing is stored for that page. Nothing here tries to get past a challenge.

HTML is read with BeautifulSoup (beautifulsoup4) on the lxml parser, because a supplier's pages are not well formed
XML (a cell without its closing tag is ordinary) and a table is easier to read with a tolerant tree and a CSS
selector than with a regular expression.
"""
from __future__ import annotations

import codecs
import json
import os
import random
import re
import time
from dataclasses import dataclass, field, fields
from html.parser import HTMLParser
from http.cookiejar import LWPCookieJar
from typing import Callable, Iterator, Mapping
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .adapters import SourceGap, SourceRecord
from .paged import FatalError
from .redact import identity_url, query_values, userinfo_secrets
from .rest import _retry_after
from .transport import CONFIG_CODE, CONFIG_PHRASE, classify

DEFAULT_USER_AGENT = "camlobe-feed/1.0 (supplier catalog ingest)"
DEFAULT_CHALLENGE_MARKERS = (
    "checking your browser",
    "cf-chl",
    "/cdn-cgi/challenge-platform",
    "g-recaptcha",
    "h-captcha",
    "captcha",
    "are you a robot",
    "verify you are human",
    "unusual traffic",
    "access denied",
)
PARSER = "lxml"  # closes an unclosed <td> or <tr> the way a browser does; the stdlib parser nests them instead
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
MAX_REDIRECT_HOPS = 3
MAX_COLSPAN = 1000  # the HTML standard's own limit for a colspan


def default_next_selector() -> str:
    """rel is a set of ASCII case insensitive tokens (HTML standard 4.6.7), so the next link is the one whose rel HAS the token
    `next`: `rel="nofollow next"` and `rel="NEXT"` are next links, `rel="nextpage"` is not."""
    return "a[rel~=next i]"


PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
_PROXY_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")
_PROXY_HOST = re.compile(r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._~%-]+)(?::(?P<port>[^:]*))?")


class ScrapeConfigError(ValueError):
    """The config cannot be used. The message names the setting or the environment variable, never a value."""


def proxy_problem(url: str) -> str:
    """Why `url` is not a usable proxy URL, or "". The answer is a fixed phrase: a proxy URL may carry a password, and
    what is wrong with it must never be said by quoting a part of it."""
    scheme, sep, rest = url.partition("://")
    if not sep or not _PROXY_SCHEME.fullmatch(scheme):
        return "it has no scheme such as http://"
    if scheme.lower() not in PROXY_SCHEMES:
        return "its scheme is not one of http, https, socks5, socks5h"
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    hostport = authority.rsplit("@", 1)[-1]
    match = _PROXY_HOST.fullmatch(hostport)
    if not hostport or match is None:
        return "its host is missing or not valid"
    port = match.group("port")
    if port is not None and not (port.isascii() and port.isdigit() and 1 <= int(port) <= 65535):
        return "its port is not a number from 1 to 65535"
    return ""


@dataclass
class ScrapeConfig:
    start_url: str
    login_url: str = ""  # empty: the pages need no login
    username_env: str = ""  # NAMES of environment variables, never the credentials themselves
    password_env: str = ""
    username_field: str = "username"
    password_field: str = "password"
    csrf_field: str = "csrf_token"  # a hidden input of the login form; the form is refused if it is missing
    session_cookie: str = ""  # when set, a login only counts if this cookie is in the jar afterwards
    cookie_jar: str = ""  # a file the session is saved to and loaded from; it holds a live session, treat it as a secret
    table_selector: str = "table.products"
    next_selector: str = field(default_factory=lambda: default_next_selector())  # a rel TOKEN, case insensitive: "nofollow next", "NEXT"
    delay_seconds: float = 1.0
    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_cap: float = 30.0
    user_agent: str = DEFAULT_USER_AGENT
    respect_robots: bool = True
    proxies: list[str] = field(default_factory=list)  # proxy URLs without credentials
    proxy_envs: list[str] = field(default_factory=list)  # names of variables holding a proxy URL, credentials allowed
    challenge_markers: list[str] = field(default_factory=lambda: list(DEFAULT_CHALLENGE_MARKERS))
    max_pages: int = 1000
    timeout: float = 20.0
    allow_empty: bool = False  # a table with no rows is the end of the data (a supplier that may list nothing), not a fault

    def __post_init__(self) -> None:
        for index, url in enumerate(self.proxies):
            problem = proxy_problem(url)
            if problem:
                raise ScrapeConfigError(f"E_PROXY_URL: proxies[{index}] is not a usable proxy URL: {problem}")

    @classmethod
    def from_dict(cls, data: Mapping) -> "ScrapeConfig":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ScrapeConfigError(f"unknown setting(s): {', '.join(sorted(unknown))}")
        if not data.get("start_url"):
            raise ScrapeConfigError("start_url is required")
        return cls(**data)

    @classmethod
    def from_file(cls, path: str) -> "ScrapeConfig":
        try:
            with open(path, encoding="utf-8") as handle:
                return cls.from_dict(json.load(handle))
        except (OSError, json.JSONDecodeError) as exc:
            raise ScrapeConfigError(f"cannot read the scrape config {path}: {type(exc).__name__}") from exc


class _Stop(Exception):
    """The run cannot go on: it ends as a gap with this code, or as a failed run when `fatal`. Never holds a credential."""

    def __init__(self, code: str, message: str, fatal: bool = False):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.fatal = code, message, fatal


class _Transient(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _SessionExpired(Exception):
    pass


# BOMs, longest first: the UTF-32 little endian mark starts with the UTF-16 one
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"), (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
)
_HEADER_CHARSET = re.compile(r"charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE)
_META_CHARSET = re.compile(rb"<meta[^>]+?charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE)
_LATIN_ONE = frozenset({"iso8859-1", "latin-1", "ascii", "cp1252"})  # a browser reads all of these as windows-1252


def _codec(label: str, from_meta: bool = False) -> str | None:
    try:
        name = codecs.lookup(label.strip()).name
    except LookupError:
        return None
    if from_meta and name.startswith(("utf-16", "utf-32")):
        return "utf-8"  # a meta tag is read as ASCII to find it, so a page that says UTF-16 there is not UTF-16
    return name


def decode_page(data: bytes, content_type: str = "") -> str:
    """The text of a page, decoded as the page says: its byte order mark, else the charset of its Content-Type, else its meta
    tag, else UTF-8. Never ISO-8859-1 by default (requests does that for any text/* type with no charset, which turns UTF-8
    into mojibake). A latin-1 label reads as windows-1252, as a browser does. Raises UnicodeError when nothing fits."""
    for bom, codec in _BOMS:
        if data.startswith(bom):
            return data.decode(codec)
    labels = []
    header = _HEADER_CHARSET.search(content_type or "")
    if header:
        labels.append((header.group(1), False))
    meta = _META_CHARSET.search(data[:4096])
    if meta:
        labels.append((meta.group(1).decode("ascii", "replace"), True))
    labels.append(("utf-8", False))
    for label, from_meta in labels:
        codec = _codec(label, from_meta)
        if codec is None:
            continue
        try:
            return data.decode("cp1252" if codec in _LATIN_ONE else codec)
        except UnicodeDecodeError:
            if codec in _LATIN_ONE:
                return data.decode("latin-1")  # the few bytes windows-1252 leaves undefined are C1 controls in latin-1
    raise UnicodeDecodeError("utf-8", data, 0, 1, "the page's declared charset does not fit its bytes")


class _TagScan(HTMLParser):
    """Reads the page's own tags, not the tree a tolerant parser builds from them: lxml closes a table the way a browser does,
    so a body cut in the middle of a row looks like a whole page once it is parsed. What matters is what the SITE sent."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open_tables = 0
        self.html_closed = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.open_tables += 1

    def handle_endtag(self, tag):
        if tag == "table":
            self.open_tables -= 1
        elif tag in ("body", "html"):
            self.html_closed = True


@dataclass
class _Page:
    url: str  # the address the page was served from (after the redirects), not the one asked for
    soup: BeautifulSoup
    table: object
    table_closed: bool  # every <table> the site opened, it closed
    html_closed: bool  # the page reached its closing body or html tag


def _scan(text: str) -> tuple[bool, bool]:
    scan = _TagScan()
    scan.feed(text)
    scan.close()
    return scan.open_tables <= 0, scan.html_closed


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme, parts.netloc.lower()


def _check_url(url: str, what: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ScrapeConfigError(f"{what} must be an http or https URL")
    if parts.username or parts.password:
        raise ScrapeConfigError(f"{what} must not carry credentials")


class ScrapeAdapter:
    def __init__(
        self,
        config: ScrapeConfig,
        session: requests.Session | None = None,
        environ: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float], float] | None = None,
    ):
        self.cfg = config
        _check_url(config.start_url, "start_url")
        self.source_ref = identity_url(config.start_url)  # stored with every run: a secret query value is not part of it
        env = os.environ if environ is None else environ
        self._username = self._password = ""
        if config.login_url:
            _check_url(config.login_url, "login_url")
            if _origin(config.login_url) != _origin(config.start_url):
                raise ScrapeConfigError("login_url must be on the same site as start_url")
            for setting in ("username_env", "password_env"):
                name = getattr(config, setting)
                if not name:
                    raise ScrapeConfigError(f"{setting} is required when login_url is set")
                if not env.get(name):
                    raise ScrapeConfigError(f"environment variable {name} is empty or not set")
            self._username, self._password = env[config.username_env], env[config.password_env]
        self._proxies = list(config.proxies)
        for url in self._proxies:
            if "@" in urlsplit(url).netloc:
                raise ScrapeConfigError("a proxy with credentials goes in proxy_envs, not in proxies")
        self._proxy_problem = ""
        self._secrets = [self._password, *query_values(config.start_url), *query_values(config.login_url)]
        for name in config.proxy_envs:
            if not env.get(name):
                raise ScrapeConfigError(f"environment variable {name} is empty or not set")
            self._proxies.append(env[name])
            self._secrets.extend(userinfo_secrets(env[name]))
            problem = proxy_problem(env[name])
            if problem and not self._proxy_problem:
                # a value read from the environment is checked when the run starts, so the failure is a run record in the
                # same place as every other feed failure; the message names the variable and never a part of its value
                self._proxy_problem = f"the proxy in {name} is not a usable proxy URL: {problem}"
        self._sleep, self._clock = sleep, clock
        self._jitter = jitter or (lambda d: random.uniform(d / 2, d))
        self._last_request: float | None = None
        self._crawl_delay = 0.0
        self._robots: robotparser.RobotFileParser | None = None
        self.session = session or requests.Session()
        self.session.trust_env = False  # no ambient proxy, no .netrc: what is used is what the config says
        self.session.headers["User-Agent"] = config.user_agent
        self._jar: LWPCookieJar | None = None
        self._dead: set[int] = set()
        self._position = 0
        self._data_pages = 0
        self._initial_login_done = False
        self._relogins = 0
        self._stats = {
            "requests": 0, "pages": 0, "logins": 0, "relogins": 0, "rate_limited": 0, "retries": 0,
            "proxy_failovers": 0, "robots": "off" if not config.respect_robots else "not read",
        }

    # ---- the interface the runner uses -------------------------------------------------------------

    def fingerprint(self) -> str:
        return ""  # a live site has no content identity: every run reads it again

    def secret_values(self) -> list[str]:
        """What the runner takes out of every message it stores: the password, proxy credentials, query values of the URLs."""
        return [secret for secret in self._secrets if secret]

    def notes(self) -> dict:
        return dict(self._stats)

    def cleanup(self) -> None:
        self._save_jar()

    # The order of a live site can change between two runs (a part is withdrawn, a price re-sorts the list), so row N of the
    # earlier run is not row N now. The adapter therefore never resumes by position: every run walks from the first page, and
    # what it already stored is not stored again (a row's key and "unchanged since the latest stored version" see to that).
    resumable = False

    def records(self, start_after: int = 0) -> Iterator[SourceRecord | SourceGap]:
        """Every row of the site from its first page. `start_after` is accepted for the interface and ignored (see `resumable`)."""
        self._position = 0
        try:
            if self._proxy_problem:
                raise _Stop("E_PROXY_URL", self._proxy_problem, fatal=True)
            self._load_jar()
            yield from self._walk()
        except _Stop as stop:
            self._stats["stopped"] = stop.code
            if stop.fatal:
                raise FatalError(str(stop)) from None
            yield SourceGap(self._position + 1, self._position + 1, str(stop))
        finally:
            self._save_jar()

    # ---- walking -----------------------------------------------------------------------------------

    def _walk(self) -> Iterator[SourceRecord]:
        if self.cfg.respect_robots:
            self._read_robots()
        url, seen, pages = urldefrag(self.cfg.start_url)[0], set(), 0  # a fragment is never sent and never tells two pages apart
        while url:
            if url in seen:
                raise _Stop("E_PAGE_LOOP", "a next link leads back to a page already read")
            if pages >= self.cfg.max_pages:
                raise _Stop("E_TOO_MANY_PAGES", f"more than {self.cfg.max_pages} pages")
            seen.add(url)
            page = self._page(url)
            served = urldefrag(page.url)[0]  # where a redirect landed: the base of the page's links, and a second name for it
            if served != url:
                if served in seen:
                    raise _Stop("E_PAGE_LOOP", "a next link leads back to a page already read")
                seen.add(served)
            pages += 1
            self._data_pages += 1
            self._stats["pages"] += 1
            soup = page.soup
            if not page.table_closed:  # nothing is read from a page the site did not finish: its last row may be cut
                raise _Stop("E_PAGE_TRUNCATED", "the page ends inside its product table, so nothing is read from it")
            header, rows = self._table(page.table)
            if not rows and not self.cfg.allow_empty:
                first = pages == 1  # an empty FIRST page is a failed run; an empty later one is a gap
                raise _Stop("E_PAGE_EMPTY", "the product table has no rows" + ("; allow_empty says a supplier may list nothing" if first else ""), fatal=first)
            for cells in rows:
                self._position += 1
                raw = " | ".join(cells)[:2000]
                if len(cells) != len(header):
                    yield SourceRecord(self._position, {}, raw, reject="column_count")
                else:
                    yield SourceRecord(self._position, dict(zip(header, cells)), raw)
            url = self._next_url(soup, served, asked=url)
            if url and _origin(url) != _origin(self.cfg.start_url):
                raise _Stop("E_OFFSITE", "a next link leaves the site")
            if not page.html_closed:  # a body cut after the table, or inside the next link, is not the site saying the catalog ended
                raise _Stop("E_PAGE_TRUNCATED", "the page ends before its html does, so the site may have cut it short (its next link too)")

    def _next_url(self, soup: BeautifulSoup, current: str, asked: str = "") -> str:
        """The URL of the next page, or "" when the page has none. A link that is not a page is "no next page": an empty
        href, `#`, `javascript:`, a link the site marks disabled (the class or aria-disabled of the last page's pager), and
        a link back to the page itself (by the address it was served from or the one asked for). A link to a DIFFERENT page already
        read is not excused here: the walk calls it a loop. A relative link is resolved against `current`, the address the page was
        SERVED from (after a trailing slash redirect that is not the address that was asked for), or against the page's first
        `<base href>` when it has one, which is itself resolved against that address."""
        link = soup.select_one(self.cfg.next_selector)
        if link is None:
            return ""
        href = (link.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:") or self._says_disabled(link):
            return ""
        base_tag = soup.select_one("base[href]")  # the FIRST base element with an href is the document base, the rest are ignored
        base = urljoin(current, base_tag["href"].strip()) if base_tag is not None else current
        target = urldefrag(urljoin(base, href))[0]
        itself = {urldefrag(current)[0]} | ({urldefrag(asked)[0]} if asked else set())  # the page has two names after a redirect
        return "" if target in itself else target

    @staticmethod
    def _says_disabled(link) -> bool:
        classes = link.get("class") or []
        if isinstance(classes, str):
            classes = classes.split()
        return any("disabled" in c.lower() for c in classes) or str(link.get("aria-disabled", "")).strip().lower() == "true" or link.has_attr("disabled")

    @staticmethod
    def _cells(tr, kinds=("td", "th")) -> list:
        return tr.find_all(kinds, recursive=False)  # the row's own cells: a table nested in a cell has its own rows

    @staticmethod
    def _span(cell) -> int:
        try:
            return min(max(int(str(cell.get("colspan", "1")).strip()), 1), MAX_COLSPAN)
        except ValueError:
            return 1

    @staticmethod
    def _table(table) -> tuple[list[str], list[list[str]]]:
        """(the header, the rows) of the product table, read from ITS OWN rows (never a nested table's) and cells (a th in a body
        row is a cell like a td). A cell with colspan fills that many columns, in the header and in a row. The header is the
        first row of the thead that has any text (a second one, a filter row, is skipped), else the first row of the table."""
        own = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
        if not own:
            return [], []
        in_head = [tr for tr in own if tr.parent.name == "thead"]
        header_tr = next((tr for tr in in_head if tr.get_text(strip=True)), in_head[0] if in_head else own[0])
        header: list[str] = []
        for cell in ScrapeAdapter._cells(header_tr):
            text = cell.get_text(" ", strip=True)
            header.extend([text, *[f"{text} ({n})" if text else "" for n in range(2, ScrapeAdapter._span(cell) + 1)]])
        body = [tr for tr in own if tr.parent.name == "tbody"] or [tr for tr in own if tr is not header_tr and tr.parent.name != "thead"]
        rows = []
        for tr in body:
            if tr is header_tr or not ScrapeAdapter._cells(tr, ("td",)):  # a row of th cells only is a heading, not a part
                continue
            cells: list[str] = []
            for cell in ScrapeAdapter._cells(tr):
                cells.extend([cell.get_text(" ", strip=True), *[""] * (ScrapeAdapter._span(cell) - 1)])
            rows.append(cells)
        return header, rows

    @staticmethod
    def _decode(response: requests.Response) -> str:
        try:
            return decode_page(response.content, response.headers.get("Content-Type", ""))
        except UnicodeError:
            raise _Stop("E_PAGE_ENCODING", "the page cannot be decoded as text without replacement characters") from None

    def _page(self, url: str) -> _Page:
        """The page at `url`, logging in or in again when the site sends us to its login form."""
        while True:
            try:
                return self._read_page(url)
            except _SessionExpired:
                self._renew_session()

    def _renew_session(self) -> None:
        if not self.cfg.login_url:
            raise _Stop("E_LOGIN_REQUIRED", "the site wants a login and the config has no login_url")
        if self._data_pages == 0:
            if self._initial_login_done:
                raise _Stop("E_LOGIN_FAILED", "logged in, and the site still sends us to the login form")
            self._initial_login_done = True
            self._login()
            return
        if self._relogins >= 1:
            raise _Stop("E_SESSION_EXPIRED", "the session expired again after one re-login; not trying a third time")
        self._relogins += 1
        self._stats["relogins"] += 1
        self._login()

    def _read_page(self, url: str) -> _Page:
        response, url = self._fetch(url)
        announced = response.headers.get("Content-Length", "")
        if announced.isdigit() and not response.headers.get("Content-Encoding") and len(response.content) != int(announced):
            raise _Stop("E_PAGE_TRUNCATED", "the body is not as long as the site said it would be")
        text = self._decode(response)
        soup = BeautifulSoup(text, PARSER)
        table = soup.select_one(self.cfg.table_selector)
        self._refuse_a_challenge(soup, table)
        if soup.select_one("input[type=password]") is not None:
            raise _SessionExpired()
        if table is None:
            raise _Stop("E_UNEXPECTED_PAGE", "the page has no product table")
        table_closed, html_closed = _scan(text)
        return _Page(url, soup, table, table_closed, html_closed)

    def _refuse_a_challenge(self, soup: BeautifulSoup, table=None) -> None:
        """A challenge is looked for everywhere EXCEPT the product table: a product called "Captcha reader" is a product."""
        outside = str(soup).replace(str(table), "", 1) if table is not None else str(soup)
        lowered = outside.lower()
        for marker in self.cfg.challenge_markers:
            if marker.lower() in lowered:
                raise _Stop("E_CHALLENGE", "the page carries a bot challenge; it was not read as data and nothing is done to get past it")

    # ---- login -------------------------------------------------------------------------------------

    def _login(self) -> None:
        response, _ = self._fetch(self.cfg.login_url, login_page=True)
        soup = BeautifulSoup(self._decode(response), PARSER)
        self._refuse_a_challenge(soup)
        password_input = soup.select_one("input[type=password]")
        form = password_input.find_parent("form") if password_input is not None else None
        if form is None:
            raise _Stop("E_LOGIN_FORM", "the login page has no form with a password field")
        hidden = {i["name"]: i.get("value", "") for i in form.find_all("input", type="hidden") if i.get("name")}
        if self.cfg.csrf_field and not hidden.get(self.cfg.csrf_field):
            raise _Stop("E_LOGIN_FORM", f"the login form has no hidden field named {self.cfg.csrf_field}")
        action = urljoin(self.cfg.login_url, form.get("action") or self.cfg.login_url)
        if _origin(action) != _origin(self.cfg.start_url):
            raise _Stop("E_OFFSITE", "the login form posts to another site")
        data = {**hidden, self.cfg.username_field: self._username, self.cfg.password_field: self._password}
        posted = self._request("POST", action, data)
        self._stats["logins"] += 1
        refused = posted.status_code >= 400
        if posted.status_code in REDIRECT_STATUSES:
            refused = self._is_login(urljoin(action, posted.headers.get("Location", "")))
        elif posted.status_code == 200:
            after = BeautifulSoup(self._decode(posted), PARSER)
            self._refuse_a_challenge(after)
            refused = after.select_one("input[type=password]") is not None
        if not refused and self.cfg.session_cookie and not any(c.name == self.cfg.session_cookie for c in self.session.cookies):
            refused = True
        if refused:
            raise _Stop("E_LOGIN_FAILED", f"the site did not accept the login (HTTP {posted.status_code})")
        self._save_jar()

    def _is_login(self, url: str) -> bool:
        return bool(self.cfg.login_url) and urlsplit(url).path == urlsplit(self.cfg.login_url).path

    # ---- requests ----------------------------------------------------------------------------------

    def _fetch(self, url: str, login_page: bool = False) -> tuple[requests.Response, str]:
        """GET `url`, following redirects by hand: same site only, a redirect to the login form is an expired session.
        Returns the response and the address it was served from."""
        for _ in range(MAX_REDIRECT_HOPS + 1):
            response = self._request("GET", url)
            if response.status_code in REDIRECT_STATUSES:
                target = urljoin(url, response.headers.get("Location", ""))
                if _origin(target) != _origin(self.cfg.start_url):
                    raise _Stop("E_OFFSITE", "the site redirected to another site")
                if self._is_login(target) and not login_page:
                    raise _SessionExpired()
                url = target
                continue
            if response.status_code == 401 and not login_page:
                raise _SessionExpired()
            if response.status_code >= 400:
                raise _Stop(f"E_HTTP_{response.status_code}", f"HTTP {response.status_code} for a page")
            return response, url
        raise _Stop("E_REDIRECT_LOOP", "too many redirects")

    def _request(self, method: str, url: str, data: dict | None = None) -> requests.Response:
        """One request with the pause, the retries and the proxy failover. 3xx and 4xx other than 403 and 429 are returned."""
        self._check_robots(url)
        cfg, code, last = self.cfg, "E_UNREACHABLE", ""
        for attempt in range(cfg.max_retries + 1):
            self._throttle()
            self._stats["requests"] += 1
            delay = self._jitter(min(cfg.backoff_cap, cfg.backoff_base * 2**attempt))
            try:
                response = self._send(method, url, data)
            except _Transient as exc:
                code, last = exc.code, str(exc)
            else:
                status = response.status_code
                if status == 403:
                    raise _Stop("E_BLOCKED", "HTTP 403: the site refused the request; it is not retried and nothing is done about it")
                if status == 429:
                    asked = _retry_after(response)
                    if asked is not None and asked > cfg.backoff_cap:
                        raise _Stop("E_RATE_LIMITED", f"the site asked for a wait of {asked:g} s, longer than the {cfg.backoff_cap:g} s allowed")
                    self._stats["rate_limited"] += 1
                    code, last = "E_RATE_LIMITED", "HTTP 429"
                    delay = max(delay, asked or 0.0)
                elif status >= 500:
                    code, last = "E_SERVER_ERROR", f"HTTP {status}"
                else:
                    return response
            if attempt < cfg.max_retries:
                self._stats["retries"] += 1
                self._sleep(delay)
        raise _Stop(code, f"{last} on every one of {cfg.max_retries + 1} attempts")

    def _send(self, method: str, url: str, data: dict | None) -> requests.Response:
        kwargs = {"allow_redirects": False, "timeout": self.cfg.timeout}
        if data is not None:
            kwargs["data"] = data
        if not self._proxies:
            try:
                return self.session.request(method, url, **kwargs)
            except requests.exceptions.RequestException as exc:
                raise self._refused(exc) from exc
        if len(self._dead) >= len(self._proxies):
            self._dead.clear()  # every proxy failed earlier: a retry tries them all again, one may be back
        for index, proxy in enumerate(self._proxies):
            if index in self._dead:
                continue
            try:
                response = self.session.request(method, url, proxies={"http": proxy, "https": proxy}, **kwargs)
            except (requests.exceptions.ProxyError, requests.Timeout, requests.ConnectionError):
                self._dead.add(index)  # this proxy refused or dropped the connection: the next one carries the request
                self._stats["proxy_failovers"] += 1
                continue
            except requests.exceptions.RequestException as exc:  # not the proxy's fault: a cut body, an invalid URL
                raise self._refused(exc) from exc
            self._stats["proxy"] = index
            return response
        raise _Transient("E_PROXY_UNAVAILABLE", "every proxy refused the connection")

    @staticmethod
    def _refused(exc: requests.exceptions.RequestException) -> Exception:
        """Any error `requests` raises, as the run's own outcome (see transport.py): retried, or a fixed phrase."""
        outcome = classify(exc)
        if outcome.transient:
            return _Transient(outcome.code, outcome.name)  # the class name only, never the text of the exception
        return _Stop(CONFIG_CODE, CONFIG_PHRASE, fatal=True)

    def _throttle(self) -> None:
        pause = max(self.cfg.delay_seconds, self._crawl_delay)
        if pause and self._last_request is not None:
            wait = pause - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    # ---- robots.txt --------------------------------------------------------------------------------

    def _read_robots(self) -> None:
        scheme, host = _origin(self.cfg.start_url)
        try:
            response = self._request("GET", f"{scheme}://{host}/robots.txt")
        except _Stop as stop:
            if stop.code == "E_SERVER_ERROR":  # RFC 9309: a robots.txt the server fails on is read as everything disallowed
                raise _Stop("E_ROBOTS_UNAVAILABLE", "robots.txt answered with a server error, so nothing is read") from stop
            raise
        parser = robotparser.RobotFileParser()
        if response.status_code == 200:
            parser.parse(response.text.splitlines())
            self._stats["robots"] = "read"
        else:  # a missing robots.txt (or any other 4xx) means no rules, as RFC 9309 says
            parser.parse([])
            self._stats["robots"] = f"none (HTTP {response.status_code})"
        self._robots = parser
        delay = parser.crawl_delay(self.cfg.user_agent)
        if delay:
            self._crawl_delay = float(delay)
            self._stats["crawl_delay"] = self._crawl_delay

    def _check_robots(self, url: str) -> None:
        if self._robots is not None and not self._robots.can_fetch(self.cfg.user_agent, url):
            raise _Stop("E_ROBOTS", f"robots.txt disallows {urlsplit(url).path}")

    # ---- the session cookie jar --------------------------------------------------------------------

    def _load_jar(self) -> None:
        if not self.cfg.cookie_jar:
            return
        self._jar = LWPCookieJar(self.cfg.cookie_jar)
        try:
            self._jar.load(ignore_discard=True, ignore_expires=False)
        except (OSError, ValueError):
            pass  # no jar yet, or an unreadable one: log in as if there were none
        self.session.cookies = self._jar

    def _save_jar(self) -> None:
        if self._jar is None:
            return
        try:
            self._jar.save(ignore_discard=True, ignore_expires=True)
            os.chmod(self.cfg.cookie_jar, 0o600)
        except OSError:
            pass
