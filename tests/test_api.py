from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import reviewctl.api as api_module
from reviewctl.api import (
    MAX_SOURCE_BYTES,
    Finding,
    ReviewClient,
    ReviewRequest,
    finding_id,
    verify_project_receipt,
)
from reviewctl.backends import BackendEvidence, BackendExecution, PersistedResponse
from reviewctl.codex_project_transport import CodexProjectTransport
from reviewctl.contracts import (
    ContractContext,
    ContractFragment,
    EvaluationStatus,
    FragmentKind,
    get_contract,
)
from reviewctl.errors import JournalOperationError
from reviewctl.pi_transport import PiTransport


class FakeTransport:
    def __init__(self, response: str | None = None) -> None:
        self.response = response or '{"verdict":"approved","findings":[]}'

    def execute(self, request):
        return BackendExecution(
            exit_code=0,
            diagnostic="",
            response=PersistedResponse(
                conversation_id="fake-1",
                cost_usd=0.0,
                duration_ms=1,
                input_tokens=1,
                model=request.model,
                output_tokens=1,
                provider="fake",
                response=self.response,
            ),
            evidence=BackendEvidence(),
        )


class QueueTransport:
    def __init__(self, responses: list[str | None]) -> None:
        self.responses = list(responses)
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if response is None:
            return BackendExecution(
                exit_code=1,
                diagnostic="provider unavailable",
                response=None,
                evidence=BackendEvidence(),
            )
        return BackendExecution(
            exit_code=0,
            diagnostic="",
            response=PersistedResponse(
                conversation_id=f"fake-{len(self.requests)}",
                cost_usd=0.0,
                duration_ms=1,
                input_tokens=1,
                model=request.model,
                output_tokens=1,
                provider="fake",
                response=response,
            ),
            evidence=BackendEvidence(),
        )


def write_default_config(project: Path) -> None:
    (project / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:fake/model"]\n'
        'execution = "remote"\n'
    )


def test_client_from_project_does_not_create_a_missing_project(tmp_path: Path) -> None:
    missing = tmp_path / "mistyped"

    with pytest.raises(ValueError, match="existing project directory"):
        ReviewClient.from_project(missing)

    assert not missing.exists()


def test_client_rejects_an_unexpected_project_identity(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    config = api_module.load_config(tmp_path)

    with pytest.raises(ValueError, match="project directory identity changed"):
        ReviewClient(
            tmp_path,
            config,
            {"pi": FakeTransport()},
            expected_project_identity=(-1, -1),
        )


def test_client_from_project_rejects_an_unsafe_project_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsafe_project(*args: object, **kwargs: object):
        raise PermissionError("unsafe project")

    monkeypatch.setattr(api_module, "confined_directory_descriptor", unsafe_project)

    with pytest.raises(ValueError, match="safe project directory"):
        ReviewClient.from_project(tmp_path)


def test_client_from_project_rejects_project_directory_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    displaced = tmp_path / "displaced"
    project.mkdir()
    write_default_config(project)
    real_load_config = api_module.load_config

    def load_then_replace(path: Path, *args: object, **kwargs: object):
        config = real_load_config(path, *args, **kwargs)
        project.rename(displaced)
        project.mkdir()
        (project / "reviewctl.toml").write_text(
            '[project]\nprivacy_mode = "sensitive"\n[profiles.default]\nroutes = []\n'
        )
        return config

    monkeypatch.setattr(api_module, "load_config", load_then_replace)

    with pytest.raises(JournalOperationError, match="unsafe"):
        ReviewClient.from_project(project, transports={"pi": FakeTransport()})

    assert not (project / ".reviewctl").exists()


def test_client_from_project_rejects_replacement_before_journal_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    displaced = tmp_path / "displaced"
    project.mkdir()
    write_default_config(project)
    real_project_journal = api_module.ProjectJournal

    def replace_then_construct(path: Path, *args: object, **kwargs: object):
        project.rename(displaced)
        project.mkdir()
        return real_project_journal(path, *args, **kwargs)

    monkeypatch.setattr(api_module, "ProjectJournal", replace_then_construct)

    with pytest.raises(JournalOperationError, match="journal"):
        ReviewClient.from_project(project, transports={"pi": FakeTransport()})

    assert not (project / ".reviewctl").exists()


def test_client_review_returns_typed_result_and_journal(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    (tmp_path / "a.py").write_text("value = 1\n")
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review", files=(tmp_path / "a.py",)))

    assert result.status == "accepted"
    assert result.findings == ()
    assert result.receipt_path.is_file()
    assert [event["type"] for event in client.journal().events()] == [
        "review_started",
        "review_attempt",
        "review_finished",
    ]


def test_client_review_accepts_a_registered_openrouter_profile(tmp_path: Path) -> None:
    # A project profile may route to openrouter for formal review as long as the
    # openrouter transport is registered (github review / CI advisory path).
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["openrouter:deepseek/deepseek-v4.1"]\n'
        'execution = "remote"\n'
    )
    (tmp_path / "a.py").write_text("value = 1\n")
    approved = '{"verdict":"approved","findings":[],"reviewedFiles":["a.py"]}'
    client = ReviewClient.from_project(
        tmp_path,
        transports={"openrouter": FakeTransport(approved)},
    )

    result = client.review(ReviewRequest(prompt="review", files=(tmp_path / "a.py",)))

    # Accepted, not route_invalid — the openrouter route is honored.
    assert result.status == "accepted"


def test_client_review_rejects_openrouter_profile_when_transport_absent(tmp_path: Path) -> None:
    # If the openrouter transport is not registered, a formal openrouter route is
    # rejected — the injection is gated on registration (fail-closed).
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["openrouter:deepseek/deepseek-v4.1"]\n'
        'execution = "remote"\n'
    )
    (tmp_path / "a.py").write_text("value = 1\n")
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review", files=(tmp_path / "a.py",)))

    assert result.status == "route_invalid"


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        ("exception", "transport_unavailable"),
        ("failed", "transport_unavailable"),
        ("empty", "empty_response"),
        ("identity", "transport_unavailable"),
        ("partial", "partial"),
        ("contract", "contract_failed"),
        ("accepted", "accepted"),
    ],
)
def test_client_journals_each_registered_attempt_outcome(
    tmp_path: Path, outcome: str, expected_status: str
) -> None:
    write_default_config(tmp_path)

    class OutcomeTransport:
        def execute(self, request):
            if outcome == "exception":
                raise ValueError("transport exploded")
            if outcome == "failed":
                return BackendExecution(1, "provider failed", None, BackendEvidence())
            response = {
                "empty": "",
                "identity": '{"verdict":"approved","findings":[]}',
                "partial": (
                    '{"findings":[{"severity":"high","path":"a.py","line":1,'
                    '"title":"title","evidence":"e","reproduction":"r"}]}'
                ),
                "contract": "not-json",
                "accepted": '{"verdict":"approved","findings":[]}',
            }[outcome]
            conversation_id = "" if outcome == "identity" else "conversation"
            return BackendExecution(
                0,
                "",
                PersistedResponse(
                    conversation_id,
                    0.0,
                    1,
                    1,
                    request.model if outcome != "identity" else "other/model",
                    1,
                    "fake",
                    response,
                ),
                BackendEvidence(),
            )

    client = ReviewClient.from_project(tmp_path, transports={"pi": OutcomeTransport()})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == expected_status
    assert result.receipt_path.is_file()
    attempt_events = [
        event for event in client.journal().events() if event["type"] == "review_attempt"
    ]
    assert len(attempt_events) == 1
    assert attempt_events[0]["attempt"] == 1
    assert attempt_events[0]["route"] == "pi:fake/model"
    assert attempt_events[0]["status"] == expected_status


