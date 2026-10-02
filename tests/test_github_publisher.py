from __future__ import annotations

import json
from pathlib import Path

import pytest

import reviewctl.github_publisher as publisher_module
from reviewctl.github import (
    ChangedFileSnapshot,
    CommandResult,
    PullRequestRef,
    PullRequestSnapshot,
    build_publication_plan,
)
from reviewctl.github_publisher import GitHubPublisher

HEAD = "b" * 40
OTHER_HEAD = "c" * 40
BASE = "a" * 40


def make_snapshot() -> PullRequestSnapshot:
    return PullRequestSnapshot(
        ref=PullRequestRef("example/project", 7),
        base_sha=BASE,
        head_sha=HEAD,
        visibility="private",
        changed_files=(
            ChangedFileSnapshot(path="src/app.py", status="modified", content="value = 2\n"),
        ),
        diff=(
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-value = 1\n"
            "+value = 2\n"
        ),
        evidence=("test",),
    )


def make_plan():
    snapshot = make_snapshot()
    return build_publication_plan(
        snapshot,
        project_id="project-1",
        review_id="review-1",
        findings=(
            {
                "findingId": "finding-inline",
                "severity": "high",
                "path": "src/app.py",
                "line": 1,
                "title": "Handle failure",
            },
            {
                "findingId": "finding-summary",
                "severity": "medium",
                "path": "README.md",
                "line": 1,
                "title": "Document behavior",
            },
        ),
        review_status="accepted",
    )


class FakeRunner:
    def __init__(
        self,
        *,
        head_values: list[str] | None = None,
        base_values: list[str] | None = None,
        comments: dict[int, list[dict]] | None = None,
        reviews: dict[int, list[dict]] | None = None,
    ) -> None:
        self.head_values = list(head_values or [HEAD])
        self.base_values = list(base_values or [BASE])
        self.comments = comments or {1: []}
        self.reviews = reviews or {1: []}
        self.calls: list[tuple[str, ...]] = []
        self.cwds: list[Path] = []
        self.timeouts: list[float] = []
        self.inputs: list[bytes | None] = []
        self.post_payload = None

    def __call__(
        self, command, *, cwd: Path, timeout_seconds: int, input_bytes: bytes | None = None
    ) -> CommandResult:
        self.cwds.append(cwd)
        self.timeouts.append(timeout_seconds)
        self.inputs.append(input_bytes)
        call = tuple(command)
        self.calls.append(call)
        endpoint = command[2]
        if "--method" in command and command[command.index("--method") + 1] == "POST":
            self.post_payload = json.loads(input_bytes.decode("utf-8"))
            return CommandResult(
                0,
                json.dumps({"id": 9001, "comments": [{"id": 9002}, {"id": 9003}]}).encode(),
                b"",
            )
        if endpoint == "repos/example/project/pulls/7":
            value = self.head_values.pop(0) if len(self.head_values) > 1 else self.head_values[0]
            base = self.base_values.pop(0) if len(self.base_values) > 1 else self.base_values[0]
            return CommandResult(
                0,
                json.dumps(
                    {
                        "head": {"sha": value},
                        "base": {"sha": base},
                        "repository": {"visibility": "private"},
                    }
                ).encode(),
                b"",
            )
        if endpoint.endswith("/comments"):
            if endpoint.endswith("/reviews/9001/comments"):
                page = int(
                    next(field.split("=", 1)[1] for field in command if field.startswith("page="))
                )
                return CommandResult(
                    0,
                    json.dumps(
                        [{"id": 9002, "body": make_plan().items[0].body}] if page == 1 else []
                    ).encode(),
                    b"",
                )
            page = int(
                next(field.split("=", 1)[1] for field in command if field.startswith("page="))
            )
            return CommandResult(0, json.dumps(self.comments.get(page, [])).encode(), b"")
        if endpoint.endswith("/reviews"):
            page = int(
                next(field.split("=", 1)[1] for field in command if field.startswith("page="))
            )
            return CommandResult(0, json.dumps(self.reviews.get(page, [])).encode(), b"")
        raise AssertionError(f"unexpected command: {command}")


