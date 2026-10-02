"""Byte adapter contracts; all HTTP traffic is confined to a local test server."""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from reviewctl.contracts import canonical_json


def receipt_bytes() -> bytes:
    unsigned = {"status": "accepted", "note": "exact historical bytes"}
    signed = {**unsigned, "sha256": hashlib.sha256(canonical_json(unsigned)).hexdigest()}
    return json.dumps(signed, indent=2).encode() + b"\n"


@pytest.fixture
def server():
    state = SimpleNamespace(objects={}, requests=[], status=None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state.requests.append((self.command, self.path, dict(self.headers), body))
            digest = self.headers["Digest"]
            status = state.status or (409 if digest in state.objects else 201)
            if status == 201:
                state.objects[digest] = body
            self.send_response(status)
            if status in (301, 302, 303, 307, 308):
                self.send_header("Location", state.endpoint + "/redirect-target")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            state.requests.append((self.command, self.path, dict(self.headers), b""))
            body = state.objects.get(self.path.removeprefix("/v1/objects/"))
            status = state.status or (200 if body is not None else 404)
            self.send_response(status)
            if status in (301, 302, 303, 307, 308):
                self.send_header("Location", state.endpoint + "/redirect-target")
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    state.endpoint = f"http://127.0.0.1:{httpd.server_port}"
    yield state
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def settings(endpoint: str):
    return SimpleNamespace(
        endpoint=endpoint, namespace="reviews", token_env="RECEIPT_TEST_TOKEN", timeout_seconds=1
    )


def test_roundtrip_preserves_exact_receipt_bytes_and_project_mapping(server):
    from reviewctl.receipt_store import PotzalReceiptStore, StoredReceipt

    raw = receipt_bytes()
    store = PotzalReceiptStore(settings(server.endpoint), "project-one", token="local-test-token")
    stored = store.push(raw)
    assert isinstance(stored, StoredReceipt)
    assert stored.receipt_bytes == raw
    assert stored.receipt_sha256 == hashlib.sha256(raw).hexdigest()
    assert stored.namespace == "reviews/" + hashlib.sha256(b"project-one").hexdigest()
    assert stored.release == "0.0.0+receipt." + stored.object_digest.removeprefix("sha256:")
    assert store.pull(stored.object_digest) == stored
    assert (
        PotzalReceiptStore(settings(server.endpoint), "project-one", token="local-test-token").push(
            raw
        )
        == stored
    )
    other = PotzalReceiptStore(settings(server.endpoint), "project-two", token="local-test-token")
    assert other.push(raw).object_digest != stored.object_digest
    assert other.pull(other.push(raw).object_digest).receipt_bytes == raw
    method, path, headers, artifact = server.requests[0]
    assert (method, path) == ("POST", "/v1/objects")
    assert headers["Authorization"] == "Bearer local-test-token"
    assert headers["Digest"] == stored.object_digest
    assert headers["Potzal-Namespace"] == stored.namespace
    assert headers["Potzal-Release"] == stored.release
    assert headers["Content-Type"] == "application/octet-stream"
    expected = envelope(raw)
    assert artifact == canonical_json(expected)
    assert stored.object_digest == object_digest(artifact)


def envelope(raw: bytes, project_id="project-one") -> dict:
    return {
        "artifactKind": "reviewctl-receipt-artifact",
        "schemaVersion": 1,
        "projectId": project_id,
        "receiptSha256": hashlib.sha256(raw).hexdigest(),
        "receiptBase64": base64.b64encode(raw).decode("ascii"),
    }


def object_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def make_store(endpoint="http://127.0.0.1:1", project_id="project-one", **kwargs):
    from reviewctl.receipt_store import PotzalReceiptStore

    return PotzalReceiptStore(settings(endpoint), project_id, token="local-test-token", **kwargs)


def assert_error(call, code: str, *, retryable=False):
    from reviewctl.errors import Diagnostic
    from reviewctl.receipt_store import ReceiptStoreError

    with pytest.raises(ReceiptStoreError) as caught:
        call()
    diagnostic = caught.value.diagnostic
    assert isinstance(diagnostic, Diagnostic)
    assert diagnostic.code == code
    assert diagnostic.retryable is retryable
    assert diagnostic.next
    serialized = json.dumps(diagnostic.to_dict()) + str(caught.value)
    for secret in ("local-test-token", "private-remote-body", "http://", "https://"):
        assert secret not in serialized
    return diagnostic


class Response(io.BytesIO):
    def __init__(self, body=b"", status=200, headers=None, chunk_size=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}
        self.read_sizes = []
        self.chunk_size = chunk_size

    def read(self, size=-1):
        assert size > 0, "remote reads must always be explicitly bounded"
        self.read_sizes.append(size)
        return super().read(min(size, self.chunk_size) if self.chunk_size else size)


@pytest.fixture
def fake_http(monkeypatch):
    calls = []
    outcomes = []

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request, timeout))
            assert outcomes, "unexpected HTTP request"
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    return SimpleNamespace(calls=calls, outcomes=outcomes)


