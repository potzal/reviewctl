"""Bounded native Potzal storage of exact, locally verified receipt bytes."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from reviewctl.config import PROJECT_ID, PotzalSettings
from reviewctl.contracts import canonical_json, exact_json_object
from reviewctl.errors import ConfigError, Diagnostic, ReviewctlError

MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_ENVELOPE_KEYS = {"artifactKind", "schemaVersion", "projectId", "receiptSha256", "receiptBase64"}


class ReceiptStoreError(ReviewctlError):
    """A storage failure with a safe, actionable diagnostic."""

    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class StoredReceipt:
    """Storage identity, not release-catalog or review-approval evidence."""

    project_id: str
    object_digest: str
    receipt_sha256: str
    receipt_bytes: bytes
    namespace: str
    release: str


def _failure(code: str) -> ReceiptStoreError:
    message, next_action = {
        "receipt_invalid": (
            "receipt content or project binding is invalid",
            "verify the canonical receipt, exact project ID, and requested object digest",
        ),
        "evidence_store_denied": (
            "receipt store credentials are missing, invalid, or denied",
            "set a valid bearer token in the configured environment variable and check grants",
        ),
        "evidence_store_missing": (
            "receipt object or store route was not found",
            "check the configured store and object digest; publish the receipt if absent",
        ),
        "evidence_store_unavailable": (
            "receipt store transport is unavailable",
            "check connectivity and retry; a previous upload may already have stored the object",
        ),
        "evidence_store_failed": (
            "receipt store configuration or protocol failed validation",
            "check store settings and native object API compatibility, then retry",
        ),
    }[code]
    return ReceiptStoreError(
        Diagnostic(code, message, retryable=code == "evidence_store_unavailable", next=next_action)
    )


def _verify_receipt(raw: bytes) -> None:
    # Import lazily: the CLI also registers commands that import this adapter.
    from reviewctl.cli import receipt_bytes_violations

    if not isinstance(raw, bytes) or len(raw) > MAX_RECEIPT_BYTES:
        raise _failure("receipt_invalid")
    try:
        violations = receipt_bytes_violations(raw)
    except ValueError, TypeError, RecursionError:
        raise _failure("receipt_invalid") from None
    if violations:
        raise _failure("receipt_invalid")


def _reject_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _failure("evidence_store_unavailable")
    return remaining


class _Deadline:
    """Cancel active I/O, never delegate credential-bearing work to a background thread.

    getaddrinfo remains synchronous and cannot be cancelled portably. Its return
    is checked before creating any socket, so delayed DNS cannot start a late upload.
    """

    def __init__(self, end: float):
        self.end = end
        self.lock = threading.Lock()
        self.sockets: list[tuple[socket.socket, socket.socket]] = []
        self.timer = threading.Timer(_remaining(end), self._expire)
        self.timer.name = "reviewctl-receipt-deadline"

    def __enter__(self):
        self.timer.start()
        return self

    def _expire(self):
        with self.lock:
            for original, interrupt in self.sockets:
                try:
                    # The duplicate still addresses the connection after TLS has
                    # detached the original socket or HTTP has created a file reader.
                    interrupt.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # A failed connect or already closed peer is harmless.
                original.close()

    def __exit__(self, *exc):
        self.timer.cancel()
        self.timer.join()
        for original, interrupt in self.sockets:
            interrupt.close()
            original.close()
        _remaining(self.end)

    def _watch(self, sock):
        with self.lock:
            _remaining(self.end)
            self.sockets.append((sock, sock.dup()))

    def connect(self, address, timeout, source_address):
        _remaining(self.end)
        addresses = socket.getaddrinfo(*address, type=socket.SOCK_STREAM)
        _remaining(self.end)
        for family, kind, protocol, _, target in addresses:
            _remaining(self.end)
            sock = socket.socket(family, kind, protocol)
            try:
                self._watch(sock)
                sock.settimeout(_remaining(self.end))
                sock.connect(target)
                _remaining(self.end)
                return sock
            except OSError:
                sock.close()
                _remaining(self.end)
            except BaseException:
                sock.close()
                raise
        raise OSError("receipt store connection failed")


class _DeadlineHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    def __init__(self, deadline: _Deadline):
        urllib.request.HTTPHandler.__init__(self)
        self.deadline = deadline

    def _connection(self, host, timeout, *, secure=False):
        connection_type = http.client.HTTPSConnection if secure else http.client.HTTPConnection
        connection = connection_type(host, timeout=timeout)
        connection._create_connection = self.deadline.connect
        return connection

    def http_open(self, request):
        return self.do_open(self._connection, request)

    def https_open(self, request):
        return self.do_open(self._connection, request, secure=True)


class PotzalReceiptStore:
    """Opt-in byte adapter using explicit credentials and no HTTP redirects."""

    def __init__(self, settings: PotzalSettings, project_id: str, *, token: str | None = None):
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            raise _failure("receipt_invalid")
        if settings.timeout_seconds is None:
            raise _failure("evidence_store_failed")
        try:
            self.settings = PotzalSettings(
                settings.endpoint, settings.namespace, settings.token_env, settings.timeout_seconds
            )
        except ConfigError:
            raise _failure("evidence_store_failed") from None
        credential = os.environ.get(self.settings.token_env) if token is None else token
        if not isinstance(credential, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", credential):
            raise _failure("evidence_store_denied")
        self._token = credential
        self.project_id = project_id
        self.namespace = (
            self.settings.namespace + "/" + hashlib.sha256(project_id.encode()).hexdigest()
        )

    def _stored(self, digest: str, raw: bytes) -> StoredReceipt:
        return StoredReceipt(
            self.project_id,
            digest,
            hashlib.sha256(raw).hexdigest(),
            raw,
            self.namespace,
            "0.0.0+receipt." + digest.removeprefix("sha256:"),
        )

    def push(self, raw: bytes) -> StoredReceipt:
        """Verify then publish; reconcile conflicts only by exact CAS byte identity."""
        deadline = time.monotonic() + self.settings.timeout_seconds
        _verify_receipt(raw)
        artifact = canonical_json(
            {
                "artifactKind": "reviewctl-receipt-artifact",
                "schemaVersion": 1,
                "projectId": self.project_id,
                "receiptSha256": hashlib.sha256(raw).hexdigest(),
                "receiptBase64": base64.b64encode(raw).decode("ascii"),
            }
        )
        if len(artifact) > MAX_ARTIFACT_BYTES:
            raise _failure("receipt_invalid")
        digest = "sha256:" + hashlib.sha256(artifact).hexdigest()
        stored = self._stored(digest, raw)
        status, _ = self._request("POST", digest, artifact, deadline=deadline)
        if status == 409:
            _, existing = self._request("GET", digest, deadline=deadline)
            if existing != artifact:
                raise _failure("evidence_store_failed")
        _remaining(deadline)
        return stored

    def pull(self, digest: str) -> StoredReceipt:
        """Verify object integrity, envelope, project binding, and canonical receipt."""
        deadline = time.monotonic() + self.settings.timeout_seconds
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise _failure("receipt_invalid")
        _, artifact = self._request("GET", digest, deadline=deadline)
        if "sha256:" + hashlib.sha256(artifact).hexdigest() != digest:
            raise _failure("receipt_invalid")
        try:
            payload = json.loads(
                artifact.decode("utf-8"),
                object_pairs_hook=exact_json_object,
                parse_constant=_reject_constant,
            )
            if (
                not isinstance(payload, dict)
                or payload.keys() != _ENVELOPE_KEYS
                or payload["artifactKind"] != "reviewctl-receipt-artifact"
                or type(payload["schemaVersion"]) is not int
                or payload["schemaVersion"] != 1
                or payload["projectId"] != self.project_id
                or not isinstance(payload["receiptSha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", payload["receiptSha256"])
                or not isinstance(payload["receiptBase64"], str)
            ):
                raise _failure("receipt_invalid")
            raw = base64.b64decode(payload["receiptBase64"], validate=True)
            if (
                base64.b64encode(raw).decode("ascii") != payload["receiptBase64"]
                or hashlib.sha256(raw).hexdigest() != payload["receiptSha256"]
            ):
                raise _failure("receipt_invalid")
        except ValueError, RecursionError:
            raise _failure("receipt_invalid") from None
        _verify_receipt(raw)
        _remaining(deadline)
        return self._stored(digest, raw)

    def _request(
        self, method: str, digest: str, artifact: bytes | None = None, *, deadline: float
    ) -> tuple[int, bytes]:
        headers = {"Authorization": "Bearer " + self._token}
        path = "/v1/objects"
        if method == "POST":
            headers.update(
                {
                    "Digest": digest,
                    "Potzal-Namespace": self.namespace,
                    "Potzal-Release": "0.0.0+receipt." + digest.removeprefix("sha256:"),
                    "Content-Type": "application/octet-stream",
                }
            )
        else:
            path += "/" + digest
        try:
            request = urllib.request.Request(
                self.settings.endpoint + path, data=artifact, headers=headers, method=method
            )
            with _Deadline(deadline) as operation:
                opener = urllib.request.build_opener(_NoRedirect(), _DeadlineHandler(operation))
                try:
                    response = opener.open(request, timeout=_remaining(deadline))
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    _remaining(deadline)
                    status = response.status
                    if status not in ({201, 409} if method == "POST" else {200}):
                        code = {
                            401: "evidence_store_denied",
                            403: "evidence_store_denied",
                            404: "evidence_store_missing",
                        }.get(status, "evidence_store_failed")
                        raise _failure(code)
                    return status, self._read_artifact(response) if method == "GET" else b""
        except OSError, urllib.error.URLError:
            raise _failure("evidence_store_unavailable") from None
        except ValueError, http.client.HTTPException:
            raise _failure("evidence_store_failed") from None

    @staticmethod
    def _read_artifact(response) -> bytes:
        length = response.headers.get("Content-Length")
        if length is not None:
            if not re.fullmatch(r"[0-9]+", length) or int(length) > MAX_ARTIFACT_BYTES:
                raise _failure("evidence_store_failed")
            length = int(length)
        chunks = []
        size = 0
        while True:
            chunk = response.read(min(65536, MAX_ARTIFACT_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_ARTIFACT_BYTES:
                raise _failure("evidence_store_failed")
            chunks.append(chunk)
        if length is not None and length != size:
            raise _failure("evidence_store_failed")
        return b"".join(chunks)
