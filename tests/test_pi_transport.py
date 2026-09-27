from __future__ import annotations

import json
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import reviewctl.pi_transport as pi_module
from reviewctl.backends import BackendRequest
from reviewctl.pi_transport import PiProcessResult, PiTransport


def request(
    tmp_path: Path,
    *,
    prompt: str = "private prompt",
    tools: str = "none",
    thinking: str = "minimal",
) -> BackendRequest:
    return BackendRequest(
        prompt=prompt,
        model="openrouter/stealth/ox-alpha",
        response_contract="findings-json",
        files=(),
        attempt_dir=tmp_path / "attempt",
        timeout_seconds=30,
        max_output_tokens=8000,
        source_class="private",
        source_roots=(tmp_path,),
        provider_preferences=None,
        tools=tools,
        thinking=thinking,
    )


def assistant_stream(text: str, *, output: int = 20) -> bytes:
    events = [
        {"type": "session", "id": "session-1"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "provider": "openrouter",
                "model": "stealth/ox-alpha",
                "usage": {"input": 10, "output": output, "cost": {"total": 0.01}},
                "content": [{"type": "text", "text": text}],
            },
        },
    ]
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


class FakeRunner:
    def __init__(self, stdout: bytes) -> None:
        self.stdout = stdout
        self.last_command: list[str] = []
        self.last_stdin = ""

    def __call__(self, command, *, input_text, timeout_seconds, cwd):
        self.last_command = command
        self.last_stdin = input_text
        return PiProcessResult(returncode=0, stdout=self.stdout, stderr=b"", timed_out=False)


def popen_factory(process: object, *, expected_command: list[str], expected_cwd: Path):
    def fake_popen(
        command: list[str],
        *,
        stdin: object,
        stdout: object,
        stderr: object,
        cwd: Path,
        start_new_session: bool,
        preexec_fn: object,
    ) -> object:
        assert command == expected_command
        assert stdin is subprocess.PIPE
        assert hasattr(stdout, "write")
        assert hasattr(stderr, "write")
        assert cwd == expected_cwd
        assert start_new_session is True
        assert callable(preexec_fn)
        return process

    return fake_popen


def test_pi_request_uses_exact_model_and_no_tools_by_default(tmp_path: Path) -> None:
    runner = FakeRunner(assistant_stream('{"verdict":"approved","findings":[]}'))
    execution = PiTransport(run_process=runner).execute(request(tmp_path))

    assert "--model" in runner.last_command
    assert "openrouter/stealth/ox-alpha" in runner.last_command
    assert "--no-tools" in runner.last_command
    assert "--thinking" in runner.last_command
    assert "minimal" in runner.last_command
    assert "private prompt" not in runner.last_command
    assert runner.last_stdin == "private prompt"
    assert execution.response is not None
    assert execution.response.model == "openrouter/stealth/ox-alpha"
    assert execution.evidence.request == tmp_path / "attempt" / "request.json"
    assert json.loads(execution.evidence.request.read_text())["thinking"] == "minimal"


def test_pi_request_uses_configured_thinking_level(tmp_path: Path) -> None:
    runner = FakeRunner(assistant_stream('{"verdict":"approved","findings":[]}'))

    PiTransport(run_process=runner).execute(request(tmp_path, thinking="max"))

    thinking_index = runner.last_command.index("--thinking")
    assert runner.last_command[thinking_index + 1] == "max"


def test_pi_missing_resolved_model_stays_empty(tmp_path: Path) -> None:
    events = [json.loads(line) for line in assistant_stream("{}").decode().splitlines()]
    events[1]["message"].pop("model")
    runner = FakeRunner(b"".join(json.dumps(event).encode() + b"\n" for event in events))

    execution = PiTransport(run_process=runner).execute(request(tmp_path))

    assert execution.response is not None
    assert execution.response.model == ""


def test_pi_unqualified_requested_model_stays_unqualified(tmp_path: Path) -> None:
    events = [json.loads(line) for line in assistant_stream("{}").decode().splitlines()]
    events[1]["message"]["provider"] = "provider"
    events[1]["message"]["model"] = "model"
    runner = FakeRunner(b"".join(json.dumps(event).encode() + b"\n" for event in events))

    execution = PiTransport(run_process=runner).execute(replace(request(tmp_path), model="model"))

    assert execution.response is not None
    assert execution.response.model == "model"