def test_environment_token_and_explicit_token_override(monkeypatch, fake_http):
    from reviewctl.receipt_store import PotzalReceiptStore

    monkeypatch.setenv("RECEIPT_TEST_TOKEN", "environment-token")
    fake_http.outcomes.extend([Response(status=201), Response(status=201)])
    PotzalReceiptStore(settings("http://127.0.0.1:1"), "project-one").push(receipt_bytes())
    make_store().push(receipt_bytes())
    assert fake_http.calls[0][0].get_header("Authorization") == "Bearer environment-token"
    assert fake_http.calls[1][0].get_header("Authorization") == "Bearer local-test-token"
    assert all(timeout == 1 for _, timeout in fake_http.calls)


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        " ",
        "a b",
        "a\nb",
        "a\rb",
        "a\x00b",
        "a\x7fb",
        "a\tb",
        "é",
        "a:b",
        "=abc",
        "ab=c",
        7,
        False,
    ],
)
def test_rejects_absent_or_invalid_bearer_without_network(monkeypatch, fake_http, token):
    from reviewctl.receipt_store import PotzalReceiptStore

    monkeypatch.delenv("RECEIPT_TEST_TOKEN", raising=False)
    assert_error(
        lambda: PotzalReceiptStore(settings("http://127.0.0.1:1"), "project-one", token=token),
        "evidence_store_denied",
    )
    assert not fake_http.calls


def test_accepts_bearer_alphabet(fake_http):
    from reviewctl.receipt_store import PotzalReceiptStore

    fake_http.outcomes.append(Response(status=201))
    PotzalReceiptStore(settings("http://127.0.0.1:1"), "project-one", token="aZ09-._~+/==").push(
        receipt_bytes()
    )


@pytest.mark.parametrize(
    "project_id", ["", "x\n", "../x", "a/b", " a", "a ", "é", "-x", None, 1, True]
)
def test_rejects_inexact_project_id(fake_http, project_id):
    assert_error(lambda: make_store(project_id=project_id), "receipt_invalid")
    assert not fake_http.calls


@pytest.mark.parametrize("project_id", ["a", "A0_.-", "project.one-2_3"])
def test_valid_project_ids_are_not_normalized(fake_http, project_id):
    fake_http.outcomes.append(Response(status=201))
    stored = make_store(project_id=project_id).push(receipt_bytes())
    assert stored.project_id == project_id
    assert stored.namespace == "reviews/" + hashlib.sha256(project_id.encode()).hexdigest()


@pytest.mark.parametrize("timeout", [0, -1, True, None, float("inf"), float("nan"), "30"])
def test_rejects_nonfinite_or_invalid_timeout(timeout, fake_http):
    from reviewctl.receipt_store import PotzalReceiptStore

    configured = settings("http://127.0.0.1:1")
    configured.timeout_seconds = timeout
    assert_error(
        lambda: PotzalReceiptStore(configured, "project-one", token="local-test-token"),
        "evidence_store_failed",
    )
    assert not fake_http.calls


