"""Formal repository review contracts exercised with a local Pi executable."""

import json
import subprocess
from pathlib import Path

import pytest
from test_run import make_range_cli_repository, run_cli, write_fake_pi

from reviewctl import cli

pytestmark = pytest.mark.contract


def arguments(tmp_path, repository, binary):
    return [
        "run",
        "--review-id",
        "repo-review",
        "--prompt",
        "Review the repository",
        "--transport",
        "pi",
        "--model",
        "openrouter/accepted",
        "--cwd",
        str(repository),
        "--pi-bin",
        str(binary),
        "--artifact-root",
        str(tmp_path / "artifacts"),
        "--response-contract",
        "findings-json",
        "--max-attempts",
        "1",
    ]


def test_repository_receipt_and_live_verification(tmp_path):
    repository, _, head = make_range_cli_repository(tmp_path)
    binary = write_fake_pi(tmp_path)
    log = tmp_path / "argv.json"
    result = run_cli(*arguments(tmp_path, repository, binary), env={"PI_ARGUMENTS_LOG": str(log)})
    assert result.returncode == 0, result.stderr
    receipt_path = Path(result.stdout.strip()) / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    context = receipt["extension.repositoryReview"]
    assert context["head"] == head
    assert context["clean"] is True
    assert context["tools"] == ["read", "grep", "find"]
    assert context["cwd"] == str(repository)
    assert receipt["source"]["git"]["head"] == head
    argv = json.loads(log.read_text())
    assert argv[argv.index("--tools") + 1] == "read,grep,find"
    assert "--no-tools" not in argv
    request = json.loads(Path(receipt["attempts"][0]["evidence"]["request"]).read_text())
    assert request["cwd"] == str(repository)
    assert request["tools"] == context["tools"]
    assert (
        run_cli(
            "verify", str(receipt_path), "--cwd", str(repository), "--expect-sha", head
        ).returncode
        == 0
    )
    assert run_cli("verify", str(receipt_path), "--expect-sha", "0" * 40).returncode == 1
    (repository / "ledger.txt").write_text("dirty\n")
    assert (
        run_cli(
            "verify", str(receipt_path), "--cwd", str(repository), "--expect-sha", head
        ).returncode
        == 1
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "next"], check=True)
    assert (
        run_cli(
            "verify", str(receipt_path), "--cwd", str(repository), "--expect-sha", head
        ).returncode
        == 1
    )


@pytest.mark.parametrize("tools", ["bash", "write", "edit", "read,bash", "", "read,unknown"])
def test_run_rejects_unsafe_tools(tmp_path, tools):
    repository, _, _ = make_range_cli_repository(tmp_path)
    result = run_cli(*arguments(tmp_path, repository, write_fake_pi(tmp_path)), "--tools", tools)
    assert result.returncode == 2
    assert "read-only" in result.stderr


def test_run_rejects_dirty_repository(tmp_path):
    repository, _, _ = make_range_cli_repository(tmp_path)
    (repository / "untracked").write_text("dirty")
    result = run_cli(*arguments(tmp_path, repository, write_fake_pi(tmp_path)))
    assert result.returncode == 2
    assert "clean" in result.stderr


@pytest.mark.parametrize("error_type", [OSError, subprocess.SubprocessError])
def test_pi_process_exception_redacts_actual_api_key(tmp_path, monkeypatch, error_type):
    secret = "custom-api-credential-without-recognizable-prefix"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)

    def fail(*args, **kwargs):
        raise error_type(secret)

    monkeypatch.setattr(cli.subprocess, "Popen", fail)
    result = cli.invoke_pi(
        pi_bin="omp",
        prompt="review",
        model="openrouter/accepted",
        files=[],
        max_output_tokens=100,
        response_contract="findings-json",
        timeout_seconds=1,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "events",
        session_path=tmp_path / "session",
        diagnostic_path=tmp_path / "stderr",
    )
    assert secret not in result[1]