def post_calls(runner: FakeRunner) -> list[tuple[str, ...]]:
    return [
        call
        for call in runner.calls
        if "--method" in call and call[call.index("--method") + 1] == "POST"
    ]


@pytest.mark.parametrize("base_values", [[OTHER_HEAD], [BASE, OTHER_HEAD]])
def test_publisher_refuses_base_change_with_unchanged_head(
    tmp_path: Path, base_values: list[str]
) -> None:
    runner = FakeRunner(base_values=base_values)

    result = GitHubPublisher(tmp_path, runner=runner).publish(
        make_plan(), expected_base_sha=BASE, expected_visibility="private"
    )

    assert result.status == "failed"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "github_source_identity_changed"
    assert post_calls(runner) == []


def test_publisher_records_identity_race_after_post_without_retry(tmp_path: Path) -> None:
    runner = FakeRunner(base_values=[BASE, BASE, OTHER_HEAD])
    result = GitHubPublisher(tmp_path, runner=runner).publish(
        make_plan(), expected_base_sha=BASE, expected_visibility="private"
    )
    assert result.status == "stale_head_race"
    assert result.summary_comment_id == "9001"
    assert result.published_comment_ids == ("9002",)
    assert result.diagnostic.code == "github_source_identity_changed"
    assert len(post_calls(runner)) == 1


@pytest.mark.parametrize(
    "metadata", [{"head": {"sha": HEAD}}, {"head": {"sha": HEAD}, "base": {"sha": None}}]
)
def test_publisher_rejects_missing_base_identity(tmp_path: Path, metadata) -> None:
    class MalformedIdentityRunner(FakeRunner):
        def __call__(self, command, **kwargs):
            if command[2] == "repos/example/project/pulls/7":
                return CommandResult(0, json.dumps(metadata).encode(), b"")
            return super().__call__(command, **kwargs)

    runner = MalformedIdentityRunner()
    result = GitHubPublisher(tmp_path, runner=runner).publish(make_plan(), expected_base_sha=BASE)
    assert result.status == "failed"
    assert result.diagnostic.code in {
        "github_publication_response_invalid",
        "github_source_identity_changed",
    }
    assert post_calls(runner) == []


@pytest.mark.parametrize(
    ("expected_base_sha", "expected_visibility"),
    [(BASE, None), (None, "private")],
)
def test_publisher_accepts_independent_identity_expectations(
    tmp_path: Path, expected_base_sha: str | None, expected_visibility: str | None
) -> None:
    runner = FakeRunner()

    result = GitHubPublisher(tmp_path, runner=runner).publish(
        make_plan(),
        expected_base_sha=expected_base_sha,
        expected_visibility=expected_visibility,
    )

    assert result.status == "published"
    assert len(post_calls(runner)) == 1


@pytest.mark.parametrize(
    ("expected_base_sha", "expected_visibility"),
    [(OTHER_HEAD, None), (None, "public")],
)
def test_publisher_refuses_independent_identity_mismatch(
    tmp_path: Path, expected_base_sha: str | None, expected_visibility: str | None
) -> None:
    runner = FakeRunner()

    result = GitHubPublisher(tmp_path, runner=runner).publish(
        make_plan(),
        expected_base_sha=expected_base_sha,
        expected_visibility=expected_visibility,
    )

    assert result.status == "failed"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "github_source_identity_changed"
    assert post_calls(runner) == []