@pytest.mark.parametrize("provider", [None, "invalid/provider"])
def test_pi_qualified_model_requires_atomic_provider(provider: str | None) -> None:
    assert pi_module._resolved_model("provider/model", provider, "model") == ""


def test_pi_empty_response_preserves_usage_and_diagnostic(tmp_path: Path) -> None:
    runner = FakeRunner(assistant_stream("", output=8000))
    execution = PiTransport(run_process=runner).execute(request(tmp_path))

    assert execution.response is not None
    assert execution.response.response == ""
    assert execution.response.output_tokens == 8000
    assert execution.diagnostic == "empty_response"
    assert execution.evidence.response is not None


def test_pi_transport_normalizes_one_json_markdown_fence(tmp_path: Path) -> None:
    runner = FakeRunner(assistant_stream('```json\n{"verdict":"approved","findings":[]}\n```'))

    execution = PiTransport(run_process=runner).execute(request(tmp_path))

    assert execution.response is not None
    assert execution.response.response == '{"verdict":"approved","findings":[]}'


def test_pi_read_only_profile_enables_only_read_tools(tmp_path: Path) -> None:
    runner = FakeRunner(assistant_stream('{"verdict":"approved","findings":[]}'))

    PiTransport(run_process=runner).execute(request(tmp_path, tools="read-only"))

    assert "--tools" in runner.last_command
    assert "read,grep,find,ls" in runner.last_command
    assert "--no-tools" not in runner.last_command


def test_pi_transport_does_not_claim_unenforced_token_cap() -> None:
    capabilities = PiTransport.capabilities()

    assert capabilities.output_token_limit_enforced is False


def test_pi_transport_preserves_timeout_as_transport_diagnostic(tmp_path: Path) -> None:
    class TimeoutRunner(FakeRunner):
        def __call__(self, command, *, input_text, timeout_seconds, cwd):
            self.last_command = command
            self.last_stdin = input_text
            return PiProcessResult(
                returncode=124,
                stdout=b"",
                stderr=b"timed out",
                timed_out=True,
                stderr_truncated=True,
            )

    execution = PiTransport(run_process=TimeoutRunner(b"")).execute(request(tmp_path))

    assert execution.response is None
    assert "timed out" in execution.diagnostic


def test_run_process_returns_successful_process_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class SuccessfulProcess:
        pid = 123
        returncode = 0

        def communicate(self, *, input: bytes, timeout: int) -> tuple[bytes, bytes]:
            assert input == b"prompt"
            assert timeout == 4
            return b"stdout", b"stderr"

    process = SuccessfulProcess()
    monkeypatch.setattr(
        pi_module.subprocess,
        "Popen",
        popen_factory(process, expected_command=["pi"], expected_cwd=tmp_path),
    )
    result = pi_module._run_process(["pi"], input_text="prompt", timeout_seconds=4, cwd=tmp_path)

    assert result == PiProcessResult(0, b"stdout", b"stderr", False)


def test_run_process_fails_typed_without_resource_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pi_module, "resource", None)

    result = pi_module._run_process(["pi"], input_text="prompt", timeout_seconds=4, cwd=tmp_path)

    assert result == PiProcessResult(
        126,
        b"",
        b"Pi bounded output capture unsupported on this platform",
        False,
    )


@pytest.mark.parametrize(
    ("stream", "size", "limit_name", "truncated_name"),
    [
        ("stdout", 8 * 1024 * 1024 + 1024, "MAX_PI_STDOUT_BYTES", "stdout_truncated"),
        ("stderr", 1024 * 1024 + 1024, "MAX_PI_STDERR_BYTES", "stderr_truncated"),
    ],
)
def test_run_process_bounds_child_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: str,
    size: int,
    limit_name: str,
    truncated_name: str,
) -> None:
    monkeypatch.setattr(pi_module, limit_name, 1024)
    descriptor = 1 if stream == "stdout" else 2

    result = pi_module._run_process(
        [sys.executable, "-c", f"import os; os.write({descriptor}, b'x' * {size})"],
        input_text="",
        timeout_seconds=10,
        cwd=tmp_path,
    )

    limit = getattr(pi_module, limit_name)
    assert len(getattr(result, stream)) <= limit
    assert getattr(result, truncated_name)