INVALID_RECEIPTS = [
    b"{}",
    b"[]",
    b"null",
    b"{",
    b"\xff",
    b'{"x":NaN}',
    b'{"x":Infinity}',
    b'{"x":-Infinity}',
    b'{"x":1,"x":2}',
    b'{"receiptSchemaVersion":true}',
    b'{"receiptSchemaVersion":3}',
    b'{"receiptSchemaVersion":2}',
    b'{"artifactKind":"project-review-checkpoint"}',
    b'{"projectCheckpointSchemaVersion":1}',
    b'{"sha256":"' + b"0" * 64 + b'"}',
]


@pytest.mark.parametrize("raw", INVALID_RECEIPTS + ["not-bytes", bytearray(b"{}"), None])
def test_push_rejects_invalid_receipt_before_network(fake_http, raw):
    store = make_store()
    assert_error(lambda: store.push(raw), "receipt_invalid")
    assert not fake_http.calls


def test_push_rejects_signed_checkpoint_and_disqualified_legacy_transport(fake_http):
    store = make_store()
    for unsigned in ({"artifactKind": "project-review-checkpoint"}, {"transport": "kiro"}):
        signed = {**unsigned, "sha256": hashlib.sha256(canonical_json(unsigned)).hexdigest()}
        assert_error(lambda signed=signed: store.push(canonical_json(signed)), "receipt_invalid")
    assert not fake_http.calls


def test_receipt_exact_size_limit_and_overflow(fake_http):
    from reviewctl.receipt_store import MAX_RECEIPT_BYTES

    raw = receipt_bytes()
    at_limit = raw + b" " * (MAX_RECEIPT_BYTES - len(raw))
    fake_http.outcomes.append(Response(status=201))
    store = make_store()
    assert store.push(at_limit).receipt_bytes == at_limit
    assert_error(lambda: store.push(at_limit + b" "), "receipt_invalid")
    assert len(fake_http.calls) == 1


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "sha256:",
        "a" * 64,
        "sha256:" + "A" * 64,
        "sha256:" + "g" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 64 + "\n",
        "sha256:../x",
        None,
        1,
    ],
)
def test_pull_rejects_malformed_digest_without_network(fake_http, digest):
    store = make_store()
    assert_error(lambda: store.pull(digest), "receipt_invalid")
    assert not fake_http.calls


@pytest.mark.parametrize("raw", INVALID_RECEIPTS)
def test_pull_rejects_invalid_inner_receipt_even_with_correct_digests(fake_http, raw):
    artifact = canonical_json(envelope(raw))
    fake_http.outcomes.append(Response(artifact))
    assert_error(lambda: make_store().pull(object_digest(artifact)), "receipt_invalid")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifactKind", "other"),
        ("artifactKind", 1),
        ("schemaVersion", True),
        ("schemaVersion", 1.0),
        ("schemaVersion", "1"),
        ("schemaVersion", 2),
        ("projectId", "project-two"),
        ("projectId", "project-one\n"),
        ("projectId", None),
        ("receiptSha256", "0" * 64),
        ("receiptSha256", "A" * 64),
        ("receiptSha256", 1),
        ("receiptBase64", None),
        ("receiptBase64", "%%%"),
        ("receiptBase64", "e30=\n"),
        ("receiptBase64", "e31="),
        ("receiptBase64", "é"),
        ("extra", "field"),
    ],
)
def test_pull_rejects_strict_envelope_violations(fake_http, field, value):
    payload = envelope(receipt_bytes())
    payload[field] = value
    artifact = canonical_json(payload)
    fake_http.outcomes.append(Response(artifact))
    assert_error(lambda: make_store().pull(object_digest(artifact)), "receipt_invalid")


@pytest.mark.parametrize("field", list(envelope(b"{}")))
def test_pull_rejects_missing_envelope_field(fake_http, field):
    payload = envelope(receipt_bytes())
    del payload[field]
    artifact = canonical_json(payload)
    fake_http.outcomes.append(Response(artifact))
    assert_error(lambda: make_store().pull(object_digest(artifact)), "receipt_invalid")