def test_publisher_reconciles_both_comment_and_review_bodies_and_posts_one_group(
    tmp_path: Path,
) -> None:
    plan = make_plan()
    runner = FakeRunner(comments={1: [{"id": 1, "body": "unrelated"}], 2: []}, reviews={1: []})

    result = GitHubPublisher(tmp_path, runner=runner, timeout_seconds=19, page_size=2).publish(plan)

    assert result.status == "published"
    assert result.summary_comment_id == "9001"
    assert result.published_comment_ids == ("9002",)
    assert result.to_payload()["publishedComments"] == [
        {"findingId": "finding-inline", "commentId": "9002"}
    ]
    assert result.skipped_finding_ids == ()
    posts = post_calls(runner)
    assert len(posts) == 1
    assert runner.post_payload["commit_id"] == HEAD
    assert runner.post_payload["comments"][0]["path"] == "src/app.py"
    assert "finding-summary" in runner.post_payload["body"]
    assert runner.cwds and all(cwd == tmp_path.resolve() for cwd in runner.cwds)
    assert runner.timeouts and all(0 < timeout <= 19 for timeout in runner.timeouts)
    assert runner.timeouts == sorted(runner.timeouts, reverse=True)
    assert any(input_bytes is not None for input_bytes in runner.inputs)


def test_publisher_enforces_one_deadline_across_all_requests(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    moments = iter([100.0, 100.0, 104.0, 109.25, 110.0])
    monkeypatch.setattr(publisher_module.time, "monotonic", lambda: next(moments))

    result = GitHubPublisher(tmp_path, runner=runner, timeout_seconds=10, page_size=100).publish(
        make_plan()
    )

    assert result.status == "failed"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "github_publication_timeout"
    assert runner.timeouts == [10, 6, 0.75]
    assert len(runner.calls) == 3


def test_publisher_recovers_inline_comment_ids_from_review_comments_endpoint() -> None:
    class RealEndpointRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds, input_bytes=None):
            if command[2].endswith("/reviews/9001/comments"):
                self.calls.append(tuple(command))
                return CommandResult(
                    0,
                    json.dumps([{"id": 9002, "body": make_plan().items[0].body}]).encode(),
                    b"",
                )
            if "--method" in command and command[command.index("--method") + 1] == "POST":
                self.calls.append(tuple(command))
                self.post_payload = json.loads(input_bytes.decode("utf-8"))
                return CommandResult(0, b'{"id": 9001}', b"")
            return super().__call__(
                command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                input_bytes=input_bytes,
            )

    runner = RealEndpointRunner(comments={1: [], 2: []}, reviews={1: [], 2: []})

    result = GitHubPublisher(Path("."), runner=runner).publish(make_plan())

    assert result.status == "published"
    assert result.published_comment_ids == ("9002",)
    review_comment_calls = [
        call for call in runner.calls if call[2].endswith("/reviews/9001/comments")
    ]
    assert len(review_comment_calls) == 1


def test_publisher_skips_existing_markers_even_on_a_later_head() -> None:
    plan = make_plan()
    runner = FakeRunner(
        head_values=[HEAD],
        comments={1: [{"id": 1, "body": plan.items[0].body}], 2: []},
        reviews={1: [{"id": 2, "body": plan.items[1].body}], 2: []},
    )

    result = GitHubPublisher(Path("."), runner=runner, page_size=2).publish(plan)

    assert result.status == "skipped_duplicate"
    assert result.published_comment_ids == ()
    assert result.skipped_finding_ids == ("finding-inline", "finding-summary")
    assert post_calls(runner) == []


def test_publisher_rechecks_head_before_confirming_all_findings_are_duplicates() -> None:
    plan = make_plan()
    runner = FakeRunner(
        head_values=[HEAD, OTHER_HEAD],
        comments={1: [{"id": 1, "body": plan.items[0].body}]},
        reviews={1: [{"id": 2, "body": plan.items[1].body}]},
    )

    result = GitHubPublisher(Path("."), runner=runner).publish(plan)

    assert result.status == "stale_head"
    assert result.observed_head_sha == OTHER_HEAD
    assert result.skipped_finding_ids == ("finding-inline", "finding-summary")
    assert post_calls(runner) == []


def test_publisher_checks_head_even_when_accepted_review_has_no_findings() -> None:
    plan = build_publication_plan(
        make_snapshot(),
        project_id="project-1",
        review_id="review-1",
        findings=(),
        review_status="accepted",
    )
    runner = FakeRunner(head_values=[OTHER_HEAD])

    result = GitHubPublisher(Path("."), runner=runner).publish(plan)

    assert result.status == "stale_head"
    assert result.observed_head_sha == OTHER_HEAD
    assert result.diagnostic is not None
    assert result.diagnostic.code == "github_publication_stale_head"
    assert post_calls(runner) == []


