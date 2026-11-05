"""Offline native smoke checks; fixtures are generated locally without credentials."""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def validate_linux_trace(trace_text, binary):
    """Require successful reads of bundled Python and reject external Python files."""
    assert "/.venv/" not in trace_text
    assert "/source/" not in trace_text
    assert "connect(" not in trace_text
    reads = re.findall(r'openat\([^,]+, "([^"]+)"[^\n]*O_RDONLY[^\n]*\)\s*=\s*\d+', trace_text)
    python_paths = [path for path in reads if "libpython" in Path(path).name]
    assert python_paths, "no successful embedded libpython read in trace"
    assert all("/_MEI" in path for path in python_paths), python_paths
    assert any("/_MEI" in path and path.endswith("/base_library.zip") for path in reads)
    for path in reads:
        if re.search(r"/python3(?:\.\d+|\d+\.zip)(?:/|$)", path) or Path(path).suffix in {
            ".py",
            ".pyc",
            ".pyo",
        }:
            assert "/_MEI" in path, f"external Python standard library: {path}"
    for line in trace_text.splitlines():
        if "execve(" in line:
            assert f'execve("{binary}"' in line, line


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--require-trace", action="store_true")
    args = parser.parse_args()
    strace = shutil.which("strace") if sys.platform.startswith("linux") else None
    if args.require_trace and sys.platform.startswith("linux") and not strace:
        parser.error("strace is required for Linux native dependency verification")
    if args.require_trace and not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        parser.error("required native tracing is supported on Linux and macOS only")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    binary_dir = output / "relocated"
    binary_dir.mkdir()
    binary = binary_dir / args.binary.name
    shutil.copy2(args.binary, binary)
    for name in ("home", "empty-path", "tmp", "unrelated-cwd", "poison-python"):
        (output / name).mkdir()
    environment = {
        "HOME": str(output / "home"),
        "PATH": str(output / "empty-path"),
        "TMPDIR": str(output / "tmp"),
        "LANG": "C.UTF-8",
        "PYTHONHOME": str(output / "poison-python"),
        "PYTHONPATH": str(output / "poison-python"),
    }
    if os.name == "nt":
        environment["SystemRoot"] = os.environ["SystemRoot"]
    receipt = {"reviewId": "native-offline-fixture", "result": "synthetic", "findings": []}
    canonical = json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    receipt["sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    valid = output / "valid.json"
    valid.write_text(json.dumps(receipt))
    receipt["reviewId"] = "tampered"
    tampered = output / "tampered.json"
    tampered.write_text(json.dumps(receipt))
    missing_hash = output / "missing-hash.json"
    missing_hash.write_text('{"reviewId": "synthetic"}')
    policy = output / "policy.toml"
    policy.write_text(
        '[models."fixture/allowed"]\nsource_allowed = true\n'
        '[models."fixture/denied"]\nsource_allowed = false\n'
    )
    results = []

    def check(name, arguments, expected_code, expected_valid=None, cwd=None, env=None):
        result = subprocess.run(
            [str(binary), *arguments],
            env=environment if env is None else env,
            cwd=cwd or output / "unrelated-cwd",
            capture_output=True,
            text=True,
        )
        assert result.returncode == expected_code, (name, result.returncode, result.stderr)
        if expected_valid is not None:
            assert json.loads(result.stdout)["valid"] is expected_valid, result.stdout
        results.append(
            {
                "name": name,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )

    check("version", ["--version"], 0)
    assert results[-1]["stdout"].strip() == f"reviewctl {args.expected_version}"
    check("help", ["--help"], 0)
    assert "verify" in results[-1]["stdout"]
    check("valid-receipt-unrelated-cwd", ["verify", str(valid)], 0, True)
    check("valid-receipt-filesystem-root-cwd", ["verify", str(valid)], 0, True, Path("/"))
    check("tampered-receipt-rejected", ["verify", str(tampered)], 1, False)
    check("missing-hash-rejected", ["verify", str(missing_hash)], 1, False)
    check(
        "policy-allowed",
        ["policy-check", "--policy", str(policy), "--model", "fixture/allowed", "--enforce"],
        0,
    )
    check(
        "policy-denied",
        ["policy-check", "--policy", str(policy), "--model", "fixture/denied", "--enforce"],
        3,
    )
    check("agent-guidance-json", ["help-llm", "--format", "json"], 0)
    assert isinstance(json.loads(results[-1]["stdout"]), dict)
    project = output / "project-fixture"
    check("project-init-without-transports", ["init", "--project", str(project)], 0)
    assert (project / "reviewctl.toml").is_file()
    check("project-findings-empty", ["findings", "--project", str(project), "--format", "json"], 0)
    patch = (
        b"diff --git a/fixture.txt b/fixture.txt\n--- a/fixture.txt\n+++ b/fixture.txt\n"
        b"@@ -1 +1 @@\n-before\n+after\n"
    )
    patch_sha = hashlib.sha256(patch).hexdigest()
    manifest = output / "range-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "status": "manifest-created",
                "evidenceStatus": "planning-only",
                "repository": str(project),
                "baseSha": "a" * 40,
                "headSha": "b" * 40,
                "mergeBaseSha": "a" * 40,
                "comparison": f"{'a' * 40}..{'b' * 40}",
                "contextLines": 3,
                "chunkingVersion": "file-sections-v1",
                "canonicalDiffSha256": patch_sha,
                "chunkCount": 1,
                "chunks": [
                    {
                        "index": 0,
                        "patchSha256": patch_sha,
                        "byteLength": len(patch),
                        "fileCount": 1,
                        "paths": ["fixture.txt"],
                        "patch": base64.b64encode(patch).decode(),
                    }
                ],
            }
        )
    )
    aggregate = output / "range-aggregate.json"
    git = shutil.which("git")
    assert git, "the formal range smoke requires Git for source provenance"
    git_directory = output / "git-only-path"
    git_directory.mkdir()
    shutil.copy2(git, git_directory / Path(git).name)
    formal_environment = {
        **environment,
        "PATH": str(git_directory),
        "PI_BIN": str(output / "missing-pi"),
    }
    check(
        "formal-range-missing-backend",
        [
            "range-review",
            "--manifest",
            str(manifest),
            "--review-id",
            "native-range",
            "--prompt",
            "Review only this synthetic fixture.",
            "--model",
            "fixture/missing",
            "--transport",
            "pi",
            "--source-class",
            "synthetic",
            "--artifact-root",
            str(output / "formal-artifacts"),
            "--aggregate-output",
            str(aggregate),
            "--timeout-seconds",
            "5",
            "--max-attempts",
            "1",
        ],
        1,
        env=formal_environment,
    )
    formal = json.loads(aggregate.read_text())
    assert formal["result"] == "incomplete"
    assert formal["aggregate"]["approved"] is False
    chunk = formal["chunks"][0]
    assert chunk["receipt"], "frozen child must persist an unavailable receipt"
    assert chunk["result"] == "unavailable", chunk
    check("formal-unavailable-receipt-integrity", ["verify", chunk["receipt"]], 0, True)
    traced = False
    loader_traced = False
    if sys.platform.startswith("linux") and strace:
        trace = output / "verify.strace"
        result = subprocess.run(
            [
                strace,
                "-f",
                "-e",
                "trace=execve,openat,network",
                "-o",
                str(trace),
                str(binary),
                "verify",
                str(valid),
            ],
            env=environment,
            cwd=output / "unrelated-cwd",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        trace_text = trace.read_text()
        validate_linux_trace(trace_text, binary)
        traced = True
    if sys.platform == "darwin":
        loader_environment = {**environment, "DYLD_PRINT_LIBRARIES": "1"}
        result = subprocess.run(
            [str(binary), "verify", str(valid)],
            env=loader_environment,
            cwd=output / "unrelated-cwd",
            capture_output=True,
            text=True,
        )
        (output / "dyld-libraries.txt").write_text(result.stderr)
        assert result.returncode == 0, result.stderr
        assert any(
            "/_MEI" in line and ("Python" in line or "libpython" in line)
            for line in result.stderr.splitlines()
        ), "embedded Python loader trace unavailable"
        assert "/opt/homebrew/" not in result.stderr
        assert "/.venv/" not in result.stderr
        assert "/source/" not in result.stderr
        loader_traced = True
    report = {
        "passed": len(results),
        "straceChecked": traced,
        "loaderTraceChecked": loader_traced,
        "checks": results,
        "environment": environment,
        "formalEnvironment": formal_environment,
        "cwdIndependent": True,
        "scope": "Offline bundle checks only; no provider, API, harness, signing or publishing.",
    }
    (output / "smoke-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": len(results), "straceChecked": traced}))


if __name__ == "__main__":
    main()
