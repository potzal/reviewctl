from __future__ import annotations

from pathlib import Path

import pytest

from reviewctl.backends import (
    BackendEvidence,
    BackendExecution,
    BackendRequest,
    PersistedResponse,
    ReadOnlyCapability,
    SourceIsolation,
)
from reviewctl.openrouter_project_transport import OpenRouterProjectTransport


def request(tmp_path: Path, source: Path) -> BackendRequest:
    return BackendRequest(
        prompt="Review the supplied source.",
        model="openrouter/deepseek/deepseek-v4.1",
        response_contract="findings-json",
        files=(source,),
        attempt_dir=tmp_path / "attempt",
        timeout_seconds=30,
        max_output_tokens=8000,
        source_class="private",
        source_roots=(tmp_path,),
        provider_preferences=None,
    )


def test_openrouter_project_transport_delegates_to_the_canonical_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src.py"
    source.write_text("secret = 42\n")
    observed: dict[str, object] = {}

    def fake_execute(req: BackendRequest) -> BackendExecution:
        # The project transport must forward the request unchanged; OpenRouter
        # receives files inline, so there is no staging/path rewriting.
        observed["request"] = req
        return BackendExecution(
            0,
            "",
            PersistedResponse(
                "openrouter-session",
                0.0001,
                1200,
                100,
                req.model,
                50,
                "deepseek",
                '{"verdict":"approved","findings":[],"reviewedFiles":["src.py"]}',
            ),
            BackendEvidence(
                request=req.attempt_dir / "request.json",
                response=req.attempt_dir / "response.json",
            ),
        )

    monkeypatch.setattr("reviewctl.cli.execute_openrouter_backend", fake_execute)
    execution = OpenRouterProjectTransport().execute(request(tmp_path, source))

    assert execution.exit_code == 0
    assert execution.diagnostic == ""
    assert execution.response is not None
    assert '"verdict":"approved"' in execution.response.response
    # Forwarded unchanged: same object, no staging.
    assert observed["request"].files == (source,)
    assert observed["request"].model == "openrouter/deepseek/deepseek-v4.1"


def test_openrouter_project_transport_propagates_backend_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src.py"
    source.write_text("x = 1\n")

    def fake_execute(req: BackendRequest) -> BackendExecution:
        return BackendExecution(
            127, "OPENROUTER_API_KEY is not configured", None, BackendEvidence()
        )

    monkeypatch.setattr("reviewctl.cli.execute_openrouter_backend", fake_execute)
    execution = OpenRouterProjectTransport().execute(request(tmp_path, source))

    assert execution.exit_code == 127
    assert "OPENROUTER_API_KEY" in execution.diagnostic
    assert execution.response is None


def test_openrouter_project_transport_capabilities_match_the_canonical_descriptor() -> None:
    caps = OpenRouterProjectTransport.capabilities()
    # Must agree with the "openrouter" descriptor registered in reviewctl.cli so
    # `run` and `github review` share one identity/receipt shape.
    assert caps.review_read_only is ReadOnlyCapability.UNSUPPORTED
    assert caps.editable_execution is False
    assert caps.structured_output is True
    assert caps.resolved_model_identity is True
    assert caps.resolved_provider_identity is True
    assert caps.conversation_identity is True
    assert caps.usage_reporting is True
    assert caps.timeout_control is True
    assert caps.tool_control is False
    assert caps.source_isolation is SourceIsolation.UNAVAILABLE
