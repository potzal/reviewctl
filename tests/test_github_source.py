from __future__ import annotations

import json
from pathlib import Path

import pytest

import reviewctl.github as github_module
from reviewctl.errors import exit_code_for
from reviewctl.github import (
    CommandResult,
    GitHubSourceError,
    LocalGitHubSource,
    PullRequestRef,
)

HEAD = "b" * 40
BASE = "a" * 40
DIFF_HEADER = "Accept: application/vnd.github.diff"


def _is_metadata_command(command) -> bool:
    return command[:2] == ["gh", "api"] and len(command) == 3 and command[2].endswith("/pulls/7")


def _is_diff_command(command) -> bool:
    return (
        command[:2] == ["gh", "api"]
        and command[2].endswith("/pulls/7")
        and command[3:] == ["--header", DIFF_HEADER]
    )


class FakeRunner:
    def __init__(self, *, head: str = HEAD, metadata: dict | None = None) -> None:
        self.head = head
        self.metadata = metadata or {
            "base": {"sha": BASE},
            "head": {"sha": HEAD},
            "repository": {"visibility": "private"},
        }
        self.calls: list[tuple[str, ...]] = []
        self.cwds: list[Path] = []
        self.timeouts: list[float] = []

    def __call__(self, command, *, cwd: Path, timeout_seconds: int) -> CommandResult:
        self.cwds.append(cwd)
        self.timeouts.append(timeout_seconds)
        self.calls.append(tuple(command))
        if _is_metadata_command(command):
            return CommandResult(0, json.dumps(self.metadata).encode(), b"")
        if _is_diff_command(command):
            return CommandResult(
                0,
                b"diff --git a/src/app.py b/src/app.py\n"
                b"--- a/src/app.py\n"
                b"+++ b/src/app.py\n"
                b"@@ -1,1 +1,1 @@\n"
                b"-value = 1\n"
                b"+value = 2\n",
                b"",
            )
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return CommandResult(0, f"{self.head}\n".encode(), b"")
        if command[:3] == ["git", "cat-file", "-s"]:
            return CommandResult(0, b"10\n", b"")
        if command[:2] == ["git", "show"]:
            return CommandResult(0, b"value = 2\n", b"")
        raise AssertionError(f"unexpected command: {command}")


def test_local_source_freezes_metadata_diff_and_exact_commit_content(tmp_path: Path) -> None:
    runner = FakeRunner()
    snapshot = LocalGitHubSource(tmp_path, runner=runner, timeout_seconds=17).resolve(
        PullRequestRef("example/project", 7)
    )

    assert snapshot.ref.repository == "example/project"
    assert snapshot.ref.number == 7
    assert snapshot.base_sha == BASE
    assert snapshot.head_sha == HEAD
    assert snapshot.visibility == "private"
    assert [item.path for item in snapshot.changed_files] == ["src/app.py"]
    assert snapshot.changed_files[0].content == "value = 2\n"
    assert snapshot.changed_files[0].sha256
    assert snapshot.diff_sha256
    assert snapshot.snapshot_sha256
    assert any(command[:2] == ("gh", "api") for command in runner.calls)
    assert ("git", "show", f"{HEAD}:src/app.py") in runner.calls
    assert runner.cwds and all(cwd == tmp_path.resolve() for cwd in runner.cwds)
    assert runner.timeouts and all(0 < timeout <= 17 for timeout in runner.timeouts)
    assert runner.timeouts == sorted(runner.timeouts, reverse=True)


def test_local_source_requests_diff_media_type(tmp_path: Path) -> None:
    runner = FakeRunner()

    LocalGitHubSource(tmp_path, runner=runner).resolve(PullRequestRef("example/project", 7))

    assert (
        "gh",
        "api",
        "repos/example/project/pulls/7",
        "--header",
        "Accept: application/vnd.github.diff",
    ) in runner.calls


def test_observe_identity_uses_only_metadata_without_source(tmp_path: Path) -> None:
    runner = FakeRunner()
    identity = LocalGitHubSource(tmp_path, runner=runner).observe_identity(
        PullRequestRef("example/project", 7)
    )

    assert identity == (BASE, HEAD, "private")
    assert runner.calls == [("gh", "api", "repos/example/project/pulls/7")]