@pytest.mark.parametrize(
    "artifact",
    [
        b"{",
        b"[]",
        b"null",
        b"\xff",
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":1,"x":2}',
        b"[" * 1500 + b"]" * 1500,
    ],
)
def test_pull_rejects_hostile_outer_json(fake_http, artifact):
    fake_http.outcomes.append(Response(artifact))
    assert_error(lambda: make_store().pull(object_digest(artifact)), "receipt_invalid")


def test_pull_rejects_corrupt_object_digest(fake_http):
    fake_http.outcomes.append(Response(canonical_json(envelope(receipt_bytes()))))
    assert_error(lambda: make_store().pull("sha256:" + "0" * 64), "receipt_invalid")


def test_pull_rejects_decoded_receipt_over_limit(fake_http):
    from reviewctl.receipt_store import MAX_RECEIPT_BYTES

    artifact = canonical_json(envelope(b" " * (MAX_RECEIPT_BYTES + 1)))
    fake_http.outcomes.append(Response(artifact))
    assert_error(lambda: make_store().pull(object_digest(artifact)), "receipt_invalid")


def test_artifact_limit_applies_on_publish(monkeypatch, fake_http):
    import reviewctl.receipt_store as adapter

    monkeypatch.setattr(adapter, "MAX_ARTIFACT_BYTES", 1)
    assert_error(lambda: make_store().push(receipt_bytes()), "receipt_invalid")
    assert not fake_http.calls


def test_receipt_size_checked_after_download_and_global_verifier_used(monkeypatch, fake_http):
    import reviewctl.cli as cli

    raw = receipt_bytes()
    artifact = canonical_json(envelope(raw))
    fake_http.outcomes.extend([Response(status=201), Response(artifact)])
    observed = []

    def verify(raw):
        observed.append(raw)
        return ()

    monkeypatch.setattr(cli, "receipt_bytes_violations", verify)
    store = make_store()
    store.push(raw)
    store.pull(object_digest(artifact))
    assert observed == [raw, raw]


def test_conflict_requires_exact_existing_artifact_bytes(fake_http):
    artifact = canonical_json(envelope(receipt_bytes()))
    fake_http.outcomes.extend([Response(status=409), Response(artifact + b"\n")])
    assert_error(lambda: make_store().push(receipt_bytes()), "evidence_store_failed")
    assert [request.method for request, _ in fake_http.calls] == ["POST", "GET"]


@pytest.mark.parametrize("method", ["push", "pull"])
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "evidence_store_denied"),
        (403, "evidence_store_denied"),
        (404, "evidence_store_missing"),
        (400, "evidence_store_failed"),
        (429, "evidence_store_failed"),
        (500, "evidence_store_failed"),
        (503, "evidence_store_failed"),
        (204, "evidence_store_failed"),
        (302, "evidence_store_failed"),
    ],
)
@pytest.mark.parametrize("raise_http_error", [False, True])
def test_http_status_mapping_never_reads_error_bodies(
    fake_http, method, status, code, raise_http_error
):
    response = Response(b"private-remote-body", status=status)
    outcome = urllib.error.HTTPError(
        "https://private.invalid/secret", status, "private-remote-body", {}, response
    )
    fake_http.outcomes.append(outcome if raise_http_error else response)
    store = make_store()
    argument = receipt_bytes() if method == "push" else "sha256:" + "0" * 64
    assert_error(lambda: getattr(store, method)(argument), code)
    assert response.closed
    assert not response.read_sizes


@pytest.mark.parametrize("method", ["push", "pull"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_real_redirects_are_not_followed(server, method, status):
    server.status = status
    store = make_store(server.endpoint)
    argument = receipt_bytes() if method == "push" else "sha256:" + "0" * 64
    assert_error(lambda: getattr(store, method)(argument), "evidence_store_failed")
    assert len(server.requests) == 1
    assert server.requests[0][1] != "/redirect-target"


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("private-remote-body"),
        urllib.error.URLError("https://private.invalid/secret"),
        ConnectionError("local-test-token"),
        OSError("private-remote-body"),
    ],
)
def test_unavailable_transport_is_safe_and_retryable(fake_http, error):
    fake_http.outcomes.append(error)
    assert_error(
        lambda: make_store().push(receipt_bytes()), "evidence_store_unavailable", retryable=True
    )


