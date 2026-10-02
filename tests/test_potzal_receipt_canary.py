"""Opt-in real Potzal transport canary; no provider or shared deployment is used."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reviewctl.cli import canonical_json


@pytest.mark.live
def test_real_potzal_receipts_between_two_checkouts(tmp_path: Path) -> None:
    binary = os.environ.get("REVIEWCTL_TEST_POTZ_BINARY")
    if not binary:
        pytest.skip("set REVIEWCTL_TEST_POTZ_BINARY to an explicitly built local potz binary")
    store_root = tmp_path / "store"
    store_root.mkdir()
    subprocess.run([binary, "init", str(store_root)], check=True, capture_output=True)
    env = {key: os.environ[key] for key in ("PATH", "LANG", "TMPDIR") if key in os.environ}
    env["POTZ_PUBLISH_TOKEN"] = "receipt-canary-local-only"
    env["REVIEWCTL_CANARY_TOKEN"] = env["POTZ_PUBLISH_TOKEN"]
    log = tmp_path / "server.log"
    with log.open("w") as stream:
        daemon = subprocess.Popen(
            [binary, "serve", "--port", "0"],
            cwd=store_root,
            env=env,
            stdout=stream,
            stderr=stream,
        )
    try:
        endpoint = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            match = re.search(r"listening on (http://127\.0\.0\.1:\d+)", log.read_text())
            if match:
                endpoint = match.group(1)
                break
            if daemon.poll() is not None:
                pytest.fail("local Potzal daemon exited before listening")
            time.sleep(0.05)
        assert endpoint is not None, "local Potzal daemon did not listen within 15 seconds"
        projects = [tmp_path / "first", tmp_path / "second", tmp_path / "unrelated"]
        for index, project in enumerate(projects):
            project.mkdir()
            project_id = "canary-project" if index < 2 else "unrelated-project"
            (project / "reviewctl.toml").write_text(
                f'[project]\nid = "{project_id}"\n'
                '[profiles.default]\nroutes = []\nexecution = "local"\n'
                f'[evidence.potzal]\nendpoint = "{endpoint}"\n'
                'namespace = "reviews"\ntoken_env = "REVIEWCTL_CANARY_TOKEN"\n'
            )
        # A synthetic legacy V1 fixture tests byte storage, not model acceptance.
        receipt = {"reviewId": "storage-canary", "result": "unavailable"}
        receipt["sha256"] = hashlib.sha256(canonical_json(receipt)).hexdigest()
        original = json.dumps(receipt, indent=2).encode() + b"\n"
        source = tmp_path / "receipt.json"
        source.write_bytes(original)

        def command(*args, command_env=env, expected=0):
            result = subprocess.run(
                [sys.executable, "-m", "reviewctl", *args],
                env=command_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == expected, result.stderr
            return json.loads(result.stdout)

        pushed = command(
            "receipts", "push", str(source), "--project", str(projects[0]), "--format", "json"
        )
        digest = pushed["objectDigest"]
        retry = command(
            "receipts", "push", str(source), "--project", str(projects[0]), "--format", "json"
        )
        assert retry["objectDigest"] == digest
        pulled = command(
            "receipts", "pull", digest, "--project", str(projects[1]), "--format", "json"
        )
        local = Path(pulled["receipt"])
        assert local.read_bytes() == original
        assert command("verify", str(local))["valid"] is True
        assert (
            command("journal", "verify", "--project", str(projects[1]), "--format", "json")["valid"]
            is True
        )
        mismatch = command(
            "receipts",
            "pull",
            digest,
            "--project",
            str(projects[2]),
            "--format",
            "json",
            expected=5,
        )
        assert mismatch["diagnostic"]["code"] == "receipt_invalid"
        denied_env = {**env, "REVIEWCTL_CANARY_TOKEN": "wrong-local-only-token"}
        denied = command(
            "receipts",
            "pull",
            digest,
            "--project",
            str(projects[1]),
            "--format",
            "json",
            command_env=denied_env,
            expected=4,
        )
        assert denied["diagnostic"]["code"] == "evidence_store_denied"
        print(
            json.dumps(
                {
                    "canary": "potzal-native-receipt-storage",
                    "result": "passed",
                    "objectDigest": digest,
                    "receiptBytesSha256": hashlib.sha256(original).hexdigest(),
                    "identicalBytes": True,
                    "retry": True,
                    "wrongProjectRejected": True,
                    "wrongCredentialRejected": True,
                    "providerCalls": 0,
                },
                sort_keys=True,
            )
        )
    finally:
        daemon.terminate()
        daemon.wait(timeout=10)