def test_client_rejects_private_remote_pi_read_only_profile(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:openrouter/model"]\n'
        'execution = "remote"\n'
        'tools = "read-only"\n'
    )
    calls = []

    class ForbiddenTransport:
        def execute(self, request):
            calls.append(request)
            raise AssertionError("unsafe private remote read-only transport was invoked")

    client = ReviewClient.from_project(tmp_path, transports={"pi": ForbiddenTransport()})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "privacy_denied"
    assert result.diagnostic is not None
    assert "read-only" in result.diagnostic.message
    assert calls == []


def test_client_injects_contract_instructions_into_transport_prompt(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    transport = QueueTransport(['{"verdict":"approved","findings":[]}'])
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    client.review(ReviewRequest(prompt="review"))

    assert "Return only JSON matching the supplied schema" in transport.requests[0].prompt


def test_client_forwards_profile_thinking_to_transport(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:fake/model"]\n'
        'execution = "remote"\n'
        'thinking = "max"\n'
    )
    transport = QueueTransport(['{"verdict":"approved","findings":[]}'])
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "accepted"
    assert transport.requests[0].thinking == "max"


def test_client_rejects_review_id_path_traversal(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review", review_id="../../escape"))

    assert result.status == "invalid_request"
    assert not (tmp_path.parent / "escape").exists()


def test_client_uses_max_attempts_as_bounded_per_route_retries(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:first/model"]\n'
        "max_attempts = 2\n"
        'execution = "remote"\n'
    )
    transport = QueueTransport([None, '{"verdict":"approved","findings":[]}'])
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "accepted"
    assert len(transport.requests) == 2
    receipt = json.loads(result.receipt_path.read_text())
    assert [attempt["status"] for attempt in receipt["attempts"]] == [
        "transport_unavailable",
        "accepted",
    ]


def test_accepted_checkpoint_binds_profile_and_exact_persisted_response(
    tmp_path: Path,
) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.security]\n"
        'routes = ["pi:fake/model"]\n'
        'execution = "remote"\n'
    )
    response = (
        '{"verdict":"changes-requested","findings":[{"severity":"high",'
        '"path":"app.py","line":1,"title":"\u00a1listo!","evidence":"e",'
        '"reproduction":"r"}]}'
    )
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport(response)})

    result = client.review(ReviewRequest(prompt="review", profile="security"))

    assert result.status == "accepted"
    response_bytes = response.encode("utf-8")
    assert (result.receipt_path.parent / "attempt-01" / "response.md").read_bytes() == (
        response_bytes
    )
    receipt = json.loads(result.receipt_path.read_text())
    assert receipt["projectCheckpointSchemaVersion"] == 2
    assert receipt["profile"] == "security"
    assert receipt["acceptedResponse"] == {
        "sha256": hashlib.sha256(response_bytes).hexdigest(),
        "characters": len(response),
    }


@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        (None, "transport_unavailable"),
        ("not-json", "contract_failed"),
        (
            '{"findings":[{"severity":"high","path":"app.py","line":1,'
            '"title":"Handle failure","evidence":"e","reproduction":"r"}]}',
            "partial",
        ),
    ],
)
def test_nonaccepted_checkpoint_omits_accepted_response(
    tmp_path: Path, response: str | None, expected_status: str
) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": QueueTransport([response])})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == expected_status
    assert "acceptedResponse" not in json.loads(result.receipt_path.read_text())