@pytest.mark.parametrize(
    "error",
    [
        ValueError("private-remote-body"),
        http.client.BadStatusLine("https://private.invalid/secret"),
        http.client.IncompleteRead(b"private-remote-body", 100),
    ],
)
def test_protocol_failures_are_safe(fake_http, error):
    fake_http.outcomes.append(error)
    assert_error(lambda: make_store().pull("sha256:" + "0" * 64), "evidence_store_failed")


def test_download_reads_incrementally_with_finite_bound(fake_http):
    artifact = canonical_json(envelope(receipt_bytes()))
    response = Response(artifact, chunk_size=7)
    fake_http.outcomes.append(response)
    assert make_store().pull(object_digest(artifact)).receipt_bytes == receipt_bytes()
    assert response.closed
    assert len(response.read_sizes) > 1
    assert all(size <= 65536 for size in response.read_sizes)


def test_download_size_bound_without_content_length(fake_http):
    from reviewctl.receipt_store import MAX_ARTIFACT_BYTES

    response = Response(b"x" * (MAX_ARTIFACT_BYTES + 2))
    fake_http.outcomes.append(response)
    assert_error(lambda: make_store().pull("sha256:" + "0" * 64), "evidence_store_failed")
    assert sum(response.read_sizes) == MAX_ARTIFACT_BYTES + 1
    assert response.closed


@pytest.mark.parametrize("length", ["-1", "nonsense", "8388609"])
def test_download_rejects_bad_or_oversized_content_length_without_read(fake_http, length):
    response = Response(b"private-remote-body", headers={"Content-Length": length})
    fake_http.outcomes.append(response)
    assert_error(lambda: make_store().pull("sha256:" + "0" * 64), "evidence_store_failed")
    assert not response.read_sizes
    assert response.closed


def test_download_content_length_must_match_actual_bytes(fake_http):
    response = Response(b"short", headers={"Content-Length": "100"})
    fake_http.outcomes.append(response)
    assert_error(lambda: make_store().pull(object_digest(b"short")), "evidence_store_failed")


def test_stored_receipt_is_immutable(server):
    stored = make_store(server.endpoint).push(receipt_bytes())
    assert replace(stored, project_id="project-two") != stored
    with pytest.raises(AttributeError):
        stored.project_id = "project-two"


@pytest.mark.parametrize("method", ["push", "pull"])
def test_deeply_nested_receipt_is_a_safe_content_failure(fake_http, method):
    from reviewctl.cli import receipt_bytes_violations

    # Python 3.14's C JSON decoder can exceed the Python recursion limit.
    # Stay below the byte bound while exceeding its native stack guard as well.
    depth = 100_000
    raw = b"[" * depth + b"]" * depth
    with pytest.raises(RecursionError):
        receipt_bytes_violations(raw)
    argument = raw
    if method == "pull":
        artifact = canonical_json(envelope(raw))
        fake_http.outcomes.append(Response(artifact))
        argument = object_digest(artifact)
    assert_error(lambda: getattr(make_store(), method)(argument), "receipt_invalid")
    assert len(fake_http.calls) == (method == "pull")


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (TimeoutError("local-test-token"), "evidence_store_unavailable", True),
        (OSError("private-remote-body"), "evidence_store_unavailable", True),
        (http.client.IncompleteRead(b"private-remote-body"), "evidence_store_failed", False),
    ],
)
def test_download_failure_closes_stream_and_sanitizes_diagnostic(fake_http, error, code, retryable):
    class BrokenResponse(Response):
        def read(self, size=-1):
            raise error

    response = BrokenResponse()
    fake_http.outcomes.append(response)
    assert_error(lambda: make_store().pull("sha256:" + "0" * 64), code, retryable=retryable)
    assert response.closed


