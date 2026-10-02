"""Provider-neutral contracts for bounded GitHub pull-request reviews.

This module is deliberately free of network and model behavior.  The source
and publisher adapters may depend on these contracts, while the review core
keeps ownership of snapshots, finding identity, receipts, and policy.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from reviewctl.errors import Diagnostic, ReviewctlError

try:
    import resource
except ImportError:  # pragma: no cover - exercised on non-POSIX hosts
    resource = None  # type: ignore[assignment]

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_PATH_PART = re.compile(r"^[^/]+$")
_STATUSES = frozenset({"added", "modified", "deleted", "renamed"})
_VISIBILITIES = frozenset({"public", "private", "unknown"})
MAX_GITHUB_FILES = 100
MAX_GITHUB_FILE_BYTES = 2 * 1024 * 1024
MAX_GITHUB_DIFF_BYTES = 8 * 1024 * 1024


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def canonical_sha256(value: object) -> str:
    """Return a stable digest for JSON-safe values."""
    return _sha256_bytes(_canonical_json(value))


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    output_truncated: bool = False


class CommandRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        input_bytes: bytes | None = None,
    ) -> CommandResult: ...


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    input_bytes: bytes | None = None,
) -> CommandResult:
    if resource is None:
        return CommandResult(
            126,
            b"",
            b"bounded command capture unsupported on this platform",
        )
    capture_limit = MAX_GITHUB_DIFF_BYTES + 1

    def limit_output_files() -> None:
        resource.setrlimit(  # pragma: no cover - runs only in the pre-exec child
            resource.RLIMIT_FSIZE, (capture_limit, capture_limit)
        )

    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                stdout=stdout_file,
                stderr=stderr_file,
                check=False,
                input=input_bytes,
                timeout=timeout_seconds,
                preexec_fn=limit_output_files,
            )
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = (
                completed.stdout
                if isinstance(completed.stdout, bytes)
                else stdout_file.read(capture_limit)
            )
            stderr = (
                completed.stderr
                if isinstance(completed.stderr, bytes)
                else stderr_file.read(capture_limit)
            )
    except subprocess.TimeoutExpired:
        return CommandResult(124, b"", b"timeout")
    except OSError:
        return CommandResult(127, b"", b"command unavailable")
    return CommandResult(
        completed.returncode,
        stdout,
        stderr,
        output_truncated=len(stdout) == capture_limit or len(stderr) == capture_limit,
    )


class GitHubSourceError(ReviewctlError):
    """A safe, typed failure while freezing a GitHub pull-request source."""

    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


def _validate_relative_path(path: str) -> str:
    value = path
    if value != value.strip():
        raise ValueError("path must not contain leading or trailing whitespace")
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("path must be a non-empty relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("path must not contain empty, '.' or '..' components")
    if any(not _PATH_PART.fullmatch(part) for part in parts):
        raise ValueError("path contains an invalid component")
    return value


@dataclass(frozen=True)
class PullRequestRef:
    """Stable repository and pull-request identity."""

    repository: str
    number: int

    def __init__(self, repository: str, number: int = 1) -> None:
        normalized = repository.strip().lower()
        if not _REPOSITORY.fullmatch(normalized):
            raise ValueError("repository must have the form owner/name")
        if type(number) is not int or number <= 0:
            raise ValueError("pull request number must be a positive integer")
        object.__setattr__(self, "repository", normalized)
        object.__setattr__(self, "number", number)


@dataclass(frozen=True)
class ChangedFileSnapshot:
    """One bounded text representation selected for review."""

    path: str
    status: str
    content: str
    old_path: str | None = None
    sha256: str = field(init=False)
    size: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _validate_relative_path(self.path))
        if self.status not in _STATUSES:
            raise ValueError(f"unsupported changed-file status: {self.status!r}")
        if not isinstance(self.content, str):
            raise ValueError("changed-file content must be UTF-8 text")
        if self.old_path is not None:
            object.__setattr__(self, "old_path", _validate_relative_path(self.old_path))
        content_bytes = self.content.encode("utf-8")
        object.__setattr__(self, "sha256", _sha256_bytes(content_bytes))
        object.__setattr__(self, "size", len(content_bytes))

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "oldPath": self.old_path,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True)
class PullRequestSnapshot:
    """Immutable, provider-neutral review input identity."""

    ref: PullRequestRef
    base_sha: str
    head_sha: str
    visibility: str
    changed_files: tuple[ChangedFileSnapshot, ...]
    diff: str
    evidence: tuple[str, ...] = ()
    diff_sha256: str = field(init=False)
    snapshot_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for label, value in (("base SHA", self.base_sha), ("head SHA", self.head_sha)):
            if not isinstance(value, str) or not _SHA.fullmatch(value):
                raise ValueError(f"{label} must be a 40-character commit SHA")
        if self.visibility not in _VISIBILITIES:
            raise ValueError(f"unsupported repository visibility: {self.visibility!r}")
        if not isinstance(self.diff, str):
            raise ValueError("pull-request diff must be UTF-8 text")
        files = tuple(self.changed_files)
        if len({item.path for item in files}) != len(files):
            raise ValueError("changed-file paths must be unique")
        if any(not isinstance(item, ChangedFileSnapshot) for item in files):
            raise ValueError("changed_files must contain ChangedFileSnapshot values")
        object.__setattr__(self, "changed_files", files)
        object.__setattr__(self, "base_sha", self.base_sha.lower())
        object.__setattr__(self, "head_sha", self.head_sha.lower())
        diff_sha = _sha256_bytes(self.diff.encode("utf-8"))
        object.__setattr__(self, "diff_sha256", diff_sha)
        object.__setattr__(self, "snapshot_sha256", canonical_sha256(self.to_payload()))

    def to_payload(self) -> dict[str, Any]:
        return {
            "repository": self.ref.repository,
            "pullNumber": self.ref.number,
            "baseSha": self.base_sha,
            "headSha": self.head_sha,
            "visibility": self.visibility,
            "changedFiles": [
                item.to_payload() for item in sorted(self.changed_files, key=lambda x: x.path)
            ],
            "diffSha256": (
                self.diff_sha256 if hasattr(self, "diff_sha256") else canonical_sha256(self.diff)
            ),
            "evidence": list(self.evidence),
        }

    def to_context(self) -> dict[str, Any]:
        """Return source identity without raw diff or file contents."""
        return {
            "kind": "github_pull_request",
            **self.to_payload(),
            "snapshotSha256": self.snapshot_sha256,
        }


@dataclass(frozen=True)
class PublicationTarget:
    path: str
    line: int
    side: str = "RIGHT"

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _validate_relative_path(self.path))
        if type(self.line) is not int or self.line <= 0:
            raise ValueError("publication line must be a positive integer")
        if self.side not in {"RIGHT", "LEFT"}:
            raise ValueError("publication side must be RIGHT or LEFT")

    def to_payload(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "side": self.side}


@dataclass(frozen=True)
class PublicationItem:
    finding_id: str
    marker: str
    body: str
    target: PublicationTarget | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "findingId": self.finding_id,
            "marker": self.marker,
            "body": self.body,
            "target": self.target.to_payload() if self.target else None,
        }


@dataclass(frozen=True)
class ReviewPublicationPlan:
    review_id: str
    repository: str
    pull_number: int
    head_sha: str
    snapshot_sha256: str
    items: tuple[PublicationItem, ...]
    executable: bool
    reason: str | None = None
    plan_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", canonical_sha256(self.to_payload()))

    def to_payload(self) -> dict[str, Any]:
        return {
            "reviewId": self.review_id,
            "repository": self.repository,
            "pullNumber": self.pull_number,
            "headSha": self.head_sha,
            "snapshotSha256": self.snapshot_sha256,
            "items": [item.to_payload() for item in self.items],
            "executable": self.executable,
            "reason": self.reason,
        }


def _safe_text(value: object) -> str:
    return " ".join(str(value).split())


def _diff_added_lines(snapshot: PullRequestSnapshot) -> set[tuple[str, int]]:
    """Return (path, new-line) pairs that are present on the diff's right side."""
    lines: set[tuple[str, int]] = set()
    current_path: str | None = None
    new_line: int | None = None
    in_hunk = False
    saw_diff_header = False
    for raw in snapshot.diff.splitlines():
        if raw.startswith("diff --git "):
            saw_diff_header = True
            in_hunk = False
            current_path = None
            new_line = None
            continue
        if not in_hunk and raw.startswith("+++ "):
            current_path = _diff_path(raw[4:])
            new_line = None
            continue
        if raw.startswith("@@ "):
            match = re.search(r"\+(\d+)(?:,(\d+))?", raw)
            new_line = int(match.group(1)) if match else None
            in_hunk = True
            continue
        if new_line is None:
            continue
        if raw.startswith("+"):
            if current_path is not None:
                lines.add((current_path, new_line))
            new_line += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        else:
            new_line += 1
    if not saw_diff_header and not current_path and len(snapshot.changed_files) == 1:
        only_path = snapshot.changed_files[0].path
        return {(only_path, line) for _, line in lines} if lines else {(only_path, 1)}
    return lines


