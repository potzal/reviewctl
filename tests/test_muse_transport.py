"""Contract tests for the headless Muse Code (`muse exec`) review transport."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from reviewctl import cli, review_flow
from reviewctl.backends import PersistedResponse

MODEL = "muse-spark-1.3-contributor"
SESSION = "01a19b38-bd4c-7993-8aa6-15452177631f"
VERDICT = "VERDICT: APPROVED\nNo findings."


REPOSITORY = Path(__file__).resolve().parents[1]


def run_cli(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "reviewctl", *arguments],
        cwd=REPOSITORY,
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=True,
        check=False,
    )


def write_fake_python_executable(path: Path, name: str, source: str) -> Path:
    """Create a coverage-neutral stdlib fake without disabling CLI coverage."""
    executable = path / name
    payload = path / f".{name}.py"
    payload.write_text(source)
    executable.write_text(
        "#!/bin/sh\n"
        "unset COVERAGE_PROCESS_CONFIG COVERAGE_PROCESS_START COV_CORE_SOURCE COV_CORE_CONFIG\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(payload))} "$@"\n'
    )
    executable.chmod(0o755)
    return executable


def review_arguments(tmp_path: Path) -> list[str]:
    prompt = tmp_path / "prompt.md"
    source = tmp_path / "source.py"
    prompt.write_text("Review this bounded change. Return VERDICT and numbered findings.")
    source.write_text("def example() -> None: pass\n")
    return [
        "run",
        "--review-id",
        "muse-review",
        "--prompt-file",
        str(prompt),
        "--file",
        str(source),
        "--artifact-root",
        str(tmp_path / "artifacts"),
        "--model",
        MODEL,
    ]


def event(payload_type: str, payload: dict[str, Any], *, kind: str = "session") -> str:
    return json.dumps(
        {
            "stream": {"kind": kind, "id": SESSION},
            "payload_type": payload_type,
            "payload": payload,
        }
    )


def configured(model: str = MODEL, provider: object = "meta") -> str:
    return event("run.model.configured", {"model_id": model, "provider_id": provider})


def usage(model: str = MODEL, input_tokens: object = 12, output_tokens: object = 34) -> str:
    return event(
        "run.usage",
        {
            "models": [
                {"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens}
            ]
        },
        kind="exec.usage",
    )


def terminal(text: object = VERDICT, name: str = "completed") -> str:
    return event(f"run.terminal.{name}", {"terminal": name, "text": text, "reason": None})


def tool_result(text: str, **extra: object) -> str:
    return event("tool.result", {"text": text, **extra})


def stream(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def valid_stream() -> bytes:
    return stream(
        configured(),
        event("run.output.delta", {"text": "noise that is not the final answer"}),
        usage(),
        terminal(),
    )


def write_fake_muse(path: Path, stdout: bytes, *, exit_code: int = 0, stderr: bytes = b"") -> Path:
    """Fake `muse` that records its launch and replays one fixed event stream."""
    (path / "muse-stdout.bin").write_bytes(stdout)
    (path / "muse-stderr.bin").write_bytes(stderr)
    return write_fake_python_executable(
        path,
        "muse",
        f"""import json
import os
import sys
from pathlib import Path