@pytest.mark.parametrize(("method", "status"), [("push", 200), ("pull", 201), ("pull", 409)])
def test_only_method_specific_success_statuses_are_accepted(fake_http, method, status):
    response = Response(b"private-remote-body", status=status)
    fake_http.outcomes.append(response)
    argument = receipt_bytes() if method == "push" else "sha256:" + "0" * 64
    assert_error(lambda: getattr(make_store(), method)(argument), "evidence_store_failed")
    assert response.closed
    assert not response.read_sizes


@pytest.mark.parametrize(
    ("status", "code"), [(401, "evidence_store_denied"), (404, "evidence_store_missing")]
)
def test_conflict_is_not_success_without_read_authorization_and_existing_object(
    fake_http, status, code
):
    fake_http.outcomes.extend([Response(status=409), Response(status=status)])
    assert_error(lambda: make_store().push(receipt_bytes()), code)
    assert [request.method for request, _ in fake_http.calls] == ["POST", "GET"]


def test_artifact_exact_download_limit_is_accepted(fake_http):
    from reviewctl.receipt_store import MAX_ARTIFACT_BYTES

    raw = receipt_bytes()
    artifact = canonical_json(envelope(raw))
    artifact += b" " * (MAX_ARTIFACT_BYTES - len(artifact))
    response = Response(artifact, headers={"Content-Length": str(len(artifact))})
    fake_http.outcomes.append(response)
    assert make_store().pull(object_digest(artifact)).receipt_bytes == raw
    assert response.closed


def test_explicit_invalid_token_does_not_fall_back_to_valid_environment(monkeypatch, fake_http):
    from reviewctl.receipt_store import PotzalReceiptStore

    monkeypatch.setenv("RECEIPT_TEST_TOKEN", "environment-token")
    assert_error(
        lambda: PotzalReceiptStore(settings("http://127.0.0.1:1"), "project-one", token=""),
        "evidence_store_denied",
    )
    assert not fake_http.calls


@pytest.fixture
def canonical_v2_receipt(tmp_path):
    from test_github_receipt import _accepted_promotion_inputs

    from reviewctl.cli import receipt_bytes_violations
    from reviewctl.github_receipt import write_github_v2_receipt

    # Existing synthetic transport: no GitHub or external model/provider calls.
    client, result, snapshot = _accepted_promotion_inputs(tmp_path)
    path = write_github_v2_receipt(
        client=client,
        result=result,
        snapshot=snapshot,
        receipt_path=result.receipt_path,
        profile_name="default",
    )
    raw = path.read_bytes()
    assert json.loads(raw)["receiptSchemaVersion"] == 2
    assert receipt_bytes_violations(raw) == ()
    return raw


def test_canonical_v2_roundtrip_preserves_exact_bytes(server, canonical_v2_receipt):
    store = make_store(server.endpoint)
    stored = store.push(canonical_v2_receipt)
    assert stored.receipt_bytes == canonical_v2_receipt
    assert stored.receipt_sha256 == hashlib.sha256(canonical_v2_receipt).hexdigest()
    assert store.pull(stored.object_digest) == stored
    assert store.push(canonical_v2_receipt) == stored


@pytest.mark.parametrize("method", ["push", "pull"])
def test_invalid_v2_rejected_even_with_recomputed_digests(fake_http, canonical_v2_receipt, method):
    from reviewctl.cli import canonical_json as receipt_canonical_json
    from reviewctl.cli import receipt_bytes_violations, valid_receipt

    payload = json.loads(canonical_v2_receipt)
    payload["attempts"] = []
    payload.pop("sha256")
    payload["sha256"] = hashlib.sha256(receipt_canonical_json(payload)).hexdigest()
    raw = receipt_canonical_json(payload)
    assert valid_receipt(payload)  # Digest-only V1 acceptance must not bypass V2 structure.
    assert receipt_bytes_violations(raw)
    argument = raw
    if method == "pull":
        artifact = canonical_json(envelope(raw))
        fake_http.outcomes.append(Response(artifact))
        argument = object_digest(artifact)
    assert_error(lambda: getattr(make_store(), method)(argument), "receipt_invalid")
    assert len(fake_http.calls) == (method == "pull")
