"""Pi JSON-mode transport for bounded review attempts."""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reviewctl.artifacts import ArtifactStore
from reviewctl.backends import (
    BackendCapabilities,
    BackendEvidence,
    BackendExecution,
    BackendRequest,
    PersistedResponse,
    ReadOnlyCapability,
    SourceIsolation,
)

try:
    import resource
except ImportError:  # pragma: no cover - exercised on non-POSIX hosts
    resource = None  # type: ignore[assignment]

MAX_PI_STDOUT_BYTES = 256 * 1024 * 1024
MAX_PI_STDERR_BYTES = 1024 * 1024
# Runtime/session files need headroom beyond the retained reasoning stream.
MAX_PI_PROCESS_FILE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class PiProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def output_truncated(self) -> bool:
        return self.stdout_truncated or self.stderr_truncated


ProcessRunner = Callable[..., PiProcessResult]


def _signal_process(process: Any, sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
    except OSError:
        fallback = process.terminate if sig == signal.SIGTERM else process.kill
        try:
            fallback()
        except OSError:
            pass


def _drain_process(process: Any) -> tuple[bytes | None, bytes | None, bool]:
    try:
        stdout, stderr = process.communicate(timeout=2)
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout if isinstance(error.stdout, bytes) else None
        stderr = error.stderr if isinstance(error.stderr, bytes) else None
        return stdout, stderr, False
    except OSError:
        return None, None, False
    return (
        stdout if isinstance(stdout, bytes) else None,
        stderr if isinstance(stderr, bytes) else None,
        True,
    )


def _terminate_timed_out_process(process: Any) -> tuple[bytes | None, bytes | None]:
    _signal_process(process, signal.SIGTERM)
    stdout, stderr, finished = _drain_process(process)
    if finished:
        return stdout, stderr
    _signal_process(process, signal.SIGKILL)
    killed_stdout, killed_stderr, _finished = _drain_process(process)
    return killed_stdout or stdout, killed_stderr or stderr


def _kill_failed_process(process: Any) -> None:
    _signal_process(process, signal.SIGKILL)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired, OSError:
        pass


def _run_process(
    command: list[str], *, input_text: str, timeout_seconds: int, cwd: Path
) -> PiProcessResult:
    if resource is None:
        return PiProcessResult(
            126,
            b"",
            b"Pi bounded output capture unsupported on this platform",
            False,
        )
    capture_file_limit = (
        max(MAX_PI_PROCESS_FILE_BYTES, MAX_PI_STDOUT_BYTES, MAX_PI_STDERR_BYTES) + 1
    )

    def limit_output_files() -> None:
        resource.setrlimit(  # pragma: no cover - runs only in the pre-exec child
            resource.RLIMIT_FSIZE, (capture_file_limit, capture_file_limit)
        )

    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout_file,
            stderr=stderr_file,
            cwd=cwd,
            start_new_session=True,
            preexec_fn=limit_output_files,
        )
        stdout: bytes | None = None
        stderr: bytes | None = None
        timed_out = False
        try:
            communicated_stdout, communicated_stderr = process.communicate(
                input=input_text.encode("utf-8"), timeout=timeout_seconds
            )
            stdout = communicated_stdout if isinstance(communicated_stdout, bytes) else None
            stderr = communicated_stderr if isinstance(communicated_stderr, bytes) else None
        except subprocess.TimeoutExpired as error:
            timed_out = True
            stdout = error.stdout if isinstance(error.stdout, bytes) else None
            stderr = error.stderr if isinstance(error.stderr, bytes) else None
            trailing_stdout, trailing_stderr = _terminate_timed_out_process(process)
            if isinstance(trailing_stdout, bytes) and trailing_stdout:
                stdout = trailing_stdout
            if isinstance(trailing_stderr, bytes) and trailing_stderr:
                stderr = trailing_stderr
        except OSError as error:
            _kill_failed_process(process)
            return PiProcessResult(127, b"", str(error).encode("utf-8"), False)

        def bounded_output(value: bytes | None, stream, limit: int) -> tuple[bytes, bool]:
            if value is None:
                stream.seek(0)
                value = stream.read(limit + 1)
            return value[:limit], len(value) > limit

        stdout, stdout_truncated = bounded_output(stdout, stdout_file, MAX_PI_STDOUT_BYTES)
        stderr, stderr_truncated = bounded_output(stderr, stderr_file, MAX_PI_STDERR_BYTES)
        return PiProcessResult(
            124 if timed_out else process.returncode,
            stdout,
            stderr,
            timed_out,
            stdout_truncated,
            stderr_truncated,
        )


def _text_blocks(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    )


def _normalize_response(value: str) -> str:
    """Remove one conventional Markdown JSON fence without repairing content."""
    stripped = value.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return value
    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[0].strip().lower() in {"```", "```json"}:
        return "\n".join(lines[1:-1]).strip()
    return value


def _usage(value: object) -> tuple[float | None, int | None, int | None]:
    if not isinstance(value, dict):
        return None, None, None
    cost = value.get("cost")
    cost_value = cost.get("total") if isinstance(cost, dict) else cost
    cost_result = None
    if isinstance(cost_value, (int, float)) and not isinstance(cost_value, bool):
        try:
            numeric_cost = float(cost_value)
        except OverflowError:
            pass
        else:
            cost_result = (
                numeric_cost if math.isfinite(numeric_cost) and numeric_cost >= 0 else None
            )
    input_tokens = value.get("input")
    output_tokens = value.get("output")
    return (
        cost_result,
        input_tokens if type(input_tokens) is int and input_tokens >= 0 else None,
        output_tokens if type(output_tokens) is int and output_tokens >= 0 else None,
    )


