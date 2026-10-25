"""What an exception from `requests` means for a run, answered once for every adapter that fetches over HTTP.

`requests` raises a family of exceptions (`requests.exceptions.RequestException` and its subclasses) and not only from the
connection: a body cut after the headers raises ChunkedEncodingError while the page is being READ, a gzip body that does not
decode raises ContentDecodingError, a malformed proxy URL raises InvalidURL. An adapter that catches two of them lets every other
one end the whole run with nothing stored from the page it was on, and that is the wrong answer for a cut connection.

Every subclass is one of two things:
  - a CONFIGURATION error (an invalid URL, a missing schema or a missing optional dependency such as SOCKS support, an invalid
    header or proxy URL): retrying cannot change it, so the run fails with a FIXED phrase. The exception's own text is never
    stored: it may quote the URL of a proxy, and the runner's redaction is a second line of defence, not the first;
  - TRANSIENT (everything else, also a subclass added by a later `requests`): retried with the adapter's backoff, then reported
    as a coded gap for that page, so the rows of the earlier pages are kept and the run is partial.
"""
from __future__ import annotations

from dataclasses import dataclass

from requests import exceptions as rx

CONFIG_ERRORS = (
    rx.InvalidURL,  # also InvalidProxyURL
    rx.InvalidSchema,  # also "Missing dependencies for SOCKS support"
    rx.MissingSchema,
    rx.InvalidHeader,
    rx.InvalidJSONError,  # also JSONDecodeError
    rx.URLRequired,
    rx.UnrewindableBodyError,
    rx.TooManyRedirects,
)

CONFIG_CODE = "E_REQUEST_INVALID"
CONFIG_PHRASE = "the request cannot be made as configured (an invalid URL, header or proxy setting, or a missing dependency); see the config"


@dataclass(frozen=True)
class Outcome:
    code: str  # E_..., the code the run's error shows
    transient: bool  # False: a configuration error, the run fails with CONFIG_PHRASE
    name: str  # the exception's class name, the only part of it that is ever stored


def classify(exc: BaseException) -> Outcome:
    name = type(exc).__name__
    if isinstance(exc, CONFIG_ERRORS):
        return Outcome(CONFIG_CODE, False, name)
    if isinstance(exc, rx.ChunkedEncodingError):
        return Outcome("E_PAGE_TRUNCATED", True, name)  # the body was cut after the headers
    if isinstance(exc, rx.ContentDecodingError):
        return Outcome("E_PAGE_ENCODING", True, name)
    if isinstance(exc, (rx.Timeout, rx.ConnectionError)):
        return Outcome("E_UNREACHABLE", True, name)
    return Outcome("E_TRANSPORT", True, name)