@pytest.mark.parametrize(
    ("metadata", "code"),
    [
        (b"not-json", "github_metadata_invalid"),
        ({"base": [], "head": {"sha": HEAD}}, "github_metadata_invalid"),
        ({"base": {"sha": 1}, "head": {"sha": HEAD}}, "github_metadata_invalid"),
        ({"base": {"sha": "bad"}, "head": {"sha": HEAD}}, "github_metadata_invalid"),
        ({"base": {"sha": BASE}, "head": {"sha": "bad"}}, "github_metadata_invalid"),
        ({"base": {"sha": BASE}, "head": {"sha": None}}, "github_metadata_invalid"),
        ({"base": {"sha": BASE}, "head": {"sha": HEAD}}, "github_visibility_unknown"),
        (
            {"base": {"sha": BASE}, "head": {"sha": HEAD}, "repository": {"visibility": []}},
            "github_visibility_unknown",
        ),
    ],
)
def test_observe_identity_reports_malformed_metadata(tmp_path: Path, metadata, code: str) -> None:
    def runner(command, **kwargs):
        raw = metadata if isinstance(metadata, bytes) else json.dumps(metadata).encode()
        return CommandResult(0, raw, b"")

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=runner).observe_identity(
            PullRequestRef("example/project", 7)
        )
    assert error.value.diagnostic.code == code


def test_local_source_enforces_one_deadline_across_all_commands(
    tmp_path: Path, monkeypatch
) -> None:
    runner = FakeRunner()
    moments = iter([100.0, 100.0, 104.0, 109.25, 110.0])
    monkeypatch.setattr(github_module.time, "monotonic", lambda: next(moments))

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=runner, timeout_seconds=10).resolve(
            PullRequestRef("example/project", 7)
        )

    assert error.value.diagnostic.code == "github_source_timeout"
    assert runner.timeouts == [10, 6, 0.75]
    assert len(runner.calls) == 3


def test_local_source_rejects_truncated_runner_output(tmp_path: Path) -> None:
    source = LocalGitHubSource(
        tmp_path,
        runner=lambda *args, **kwargs: CommandResult(1, b"bounded", b"", True),
    )

    with pytest.raises(GitHubSourceError) as error:
        source._run("operation", ["gh"])

    assert error.value.diagnostic.code == "github_source_too_large"


@pytest.mark.parametrize(
    "recheck_metadata",
    [
        {
            "base": {"sha": BASE},
            "head": {"sha": "c" * 40},
            "repository": {"visibility": "private"},
        },
        {
            "base": {"sha": "c" * 40},
            "head": {"sha": HEAD},
            "repository": {"visibility": "private"},
        },
    ],
)
def test_local_source_rejects_identity_change_after_diff_before_materialization(
    tmp_path: Path, recheck_metadata: dict
) -> None:
    class HeadRaceRunner(FakeRunner):
        metadata_calls = 0

        def __call__(self, command, *, cwd, timeout_seconds):
            if _is_metadata_command(command):
                self.metadata_calls += 1
                metadata = {
                    "base": {"sha": BASE},
                    "head": {"sha": HEAD},
                    "repository": {"visibility": "private"},
                }
                if self.metadata_calls == 2:
                    metadata = recheck_metadata
                self.cwds.append(cwd)
                self.timeouts.append(timeout_seconds)
                self.calls.append(tuple(command))
                return CommandResult(0, json.dumps(metadata).encode(), b"")
            return super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)

    runner = HeadRaceRunner()
    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=runner).resolve(PullRequestRef("example/project", 7))

    assert error.value.diagnostic.code == "github_source_identity_changed"
    assert "base or head changed" in error.value.diagnostic.message
    assert runner.metadata_calls == 2
    assert runner.calls[:3] == [
        ("gh", "api", "repos/example/project/pulls/7"),
        ("git", "rev-parse", "HEAD"),
        ("gh", "api", "repos/example/project/pulls/7", "--header", DIFF_HEADER),
    ]
    assert runner.calls[3] == ("gh", "api", "repos/example/project/pulls/7")
    assert not any(command[:2] == ("git", "show") for command in runner.calls)


