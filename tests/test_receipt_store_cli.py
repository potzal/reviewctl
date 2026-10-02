from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from reviewctl.cli import canonical_json, run_cli
from reviewctl.journal import ProjectJournal


def _project(path: Path, project_id="shared-project") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "reviewctl.toml").write_text(
        f'[project]\nid = "{project_id}"\n'
        '[evidence.potzal]\nendpoint = "http://127.0.0.1:8123"\n'
        'namespace = "reviews"\ntoken_env = "TEST_RECEIPT_STORE_TOKEN"\n'
    )
    return path


def _receipt(path: Path) -> bytes:
    value = {"reviewId": "fixture", "result": "unavailable"}
    value["sha256"] = hashlib.sha256(canonical_json(value)).hexdigest()
    contents = json.dumps(value, indent=2).encode() + b"\n"
    path.write_bytes(contents)
    return contents


def test_receipts_require_configured_store(tmp_path: Path, capsys) -> None:
    (tmp_path / "reviewctl.toml").write_text('[project]\nid = "project"\n')
    assert (
        run_cli(
            [
                "receipts",
                "pull",
                "sha256:" + "a" * 64,
                "--project",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 2
    )
    result = json.loads(capsys.readouterr().out)
    assert result["diagnostic"]["code"] == "config_invalid"
    assert result["diagnostic"]["next"]


def test_receipts_require_environment_credential(tmp_path: Path, monkeypatch, capsys) -> None:
    _project(tmp_path)
    monkeypatch.delenv("TEST_RECEIPT_STORE_TOKEN", raising=False)
    assert (
        run_cli(
            [
                "receipts",
                "pull",
                "sha256:" + "a" * 64,
                "--project",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 4
    )
    assert json.loads(capsys.readouterr().out)["diagnostic"]["code"] == "evidence_store_denied"


def test_receipts_copy_exact_bytes_between_checkouts(tmp_path: Path, monkeypatch, capsys) -> None:
    import reviewctl.receipt_store_cli as commands
    from reviewctl.receipt_store import StoredReceipt

    first = _project(tmp_path / "first")
    second = _project(tmp_path / "second")
    receipt = tmp_path / "receipt.json"
    contents = _receipt(receipt)
    object_digest = "sha256:" + "b" * 64
    namespace = "reviews/" + hashlib.sha256(b"shared-project").hexdigest()
    stored = StoredReceipt(
        project_id="shared-project",
        object_digest=object_digest,
        receipt_sha256=hashlib.sha256(contents).hexdigest(),
        receipt_bytes=contents,
        namespace=namespace,
        release="0.0.0+receipt." + "b" * 64,
    )

    class Store:
        def __init__(self, settings, project_id):
            assert project_id == stored.project_id

        def push(self, raw):
            assert raw == contents
            return stored

        def pull(self, digest):
            assert digest == object_digest
            return stored

    monkeypatch.setattr(commands, "PotzalReceiptStore", Store)
    args = ["receipts", "push", str(receipt), "--project", str(first), "--format", "json"]
    assert run_cli(args) == 0
    assert json.loads(capsys.readouterr().out)["objectDigest"] == object_digest
    for _ in range(2):
        assert (
            run_cli(
                [
                    "receipts",
                    "pull",
                    object_digest,
                    "--project",
                    str(second),
                    "--format",
                    "json",
                ]
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
        downloaded = Path(result["receipt"])
        assert downloaded.read_bytes() == contents
        assert os.stat(downloaded).st_mode & 0o777 == 0o600
    assert run_cli(["verify", str(downloaded)]) == 0
    capsys.readouterr()
    journal = ProjectJournal(second / ".reviewctl/journal.jsonl", prepare_for_writes=False)
    assert journal.verify() == []
    assert {event["type"] for event in journal.events()} == {"receipt_retrieved"}
    assert receipt.read_bytes() == contents


def test_receipts_push_refuses_missing_file_before_network(tmp_path, monkeypatch, capsys):
    _project(tmp_path)
    monkeypatch.setenv("TEST_RECEIPT_STORE_TOKEN", "local-canary-token")
    assert (
        run_cli(
            [
                "receipts",
                "push",
                str(tmp_path / "missing"),
                "--project",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 5
    )
    assert json.loads(capsys.readouterr().out)["diagnostic"]["code"] == "receipt_invalid"


def _fake_store(monkeypatch, contents: bytes):
    import reviewctl.receipt_store_cli as commands
    from reviewctl.receipt_store import StoredReceipt

    stored = StoredReceipt(
        project_id="shared-project",
        object_digest="sha256:" + "c" * 64,
        receipt_sha256=hashlib.sha256(contents).hexdigest(),
        receipt_bytes=contents,
        namespace="reviews/project",
        release="0.0.0+receipt." + "c" * 64,
    )

    class Store:
        def __init__(self, *_args):
            pass

        def pull(self, _digest):
            return stored

        def push(self, _raw):
            return stored

    monkeypatch.setattr(commands, "PotzalReceiptStore", Store)
    return commands, stored


def test_pull_preserves_conflicting_local_file(tmp_path, monkeypatch, capsys):
    _project(tmp_path)
    contents = _receipt(tmp_path / "original.json")
    _, stored = _fake_store(monkeypatch, contents)
    root = tmp_path / ".reviewctl/shared" / stored.object_digest.removeprefix("sha256:")
    root.mkdir(parents=True)
    target = root / "receipt.json"
    target.write_bytes(b"conflicting local evidence")
    assert run_cli(["receipts", "pull", stored.object_digest, "--project", str(tmp_path)]) == 5
    assert target.read_bytes() == b"conflicting local evidence"
    assert "existing bytes were preserved" in capsys.readouterr().err


def test_pull_rejects_symlinked_local_target(tmp_path, monkeypatch, capsys):
    _project(tmp_path)
    contents = _receipt(tmp_path / "original.json")
    _, stored = _fake_store(monkeypatch, contents)
    root = tmp_path / ".reviewctl/shared" / stored.object_digest.removeprefix("sha256:")
    root.mkdir(parents=True)
    target = tmp_path / "external.json"
    target.write_bytes(contents)
    (root / "receipt.json").symlink_to(target)
    assert (
        run_cli(
            [
                "receipts",
                "pull",
                stored.object_digest,
                "--project",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 3
    )
    assert target.read_bytes() == contents
    assert json.loads(capsys.readouterr().out)["diagnostic"]["code"] == "evidence_store_failed"


def test_corrupt_journal_stops_publication_before_network(tmp_path, monkeypatch, capsys):
    _project(tmp_path)
    commands, _ = _fake_store(monkeypatch, _receipt(tmp_path / "receipt.json"))
    (tmp_path / ".reviewctl").mkdir()
    (tmp_path / ".reviewctl/journal.jsonl").write_text("broken journal\n")
    monkeypatch.setattr(commands, "PotzalReceiptStore", lambda *_: pytest.fail("network reached"))
    assert (
        run_cli(
            [
                "receipts",
                "push",
                str(tmp_path / "receipt.json"),
                "--project",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 5
    )
    assert json.loads(capsys.readouterr().out)["diagnostic"]["code"] == "journal_corrupt"


def test_journal_failure_preserves_completed_remote_storage_status(tmp_path, monkeypatch, capsys):
    from reviewctl.errors import Diagnostic, JournalOperationError

    _project(tmp_path)
    _, stored = _fake_store(monkeypatch, _receipt(tmp_path / "receipt.json"))

    def failed_append(*_args, **_kwargs):
        raise JournalOperationError(Diagnostic("journal_unavailable", "write failed"))

    monkeypatch.setattr(ProjectJournal, "append", failed_append)
    assert (
        run_cli(
            [
                "receipts",
                "push",
                str(tmp_path / "receipt.json"),
                "--project",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 3
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "stored"
    assert result["objectDigest"] == stored.object_digest
    assert result["diagnostic"]["code"] == "journal_unavailable"
    assert result["diagnostic"]["next"]


def test_storage_success_text_and_failure_text_have_actionable_output(capsys):
    from reviewctl.receipt_store_cli import _output

    _output({"status": "stored", "objectDigest": "digest", "receipt": "/private/receipt"}, "text")
    assert "receipt: /private/receipt" in capsys.readouterr().out
    _output(
        {
            "status": "failed",
            "diagnostic": {
                "code": "evidence_store_denied",
                "message": "denied",
                "next": None,
            },
        },
        "text",
    )
    assert "evidence_store_denied" in capsys.readouterr().err


def test_storage_reports_preflight_journal_violations(tmp_path, monkeypatch, capsys):
    _project(tmp_path)
    monkeypatch.setattr(ProjectJournal, "verify", lambda _: ["fixture continuity violation"])
    assert (
        run_cli(
            [
                "receipts",
                "pull",
                "sha256:" + "a" * 64,
                "--project",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 5
    )
    result = json.loads(capsys.readouterr().out)
    assert result["diagnostic"]["code"] == "journal_corrupt"
    assert result["diagnostic"]["next"]
