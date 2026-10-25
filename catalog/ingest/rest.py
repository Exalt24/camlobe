"""A paginated JSON REST source: GET <url>?offset=N&limit=M answering {"items": [...], "total": N}."""
from __future__ import annotations

import json

import requests

from .paged import FatalError, PagedAdapter, PermanentError, TransientError
from .transport import CONFIG_CODE, CONFIG_PHRASE, classify


def _retry_after(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:  # an HTTP date: not worth parsing, fall back to the backoff
        return None


def _json_body(response: requests.Response):
    """JSON is UTF-8 (RFC 8259): read the bytes, not requests' guess from the Content-Type, which is ISO-8859-1 for any
    text/* type with no charset and turned every accented brand into mojibake."""
    return json.loads(response.content)


class RestAdapter(PagedAdapter):
    def __init__(self, url: str, token: str | None = None, timeout: tuple[float, float] = (5.0, 20.0), session=None, **kwargs):
        # the URL is the source identity and is stored with the run, so it must never carry a secret
        if "?" in url or "@" in url.split("//", 1)[-1].split("/", 1)[0]:
            raise ValueError("pass credentials as a token, not inside the URL")
        super().__init__(source_ref=url, **kwargs)
        self.url = url
        self.timeout = timeout
        self.session = session or requests.Session()
        self.headers = {"Accept": "application/json"}
        self._token = token or ""
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def secret_values(self) -> list[str]:
        return [self._token] if self._token else []

    def fetch_page(self, offset: int, limit: int):
        try:
            response = self.session.get(
                self.url, params={"offset": offset, "limit": limit}, headers=self.headers, timeout=self.timeout
            )
        except requests.exceptions.RequestException as exc:  # every one: a cut body is as transient as a refused connection
            outcome = classify(exc)
            if not outcome.transient:
                raise FatalError(f"{CONFIG_CODE}: {CONFIG_PHRASE}") from None
            raise TransientError(outcome.name) from exc
        status = response.status_code
        if status in (401, 403):
            raise FatalError(f"HTTP {status}: the source refused our credentials")
        if status == 429 or status >= 500:
            raise TransientError(f"HTTP {status}", retry_after=_retry_after(response))
        if status >= 400:
            raise PermanentError(f"HTTP {status}")
        try:
            body = _json_body(response)
            items = body["items"]
            total = body.get("total")
        except (ValueError, KeyError, TypeError) as exc:
            raise PermanentError("the answer was not the expected JSON") from exc
        if not isinstance(items, list) or (total is not None and not isinstance(total, int)):
            raise PermanentError("the answer was not the expected JSON")
        return items, total