@pytest.mark.parametrize(
    ("rechecked_repository", "base_repository", "expected_code"),
    [
        ({"visibility": "public"}, None, "github_source_identity_changed"),
        ({}, None, "github_visibility_unknown"),
        (None, {"visibility": "public"}, "github_source_identity_changed"),
        (None, {}, "github_visibility_unknown"),
    ],
)
def test_local_source_rechecks_visibility_before_materializing_source(
    tmp_path: Path,
    rechecked_repository: dict | None,
    base_repository: dict | None,
    expected_code: str,
) -> None:
    class VisibilityRaceRunner(FakeRunner):
        metadata_calls = 0

        def __call__(self, command, *, cwd, timeout_seconds):
            if _is_metadata_command(command):
                self.metadata_calls += 1
                if self.metadata_calls == 2:
                    self.calls.append(tuple(command))
                    metadata = {"base": {"sha": BASE}, "head": {"sha": HEAD}}
                    if rechecked_repository is not None:
                        metadata["repository"] = rechecked_repository
                    if base_repository is not None:
                        metadata["base"]["repo"] = base_repository
                    return CommandResult(0, json.dumps(metadata).encode(), b"")
            return super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)

    runner = VisibilityRaceRunner()
    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=runner).resolve(PullRequestRef("example/project", 7))

    assert error.value.diagnostic.code == expected_code
    assert not any(command[:2] == ("git", "show") for command in runner.calls)


@pytest.mark.parametrize(
    "recheck_metadata",
    [
        b"not-json",
        json.dumps({"head": {}}).encode(),
        json.dumps({"base": {"sha": BASE}, "head": {"sha": 7}}).encode(),
        json.dumps({"base": {"sha": 7}, "head": {"sha": HEAD}}).encode(),
        json.dumps({"base": {"sha": "HEAD"}, "head": {"sha": HEAD}}).encode(),
        json.dumps({"base": {"sha": BASE}, "head": {"sha": "HEAD"}}).encode(),
    ],
)
def test_local_source_rejects_invalid_head_recheck(tmp_path: Path, recheck_metadata: bytes) -> None:
    class InvalidRecheckRunner(FakeRunner):
        metadata_calls = 0

        def __call__(self, command, *, cwd, timeout_seconds):
            if _is_metadata_command(command):
                self.metadata_calls += 1
                if self.metadata_calls == 2:
                    self.cwds.append(cwd)
                    self.timeouts.append(timeout_seconds)
                    self.calls.append(tuple(command))
                    return CommandResult(0, recheck_metadata, b"")
            return super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)

    runner = InvalidRecheckRunner()
    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=runner).resolve(PullRequestRef("example/project", 7))

    assert error.value.diagnostic.code == "github_metadata_invalid"
    assert runner.metadata_calls == 2
    assert not any(command[:2] == ("git", "show") for command in runner.calls)


def test_source_refuses_stale_checkout(tmp_path: Path) -> None:
    runner = FakeRunner(head="c" * 40)

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=runner).resolve(PullRequestRef("example/project", 7))

    assert error.value.diagnostic.code == "github_checkout_stale"


def test_source_refuses_empty_diff_before_model_materialization(tmp_path: Path) -> None:
    class EmptyDiffRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            if _is_diff_command(command):
                self.calls.append(tuple(command))
                return CommandResult(0, b"", b"")
            return super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)

    runner = EmptyDiffRunner()
    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=runner).resolve(PullRequestRef("example/project", 7))

    assert error.value.diagnostic.code == "github_source_unsupported"
    assert not any(command[:2] == ("git", "show") for command in runner.calls)


def test_source_refuses_unknown_visibility_before_reading_source(tmp_path: Path) -> None:
    runner = FakeRunner(metadata={"base": {"sha": BASE}, "head": {"sha": HEAD}})

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=runner).resolve(PullRequestRef("example/project", 7))

    assert error.value.diagnostic.code == "github_visibility_unknown"
    assert not any(command[:3] == ("git", "rev-parse", "HEAD") for command in runner.calls)