root = Path({str(path)!r})
arguments = sys.argv[1:]
prompt_file = Path(arguments[arguments.index('--prompt-file') + 1])
workspace = Path(arguments[arguments.index('--workspace') + 1])
(root / 'muse-launch.json').write_text(json.dumps({{
    'arguments': arguments,
    'cwd': os.getcwd(),
    'environment': dict(os.environ),
    'packet': prompt_file.read_text(),
    'promptMode': oct(prompt_file.stat().st_mode & 0o777),
    'stdin': sys.stdin.read(),
    'workspace': str(workspace),
    'workspaceEntries': os.listdir(workspace),
}}))
sys.stdout.buffer.write((root / 'muse-stdout.bin').read_bytes())
sys.stderr.buffer.write((root / 'muse-stderr.bin').read_bytes())
sys.exit({exit_code})
""",
    )


def invoke(tmp_path: Path, muse_bin: str, **overrides: Any) -> tuple[int, str, PersistedResponse]:
    source = tmp_path / "source.py"
    source.write_text("def send() -> None: pass\n")
    arguments: dict[str, Any] = {
        "muse_bin": muse_bin,
        "prompt": "Review the bounded synthetic source.",
        "model": MODEL,
        "files": [source],
        "max_output_tokens": 123,
        "response_contract": "verdict",
        "timeout_seconds": 20,
        "request_path": tmp_path / "request.json",
        "response_path": tmp_path / "response.jsonl",
        "session_path": tmp_path / "session.json",
        "diagnostic_path": tmp_path / "stderr.log",
    }
    arguments.update(overrides)
    return cli.invoke_muse(**arguments)


def test_invoke_muse_runs_without_tools_and_keeps_only_the_final_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-reach-muse")
    monkeypatch.setenv("MUSE_AUTH_PATH", "/test/auth")
    fake = write_fake_muse(tmp_path, valid_stream())

    exit_code, diagnostic, response = invoke(tmp_path, str(fake), reasoning_effort="low")

    assert (exit_code, diagnostic) == (0, "")
    assert response == PersistedResponse(
        conversation_id=SESSION,
        cost_usd=None,
        duration_ms=response.duration_ms,
        input_tokens=12,
        model=MODEL,
        output_tokens=34,
        provider="meta",
        response=VERDICT,
    )
    launch = json.loads((tmp_path / "muse-launch.json").read_text())
    arguments = launch["arguments"]
    prompt_index = arguments.index("--prompt-file") + 1
    workspace_index = arguments.index("--workspace") + 1
    expected = [
        "exec",
        "--json",
        "--prompt-file",
        arguments[prompt_index],
        "--model",
        MODEL,
        "--reasoning-effort",
        "low",
        "--preset",
        "empty",
        "--disable-write",
        "--disable-shell",
        "--disable-web-tools",
        "--disable-reminders",
        "--disable-request-user-input",
        "--no-foreign-personal-context",
        "--no-session-log",
        "--approval-mode",
        "untrusted",
        "--approval-judge",
        "off",
        "--max-model-steps",
        "4",
        "--workspace",
        arguments[workspace_index],
    ]
    assert arguments == expected
    # The packet carries the frozen source; the workspace is empty, so nothing else is reachable.
    assert "def send() -> None: pass" in launch["packet"]
    assert launch["promptMode"] == "0o600"
    assert launch["workspaceEntries"] == []
    assert Path(launch["cwd"]).resolve() == Path(launch["workspace"]).resolve()
    assert (
        not Path(arguments[prompt_index])
        .resolve()
        .is_relative_to(Path(launch["workspace"]).resolve())
    )
    assert launch["stdin"] == ""
    assert "OPENROUTER_API_KEY" not in launch["environment"]
    assert launch["environment"]["MUSE_AUTH_PATH"] == "/test/auth"
    assert launch["environment"]["MUSE_NO_AUTO_UPDATE"] == "1"

    request = json.loads((tmp_path / "request.json").read_text())
    assert request["command"][0] == str(fake)
    assert request["command"][1:] == expected
    assert request["model"] == MODEL
    assert request["reasoningEffort"] == "low"
    assert request["preset"] == "empty"
    assert request["maxOutputTokens"] == 123
    assert request["outputTokenLimitEnforced"] is False
    assert request["responseContract"] == "verdict"
    assert "source.py" in request["prompt"]
    assert (tmp_path / "response.jsonl").read_bytes() == valid_stream()
    assert json.loads((tmp_path / "session.json").read_text()) == {
        "modelId": MODEL,
        "observedModels": [{"input_tokens": 12, "model": MODEL, "output_tokens": 34}],
        "providerId": "meta",
        "sessionId": SESSION,
    }


def test_invoke_muse_defaults_to_explicit_high_effort_and_strips_json_fence(
    tmp_path: Path,
) -> None:
    fake = write_fake_muse(
        tmp_path,
        stream(
            configured(),
            usage(input_tokens=None, output_tokens=None),
            terminal('```json\n{"verdict": "approved", "findings": []}\n```'),
        ),
    )

    exit_code, _, response = invoke(tmp_path, str(fake), response_contract="findings-json")

    assert exit_code == 0
    assert response.response == '{"verdict": "approved", "findings": []}'
    assert (response.input_tokens, response.output_tokens) == (None, None)
    arguments = json.loads((tmp_path / "muse-launch.json").read_text())["arguments"]
    assert arguments[arguments.index("--reasoning-effort") + 1] == "high"


def test_invoke_muse_tolerates_a_refused_tool_call_and_unreported_counters(tmp_path: Path) -> None:
    fake = write_fake_muse(
        tmp_path,
        stream(
            "",
            configured(),
            tool_result(
                "tool unavailable: unknown tool `muse.read_file`. Session tool ids: (none)"
            ),
            event("run.model.configured", {"model_id": None}),
            event("", {}),
            usage(input_tokens="12", output_tokens=True),
            terminal(),
        ),
    )

    exit_code, diagnostic, response = invoke(tmp_path, str(fake))

    assert (exit_code, diagnostic) == (0, "")
    assert response.response == VERDICT
    assert (response.input_tokens, response.output_tokens) == (None, None)


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        (b"{\n", "Muse returned invalid JSONL"),
        (b"\xff\n", "Muse returned invalid JSONL"),
        (b"[]\n", "Muse returned a non-object event"),
        (
            b'{"payload_type":"a","payload_type":"b","payload":{}}\n',
            "Muse returned invalid JSONL",
        ),
        (stream(configured(), usage()), "Muse returned no terminal event"),
        (stream(configured(), terminal()), "Muse returned no billed model evidence"),
        (
            stream(configured(), event("run.usage", []), terminal()),
            "Muse returned malformed usage evidence",
        ),
        (
            stream(configured(), event("run.usage", {"models": "unexpected"}), terminal()),
            "Muse returned malformed usage evidence",
        ),
        (
            stream(configured(), event("run.usage", {"models": []}), terminal()),
            "Muse returned malformed usage evidence",
        ),
        (
            stream(configured(), event("run.usage", {"models": ["unexpected"]}), terminal()),
            "Muse returned malformed usage evidence",
        ),
        (
            stream(configured(), event("run.usage", {"models": [{"input_tokens": 1}]}), terminal()),
            "Muse returned malformed usage evidence",
        ),
        (
            stream(
                configured(),
                event(
                    "run.usage",
                    {"models": [{"model": MODEL, "input_tokens": 1}, "unexpected"]},
                ),
                terminal(),
            ),
            "Muse returned malformed usage evidence",
        ),
        (
            stream(configured(), usage(), event("run.usage", {"models": 1}), terminal()),
            "Muse returned malformed usage evidence",
        ),
        (stream(configured(), terminal(), terminal()), "Muse returned multiple terminal events"),
        (stream(configured(), terminal("", "failed")), "Muse run did not complete"),
        (
            stream(configured(), event("run.terminal.completed", {"terminal": "failed"})),
            "Muse run did not complete",
        ),
        (
            stream(configured(), event("run.terminal.completed", [])),
            "Muse returned a malformed terminal event",
        ),
        (stream(configured(), terminal(None)), "Muse returned no response"),
        (stream(terminal()), "Muse returned no model identity"),
        (
            stream(event("run.model.configured", []), terminal()),
            "Muse returned no model identity",
        ),
        (stream(configured("other-model"), terminal()), "Muse ran a different model: other-model"),
        (stream(configured(provider=None), terminal()), "Muse returned no provider identity"),
        (
            stream(configured(), usage(model="other-model"), terminal()),
            "Muse ran a different model: other-model",
        ),
        (
            stream(configured(), tool_result("Read text file `/etc/hosts`."), terminal()),
            "Muse reported a tool execution in a tool-free review",
        ),
        (
            stream(configured(), event("tool.result", []), terminal()),
            "Muse reported a tool execution in a tool-free review",
        ),
        (
            stream(
                configured(),
                json.dumps(
                    {
                        "stream": "bad",
                        "payload_type": "run.terminal.completed",
                        "payload": {"terminal": "completed", "text": VERDICT},
                    }
                ),
            ),
            "Muse returned no session identifier",
        ),
    ],
)
def test_invoke_muse_fails_closed_on_untrustworthy_event_streams(
    tmp_path: Path, stdout: bytes, message: str
) -> None:
    fake = write_fake_muse(tmp_path, stdout)

    exit_code, diagnostic, response = invoke(tmp_path, str(fake))

    assert (exit_code, diagnostic) == (502, message)
    assert response.response == ""
    assert (tmp_path / "response.jsonl").read_bytes() == stdout
    assert not (tmp_path / "session.json").exists()


def test_invoke_muse_preserves_failed_exit_and_redacted_diagnostic(tmp_path: Path) -> None:
    fake = write_fake_muse(
        tmp_path,
        stream(terminal("", "failed")),
        exit_code=1,
        stderr=b"run ended with Failed: model does not exist\n",
    )

    exit_code, diagnostic, response = invoke(tmp_path, str(fake))

    assert exit_code == 1
    assert "model does not exist" in diagnostic
    assert response.response == ""
    assert "model does not exist" in (tmp_path / "stderr.log").read_text()


@pytest.mark.parametrize(
    ("capture", "expected"),
    [
        ((b"partial", b"late", True, False, False), (124, "review attempt timed out")),
        ((b"", b"", True, False, False), (124, "review attempt timed out")),
        (
            (b"partial", b"", False, True, False),
            (502, "Muse transport output exceeded bounded capture"),
        ),
        ((b"partial", b"", False, False, True), (502, "Muse transport capture did not reach EOF")),
    ],
)
def test_invoke_muse_retains_partial_evidence_when_capture_stops_early(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: tuple[bytes, bytes, bool, bool, bool],
    expected: tuple[int, str],
) -> None:
    fake = write_fake_muse(tmp_path, b"")
    monkeypatch.setattr(cli, "_communicate_kiro_bounded", lambda *_a, **_k: capture)

    exit_code, diagnostic, response = invoke(tmp_path, str(fake))

    assert (exit_code, diagnostic) == expected
    assert response.response == ""
    assert (tmp_path / "response.jsonl").exists() is bool(capture[0])
    if capture[0]:
        assert (tmp_path / "response.jsonl").read_bytes() == capture[0]
    assert (tmp_path / "stderr.log").exists() is bool(capture[1])


def test_invoke_muse_reports_launch_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = invoke(tmp_path, str(tmp_path / "absent-muse"))
    assert missing[:2] == (
        127,
        f"Muse transport executable not found: {tmp_path / 'absent-muse'}",
    )

    def fail_popen(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("denied")

    second = tmp_path / "second"
    second.mkdir()
    monkeypatch.setattr(cli.subprocess, "Popen", fail_popen)
    denied = invoke(
        second,
        "muse",
        request_path=second / "request.json",
    )
    assert denied[0] == 127
    assert denied[1].startswith("Muse transport could not execute:")


def test_invoke_muse_reports_a_capture_subprocess_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = write_fake_muse(tmp_path, b"")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.SubprocessError("capture broke")

    monkeypatch.setattr(cli, "_communicate_kiro_bounded", fail)

    assert invoke(tmp_path, str(fake))[:2] == (
        126,
        "Muse bounded output capture failed: capture broke",
    )


def test_invoke_muse_refuses_a_model_that_could_become_a_flag(tmp_path: Path) -> None:
    exit_code, diagnostic, _ = invoke(tmp_path, "muse", model="--yolo")

    assert (exit_code, diagnostic) == (502, "Muse model id must not start with '-': --yolo")
    assert not (tmp_path / "request.json").exists()


def test_muse_environment_keeps_login_and_drops_unrelated_credentials() -> None:
    environment = cli.muse_process_environment(
        {
            "HOME": "/test/home",
            "PATH": "/test/bin",
            "MUSE_AUTH_PATH": "/test/auth",
            "OPENROUTER_API_KEY": "secret",
            "GITHUB_TOKEN": "secret",
        }
    )

    assert environment == {
        "HOME": "/test/home",
        "PATH": "/test/bin",
        "MUSE_AUTH_PATH": "/test/auth",
        "MUSE_NO_AUTO_UPDATE": "1",
        "NO_COLOR": "1",
        "CLICOLOR": "0",
        "TERM": "dumb",
    }


def test_muse_is_a_known_route_and_receipt_transport() -> None:
    assert cli.parse_route(f"muse:{MODEL}").transport == "muse"
    assert "muse" in review_flow.SUPPORTED_REVIEW_TRANSPORTS
    assert "muse" in cli.LOCAL_POLICY_TRANSPORTS
    assert "muse" in cli.REQUIRED_LOCAL_POLICY_TRANSPORTS


def verdict_fake(tmp_path: Path) -> Path:
    return write_fake_muse(tmp_path, stream(configured(), usage(), terminal()))


def test_run_accepts_a_muse_verdict_with_a_verifiable_receipt_and_forwards_effort(
    tmp_path: Path,
) -> None:
    fake = verdict_fake(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path),
        "--transport",
        "muse",
        "--reasoning-effort",
        "medium",
        env={"MUSE_BIN": str(fake)},
    )

    assert result.returncode == 0, result.stderr
    receipt_path = next((tmp_path / "artifacts").rglob("receipt.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["acceptedAttempt"] == 1
    attempt = receipt["attempts"][0]
    assert attempt["transport"] == "muse"
    assert attempt["model"]["resolved"] == MODEL
    assert attempt["provider"]["resolved"] == "meta"
    arguments = json.loads((tmp_path / "muse-launch.json").read_text())["arguments"]
    assert arguments[arguments.index("--reasoning-effort") + 1] == "medium"
    verified = run_cli("verify", str(receipt_path))
    assert verified.returncode == 0, verified.stderr


def test_run_requires_policy_before_sending_proprietary_source_to_muse(tmp_path: Path) -> None:
    fake = verdict_fake(tmp_path)
    arguments = [
        *review_arguments(tmp_path),
        "--transport",
        "muse",
        "--source-class",
        "proprietary",
    ]
    environment = {"MUSE_BIN": str(fake)}

    missing = run_cli(*arguments, env=environment)
    assert missing.returncode == 2
    assert "proprietary local transport reviews require --policy" in missing.stderr.lower()
    assert not (tmp_path / "muse-launch.json").exists()

    policy = tmp_path / "muse-policy.toml"
    policy.write_text("[transports.muse]\nsource_allowed = true\n")
    allowed = run_cli(*arguments, "--policy", str(policy), env=environment)
    assert allowed.returncode == 0, allowed.stderr
    assert (tmp_path / "muse-launch.json").exists()
