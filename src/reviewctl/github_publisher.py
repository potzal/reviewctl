"""Explicit, comment-only GitHub publication for verified review plans."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reviewctl.errors import Diagnostic, ReviewctlError
from reviewctl.github import (
    CommandRunner,
    ReviewPublicationPlan,
    _metadata_visibility,
    _run_command,
)


class GitHubPublisherError(ReviewctlError):
    """Safe, typed failure from the GitHub publication adapter."""

    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class PublishedComment:
    finding_id: str
    comment_id: str

    def to_payload(self) -> dict[str, str]:
        return {"findingId": self.finding_id, "commentId": self.comment_id}


@dataclass(frozen=True)
class PublicationResult:
    publication_key: str
    head_sha: str
    status: str
    published_comment_ids: tuple[str, ...] = ()
    published_comments: tuple[PublishedComment, ...] = ()
    skipped_finding_ids: tuple[str, ...] = ()
    summary_comment_id: str | None = None
    observed_head_sha: str | None = None
    diagnostic: Diagnostic | None = None

    def __post_init__(self) -> None:
        if self.published_comment_ids != tuple(
            comment.comment_id for comment in self.published_comments
        ):
            raise ValueError("published comment ids must match finding/comment evidence")

    def to_payload(self) -> dict[str, Any]:
        return {
            "publicationKey": self.publication_key,
            "headSha": self.head_sha,
            "status": self.status,
            "publishedCommentIds": list(self.published_comment_ids),
            "publishedComments": [comment.to_payload() for comment in self.published_comments],
            "skippedFindingIds": list(self.skipped_finding_ids),
            "summaryCommentId": self.summary_comment_id,
            "observedHeadSha": self.observed_head_sha,
            "diagnostic": self.diagnostic.to_dict() if self.diagnostic else None,
        }


def publication_key(plan: ReviewPublicationPlan) -> str:
    return (
        f"github:{plan.repository}:{plan.pull_number}:{plan.review_id}:{plan.snapshot_sha256[:24]}"
    )


def _failure(code: str, message: str, *, retryable: bool = False) -> GitHubPublisherError:
    return GitHubPublisherError(
        Diagnostic(
            code,
            message,
            retryable=retryable,
            next="inspect the publication diagnostic before retrying the explicit write",
        )
    )


class GitHubPublisher:
    """Publish one verified plan through the user's local ``gh`` credential."""

    def __init__(
        self,
        project_dir: Path,
        *,
        runner: CommandRunner = _run_command,
        timeout_seconds: int = 30,
        page_size: int = 100,
        max_pages: int = 20,
    ) -> None:
        if timeout_seconds <= 0 or not 1 <= page_size <= 100 or max_pages <= 0:
            raise ValueError("invalid GitHub publisher bounds")
        self.project_dir = project_dir.expanduser().resolve()
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.page_size = page_size
        self.max_pages = max_pages

    def _run(
        self,
        operation: str,
        command: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        deadline: float | None = None,
    ) -> bytes:
        timeout_seconds = self.timeout_seconds
        if deadline is not None:
            timeout_seconds = deadline - time.monotonic()
            if timeout_seconds <= 0:
                raise _failure(
                    "github_publication_timeout", f"{operation} timed out", retryable=True
                )
        try:
            if input_bytes is None:
                result = self.runner(
                    command,
                    cwd=self.project_dir,
                    timeout_seconds=timeout_seconds,
                )
            else:
                result = self.runner(
                    command,
                    cwd=self.project_dir,
                    timeout_seconds=timeout_seconds,
                    input_bytes=input_bytes,
                )
        except (OSError, UnicodeError, ValueError) as error:
            raise _failure("github_publication_failed", f"{operation} could not run") from error
        if result.output_truncated:
            raise _failure(
                "github_publication_response_invalid",
                f"{operation} exceeded the bounded output limit",
            )
        if result.returncode == 124:
            raise _failure("github_publication_timeout", f"{operation} timed out", retryable=True)
        if result.returncode != 0:
            raise _failure(
                "github_publication_failed",
                f"{operation} failed with exit code {result.returncode}",
                retryable=True,
            )
        return result.stdout

    def _head(
        self,
        plan: ReviewPublicationPlan,
        *,
        deadline: float | None = None,
        expected_base_sha: str | None = None,
        expected_visibility: str | None = None,
    ) -> str:
        endpoint = f"repos/{plan.repository}/pulls/{plan.pull_number}"
        raw = self._run(
            "GitHub pull-request head lookup", ["gh", "api", endpoint], deadline=deadline
        )
        try:
            value = json.loads(raw.decode("utf-8"))
            head = value["head"]["sha"]
        except (UnicodeDecodeError, TypeError, ValueError, KeyError) as error:
            raise _failure(
                "github_publication_response_invalid",
                "GitHub head lookup returned malformed data",
            ) from error
        if not isinstance(head, str) or not head:
            raise _failure(
                "github_publication_response_invalid",
                "GitHub head lookup did not return a commit SHA",
            )
        if expected_base_sha is not None or expected_visibility is not None:
            try:
                base = value["base"]["sha"]
            except (KeyError, TypeError) as error:
                raise _failure(
                    "github_publication_response_invalid",
                    "GitHub identity lookup did not return a base SHA",
                ) from error
            visibility = _metadata_visibility(value)
            if (
                not isinstance(base, str)
                or (expected_base_sha is not None and base.lower() != expected_base_sha)
                or (expected_visibility is not None and visibility != expected_visibility)
            ):
                raise _failure(
                    "github_source_identity_changed",
                    "pull-request base or visibility changed before/during publication",
                )
        return head.lower()

    def _page(
        self, endpoint: str, page: int, *, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        command = [
            "gh",
            "api",
            endpoint,
            "--method",
            "GET",
            "-f",
            f"per_page={self.page_size}",
            "-f",
            f"page={page}",
        ]
        raw = self._run(
            "GitHub publication reconciliation",
            command,
            deadline=deadline,
        )
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError) as error:
            raise _failure(
                "publication_reconciliation_incomplete",
                "GitHub reconciliation returned malformed JSON",
            ) from error
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise _failure(
                "publication_reconciliation_incomplete",
                "GitHub reconciliation did not return a bounded object list",
            )
        return value

    def _existing_bodies(
        self, plan: ReviewPublicationPlan, *, deadline: float | None = None
    ) -> tuple[str, ...]:
        bodies: list[str] = []
        endpoints = (
            f"repos/{plan.repository}/pulls/{plan.pull_number}/comments",
            f"repos/{plan.repository}/pulls/{plan.pull_number}/reviews",
        )
        for endpoint in endpoints:
            for page in range(1, self.max_pages + 1):
                values = self._page(endpoint, page, deadline=deadline)
                for value in values:
                    body = value.get("body")
                    if body is None:
                        body = ""
                    if not isinstance(body, str):
                        raise _failure(
                            "publication_reconciliation_incomplete",
                            "GitHub reconciliation returned an item without a text body",
                        )
                    bodies.append(body)
                if len(values) < self.page_size:
                    break
            else:
                raise _failure(
                    "publication_reconciliation_incomplete",
                    "GitHub reconciliation exceeded its page budget",
                )
        return tuple(bodies)

    def _published_comments(
        self,
        plan: ReviewPublicationPlan,
        review_id: object,
        items: Sequence[Any] | None = None,
        *,
        deadline: float | None = None,
    ) -> tuple[PublishedComment, ...]:
        endpoint = f"repos/{plan.repository}/pulls/{plan.pull_number}/reviews/{review_id}/comments"
        inline_items = tuple(item for item in (items or plan.items) if item.target is not None)
        comments_by_finding: dict[str, PublishedComment] = {}
        observed_comment_ids: set[str] = set()
        for page in range(1, self.max_pages + 1):
            values = self._page(endpoint, page, deadline=deadline)
            for value in values:
                comment_id = value.get("id")
                if type(comment_id) is not int or comment_id <= 0:
                    raise _failure(
                        "github_publication_response_invalid",
                        "GitHub publication did not return valid comment ids",
                    )
                body = value.get("body")
                if not isinstance(body, str):
                    raise _failure(
                        "publication_reconciliation_incomplete",
                        "GitHub publication comment lacks a text body for finding reconciliation",
                    )
                matches = [item for item in inline_items if item.marker in body]
                if len(matches) != 1:
                    raise _failure(
                        "publication_reconciliation_incomplete",
                        "GitHub publication comment could not be bound to one finding",
                    )
                finding_id = matches[0].finding_id
                normalized_comment_id = str(comment_id)
                if (
                    finding_id in comments_by_finding
                    or normalized_comment_id in observed_comment_ids
                ):
                    raise _failure(
                        "publication_reconciliation_incomplete",
                        "GitHub publication returned duplicate finding/comment evidence",
                    )
                observed_comment_ids.add(normalized_comment_id)
                comments_by_finding[finding_id] = PublishedComment(
                    finding_id=finding_id,
                    comment_id=normalized_comment_id,
                )
            if len(values) < self.page_size:
                if set(comments_by_finding) != {item.finding_id for item in inline_items}:
                    raise _failure(
                        "publication_reconciliation_incomplete",
                        "GitHub publication did not return one comment for every inline finding",
                    )
                return tuple(comments_by_finding[item.finding_id] for item in inline_items)
        raise _failure(
            "github_publication_response_invalid",
            "GitHub publication comment lookup exceeded its page budget",
        )

    def _published_comment_ids(
        self,
        plan: ReviewPublicationPlan,
        review_id: object,
        *,
        deadline: float | None = None,
    ) -> tuple[str, ...]:
        return tuple(
            comment.comment_id
            for comment in self._published_comments(plan, review_id, deadline=deadline)
        )

    @staticmethod
    def _summary_body(plan: ReviewPublicationPlan, items: Sequence[Any]) -> str:
        summary_items = [item.body for item in items if item.target is None]
        heading = f"reviewctl review {plan.review_id} at head {plan.head_sha}"
        return heading if not summary_items else heading + "\n\n" + "\n\n".join(summary_items)

    def _post(
        self,
        plan: ReviewPublicationPlan,
        items: Sequence[Any],
        *,
        deadline: float | None = None,
    ) -> str:
        endpoint = f"repos/{plan.repository}/pulls/{plan.pull_number}/reviews"
        command = [
            "gh",
            "api",
            endpoint,
            "--method",
            "POST",
            "--input",
            "-",
        ]
        payload: dict[str, Any] = {
            "body": self._summary_body(plan, items),
            "event": "COMMENT",
            "commit_id": plan.head_sha,
            "comments": [
                {
                    "path": item.target.path,
                    "line": item.target.line,
                    "side": item.target.side,
                    "body": item.body,
                }
                for item in items
                if item.target is not None
            ],
        }
        raw = self._run(
            "GitHub comment publication",
            command,
            input_bytes=json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8"),
            deadline=deadline,
        )
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError) as error:
            raise _failure(
                "github_publication_response_invalid",
                "GitHub publication returned malformed JSON",
            ) from error
        if not isinstance(value, dict):
            raise _failure(
                "github_publication_response_invalid",
                "GitHub publication did not return a review id",
            )
        review_id = value.get("id")
        if type(review_id) is not int or review_id <= 0:
            raise _failure(
                "github_publication_response_invalid",
                "GitHub publication did not return a valid review id",
            )
        return str(review_id)

    def publish(
        self,
        plan: ReviewPublicationPlan,
        *,
        expected_base_sha: str | None = None,
        expected_visibility: str | None = None,
    ) -> PublicationResult:
        key = publication_key(plan)
        posted: dict[str, Any] | None = None
        skipped: tuple[str, ...] = ()
        if not plan.executable:
            diagnostic = Diagnostic(
                "github_publication_plan_invalid",
                "publication plan is not executable",
                next="run a complete accepted review before publishing",
            )
            return PublicationResult(key, plan.head_sha, "plan_invalid", diagnostic=diagnostic)
        deadline = time.monotonic() + self.timeout_seconds
        try:
            initial_head = self._head(
                plan,
                deadline=deadline,
                expected_base_sha=expected_base_sha,
                expected_visibility=expected_visibility,
            )
            if initial_head != plan.head_sha.lower():
                diagnostic = Diagnostic(
                    "github_publication_stale_head",
                    "pull-request head changed before publication",
                    next="rerun the bounded review for the new head SHA",
                )
                return PublicationResult(
                    key,
                    plan.head_sha,
                    "stale_head",
                    observed_head_sha=initial_head,
                    diagnostic=diagnostic,
                )
            if not plan.items:
                return PublicationResult(
                    key, plan.head_sha, "no_findings", observed_head_sha=initial_head
                )
            existing = self._existing_bodies(plan, deadline=deadline)
            pending = tuple(
                item for item in plan.items if not any(item.marker in body for body in existing)
            )
            skipped = tuple(item.finding_id for item in plan.items if item not in pending)
            prepost_head = self._head(
                plan,
                deadline=deadline,
                expected_base_sha=expected_base_sha,
                expected_visibility=expected_visibility,
            )
            if prepost_head != plan.head_sha.lower():
                diagnostic = Diagnostic(
                    "github_publication_stale_head",
                    "pull-request head changed before publication",
                    next="rerun the bounded review for the new head SHA",
                )
                return PublicationResult(
                    key,
                    plan.head_sha,
                    "stale_head",
                    skipped_finding_ids=skipped,
                    observed_head_sha=prepost_head,
                    diagnostic=diagnostic,
                )
            if not pending:
                return PublicationResult(
                    key,
                    plan.head_sha,
                    "skipped_duplicate",
                    skipped_finding_ids=skipped,
                    observed_head_sha=prepost_head,
                )
            review_id = self._post(plan, pending, deadline=deadline)
            posted = {"summaryCommentId": review_id, "comments": (), "commentIds": ()}
            posted["comments"] = self._published_comments(
                plan, review_id, pending, deadline=deadline
            )
            posted["commentIds"] = tuple(comment.comment_id for comment in posted["comments"])
            observed_head = self._head(
                plan,
                deadline=deadline,
                expected_base_sha=expected_base_sha,
                expected_visibility=expected_visibility,
            )
            if observed_head != plan.head_sha.lower():
                diagnostic = Diagnostic(
                    "github_publication_stale_head_race",
                    "pull-request head changed after GitHub accepted the publication",
                    next="do not retry automatically; reconcile markers before any later decision",
                )
                return PublicationResult(
                    key,
                    plan.head_sha,
                    "stale_head_race",
                    published_comment_ids=posted["commentIds"],
                    published_comments=posted["comments"],
                    skipped_finding_ids=skipped,
                    summary_comment_id=posted["summaryCommentId"],
                    observed_head_sha=observed_head,
                    diagnostic=diagnostic,
                )
            return PublicationResult(
                key,
                plan.head_sha,
                "published",
                published_comment_ids=posted["commentIds"],
                published_comments=posted["comments"],
                skipped_finding_ids=skipped,
                summary_comment_id=posted["summaryCommentId"],
                observed_head_sha=observed_head,
            )
        except GitHubPublisherError as error:
            if posted is not None and error.diagnostic.code == "github_source_identity_changed":
                status = "stale_head_race"
            else:
                status = (
                    "reconciliation_incomplete"
                    if error.diagnostic.code == "publication_reconciliation_incomplete"
                    else "failed"
                )
            return PublicationResult(
                key,
                plan.head_sha,
                status,
                published_comment_ids=posted["commentIds"] if posted is not None else (),
                published_comments=posted["comments"] if posted is not None else (),
                skipped_finding_ids=skipped,
                summary_comment_id=posted["summaryCommentId"] if posted is not None else None,
                diagnostic=error.diagnostic,
            )


__all__ = [
    "GitHubPublisher",
    "GitHubPublisherError",
    "PublicationResult",
    "PublishedComment",
    "publication_key",
]
