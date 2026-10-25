"""Local stand-ins for the three remote transports. They run in this process, on 127.0.0.1, with invented data."""
from __future__ import annotations

import json
import os
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

import paramiko

# ------------------------------------------------------------------------------------------------
# HTTP: a paged JSON API and a SOAP service share one behaviour script.
# ------------------------------------------------------------------------------------------------


class Behaviour:
    """What the fake does per offset. `script[offset]` is a list of statuses served before a normal answer."""

    def __init__(self, items, script=None, always=None, cap=None, token=None, retry_after=None, hide_total=False, serve_as=None):
        self.items = items
        self.serve_as = dict(serve_as or {})  # offset -> the offset whose page is served instead (a server that repeats a page)
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.always = dict(always or {})  # offset -> status, forever
        self.cap = cap  # largest page the server will give, whatever the client asks
        self.token = token
        self.retry_after = retry_after
        self.hide_total = hide_total
        self.requests: list[tuple[int, int]] = []
        self.lock = threading.Lock()

    def decide(self, offset, limit, auth):
        with self.lock:
            self.requests.append((offset, limit))
            if self.token and auth != f"Bearer {self.token}":
                return 401, None
            if offset in self.always:
                return self.always[offset], None
            queue = self.script.get(offset)
            if queue:
                return queue.pop(0), None
        size = min(limit, self.cap) if self.cap else limit
        offset = self.serve_as.get(offset, offset)
        return 200, self.items[offset : offset + size]


class _Server:
    def __init__(self, handler_cls, behaviour):
        handler_cls.behaviour = behaviour
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.httpd.server_address[1]
        self.behaviour = behaviour
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _RestHandler(BaseHTTPRequestHandler):
    behaviour: Behaviour

    def log_message(self, *args):
        pass

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        offset, limit = int(q["offset"][0]), int(q["limit"][0])
        status, items = self.behaviour.decide(offset, limit, self.headers.get("Authorization"))
        if items is None:
            self.send_response(status)
            if status == 429 and self.behaviour.retry_after is not None:
                self.send_header("Retry-After", str(self.behaviour.retry_after))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = {"items": items}
        if not self.behaviour.hide_total:
            body["total"] = len(self.behaviour.items)
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def rest_server(behaviour: Behaviour) -> _Server:
    return _Server(_RestHandler, behaviour)


WSDL = """<?xml version="1.0"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/" xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
  xmlns:tns="urn:camlobe:fake" xmlns:xsd="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:camlobe:fake" name="FakeCatalog">
 <types><xsd:schema targetNamespace="urn:camlobe:fake" elementFormDefault="qualified">
  <xsd:element name="GetCatalogPage"><xsd:complexType><xsd:sequence>
   <xsd:element name="offset" type="xsd:int"/><xsd:element name="limit" type="xsd:int"/>
  </xsd:sequence></xsd:complexType></xsd:element>
  <xsd:element name="GetCatalogPageResponse"><xsd:complexType><xsd:sequence>
   <xsd:element name="total" type="xsd:int" minOccurs="0"/>
   <xsd:element name="item" minOccurs="0" maxOccurs="unbounded"><xsd:complexType><xsd:sequence>
    <xsd:element name="sku" type="xsd:string"/>
    <xsd:element name="manufacturer" type="xsd:string" minOccurs="0"/>
    <xsd:element name="title" type="xsd:string" minOccurs="0"/>
    <xsd:element name="price" type="xsd:string" minOccurs="0"/>
    <xsd:element name="gtin" type="xsd:string" minOccurs="0"/>
   </xsd:sequence></xsd:complexType></xsd:element>
  </xsd:sequence></xsd:complexType></xsd:element>
 </xsd:schema></types>
 <message name="In"><part name="parameters" element="tns:GetCatalogPage"/></message>
 <message name="Out"><part name="parameters" element="tns:GetCatalogPageResponse"/></message>
 <portType name="CatalogPort"><operation name="GetCatalogPage"><input message="tns:In"/><output message="tns:Out"/></operation></portType>
 <binding name="CatalogBinding" type="tns:CatalogPort"><soap:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
  <operation name="GetCatalogPage"><soap:operation soapAction="urn:camlobe:fake/GetCatalogPage"/>
   <input><soap:body use="literal"/></input><output><soap:body use="literal"/></output></operation></binding>
 <service name="CatalogService"><port name="CatalogPort" binding="tns:CatalogBinding"><soap:address location="http://127.0.0.1:{port}/soap"/></port></service>
</definitions>"""