def test_project_pi_capture_accepts_large_reasoning_and_runtime_files(tmp_path: Path) -> None:
    result = pi_module._run_process(
        [
            sys.executable,
            "-c",
            "import os,resource; "
            "assert resource.getrlimit(resource.RLIMIT_FSIZE) == (536870913, 536870913); "
            "open('runtime.bin', 'wb').write(b'r' * (9 * 1024 * 1024)); "
            "os.write(1, b'x' * (9 * 1024 * 1024))",
        ],
        input_text="",
        timeout_seconds=10,
        cwd=tmp_path,
    )
    assert result.returncode == 0
    assert len(result.stdout) == 9 * 1024 * 1024
    assert not result.output_truncated
    assert (tmp_path / "runtime.bin").stat().st_size == 9 * 1024 * 1024
    assert pi_module.MAX_PI_STDOUT_BYTES == 256 * 1024 * 1024


@pytest.mark.parametrize(
    ("returncode", "stdout_truncated", "stderr_truncated", "stream"),
    [(0, True, False, "stdout"), (2, False, True, "stderr")],
)
def test_pi_transport_maps_truncated_output_to_failure(
    tmp_path: Path,
    returncode: int,
    stdout_truncated: bool,
    stderr_truncated: bool,
    stream: str,
) -> None:
    class TruncatedRunner(FakeRunner):
        def __call__(self, command, *, input_text, timeout_seconds, cwd):
            return PiProcessResult(
                returncode=returncode,
                stdout=assistant_stream('{"verdict":"approved","findings":[]}'),
                stderr=b"",
                timed_out=False,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )

    execution = PiTransport(run_process=TruncatedRunner(b"")).execute(request(tmp_path))

    assert execution.exit_code != 0
    assert execution.response is None
    assert stream in execution.diagnostic
    assert "output" in execution.diagnostic
    assert "limit" in execution.diagnostic


def test_run_process_keeps_timeout_priority_with_bounded_output(tmp_path: Path) -> None:
    size = pi_module.MAX_PI_STDERR_BYTES + 1024

    result = pi_module._run_process(
        [
            sys.executable,
            "-c",
            f"import os,time; os.write(2, b'x' * {size}); time.sleep(5)",
        ],
        input_text="",
        timeout_seconds=1,
        cwd=tmp_path,
    )

    assert result.returncode == 124
    assert result.timed_out
    assert result.stderr_truncated
    assert len(result.stderr) == pi_module.MAX_PI_STDERR_BYTES