def _marker(project_id: str, snapshot: PullRequestSnapshot, finding_id: str) -> str:
    return (
        "<!-- reviewctl:project="
        f"{_safe_text(project_id)} repo={snapshot.ref.repository} pr={snapshot.ref.number} "
        f"finding={_safe_text(finding_id)} -->"
    )


def _body(
    marker: str,
    snapshot: PullRequestSnapshot,
    finding: Mapping[str, object],
) -> str:
    path = _safe_text(finding.get("path", "")) or "(project)"
    line = finding.get("line")
    location = f"{path}:{line}" if type(line) is int else path
    severity = _safe_text(finding.get("severity", "info"))
    title = _safe_text(finding.get("title", "Review finding"))
    return f"{marker}\nreviewctl-head: {snapshot.head_sha}\n**{severity}** `{location}` — {title}"


def build_publication_plan(
    snapshot: PullRequestSnapshot,
    *,
    project_id: str,
    review_id: str,
    findings: Sequence[Mapping[str, object]],
    review_status: str,
) -> ReviewPublicationPlan:
    """Build a deterministic, side-effect-free plan from an accepted review."""
    if review_status != "accepted":
        return ReviewPublicationPlan(
            review_id=review_id,
            repository=snapshot.ref.repository,
            pull_number=snapshot.ref.number,
            head_sha=snapshot.head_sha,
            snapshot_sha256=snapshot.snapshot_sha256,
            items=(),
            executable=False,
            reason="review_not_accepted",
        )

    changed_status = {item.path: item.status for item in snapshot.changed_files}
    added_lines = _diff_added_lines(snapshot)
    items: list[PublicationItem] = []
    for finding in findings:
        finding_id = finding.get("findingId")
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise ValueError("accepted finding is missing findingId")
        path = finding.get("path")
        line = finding.get("line")
        target = None
        if (
            isinstance(path, str)
            and type(line) is int
            and path in changed_status
            and changed_status[path] != "deleted"
            and (path, line) in added_lines
        ):
            target = PublicationTarget(path=path, line=line)
        marker = _marker(project_id, snapshot, finding_id)
        items.append(
            PublicationItem(
                finding_id=finding_id,
                marker=marker,
                body=_body(marker, snapshot, finding),
                target=target,
            )
        )
    return ReviewPublicationPlan(
        review_id=review_id,
        repository=snapshot.ref.repository,
        pull_number=snapshot.ref.number,
        head_sha=snapshot.head_sha,
        snapshot_sha256=snapshot.snapshot_sha256,
        items=tuple(items),
        executable=True,
    )


