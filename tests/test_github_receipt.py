from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import reviewctl.github_receipt as github_receipt
from reviewctl.api import ReviewClient, ReviewRequest, ReviewResult
from reviewctl.backends import BackendEvidence, BackendExecution, PersistedResponse
from reviewctl.github import ChangedFileSnapshot, PullRequestRef, PullRequestSnapshot
from reviewctl.github_receipt import (
    GitHubReceiptError,
    _load_json_bytes,
    _valid_usage,
    github_review_prompt,
    github_v2_findings,
    write_github_v2_receipt,
)


def test_load_json_bytes_returns_an_object() -> None:
    assert _load_json_bytes(b'{"key":"value"}', label="fixture") == {"key": "value"}


@pytest.mark.parametrize("contents", [b"not-json", b"[]", b'{"key":NaN}'])
def test_load_json_bytes_rejects_non_object_or_nonstandard_json(contents: bytes) -> None:
    with pytest.raises(GitHubReceiptError, match="fixture"):
        _load_json_bytes(contents, label="fixture")


@pytest.mark.parametrize(
    ("usage", "valid"),
    [
        ({"model": "model"}, True),
        ({"model": "other"}, False),
        ({"model": "model", "provider": "provider", "costUsd": 0.0}, True),
        ({"model": "model", "provider": ""}, False),
        ({"model": "model", "provider": 1}, False),
        ({"model": "model", "costUsd": -1}, False),
        ({"model": "model", "costUsd": float("inf")}, False),
        ({"model": "model", "durationMs": -1}, False),
        ({"model": "model", "inputTokens": True}, False),
        ({"model": "model", "outputTokens": 0}, True),
        (None, False),
    ],
)
def test_valid_usage_requires_safe_metadata(usage: object, valid: bool) -> None:
    assert _valid_usage(usage, expected_model="model") is valid


def test_github_v2_findings_rejects_missing_receipt(tmp_path) -> None:
    with pytest.raises(GitHubReceiptError, match="could not read promoted GitHub receipt"):
        github_v2_findings(tmp_path / "missing.json")


@pytest.mark.parametrize("contents", [b"not-json", b"{}"])
def test_github_v2_findings_rejects_noncanonical_receipt(tmp_path, contents: bytes) -> None:
    receipt = tmp_path / "github-review-receipt.json"
    receipt.write_bytes(contents)

    with pytest.raises(GitHubReceiptError, match="promoted GitHub receipt"):
        github_v2_findings(receipt)


def test_github_v2_findings_rejects_inconsistent_validated_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, result, snapshot = _accepted_promotion_inputs(tmp_path)
    receipt = write_github_v2_receipt(
        client=client,
        result=result,
        snapshot=snapshot,
        receipt_path=result.receipt_path,
        profile_name="default",
    )
    payload = json.loads(receipt.read_text())
    payload["findings"] = [{}]
    receipt.write_text(json.dumps(payload))
    monkeypatch.setattr(github_receipt, "validate_v2_receipt", lambda _receipt: ())

    with pytest.raises(GitHubReceiptError, match="findings are inconsistent"):
        github_v2_findings(receipt)


def _snapshot() -> PullRequestSnapshot:
    return PullRequestSnapshot(
        PullRequestRef("example/project", 1),
        "a" * 40,
        "b" * 40,
        "private",
        (ChangedFileSnapshot("app.py", "modified", "value = 2\n"),),
        "diff --git a/app.py b/app.py\n",
    )


class _ApprovedTransport:
    def execute(self, request):
        return BackendExecution(
            0,
            "",
            PersistedResponse(
                "test-conversation",
                0.0,
                1,
                1,
                request.model,
                1,
                "test-provider",
                '{"verdict":"approved","findings":[]}',
            ),
            BackendEvidence(),
        )


def _accepted_promotion_inputs(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:fake/model"]\n'
        'execution = "remote"\n'
    )
    source = tmp_path / "app.py"
    source.write_text("value = 2\n")
    snapshot = _snapshot()
    client = ReviewClient.from_project(tmp_path, transports={"pi": _ApprovedTransport()})
    result = client.review(
        ReviewRequest(
            prompt=github_review_prompt(snapshot),
            files=(source,),
            profile="default",
            review_id="promotion",
            source_context=snapshot.to_context(),
            source_names=("app.py",),
            source_root=tmp_path,
        )
    )
    assert result.status == "accepted"
    assert result.receipt_sha256 is not None
    return client, result, snapshot