ENVELOPE = '<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>{body}</soap:Body></soap:Envelope>'


class _SoapHandler(BaseHTTPRequestHandler):
    behaviour: Behaviour

    def log_message(self, *args):
        pass

    def do_GET(self):
        wsdl = WSDL.replace("{port}", str(self.server.server_address[1])).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(wsdl)))
        self.end_headers()
        self.wfile.write(wsdl)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"])).decode()
        offset = int(re.search(r"<(?:\w+:)?offset>(\d+)<", raw).group(1))
        limit = int(re.search(r"<(?:\w+:)?limit>(\d+)<", raw).group(1))
        status, items = self.behaviour.decide(offset, limit, self.headers.get("Authorization"))
        if items is None and status == 200:
            status = 500
        if items is None:
            if status == -1:  # a SOAP fault with a transient code, served as HTTP 500
                fault = "<soap:Fault><faultcode>Server.Busy</faultcode><faultstring>try again</faultstring></soap:Fault>"
                data = ENVELOPE.format(body=fault).encode()
                status = 500
            else:
                data = b""
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        parts = []
        if not self.behaviour.hide_total:
            parts.append(f"<tns:total>{len(self.behaviour.items)}</tns:total>")
        for it in items:
            inner = "".join(f"<tns:{k}>{escape(str(v))}</tns:{k}>" for k, v in it.items())
            parts.append(f"<tns:item>{inner}</tns:item>")
        body = f'<tns:GetCatalogPageResponse xmlns:tns="urn:camlobe:fake">{"".join(parts)}</tns:GetCatalogPageResponse>'
        data = ENVELOPE.format(body=body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def soap_server(behaviour: Behaviour) -> _Server:
    return _Server(_SoapHandler, behaviour)


# ------------------------------------------------------------------------------------------------
# SFTP: paramiko's server side, serving one directory.
# ------------------------------------------------------------------------------------------------


class _SftpHandle(paramiko.SFTPHandle):
    truncate_at: int | None = None

    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def read(self, offset, length):
        if self.truncate_at is not None and offset >= self.truncate_at:
            return b""
        if self.truncate_at is not None:
            length = min(length, self.truncate_at - offset)
        return super().read(offset, length)


def _make_sftp_interface(root: str, truncate_at):
    class Handle(_SftpHandle):
        pass

    Handle.truncate_at = truncate_at

    class Interface(paramiko.SFTPServerInterface):
        def _real(self, path):
            return root + self.canonicalize(path)

        def list_folder(self, path):
            out = []
            for name in os.listdir(self._real(path)):
                attr = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(self._real(path), name)))
                attr.filename = name
                out.append(attr)
            return out

        def stat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

        lstat = stat

        def open(self, path, flags, attr):
            try:
                fd = os.open(self._real(path), flags | getattr(os, "O_BINARY", 0), 0o666)
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)
            handle = Handle(flags)
            handle.filename = self._real(path)
            handle.readfile = handle.writefile = os.fdopen(fd, "rb")
            return handle

    return Interface