def _resolved_model(requested: str, provider: str | None, resolved: str) -> str:
    if not resolved:
        return ""
    if "/" not in requested:
        return resolved
    if not provider or "/" in provider:
        return ""
    return resolved if resolved.startswith(f"{provider}/") else f"{provider}/{resolved}"


def _persisted_response(
    stdout: bytes, requested_model: str, duration_ms: int
) -> PersistedResponse | None:
    session_id = ""
    assistant: dict[str, object] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "session" and isinstance(event.get("id"), str):
            session_id = event["id"]
        if event.get("type") == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                assistant = message
        if event.get("type") == "agent_end":
            messages = event.get("messages")
            if isinstance(messages, list):
                for message in messages:
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        assistant = message
    if assistant is None:
        return None
    cost, input_tokens, output_tokens = _usage(assistant.get("usage"))
    provider = assistant.get("provider") if isinstance(assistant.get("provider"), str) else None
    model = assistant.get("model") if isinstance(assistant.get("model"), str) else ""
    return PersistedResponse(
        conversation_id=session_id,
        cost_usd=cost,
        duration_ms=duration_ms,
        input_tokens=input_tokens,
        model=_resolved_model(requested_model, provider, model),
        output_tokens=output_tokens,
        provider=provider,
        response=_normalize_response(_text_blocks(assistant.get("content"))),
    )


class PiTransport:
    """Invoke Pi with a bounded stdin packet and private observed artifacts."""

    def __init__(self, *, pi_bin: str = "pi", run_process: ProcessRunner = _run_process) -> None:
        self.pi_bin = pi_bin
        self.run_process = run_process

    @classmethod
    def capabilities(cls) -> BackendCapabilities:
        return BackendCapabilities(
            review_read_only=ReadOnlyCapability.ENFORCED,
            editable_execution=False,
            structured_output=True,
            resolved_model_identity=True,
            resolved_provider_identity=True,
            conversation_identity=True,
            usage_reporting=True,
            timeout_control=True,
            tool_control=True,
            source_isolation=SourceIsolation.UNAVAILABLE,
        )

    def execute(self, request: BackendRequest) -> BackendExecution:
        request.attempt_dir.mkdir(parents=True, exist_ok=True)
        artifacts = ArtifactStore(request.attempt_dir)
        session_path = request.attempt_dir / "session.jsonl"
        tool_flags = (
            ["--no-tools"]
            if request.tools == "none"
            else [
                "--tools",
                "read,grep,find,ls",
            ]
        )
        command = [
            self.pi_bin,
            "--mode",
            "json",
            "--print",
            *tool_flags,
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-approve",
            "--thinking",
            request.thinking,
            "--system-prompt",
            "Return only raw JSON matching the requested review contract; "
            "do not use Markdown fences.",
            "--model",
            request.model,
            "--session",
            str(session_path),
        ]
        packet = request.prompt
        for path in request.files:
            packet += f"\n\n--- FILE {path.name} ---\n" + path.read_text(encoding="utf-8")
        request_payload = {
            "command": command,
            "model": request.model,
            "responseContract": request.response_contract,
            "requestedMaxOutputTokens": request.max_output_tokens,
            "thinking": request.thinking,
            "outputTokenLimitEnforced": False,
            "tools": request.tools,
            "files": [path.name for path in request.files],
        }
        artifacts.write_text(
            "request.json", json.dumps(request_payload, ensure_ascii=True, sort_keys=True, indent=2)
        )
        started = time.monotonic()
        result = self.run_process(
            command,
            input_text=packet,
            timeout_seconds=request.timeout_seconds,
            cwd=request.source_roots[0] if request.source_roots else request.attempt_dir,
        )
        if result.stdout:
            response_path = artifacts.write_bytes("events.jsonl", result.stdout)
        else:
            response_path = None
        stderr_path = None
        if result.stderr:
            stderr_path = artifacts.write_bytes("stderr.log", result.stderr)
        persisted = (
            None
            if result.output_truncated
            else _persisted_response(
                result.stdout,
                request.model,
                round((time.monotonic() - started) * 1000),
            )
        )
        exit_code = result.returncode
        diagnostic = ""
        if result.timed_out:
            diagnostic = "review attempt timed out"
        elif result.output_truncated:
            streams = " and ".join(
                name
                for name, truncated in (
                    ("stdout", result.stdout_truncated),
                    ("stderr", result.stderr_truncated),
                )
                if truncated
            )
            diagnostic = f"Pi process {streams} output exceeded the capture limit"
            if exit_code == 0:
                exit_code = 1
        elif result.returncode != 0:
            diagnostic = result.stderr.decode(errors="replace").strip() or "Pi process failed"
        elif persisted is None or not persisted.response.strip():
            diagnostic = "empty_response"
        return BackendExecution(
            exit_code=exit_code,
            diagnostic=diagnostic,
            response=persisted,
            evidence=BackendEvidence(
                request=request.attempt_dir / "request.json",
                response=response_path,
                session=session_path if session_path.is_file() else None,
                stderr=stderr_path,
            ),
        )
