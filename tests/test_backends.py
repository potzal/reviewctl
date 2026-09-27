from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from reviewctl import backends, cli
from reviewctl.backends import (
    BackendCapabilities,
    BackendDescriptor,
    BackendEvidence,
    BackendExecution,
    BackendFamily,
    BackendRegistry,
    BackendRequest,
    DiscoveryKind,
    PersistedResponse,
    ReadOnlyCapability,
    SourceIsolation,
)


def descriptor(name: str) -> BackendDescriptor:
    return BackendDescriptor(
        name=name,
        family=BackendFamily.AGENT_CLI,
        discovery_kind=DiscoveryKind.EXECUTABLE,
        executable_env=f"{name.upper()}_BIN",
        default_executable=name,
        capabilities=BackendCapabilities(
            review_read_only=ReadOnlyCapability.ADVISORY,
            editable_execution=False,
            structured_output=True,
            resolved_model_identity=True,
            resolved_provider_identity=False,
            conversation_identity=True,
            usage_reporting=False,
            timeout_control=True,
            tool_control=False,
            source_isolation=SourceIsolation.UNAVAILABLE,
        ),
        qualification="unqualified",
    )


def test_registry_requires_unique_known_backends_and_sorts_descriptors(tmp_path: Path) -> None:
    response = PersistedResponse("turn", None, 1, None, "model", None, None, "ok")

    def execute(request: BackendRequest) -> BackendExecution:
        assert request.attempt_dir == tmp_path
        return BackendExecution(0, "", response, BackendEvidence())

    registry = BackendRegistry()
    registry.register(descriptor("cursor"), execute)
    registry.register(descriptor("claude"), execute)
    request = BackendRequest(
        prompt="Review",
        model="model",
        response_contract="findings-json",
        files=(tmp_path / "source.py",),
        attempt_dir=tmp_path,
        timeout_seconds=30,
        max_output_tokens=4096,
        source_class="synthetic",
        source_roots=(),
        provider_preferences=None,
    )

    assert registry.require("cursor").descriptor.name == "cursor"
    execution = registry.require("cursor").execute(request)
    assert execution == BackendExecution(0, "", response, BackendEvidence())
    assert execution.response is response
    assert tuple(item.name for item in registry.descriptors()) == ("claude", "cursor")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(descriptor("cursor"), execute)
    with pytest.raises(KeyError, match="unknown backend"):
        registry.require("missing")


def test_backend_request_is_immutable(tmp_path: Path) -> None:
    request = BackendRequest(
        prompt="Review",
        model="model",
        response_contract="findings-json",
        files=(tmp_path / "source.py",),
        attempt_dir=tmp_path,
        timeout_seconds=30,
        max_output_tokens=4096,
        source_class="synthetic",
        source_roots=(),
        provider_preferences=None,
    )

    with pytest.raises(FrozenInstanceError):
        request.model = "changed"  # type: ignore[misc]


def test_descriptor_serialization_uses_enum_values() -> None:
    assert descriptor("cursor").to_dict() == {
        "name": "cursor",
        "family": "agent-cli",
        "discovery_kind": "executable",
        "executable_env": "CURSOR_BIN",
        "default_executable": "cursor",
        "capabilities": {
            "review_read_only": "advisory",
            "editable_execution": False,
            "structured_output": True,
            "resolved_model_identity": True,
            "resolved_provider_identity": False,
            "conversation_identity": True,
            "usage_reporting": False,
            "timeout_control": True,
            "tool_control": False,
            "source_isolation": "unavailable",
        },
        "qualification": "unqualified",
    }


def test_cli_persisted_response_is_backend_contract() -> None:
    assert cli.PersistedResponse is backends.PersistedResponse


def backend_request(tmp_path: Path) -> BackendRequest:
    source_dir = tmp_path / "source"
    return BackendRequest(
        prompt="Review this change",
        model="example/model",
        response_contract="findings-json",
        files=(source_dir / "source.py", source_dir / "test_source.py"),
        attempt_dir=tmp_path / "attempt",
        timeout_seconds=45,
        max_output_tokens=8192,
        source_class="proprietary",
        source_roots=(source_dir, tmp_path / "shared"),
        provider_preferences={"only": ["example"]},
    )