class _Auth(paramiko.ServerInterface):
    def __init__(self, user, password):
        self.user, self.password = user, password

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        ok = username == self.user and password == self.password
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class SftpServer:
    def __init__(self, root, user="feeds", password="correct horse", truncate_at=None):
        self.host_key = paramiko.RSAKey.generate(2048)
        self.user, self.password, self.root, self.truncate_at = user, password, str(root), truncate_at
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self.transports = []
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def fingerprint(self):
        from catalog.ingest.sftp import key_fingerprint

        return key_fingerprint(self.host_key)

    def _serve(self):
        iface = _make_sftp_interface(self.root, self.truncate_at)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            transport = paramiko.Transport(conn)
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, iface)
            self.transports.append(transport)
            try:
                transport.start_server(server=_Auth(self.user, self.password))
            except (paramiko.SSHException, EOFError, OSError):
                pass

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass
        for t in self.transports:
            t.close()


# ------------------------------------------------------------------------------------------------
# A supplier site that needs a login: CSRF token, session cookie, expiry, rate limit, a challenge page.
# ------------------------------------------------------------------------------------------------

import http.client  # noqa: E402
import secrets  # noqa: E402
from html import escape as html_escape  # noqa: E402

CHALLENGE_PAGE = (
    "<html><head><title>Just a moment...</title></head><body><h1>Checking your browser before accessing the site.</h1>"
    '<div class="cf-chl-widget"></div></body></html>'
)