def test_run_process_terminates_then_kills_a_timed_out_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class TimeoutProcess:
        pid = 456
        returncode = -signal.SIGKILL

        def __init__(self) -> None:
            self.communicate_calls = 0

        def communicate(self, **kwargs: object) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired(
                    "pi", 4, output=b"partial stdout", stderr=b"partial stderr"
                )
            if self.communicate_calls == 2:
                raise subprocess.TimeoutExpired("pi", 2)
            return b"trailing stdout", b"trailing stderr"

    process = TimeoutProcess()
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        pi_module.subprocess,
        "Popen",
        popen_factory(process, expected_command=["pi"], expected_cwd=tmp_path),
    )
    monkeypatch.setattr(
        pi_module.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    result = pi_module._run_process(["pi"], input_text="prompt", timeout_seconds=4, cwd=tmp_path)

    assert result == PiProcessResult(124, b"trailing stdout", b"trailing stderr", True)
    assert signals == [(456, signal.SIGTERM), (456, signal.SIGKILL)]


def test_run_process_preserves_partial_output_when_timeout_process_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class GoneProcess:
        pid = 789
        returncode = -signal.SIGTERM

        def __init__(self) -> None:
            self.communicate_calls = 0

        def communicate(self, **kwargs: object) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired("pi", 4, output=b"partial", stderr=b"error")
            return b"", b""

    process = GoneProcess()
    monkeypatch.setattr(
        pi_module.subprocess,
        "Popen",
        popen_factory(process, expected_command=["pi"], expected_cwd=tmp_path),
    )

    def process_gone(pid: int, sig: signal.Signals) -> None:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(pi_module.os, "killpg", process_gone)
    result = pi_module._run_process(["pi"], input_text="prompt", timeout_seconds=4, cwd=tmp_path)

    assert result == PiProcessResult(124, b"partial", b"error", True)


def test_run_process_ignores_process_gone_before_timeout_sigkill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class GoneBeforeKillProcess:
        pid = 790
        returncode = -signal.SIGTERM

        def __init__(self) -> None:
            self.communicate_calls = 0

        def communicate(self, **kwargs: object) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            if self.communicate_calls <= 2:
                raise subprocess.TimeoutExpired("pi", kwargs.get("timeout", 4))
            return b"", b""

    process = GoneBeforeKillProcess()
    monkeypatch.setattr(
        pi_module.subprocess,
        "Popen",
        popen_factory(process, expected_command=["pi"], expected_cwd=tmp_path),
    )

    def disappear_before_kill(pid: int, sig: signal.Signals) -> None:
        if sig == signal.SIGKILL:
            raise ProcessLookupError(pid)

    monkeypatch.setattr(pi_module.os, "killpg", disappear_before_kill)

    result = pi_module._run_process(["pi"], input_text="prompt", timeout_seconds=4, cwd=tmp_path)

    assert result == PiProcessResult(124, b"", b"", True)
    assert process.communicate_calls == 3


@pytest.mark.parametrize("denied_signal", [signal.SIGTERM, signal.SIGKILL])
def test_run_process_preserves_timeout_when_group_signal_is_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, denied_signal: signal.Signals
) -> None:
    class PermissionProcess:
        pid = 791
        returncode = -signal.SIGKILL

        def __init__(self) -> None:
            self.communicate_calls = 0
            self.terminated = False
            self.killed = False

        def communicate(self, **kwargs: object) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            required_timeouts = 1 if denied_signal == signal.SIGTERM else 2
            if self.communicate_calls <= required_timeouts:
                raise subprocess.TimeoutExpired("pi", kwargs.get("timeout", 4))
            return b"", b""

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    process = PermissionProcess()
    monkeypatch.setattr(
        pi_module.subprocess,
        "Popen",
        popen_factory(process, expected_command=["pi"], expected_cwd=tmp_path),
    )

    def deny_signal(pid: int, sig: signal.Signals) -> None:
        if sig == denied_signal:
            raise PermissionError("permission denied")

    monkeypatch.setattr(pi_module.os, "killpg", deny_signal)

    result = pi_module._run_process(["pi"], input_text="prompt", timeout_seconds=4, cwd=tmp_path)

    assert result == PiProcessResult(124, b"", b"", True)
    assert process.terminated is (denied_signal == signal.SIGTERM)
    assert process.killed is (denied_signal == signal.SIGKILL)


@pytest.mark.parametrize("recovery", ["after-term", "after-kill"])
def test_run_process_preserves_timeout_when_termination_and_drain_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recovery: str
) -> None:
    class UnstoppableProcess:
        pid = 792
        returncode = 1

        def __init__(self) -> None:
            self.communicate_calls = 0
            self.terminate_attempts = 0
            self.kill_attempts = 0

        def communicate(self, **kwargs: object) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            if self.communicate_calls == 1 or (
                recovery == "after-kill" and self.communicate_calls == 2
            ):
                raise subprocess.TimeoutExpired("pi", kwargs.get("timeout", 4))
            raise OSError("drain failed")

        def terminate(self) -> None:
            self.terminate_attempts += 1
            raise PermissionError("terminate denied")

        def kill(self) -> None:
            self.kill_attempts += 1
            raise PermissionError("kill denied")

    process = UnstoppableProcess()
    monkeypatch.setattr(
        pi_module.subprocess,
        "Popen",
        popen_factory(process, expected_command=["pi"], expected_cwd=tmp_path),
    )
    monkeypatch.setattr(
        pi_module.os,
        "killpg",
        lambda pid, sig: (_ for _ in ()).throw(PermissionError("group signal denied")),
    )

    result = pi_module._run_process(["pi"], input_text="prompt", timeout_seconds=4, cwd=tmp_path)

    assert result == PiProcessResult(124, b"", b"", True)
    assert process.communicate_calls == 3
    assert process.terminate_attempts == 1
    assert process.kill_attempts == 1


def test_run_process_reports_communication_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class BrokenProcess:
        pid = 101
        returncode = 1

        def __init__(self) -> None:
            self.killed = False
            self.waited = False

        def communicate(self, **kwargs: object) -> tuple[bytes, bytes]:
            raise OSError("pipe broke")

        def kill(self) -> None:
            self.killed = True

        def wait(self, *, timeout: int) -> None:
            assert timeout == 2
            self.waited = True

    process = BrokenProcess()
    monkeypatch.setattr(
        pi_module.subprocess,
        "Popen",
        popen_factory(process, expected_command=["pi"], expected_cwd=tmp_path),
    )
    monkeypatch.setattr(
        pi_module.os,
        "killpg",
        lambda pid, sig: (_ for _ in ()).throw(PermissionError("group signal denied")),
    )

    result = pi_module._run_process(["pi"], input_text="prompt", timeout_seconds=4, cwd=tmp_path)

    assert result == PiProcessResult(127, b"", b"pipe broke", False)
    assert process.killed is True
    assert process.waited is True


