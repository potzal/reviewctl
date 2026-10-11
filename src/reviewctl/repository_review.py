"""Read-only Git provenance for formal Pi repository reviews."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from reviewctl.contracts import valid_review_path

ALLOWED_TOOLS = frozenset({"read", "grep", "find", "ls"})
DEFAULT_TOOLS = "read,grep,find"


def read_only_tools(value: str) -> list[str]:
    tools = value.split(",")
    if not tools or any(tool not in ALLOWED_TOOLS for tool in tools):
        raise ValueError("run tools must be read-only: read,grep,find,ls")
    return list(dict.fromkeys(tools))


def repository_state(cwd: Path) -> dict[str, object]:
    def git(*args: str) -> str:
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
        if result.returncode:
            raise ValueError("--cwd must be a Git worktree with HEAD")
        return result.stdout.strip()

    root = git("rev-parse", "--show-toplevel")
    head = git("rev-parse", "HEAD")
    clean = not git("status", "--porcelain", "--untracked-files=all")
    return {"cwd": str(cwd), "repositoryRoot": root, "head": head, "clean": clean}


def repository_sources(state: dict[str, object]) -> list[dict[str, str]]:
    root = Path(str(state["repositoryRoot"]))
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    )
    files = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        name = raw.decode("utf-8")
        if not valid_review_path(name):
            raise ValueError("repository paths must be safe printable relative paths")
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("repository review does not support symlinks or submodules")
        files.append(
            {
                "name": name,
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if not files:
        raise ValueError("repository review requires tracked files")
    return sorted(files, key=lambda file: file["name"])


def repository_receipt_violations(receipt: dict) -> tuple[str, ...]:
    context = receipt.get("extension.repositoryReview")
    if "extension.repositoryReview" not in receipt:
        return ()
    if (
        type(context) is not dict
        or set(context) != {"cwd", "repositoryRoot", "head", "clean", "tools"}
        or any(
            type(context.get(key)) is not str or not context[key]
            for key in ("cwd", "repositoryRoot", "head")
        )
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", context["head"])
        or context["clean"] is not True
        or type(context["tools"]) is not list
        or not context["tools"]
        or any(type(tool) is not str or tool not in ALLOWED_TOOLS for tool in context["tools"])
        or len(set(context["tools"])) != len(context["tools"])
        or receipt.get("transport") != "pi"
        or type(receipt.get("source")) is not dict
        or type(receipt["source"].get("git")) is not dict
        or receipt["source"]["git"].get("head") != context["head"]
    ):
        return ("repository-review",)
    attempts = receipt.get("attempts")
    if type(attempts) is not list:
        return ("repository-evidence",)
    for attempt in attempts:
        if type(attempt) is not dict or type(attempt.get("evidence")) is not dict:
            return ("repository-evidence",)
        recorded = attempt.get("extension.repositoryEvidence")
        expected = {key: value for key, value in attempt["evidence"].items() if value is not None}
        if type(recorded) is not dict or set(recorded) != set(expected):
            return ("repository-evidence",)
        for key, value in recorded.items():
            if (
                type(value) is not dict
                or set(value) != {"path", "sha256"}
                or value["path"] != expected[key]
                or type(value["sha256"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
            ):
                return ("repository-evidence",)
    return ()


def repository_evidence_violations(receipt: dict) -> tuple[str, ...]:
    """Re-read repository request/response artifacts without consulting the checkout."""
    import json

    from reviewctl.filesystem import read_confined_bytes

    context = receipt["extension.repositoryReview"]
    try:
        prompt = receipt["extension.repositoryPrompt"]
        if (
            hashlib.sha256(read_confined_bytes(Path(prompt["path"]))).hexdigest()
            != receipt["prompt"]["sha256"]
        ):
            return ("repository-prompt-digest",)
        for attempt in receipt["attempts"]:
            for evidence in attempt["extension.repositoryEvidence"].values():
                if (
                    hashlib.sha256(read_confined_bytes(Path(evidence["path"]))).hexdigest()
                    != evidence["sha256"]
                ):
                    return ("repository-evidence-digest",)
            request_path = attempt["evidence"].get("request")
            if request_path:
                request = json.loads(read_confined_bytes(Path(request_path)))
                if (
                    hashlib.sha256(request["prompt"].encode()).hexdigest()
                    != attempt["attemptRequestSha256"]
                    or request["model"] != attempt["model"]["requested"]
                ):
                    return ("repository-request",)
                if request.get("cwd") != context["cwd"] or request.get("tools") != context["tools"]:
                    return ("repository-request",)
        number = receipt.get("acceptedAttempt")
        if number is not None:
            attempt = receipt["attempts"][number - 1]
            response = read_confined_bytes(Path(attempt["evidence"]["finalResponse"]))
            from reviewctl.cli import normalize_pi_response, pi_persisted_response

            observed = pi_persisted_response(
                read_confined_bytes(Path(attempt["evidence"]["response"])),
                attempt["model"]["requested"],
                0,
            )
            if (
                observed.model != attempt["model"]["resolved"]
                or normalize_pi_response(observed.response, receipt["reviewContract"]).encode()
                != response
            ):
                return ("repository-response",)

            if (
                hashlib.sha256(response).hexdigest() != receipt["response"]["sha256"]
                or receipt["model"]["resolved"] != attempt["model"]["resolved"]
            ):
                return ("repository-response",)
    except OSError, KeyError, TypeError, ValueError:
        return ("repository-evidence",)
    return ()