class ScrapeSite:
    """A small shop site on 127.0.0.1 that records every request it gets (never a password).

    products: list of dicts with the table's columns. Knobs a test can set at any time:
      expire_after      a session dies after this many catalog requests (None: never)
      throttle          {"after": n, "times": k, "retry_after": s}: after n catalog requests, the next k answer 429
      throttle_forever  every catalog request after the first gets a 429 with this Retry-After (None: off)
      challenge_page    catalog page number that answers 200 with a bot challenge instead of the table
      block_page        catalog page number that answers 403
      robots            the robots.txt text, or an int to answer that HTTP status
      no_login          the catalog is public
      login_form_challenge  the login page itself is a bot challenge
      csrf_field        name of the hidden CSRF input ("" for none)
      loop_back, offsite_next, fail_times   a next link that loops, a next link off the site, n answers of 503
      expire_as, broken_session, offsite_redirect, redirect_on_bad_login, login_post_challenge   see the lines in __init__
    """

    COLUMNS = ["Part No", "Brand", "Description", "Price"]

    def __init__(self, products, per_page=3, user="buyer", password="s3cret pass 42", **knobs):
        self.products, self.per_page, self.user, self.password = products, per_page, user, password
        self.expire_after = knobs.pop("expire_after", None)
        self.throttle = knobs.pop("throttle", None)
        self.throttle_forever = knobs.pop("throttle_forever", None)
        self.challenge_page = knobs.pop("challenge_page", None)
        self.block_page = knobs.pop("block_page", None)
        self.robots = knobs.pop("robots", "User-agent: *\nDisallow: /private/\n")
        self.no_login = knobs.pop("no_login", False)
        self.login_form_challenge = knobs.pop("login_form_challenge", False)
        self.csrf_field = knobs.pop("csrf_field", "csrf_token")
        self.loop_back = knobs.pop("loop_back", False)  # the last page's next link leads back to page 1
        self.offsite_next = knobs.pop("offsite_next", False)  # page 2's next link leaves the site
        self.fail_times = knobs.pop("fail_times", 0)  # the next n catalog requests answer 503
        self.expire_as = knobs.pop("expire_as", "redirect")  # a dead session gets a 302 to /login, the login form itself, or a 401
        self.broken_session = knobs.pop("broken_session", False)  # the login works and no session is ever accepted afterwards
        self.offsite_redirect = knobs.pop("offsite_redirect", None)  # this catalog page redirects to another site
        self.redirect_on_bad_login = knobs.pop("redirect_on_bad_login", False)  # a wrong password answers 302 to /login, not 200
        self.login_post_challenge = knobs.pop("login_post_challenge", False)  # posting the login answers with a bot challenge
        assert not knobs, knobs
        self.sessions: dict[str, int] = {}  # sid -> catalog requests still allowed (-1: unlimited)
        self.csrf_tokens: set[str] = set()
        self.log: list[dict] = []
        self.catalog_requests = 0
        self.logins = 0
        self.lock = threading.Lock()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                site.handle(self, "GET")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode()
                site.handle(self, "POST", {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    # -- what a test reads -----------------------------------------------------------------------

    def paths(self, method=None):
        return [e["path"] + (f"?{e['query']}" if e["query"] else "") for e in self.log if method in (None, e["method"])]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def pages(self):
        return max(1, -(-len(self.products) // self.per_page))

    # -- the site --------------------------------------------------------------------------------

    def reply(self, h, status, body="", headers=None):
        data = body.encode() if isinstance(body, str) else body
        h.send_response(status)
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def handle(self, h, method, form=None):
        parts = urlparse(h.path)
        cookie = h.headers.get("Cookie") or ""
        sid = next((c.split("=", 1)[1] for c in (x.strip() for x in cookie.split(";")) if c.startswith("sid=")), "")
        with self.lock:
            self.log.append(
                {"method": method, "path": parts.path, "query": parts.query, "ua": h.headers.get("User-Agent"),
                 "via": h.headers.get("Via"), "sid": sid, "form_keys": sorted(form or {})}
            )
            if parts.path == "/robots.txt":
                if isinstance(self.robots, int):
                    return self.reply(h, self.robots)
                return self.reply(h, 200, self.robots)
            if parts.path == "/login":
                return self.login(h, method, form or {})
            if parts.path == "/catalog" or parts.path.startswith("/private/"):
                return self.catalog(h, parts, sid)
        self.reply(h, 404, "not found")

    def login(self, h, method, form):
        if self.login_form_challenge:
            return self.reply(h, 200, CHALLENGE_PAGE)
        if method == "GET":
            token = secrets.token_hex(8)
            self.csrf_tokens.add(token)
            hidden = f'<input type="hidden" name="{self.csrf_field}" value="{token}">' if self.csrf_field else ""
            page = (
                f'<html><body><form method="post" action="/login">{hidden}<input name="username">'
                '<input type="password" name="password"><button>Sign in</button></form></body></html>'
            )
            return self.reply(h, 200, page)
        if self.login_post_challenge:
            return self.reply(h, 200, CHALLENGE_PAGE)
        if self.csrf_field and form.get(self.csrf_field, "") not in self.csrf_tokens:
            return self.reply(h, 403, "csrf token missing or used")
        self.csrf_tokens.discard(form.get(self.csrf_field, ""))
        if (form.get("username") != self.user or form.get("password") != self.password) and self.redirect_on_bad_login:
            return self.reply(h, 302, "", {"Location": "/login"})
        if form.get("username") != self.user or form.get("password") != self.password:
            return self.reply(h, 200, '<html><body><p>Wrong login</p><form><input type="password" name="password"></form></body></html>')
        sid = secrets.token_hex(12)
        self.sessions[sid] = 0 if self.broken_session else (-1 if self.expire_after is None else self.expire_after)
        self.logins += 1
        self.reply(h, 302, "", {"Location": "/catalog?page=1", "Set-Cookie": f"sid={sid}; Path=/; HttpOnly"})

    def catalog(self, h, parts, sid):
        if parts.path.startswith("/private/"):
            return self.reply(h, 200, "<html><body>private</body></html>")
        if not self.no_login:
            left = self.sessions.get(sid)
            if left is None or left == 0:
                self.sessions.pop(sid, None)
                if self.expire_as == "form":
                    return self.reply(h, 200, '<html><body><form method="post" action="/login"><input type="password" name="password"></form></body></html>')
                if self.expire_as == "401":
                    return self.reply(h, 401, "login required")
                return self.reply(h, 302, "", {"Location": "/login"})
            if left > 0:
                self.sessions[sid] = left - 1
        self.catalog_requests += 1
        page = int((parse_qs(parts.query).get("page") or ["1"])[0])
        if self.throttle_forever is not None and self.catalog_requests > 1:
            return self.reply(h, 429, "slow down", {"Retry-After": str(self.throttle_forever)})
        if self.throttle and self.catalog_requests > self.throttle["after"] and self.throttle["times"] > 0:
            self.throttle["times"] -= 1
            return self.reply(h, 429, "slow down", {"Retry-After": str(self.throttle["retry_after"])})
        if self.fail_times > 0:
            self.fail_times -= 1
            return self.reply(h, 503, "try later")
        if page == self.offsite_redirect:
            return self.reply(h, 302, "", {"Location": "http://example.invalid/elsewhere"})
        if page == self.block_page:
            return self.reply(h, 403, "forbidden")
        if page == self.challenge_page:
            return self.reply(h, 200, CHALLENGE_PAGE)
        chunk = self.products[(page - 1) * self.per_page : page * self.per_page]
        rows = "".join("<tr>" + "".join(f"<td>{html_escape(str(p[c]))}</td>" for c in self.COLUMNS) + "</tr>" for p in chunk)
        head = "".join(f"<th>{c}</th>" for c in self.COLUMNS)
        nxt = f'<a rel="next" href="/catalog?page={page + 1}">Next</a>' if page < self.pages else ""
        if page == self.pages and self.loop_back:
            nxt = '<a rel="next" href="/catalog?page=1">Next</a>'
        if page == 2 and self.offsite_next:
            nxt = '<a rel="next" href="http://example.invalid/catalog?page=3">Next</a>'
        self.reply(h, 200, f'<html><body><table class="products"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>{nxt}</body></html>')


def shop_products(n, brand="Marlow"):
    return [{"Part No": f"MF-H{i:04d}A", "Brand": brand, "Description": f"Oil filter {i}", "Price": f"{i % 90 + 1}.00"} for i in range(1, n + 1)]


class ForwardProxy:
    """A tiny HTTP proxy on 127.0.0.1 that records every request it carried and tags it with a Via header."""

    def __init__(self):
        self.passed: list[tuple[str, str]] = []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def relay(self):
                target = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                proxy.passed.append((self.command, self.path))
                headers = {k: v for k, v in self.headers.items() if k.lower() not in ("proxy-connection", "connection", "host")}
                headers["Via"] = "1.1 camlobe-test-proxy"
                conn = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
                path = target.path + (f"?{target.query}" if target.query else "")
                conn.request(self.command, path, body=body, headers=headers)
                response = conn.getresponse()
                data = response.read()
                self.send_response(response.status)
                for k, v in response.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                conn.close()

            do_GET = do_POST = relay

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def dead_proxy_url() -> str:
    """The address of a proxy that is not there: a port that was open a moment ago and refuses connections now."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


class DroppingProxy:
    """A proxy address that accepts the connection and hangs up at once: a fast stand-in for a proxy that is down."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.accepted = 0
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            conn.close()

    def close(self):
        self._stop = True
        self.sock.close()


# ------------------------------------------------------------------------------------------------
# A session that serves pages from memory: for the cases a real socket makes slow or awkward (a body cut at any byte).
# ------------------------------------------------------------------------------------------------


class PageSession:
    """Stands in for requests.Session: `route(url)` answers (status, headers, body bytes). Every request is logged."""

    def __init__(self, route):
        import requests

        self.route, self.log = route, []
        self.headers: dict = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.trust_env = False

    def request(self, method, url, **kwargs):
        import requests

        self.log.append(url)
        status, headers, body = self.route(url)
        response = requests.Response()
        response.status_code = status
        response.url = url
        response._content = body.encode() if isinstance(body, str) else body
        response.headers.update(headers)
        response.encoding = None
        return response


def products_table(rows, header=("Part No", "Brand", "Description", "Price"), nxt="", head="", foot="", close=True):
    """A catalog page as a supplier's site would send it. `close=False` leaves out the closing </body></html>."""
    th = "".join(f"<th>{h}</th>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    end = "</body></html>" if close else ""
    return (
        f'<html><head><title>Catalog</title>{head}</head><body><table class="products"><thead><tr>{th}</tr></thead>'
        f"<tbody>{body}</tbody></table>{nxt}{foot}{end}"
    )