def test_publisher_accepts_github_reviews_with_empty_summary_body() -> None:
    runner = FakeRunner(comments={1: [], 2: []}, reviews={1: [{"id": 2, "body": None}]})

    result = GitHubPublisher(Path("."), runner=runner, page_size=2).publish(make_plan())

    assert result.status == "published"


def test_publisher_refuses_stale_head_before_post() -> None:
    runner = FakeRunner(head_values=[OTHER_HEAD])

    result = GitHubPublisher(Path("."), runner=runner).publish(make_plan())

    assert result.status == "stale_head"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "github_publication_stale_head"
    assert post_calls(runner) == []


def test_publisher_refuses_head_change_after_reconciliation() -> None:
    runner = FakeRunner(head_values=[HEAD, OTHER_HEAD])
    result = GitHubPublisher(Path("."), runner=runner).publish(make_plan())
    assert result.status == "stale_head"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "github_publication_stale_head"
    assert post_calls(runner) == []


def test_publisher_fails_closed_when_reconciliation_budget_cannot_prove_exhaustion() -> None:
    plan = make_plan()
    runner = FakeRunner(comments={1: [{"id": 1, "body": "x"}], 2: [{"id": 2, "body": "x"}]})

    result = GitHubPublisher(Path("."), runner=runner, page_size=1, max_pages=2).publish(plan)

    assert result.status == "reconciliation_incomplete"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "publication_reconciliation_incomplete"
    assert post_calls(runner) == []


def test_publisher_fails_closed_on_malformed_reconciliation_page() -> None:
    class MalformedRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds, input_bytes=None):
            if command[2].endswith("/comments"):
                self.calls.append(tuple(command))
                return CommandResult(0, b"{}", b"")
            return super().__call__(
                command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                input_bytes=input_bytes,
            )

    runner = MalformedRunner()
    result = GitHubPublisher(Path("."), runner=runner).publish(make_plan())

    assert result.status == "reconciliation_incomplete"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "publication_reconciliation_incomplete"
    assert post_calls(runner) == []


def test_publisher_does_not_claim_success_for_malformed_post_response() -> None:
    class BadPostRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds, input_bytes=None):
            if "--method" in command and command[command.index("--method") + 1] == "POST":
                self.calls.append(tuple(command))
                return CommandResult(0, b"{}", b"")
            return super().__call__(
                command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                input_bytes=input_bytes,
            )

    runner = BadPostRunner()
    result = GitHubPublisher(Path("."), runner=runner).publish(make_plan())

    assert result.status == "failed"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "github_publication_response_invalid"


def test_publisher_classifies_post_head_race_without_retry() -> None:
    runner = FakeRunner(head_values=[HEAD, HEAD, OTHER_HEAD])

    result = GitHubPublisher(Path("."), runner=runner).publish(make_plan())

    assert result.status == "stale_head_race"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "github_publication_stale_head_race"
    assert len(post_calls(runner)) == 1
    assert result.summary_comment_id == "9001"


def test_publisher_preserves_posted_ids_when_final_head_lookup_fails() -> None:
    class FinalHeadTimeoutRunner(FakeRunner):
        def __init__(self):
            super().__init__()
            self.head_lookups = 0

        def __call__(self, command, *, cwd, timeout_seconds, input_bytes=None):
            if command[2] == "repos/example/project/pulls/7":
                self.head_lookups += 1
                if self.head_lookups == 3:
                    self.calls.append(tuple(command))
                    return CommandResult(124, b"", b"")
            return super().__call__(
                command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                input_bytes=input_bytes,
            )

    runner = FinalHeadTimeoutRunner()

    result = GitHubPublisher(Path("."), runner=runner).publish(make_plan())

    assert result.status == "failed"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "github_publication_timeout"
    assert result.summary_comment_id == "9001"
    assert result.published_comment_ids == ("9002",)
    assert len(post_calls(runner)) == 1