def test_source_refuses_invalid_path_and_non_utf8_content(tmp_path: Path) -> None:
    class InvalidPathRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            result = super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)
            if _is_diff_command(command):
                return CommandResult(
                    0,
                    b"diff --git a/../secret b/../secret\n"
                    b"--- a/../secret\n+++ b/../secret\n@@ -0,0 +1,1 @@\n+x\n",
                    b"",
                )
            return result

    with pytest.raises(GitHubSourceError) as path_error:
        LocalGitHubSource(tmp_path, runner=InvalidPathRunner()).resolve(
            PullRequestRef("example/project", 7)
        )
    assert path_error.value.diagnostic.code == "github_path_invalid"

    class BinaryRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            result = super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)
            if command[:2] == ["git", "show"]:
                return CommandResult(0, b"\xff\xfe", b"")
            return result

    with pytest.raises(GitHubSourceError) as binary_error:
        LocalGitHubSource(tmp_path, runner=BinaryRunner()).resolve(
            PullRequestRef("example/project", 7)
        )
    assert binary_error.value.diagnostic.code == "github_source_not_utf8"


def test_source_accepts_visibility_from_github_base_repository_shape(tmp_path: Path) -> None:
    metadata = {
        "base": {"sha": BASE, "repo": {"private": True}},
        "head": {"sha": HEAD},
    }
    runner = FakeRunner(metadata=metadata)

    snapshot = LocalGitHubSource(tmp_path, runner=runner).resolve(
        PullRequestRef("example/project", 7)
    )

    assert snapshot.visibility == "private"


def test_source_rejects_control_characters_in_paths(tmp_path: Path) -> None:
    class ControlPathRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            result = super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)
            if _is_diff_command(command):
                return CommandResult(
                    0,
                    b"diff --git a/src/bad\x01.py b/src/bad\x01.py\n"
                    b"--- a/src/bad\x01.py\n+++ b/src/bad\x01.py\n"
                    b"@@ -0,0 +1,1 @@\n+bad\n",
                    b"",
                )
            return result

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=ControlPathRunner()).resolve(
            PullRequestRef("example/project", 7)
        )

    assert error.value.diagnostic.code == "github_path_invalid"


def test_source_failure_does_not_expose_command_stderr(tmp_path: Path) -> None:
    class FailedRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            del cwd, timeout_seconds
            if command[:2] == ["gh", "api"]:
                return CommandResult(1, b"", b"Authorization: super-secret-token")
            return super().__call__(command, cwd=tmp_path, timeout_seconds=30)

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=FailedRunner()).resolve(
            PullRequestRef("example/project", 7)
        )

    assert error.value.diagnostic.code == "github_command_failed"
    assert "super-secret-token" not in error.value.diagnostic.message


def test_github_diagnostics_have_documented_exit_classes() -> None:
    assert exit_code_for("github_checkout_stale") == 2
    assert exit_code_for("github_command_failed") == 3
    assert exit_code_for("github_source_binary") == 2
    assert exit_code_for("github_source_unsupported") == 2
    assert exit_code_for("github_source_identity_changed") == 3
    assert exit_code_for("github_visibility_unknown") == 4


def test_github_source_command_timeout_oserror_and_success(tmp_path: Path) -> None:
    source = LocalGitHubSource(
        tmp_path, runner=lambda *args, **kwargs: CommandResult(124, b"", b"")
    )
    with pytest.raises(GitHubSourceError, match="timed out"):
        source._run("operation", ["gh"])
    source = LocalGitHubSource(tmp_path, runner=lambda *args, **kwargs: CommandResult(1, b"", b""))
    with pytest.raises(GitHubSourceError, match="failed"):
        source._run("operation", ["gh"])
    source = LocalGitHubSource(
        tmp_path, runner=lambda *args, **kwargs: CommandResult(0, b"ok", b"")
    )
    assert source._run("operation", ["gh"]) == b"ok"
    with pytest.raises(GitHubSourceError, match="non-UTF-8"):
        source._decode("operation", b"\xff")


@pytest.mark.parametrize(
    "metadata",
    [
        b"not-json",
        b"[]",
        json.dumps({"base": {}, "head": {}}).encode(),
    ],
)
def test_github_source_rejects_malformed_metadata(tmp_path: Path, metadata: bytes) -> None:
    class MetadataRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            if _is_metadata_command(command):
                return CommandResult(0, metadata, b"")
            return super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)

    with pytest.raises(GitHubSourceError, match="metadata"):
        LocalGitHubSource(tmp_path, runner=MetadataRunner()).resolve(
            PullRequestRef("example/project", 7)
        )