@pytest.mark.parametrize("transport", ["llm", "codex", "kiro", "openrouter", "agy", "gemini", "pi"])
def test_every_backend_forwards_the_exact_prepared_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    prepared = cli.get_contract("findings-json").prepare(
        cli.ContractContext(
            file_names=("source.py", "test_source.py"),
            review_declaration_required=True,
        )
    )
    request = replace(backend_request(tmp_path), prepared_contract=prepared)
    request.attempt_dir.mkdir()
    calls = []

    def capture(**kwargs):
        calls.append(kwargs)
        if transport == "llm":
            return 0, ""
        return 0, "", PersistedResponse("turn", None, None, None, "example/model", None, None, "")

    monkeypatch.setattr(cli, f"invoke_{transport}", capture)
    getattr(cli, f"execute_{transport}_backend")(request)
    assert calls[0]["prepared_contract"] is prepared


def assert_execution_has_transport_semantics_only(execution: BackendExecution) -> None:
    assert set(execution.__dataclass_fields__) == {
        "exit_code",
        "diagnostic",
        "response",
        "evidence",
    }
    assert not hasattr(execution, "acceptance")
    assert not hasattr(execution, "result")


def test_build_backend_registry_has_exact_inventory_and_unqualified_descriptors() -> None:
    descriptors = cli.build_backend_registry().descriptors()

    assert tuple(item.name for item in descriptors) == (
        "agy",
        "codex",
        "gemini",
        "kiro",
        "llm",
        "openrouter",
        "pi",
    )
    assert {item.qualification for item in descriptors} == {"unqualified"}


def test_route_transports_match_registered_backend_names() -> None:
    assert set(cli.ROUTE_TRANSPORTS) == {
        descriptor.name for descriptor in cli.build_backend_registry().descriptors()
    }


@pytest.mark.parametrize(
    ("name", "family", "discovery_kind", "executable_env", "default_executable", "capabilities"),
    [
        (
            "agy",
            BackendFamily.AGENT_CLI,
            DiscoveryKind.EXECUTABLE,
            "AGY_BIN",
            "agy",
            BackendCapabilities(
                ReadOnlyCapability.ADVISORY,
                False,
                True,
                True,
                False,
                True,
                False,
                True,
                False,
                SourceIsolation.UNAVAILABLE,
            ),
        ),
        (
            "codex",
            BackendFamily.AGENT_CLI,
            DiscoveryKind.EXECUTABLE,
            "CODEX_BIN",
            "codex",
            BackendCapabilities(
                ReadOnlyCapability.SANDBOXED,
                False,
                True,
                True,
                False,
                True,
                False,
                True,
                True,
                SourceIsolation.EXTERNAL_SANDBOX,
            ),
        ),
        (
            "gemini",
            BackendFamily.AGENT_CLI,
            DiscoveryKind.EXECUTABLE,
            "GEMINI_BIN",
            "gemini",
            BackendCapabilities(
                ReadOnlyCapability.ADVISORY,
                False,
                True,
                False,
                True,
                True,
                True,
                True,
                False,
                SourceIsolation.UNAVAILABLE,
            ),
        ),
        (
            "llm",
            BackendFamily.GENERIC_MODEL_CLI,
            DiscoveryKind.EXECUTABLE,
            "LLM_BIN",
            "llm",
            BackendCapabilities(
                ReadOnlyCapability.ADVISORY,
                False,
                True,
                True,
                True,
                True,
                True,
                True,
                False,
                SourceIsolation.UNAVAILABLE,
            ),
        ),
        (
            "kiro",
            BackendFamily.AGENT_CLI,
            DiscoveryKind.EXECUTABLE,
            "KIRO_BIN",
            "kiro-cli",
            BackendCapabilities(
                ReadOnlyCapability.ADVISORY,
                False,
                False,
                False,
                False,
                True,
                False,
                True,
                True,
                SourceIsolation.UNAVAILABLE,
            ),
        ),
        (
            "openrouter",
            BackendFamily.PROVIDER_GATEWAY,
            DiscoveryKind.REMOTE_API,
            "",
            "",
            BackendCapabilities(
                ReadOnlyCapability.UNSUPPORTED,
                False,
                True,
                True,
                True,
                True,
                True,
                True,
                False,
                SourceIsolation.UNAVAILABLE,
            ),
        ),
        (
            "pi",
            BackendFamily.AGENT_CLI,
            DiscoveryKind.EXECUTABLE,
            "PI_BIN",
            "pi",
            BackendCapabilities(
                ReadOnlyCapability.ADVISORY,
                False,
                True,
                True,
                True,
                True,
                True,
                True,
                False,
                SourceIsolation.UNAVAILABLE,
            ),
        ),
    ],
)
def test_build_backend_registry_declares_exact_descriptor_capabilities(
    name: str,
    family: BackendFamily,
    discovery_kind: DiscoveryKind,
    executable_env: str,
    default_executable: str,
    capabilities: BackendCapabilities,
) -> None:
    descriptor = cli.build_backend_registry().require(name).descriptor

    assert descriptor == BackendDescriptor(
        name=name,
        family=family,
        discovery_kind=discovery_kind,
        executable_env=executable_env,
        default_executable=default_executable,
        capabilities=capabilities,
        qualification="unqualified",
    )