def test_nested_repository_files_and_openrouter_model_resolution(tmp_path):
    repository, _, _ = make_range_cli_repository(tmp_path)
    (repository / "src").mkdir()
    (repository / "src" / "ledger.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "nested"], check=True)
    args = arguments(tmp_path, repository, write_fake_pi(tmp_path))
    args[args.index("openrouter/accepted")] = "meta/muse-spark-1.3-contributor"
    result = run_cli(*args)
    assert result.returncode == 0, result.stderr
    receipt_path = Path(result.stdout.strip()) / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["model"]["resolved"] == "openrouter/meta/muse-spark-1.3-contributor"
    assert "src/ledger.py" in [file["name"] for file in receipt["source"]["files"]]
    assert run_cli("verify", str(receipt_path)).returncode == 0
    request = Path(receipt["attempts"][0]["evidence"]["request"])
    request.write_text("tampered")
    assert run_cli("verify", str(receipt_path)).returncode == 1


def test_run_detects_repository_mutation(tmp_path):
    repository, _, _ = make_range_cli_repository(tmp_path)
    result = run_cli(
        *arguments(tmp_path, repository, write_fake_pi(tmp_path)), env={"PI_LARGE_STATE": "1"}
    )
    assert result.returncode != 0
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["result"] == "unavailable"
    assert "repository changed" in receipt["attempts"][0]["diagnostic"]


@pytest.mark.parametrize(
    "mutation", ["tools", "model", "modelboth", "packet", "evidence", "prompt", "response"]
)
def test_resigned_inconsistent_repository_receipt_is_rejected(tmp_path, mutation):
    repository, _, _ = make_range_cli_repository(tmp_path)
    result = run_cli(*arguments(tmp_path, repository, write_fake_pi(tmp_path)))
    assert result.returncode == 0, result.stderr
    receipt_path = Path(result.stdout.strip()) / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    if mutation == "tools":
        receipt["extension.repositoryReview"]["tools"] = ["bash"]
    elif mutation == "model":
        receipt["model"]["resolved"] = "openrouter/wrong"
    elif mutation == "modelboth":
        receipt["model"]["resolved"] = "openrouter/wrong"
        receipt["attempts"][0]["model"]["resolved"] = "openrouter/wrong"
    elif mutation == "packet":
        receipt["attempts"][0]["attemptRequestSha256"] = "0" * 64
    elif mutation == "evidence":
        del receipt["attempts"][0]["extension.repositoryEvidence"]
    elif mutation == "prompt":
        receipt["prompt"]["sha256"] = "0" * 64
    else:
        receipt["response"]["sha256"] = "0" * 64
    del receipt["sha256"]
    receipt["sha256"] = cli.sha256_bytes(cli.canonical_json(receipt))
    receipt_path.write_bytes(cli.canonical_json(receipt))
    assert run_cli("verify", str(receipt_path)).returncode == 1


def test_repository_relative_findings_remain_bounded():
    from reviewctl.contracts import ContractContext, EvaluationStatus, get_contract

    contract = get_contract("findings-json")
    context = ContractContext(file_names=("src/ledger.py",))
    prepared = contract.prepare(context)
    finding = {
        "severity": "high",
        "path": "src/ledger.py",
        "line": 1,
        "title": "Defect",
        "evidence": "line",
        "reproduction": "run",
    }
    payload = {"verdict": "changes-requested", "findings": [finding]}
    assert (
        contract.evaluate(json.dumps(payload), prepared, context).status
        == EvaluationStatus.COMPLETE
    )
    for path in (
        "../ledger.py",
        "/etc/passwd",
        "src//ledger.py",
        "other/ledger.py",
        "src/./ledger.py",
    ):
        finding["path"] = path
        assert (
            contract.evaluate(json.dumps(payload), prepared, context).status
            != EvaluationStatus.COMPLETE
        )


def test_run_rejects_artifacts_inside_reviewed_repository(tmp_path):
    repository, _, _ = make_range_cli_repository(tmp_path)
    args = arguments(tmp_path, repository, write_fake_pi(tmp_path))
    args[args.index(str(tmp_path / "artifacts"))] = str(repository / "artifacts")
    result = run_cli(*args)
    assert result.returncode == 2
    assert not (repository / "artifacts").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"clean": False},
        {"clean": 1},
        {"head": "bad"},
        {"head": 3},
        {"cwd": ""},
        {"repositoryRoot": None},
        {"tools": []},
        {"tools": "read"},
        {"tools": [3]},
        {"tools": ["read", "read"]},
        {"tools": ["write"]},
    ],
)
def test_repository_provenance_rejects_malformed_claims(change):
    from reviewctl.repository_review import repository_receipt_violations

    context = {
        "cwd": "/repo",
        "repositoryRoot": "/repo",
        "head": "a" * 40,
        "clean": True,
        "tools": ["read"],
    }
    receipt = {
        "extension.repositoryReview": context | change,
        "transport": "pi",
        "source": {"git": {"head": "a" * 40}},
    }
    assert repository_receipt_violations(receipt)


