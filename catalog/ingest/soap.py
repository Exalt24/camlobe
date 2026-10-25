"""A SOAP source read with zeep: an operation taking (offset, limit) and answering (total, item...)."""
from __future__ import annotations

import requests
from requests import Session
from zeep import Client
from zeep.exceptions import Fault, TransportError
from zeep.helpers import serialize_object
from zeep.transports import Transport

from .paged import FatalError, PagedAdapter, PermanentError, TransientError
from .redact import identity_url, query_values

TRANSIENT_FAULTS = ("server.busy", "server.unavailable", "server.timeout")


class SoapAdapter(PagedAdapter):
    def __init__(self, wsdl_url: str, operation: str = "GetCatalogPage", timeout: float = 15.0, **kwargs):
        if "@" in wsdl_url.split("//", 1)[-1].split("/", 1)[0]:
            raise ValueError("pass credentials separately, not inside the WSDL URL")
        super().__init__(source_ref=identity_url(wsdl_url), **kwargs)  # a token in the query is not part of the stored identity
        self._secrets = query_values(wsdl_url)
        self.operation = operation
        session = Session()
        self.client = Client(wsdl_url, transport=Transport(session=session, timeout=timeout, operation_timeout=timeout))

    def secret_values(self) -> list[str]:
        return list(self._secrets)

    def fetch_page(self, offset: int, limit: int):
        call = getattr(self.client.service, self.operation)
        try:
            result = call(offset=offset, limit=limit)
        except Fault as fault:
            code = (fault.code or "").lower()
            if any(code.endswith(t) or code == t for t in TRANSIENT_FAULTS):
                raise TransientError(f"SOAP fault {fault.code}") from fault
            if "auth" in code:
                raise FatalError(f"SOAP fault {fault.code}: the source refused our credentials") from fault
            raise PermanentError(f"SOAP fault {fault.code}: {fault.message}") from fault
        except TransportError as exc:
            if exc.status_code in (401, 403):
                raise FatalError(f"HTTP {exc.status_code}: the source refused our credentials") from exc
            if exc.status_code == 429 or exc.status_code >= 500:
                raise TransientError(f"HTTP {exc.status_code}") from exc
            raise PermanentError(f"HTTP {exc.status_code}") from exc
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise TransientError(type(exc).__name__) from exc
        data = serialize_object(result, target_cls=dict)
        items = data.get("item") or []
        return [dict(i) for i in items], data.get("total")
