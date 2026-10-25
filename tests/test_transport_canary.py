from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from reviewctl import cli
from reviewctl.transport_canary import CANARY_SOURCE_NAME, build_canary_report, canary_prompt


def test_canary_prompt_requires_the_synthetic_file_declaration() -> None:
    assert f'"reviewedFiles":["{CANARY_SOURCE_NAME}"]' in canary_prompt()


def test_canary_report_binds_the_receipt_and_profile() -> None:
    receipt = {
        "acceptedAttempt": 1,
        "result": "accepted",
        "sha256": "a" * 64,
    }

    report = build_canary_report(
        profile={
            "name": "llm",
            "path": "/tmp/config.toml",
            "sha256": "b" * 64,
            "settings": {"thinking": "minimal"},
        },
        routes=[{"transport": "llm", "model": "accepted"}],
        receipt_path=Path("/tmp/receipt.json"),
        receipt=receipt,
        exit_code=0,
    )

    assert report == {
        "acceptedAttempt": 1,
        "exitCode": 0,
        "kind": "transport-canary",
        "profile": {
            "name": "llm",
            "path": "/tmp/config.toml",
            "settings": {"thinking": "minimal"},
            "sha256": "b" * 64,
        },
        "receipt": {"path": "/tmp/receipt.json", "sha256": "a" * 64},
        "result": "accepted",
        "routes": [{"model": "accepted", "transport": "llm"}],
        "version": 1,
    }


def write_profile(path: Path) -> None:
    path.write_text('[profiles.accepted]\nroutes = ["llm:accepted"]\n')


def write_receipt(path: Path, review_id: str) -> None:
    receipt = {
        "acceptedAttempt": 1,
        "result": "accepted",
        "reviewId": review_id,
    }
    receipt["sha256"] = cli.sha256_bytes(cli.contract_canonical_json(receipt))
    path.write_text(json.dumps(receipt))


def test_transport_canary_runs_a_profile_and_writes_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.toml"
    artifacts = tmp_path / "artifacts"
    write_profile(config)

    def fake_run_review(parser: object, namespace: argparse.Namespace) -> int:
        assert namespace.response_contract == "findings-json"
        assert namespace.require_reviewed_files is True
        assert namespace.max_attempts == 1
        assert namespace.timeout_seconds == 13
        source = Path(namespace.files[0])
        assert source.name == CANARY_SOURCE_NAME
        turn = Path(namespace.artifact_root) / namespace.review_id / "turn"
        turn.mkdir(parents=True)
        write_receipt(turn / "receipt.json", namespace.review_id)
        return 0

    monkeypatch.setattr(cli, "run_review", fake_run_review)
    namespace = cli.build_parser().parse_args(
        [
            "transport-canary",
            "--profile",
            "accepted",
            "--config",
            str(config),
            "--artifact-root",
            str(artifacts),
            "--timeout-seconds",
            "13",
        ]
    )

    assert namespace.handler(namespace) == 0
    report_path = Path(capsys.readouterr().out.strip())
    report = json.loads(report_path.read_text())
    assert report["profile"]["name"] == "accepted"
    assert report["routes"] == [{"model": "accepted", "transport": "llm"}]
    assert Path(report["receipt"]["path"]).parent == report_path.parent
    assert report["receipt"]["sha256"] == cli.sha256_bytes(
        cli.contract_canonical_json(
            {"acceptedAttempt": 1, "result": "accepted", "reviewId": report_path.parent.parent.name}
        )
    )


@pytest.mark.parametrize("profile_limit", [None, 8192])
@pytest.mark.parametrize("default_limit", [None, 4096])
@pytest.mark.parametrize("explicit_limit", [None, 512, cli.DEFAULT_MAX_OUTPUT_TOKENS])
def test_transport_canary_propagates_reviewed_file_requirement_to_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    profile_limit: int | None,
    default_limit: int | None,
    explicit_limit: int | None,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[profiles.codex]\nroutes = ["codex:canary"]\n'
        + (f"max_output_tokens = {profile_limit}\n" if profile_limit is not None else "")
        + (
            f"[defaults.codex]\nmax_output_tokens = {default_limit}\n"
            if default_limit is not None
            else ""
        )
    )
    registry = cli.BackendRegistry()
    descriptor = cli.build_backend_registry().require("codex").descriptor
    captured: list[cli.BackendRequest] = []

    def execute(request: cli.BackendRequest) -> cli.BackendExecution:
        captured.append(request)
        return cli.BackendExecution(
            0,
            "",
            cli.PersistedResponse(
                "conversation",
                None,
                1,
                1,
                request.model,
                1,
                "openai-codex",
                json.dumps(
                    {
                        "verdict": "approved",
                        "findings": [],
                        "reviewedFiles": [CANARY_SOURCE_NAME],
                    }
                ),
            ),
            cli.BackendEvidence(),
        )

    registry.register(descriptor, execute)
    monkeypatch.setattr(cli, "build_backend_registry", lambda: registry)
    arguments = [
        "transport-canary",
        "--profile",
        "codex",
        "--config",
        str(config),
        "--artifact-root",
        str(tmp_path / "artifacts"),
    ]
    if explicit_limit is not None:
        arguments.extend(("--max-output-tokens", str(explicit_limit)))
    namespace = cli.build_parser().parse_args(arguments)

    assert namespace.handler(namespace) == 0
    assert captured[0].prepared_contract is not None
    assert captured[0].prepared_contract.review_declaration_required is True
    assert captured[0].max_output_tokens == (
        explicit_limit
        if explicit_limit is not None
        else profile_limit or default_limit or cli.DEFAULT_MAX_OUTPUT_TOKENS
    )
    report_path = Path(capsys.readouterr().out.splitlines()[-1])
    assert json.loads(report_path.read_text())["result"] == "accepted"


def test_transport_canary_does_not_write_report_without_one_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "config.toml"
    write_profile(config)
    monkeypatch.setattr(cli, "run_review", lambda parser, args: 1)
    namespace = cli.build_parser().parse_args(
        [
            "transport-canary",
            "--profile",
            "accepted",
            "--config",
            str(config),
            "--artifact-root",
            str(tmp_path / "artifacts"),
        ]
    )

    assert namespace.handler(namespace) == 1
    assert not list(tmp_path.glob("**/transport-canary.json"))


def test_transport_canary_rejects_unknown_profile_before_creating_artifacts(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    artifacts = tmp_path / "artifacts"
    write_profile(config)
    namespace = cli.build_parser().parse_args(
        [
            "transport-canary",
            "--profile",
            "missing",
            "--config",
            str(config),
            "--artifact-root",
            str(artifacts),
        ]
    )

    with pytest.raises(SystemExit):
        namespace.handler(namespace)
    assert not artifacts.exists()