@pytest.mark.parametrize("cleanup_error", [ProcessLookupError, PermissionError])
def test_run_process_preserves_communication_oserror_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cleanup_error: type[OSError],
) -> None:
    class VanishedProcess:
        pid = 102
        returncode = 1

        def communicate(self, **kwargs: object) -> tuple[bytes, bytes]:
            raise OSError("primary pipe broke")

        def kill(self) -> None:
            raise cleanup_error("cleanup failed")

        def wait(self, *, timeout: int) -> None:
            assert timeout == 2
            raise OSError("wait failed")

    process = VanishedProcess()
    monkeypatch.setattr(
        pi_module.subprocess,
        "Popen",
        popen_factory(process, expected_command=["pi"], expected_cwd=tmp_path),
    )
    monkeypatch.setattr(
        pi_module.os,
        "killpg",
        lambda pid, sig: (_ for _ in ()).throw(PermissionError("group signal denied")),
    )

    result = pi_module._run_process(["pi"], input_text="prompt", timeout_seconds=4, cwd=tmp_path)

    assert result == PiProcessResult(127, b"", b"primary pipe broke", False)


def test_text_blocks_accepts_strings_and_rejects_nonlists() -> None:
    assert pi_module._text_blocks("plain text") == "plain text"
    assert pi_module._text_blocks({"type": "text"}) == ""


def test_normalize_response_preserves_noncanonical_fences() -> None:
    value = "```yaml\n{}\n```"

    assert pi_module._normalize_response(value) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, (None, None, None)),
        ({"cost": "invalid", "input": True, "output": -1}, (None, None, None)),
        ({"cost": float("nan"), "input": -1, "output": 1.5}, (None, None, None)),
        ({"cost": float("inf"), "input": 1, "output": 0}, (None, 1, 0)),
        ({"cost": 10**1000, "input": 1, "output": 0}, (None, 1, 0)),
    ],
)
def test_usage_rejects_nonmappings_and_invalid_numbers(
    value: object, expected: tuple[float | None, int | None, int | None]
) -> None:
    assert pi_module._usage(value) == expected


def test_persisted_response_skips_malformed_events_and_reads_agent_end_messages() -> None:
    events = [
        b"not json",
        json.dumps([]).encode(),
        json.dumps({"type": "session", "id": "session-7"}).encode(),
        json.dumps({"type": "agent_end", "messages": None}).encode(),
        json.dumps({"type": "message_end", "message": None}).encode(),
        json.dumps({"type": "message_end", "message": {"role": "user"}}).encode(),
        json.dumps(
            {
                "type": "agent_end",
                "messages": [
                    {"role": "user"},
                    {
                        "role": "assistant",
                        "provider": "provider",
                        "model": "provider/model",
                        "usage": {"cost": 0.25, "input": 2, "output": 3},
                        "content": [{"type": "text", "text": "answer"}],
                    },
                ],
            }
        ).encode(),
    ]

    persisted = pi_module._persisted_response(b"\n".join(events), "requested/model", 42)

    assert persisted is not None
    assert persisted.conversation_id == "session-7"
    assert persisted.model == "provider/model"
    assert persisted.provider == "provider"
    assert persisted.cost_usd == 0.25
    assert persisted.response == "answer"


def test_pi_transport_includes_files_in_packet(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("print('source')\n")
    runner = FakeRunner(assistant_stream('{"verdict":"approved","findings":[]}'))

    execution = PiTransport(run_process=runner).execute(replace(request(tmp_path), files=(source,)))

    assert execution.response is not None
    assert "--- FILE source.py ---" in runner.last_stdin
    assert "print('source')" in runner.last_stdin


def test_pi_transport_reports_default_nonzero_diagnostic(tmp_path: Path) -> None:
    def fail_process(command, *, input_text, timeout_seconds, cwd):
        return PiProcessResult(2, b"", b"process failed", False)

    execution = PiTransport(run_process=fail_process).execute(request(tmp_path))

    assert execution.response is None
    assert execution.diagnostic == "process failed"