@pytest.mark.parametrize("state", ["empty", "symlink", "unsafe"])
def test_repository_inventory_rejects_unsupported_files(tmp_path, state):
    from reviewctl.repository_review import repository_sources, repository_state

    repository, _, _ = make_range_cli_repository(tmp_path)
    if state == "empty":
        subprocess.run(["git", "-C", str(repository), "rm", "-q", "ledger.txt"], check=True)
    elif state == "symlink":
        (repository / "link").symlink_to(repository / "ledger.txt")
    else:
        (repository / "bad\nname").write_text("x")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    with pytest.raises(ValueError):
        repository_sources(repository_state(repository))


def test_invalid_repository_and_tools_without_cwd(tmp_path):
    args = arguments(tmp_path, tmp_path, write_fake_pi(tmp_path))
    assert run_cli(*args).returncode == 2
    index = args.index("--cwd")
    del args[index : index + 2]
    assert run_cli(*args, "--tools", "read").returncode == 2
    args.extend(["--cwd", str(tmp_path)])
    repository, _, _ = make_range_cli_repository(tmp_path)
    args[args.index(str(tmp_path))] = str(repository)
    args[args.index("pi")] = "llm"
    assert run_cli(*args).returncode == 2


def test_omp_environment_and_missing_evidence(tmp_path):
    repository, _, head = make_range_cli_repository(tmp_path)
    binary = write_fake_pi(tmp_path).rename(tmp_path / "omp")
    args = arguments(tmp_path, repository, binary)
    index = args.index("--pi-bin")
    del args[index : index + 2]
    result = run_cli(*args, env={"PI_BIN": str(binary)})
    assert result.returncode == 0, result.stderr
    path = Path(result.stdout.strip()) / "receipt.json"
    receipt = json.loads(path.read_text())
    request = Path(receipt["attempts"][0]["evidence"]["request"])
    assert json.loads(request.read_text())["command"] == str(binary)
    session = Path(receipt["attempts"][0]["evidence"]["session"])
    assert json.loads(session.read_text().splitlines()[0])["cwd"] == str(repository)
    request.unlink()
    assert run_cli("verify", str(path), "--expect-sha", head).returncode == 1


def test_live_verify_requires_repository_provenance(tmp_path):
    from test_run import review_arguments

    result = run_cli(
        *review_arguments(tmp_path),
        "--route",
        "pi:openrouter/accepted",
        "--response-contract",
        "findings-json",
        env={"PI_BIN": str(write_fake_pi(tmp_path))},
    )
    assert result.returncode == 0, result.stderr
    path = Path(result.stdout.strip()) / "receipt.json"
    assert run_cli("verify", str(path), "--expect-sha", "a" * 40).returncode == 1