def test_publisher_preserves_review_id_when_comment_lookup_fails() -> None:
    class MalformedCommentLookupRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds, input_bytes=None):
            if command[2].endswith("/reviews/9001/comments"):
                self.calls.append(tuple(command))
                return CommandResult(0, b"{}", b"")
            return super().__call__(
                command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                input_bytes=input_bytes,
            )

    runner = MalformedCommentLookupRunner()

    result = GitHubPublisher(Path("."), runner=runner).publish(make_plan())

    assert result.status == "reconciliation_incomplete"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "publication_reconciliation_incomplete"
    assert result.summary_comment_id == "9001"
    assert result.published_comment_ids == ()
    assert len(post_calls(runner)) == 1


def test_publisher_redacts_failed_command_details() -> None:
    class FailedRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds, input_bytes=None):
            del cwd, timeout_seconds, input_bytes
            self.calls.append(tuple(command))
            return CommandResult(1, b"", b"Authorization: super-secret-token")

    result = GitHubPublisher(Path("."), runner=FailedRunner()).publish(make_plan())

    assert result.status == "failed"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "github_publication_failed"
    assert "super-secret-token" not in result.diagnostic.message


def test_publisher_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="bounds"):
        GitHubPublisher(Path("."), timeout_seconds=0)
    with pytest.raises(ValueError, match="bounds"):
        GitHubPublisher(Path("."), page_size=0)
    with pytest.raises(ValueError, match="bounds"):
        GitHubPublisher(Path("."), max_pages=0)


def test_publisher_runner_errors_and_timeouts_are_typed() -> None:
    def fail(*args, **kwargs):
        raise ValueError("runner failure")

    publisher = GitHubPublisher(Path("."), runner=fail)
    with pytest.raises(publisher_module.GitHubPublisherError, match="could not run"):
        publisher._run("operation", ["gh"])

    publisher = GitHubPublisher(
        Path("."), runner=lambda *args, **kwargs: CommandResult(124, b"", b"")
    )
    with pytest.raises(publisher_module.GitHubPublisherError, match="timed out"):
        publisher._run("operation", ["gh"], input_bytes=b"payload")

    publisher = GitHubPublisher(
        Path("."), runner=lambda *args, **kwargs: CommandResult(1, b"bounded", b"", True)
    )
    with pytest.raises(publisher_module.GitHubPublisherError, match="bounded output"):
        publisher._run("operation", ["gh"])


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b'{"head": {}}', b'{"head":{"sha":7}}'])
def test_publisher_rejects_malformed_head(raw: bytes) -> None:
    publisher = GitHubPublisher(
        Path("."), runner=lambda *args, **kwargs: CommandResult(0, raw, b"")
    )
    plan = make_plan()
    with pytest.raises(publisher_module.GitHubPublisherError, match="head"):
        publisher._head(plan)


@pytest.mark.parametrize("raw", [b"not-json", b"{}", b"[1]"])
def test_publisher_rejects_malformed_reconciliation_page(raw: bytes) -> None:
    publisher = GitHubPublisher(
        Path("."), runner=lambda *args, **kwargs: CommandResult(0, raw, b"")
    )
    with pytest.raises(publisher_module.GitHubPublisherError, match="reconciliation"):
        publisher._page("endpoint", 1)


def test_reconciliation_page_explicitly_uses_get_with_query_fields() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(command, *, cwd, timeout_seconds, input_bytes=None):
        del cwd, timeout_seconds, input_bytes
        calls.append(tuple(command))
        return CommandResult(0, b"[]", b"")

    publisher = GitHubPublisher(Path("."), runner=runner, page_size=7)

    assert publisher._page("endpoint", 3) == []
    assert calls == [
        (
            "gh",
            "api",
            "endpoint",
            "--method",
            "GET",
            "-f",
            "per_page=7",
            "-f",
            "page=3",
        )
    ]


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b'{"comments": []}'])
def test_publisher_rejects_malformed_post_bodies(raw: bytes) -> None:
    publisher = GitHubPublisher(
        Path("."), runner=lambda *args, **kwargs: CommandResult(0, raw, b"")
    )
    with pytest.raises(publisher_module.GitHubPublisherError, match="publication"):
        publisher._post(make_plan(), make_plan().items)


