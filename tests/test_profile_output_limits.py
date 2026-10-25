"""The legacy run interface must not silently discard configured output limits."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from reviewctl import cli


@pytest.mark.parametrize(
    ("profile_limit", "default_limit", "explicit_limit", "expected", "use_profile"),
    [
        (8192, None, None, 8192, True),
        (8192, 4096, None, 8192, True),
        (8192, 4096, 512, 512, True),
        (8192, 4096, cli.DEFAULT_MAX_OUTPUT_TOKENS, cli.DEFAULT_MAX_OUTPUT_TOKENS, True),
        (None, 4096, None, 4096, True),
        (None, None, None, cli.DEFAULT_MAX_OUTPUT_TOKENS, True),
        (None, 4096, None, 4096, False),
        (None, None, None, cli.DEFAULT_MAX_OUTPUT_TOKENS, False),
    ],
)
def test_run_resolves_requested_output_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile_limit: int | None,
    default_limit: int | None,
    explicit_limit: int | None,
    expected: int,
    use_profile: bool,
) -> None:
    observed = []
    registry = cli.BackendRegistry()

    def execute(request):
        observed.append(request.max_output_tokens)
        return cli.BackendExecution(
            0,
            "",
            cli.PersistedResponse(
                "conversation", None, 1, 2, request.model, 3, None, "VERDICT: approved"
            ),
            cli.BackendEvidence(),
        )

    registry.register(cli.build_backend_registry().require("llm").descriptor, execute)
    monkeypatch.setattr(cli, "build_backend_registry", lambda: registry)
    config = tmp_path / "reviewctl.toml"
    text = '[profiles.code]\nroutes = ["llm:accepted"]\n'
    if profile_limit is not None:
        text += f"max_output_tokens = {profile_limit}\n"
    if default_limit is not None:
        text += f"[defaults.llm]\nmax_output_tokens = {default_limit}\n"
    config.write_text(text)
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    args = [
        "run",
        "--profile" if use_profile else "--route",
        "code" if use_profile else "llm:accepted",
        "--config",
        str(config),
        "--review-id",
        "configured-output",
        "--prompt",
        "Review this synthetic file.",
        "--file",
        str(source),
        "--source-class",
        "synthetic",
        "--artifact-root",
        str(tmp_path / "artifacts"),
    ]
    if explicit_limit is not None:
        args += ["--max-output-tokens", str(explicit_limit)]
    parser = cli.build_parser()
    namespace = parser.parse_args(args)
    assert namespace.handler(namespace) == 0
    assert observed == [expected]
    receipt = json.loads((Path(capsys.readouterr().out.strip()) / "receipt.json").read_text())
    if profile_limit is not None:
        assert receipt["routeProfile"]["settings"]["max_output_tokens"] == profile_limit


@pytest.mark.parametrize("section", ["profiles.code", "defaults.llm"])
@pytest.mark.parametrize("value", ["0", "-1", "true", '"8192"'])
def test_profile_output_limit_rejects_invalid_values_before_execution(
    tmp_path: Path, section: str, value: str
) -> None:
    config = tmp_path / "reviewctl.toml"
    text = '[profiles.code]\nroutes = ["llm:accepted"]\n'
    if section != "profiles.code":
        text += f"[{section}]\n"
    config.write_text(text + f"max_output_tokens = {value}\n")
    with pytest.raises(SystemExit) as error:
        cli.load_route_profile(argparse.ArgumentParser(), str(config), "code")
    assert error.value.code == 2


@pytest.mark.parametrize("command", ["run", "transport-canary"])
@pytest.mark.parametrize("section", ["profiles.code", "defaults.llm"])
@pytest.mark.parametrize("explicit_limit", [None, 512])
def test_invalid_configured_limit_creates_no_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    section: str,
    explicit_limit: int | None,
) -> None:
    config = tmp_path / "reviewctl.toml"
    text = '[profiles.code]\nroutes = ["llm:accepted"]\n'
    if section != "profiles.code":
        text += f"[{section}]\n"
    config.write_text(text + "max_output_tokens = 0\n")
    artifacts = tmp_path / "artifacts"
    parser = cli.build_parser()
    args = [
        command,
        "--profile",
        "code",
        "--config",
        str(config),
        "--artifact-root",
        str(artifacts),
    ]
    if command == "run":
        source = tmp_path / "source.py"
        source.write_text("pass\n")
        args += ["--review-id", "invalid-config", "--prompt", "Synthetic.", "--file", str(source)]
    if explicit_limit is not None:
        args += ["--max-output-tokens", str(explicit_limit)]
    namespace = parser.parse_args(args)

    def no_backend():
        pytest.fail("invalid configuration reached backend construction")

    monkeypatch.setattr(cli, "build_backend_registry", no_backend)
    with pytest.raises(SystemExit) as error:
        namespace.handler(namespace)
    assert error.value.code == 2
    assert not artifacts.exists()


@pytest.mark.parametrize("value", [0, True, "8192", None])
def test_programmatic_invalid_output_limit_fails_before_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    config = tmp_path / "reviewctl.toml"
    config.write_text('[profiles.code]\nroutes = ["llm:accepted"]\n')
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    artifacts = tmp_path / "artifacts"
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "run",
            "--profile",
            "code",
            "--config",
            str(config),
            "--review-id",
            "invalid-request",
            "--prompt",
            "Synthetic.",
            "--file",
            str(source),
            "--artifact-root",
            str(artifacts),
        ]
    )
    # Existing callers may construct a Namespace without argparse provenance.
    del args._explicit_output_limit
    args.max_output_tokens = value

    def no_backend():
        pytest.fail("invalid request reached backend construction")

    monkeypatch.setattr(cli, "build_backend_registry", no_backend)
    with pytest.raises(SystemExit) as error:
        args.handler(args)
    assert error.value.code == 2
    assert not artifacts.exists()


@pytest.mark.parametrize("value", ["0", "-1"])
def test_run_rejects_nonpositive_explicit_output_limit(value: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(
            [
                "run",
                "--review-id",
                "invalid-limit",
                "--prompt",
                "Synthetic.",
                "--model",
                "accepted",
                "--max-output-tokens",
                value,
            ]
        )
    assert error.value.code == 2