def _resign_checkpoint(path: Path, result: ReviewResult, mutate) -> ReviewResult:
    checkpoint = json.loads(path.read_text())
    mutate(checkpoint)
    checkpoint.pop("sha256", None)
    unsigned = json.dumps(
        checkpoint, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    checkpoint["sha256"] = hashlib.sha256(unsigned).hexdigest()
    path.write_text(json.dumps(checkpoint, sort_keys=True, indent=2) + "\n")
    return replace(result, receipt_sha256=checkpoint["sha256"])


def test_promotion_writes_a_canonical_v2_receipt(tmp_path: Path) -> None:
    client, result, snapshot = _accepted_promotion_inputs(tmp_path)

    receipt = write_github_v2_receipt(
        client=client,
        result=result,
        snapshot=snapshot,
        receipt_path=result.receipt_path,
        profile_name="default",
    )

    assert github_v2_findings(receipt) == ()
    payload = _load_json_bytes(receipt.read_bytes(), label="receipt")
    assert payload["receiptSchemaVersion"] == 2


@pytest.mark.parametrize("field", ["configDigest", "projectId", "originId", "privacyMode"])
def test_promotion_rejects_changed_project_binding(tmp_path: Path, field: str) -> None:
    client, result, snapshot = _accepted_promotion_inputs(tmp_path)
    result = _resign_checkpoint(
        result.receipt_path, result, lambda checkpoint: checkpoint.__setitem__(field, "different")
    )

    with pytest.raises(GitHubReceiptError, match="configuration"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )


def test_promotion_rejects_findings_different_from_review_result(tmp_path: Path) -> None:
    client, result, snapshot = _accepted_promotion_inputs(tmp_path)
    result = _resign_checkpoint(
        result.receipt_path,
        result,
        lambda checkpoint: checkpoint["findings"].append({"title": "hidden"}),
    )

    with pytest.raises(GitHubReceiptError, match="checkpoint findings"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda checkpoint: checkpoint.__setitem__("reviewId", "different"), "findings-json"),
        (
            lambda checkpoint: checkpoint.__setitem__("attempts", []),
            "one accepted attempt",
        ),
        (
            lambda checkpoint: checkpoint["attempts"][0].__setitem__("attempt", 0),
            "attempt is inconsistent",
        ),
        (
            lambda checkpoint: checkpoint["attempts"][0].__setitem__("route", "pi:other/model"),
            "route does not match",
        ),
        (lambda checkpoint: checkpoint.__setitem__("packetDigest", "bad"), "packet digest"),
    ],
)
def test_promotion_rejects_inconsistent_checkpoint_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate, message: str
) -> None:
    client, result, snapshot = _accepted_promotion_inputs(tmp_path)
    result = _resign_checkpoint(result.receipt_path, result, mutate)
    monkeypatch.setattr(github_receipt, "verify_project_receipt", lambda *args, **kwargs: None)

    with pytest.raises(GitHubReceiptError, match=message):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )


def _write_packet(path: Path, packet: dict) -> bytes:
    contents = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(contents + b"\n")
    return contents


def test_promotion_rejects_unverifiable_result_or_inputs(tmp_path: Path) -> None:
    client, result, snapshot = _accepted_promotion_inputs(tmp_path)

    with pytest.raises(GitHubReceiptError, match="accepted review result"):
        write_github_v2_receipt(
            client=client,
            result=replace(result, receipt_sha256="a" * 64),
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )
    with pytest.raises(GitHubReceiptError, match="promotion inputs"):
        write_github_v2_receipt(
            client=None,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )


def test_promotion_preserves_checkpoint_parsing_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, result, snapshot = _accepted_promotion_inputs(tmp_path)
    monkeypatch.setattr(
        github_receipt,
        "_load_json_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(GitHubReceiptError("exact JSON")),
    )

    with pytest.raises(GitHubReceiptError, match="exact JSON"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )


def test_promotion_rejects_corrupt_packet_and_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, result, snapshot = _accepted_promotion_inputs(tmp_path)
    packet_path = result.receipt_path.parent / "packet.json"
    monkeypatch.setattr(github_receipt, "verify_project_receipt", lambda *args, **kwargs: None)

    packet_path.unlink()
    with pytest.raises(GitHubReceiptError, match="frozen packet"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )

    client, result, snapshot = _accepted_promotion_inputs(tmp_path / "unterminated")
    packet_path = result.receipt_path.parent / "packet.json"
    packet_path.write_bytes(packet_path.read_bytes().rstrip(b"\n"))
    monkeypatch.setattr(github_receipt, "verify_project_receipt", lambda *args, **kwargs: None)
    with pytest.raises(GitHubReceiptError, match="canonically terminated"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )


def test_promotion_rejects_packet_digest_snapshot_prompt_and_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, result, snapshot = _accepted_promotion_inputs(tmp_path)
    packet_path = result.receipt_path.parent / "packet.json"
    packet = json.loads(packet_path.read_text())
    packet_path.write_bytes(packet_path.read_bytes().rstrip(b"\n") + b"extra\n")
    monkeypatch.setattr(github_receipt, "verify_project_receipt", lambda *args, **kwargs: None)
    with pytest.raises(GitHubReceiptError, match="does not match the project checkpoint"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )

    client, result, snapshot = _accepted_promotion_inputs(tmp_path / "snapshot")
    monkeypatch.setattr(github_receipt, "verify_project_receipt", lambda *args, **kwargs: None)
    other = replace(snapshot, head_sha="c" * 40)
    with pytest.raises(GitHubReceiptError, match="GitHub snapshot"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=other,
            receipt_path=result.receipt_path,
            profile_name="default",
        )

    client, result, snapshot = _accepted_promotion_inputs(tmp_path / "prompt")
    monkeypatch.setattr(github_receipt, "github_review_prompt", lambda _snapshot: "different")
    with pytest.raises(GitHubReceiptError, match="prompt"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )
    monkeypatch.undo()

    client, result, snapshot = _accepted_promotion_inputs(tmp_path / "contract")
    packet_path = result.receipt_path.parent / "packet.json"
    packet = json.loads(packet_path.read_text())
    packet["contractDigest"] = "0" * 64
    contents = _write_packet(packet_path, packet)
    result = _resign_checkpoint(
        result.receipt_path,
        result,
        lambda checkpoint: checkpoint.__setitem__(
            "packetDigest", hashlib.sha256(contents).hexdigest()
        ),
    )
    with pytest.raises(GitHubReceiptError, match="contract"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )


def test_promotion_rejects_bad_response_usage_or_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, result, snapshot = _accepted_promotion_inputs(tmp_path)
    response_path = result.receipt_path.parent / "attempt-01" / "response.md"
    response_path.unlink()
    monkeypatch.setattr(github_receipt, "verify_project_receipt", lambda *args, **kwargs: None)
    with pytest.raises(GitHubReceiptError, match="accepted response"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )

    client, result, snapshot = _accepted_promotion_inputs(tmp_path / "binding")
    result = _resign_checkpoint(
        result.receipt_path,
        result,
        lambda checkpoint: checkpoint["acceptedResponse"].__setitem__("characters", 0),
    )
    monkeypatch.setattr(github_receipt, "verify_project_receipt", lambda *args, **kwargs: None)
    with pytest.raises(GitHubReceiptError, match="binding"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )

    client, result, snapshot = _accepted_promotion_inputs(tmp_path / "response")
    response_path = result.receipt_path.parent / "attempt-01" / "response.md"
    response_bytes = b"{}"
    response_path.write_bytes(response_bytes)
    result = _resign_checkpoint(
        result.receipt_path,
        result,
        lambda checkpoint: checkpoint.__setitem__(
            "acceptedResponse",
            {"sha256": hashlib.sha256(response_bytes).hexdigest(), "characters": 2},
        ),
    )
    with pytest.raises(GitHubReceiptError, match="complete findings-json"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )

    client, result, snapshot = _accepted_promotion_inputs(tmp_path / "usage")
    result = _resign_checkpoint(
        result.receipt_path,
        result,
        lambda checkpoint: checkpoint["usage"].__setitem__("model", "wrong/model"),
    )
    with pytest.raises(GitHubReceiptError, match="usage"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )

    client, result, snapshot = _accepted_promotion_inputs(tmp_path / "invalid-v2")
    monkeypatch.setattr(github_receipt, "validate_v2_receipt", lambda _receipt: ("broken",))
    with pytest.raises(GitHubReceiptError, match="V2 validation"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )


def test_promotion_marks_kiro_receipts_and_wraps_persistence_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, result, snapshot = _accepted_promotion_inputs(tmp_path)
    profile = replace(client.config.profile("default"), routes=("kiro:model",))
    kiro_client = SimpleNamespace(
        config=replace(client.config, profiles={"default": profile}), journal=client.journal
    )
    result = _resign_checkpoint(
        result.receipt_path,
        result,
        lambda checkpoint: (
            checkpoint.__setitem__("route", "kiro:model"),
            checkpoint["attempts"][0].__setitem__("route", "kiro:model"),
            checkpoint["usage"].__setitem__("model", "model"),
        ),
    )
    receipt = write_github_v2_receipt(
        client=kiro_client,
        result=result,
        snapshot=snapshot,
        receipt_path=result.receipt_path,
        profile_name="default",
    )
    payload = _load_json_bytes(receipt.read_bytes(), label="kiro receipt")
    assert payload["extension.kiroUnresolvedIdentityWaiver"] is True
    assert payload["extension.mergeGateEligible"] is False

    client, result, snapshot = _accepted_promotion_inputs(tmp_path / "persistence")
    monkeypatch.setattr(
        github_receipt.ArtifactStore,
        "write_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(GitHubReceiptError, match="persist"):
        write_github_v2_receipt(
            client=client,
            result=result,
            snapshot=snapshot,
            receipt_path=result.receipt_path,
            profile_name="default",
        )


def test_promotion_requires_accepted_result_with_digest(tmp_path) -> None:
    result = ReviewResult("timeout", "review", tmp_path / "receipt.json", ())

    with pytest.raises(GitHubReceiptError, match="only an accepted review"):
        write_github_v2_receipt(
            client=None,
            result=result,
            snapshot=_snapshot(),
            receipt_path=result.receipt_path,
            profile_name="default",
        )


def test_promotion_requires_the_result_checkpoint_path(tmp_path) -> None:
    result = ReviewResult(
        "accepted", "review", tmp_path / "receipt.json", (), receipt_sha256="a" * 64
    )

    with pytest.raises(GitHubReceiptError, match="path does not match"):
        write_github_v2_receipt(
            client=None,
            result=result,
            snapshot=_snapshot(),
            receipt_path=tmp_path / "other.json",
            profile_name="default",
        )