def test_github_source_rejects_invalid_sha_before_local_source_reads(tmp_path: Path) -> None:
    runner = FakeRunner(
        metadata={
            "base": {"sha": "HEAD"},
            "head": {"sha": HEAD},
            "repository": {"visibility": "private"},
        }
    )

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=runner).resolve(PullRequestRef("example/project", 7))

    assert error.value.diagnostic.code == "github_metadata_invalid"
    assert runner.calls == [("gh", "api", "repos/example/project/pulls/7")]


def test_github_source_accepts_boolean_visibility_fallback(tmp_path: Path) -> None:
    metadata = {"base": {"sha": BASE}, "head": {"sha": HEAD}, "repository": {"private": False}}
    snapshot = LocalGitHubSource(tmp_path, runner=FakeRunner(metadata=metadata)).resolve(
        PullRequestRef("example/project", 7)
    )
    assert snapshot.visibility == "public"


def test_github_source_rejects_diff_and_file_limits(tmp_path: Path) -> None:
    class LargeDiffRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            if _is_diff_command(command):
                return CommandResult(0, b"x" * (github_module.MAX_GITHUB_DIFF_BYTES + 1), b"")
            return super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)

    with pytest.raises(GitHubSourceError, match="limit"):
        LocalGitHubSource(tmp_path, runner=LargeDiffRunner()).resolve(
            PullRequestRef("example/project", 7)
        )

    many = b"".join(
        f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n".encode()
        for i in range(github_module.MAX_GITHUB_FILES + 1)
    )

    class ManyFilesRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            if _is_diff_command(command):
                return CommandResult(0, many, b"")
            return super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)

    with pytest.raises(GitHubSourceError, match="file limit"):
        LocalGitHubSource(tmp_path, runner=ManyFilesRunner()).resolve(
            PullRequestRef("example/project", 7)
        )


@pytest.mark.parametrize(
    "binary_block",
    [
        b"Binary files /dev/null and b/image.png differ",
        b"GIT binary patch",
    ],
)
def test_github_source_rejects_binary_diff_blocks(tmp_path: Path, binary_block: bytes) -> None:
    class BinaryDiffRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            if _is_diff_command(command):
                return CommandResult(
                    0,
                    b"diff --git a/image.png b/image.png\n"
                    b"new file mode 100644\n" + binary_block + b"\n",
                    b"",
                )
            return super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=BinaryDiffRunner()).resolve(
            PullRequestRef("example/project", 7)
        )

    assert error.value.diagnostic.code == "github_source_binary"
    assert "binary" in error.value.diagnostic.message


@pytest.mark.parametrize(
    ("diff_body", "expected_status", "source_sha"),
    [
        (b"old mode 100644\nnew mode 100755\n", "modified", HEAD),
        (b"new file mode 100644\n", "added", HEAD),
        (b"deleted file mode 100644\n", "deleted", BASE),
    ],
    ids=["mode-only", "empty-added-file", "empty-deleted-file"],
)
def test_github_source_materializes_paths_from_metadata_only_diff_headers(
    tmp_path: Path, diff_body: bytes, expected_status: str, source_sha: str
) -> None:
    class MetadataOnlyDiffRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            if _is_diff_command(command):
                return CommandResult(
                    0,
                    b"diff --git a/script.sh b/script.sh\n" + diff_body,
                    b"",
                )
            return super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)

    runner = MetadataOnlyDiffRunner()
    snapshot = LocalGitHubSource(tmp_path, runner=runner).resolve(
        PullRequestRef("example/project", 7)
    )

    assert [(item.path, item.status) for item in snapshot.changed_files] == [
        ("script.sh", expected_status)
    ]
    assert ("git", "show", f"{source_sha}:script.sh") in runner.calls


@pytest.mark.parametrize(
    "malformed_diff",
    [
        b"--- a/source.py\n+++ b/source.py\n",
        b"not a Git diff\n",
    ],
    ids=["headerless-fragment", "arbitrary-text"],
)
def test_github_source_rejects_nonempty_diff_without_git_header(
    tmp_path: Path, malformed_diff: bytes
) -> None:
    class MalformedDiffRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            if _is_diff_command(command):
                return CommandResult(0, malformed_diff, b"")
            return super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=MalformedDiffRunner()).resolve(
            PullRequestRef("example/project", 7)
        )

    assert error.value.diagnostic.code == "github_source_unsupported"
    assert "diff --git" in error.value.diagnostic.message


