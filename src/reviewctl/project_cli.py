"""Project-oriented command line entry points."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import tempfile
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from reviewctl.api import (
    FORMAL_PROJECT_TRANSPORTS,
    Finding,
    ReviewClient,
    ReviewRequest,
    ReviewResult,
    finding_id,
    verify_project_receipt,
)
from reviewctl.artifacts import ArtifactStore
from reviewctl.codex_project_transport import CodexProjectTransport
from reviewctl.config import ReviewConfig, load_config
from reviewctl.dimensions import normalize_dimensions
from reviewctl.errors import Diagnostic, JournalOperationError, exit_code_for
from reviewctl.github import (
    GitHubSourceError,
    LocalGitHubSource,
    PullRequestRef,
    PullRequestSnapshot,
    ReviewPublicationPlan,
    build_publication_plan,
)
from reviewctl.github_publisher import GitHubPublisher, PublicationResult, publication_key
from reviewctl.github_receipt import (
    GitHubReceiptError,
    github_review_prompt,
    github_v2_findings,
    write_github_v2_receipt,
)
from reviewctl.identity import ProjectIdentityStore, ensure_project_state_root
from reviewctl.journal import FINDING_STATUSES, ProjectJournal
from reviewctl.pi_transport import PiTransport

PROJECT_TEMPLATE = """# Project-local reviewctl configuration.
# Keep the project private by default. Change privacy_mode to "sensitive" to
# require local execution for every profile.
[project]
id = "{project_id}"
visibility = "private"
privacy_mode = "private"
required_dimensions = ["correctness"]