def test_execute_llm_backend_invokes_legacy_transport_and_maps_database_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = backend_request(tmp_path)
    expected_response = PersistedResponse("turn", 0.1, 10, 20, "resolved", 30, "provider", "ok")
    calls: dict[str, Any] = {}

    def fake_invoke_llm(**kwargs: object) -> tuple[int, str]:
        calls["invoke"] = kwargs
        Path(kwargs["database"]).write_bytes(b"database evidence")
        return 17, "diagnostic"

    def fake_load_response(database: Path | bytes) -> PersistedResponse:
        calls["load"] = database
        return expected_response

    monkeypatch.delenv("LLM_BIN", raising=False)
    monkeypatch.setattr(cli, "invoke_llm", fake_invoke_llm)
    monkeypatch.setattr(cli, "load_response", fake_load_response)
    request.attempt_dir.mkdir()

    execution = cli.execute_llm_backend(request)
    database = request.attempt_dir / "transport.sqlite3"
    loaded_database = calls["load"]
    assert loaded_database == b"database evidence"
    scratch_database = calls["invoke"]["database"]
    assert isinstance(scratch_database, Path)

    assert calls == {
        "invoke": {
            "llm_bin": "llm",
            "prompt": request.prompt,
            "model": request.model,
            "database": scratch_database,
            "files": list(request.files),
            "max_output_tokens": request.max_output_tokens,
            "response_contract": request.response_contract,
            "timeout_seconds": request.timeout_seconds,
            "prepared_contract": request.prepared_contract,
        },
        "load": b"database evidence",
    }
    assert scratch_database != database
    assert database.read_bytes() == b"database evidence"
    assert execution == BackendExecution(
        17, "diagnostic", expected_response, BackendEvidence(database=database)
    )
    assert_execution_has_transport_semantics_only(execution)


def test_execute_llm_backend_preserves_failure_without_persisted_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = backend_request(tmp_path)
    database = request.attempt_dir / "transport.sqlite3"

    monkeypatch.setattr(cli, "invoke_llm", lambda **_kwargs: (42, "transport failed"))
    monkeypatch.setattr(cli, "load_response", lambda _database: None)

    execution = cli.execute_llm_backend(request)

    assert execution == BackendExecution(
        exit_code=42,
        diagnostic="transport failed",
        response=None,
        evidence=BackendEvidence(database=database),
    )


def test_execute_llm_backend_classifies_unsafe_runtime_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = backend_request(tmp_path)
    external = tmp_path / "external.sqlite3"
    external.write_bytes(b"outside")

    def fake_invoke_llm(**kwargs: object) -> tuple[int, str]:
        Path(kwargs["database"]).symlink_to(external)
        return 0, ""

    monkeypatch.setattr(cli, "invoke_llm", fake_invoke_llm)

    execution = cli.execute_llm_backend(request)

    assert execution.exit_code == 502
    assert "database evidence was unsafe" in execution.diagnostic
    assert execution.response is None
    assert external.read_bytes() == b"outside"