@dataclass(frozen=True)
class _DiffFile:
    path: str
    status: str
    old_path: str | None = None


class _BinaryDiffError(ValueError):
    """The pull-request diff contains content that source review cannot safely materialize."""


class _UnsupportedDiffError(ValueError):
    """The pull-request diff contains an entry without a materializable source path."""


def _decode_git_path(value: str) -> str:
    path = value
    if not path.startswith('"') and not path.endswith('"'):
        return path
    if len(path) < 2 or not path.startswith('"') or not path.endswith('"'):
        raise ValueError("path contains an unterminated quote")
    encoded = path[1:-1]
    decoded = bytearray()
    escapes = {
        "a": 7,
        "b": 8,
        "f": 12,
        "n": 10,
        "r": 13,
        "t": 9,
        "v": 11,
        "\\": 92,
        '"': 34,
    }
    index = 0
    while index < len(encoded):
        character = encoded[index]
        if character != "\\":
            decoded.extend(character.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index == len(encoded):
            raise ValueError("path contains an incomplete quoted escape")
        character = encoded[index]
        if character in escapes:
            decoded.append(escapes[character])
            index += 1
            continue
        if character in "01234567":
            end = index
            while end < len(encoded) and end < index + 3 and encoded[end] in "01234567":
                end += 1
            decoded.append(int(encoded[index:end], 8))
            index = end
            continue
        raise ValueError("path contains an invalid quoted escape")
    try:
        return bytes(decoded).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("path is not valid UTF-8") from error


def _diff_path(value: str, *, side_prefixed: bool = True) -> str | None:
    path = _decode_git_path(value)
    if path == "/dev/null":
        return None
    if side_prefixed and path.startswith(("a/", "b/")):
        path = path[2:]
    return _validate_relative_path(path)


_DIFF_HEADER = re.compile(r'diff --git ("(?:\\.|[^"\\])*"|[^ ]+) ("(?:\\.|[^"\\])*"|[^ ]+)')


def _diff_header_paths(value: str) -> tuple[str, str]:
    match = _DIFF_HEADER.fullmatch(value)
    if match is None:
        raise _UnsupportedDiffError("pull-request diff has a malformed diff --git header")
    return match.group(1), match.group(2)


def _diff_files(diff: str) -> tuple[_DiffFile, ...]:
    if diff and not diff.startswith("diff --git "):
        raise _UnsupportedDiffError("non-empty pull-request diff must begin with diff --git")
    entries: list[_DiffFile] = []
    entry_started = False
    old_path: str | None = None
    new_path: str | None = None
    status = "modified"
    rename_from: str | None = None
    rename_to: str | None = None
    in_hunk = False

    def flush() -> None:
        nonlocal entry_started, old_path, new_path, status, rename_from, rename_to, in_hunk
        if entry_started:
            path = rename_to or new_path or old_path or rename_from or ""
            resolved_status = status
            if path == "/dev/null":
                path = old_path or rename_from or ""
                resolved_status = "deleted"
            entries.append(
                _DiffFile(
                    path=_diff_path(path, side_prefixed=rename_to is None) or path,
                    status=resolved_status,
                    old_path=_diff_path(
                        rename_from or old_path,
                        side_prefixed=rename_from is None,
                    ),
                )
            )
        old_path = None
        new_path = None
        status = "modified"
        rename_from = None
        rename_to = None
        entry_started = False
        in_hunk = False

    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            flush()
            old_path, new_path = _diff_header_paths(raw)
            entry_started = True
        elif raw.startswith("@@ "):
            in_hunk = True
        elif raw.startswith("Binary files ") or raw == "GIT binary patch":
            raise _BinaryDiffError("binary pull-request diffs are not supported")
        elif raw.startswith("new file mode"):
            status = "added"
        elif raw.startswith("deleted file mode"):
            status = "deleted"
        elif raw.startswith("rename from "):
            status = "renamed"
            rename_from = raw.removeprefix("rename from ")
        elif raw.startswith("rename to "):
            status = "renamed"
            rename_to = raw.removeprefix("rename to ")
        elif not in_hunk and raw.startswith("--- "):
            old_path = raw.removeprefix("--- ")
        elif not in_hunk and raw.startswith("+++ "):
            new_path = raw.removeprefix("+++ ")
    flush()
    unique: dict[str, _DiffFile] = {}
    for entry in entries:
        unique[entry.path] = entry
    return tuple(unique.values())


def _source_diagnostic(code: str, message: str, *, retryable: bool = False) -> GitHubSourceError:
    return GitHubSourceError(
        Diagnostic(
            code,
            message,
            retryable=retryable,
            next="inspect the documented GitHub source diagnostic and retry the bounded command",
        )
    )


def _metadata_visibility(metadata: Mapping[str, Any]) -> str | None:
    repository = metadata.get("repository")
    if not isinstance(repository, dict):
        base = metadata.get("base")
        repository = base.get("repo") if isinstance(base, dict) else None
    visibility = repository.get("visibility") if isinstance(repository, dict) else None
    if (not isinstance(visibility, str) or visibility not in {"public", "private"}) and isinstance(
        repository, dict
    ):
        if type(repository.get("private")) is bool:
            visibility = "private" if repository["private"] else "public"
    return (
        visibility if isinstance(visibility, str) and visibility in {"public", "private"} else None
    )


class LocalGitHubSource:
    """Resolve a PR through ``gh`` and materialize content from a local commit."""

    def __init__(
        self,
        project_dir: Path,
        *,
        runner: CommandRunner = _run_command,
        timeout_seconds: int = 30,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("GitHub source timeout must be positive")
        self.project_dir = project_dir.expanduser().resolve()
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def _run(
        self, operation: str, command: Sequence[str], *, deadline: float | None = None
    ) -> bytes:
        timeout_seconds = self.timeout_seconds
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _source_diagnostic(
                    "github_source_timeout", f"{operation} timed out", retryable=True
                )
            timeout_seconds = remaining
        result = self.runner(
            command,
            cwd=self.project_dir,
            timeout_seconds=timeout_seconds,
        )
        if result.output_truncated:
            raise _source_diagnostic(
                "github_source_too_large", f"{operation} exceeded the bounded output limit"
            )
        if result.returncode == 124:
            raise _source_diagnostic(
                "github_source_timeout", f"{operation} timed out", retryable=True
            )
        if result.returncode != 0:
            raise _source_diagnostic(
                "github_command_failed",
                f"{operation} failed with exit code {result.returncode}",
                retryable=True,
            )
        return result.stdout

    @staticmethod
    def _decode(operation: str, value: bytes) -> str:
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise _source_diagnostic(
                "github_source_not_utf8",
                f"{operation} returned non-UTF-8 content",
            ) from error

    def observe_identity(self, ref: PullRequestRef) -> tuple[str, str, str]:
        """Read only the live PR base, head, and visibility after a review."""
        endpoint = f"repos/{ref.repository}/pulls/{ref.number}"
        raw = self._run("GitHub pull-request identity", ["gh", "api", endpoint])
        try:
            metadata = json.loads(self._decode("GitHub pull-request identity", raw))
            base_sha = metadata["base"]["sha"]
            head_sha = metadata["head"]["sha"]
        except (KeyError, TypeError, ValueError) as error:
            raise _source_diagnostic(
                "github_metadata_invalid", "GitHub pull-request identity is malformed"
            ) from error
        if (
            not isinstance(base_sha, str)
            or not _SHA.fullmatch(base_sha)
            or not isinstance(head_sha, str)
            or not _SHA.fullmatch(head_sha)
        ):
            raise _source_diagnostic(
                "github_metadata_invalid", "GitHub pull-request identity lacks valid SHAs"
            )
        visibility = _metadata_visibility(metadata)
        if visibility is None:
            raise _source_diagnostic(
                "github_visibility_unknown", "repository visibility is unknown after review"
            )
        return base_sha.lower(), head_sha.lower(), visibility

    def resolve(self, ref: PullRequestRef) -> PullRequestSnapshot:
        deadline = time.monotonic() + self.timeout_seconds
        endpoint = f"repos/{ref.repository}/pulls/{ref.number}"
        metadata_bytes = self._run(
            "GitHub pull-request metadata", ["gh", "api", endpoint], deadline=deadline
        )
        try:
            metadata = json.loads(self._decode("GitHub pull-request metadata", metadata_bytes))
        except (TypeError, ValueError) as error:
            raise _source_diagnostic(
                "github_metadata_invalid", "GitHub pull-request metadata is not valid JSON"
            ) from error
        if not isinstance(metadata, dict):
            raise _source_diagnostic(
                "github_metadata_invalid", "GitHub pull-request metadata is not an object"
            )
        try:
            base_sha = metadata["base"]["sha"]
            head_sha = metadata["head"]["sha"]
        except (KeyError, TypeError) as error:
            raise _source_diagnostic(
                "github_metadata_invalid", "GitHub pull-request metadata lacks base/head SHA"
            ) from error
        if (
            not isinstance(base_sha, str)
            or not _SHA.fullmatch(base_sha)
            or not isinstance(head_sha, str)
            or not _SHA.fullmatch(head_sha)
        ):
            raise _source_diagnostic(
                "github_metadata_invalid",
                "GitHub pull-request metadata lacks valid base/head SHAs",
            )
        visibility = _metadata_visibility(metadata)
        if visibility is None:
            raise _source_diagnostic(
                "github_visibility_unknown",
                "repository visibility is unknown; source transfer is blocked",
            )

        local_head = (
            self._decode(
                "local checkout head",
                self._run(
                    "local checkout head",
                    ["git", "rev-parse", "HEAD"],
                    deadline=deadline,
                ),
            )
            .strip()
            .lower()
        )
        if local_head != str(head_sha).lower():
            raise _source_diagnostic(
                "github_checkout_stale",
                "local checkout HEAD does not match the pull-request head SHA",
            )

        diff_bytes = self._run(
            "GitHub pull-request diff",
            ["gh", "api", endpoint, "--header", "Accept: application/vnd.github.diff"],
            deadline=deadline,
        )
        if len(diff_bytes) > MAX_GITHUB_DIFF_BYTES:
            raise _source_diagnostic(
                "github_source_too_large", "pull-request diff exceeds the bounded source limit"
            )
        diff = self._decode("GitHub pull-request diff", diff_bytes)
        metadata_recheck_bytes = self._run(
            "GitHub pull-request metadata recheck",
            ["gh", "api", endpoint],
            deadline=deadline,
        )
        try:
            metadata_recheck = json.loads(
                self._decode("GitHub pull-request metadata recheck", metadata_recheck_bytes)
            )
            rechecked_base = metadata_recheck["base"]["sha"]
            rechecked_head = metadata_recheck["head"]["sha"]
        except (KeyError, TypeError, ValueError) as error:
            raise _source_diagnostic(
                "github_metadata_invalid",
                "GitHub pull-request metadata recheck lacks valid base/head SHAs",
            ) from error
        if (
            not isinstance(rechecked_base, str)
            or not _SHA.fullmatch(rechecked_base)
            or not isinstance(rechecked_head, str)
            or not _SHA.fullmatch(rechecked_head)
        ):
            raise _source_diagnostic(
                "github_metadata_invalid",
                "GitHub pull-request metadata recheck lacks valid base/head SHAs",
            )
        if (
            rechecked_base.lower() != str(base_sha).lower()
            or rechecked_head.lower() != str(head_sha).lower()
        ):
            raise _source_diagnostic(
                "github_source_identity_changed",
                "pull-request base or head changed while source was being fetched; "
                "source materialization is blocked",
                retryable=True,
            )
        rechecked_visibility = _metadata_visibility(metadata_recheck)
        if rechecked_visibility is None:
            raise _source_diagnostic(
                "github_visibility_unknown",
                "repository visibility became unknown; source materialization is blocked",
            )
        if rechecked_visibility != visibility:
            raise _source_diagnostic(
                "github_source_identity_changed",
                "repository visibility changed while source was being fetched; "
                "source materialization is blocked",
                retryable=True,
            )
        try:
            diff_files = _diff_files(diff)
        except _BinaryDiffError as error:
            raise _source_diagnostic("github_source_binary", str(error)) from error
        except _UnsupportedDiffError as error:
            raise _source_diagnostic("github_source_unsupported", str(error)) from error
        except ValueError as error:
            raise _source_diagnostic("github_path_invalid", str(error)) from error
        if not diff_files:
            raise _source_diagnostic(
                "github_source_unsupported",
                "pull-request diff contains no reviewable changed files",
            )
        if len(diff_files) > MAX_GITHUB_FILES:
            raise _source_diagnostic(
                "github_source_too_large", "pull request changes exceed the file limit"
            )

        changed_files: list[ChangedFileSnapshot] = []
        for item in diff_files:
            source_sha = str(base_sha) if item.status == "deleted" else str(head_sha)
            source_path = item.old_path if item.status == "deleted" and item.old_path else item.path
            try:
                source_path = _validate_relative_path(source_path)
            except ValueError as error:
                raise _source_diagnostic("github_path_invalid", str(error)) from error
            size_bytes = self._run(
                "local committed source size",
                ["git", "cat-file", "-s", f"{source_sha}:{source_path}"],
                deadline=deadline,
            )
            try:
                blob_size = int(self._decode("local committed source size", size_bytes).strip())
            except ValueError as error:
                raise _source_diagnostic(
                    "github_command_failed",
                    "local committed source size returned invalid output",
                    retryable=True,
                ) from error
            if blob_size < 0:
                raise _source_diagnostic(
                    "github_command_failed",
                    "local committed source size returned invalid output",
                    retryable=True,
                )
            if blob_size > MAX_GITHUB_FILE_BYTES:
                raise _source_diagnostic(
                    "github_source_too_large",
                    f"changed file exceeds the bounded source limit: {item.path}",
                )
            content_bytes = self._run(
                "local committed source",
                ["git", "show", f"{source_sha}:{source_path}"],
                deadline=deadline,
            )
            if len(content_bytes) > MAX_GITHUB_FILE_BYTES:
                raise _source_diagnostic(
                    "github_source_too_large",
                    f"changed file exceeds the bounded source limit: {item.path}",
                )
            content = self._decode(f"local committed source {item.path}", content_bytes)
            changed_files.append(
                ChangedFileSnapshot(
                    path=item.path,
                    status=item.status,
                    old_path=item.old_path,
                    content=content,
                )
            )
        return PullRequestSnapshot(
            ref=ref,
            base_sha=base_sha,
            head_sha=head_sha,
            visibility=visibility,
            changed_files=tuple(changed_files),
            diff=diff,
            evidence=(
                "github.pull_request",
                "github.pull_request_diff",
                "git.rev_parse_head",
                "git.show",
            ),
        )


__all__ = [
    "ChangedFileSnapshot",
    "CommandResult",
    "GitHubSourceError",
    "LocalGitHubSource",
    "MAX_GITHUB_DIFF_BYTES",
    "MAX_GITHUB_FILE_BYTES",
    "MAX_GITHUB_FILES",
    "PublicationItem",
    "PublicationTarget",
    "PullRequestRef",
    "PullRequestSnapshot",
    "ReviewPublicationPlan",
    "build_publication_plan",
    "canonical_sha256",
]