def test_omp_uses_supported_native_flags(tmp_path):
    repository, _, _ = make_range_cli_repository(tmp_path)
    binary = write_fake_pi(tmp_path).rename(tmp_path / "omp")
    payload_path = tmp_path / ".pi.py"
    source = payload_path.read_text()
    source = source.replace(
        "arguments = sys.argv[1:]",
        """arguments = sys.argv[1:]
assert not set(arguments) & {'--no-prompt-templates', '--no-context-files', '--no-approve'}
assert {'--no-lsp', '--no-pty', '--no-rules', '--no-title', '--no-prewalk'} <= set(arguments)
""",
    )
    payload_path.write_text(source)
    result = run_cli(*arguments(tmp_path, repository, binary))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "attempts",
    [
        None,
        [None],
        [{"evidence": None}],
        [
            {
                "evidence": {"request": "/x"},
                "extension.repositoryEvidence": {"request": {"path": "/x", "sha256": "bad"}},
            }
        ],
    ],
)
def test_repository_evidence_shape_rejected_offline(attempts):
    from reviewctl.repository_review import repository_receipt_violations

    context = {
        "cwd": "/repo",
        "repositoryRoot": "/repo",
        "head": "a" * 40,
        "clean": True,
        "tools": ["read"],
    }
    receipt = {
        "extension.repositoryReview": context,
        "transport": "pi",
        "source": {"git": {"head": "a" * 40}},
        "attempts": attempts,
    }
    assert repository_receipt_violations(receipt) == ("repository-evidence",)


def test_unavailable_repository_evidence_and_request_cross_check(tmp_path):
    from reviewctl.repository_review import repository_evidence_violations

    prompt = tmp_path / "prompt.txt"
    prompt.write_text("prompt")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {"cwd": "/repo", "tools": ["read"], "prompt": "prompt", "model": "openrouter/accepted"}
        )
    )
    receipt = {
        "extension.repositoryReview": {"cwd": "/repo", "tools": ["read"]},
        "extension.repositoryPrompt": {"path": str(prompt)},
        "prompt": {"sha256": cli.sha256_bytes(prompt.read_bytes())},
        "attempts": [{"evidence": {}, "extension.repositoryEvidence": {}}],
        "acceptedAttempt": None,
    }
    assert repository_evidence_violations(receipt) == ()
    receipt["attempts"][0]["attemptRequestSha256"] = cli.sha256_bytes(b"prompt")
    receipt["attempts"][0]["model"] = {"requested": "openrouter/accepted"}
    receipt["attempts"][0]["evidence"]["request"] = str(request)
    assert repository_evidence_violations(receipt) == ()
    request.write_text(
        json.dumps(
            {"cwd": "/other", "tools": ["read"], "prompt": "prompt", "model": "openrouter/accepted"}
        )
    )
    assert repository_evidence_violations(receipt) == ("repository-request",)
    del receipt["extension.repositoryPrompt"]
    assert repository_evidence_violations(receipt) == ("repository-evidence",)


def test_repository_output_guard_and_inventory_error(tmp_path):
    repository, _, _ = make_range_cli_repository(tmp_path)
    binary = write_fake_pi(tmp_path)
    result = run_cli(
        *arguments(tmp_path, repository, binary), "--output-file", str(repository / "output.md")
    )
    assert result.returncode == 2
    (repository / "link").symlink_to(repository / "ledger.txt")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "link"], check=True)
    result = run_cli(*arguments(tmp_path, repository, binary))
    assert result.returncode == 2
    assert "symlinks" in result.stderr


def test_live_verify_missing_checkout(tmp_path):
    repository, _, _ = make_range_cli_repository(tmp_path)
    result = run_cli(*arguments(tmp_path, repository, write_fake_pi(tmp_path)))
    path = Path(result.stdout.strip()) / "receipt.json"
    assert run_cli("verify", str(path), "--cwd", str(tmp_path / "missing")).returncode == 1