@pytest.mark.parametrize(
    ("environment", "execute_name", "invoker", "executable_argument"),
    [
        ("LLM_BIN", "execute_llm_backend", "invoke_llm", "llm_bin"),
        ("CODEX_BIN", "execute_codex_backend", "invoke_codex", "codex_bin"),
        ("AGY_BIN", "execute_agy_backend", "invoke_agy", "agy_bin"),
        ("GEMINI_BIN", "execute_gemini_backend", "invoke_gemini", "gemini_bin"),
        ("KIRO_BIN", "execute_kiro_backend", "invoke_kiro", "kiro_bin"),
        ("PI_BIN", "execute_pi_backend", "invoke_pi", "pi_bin"),
    ],
)
def test_backend_executable_override_reaches_implemented_invoker_or_safe_stub(
    environment: str,
    execute_name: str,
    invoker: str | None,
    executable_argument: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = backend_request(tmp_path)
    request.attempt_dir.mkdir()
    executable = f"/test/bin/{environment.lower()}"
    captured: dict[str, object] = {}
    response = PersistedResponse("", None, None, None, "", None, None, "")
    execute = getattr(cli, execute_name)

    def fake_invoker(**kwargs: object) -> object:
        captured.update(kwargs)
        return (0, "") if invoker == "invoke_llm" else (0, "", response)

    monkeypatch.setenv(environment, executable)
    monkeypatch.setattr(cli, invoker, fake_invoker)
    if invoker == "invoke_llm":
        monkeypatch.setattr(cli, "load_response", lambda _database: response)

    execute(request)

    assert executable_argument is not None
    assert captured[executable_argument] == executable


def test_execute_kiro_backend_maps_all_persisted_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = backend_request(tmp_path)
    request.attempt_dir.mkdir()
    expected_response = PersistedResponse(
        "123e4567-e89b-12d3-a456-426614174000",
        None,
        12,
        None,
        request.model,
        None,
        None,
        "accepted response",
    )
    captured: dict[str, object] = {}

    def fake_invoke_kiro(**kwargs: object) -> tuple[int, str, PersistedResponse]:
        captured.update(kwargs)
        for name in ("request_path", "models_path", "response_path", "session_path"):
            path = kwargs[name]
            assert isinstance(path, Path)
            path.write_text("{}")
        diagnostic_path = kwargs["diagnostic_path"]
        assert isinstance(diagnostic_path, Path)
        diagnostic_path.write_text("redacted")
        return 0, "diagnostic", expected_response

    monkeypatch.setattr(cli, "invoke_kiro", fake_invoke_kiro)
    execution = cli.execute_kiro_backend(request)

    assert captured["kiro_bin"] == "kiro-cli"
    assert captured["model"] == request.model
    assert (request.attempt_dir / "response.md").read_text() == "accepted response"
    assert execution == BackendExecution(
        0,
        "diagnostic",
        expected_response,
        BackendEvidence(
            request=request.attempt_dir / "request.json",
            response=request.attempt_dir / "response.log",
            session=request.attempt_dir / "session.json",
            final_response=request.attempt_dir / "response.md",
            stderr=request.attempt_dir / "stderr.log",
        ),
    )


def test_execute_kiro_backend_omits_empty_final_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = backend_request(tmp_path)
    request.attempt_dir.mkdir()
    response = PersistedResponse("session", None, 1, None, request.model, None, None, "")
    monkeypatch.setattr(cli, "invoke_kiro", lambda **_kwargs: (0, "", response))

    execution = cli.execute_kiro_backend(request)

    assert execution.evidence.final_response is None
    assert not (request.attempt_dir / "response.md").exists()


@pytest.mark.parametrize("contract", ["verdict", "document"])
def test_execute_codex_backend_prompt_only_uses_temporary_empty_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contract: str
) -> None:
    request = replace(
        backend_request(tmp_path),
        files=(),
        source_roots=(),
        source_class="synthetic",
        response_contract=contract,
    )
    request.attempt_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    calls = []
    response = PersistedResponse("turn", None, None, None, "model", None, None, "result")

    def fake_invoke_codex(**kwargs):
        workspace = kwargs["workspace"]
        assert workspace.is_dir()
        assert workspace.resolve() != Path.cwd()
        assert workspace.resolve() != request.attempt_dir.resolve()
        assert list(workspace.iterdir()) == []
        calls.append(kwargs)
        return 0, "", response

    monkeypatch.setattr(cli, "invoke_codex", fake_invoke_codex)
    execution = cli.execute_codex_backend(request)
    assert not calls[0]["workspace"].exists()
    assert calls[0]["source_roots"] is None
    assert execution.response is response
    assert execution.exit_code == 0
    assert (request.attempt_dir / "response.md").read_text() == "result"