def test_v2_nonaccepted_checkpoint_rejects_null_accepted_response(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": QueueTransport([None])})
    result = client.review(ReviewRequest(prompt="review"))
    receipt = json.loads(result.receipt_path.read_text())
    receipt["acceptedResponse"] = None
    _resign_project_checkpoint(result.receipt_path, receipt)

    diagnostic = verify_project_receipt(result.receipt_path)

    assert diagnostic is not None
    assert diagnostic.code == "receipt_invalid"


def test_client_falls_back_and_keeps_valid_findings_from_partial_attempt(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:first/model", "pi:second/model"]\n'
        'execution = "remote"\n'
    )
    partial = (
        '{"findings":[{"severity":"high","path":"app.py","line":1,'
        '"title":"Handle failure","evidence":"e","reproduction":"r"}]}'
    )
    complete = '{"verdict":"approved","findings":[]}'
    transport = QueueTransport([partial, complete])
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "accepted"
    assert [finding.title for finding in result.findings] == ["Handle failure"]
    assert len(transport.requests) == 2
    receipt = json.loads(result.receipt_path.read_text())
    assert [attempt["status"] for attempt in receipt["attempts"]] == ["partial", "accepted"]
    assert receipt["fallbackRelationships"][0]["from"] == "pi:first/model"


def test_partial_receipt_binds_usage_to_the_attempt_that_produced_it(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:first/model", "pi:second/model"]\n'
        'execution = "remote"\n'
    )
    partial = (
        '{"findings":[{"severity":"high","path":"app.py","line":1,'
        '"title":"Handle failure","evidence":"e","reproduction":"r"}]}'
    )
    transport = QueueTransport([partial, None])
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "partial"
    receipt = json.loads(result.receipt_path.read_text())
    assert [attempt["route"] for attempt in receipt["attempts"]] == [
        "pi:first/model",
        "pi:second/model",
    ]
    assert receipt["route"] == "pi:first/model"
    assert receipt["usage"]["model"] == "first/model"


def test_project_receipt_verification_detects_tampering(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    result = client.review(ReviewRequest(prompt="review"))

    assert verify_project_receipt(result.receipt_path) is None
    assert result.receipt_sha256 is not None
    assert (
        verify_project_receipt(result.receipt_path, expected_sha256=result.receipt_sha256) is None
    )
    mismatched = verify_project_receipt(result.receipt_path, expected_sha256="another-review")
    assert mismatched is not None
    assert mismatched.code == "receipt_invalid"
    receipt = json.loads(result.receipt_path.read_text())
    assert receipt["artifactKind"] == "project-review-checkpoint"
    assert receipt["projectCheckpointSchemaVersion"] == 2
    receipt["status"] = "tampered"
    result.receipt_path.write_text(json.dumps(receipt))
    diagnostic = verify_project_receipt(result.receipt_path)
    assert diagnostic is not None
    assert diagnostic.code == "receipt_invalid"


def _resign_project_checkpoint(path: Path, receipt: dict[str, object]) -> None:
    receipt.pop("sha256", None)
    receipt["sha256"] = api_module._digest(
        json.dumps(
            receipt,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    )
    path.write_text(json.dumps(receipt))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda receipt: receipt.pop("profile", None),
        lambda receipt: receipt.__setitem__("profile", ""),
        lambda receipt: receipt.pop("acceptedResponse", None),
        lambda receipt: receipt.setdefault("acceptedResponse", {}).__setitem__(
            "path", "response.md"
        ),
        lambda receipt: receipt.setdefault("acceptedResponse", {}).__setitem__("sha256", "0" * 63),
        lambda receipt: receipt.setdefault("acceptedResponse", {}).__setitem__("characters", True),
    ],
)
def test_v2_accepted_checkpoint_rejects_malformed_response_binding(
    tmp_path: Path, mutation
) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    result = client.review(ReviewRequest(prompt="review"))
    receipt = json.loads(result.receipt_path.read_text())
    mutation(receipt)
    _resign_project_checkpoint(result.receipt_path, receipt)

    diagnostic = verify_project_receipt(result.receipt_path)

    assert diagnostic is not None


@pytest.mark.parametrize(
    ("mutation", "remove_response"),
    [
        (lambda receipt: receipt.__setitem__("attempts", {}), False),
        (lambda receipt: receipt.__setitem__("attempts", []), False),
        (
            lambda receipt: receipt["attempts"][0].__setitem__("attempt", 0),
            False,
        ),
        (lambda receipt: receipt, True),
    ],
)
def test_v2_accepted_checkpoint_rejects_unusable_attempt_or_response(
    tmp_path: Path, mutation, remove_response: bool
) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    result = client.review(ReviewRequest(prompt="review"))
    receipt = json.loads(result.receipt_path.read_text())
    mutation(receipt)
    _resign_project_checkpoint(result.receipt_path, receipt)
    if remove_response:
        (result.receipt_path.parent / "attempt-01" / "response.md").unlink()

    diagnostic = verify_project_receipt(result.receipt_path)

    assert diagnostic is not None
    assert diagnostic.code == "receipt_invalid"
    assert diagnostic.code == "receipt_invalid"


def test_v2_accepted_checkpoint_rejects_altered_contract_valid_response(
    tmp_path: Path,
) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    result = client.review(ReviewRequest(prompt="review"))
    replacement = '{"findings":[],"verdict":"approved"}'
    contract = get_contract("findings-json")
    context = ContractContext(file_names=())
    prepared = contract.prepare(context)
    assert contract.evaluate(replacement, prepared, context).status is EvaluationStatus.COMPLETE

    (result.receipt_path.parent / "attempt-01" / "response.md").write_text(replacement)
    diagnostic = verify_project_receipt(result.receipt_path)

    assert diagnostic is not None
    assert diagnostic.code == "receipt_invalid"


def test_v2_accepted_checkpoint_rejects_a_different_expected_profile(
    tmp_path: Path,
) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    result = client.review(ReviewRequest(prompt="review"))

    diagnostic = verify_project_receipt(result.receipt_path, expected_profile="security")

    assert diagnostic is not None
    assert diagnostic.code == "receipt_invalid"


def test_v1_accepted_checkpoint_retains_historical_digest_only_verification(
    tmp_path: Path,
) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    result = client.review(ReviewRequest(prompt="review"))
    receipt = json.loads(result.receipt_path.read_text())
    receipt["projectCheckpointSchemaVersion"] = 1
    receipt.pop("profile", None)
    receipt.pop("acceptedResponse", None)
    _resign_project_checkpoint(result.receipt_path, receipt)

    assert verify_project_receipt(result.receipt_path) is None


def test_project_receipt_verifier_rejects_a_mutated_marker_but_accepts_legacy(
    tmp_path: Path,
) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    result = client.review(ReviewRequest(prompt="review"))
    receipt = json.loads(result.receipt_path.read_text())

    receipt["artifactKind"] = "canonical-review-receipt"
    receipt.pop("sha256")
    receipt["sha256"] = api_module._digest(
        json.dumps(
            receipt,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    )
    result.receipt_path.write_text(json.dumps(receipt))
    marker_diagnostic = verify_project_receipt(result.receipt_path)

    assert marker_diagnostic is not None
    assert marker_diagnostic.code == "receipt_invalid"

    receipt.pop("artifactKind")
    receipt.pop("projectCheckpointSchemaVersion")
    receipt.pop("sha256")
    receipt["sha256"] = api_module._digest(
        json.dumps(
            receipt,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    )
    result.receipt_path.write_text(json.dumps(receipt))

    assert verify_project_receipt(result.receipt_path) is None


def test_project_receipt_verification_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    unsigned = {"status": "accepted"}
    digest = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        '{"status":"tampered","status":"accepted","sha256":' + json.dumps(digest) + "}"
    )

    diagnostic = verify_project_receipt(receipt)

    assert diagnostic is not None
    assert diagnostic.code == "receipt_invalid"


def test_project_receipt_verification_rejects_a_fifo_without_blocking(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    os.mkfifo(receipt)
    script = (
        "from pathlib import Path; "
        "from reviewctl.api import verify_project_receipt; "
        f"diagnostic = verify_project_receipt(Path({str(receipt)!r})); "
        "assert diagnostic is not None and diagnostic.code == 'receipt_invalid'"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=1,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_project_receipt_verification_rejects_nonfinite_json_number(
    tmp_path: Path, number: str
) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text(f'{{"sha256":"digest","usage":{{"costUsd":{number}}}}}')

    diagnostic = verify_project_receipt(receipt)

    assert diagnostic is not None
    assert diagnostic.code == "receipt_invalid"
    assert "could not read" in diagnostic.message


def test_client_rejects_nonfinite_transport_usage_before_receipt(tmp_path: Path) -> None:
    write_default_config(tmp_path)

    class NonfiniteUsageTransport:
        def execute(self, request):
            return BackendExecution(
                0,
                "",
                PersistedResponse(
                    "conversation",
                    float("nan"),
                    1,
                    1,
                    request.model,
                    1,
                    "fake",
                    '{"verdict":"approved","findings":[]}',
                ),
                BackendEvidence(),
            )

    client = ReviewClient.from_project(tmp_path, transports={"pi": NonfiniteUsageTransport()})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "transport_unavailable"
    assert b"NaN" not in result.receipt_path.read_bytes()
    assert verify_project_receipt(result.receipt_path) is None


def test_client_maps_malformed_findings_payload_to_contract_failure(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(
        tmp_path,
        transports={"pi": FakeTransport(response="not-json")},
    )

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "contract_failed"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "contract_failed"


def test_client_does_not_require_cli_parser(tmp_path: Path) -> None:
    write_default_config(tmp_path)

    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    assert client.config.project.privacy_mode == "private"


def test_client_rejects_oversized_source_before_reading_it(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    oversized = tmp_path / "oversized.txt"
    with oversized.open("wb") as stream:
        stream.truncate(MAX_SOURCE_BYTES + 1)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review", files=(oversized,)))

    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "exceeds" in result.diagnostic.message


def test_review_records_sorted_dimensions_and_unresolved_coverage(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review", dimensions=("security", "architecture")))

    assert result.status == "accepted"
    receipt = json.loads(result.receipt_path.read_text())
    assert receipt["dimensionSchemaVersion"] == 1
    assert receipt["dimensions"] == ["architecture", "correctness", "security"]
    assert receipt["dimensionCoverage"] == {
        "requested": ["architecture", "correctness", "security"],
        "observed": [],
        "unresolved": ["architecture", "correctness", "security"],
    }
    started = client.journal().events()[0]
    assert started["dimensions"] == ["architecture", "correctness", "security"]


def test_review_rejects_duplicate_request_dimensions(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review", dimensions=("security", "security")))

    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "duplicate" in result.diagnostic.message


def test_review_receipt_is_json_and_records_route(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review"))
    receipt = json.loads(result.receipt_path.read_text())

    assert receipt["route"] == "pi:fake/model"
    assert receipt["status"] == "accepted"
    assert receipt["projectId"].startswith("project-")
    assert receipt["originId"].startswith("origin-")
    assert receipt["journalSequence"] >= 1


def test_review_persists_sanitized_source_context_in_packet_receipt_and_journal(
    tmp_path: Path,
) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    context = {
        "kind": "github_pull_request",
        "repository": "example/project",
        "pullNumber": 7,
        "baseSha": "a" * 40,
        "headSha": "b" * 40,
        "snapshotSha256": "c" * 64,
        "diffSha256": "d" * 64,
        "changedFiles": [{"path": "src/app.py", "sha256": "e" * 64}],
        "evidence": ["github.pull_request"],
    }

    result = client.review(ReviewRequest(prompt="review", source_context=context))

    assert result.status == "accepted"
    packet = json.loads(result.receipt_path.with_name("packet.json").read_text())
    receipt = json.loads(result.receipt_path.read_text())
    started = client.journal().events()[0]
    assert packet["sourceContext"] == context
    assert receipt["sourceContext"] == context
    assert started["sourceContext"] == context


def test_client_reuses_finding_identity_across_reviews(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    response = (
        '{"verdict":"changes-requested","findings":[{"severity":"high",'
        '"path":"app.py","line":1,"title":"Handle failure",'
        '"evidence":"e","reproduction":"r"}]}'
    )
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport(response)})

    first = client.review(ReviewRequest(prompt="review one"))
    second = client.review(ReviewRequest(prompt="review two"))

    assert first.status == "accepted"
    assert second.status == "accepted"
    findings = client.journal().findings()
    assert len(findings) == 1
    assert findings[0]["observations"] == 2
    assert findings[0]["firstReviewId"] == first.review_id
    assert findings[0]["lastReviewId"] == second.review_id

    receipt = json.loads(second.receipt_path.read_text())
    assert receipt["findings"][0]["findingId"] == findings[0]["findingId"]


def test_reobservation_does_not_reopen_a_fixed_finding(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    response = (
        '{"verdict":"changes-requested","findings":[{"severity":"high",'
        '"path":"app.py","line":1,"title":"Handle failure",'
        '"evidence":"e","reproduction":"r"}]}'
    )
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport(response)})

    first = client.review(ReviewRequest(prompt="review one"))
    finding_id = client.journal().findings()[0]["findingId"]
    client.journal().append_status_change(finding_id, "fixed", reason="patched")
    second = client.review(ReviewRequest(prompt="review two"))

    assert first.status == "accepted"
    assert second.status == "accepted"
    finding = client.journal().finding(finding_id)
    assert finding is not None
    assert finding["status"] == "fixed"
    assert finding["observations"] == 2


def test_client_findings_does_not_hide_journal_corruption(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    journal_path = tmp_path / ".reviewctl/journal.jsonl"
    journal_path.parent.mkdir(parents=True)
    journal_path.write_text('{"type":"finding_status_changed","findingId":"f1"}\n')
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    with pytest.raises(JournalOperationError) as error:
        client.findings()

    assert error.value.diagnostic.code == "journal_corrupt"


def test_finding_requires_fields_and_integer_line() -> None:
    with pytest.raises(ValueError, match="required"):
        Finding.from_value({"severity": "high"})
    value = {
        "severity": "high",
        "path": "source.py",
        "line": "3",
        "title": "Title",
        "evidence": "Evidence",
        "reproduction": "Reproduce",
    }
    with pytest.raises(ValueError, match="integer"):
        Finding.from_value(value)


def test_private_api_helpers_cover_ids_and_execution_diagnostics() -> None:
    value = Finding("high", "source.py", None, "Title", "Evidence", "Reproduce")
    assert api_module.finding_id(value).startswith("finding-")
    assert api_module._review_id("  explicit ") == "explicit"
    assert (
        api_module._execution_diagnostic(BackendExecution(124, "", None, BackendEvidence())).code
        == "timeout"
    )
    assert (
        api_module._execution_diagnostic(
            BackendExecution(1, "provider failed", None, BackendEvidence())
        ).code
        == "transport_unavailable"
    )
    assert (
        api_module._execution_diagnostic(BackendExecution(0, "", None, BackendEvidence())).code
        == "empty_response"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"cost_usd": float("nan")},
        {"cost_usd": -1.0},
        {"cost_usd": 10**1000},
        {"cost_usd": True},
        {"cost_usd": "invalid"},
        {"duration_ms": -1},
        {"input_tokens": True},
        {"output_tokens": -1},
        {"provider": 7},
        {"provider": ""},
    ],
)
def test_response_metadata_rejects_invalid_usage(changes: dict[str, object]) -> None:
    response = PersistedResponse("conversation", 0.0, 1, 1, "fake/model", 1, "fake", "{}")

    assert not api_module._response_metadata_valid(replace(response, **changes))


def test_response_metadata_accepts_nullable_usage() -> None:
    response = PersistedResponse("conversation", None, None, None, "fake/model", None, None, "{}")

    assert api_module._response_metadata_valid(response)


@pytest.mark.parametrize(
    "value",
    [[], {"bad": object()}, "not-object"],
)
def test_source_context_rejects_nonmapping_unsafe_and_nonobject_values(value: object) -> None:
    with pytest.raises(ValueError, match="source context"):
        api_module._normalize_source_context(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("nonstandard", [float("nan"), float("inf"), -float("inf")])
def test_source_context_rejects_nonstandard_json_numbers(nonstandard: float) -> None:
    with pytest.raises(ValueError, match="JSON-safe"):
        api_module._normalize_source_context({"nested": {"value": nonstandard}})


def test_source_context_rejects_oversized_encoded_value() -> None:
    with pytest.raises(ValueError, match="limit"):
        api_module._normalize_source_context({"payload": "x" * 33_000})


def test_source_context_rejects_nonobject_decoded_value(monkeypatch) -> None:
    monkeypatch.setattr(api_module.json, "loads", lambda encoded: [])
    with pytest.raises(ValueError, match="object"):
        api_module._normalize_source_context({"value": 1})


def test_client_rejects_empty_prompt_unknown_profile_and_bad_dimensions(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    assert client.review(ReviewRequest(prompt=" ")).status == "invalid_request"
    assert (
        client.review(ReviewRequest(prompt="review", profile="missing")).status == "config_invalid"
    )
    assert (
        client.review(ReviewRequest(prompt="review", dimensions=("security", "security"))).status
        == "invalid_request"
    )


def test_client_rejects_sensitive_remote_and_empty_route_profiles(tmp_path: Path) -> None:
    project = tmp_path / "reviewctl.toml"
    project.write_text(
        '[project]\nprivacy_mode = "private"\n'
        '[profiles.remote]\nroutes = ["pi:fake/model"]\nexecution = "remote"\n'
        '[profiles.empty]\nroutes = []\nexecution = "local"\n'
    )
    config = api_module.load_config(tmp_path, user_path=None)
    config = replace(config, project=replace(config.project, privacy_mode="sensitive"))
    client = ReviewClient(tmp_path, config, {"pi": FakeTransport()})
    assert (
        client.review(ReviewRequest(prompt="review", profile="remote")).status == "privacy_denied"
    )
    assert client.review(ReviewRequest(prompt="review", profile="empty")).status == "route_invalid"


def test_from_project_constructs_default_pi_transport(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path)
    assert isinstance(client.transports["pi"], PiTransport)
    assert isinstance(client.transports["codex"], CodexProjectTransport)


@pytest.mark.parametrize("route", ("openrouter:meta/muse-spark-1.3-contributor", "kiro:auto"))
def test_project_review_rejects_exploratory_transport_before_receipt(
    tmp_path: Path, route: str
) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        f'routes = ["{route}"]\n'
        'execution = "remote"\n'
    )

    result = ReviewClient.from_project(tmp_path).review(ReviewRequest(prompt="review"))

    assert result.status == "route_invalid"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "route_invalid"
    assert "not supported for formal project review" in result.diagnostic.message
    assert not (tmp_path / ".reviewctl" / "reviews").exists()


def test_exploratory_route_preserves_invalid_contract_precedence(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["kiro:auto"]\n'
        'response_contract = "unknown-contract"\n'
        'execution = "remote"\n'
    )

    result = ReviewClient.from_project(tmp_path).review(ReviewRequest(prompt="review"))

    assert result.status == "contract_failed"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "contract_failed"


def test_project_review_accepts_openrouter_only_when_explicitly_injected(
    tmp_path: Path,
) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["openrouter:example/model"]\n'
        'execution = "remote"\n'
    )
    source = tmp_path / "src.py"
    source.write_text("value = 1\n")
    transport = QueueTransport(['{"verdict":"approved","findings":[],"reviewedFiles":["src.py"]}'])

    result = ReviewClient.from_project(tmp_path, transports={"openrouter": transport}).review(
        ReviewRequest(prompt="review", files=(source,))
    )

    assert result.status == "accepted"
    assert transport.requests[0].prepared_contract is not None


def test_default_codex_project_route_executes_registered_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["codex:gpt-5.6-luna"]\n'
        'execution = "remote"\n'
    )
    source = tmp_path / "src.py"
    source.write_text("value = 1\n")
    observed: dict[str, object] = {}

    def fake_execute(request):
        observed["request"] = request
        return BackendExecution(
            0,
            "",
            PersistedResponse(
                "codex-session",
                0.0,
                1,
                1,
                request.model,
                1,
                "openai-codex",
                '{"verdict":"approved","findings":[],"reviewedFiles":["src.py"]}',
            ),
            BackendEvidence(),
        )

    monkeypatch.setattr("reviewctl.cli.execute_codex_backend", fake_execute)
    result = ReviewClient.from_project(tmp_path).review(
        ReviewRequest(prompt="review", files=(source,))
    )

    assert result.status == "accepted"
    request = observed["request"]
    assert request.source_roots[0] == tmp_path.resolve()
    assert request.source_roots[-1].name.startswith("reviewctl-project-source-")
    assert not request.source_roots[-1].exists()
    assert not request.files[0].exists()
    assert request.files[0].parent != tmp_path
    expected_digest = (
        get_contract("findings-json")
        .prepare(ContractContext(file_names=("src.py",), review_declaration_required=True))
        .digest
    )
    packet = json.loads((result.receipt_path.parent / "packet.json").read_text())
    receipt = json.loads(result.receipt_path.read_text())
    assert packet["contractDigest"] == expected_digest
    assert receipt["attempts"][0]["contractDigest"] == expected_digest


def test_openrouter_route_forwards_its_prepared_reviewed_files_contract(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["openrouter:example/model"]\n'
        'execution = "remote"\n'
    )
    source = tmp_path / "src.py"
    source.write_text("value = 1\n")
    transport = QueueTransport(['{"verdict":"approved","findings":[],"reviewedFiles":["src.py"]}'])

    result = ReviewClient.from_project(tmp_path, transports={"openrouter": transport}).review(
        ReviewRequest(prompt="review", files=(source,))
    )

    assert result.status == "accepted"
    expected = get_contract("findings-json").prepare(
        ContractContext(file_names=("src.py",), review_declaration_required=True)
    )
    assert transport.requests[0].prepared_contract == expected


def test_openrouter_prompt_only_review_does_not_require_impossible_file_declaration(
    tmp_path: Path,
) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["openrouter:example/model"]\n'
        'execution = "remote"\n'
    )
    transport = QueueTransport(['{"verdict":"approved","findings":[]}'])

    result = ReviewClient.from_project(tmp_path, transports={"openrouter": transport}).review(
        ReviewRequest(prompt="review")
    )

    assert result.status == "accepted"
    assert transport.requests[0].prepared_contract is not None
    assert transport.requests[0].prepared_contract.review_declaration_required is False


def test_mixed_pi_codex_routes_keep_pi_contract_without_codex_read_proof(
    tmp_path: Path,
) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:first/model", "codex:second-model"]\n'
        'execution = "remote"\n'
    )
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "accepted"
    assert result.findings == ()


def test_project_review_can_confine_external_snapshot_root_for_github_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["codex:gpt-5.6-luna"]\n'
        'execution = "remote"\n'
    )
    source_root = tmp_path.parent / "external-snapshot-root"
    source_root.mkdir()
    source = source_root / "src%2Ffile.py"
    source.write_text("value = 1\n")
    observed: dict[str, object] = {}

    def fake_execute(request):
        observed["request"] = request
        return BackendExecution(
            0,
            "",
            PersistedResponse(
                "codex-session",
                0.0,
                1,
                1,
                request.model,
                1,
                "openai-codex",
                '{"verdict":"approved","findings":[],"reviewedFiles":["src%2Ffile.py"]}',
            ),
            BackendEvidence(),
        )

    monkeypatch.setattr("reviewctl.cli.execute_codex_backend", fake_execute)
    result = ReviewClient.from_project(tmp_path).review(
        ReviewRequest(
            prompt="review",
            files=(source,),
            source_names=("src/file.py",),
            source_root=source_root,
        )
    )

    assert result.status == "accepted"
    request = observed["request"]
    assert request.files[0].name == source.name
    assert request.files[0].parent != source_root
    assert request.files[0].parent != tmp_path
    assert request.source_roots[:2] == (tmp_path.resolve(), source_root.resolve())
    assert request.source_roots[-1].name.startswith("reviewctl-project-source-")
    assert not request.source_roots[-1].exists()
    assert not request.files[0].exists()
    assert not (
        tmp_path / ".reviewctl" / "reviews" / result.review_id / "attempt-01" / "source"
    ).exists()


def test_project_review_rejects_an_unsafe_external_snapshot_root(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    result = ReviewClient.from_project(tmp_path).review(
        ReviewRequest(prompt="review", source_root=tmp_path / "missing-root")
    )

    assert result.status == "privacy_denied"


def test_client_rejects_explicit_and_duplicate_review_ids(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    first = client.review(ReviewRequest(prompt="review", review_id="explicit"))
    second = client.review(ReviewRequest(prompt="review", review_id="explicit"))
    assert first.status == "accepted"
    assert second.status == "invalid_request"


def test_client_rejects_relative_outside_missing_and_invalid_utf8_files(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    (tmp_path / "valid.py").write_text("value = 1\n")
    assert (
        client.review(ReviewRequest(prompt="review", files=(Path("valid.py"),))).status
        == "accepted"
    )
    assert (
        client.review(ReviewRequest(prompt="review", files=(Path("missing.py"),))).status
        == "invalid_request"
    )
    assert (
        client.review(
            ReviewRequest(prompt="review", files=(tmp_path.parent / "outside.py",))
        ).status
        == "privacy_denied"
    )
    invalid = tmp_path / "invalid.py"
    invalid.write_bytes(b"\xff\xfe")
    result = client.review(ReviewRequest(prompt="review", files=(invalid,)))
    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "UTF-8" in result.diagnostic.message


def test_client_preserves_relative_paths_when_source_basenames_collide(
    tmp_path: Path,
) -> None:
    write_default_config(tmp_path)
    first = tmp_path / "first" / "entry.py"
    second = tmp_path / "second" / "entry.py"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("first = 1\n")
    second.write_text("second = 2\n")
    transport = QueueTransport(
        [
            '{"verdict":"changes-requested","findings":['
            '{"severity":"high","path":"first%2Fentry.py","line":1,'
            '"title":"First","evidence":"e","reproduction":"r"},'
            '{"severity":"medium","path":"second%2Fentry.py","line":1,'
            '"title":"Second","evidence":"e","reproduction":"r"}]}'
        ]
    )
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    result = client.review(
        ReviewRequest(
            prompt="review",
            review_id="duplicate-basename",
            files=(first, second),
        )
    )

    assert result.status == "accepted"
    assert [path.name for path in transport.requests[0].files] == [
        "first%2Fentry.py",
        "second%2Fentry.py",
    ]
    assert [finding.path for finding in result.findings] == [
        "first/entry.py",
        "second/entry.py",
    ]
    packet = json.loads(result.receipt_path.with_name("packet.json").read_text())
    assert [item["name"] for item in packet["files"]] == [
        "first%2Fentry.py",
        "second%2Fentry.py",
    ]


def test_client_rejects_source_file_count_before_creating_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    write_default_config(tmp_path)
    files = tuple(tmp_path / f"source-{index}.py" for index in range(3))
    for path in files:
        path.write_text("value = 1\n")
    monkeypatch.setattr(api_module, "MAX_SOURCE_FILES", 2, raising=False)
    transport = QueueTransport(['{"verdict":"approved","findings":[]}'])
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    result = client.review(ReviewRequest(prompt="review", files=files))

    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "file limit" in result.diagnostic.message
    assert transport.requests == []
    assert not client.review_root.exists()


def test_client_rejects_aggregate_source_bytes_before_creating_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    write_default_config(tmp_path)
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("1234")
    second.write_text("5678")
    monkeypatch.setattr(api_module, "MAX_SOURCE_SET_BYTES", 7, raising=False)
    transport = QueueTransport(['{"verdict":"approved","findings":[]}'])
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    result = client.review(ReviewRequest(prompt="review", files=(first, second)))

    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "aggregate source byte limit" in result.diagnostic.message
    assert transport.requests == []
    assert not client.review_root.exists()


def test_client_rejects_the_same_project_relative_source_twice(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("value = 1\n")
    transport = QueueTransport(['{"verdict":"approved","findings":[]}'])
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    result = client.review(ReviewRequest(prompt="review", files=(source, source)))

    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "unique project-relative paths" in result.diagnostic.message
    assert transport.requests == []


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        " source.py",
        "source.py\x00",
        "src\\source.py",
        "/source.py",
        ".",
        "src//x.py",
        "../x.py",
    ],
)
def test_logical_source_name_rejects_unsafe_or_noncanonical_values(value: object) -> None:
    with pytest.raises(ValueError, match="project-relative paths"):
        api_module._logical_source_name(value)


def test_client_rejects_source_names_that_do_not_align_with_files(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("value = 1\n")
    transport = QueueTransport(['{"verdict":"approved","findings":[]}'])
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    result = client.review(
        ReviewRequest(
            prompt="review",
            files=(source,),
            source_names=("src/source.py", "tests/source.py"),
        )
    )

    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "one-to-one" in result.diagnostic.message
    assert transport.requests == []

    result = client.review(
        ReviewRequest(
            prompt="review",
            files=(source,),
            source_names=("../source.py",),
        )
    )
    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "project-relative paths" in result.diagnostic.message
    assert transport.requests == []


def test_client_rejects_non_json_source_context(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    result = client.review(ReviewRequest(prompt="review", source_context=object()))
    assert result.status == "invalid_request"


def test_client_rejects_read_errors_and_post_stat_growth(tmp_path: Path, monkeypatch) -> None:
    write_default_config(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("value = 1\n")
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    original_read = api_module._read_source_bytes

    def fail_read(path: Path, **kwargs) -> bytes:
        if path == source:
            raise OSError("denied")
        return original_read(path, **kwargs)

    monkeypatch.setattr(api_module, "_read_source_bytes", fail_read)
    result = client.review(ReviewRequest(prompt="review", files=(source,)))
    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "could not be read" in result.diagnostic.message

    monkeypatch.setattr(
        api_module,
        "_read_source_bytes",
        lambda path, **kwargs: b"x" * (MAX_SOURCE_BYTES + 1),
    )
    result = client.review(ReviewRequest(prompt="review", files=(source,)))
    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "exceeds" in result.diagnostic.message


def test_source_reader_fails_closed_without_nofollow_or_for_non_regular_files(
    tmp_path: Path, monkeypatch
) -> None:
    fifo = tmp_path / "source"
    os.mkfifo(fifo)
    with pytest.raises(OSError, match="regular file"):
        api_module._read_source_bytes(fifo)

    source = tmp_path / "source.py"
    source.write_text("safe = True\n")
    monkeypatch.setattr(api_module.os, "O_NOFOLLOW", None)
    with pytest.raises(OSError, match="without following symlinks"):
        api_module._read_source_bytes(source)


def test_source_reader_fails_closed_without_dirfd_support(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.py"
    source.write_text("safe = True\n")
    monkeypatch.setattr(api_module, "_OPEN_SUPPORTS_DIR_FD", False)

    with pytest.raises(OSError, match="without following symlinks"):
        api_module._read_source_bytes(source)


@pytest.mark.parametrize("invalid_component", ["root", "ancestor"])
def test_source_reader_rejects_non_directory_descriptors(
    tmp_path: Path, monkeypatch, invalid_component: str
) -> None:
    root = tmp_path / "project"
    package = root / "package"
    package.mkdir(parents=True)
    source = package / "source.py"
    source.write_text("safe = True\n")
    real_fstat = api_module.os.fstat
    calls = 0

    def fake_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        if invalid_component == "root" and calls == 1:
            return SimpleNamespace(st_mode=0)
        if invalid_component == "ancestor" and calls == 2:
            return SimpleNamespace(st_mode=0)
        return real_fstat(descriptor)

    monkeypatch.setattr(api_module.os, "fstat", fake_fstat)

    with pytest.raises(OSError, match="not a directory"):
        api_module._read_source_bytes(source, project_dir=root)


def test_client_rejects_source_replaced_by_external_symlink_before_open(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_default_config(project)
    source = project / "source.py"
    source.write_text("safe = True\n")
    external = tmp_path / "external.py"
    external.write_text("secret = True\n")
    transport = QueueTransport(['{"verdict":"approved","findings":[]}'])
    client = ReviewClient.from_project(project, transports={"pi": transport})
    real_read = Path.read_bytes
    real_open = os.open

    def swap_source() -> None:
        if not source.is_symlink():
            source.unlink()
            source.symlink_to(external)

    def raced_read(path: Path) -> bytes:
        if path == source:
            swap_source()
        return real_read(path)

    def raced_open(path, flags, *args, **kwargs):
        if Path(path) in {source, Path(source.name)}:
            swap_source()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", raced_read)
    monkeypatch.setattr(os, "open", raced_open)

    result = client.review(ReviewRequest(prompt="review", files=(source,)))

    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "could not be read" in result.diagnostic.message
    assert transport.requests == []


def test_client_rejects_source_with_ancestor_replaced_by_external_symlink_before_open(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    package = project / "package"
    package.mkdir(parents=True)
    write_default_config(project)
    source = package / "source.py"
    source.write_text("safe = True\n")
    external_package = tmp_path / "external-package"
    external_package.mkdir()
    (external_package / source.name).write_text("secret = True\n")
    displaced_package = project / "displaced-package"
    transport = QueueTransport(['{"verdict":"approved","findings":[]}'])
    client = ReviewClient.from_project(project, transports={"pi": transport})
    real_open = os.open
    swapped = False

    def raced_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if (
            Path(path) == source
            or (Path(path) == Path("package") and kwargs.get("dir_fd") is not None)
        ) and not swapped:
            swapped = True
            package.rename(displaced_package)
            package.symlink_to(external_package, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", raced_open)

    result = client.review(ReviewRequest(prompt="review", files=(source,)))

    assert swapped
    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "could not be read" in result.diagnostic.message
    assert transport.requests == []


def test_source_reader_rejects_project_parent_replaced_before_open(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    source = project / "source.py"
    source.write_text("safe = True\n")
    displaced = tmp_path / "workspace-displaced"
    external_workspace = tmp_path / "external-workspace"
    external_source = external_workspace / "project" / source.name
    external_source.parent.mkdir(parents=True)
    external_source.write_text("secret = True\n")
    real_open = api_module.os.open
    swapped = False

    def raced_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == Path(project.anchor) and not swapped:
            swapped = True
            workspace.rename(displaced)
            workspace.symlink_to(external_workspace, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(api_module.os, "open", raced_open)

    with pytest.raises(OSError):
        api_module._read_source_bytes(source, project_dir=project)

    assert swapped
    assert external_source.read_text() == "secret = True\n"


def test_source_reader_rejects_project_replaced_by_a_real_directory(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "source.py"
    source.write_text("safe = True\n")
    displaced = tmp_path / "project-displaced"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / source.name).write_text("secret = True\n")
    real_open = api_module.os.open
    swapped = False

    def raced_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == Path(project.anchor) and not swapped:
            swapped = True
            project.rename(displaced)
            replacement.rename(project)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(api_module.os, "open", raced_open)

    with pytest.raises(OSError, match="identity changed"):
        api_module._read_source_bytes(source, project_dir=project)

    assert swapped
    assert (project / source.name).read_text() == "secret = True\n"


def test_client_rejects_unknown_contract_and_unregistered_transport(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[profiles.default]\nroutes = ["llm:model"]\nresponse_contract = "unknown-contract"\n'
    )
    client = ReviewClient.from_project(tmp_path, transports={})
    result = client.review(ReviewRequest(prompt="review"))
    assert result.status == "contract_failed"

    (tmp_path / "reviewctl.toml").write_text('[profiles.default]\nroutes = ["llm:model"]\n')
    client = ReviewClient.from_project(tmp_path, transports={})
    result = client.review(ReviewRequest(prompt="review"))
    assert result.status == "transport_unavailable"

    (tmp_path / "reviewctl.toml").write_text(
        '[profiles.default]\nroutes = ["llm:first", "llm:second"]\n'
    )
    client = ReviewClient.from_project(tmp_path, transports={})
    result = client.review(ReviewRequest(prompt="review"))
    assert result.status == "transport_unavailable"


class RaisingTransport:
    def execute(self, request):
        raise ValueError("transport exploded")


class EmptyTransport:
    def execute(self, request):
        return BackendExecution(
            0, "", PersistedResponse("id", 0, 1, 1, "model", 1, "provider", ""), BackendEvidence()
        )


def test_client_maps_transport_and_empty_response_failures(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    raising = ReviewClient.from_project(tmp_path, transports={"pi": RaisingTransport()})
    assert raising.review(ReviewRequest(prompt="review")).status == "transport_unavailable"
    empty = ReviewClient.from_project(tmp_path, transports={"pi": EmptyTransport()})
    result = empty.review(ReviewRequest(prompt="review"))
    assert result.status == "empty_response"

    (tmp_path / "reviewctl.toml").write_text(
        '[profiles.default]\nroutes = ["pi:fake/first", "pi:fake/second"]\n'
    )
    raising = ReviewClient.from_project(tmp_path, transports={"pi": RaisingTransport()})
    result = raising.review(ReviewRequest(prompt="review"))
    assert result.status == "transport_unavailable"


def test_client_rejects_failed_execution_with_parseable_response(tmp_path: Path) -> None:
    write_default_config(tmp_path)

    class FailedExecutionTransport:
        def execute(self, request):
            return BackendExecution(
                exit_code=1,
                diagnostic="provider failed",
                response=PersistedResponse(
                    conversation_id="failed-but-parseable",
                    cost_usd=0.0,
                    duration_ms=1,
                    input_tokens=1,
                    model=request.model,
                    output_tokens=1,
                    provider="fake",
                    response='{"verdict":"approved","findings":[]}',
                ),
                evidence=BackendEvidence(),
            )

    client = ReviewClient.from_project(tmp_path, transports={"pi": FailedExecutionTransport()})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "transport_unavailable"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "transport_unavailable"


def test_client_freezes_source_bytes_across_fallback_attempts(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:first/model", "pi:second/model"]\n'
    )
    source = tmp_path / "source.py"
    initial = b"value = 1\n"
    source.write_bytes(initial)
    seen: list[bytes] = []
    staged_paths: list[Path] = []
    seen_roots: list[tuple[Path, ...]] = []

    class MutatingFallbackTransport:
        def execute(self, request):
            staged_paths.append(request.files[0])
            seen_roots.append(request.source_roots)
            seen.append(request.files[0].read_bytes())
            if len(seen) == 1:
                source.write_bytes(b"value = 2\n")
                request.files[0].write_bytes(b"changed by first attempt\n")
                return BackendExecution(1, "provider failed", None, BackendEvidence())
            return BackendExecution(
                0,
                "",
                PersistedResponse(
                    "conversation",
                    0.0,
                    1,
                    1,
                    request.model,
                    1,
                    "fake",
                    '{"verdict":"approved","findings":[]}',
                ),
                BackendEvidence(),
            )

    client = ReviewClient.from_project(tmp_path, transports={"pi": MutatingFallbackTransport()})

    result = client.review(ReviewRequest(prompt="review", files=(source,)))

    assert result.status == "accepted"
    assert seen == [initial, initial]
    assert staged_paths[0] != staged_paths[1]
    assert all(not path.exists() for path in staged_paths)
    assert all(roots[0] == tmp_path.resolve() for roots in seen_roots)
    assert all(
        path.parent == roots[-1] for path, roots in zip(staged_paths, seen_roots, strict=True)
    )
    assert not list((tmp_path / ".reviewctl" / "reviews").glob("**/source"))
    packet = json.loads(result.receipt_path.with_name("packet.json").read_text())
    assert packet["files"][0]["sha256"] == api_module._digest(initial)
    packet_digest = api_module._digest(
        json.dumps(packet, ensure_ascii=True, sort_keys=True).encode()
    )
    receipt = json.loads(result.receipt_path.read_text())
    assert receipt["packetDigest"] == packet_digest
    assert verify_project_receipt(result.receipt_path) is None


def test_client_cleans_snapshot_before_unexpected_transport_error(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("secret = 1\n")
    observed: list[Path] = []

    class ExplodingTransport:
        def execute(self, request):
            observed.extend(request.files)
            raise RuntimeError("unexpected")

    client = ReviewClient.from_project(tmp_path, transports={"pi": ExplodingTransport()})

    with pytest.raises(RuntimeError, match="unexpected"):
        client.review(ReviewRequest(prompt="review", files=(source,)))

    assert observed and all(not path.exists() for path in observed)
    assert not list((tmp_path / ".reviewctl").glob("**/source"))


def test_client_never_accepts_when_snapshot_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_default_config(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("secret = 1\n")
    external = tmp_path.parent / f"{tmp_path.name}-undeletable"

    class CleanupFailure:
        def __init__(self, **kwargs: object) -> None:
            external.mkdir()

        def __enter__(self) -> str:
            return str(external)

        def __exit__(self, *args: object) -> None:
            raise OSError("cleanup refused")

    monkeypatch.setattr(api_module.tempfile, "TemporaryDirectory", CleanupFailure)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review", files=(source,)))

    assert result.status == "transport_unavailable"
    assert not external.exists()
    assert not list((tmp_path / ".reviewctl").glob("**/source"))
    assert str(external) not in result.receipt_path.read_text()


def test_client_aborts_fallback_when_snapshot_cleanup_cannot_be_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:first/model", "pi:second/model"]\n'
    )
    source = tmp_path / "source.py"
    source.write_text("secret = 1\n")
    external = tmp_path.parent / f"{tmp_path.name}-undeletable"
    transport = FakeTransport()
    requests = []
    real_execute = transport.execute
    real_rmtree = api_module.shutil.rmtree

    class CleanupFailure:
        def __init__(self, **kwargs: object) -> None:
            external.mkdir()

        def __enter__(self) -> str:
            return str(external)

        def __exit__(self, *args: object) -> None:
            raise OSError("cleanup refused")

    def execute(request):
        requests.append(request)
        return real_execute(request)

    def refuse_rescue(path: Path) -> None:
        raise OSError(f"rescue refused for {path}")

    transport.execute = execute
    monkeypatch.setattr(api_module.tempfile, "TemporaryDirectory", CleanupFailure)
    monkeypatch.setattr(api_module.shutil, "rmtree", refuse_rescue)
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    with pytest.raises(RuntimeError, match="temporary source cleanup failed"):
        client.review(ReviewRequest(prompt="review", files=(source,)))

    assert len(requests) == 1
    assert external.exists()
    real_rmtree(external)


@pytest.mark.parametrize(
    ("conversation_id", "resolved_model"),
    [(None, "fake/model"), ("", "fake/model"), ("conversation", "other/model")],
)
def test_client_rejects_invalid_response_identity_before_contract(
    tmp_path: Path,
    monkeypatch,
    conversation_id: str | None,
    resolved_model: str,
) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:first/model", "pi:second/model"]\n'
    )
    real_contract = api_module.get_contract("findings-json")

    class UnreachableEvaluation:
        def prepare(self, context):
            return real_contract.prepare(context)

        def evaluate(self, *args, **kwargs):
            raise AssertionError("response identity must be checked before contract evaluation")

    monkeypatch.setattr(api_module, "get_contract", lambda name: UnreachableEvaluation())

    class InvalidIdentityTransport:
        def execute(self, request):
            return BackendExecution(
                0,
                "",
                PersistedResponse(
                    conversation_id,
                    0.0,
                    1,
                    1,
                    resolved_model,
                    1,
                    "fake",
                    '{"verdict":"approved","findings":[]}',
                ),
                BackendEvidence(),
            )

    client = ReviewClient.from_project(tmp_path, transports={"pi": InvalidIdentityTransport()})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "transport_unavailable"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "transport_unavailable"


def test_merge_findings_deduplicates_identical_values() -> None:
    value = Finding("high", "source.py", 1, "title", "evidence", "reproduction")
    assert ReviewClient._merge_findings((value,), (value,)) == (value,)


def test_merge_findings_uses_latest_variant_for_one_stable_identity() -> None:
    partial = Finding("medium", "source.py", 1, "title", "old evidence", "reproduction")
    completed = Finding("high", "source.py", 1, "title", "new evidence", "reproduction")

    assert finding_id(partial) == finding_id(completed)
    assert ReviewClient._merge_findings((partial,), (completed,)) == (completed,)


def test_client_maps_contract_evaluation_exception(tmp_path: Path, monkeypatch) -> None:
    write_default_config(tmp_path)
    real_contract = api_module.get_contract("findings-json")

    class RaisingContract:
        def prepare(self, context):
            return real_contract.prepare(context)

        def evaluate(self, *args, **kwargs):
            raise ValueError("evaluation exploded")

    monkeypatch.setattr(api_module, "get_contract", lambda name: RaisingContract())
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    result = client.review(ReviewRequest(prompt="review"))
    assert result.status == "contract_failed"


def test_client_handles_malformed_complete_and_incomplete_findings(
    tmp_path: Path, monkeypatch
) -> None:
    write_default_config(tmp_path)
    real_contract = api_module.get_contract("findings-json")
    original_evaluate = real_contract.evaluate
    state = {"status": EvaluationStatus.COMPLETE}

    class MalformedContract:
        def prepare(self, context):
            return real_contract.prepare(context)

        def evaluate(self, payload, prepared, context, *, evidence=None):
            base = original_evaluate('{"verdict":"approved","findings":[]}', prepared, context)
            if state["status"] is EvaluationStatus.COMPLETE:
                return base.__class__(**{**base.__dict__, "value": {"findings": [{"bad": True}]}})
            fragment = ContractFragment(
                "f", "f", FragmentKind.FINDING, {"bad": True}, base.payload_digest, ()
            )
            return base.__class__(
                **{
                    **base.__dict__,
                    "status": EvaluationStatus.INCOMPLETE,
                    "value": None,
                    "normalized_digest": None,
                    "valid_fragments": (fragment,),
                }
            )

    monkeypatch.setattr(api_module, "get_contract", lambda name: MalformedContract())
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    assert client.review(ReviewRequest(prompt="review")).status == "contract_failed"
    state["status"] = EvaluationStatus.INCOMPLETE
    assert client.review(ReviewRequest(prompt="review")).status == "contract_failed"


def test_client_returns_partial_when_all_attempts_are_incomplete(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[profiles.default]\nroutes = ["pi:fake/model"]\nexecution = "remote"\n'
    )
    partial = (
        '{"findings":[{"severity":"high","path":"source.py","line":1,'
        '"title":"t","evidence":"e","reproduction":"r"}]}'
    )
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport(partial)})
    result = client.review(ReviewRequest(prompt="review"))
    assert result.status == "partial"
    assert len(result.findings) == 1


def test_verify_project_receipt_rejects_unreadable_and_missing_digest(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    diagnostic = verify_project_receipt(missing)
    assert diagnostic is not None
    assert diagnostic.code == "receipt_invalid"
    no_digest = tmp_path / "no-digest.json"
    no_digest.write_text("{}")
    diagnostic = verify_project_receipt(no_digest)
    assert diagnostic is not None
    assert diagnostic.code == "receipt_invalid"
