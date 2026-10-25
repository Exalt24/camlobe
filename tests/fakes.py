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
