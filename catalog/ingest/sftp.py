"""A CSV or XML file on an SFTP server, fetched whole and then read like any local file.

The host key is checked against a fingerprint the operator supplies, and an unknown or changed key
is refused: there is no "trust on first use" here, because a feed from the wrong machine is worse than
no feed. The download goes to a temporary file, its size must match what the server reported before
and after the transfer, and only then is it parsed. Credentials are never part of the source identity
that is stored with the run.
"""
from __future__ import annotations

import base64
import hashlib
import socket
import tempfile
import time
from pathlib import Path
from typing import Callable

import paramiko

from .adapters import FileAdapter, sha256_file


class SftpError(Exception):
    pass


class HostKeyMismatch(SftpError):
    pass


def key_fingerprint(key: paramiko.PKey) -> str:
    """The SHA256:... form that ssh-keygen -l prints."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class SftpAdapter:
    def __init__(
        self,
        host: str,
        remote_path: str,
        username: str,
        host_key_sha256: str,
        password: str | None = None,
        key_filename: str | None = None,
        port: int = 22,
        fmt: str | None = None,
        timeout: float = 15.0,
        attempts: int = 3,
        retry_delay: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not host_key_sha256.startswith("SHA256:"):
            raise ValueError("host_key_sha256 must look like SHA256:xxxx (ssh-keygen -l -E sha256)")
        if not (password or key_filename):
            raise ValueError("give a password or a key file")
        self.host, self.port, self.remote_path, self.username = host, port, remote_path, username
        self.expected_key = host_key_sha256
        self.password, self.key_filename = password, key_filename
        self.fmt = fmt or ("xml" if remote_path.lower().endswith(".xml") else "csv")
        self.timeout, self.attempts, self.retry_delay, self._sleep = timeout, attempts, retry_delay, sleep
        self.source_ref = f"sftp://{host}:{port}{remote_path}"  # no user, no secret
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._local: Path | None = None
        self._notes: dict = {}
        self._inner: FileAdapter | None = None

    def fetch(self) -> Path:
        if self._local is not None:
            return self._local
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                self._local = self._download()
                return self._local
            except (HostKeyMismatch, paramiko.AuthenticationException):
                raise  # retrying cannot fix either
            except FileNotFoundError as exc:
                raise SftpError(f"no such file on the server: {self.remote_path}") from exc
            except (OSError, EOFError, paramiko.SSHException, SftpError) as exc:
                last = exc
                self._notes["connect_attempts"] = attempt + 1
                if attempt + 1 < self.attempts:
                    self._sleep(self.retry_delay * 2**attempt)
        raise SftpError(f"could not fetch {self.source_ref} after {self.attempts} attempts: {last}")

    def _download(self) -> Path:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=self.timeout)
            got = key_fingerprint(transport.get_remote_server_key())
            if got != self.expected_key:
                raise HostKeyMismatch(f"host key {got} is not the expected {self.expected_key}")
            if self.key_filename:
                transport.auth_publickey(self.username, paramiko.PKey.from_path(self.key_filename))
            else:
                transport.auth_password(self.username, self.password)
            sftp = paramiko.SFTPClient.from_transport(transport)
            sftp.get_channel().settimeout(self.timeout)
            before = sftp.stat(self.remote_path)
            self._tmp = tempfile.TemporaryDirectory(prefix="camlobe-sftp-")
            target = Path(self._tmp.name) / Path(self.remote_path).name
            partial = target.with_suffix(target.suffix + ".part")
            sftp.get(self.remote_path, str(partial))
            after = sftp.stat(self.remote_path)
            size = partial.stat().st_size
            if size != before.st_size or after.st_size != before.st_size or after.st_mtime != before.st_mtime:
                raise SftpError(
                    f"transfer incomplete or the file changed while it was read: "
                    f"got {size} bytes, server said {before.st_size} then {after.st_size}"
                )
            partial.replace(target)
            self._notes.update({"remote_bytes": size, "host_key_verified": True})
            return target
        finally:
            transport.close()

    def secret_values(self) -> list[str]:
        return [self.password] if self.password else []

    def cleanup(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp, self._local = None, None

    def fingerprint(self) -> str:
        return sha256_file(self.fetch())

    def records(self, start_after: int = 0):
        self._inner = FileAdapter(self.fetch(), fmt=self.fmt, source_ref=self.source_ref)
        yield from self._inner.records(start_after)

    def notes(self) -> dict:
        return {**self._notes, **(self._inner.notes() if self._inner else {})}