def test_execute_codex_backend_persists_rejected_response_and_maps_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = backend_request(tmp_path)
    expected_response = PersistedResponse(
        "turn", None, 10, None, "resolved", None, None, "rejected"
    )
    calls: dict[str, object] = {}

    def fake_invoke_codex(**kwargs: object) -> tuple[int, str, PersistedResponse]:
        calls.update(kwargs)
        return 23, "rejected output", expected_response

    request.attempt_dir.mkdir()
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.setattr(cli, "invoke_codex", fake_invoke_codex)

    execution = cli.execute_codex_backend(request)
    response_path = request.attempt_dir / "response.md"

    assert calls == {
        "codex_bin": "codex",
        "prompt": request.prompt,
        "model": request.model,
        "response_contract": request.response_contract,
        "source_roots": list(request.source_roots),
        "timeout_seconds": request.timeout_seconds,
        "workspace": request.files[0].parent,
        "prepared_contract": request.prepared_contract,
        "evidence_dir": request.attempt_dir,
        "evidence_parent_identity": request.evidence_parent_identity,
    }
    assert response_path.read_text() == "rejected"
    assert execution == BackendExecution(
        23, "rejected output", expected_response, BackendEvidence(response=response_path)
    )
    assert_execution_has_transport_semantics_only(execution)


def test_execute_gemini_backend_invokes_headless_transport_and_maps_all_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = backend_request(tmp_path)
    expected_response = PersistedResponse(
        "session", None, 10, 20, request.model, 30, "google-gemini-cli", "ok"
    )
    calls: dict[str, object] = {}

    def fake_invoke_gemini(**kwargs: object) -> tuple[int, str, PersistedResponse]:
        calls.update(kwargs)
        return 0, "", expected_response

    request.attempt_dir.mkdir()
    monkeypatch.delenv("GEMINI_BIN", raising=False)
    monkeypatch.setattr(cli, "invoke_gemini", fake_invoke_gemini)

    execution = cli.execute_gemini_backend(request)
    request_path = request.attempt_dir / "request.json"
    response_path = request.attempt_dir / "response.json"
    session_path = request.attempt_dir / "session.json"
    final_response_path = request.attempt_dir / "response.md"
    stderr_path = request.attempt_dir / "stderr.log"

    assert calls == {
        "gemini_bin": "gemini",
        "prompt": request.prompt,
        "model": request.model,
        "files": list(request.files),
        "max_output_tokens": request.max_output_tokens,
        "response_contract": request.response_contract,
        "timeout_seconds": request.timeout_seconds,
        "request_path": request_path,
        "response_path": response_path,
        "session_path": session_path,
        "diagnostic_path": stderr_path,
        "evidence_parent_identity": request.evidence_parent_identity,
        "prepared_contract": request.prepared_contract,
    }
    assert final_response_path.read_text() == "ok"
    assert execution == BackendExecution(
        0,
        "",
        expected_response,
        BackendEvidence(
            request=request_path,
            response=response_path,
            session=session_path,
            final_response=final_response_path,
            stderr=stderr_path,
        ),
    )
    assert_execution_has_transport_semantics_only(execution)


def test_execute_openrouter_backend_invokes_legacy_transport_and_maps_json_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = backend_request(tmp_path)
    expected_response = PersistedResponse("turn", 0.2, 10, 20, "resolved", 30, "provider", "ok")
    calls: dict[str, object] = {}

    def fake_invoke_openrouter(**kwargs: object) -> tuple[int, str, PersistedResponse]:
        calls.update(kwargs)
        return 0, "", expected_response

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(cli, "invoke_openrouter", fake_invoke_openrouter)

    execution = cli.execute_openrouter_backend(request)
    request_path = request.attempt_dir / "request.json"
    response_path = request.attempt_dir / "response.json"

    assert calls == {
        "api_key": "test-key",
        "prompt": request.prompt,
        "model": request.model,
        "files": list(request.files),
        "max_output_tokens": request.max_output_tokens,
        "reasoning_effort": request.reasoning_effort,
        "provider_preferences": request.provider_preferences,
        "prepared_contract": request.prepared_contract,
        "response_contract": request.response_contract,
        "timeout_seconds": request.timeout_seconds,
        "request_path": request_path,
        "response_path": response_path,
        "evidence_parent_identity": request.evidence_parent_identity,
    }
    assert execution == BackendExecution(
        0,
        "",
        expected_response,
        BackendEvidence(request=request_path, response=response_path),
    )
    assert_execution_has_transport_semantics_only(execution)