[profiles.default]
routes = []
dimensions = ["correctness"]
response_contract = "findings-json"
execution = "local"
tools = "none"
timeout_seconds = 300
max_output_tokens = 8000
"""
FINDING_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _project_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _json_default(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=True, indent=2, default=_json_default, sort_keys=True))


def _result_payload(result: ReviewResult) -> dict[str, Any]:
    receipt = str(result.receipt_path)
    if receipt == ".":
        receipt = ""
    payload: dict[str, Any] = {
        "status": result.status,
        "reviewId": result.review_id,
        "receipt": receipt or None,
        "findings": [asdict(finding) for finding in result.findings],
    }
    if result.diagnostic is not None:
        payload["diagnostic"] = result.diagnostic.to_dict()
    return payload


def _print_result(result: ReviewResult, output_format: str, *, stream: Any = None) -> None:
    if stream is None:
        stream = sys.stdout
    if output_format == "json":
        print(
            json.dumps(
                _result_payload(result),
                ensure_ascii=True,
                indent=2,
                default=_json_default,
                sort_keys=True,
            ),
            file=stream,
        )
        return
    print(f"status: {result.status}", file=stream)
    if result.review_id:
        print(f"review: {result.review_id}", file=stream)
    if str(result.receipt_path) != ".":
        print(f"receipt: {result.receipt_path}", file=stream)
    for finding in result.findings:
        location = finding.path or "(project)"
        if finding.line is not None:
            location += f":{finding.line}"
        print(f"[{finding.severity}] {location} — {finding.title}", file=stream)
    if result.diagnostic is not None:
        diagnostic = result.diagnostic
        print(f"diagnostic: {diagnostic.code}: {diagnostic.message}", file=stream)
        if diagnostic.next:
            print(f"next: {diagnostic.next}", file=stream)


def _diagnostic_result(diagnostic: Diagnostic, output_format: str) -> int:
    result = ReviewResult(
        status=diagnostic.code,
        review_id="",
        receipt_path=Path(),
        findings=(),
        diagnostic=diagnostic,
    )
    _print_result(
        result,
        output_format,
        stream=sys.stderr if output_format == "text" else sys.stdout,
    )
    return exit_code_for(diagnostic.code)


def _replace_private_file(path: Path, contents: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    staged_identity = None
    try:
        try:
            staged_identity = os.fstat(descriptor)
            stream = os.fdopen(descriptor, "wb")
        except BaseException:
            try:
                os.close(descriptor)
            except BaseException:
                pass
            raise
        try:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            primary = sys.exc_info()[1]
            if primary is None:
                stream.close()
            else:
                try:
                    stream.close()
                except BaseException:
                    pass
        os.replace(temporary, path)
    except BaseException:
        try:
            candidate = os.stat(temporary, follow_symlinks=False)
            if staged_identity is not None and (candidate.st_dev, candidate.st_ino) == (
                staged_identity.st_dev,
                staged_identity.st_ino,
            ):
                temporary.unlink()
        except BaseException:
            pass
        raise


def init_project(args: Any) -> int:
    project = _project_path(args.project)
    if project.exists() and not project.is_dir():
        return _diagnostic_result(
            Diagnostic("invalid_request", f"project path is not a directory: {project}"),
            "text",
        )
    project.mkdir(parents=True, exist_ok=True)
    try:
        ensure_project_state_root(project, create=False)
    except JournalOperationError as error:
        return _diagnostic_result(error.diagnostic, "text")
    config = project / "reviewctl.toml"
    if config.is_symlink() or (config.exists() and not config.is_file()):
        return _diagnostic_result(
            Diagnostic("invalid_request", f"configuration path is not a regular file: {config}"),
            "text",
        )
    if config.exists() and not args.force:
        return _diagnostic_result(
            Diagnostic(
                "invalid_request",
                f"configuration already exists: {config}",
                next="use --force only after inspecting the existing file",
            ),
            "text",
        )
    identity_store = ProjectIdentityStore(project)
    existing_config: ReviewConfig | None = None
    existing_identity = None
    if config.exists():
        try:
            existing_config = load_config(config, user_path=None)
            identity_store.ensure(existing_config.project.project_id)
        except JournalOperationError as error:
            return _diagnostic_result(error.diagnostic, "text")
        except (OSError, UnicodeError, ValueError) as error:
            return _diagnostic_result(Diagnostic("invalid_request", str(error)), "text")
    else:
        try:
            existing_identity = identity_store.read_existing()
        except JournalOperationError as error:
            return _diagnostic_result(error.diagnostic, "text")
    project_id = (
        existing_config.project.project_id
        if existing_config is not None
        else (
            existing_identity.project_id
            if existing_identity is not None
            else "project-" + secrets.token_hex(12)
        )
    )
    template = PROJECT_TEMPLATE.format(project_id=project_id).replace(
        'privacy_mode = "private"', f'privacy_mode = "{args.mode}"'
    )
    try:
        ensure_project_state_root(project)
    except JournalOperationError as error:
        return _diagnostic_result(error.diagnostic, "text")
    _replace_private_file(config, template.encode("utf-8"))
    if existing_config is None:
        try:
            config_value = load_config(config, user_path=None)
            identity_store.ensure(config_value.project.project_id)
        except JournalOperationError as error:
            return _diagnostic_result(error.diagnostic, "text")
        except (OSError, UnicodeError, ValueError) as error:
            return _diagnostic_result(Diagnostic("invalid_request", str(error)), "text")
    print(config)
    return 0


def review_project(args: Any) -> int:
    try:
        prompt = (
            Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
            if args.prompt_file
            else args.prompt
        )
        client = ReviewClient.from_project(_project_path(args.project))
    except JournalOperationError as error:
        return _diagnostic_result(error.diagnostic, args.format)
    except (OSError, UnicodeError, ValueError) as error:
        return _diagnostic_result(Diagnostic("invalid_request", str(error)), args.format)
    result = client.review(
        ReviewRequest(
            prompt=prompt,
            files=tuple(Path(value).expanduser() for value in args.files),
            profile=args.profile,
            review_id=args.review_id,
            dimensions=tuple(args.dimensions),
        )
    )
    if result.status == "accepted":
        if result.receipt_sha256 is None:
            receipt_diagnostic = Diagnostic(
                "receipt_invalid", "accepted review result is missing its receipt digest"
            )
        else:
            receipt_diagnostic = verify_project_receipt(
                result.receipt_path, expected_sha256=result.receipt_sha256
            )
        if receipt_diagnostic is not None:
            return _diagnostic_result(receipt_diagnostic, args.format)
    _print_result(result, args.format)
    if result.status == "accepted":
        if args.fail_on is not None and any(
            FINDING_SEVERITY_RANK.get(finding.severity, 0) >= FINDING_SEVERITY_RANK[args.fail_on]
            for finding in result.findings
        ):
            return 1
        return 0
    if result.diagnostic is not None:
        return exit_code_for(result.diagnostic.code)
    return exit_code_for(result.status)


@contextmanager
def _materialized_github_files(project_dir: Path, snapshot: PullRequestSnapshot) -> Any:
    project_root = project_dir.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="github-source-") as directory:
        root = Path(directory).resolve()
        if root.is_relative_to(project_root):
            raise OSError("GitHub source staging directory must be outside the project")
        paths: list[Path] = []
        artifacts = ArtifactStore(root)
        for changed_file in snapshot.changed_files:
            paths.append(
                artifacts.write_text(quote(changed_file.path, safe=""), changed_file.content)
            )
        yield tuple(paths)


def _map_github_finding_paths(
    snapshot: PullRequestSnapshot, findings: Sequence[Finding]
) -> tuple[Finding, ...]:
    paths_by_basename: dict[str, str] = {}
    duplicate_basenames: set[str] = set()
    for changed_file in snapshot.changed_files:
        basename = Path(changed_file.path).name
        if basename in paths_by_basename:
            duplicate_basenames.add(basename)
        else:
            paths_by_basename[basename] = changed_file.path
    return tuple(
        replace(finding, path=paths_by_basename[finding.path])
        if finding.path in paths_by_basename and finding.path not in duplicate_basenames
        else finding
        for finding in findings
    )


def _github_plan_payload(plan: ReviewPublicationPlan) -> dict[str, Any]:
    return {**plan.to_payload(), "planSha256": plan.plan_sha256, "mode": "dry-run"}


def _persist_github_plan(plan: ReviewPublicationPlan, receipt_path: Path) -> Path:
    artifacts = ArtifactStore(receipt_path.parent)
    contents = json.dumps(_github_plan_payload(plan), ensure_ascii=True, sort_keys=True, indent=2)
    return artifacts.write_text("publication-plan.json", contents + "\n")


def _record_github_publication_events(
    client: ReviewClient, plan: ReviewPublicationPlan, result: PublicationResult
) -> None:
    journal = client.journal()
    common = {
        "publicationKey": result.publication_key,
        "reviewId": plan.review_id,
        "repository": plan.repository,
        "pullNumber": plan.pull_number,
        "headSha": plan.head_sha,
    }
    for finding_id_value in result.skipped_finding_ids:
        journal.append(
            {
                "type": "github_comment_skipped_duplicate",
                **common,
                "findingId": finding_id_value,
            }
        )
    for published_comment in result.published_comments:
        journal.append(
            {
                "type": "github_comment_published",
                **common,
                "findingId": published_comment.finding_id,
                "commentId": published_comment.comment_id,
            }
        )
    if result.summary_comment_id is not None:
        journal.append(
            {
                "type": "github_summary_published",
                **common,
                "summaryCommentId": result.summary_comment_id,
            }
        )
    if result.status in {"stale_head", "stale_head_race"}:
        journal.append(
            {
                "type": (
                    "github_publication_stale_head_race"
                    if result.status == "stale_head_race"
                    else "github_publication_stale_head"
                ),
                **common,
                "observedHeadSha": result.observed_head_sha,
            }
        )
    elif result.status in {"failed", "reconciliation_incomplete", "plan_invalid"}:
        journal.append(
            {
                "type": "github_publication_failed",
                **common,
                "status": result.status,
                "diagnostic": result.diagnostic.to_dict() if result.diagnostic else None,
            }
        )


def _github_review_transports(project: Path, transport: str | None) -> Any:
    """Build the transport map for a github review.

    Returns None to use the default project transports (pi + codex), which keeps
    OpenRouter exploratory. When --transport openrouter is requested explicitly,
    inject the OpenRouter transport so an openrouter-routed profile becomes
    formal for this review — the CI advisory path. This is the only way to
    promote OpenRouter, matching the _INJECTED_FORMAL_PROJECT_TRANSPORTS gate in
    reviewctl.api.
    """
    if transport != "openrouter":
        return None
    from reviewctl.codex_project_transport import CodexProjectTransport
    from reviewctl.openrouter_project_transport import OpenRouterProjectTransport
    from reviewctl.pi_transport import PiTransport

    return {
        "codex": CodexProjectTransport(project),
        "openrouter": OpenRouterProjectTransport(),
        "pi": PiTransport(),
    }


def github_review_project(args: Any) -> int:
    project = _project_path(args.project)
    try:
        transports = _github_review_transports(project, getattr(args, "transport", None))
        if transports is None:
            client = ReviewClient.from_project(project)
        else:
            client = ReviewClient.from_project(project, transports=transports)
        snapshot = LocalGitHubSource(project).resolve(PullRequestRef(args.repo, args.pr))
    except GitHubSourceError as error:
        return _diagnostic_result(error.diagnostic, args.format)
    except JournalOperationError as error:
        return _diagnostic_result(error.diagnostic, args.format)
    except (OSError, UnicodeError, ValueError) as error:
        return _diagnostic_result(Diagnostic("invalid_request", str(error)), args.format)

    try:
        with _materialized_github_files(project, snapshot) as files:
            source_root = files[0].parent if files else None
            result = client.review(
                ReviewRequest(
                    prompt=github_review_prompt(snapshot),
                    files=files,
                    source_root=source_root,
                    source_names=tuple(item.path for item in snapshot.changed_files),
                    profile=args.profile,
                    review_id=args.review_id,
                    dimensions=tuple(args.dimensions),
                    source_context=snapshot.to_context(),
                )
            )
    except (OSError, UnicodeError, ValueError) as error:
        return _diagnostic_result(Diagnostic("invalid_request", str(error)), args.format)

    receipt_diagnostic = None
    formal_receipt_path: Path | None = None
    formal_findings: tuple[Finding, ...] = ()
    if result.status == "accepted":
        if result.receipt_sha256 is None:
            receipt_diagnostic = Diagnostic(
                "receipt_invalid", "accepted review result is missing its receipt digest"
            )
        else:
            receipt_diagnostic = verify_project_receipt(
                result.receipt_path,
                expected_sha256=result.receipt_sha256,
                expected_profile=args.profile,
            )
            if receipt_diagnostic is None:
                try:
                    promoted_receipt = write_github_v2_receipt(
                        client=client,
                        result=result,
                        snapshot=snapshot,
                        receipt_path=result.receipt_path,
                        profile_name=args.profile,
                    )
                except GitHubReceiptError as error:
                    receipt_diagnostic = error.diagnostic
                else:
                    try:
                        formal_findings = github_v2_findings(promoted_receipt)
                    except GitHubReceiptError as error:
                        receipt_diagnostic = error.diagnostic
                    else:
                        formal_receipt_path = promoted_receipt
                        result = replace(
                            result,
                            receipt_path=promoted_receipt,
                            findings=formal_findings,
                        )
    plan_status = result.status if receipt_diagnostic is None else "receipt_invalid"
    findings = (
        tuple(
            {
                **asdict(mapped_finding),
                "findingId": finding_id(original_finding),
            }
            for original_finding, mapped_finding in zip(
                formal_findings,
                _map_github_finding_paths(snapshot, formal_findings),
                strict=True,
            )
        )
        if plan_status == "accepted"
        else ()
    )
    plan = build_publication_plan(
        snapshot,
        project_id=client.config.project.project_id,
        review_id=result.review_id,
        findings=findings,
        review_status=plan_status,
    )
    publication_plan_artifact: Path | None = None
    if formal_receipt_path is not None:
        try:
            publication_plan_artifact = _persist_github_plan(plan, formal_receipt_path)
        except (OSError, ValueError) as error:
            return _diagnostic_result(
                Diagnostic("receipt_invalid", f"could not persist publication plan: {error}"),
                args.format,
            )
    client.journal().append(
        {
            "type": "github_publication_planned",
            "reviewId": plan.review_id,
            "repository": plan.repository,
            "pullNumber": plan.pull_number,
            "headSha": plan.head_sha,
            "snapshotSha256": plan.snapshot_sha256,
            "planSha256": plan.plan_sha256,
            "executable": plan.executable,
            "mode": "dry-run",
        }
    )
    publication: PublicationResult | None = None
    if args.publish and plan.executable:
        client.journal().append(
            {
                "type": "github_publication_started",
                "publicationKey": publication_key(plan),
                "reviewId": plan.review_id,
                "repository": plan.repository,
                "pullNumber": plan.pull_number,
                "headSha": plan.head_sha,
            }
        )
        publication = GitHubPublisher(project).publish(plan)
        _record_github_publication_events(client, plan, publication)
    review_payload = _result_payload(result)
    review_payload["formalReceipt"] = (
        str(formal_receipt_path) if formal_receipt_path is not None else None
    )
    if result.status == "accepted" and formal_receipt_path is None:
        review_payload["receipt"] = None
        review_payload["findings"] = []
    payload = {
        "snapshot": snapshot.to_context(),
        "review": review_payload,
        "publicationPlan": _github_plan_payload(plan),
        "publicationPlanArtifact": (
            str(publication_plan_artifact) if publication_plan_artifact is not None else None
        ),
        "publication": (
            publication.to_payload()
            if publication is not None
            else {
                "mode": "dry-run",
                "requested": bool(args.publish),
                "status": "not_requested" if not args.publish else "not_published",
                "reason": plan.reason if not plan.executable else None,
            }
        ),
    }
    if receipt_diagnostic is not None:
        payload["review"]["diagnostic"] = receipt_diagnostic.to_dict()
    if args.format == "json":
        _print_json(payload)
    else:
        print(f"repository: {snapshot.ref.repository}#{snapshot.ref.number}")
        print(f"head: {snapshot.head_sha}")
        print(f"review: {result.status} ({result.review_id})")
        print(f"publication plan: dry-run ({'executable' if plan.executable else plan.reason})")
        print(f"plan: {plan.plan_sha256}")
        if publication_plan_artifact is not None:
            print(f"plan artifact: {publication_plan_artifact}")
        if publication is not None:
            print(f"publication: {publication.status}")
            if publication.diagnostic is not None:
                print(
                    f"publication diagnostic: {publication.diagnostic.code}: "
                    f"{publication.diagnostic.message}"
                )
        if receipt_diagnostic is not None:
            print(f"diagnostic: {receipt_diagnostic.code}: {receipt_diagnostic.message}")
    if receipt_diagnostic is not None:
        return exit_code_for(receipt_diagnostic.code)
    if result.status != "accepted":
        return exit_code_for(result.status)
    if publication is not None and publication.status not in {
        "published",
        "skipped_duplicate",
        "no_findings",
    }:
        code = publication.diagnostic.code if publication.diagnostic else publication.status
        return exit_code_for(code)
    return 0


def _status_payload(
    config: ReviewConfig, client: ReviewClient, *, dimension: str | None = None
) -> dict[str, Any]:
    events, diagnostic = client.journal().read_with_diagnostic()
    projected_findings, projection_diagnostic = client.journal().findings_with_diagnostic(
        dimension=dimension
    )
    diagnostic = diagnostic or projection_diagnostic
    if dimension is not None:
        try:
            selected_dimension = normalize_dimensions([dimension], label="status dimension")[0]
        except ValueError as error:
            diagnostic = Diagnostic("invalid_request", str(error))
            selected_dimension = None
        if selected_dimension is not None and diagnostic is None:
            events = [
                event
                for event in events
                if selected_dimension in ProjectJournal._event_dimensions(event)
            ]
    review_ids = {event.get("reviewId") for event in events if event.get("reviewId")}
    payload: dict[str, Any] = {
        "project": config.project.name,
        "projectId": config.project.project_id,
        "portableProjectId": config.project.portable_project_id,
        "visibility": config.project.visibility,
        "privacyMode": config.project.privacy_mode,
        "requiredDimensions": list(config.project.required_dimensions),
        "profiles": sorted(config.profiles),
        "journal": {
            "events": len(events),
            "reviews": len(review_ids),
            "findings": len(projected_findings),
        },
    }
    if diagnostic is not None:
        payload["diagnostic"] = diagnostic.to_dict()
    return payload


def status_project(args: Any) -> int:
    try:
        client = ReviewClient.from_project(_project_path(args.project))
    except JournalOperationError as error:
        return _diagnostic_result(error.diagnostic, args.format)
    except ValueError as error:
        return _diagnostic_result(Diagnostic("config_invalid", str(error)), args.format)
    payload = _status_payload(client.config, client, dimension=args.dimension)
    if args.format == "json":
        _print_json(payload)
    else:
        print(f"project: {payload['project']}")
        print(f"privacy: {payload['privacyMode']}")
        print(f"reviews: {payload['journal']['reviews']}")
        print(f"findings: {payload['journal']['findings']}")
    diagnostic = payload.get("diagnostic")
    return exit_code_for(diagnostic["code"]) if isinstance(diagnostic, dict) else 0


def findings_project(args: Any) -> int:
    if args.status is not None and args.status not in FINDING_STATUSES:
        return _diagnostic_result(
            Diagnostic(
                "invalid_request",
                f"unsupported finding status: {args.status}",
                next="choose open, disputed, fixed, verified, or dismissed",
            ),
            args.format,
        )
    try:
        client = ReviewClient.from_project(_project_path(args.project))
    except JournalOperationError as error:
        return _diagnostic_result(error.diagnostic, args.format)
    except ValueError as error:
        return _diagnostic_result(Diagnostic("config_invalid", str(error)), args.format)
    selected, diagnostic = client.journal().findings_with_diagnostic(
        status=args.status, dimension=args.dimension
    )
    payload: object = selected
    if diagnostic is not None:
        payload = {"findings": selected, "diagnostic": diagnostic.to_dict()}
    if args.format == "json":
        _print_json(payload)
    else:
        for finding in selected:
            print(
                f"[{finding.get('severity', 'unknown')}] "
                f"{finding.get('path', '(project)')} — {finding.get('message', '')}"
            )
        if diagnostic is not None:
            print(f"diagnostic: {diagnostic.code}: {diagnostic.message}")
    return exit_code_for(diagnostic.code) if diagnostic is not None else 0


def set_finding_status(args: Any) -> int:
    try:
        client = ReviewClient.from_project(_project_path(args.project))
        client.journal().append_status_change(
            args.finding_id,
            args.finding_status,
            reason=args.reason,
        )
        finding = client.journal().finding(args.finding_id)
    except JournalOperationError as error:
        return _diagnostic_result(error.diagnostic, args.format)
    except ValueError as error:
        return _diagnostic_result(Diagnostic("config_invalid", str(error)), args.format)
    if finding is None:
        return _diagnostic_result(
            Diagnostic("invalid_request", f"finding not found: {args.finding_id}"),
            args.format,
        )
    if args.format == "json":
        _print_json(finding)
    else:
        print(
            f"[{finding.get('status', 'unknown')}] "
            f"{finding.get('findingId', args.finding_id)} — "
            f"{finding.get('message', '')}"
        )
        if finding.get("statusReason"):
            print(f"reason: {finding['statusReason']}")
    return 0


def verify_journal_project(args: Any) -> int:
    project = _project_path(args.project)
    try:
        config = load_config(project)
        identity = ProjectIdentityStore(project).read_existing()
        state_root = ensure_project_state_root(project, create=False)
        journal = None
        if state_root is None:
            violations: list[str] = []
            events: list[dict[str, Any]] = []
            diagnostic = None
        else:
            journal = ProjectJournal(
                state_root / "journal.jsonl",
                project_id=config.project.project_id if identity is not None else None,
                origin_id=identity.origin_id if identity is not None else None,
                prepare_for_writes=False,
            )
            violations = journal.verify()
            events, diagnostic = journal.read_with_diagnostic()
    except JournalOperationError as error:
        payload = {
            "valid": False,
            "projectId": None,
            "originId": None,
            "sequence": None,
            "compatibility": "invalid",
            "violations": [error.diagnostic.message],
            "diagnostic": error.diagnostic.to_dict(),
        }
        if args.format == "json":
            _print_json(payload)
        else:
            print(f"invalid journal: {error.diagnostic.message}")
        return exit_code_for(error.diagnostic.code)
    except ValueError as error:
        return _diagnostic_result(Diagnostic("config_invalid", str(error)), args.format)
    versioned = [event for event in events if event.get("schemaVersion") == 1]
    payload: dict[str, Any] = {
        "valid": not violations and diagnostic is None,
        "projectId": (journal.project_id if journal is not None else None)
        or (versioned[0].get("projectId") if versioned else config.project.project_id),
        "originId": (journal.origin_id if journal is not None else None)
        or (versioned[0].get("originId") if versioned else None),
        "sequence": versioned[-1].get("sequence", 0) if versioned else 0,
        "compatibility": journal.compatibility() if journal is not None else "empty",
        "violations": violations,
    }
    if diagnostic is not None:
        payload["diagnostic"] = diagnostic.to_dict()
    if args.format == "json":
        _print_json(payload)
    else:
        print(f"valid: {'yes' if payload['valid'] else 'no'}")
        print(f"compatibility: {payload['compatibility']}")
        print(f"sequence: {payload['sequence']}")
        for violation in violations:
            print(f"violation: {violation}")
    return 0 if payload["valid"] else 5


def _capability_payload(capabilities: Any) -> dict[str, object]:
    payload = {key: _json_default(value) for key, value in asdict(capabilities).items()}
    payload["output_token_limit_enforced"] = capabilities.output_token_limit_enforced
    return payload


def doctor_project(args: Any) -> int:
    try:
        config = load_config(_project_path(args.project))
    except ValueError as error:
        return _diagnostic_result(Diagnostic("config_invalid", str(error)), args.format)
    profiles = []
    for profile in config.profiles.values():
        unsupported_transports = sorted(
            {route.transport for route in profile.parsed_routes} - FORMAL_PROJECT_TRANSPORTS
        )
        profiles.append(
            {
                "name": profile.name,
                "routes": list(profile.routes),
                "responseContract": profile.response_contract,
                "execution": profile.execution,
                "tools": profile.tools,
                "timeoutSeconds": profile.timeout_seconds,
                "maxOutputTokens": profile.max_output_tokens,
                "dimensions": list(profile.dimensions),
                "thinking": profile.thinking,
                "projectReviewSupported": not unsupported_transports,
                "unsupportedProjectReviewTransports": unsupported_transports,
            }
        )
    payload = {
        "project": config.project.name,
        "projectId": config.project.project_id,
        "portableProjectId": config.project.portable_project_id,
        "visibility": config.project.visibility,
        "privacyMode": config.project.privacy_mode,
        "requiredDimensions": list(config.project.required_dimensions),
        "profiles": profiles,
        "transports": {
            "pi": {
                "executable": bool(shutil.which(os.environ.get("PI_BIN", "pi"))),
                "capabilities": _capability_payload(PiTransport.capabilities()),
            },
            "codex": {
                "executable": bool(shutil.which(os.environ.get("CODEX_BIN", "codex"))),
                "capabilities": _capability_payload(CodexProjectTransport.capabilities()),
            },
        },
    }
    if args.format == "json":
        _print_json(payload)
    else:
        print(f"project: {config.project.name}")
        print(f"project id: {config.project.project_id}")
        print(f"portable project id: {'yes' if config.project.portable_project_id else 'no'}")
        print(f"privacy: {config.project.privacy_mode}")
        for profile in profiles:
            print(
                f"profile {profile['name']}: {', '.join(profile['routes']) or '(no route)'} "
                f"(thinking={profile['thinking']}, formal-project-review="
                f"{'yes' if profile['projectReviewSupported'] else 'no'})"
            )
        for name, transport in payload["transports"].items():
            print(f"{name} executable: {'yes' if transport['executable'] else 'no'}")
    return 0


def add_project_commands(commands: Any) -> None:
    init = commands.add_parser("init", help="create a project-local reviewctl.toml")
    init.add_argument("--project", default=".")
    init.add_argument("--force", action="store_true")
    init.add_argument("--mode", choices=("personal", "private", "sensitive"), default="private")
    init.set_defaults(handler=init_project)

    review = commands.add_parser("review", help="run one project-scoped review")
    review.add_argument("--project", default=".")
    review.add_argument("--profile", default="default")
    prompt = review.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file")
    review.add_argument("--file", dest="files", action="append", default=[])
    review.add_argument("--review-id")
    review.add_argument("--dimension", dest="dimensions", action="append", default=[])
    review.add_argument("--format", choices=("text", "json"), default="text")
    review.add_argument("--fail-on", choices=tuple(FINDING_SEVERITY_RANK), default=None)
    review.set_defaults(handler=review_project)

    github = commands.add_parser("github", help="run bounded GitHub pull-request workflows")
    github_commands = github.add_subparsers(dest="github_command", required=True)
    github_review = github_commands.add_parser(
        "review",
        help="review a pull request with a registered project-scoped transport",
        description=(
            "Review a pull request and create a local publication plan. "
            "Pi is the default project transport; Codex is available for profiles "
            "that select a codex route."
        ),
    )
    github_review.add_argument("--repo", required=True)
    github_review.add_argument("--pr", required=True, type=int)
    github_review.add_argument("--project", default=".")
    github_review.add_argument("--profile", default="default")
    github_review.add_argument("--dimension", dest="dimensions", action="append", default=[])
    github_review.add_argument("--review-id")
    github_review.add_argument("--format", choices=("text", "json"), default="text")
    github_review.add_argument("--publish", action="store_true")
    github_review.add_argument("--publish-event", choices=("comment",), default="comment")
    github_review.add_argument(
        "--transport",
        choices=("pi", "codex", "openrouter"),
        default=None,
        help=(
            "Explicitly enable a transport for this review. OpenRouter is "
            "exploratory by default; pass --transport openrouter to make an "
            "openrouter-routed profile formal (CI advisory path)."
        ),
    )
    github_review.set_defaults(handler=github_review_project)

    status = commands.add_parser("status", help="show project review status")
    status.add_argument("--project", default=".")
    status.add_argument("--dimension")
    status.add_argument("--format", choices=("text", "json"), default="text")
    status.set_defaults(handler=status_project)

    findings = commands.add_parser("findings", help="show findings from the project journal")
    findings.add_argument("--project", default=".")
    findings.add_argument("--status", choices=tuple(sorted(FINDING_STATUSES)))
    findings.add_argument("--dimension")
    findings.add_argument("--format", choices=("text", "json"), default="text")
    findings.set_defaults(handler=findings_project)
    findings_commands = findings.add_subparsers(dest="findings_command")
    set_status = findings_commands.add_parser(
        "set-status", help="append an auditable finding status transition"
    )
    set_status.add_argument("--project", default=".")
    set_status.add_argument("--id", dest="finding_id", required=True)
    set_status.add_argument(
        "--status",
        dest="finding_status",
        choices=("open", "disputed", "fixed", "verified", "dismissed"),
        required=True,
    )
    set_status.add_argument("--reason", default="")
    set_status.add_argument("--format", choices=("text", "json"), default="text")
    set_status.set_defaults(handler=set_finding_status)

    journal = commands.add_parser("journal", help="inspect the project journal")
    journal_commands = journal.add_subparsers(dest="journal_command", required=True)
    verify = journal_commands.add_parser("verify", help="verify journal continuity read-only")
    verify.add_argument("--project", default=".")
    verify.add_argument("--format", choices=("text", "json"), default="text")
    verify.set_defaults(handler=verify_journal_project)

    doctor = commands.add_parser("doctor", help="inspect safe local review configuration")
    doctor.add_argument("--project", default=".")
    doctor.add_argument("--format", choices=("text", "json"), default="text")
    doctor.set_defaults(handler=doctor_project)