def test_github_source_decodes_c_quoted_utf8_paths(tmp_path: Path) -> None:
    diff = (
        b'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
        b"new file mode 100644\n"
        b"--- /dev/null\n"
        b'+++ "b/caf\\303\\251.py"\n'
        b"@@ -0,0 +1,1 @@\n"
        b"+value = 2\n"
    )

    class QuotedPathRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            if _is_diff_command(command):
                return CommandResult(0, diff, b"")
            if command[:2] == ["git", "show"] and command[2] == f"{HEAD}:café.py":
                return CommandResult(0, b"value = 2\n", b"")
            return super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)

    snapshot = LocalGitHubSource(tmp_path, runner=QuotedPathRunner()).resolve(
        PullRequestRef("example/project", 7)
    )

    assert snapshot.changed_files[0].path == "café.py"
    assert snapshot.changed_files[0].content == "value = 2\n"


def test_github_source_rejects_large_changed_file_and_invalid_snapshot(tmp_path: Path) -> None:
    class LargeFileRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            if command[:2] == ["git", "show"]:
                return CommandResult(0, b"x" * (github_module.MAX_GITHUB_FILE_BYTES + 1), b"")
            return super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)

    with pytest.raises(GitHubSourceError, match="changed file"):
        LocalGitHubSource(tmp_path, runner=LargeFileRunner()).resolve(
            PullRequestRef("example/project", 7)
        )

    class InvalidShaRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            result = super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)
            if _is_metadata_command(command):
                metadata = {
                    "base": {"sha": "bad"},
                    "head": {"sha": HEAD},
                    "repository": {"visibility": "private"},
                }
                return CommandResult(0, json.dumps(metadata).encode(), b"")
            return result

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=InvalidShaRunner()).resolve(
            PullRequestRef("example/project", 7)
        )
    assert error.value.diagnostic.code == "github_metadata_invalid"


def test_github_source_rejects_oversized_blob_before_capturing_content(tmp_path: Path) -> None:
    class OversizedBlobRunner(FakeRunner):
        git_show_calls = 0

        def __call__(self, command, *, cwd, timeout_seconds):
            if command[:3] == ["git", "cat-file", "-s"]:
                self.cwds.append(cwd)
                self.timeouts.append(timeout_seconds)
                self.calls.append(tuple(command))
                return CommandResult(
                    0, f"{github_module.MAX_GITHUB_FILE_BYTES + 1}\n".encode(), b""
                )
            if command[:2] == ["git", "show"]:
                self.git_show_calls += 1
                return CommandResult(0, b"x" * (github_module.MAX_GITHUB_FILE_BYTES + 1), b"")
            return super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)

    runner = OversizedBlobRunner()
    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=runner).resolve(PullRequestRef("example/project", 7))

    assert error.value.diagnostic.code == "github_source_too_large"
    assert runner.git_show_calls == 0
    assert runner.calls[-1] == ("git", "cat-file", "-s", f"{HEAD}:src/app.py")


@pytest.mark.parametrize("size_output", [b"not-a-size\n", b"-1\n"])
def test_github_source_rejects_invalid_blob_size_output(tmp_path: Path, size_output: bytes) -> None:
    class InvalidSizeRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            if command[:3] == ["git", "cat-file", "-s"]:
                return CommandResult(0, size_output, b"")
            return super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=InvalidSizeRunner()).resolve(
            PullRequestRef("example/project", 7)
        )

    assert error.value.diagnostic.code == "github_command_failed"


def test_github_source_rejects_invalid_timeout_and_boolean_visibility_shape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout"):
        LocalGitHubSource(tmp_path, timeout_seconds=0)

    metadata = {
        "base": {"sha": BASE},
        "head": {"sha": HEAD},
        "repository": {"private": "unknown"},
    }
    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=FakeRunner(metadata=metadata)).resolve(
            PullRequestRef("example/project", 7)
        )
    assert error.value.diagnostic.code == "github_visibility_unknown"


def test_github_source_rejects_invalid_committed_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        github_module,
        "_diff_files",
        lambda diff: (github_module._DiffFile("../bad", "modified"),),
    )
    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=FakeRunner()).resolve(
            PullRequestRef("example/project", 7)
        )
    assert error.value.diagnostic.code == "github_path_invalid"