def test_execute_agy_backend_invokes_legacy_transport_and_maps_json_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = backend_request(tmp_path)
    expected_response = PersistedResponse("turn", None, 10, 20, "resolved", 30, "provider", "ok")
    calls: dict[str, object] = {}

    def fake_invoke_agy(**kwargs: object) -> tuple[int, str, PersistedResponse]:
        calls.update(kwargs)
        return 0, "", expected_response

    monkeypatch.delenv("AGY_BIN", raising=False)
    monkeypatch.setattr(cli, "invoke_agy", fake_invoke_agy)

    execution = cli.execute_agy_backend(request)
    request_path = request.attempt_dir / "request.json"
    response_path = request.attempt_dir / "response.json"

    assert calls == {
        "agy_bin": "agy",
        "prompt": request.prompt,
        "model": request.model,
        "files": list(request.files),
        "max_output_tokens": request.max_output_tokens,
        "response_contract": request.response_contract,
        "timeout_seconds": request.timeout_seconds,
        "request_path": request_path,
        "response_path": response_path,
        "evidence_parent_identity": request.evidence_parent_identity,
        "prepared_contract": request.prepared_contract,
    }
    assert execution == BackendExecution(
        0,
        "",
        expected_response,
        BackendEvidence(request=request_path, response=response_path),
    )
    assert_execution_has_transport_semantics_only(execution)


@pytest.mark.parametrize("response_text", ["final response", ""])
def test_execute_pi_backend_invokes_legacy_transport_and_maps_all_evidence(
    response_text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = backend_request(tmp_path)
    expected_response = PersistedResponse(
        "turn", None, 10, 20, "resolved", 30, "provider", response_text
    )
    calls: dict[str, object] = {}

    def fake_invoke_pi(**kwargs: object) -> tuple[int, str, PersistedResponse]:
        calls.update(kwargs)
        Path(kwargs["session_path"]).write_text("session evidence")
        return 0, "", expected_response

    request.attempt_dir.mkdir()
    monkeypatch.delenv("PI_BIN", raising=False)
    monkeypatch.setattr(cli, "invoke_pi", fake_invoke_pi)

    execution = cli.execute_pi_backend(request)
    request_path = request.attempt_dir / "request.json"
    events_path = request.attempt_dir / "events.jsonl"
    session_path = request.attempt_dir / "session.jsonl"
    final_response_path = request.attempt_dir / "response.md"
    stderr_path = request.attempt_dir / "stderr.log"
    scratch_session = calls["session_path"]
    assert isinstance(scratch_session, Path)

    assert calls == {
        "pi_bin": "pi",
        "prompt": request.prompt,
        "model": request.model,
        "files": list(request.files),
        "max_output_tokens": request.max_output_tokens,
        "response_contract": request.response_contract,
        "timeout_seconds": request.timeout_seconds,
        "request_path": request_path,
        "response_path": events_path,
        "session_path": scratch_session,
        "diagnostic_path": stderr_path,
        "evidence_parent_identity": request.evidence_parent_identity,
        "thinking": request.thinking,
        "prepared_contract": request.prepared_contract,
    }
    assert scratch_session != session_path
    assert session_path.read_text() == "session evidence"
    assert final_response_path.is_file() is bool(response_text)
    if response_text:
        assert final_response_path.read_text() == response_text
    assert execution == BackendExecution(
        0,
        "",
        expected_response,
        BackendEvidence(
            request=request_path,
            response=events_path,
            session=session_path,
            final_response=final_response_path,
            stderr=stderr_path,
        ),
    )
    assert_execution_has_transport_semantics_only(execution)


def test_execute_pi_backend_classifies_unsafe_runtime_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = backend_request(tmp_path)
    external = tmp_path / "external-session.jsonl"
    external.write_text("outside")

    def fake_invoke_pi(**kwargs: object) -> tuple[int, str, PersistedResponse]:
        Path(kwargs["session_path"]).symlink_to(external)
        return 0, "", PersistedResponse("turn", None, 1, 2, "model", 3, None, "ok")

    monkeypatch.setattr(cli, "invoke_pi", fake_invoke_pi)

    execution = cli.execute_pi_backend(request)

    assert execution.exit_code == 502
    assert "session evidence was unsafe" in execution.diagnostic
    assert external.read_text() == "outside"