@pytest.mark.parametrize(
    "comments",
    [
        [{"id": 9002, "body": None}],
        [{"id": 9002, "body": "unrelated"}],
        [
            {"id": 9002, "body": make_plan().items[0].body},
            {"id": 9003, "body": make_plan().items[0].body},
        ],
        [],
    ],
)
def test_publisher_requires_one_finding_marker_per_published_comment(
    comments: list[dict[str, object]],
) -> None:
    publisher = GitHubPublisher(
        Path("."),
        runner=lambda *args, **kwargs: CommandResult(0, json.dumps(comments).encode(), b""),
    )

    with pytest.raises(
        publisher_module.GitHubPublisherError, match="body|finding|every inline finding"
    ):
        publisher._published_comments(make_plan(), 9001)


@pytest.mark.parametrize("value", [None, [], {}, "", 0, -1, True])
def test_publisher_rejects_invalid_posted_comment_id(value: object) -> None:
    publisher = GitHubPublisher(
        Path("."),
        runner=lambda *args, **kwargs: CommandResult(0, json.dumps([{"id": value}]).encode(), b""),
    )

    with pytest.raises(publisher_module.GitHubPublisherError, match="valid comment ids"):
        publisher._published_comment_ids(make_plan(), 9001)


@pytest.mark.parametrize("value", [None, [], {}, "", 0, -1, True])
def test_publisher_rejects_invalid_created_review_id(value: object) -> None:
    calls = 0

    def runner(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return CommandResult(0, json.dumps({"id": value}).encode(), b"")
        return CommandResult(0, b"[]", b"")

    publisher = GitHubPublisher(Path("."), runner=runner)

    with pytest.raises(publisher_module.GitHubPublisherError, match="review id"):
        publisher._post(make_plan(), make_plan().items)
    assert calls == 1


def test_publisher_bounds_posted_comment_lookup() -> None:
    publisher = GitHubPublisher(
        Path("."),
        runner=lambda *args, **kwargs: CommandResult(
            0,
            json.dumps([{"id": 1, "body": make_plan().items[0].body}]).encode(),
            b"",
        ),
        page_size=1,
        max_pages=1,
    )

    with pytest.raises(publisher_module.GitHubPublisherError, match="page budget"):
        publisher._published_comment_ids(make_plan(), 9001)


def test_publisher_returns_plan_and_empty_findings_statuses() -> None:
    plan = build_publication_plan(
        make_snapshot(), project_id="p", review_id="r", findings=(), review_status="partial"
    )
    result = GitHubPublisher(Path("."), runner=FakeRunner()).publish(plan)
    assert result.status == "plan_invalid"
    empty = build_publication_plan(
        make_snapshot(), project_id="p", review_id="r", findings=(), review_status="accepted"
    )
    runner = FakeRunner()
    empty_result = GitHubPublisher(Path("."), runner=runner).publish(empty)
    assert empty_result.status == "no_findings"
    assert empty_result.observed_head_sha == HEAD
    assert runner.calls == [("gh", "api", "repos/example/project/pulls/7")]


def test_publisher_rejects_non_text_reconciliation_body() -> None:
    runner = FakeRunner(comments={1: [{"body": 7}]})
    result = GitHubPublisher(Path("."), runner=runner).publish(make_plan())
    assert result.status == "reconciliation_incomplete"


def test_publication_result_payload_and_key_are_stable() -> None:
    plan = make_plan()
    result = publisher_module.PublicationResult("key", HEAD, "published")
    assert result.to_payload()["publicationKey"] == "key"
    assert publisher_module.publication_key(plan).startswith("github:example/project:7:review-1:")


def test_publication_result_rejects_comment_ids_without_finding_evidence() -> None:
    with pytest.raises(ValueError, match="finding/comment evidence"):
        publisher_module.PublicationResult(
            "key", HEAD, "published", published_comment_ids=("9002",)
        )
