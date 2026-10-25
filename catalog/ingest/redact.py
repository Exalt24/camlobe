"""One place where a secret is taken out of anything an ingest run stores or shows.

A run record, its notes, the checkpoint and the log are read by people and by the unauthenticated /api/feed-runs/,
and an exception's text is not under our control: `requests` puts the whole proxy URL into an InvalidURL message,
a header error repeats the header value, a driver may repeat the password it was given. So the runner does not trust
any adapter to keep a secret out of its messages. Each adapter NAMES the secrets it was configured with
(`secret_values()`), and the runner passes every piece of text it is about to store through `Redactor.text`.

Two rules keep this from damaging ordinary text:
  - a password, token or proxy credential is replaced wherever it appears, whatever its length, also in the forms
    a message may show it in (percent encoded, form encoded, escaped inside a repr);
  - a query value of a source URL is replaced only when it is 4 characters or longer, since a value such as `page=1`
    would otherwise turn every `1` in every message into a placeholder.

The identity of a source (`identity_url`) is a different question: it is stored as the run's source_ref and as the
key of the checkpoint, so two sources that differ only in a filter (`?cat=brakes`, `?cat=wheels`) must stay two. Only
a query value whose NAME says it is a secret (`api_key`, `token`, `sig`, `session`, ...) is hidden there. A secret in
a parameter whose name says nothing cannot be told from a filter value, and hiding both would merge two sources into
one checkpoint; that is a known limit, stated in docs/feeds.md.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Any, Iterable, Iterator
from urllib.parse import parse_qsl, quote, quote_plus, unquote, unquote_plus, urlsplit, urlunsplit

REDACTED = "[redacted]"
MIN_QUERY_VALUE = 4  # shorter query values are not redacted from text: they are filters and page numbers
SECRET_WORDS = frozenset(
    "key apikey token secret password passwd pwd auth authorization sig signature credential credentials "
    "session sessionid sid bearer jwt".split()
)
_WORDS = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def secret_name(name: str) -> bool:
    """`api_key`, `apiKey`, `X-Amz-Signature` and `access_token` say they are secrets; `page` and `cat` do not."""
    words = {w.lower() for w in _WORDS.findall(name)}
    return bool(words & SECRET_WORDS) or name.lower() in SECRET_WORDS


MIN_PIECE = 4  # a piece of a secret shorter than this is not taken out of text: it would turn ordinary words into placeholders
_DELIMITERS = re.compile(r"[\\/?#@:]|%[0-9A-Fa-f]{2}|%")  # where a URL parser cuts: the backslash, slash, ? # @ :, and a percent form


def _pieces(form: str) -> set[str]:
    """The parts of a secret a library may show on their own. `requests` and urllib3 split a proxy URL at its delimiters, so a
    password such as `Zq9pass<backslash>word77` is cut at the backslash and the first half is quoted in the error ('...:Zq9pass'
    is not a valid host or port) with the rest gone. Each part of 4 characters or more is a secret too."""
    return {piece for piece in _DELIMITERS.split(form) if len(piece) >= MIN_PIECE}


def _forms(secret: str) -> set[str]:
    """The ways a message may show `secret`: as it is, percent encoded, form encoded, and as the inside of a repr."""
    out = {secret, quote(secret, safe=""), quote_plus(secret), unquote(secret), unquote_plus(secret), repr(secret)[1:-1]}
    out = {form for form in out if form}
    if "://" in secret:  # a URL is removed whole: its host and scheme are no secret, and nor are the pieces of its encoded form
        return out
    return out | {piece for form in out for piece in _pieces(form)}


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()):
        found: set[str] = set()
        for secret in secrets:
            if isinstance(secret, str) and secret:
                found |= _forms(secret)
        self._ordered = sorted(found, key=len, reverse=True)  # the longest first, so a secret holding another is removed whole

    @classmethod
    def for_adapter(cls, adapter: object) -> "Redactor":
        names = getattr(adapter, "secret_values", None)
        return cls(names() if callable(names) else ())

    def text(self, value: str) -> str:
        for secret in self._ordered:
            if secret in value:
                value = value.replace(secret, REDACTED)
        return value

    @contextmanager
    def log_guard(self) -> Iterator[None]:
        """While it is open, no log record made by ANY logger keeps a secret: urllib3's debug line shows the request path and
        query, a driver may log what it was given. The text is replaced when the record is made, before any handler sees it."""
        if not self._ordered:
            yield
            return
        previous = logging.getLogRecordFactory()

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            try:
                message = record.getMessage()
            except Exception:  # a log call whose arguments do not fit its format is the caller's bug, not ours to hide
                return record
            clean = self.text(message)
            if clean != message:
                record.msg, record.args = clean, ()
            return record

        logging.setLogRecordFactory(factory)
        try:
            yield
        finally:
            logging.setLogRecordFactory(previous)

    def data(self, value: Any) -> Any:
        """The same for the nested notes of a run."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(k) if isinstance(k, str) else k: self.data(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.data(v) for v in value]
        return value


def query_values(url: str) -> list[str]:
    """The query values of a URL that are long enough to be a secret, or whose name says so, for redacting text."""
    try:
        query = urlsplit(url).query
    except ValueError:
        return []
    values = []
    for name, value in parse_qsl(query, keep_blank_values=True):
        if value and (secret_name(name) or len(value) >= MIN_QUERY_VALUE):
            values.append(value)
    for part in query.split("&"):  # the raw, still encoded value too
        name, _, raw = part.partition("=")
        if raw and (secret_name(unquote_plus(name)) or len(raw) >= MIN_QUERY_VALUE):
            values.append(raw)
    return values


def userinfo_secrets(url: str) -> list[str]:
    """The user and the password of a URL, found by hand because a malformed URL (the very case that leaks) cannot be parsed.

    The whole URL is a secret too when it carries credentials: an error that quotes it must not show its other half.
    """
    rest = url.split("://", 1)[-1]
    if "@" not in rest:
        return []
    userinfo = rest.rsplit("@", 1)[0]  # up to the LAST @, whatever the password holds: a # or a / in it would end an "authority"
    user, _, password = userinfo.partition(":")
    out = [url]
    if password:
        out.append(password)
    if len(user) >= MIN_QUERY_VALUE:
        out.append(user)
    return out


def identity_url(url: str) -> str:
    """The URL as the identity of a source: the value of a query parameter whose name says it is a secret is hidden."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[unreadable url]"
    pieces = []
    for part in parts.query.split("&") if parts.query else []:
        name, eq, value = part.partition("=")
        pieces.append(f"{name}={REDACTED}" if eq and value and secret_name(unquote_plus(name)) else part)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(pieces), ""))
