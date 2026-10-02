"""Promotion of verified project checkpoints to canonical GitHub review receipts."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from reviewctl import __version__
from reviewctl.api import Finding, ReviewResult, finding_id, verify_project_receipt
from reviewctl.artifacts import ArtifactStore
from reviewctl.contracts import (
    ContractContext,
    EvaluationContext,
    EvaluationStatus,
    exact_json_object,
    get_contract,
)
from reviewctl.contracts import (
    canonical_json as contract_canonical_json,
)
from reviewctl.errors import Diagnostic, ReviewctlError
from reviewctl.filesystem import read_confined_bytes
from reviewctl.github import PullRequestSnapshot
from reviewctl.review_flow import consolidate, receipt_contract_identity, validate_v2_receipt


class GitHubReceiptError(ReviewctlError):
    """A verified project checkpoint could not be promoted safely."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.diagnostic = Diagnostic("receipt_invalid", message)


def github_review_prompt(snapshot: PullRequestSnapshot) -> str:
    """Return the exact prompt bound into a GitHub project-review packet."""
    return (
        "Review this GitHub pull request as a bounded, read-only code review.\n"
        f"Repository: {snapshot.ref.repository}\n"
        f"Pull request: {snapshot.ref.number}\n"
        f"Base commit: {snapshot.base_sha}\n"
        f"Head commit: {snapshot.head_sha}\n"
        "Return only the configured findings contract. Report actionable findings "
        "with a path and line only when the line is present on the pull-request diff.\n\n"
        "PULL REQUEST DIFF\n" + snapshot.diff
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _load_json_bytes(contents: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            contents.decode("utf-8"),
            object_pairs_hook=exact_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError) as error:
        raise GitHubReceiptError(f"{label} is not exact JSON: {error}") from error
    if type(value) is not dict:
        raise GitHubReceiptError(f"{label} must be a JSON object")
    return value


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _plain_json(value: object) -> Any:
    return json.loads(contract_canonical_json(value))


def _evaluation_payload(evaluation: Any, context: ContractContext) -> dict[str, Any]:
    coverage = evaluation.coverage
    completion = evaluation.completion_request
    return {
        "name": evaluation.name,
        "version": evaluation.version,
        "preparedSha256": evaluation.prepared_digest,
        "payloadSha256": evaluation.payload_digest,
        "normalizedSha256": evaluation.normalized_digest,
        "normalizedValue": _plain_json(evaluation.value),
        "contractContext": {
            "fileNames": list(context.file_names),
            "reviewDeclarationRequired": context.review_declaration_required,
        },
        "violations": list(evaluation.violations),
        "status": evaluation.status.value,
        "fragments": [
            {
                "fragmentId": fragment.fragment_id,
                "fingerprint": fragment.fingerprint,
                "kind": fragment.kind.value,
                "value": _plain_json(fragment.value),
                "payloadDigest": fragment.payload_digest,
                "scope": list(fragment.scope),
            }
            for fragment in evaluation.valid_fragments
        ],
        "coverage": (
            {
                "requiredFields": list(coverage.required_fields),
                "coveredFields": list(coverage.covered_fields),
                "missingFields": list(coverage.missing_fields),
            }
            if coverage is not None
            else None
        ),
        "completionRequest": (
            {
                "preparedDigest": completion.prepared_digest,
                "packetDigest": completion.packet_digest,
                "missingFields": list(completion.missing_fields),
                "invalidFragmentIndexes": list(completion.invalid_fragment_indexes),
                "violations": list(completion.violations),
            }
            if completion is not None
            else None
        ),
    }


def _valid_usage(usage: object, *, expected_model: str) -> bool:
    if type(usage) is not dict or usage.get("model") != expected_model:
        return False
    provider = usage.get("provider")
    if provider is not None and (type(provider) is not str or not provider.strip()):
        return False
    cost = usage.get("costUsd")
    if cost is not None and (type(cost) not in {int, float} or not math.isfinite(cost) or cost < 0):
        return False
    return all(
        value is None or (type(value) is int and value >= 0)
        for value in (
            usage.get("durationMs"),
            usage.get("inputTokens"),
            usage.get("outputTokens"),
        )
    )


def github_v2_findings(receipt_path: Path) -> tuple[Finding, ...]:
    """Read publication findings only from a valid promoted GitHub receipt."""
    try:
        receipt = _load_json_bytes(
            read_confined_bytes(receipt_path), label="promoted GitHub receipt"
        )
    except OSError as error:
        raise GitHubReceiptError(f"could not read promoted GitHub receipt: {error}") from error
    violations = validate_v2_receipt(receipt)
    if violations:
        raise GitHubReceiptError(
            "promoted GitHub receipt failed V2 validation: " + ", ".join(violations)
        )
    source = receipt.get("source")
    source_files = source.get("files") if type(source) is dict else None
    paths_by_name = (
        {item["name"]: item["path"] for item in source_files} if type(source_files) is list else {}
    )
    try:
        return tuple(
            replace(Finding.from_value(value), path=paths_by_name[value["path"]])
            for value in receipt["findings"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise GitHubReceiptError(
            f"promoted GitHub receipt findings are inconsistent: {error}"
        ) from error


def write_github_v2_receipt(
    *,
    client: Any,
    result: ReviewResult,
    snapshot: PullRequestSnapshot,
    receipt_path: Path,
    profile_name: str,
) -> Path:
    """Promote one accepted, bound project checkpoint to a validated V2 receipt."""
    if result.status != "accepted" or result.receipt_sha256 is None:
        raise GitHubReceiptError("only an accepted review with a receipt digest can be promoted")
    if receipt_path != result.receipt_path:
        raise GitHubReceiptError("project checkpoint path does not match the review result")
    diagnostic = verify_project_receipt(
        receipt_path,
        expected_sha256=result.receipt_sha256,
        expected_profile=profile_name,
    )
    if diagnostic is not None:
        raise GitHubReceiptError(diagnostic.message)

    try:
        checkpoint = _load_json_bytes(read_confined_bytes(receipt_path), label="project checkpoint")
        profile = client.config.profile(profile_name)
        client_origin_id = client.journal().origin_id
    except GitHubReceiptError:
        raise
    except (AttributeError, OSError, TypeError, ValueError) as error:
        raise GitHubReceiptError(f"could not read promotion inputs: {error}") from error
    if (
        checkpoint.get("artifactKind") != "project-review-checkpoint"
        or checkpoint.get("projectCheckpointSchemaVersion") != 2
        or checkpoint.get("status") != "accepted"
        or checkpoint.get("reviewId") != result.review_id
        or checkpoint.get("profile") != profile_name
        or profile.response_contract != "findings-json"
    ):
        raise GitHubReceiptError("project checkpoint is not an accepted findings-json checkpoint")

    if (
        checkpoint.get("configDigest") != client.config.digest
        or checkpoint.get("projectId") != client.config.project.project_id
        or checkpoint.get("originId") != client_origin_id
        or checkpoint.get("privacyMode") != client.config.project.privacy_mode
    ):
        raise GitHubReceiptError("project checkpoint configuration no longer matches this client")

    configured_routes = list(profile.parsed_routes)
    expanded_routes = [route for route in configured_routes for _ in range(profile.max_attempts)]
    checkpoint_attempts = checkpoint.get("attempts")
    accepted_attempts = (
        [attempt for attempt in checkpoint_attempts if attempt.get("status") == "accepted"]
        if type(checkpoint_attempts) is list
        and all(type(attempt) is dict for attempt in checkpoint_attempts)
        else []
    )
    if (
        len(checkpoint_attempts) != 1
        or len(accepted_attempts) != 1
        or checkpoint_attempts[0].get("status") != "accepted"
    ):
        raise GitHubReceiptError("only a checkpoint with one accepted attempt can be promoted")
    accepted_checkpoint_attempt = accepted_attempts[0]
    accepted_number = accepted_checkpoint_attempt.get("attempt")
    if (
        type(accepted_number) is not int
        or accepted_number <= 0
        or accepted_number > len(expanded_routes)
        or [attempt.get("attempt") for attempt in checkpoint_attempts]
        != list(range(1, len(checkpoint_attempts) + 1))
        or accepted_number != len(checkpoint_attempts)
    ):
        raise GitHubReceiptError("project checkpoint accepted attempt is inconsistent")
    selected_route = expanded_routes[accepted_number - 1]
    selected_route_label = f"{selected_route.transport}:{selected_route.model}"
    route_index = (accepted_number - 1) // profile.max_attempts
    if (
        checkpoint.get("route") != selected_route_label
        or accepted_checkpoint_attempt.get("route") != selected_route_label
    ):
        raise GitHubReceiptError("project checkpoint route does not match the selected profile")

    packet_digest = checkpoint.get("packetDigest")
    if not _is_sha256(packet_digest):
        raise GitHubReceiptError("project checkpoint packet digest is invalid")
    packet_path = receipt_path.parent / "packet.json"
    try:
        persisted_packet = read_confined_bytes(packet_path)
    except OSError as error:
        raise GitHubReceiptError(f"could not read frozen packet: {error}") from error
    if not persisted_packet.endswith(b"\n"):
        raise GitHubReceiptError("frozen packet is not canonically terminated")
    packet_bytes = persisted_packet[:-1]
    if _sha256(packet_bytes) != packet_digest:
        raise GitHubReceiptError("frozen packet does not match the project checkpoint")
    packet = _load_json_bytes(packet_bytes, label="frozen packet")

    snapshot_context = snapshot.to_context()
    expected_files = sorted(
        (
            {
                "name": quote(changed_file.path, safe=""),
                "path": changed_file.path,
                "sha256": changed_file.sha256,
            }
            for changed_file in snapshot.changed_files
        ),
        key=lambda item: item["name"],
    )
    packet_files = packet.get("files")
    if (
        set(packet)
        != {
            "promptDigest",
            "contractDigest",
            "projectId",
            "originId",
            "dimensionSchemaVersion",
            "dimensions",
            "files",
            "sourceContext",
        }
        or not _is_sha256(packet.get("promptDigest"))
        or packet.get("projectId") != checkpoint.get("projectId")
        or packet.get("originId") != checkpoint.get("originId")
        or packet.get("dimensionSchemaVersion") != checkpoint.get("dimensionSchemaVersion")
        or packet.get("dimensions") != checkpoint.get("dimensions")
        or packet.get("sourceContext") != snapshot_context
        or checkpoint.get("sourceContext") != snapshot_context
        or type(packet_files) is not list
        or not all(type(item) is dict for item in packet_files)
        or sorted(packet_files, key=lambda item: str(item.get("name"))) != expected_files
    ):
        raise GitHubReceiptError("frozen packet does not match the GitHub snapshot")

    prompt = github_review_prompt(snapshot)
    if packet["promptDigest"] != _sha256(prompt.encode("utf-8")):
        raise GitHubReceiptError("frozen packet prompt does not match the GitHub snapshot")
    source_names = tuple(item["name"] for item in expected_files)
    review_declaration_required = bool(source_names) and selected_route.transport in {
        "codex",
        "openrouter",
    }
    context = ContractContext(
        file_names=source_names,
        review_declaration_required=review_declaration_required,
    )
    contract = get_contract("findings-json")
    prepared = contract.prepare(context)
    first_route_context = ContractContext(
        file_names=source_names,
        review_declaration_required=bool(source_names)
        and configured_routes[0].transport in {"codex", "openrouter"},
    )
    if (
        packet.get("contractDigest") != contract.prepare(first_route_context).digest
        or accepted_checkpoint_attempt.get("contractDigest") != prepared.digest
    ):
        raise GitHubReceiptError("frozen packet contract does not match the accepted route")

    response_binding = checkpoint.get("acceptedResponse")
    response_path = receipt_path.parent / f"attempt-{accepted_number:02d}" / "response.md"
    try:
        response_bytes = read_confined_bytes(response_path)
        response_text = response_bytes.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise GitHubReceiptError(f"could not read accepted response: {error}") from error
    if (
        type(response_binding) is not dict
        or response_binding.get("sha256") != _sha256(response_bytes)
        or response_binding.get("characters") != len(response_text)
    ):
        raise GitHubReceiptError("accepted response does not match its checkpoint binding")
    evaluation = contract.evaluate(
        response_text,
        prepared,
        context,
        evidence=EvaluationContext(packet_digest=packet_digest),
    )
    if evaluation.status is not EvaluationStatus.COMPLETE or evaluation.value is None:
        raise GitHubReceiptError("accepted response is not a complete findings-json response")

    usage = checkpoint.get("usage")
    if not _valid_usage(usage, expected_model=selected_route.model):
        raise GitHubReceiptError("accepted response usage does not match the selected route")
    if checkpoint.get("findings") != [
        {**asdict(finding), "findingId": finding_id(finding)} for finding in result.findings
    ]:
        raise GitHubReceiptError("project checkpoint findings differ from the review result")
    normalized_review = _plain_json(evaluation.value)
    routes = [{"model": route.model, "transport": route.transport} for route in configured_routes]
    transports = {route.transport for route in configured_routes}
    attempt = {
        "number": 1,
        "routeIndex": route_index,
        "route": routes[route_index],
        "transport": selected_route.transport,
        "model": {"requested": selected_route.model, "resolved": usage["model"]},
        "provider": {"requested": [], "resolved": usage.get("provider")},
        "rawResponse": {
            "path": str(response_path),
            "sha256": response_binding["sha256"],
            "characters": response_binding["characters"],
        },
        "result": "accepted",
        "findings": normalized_review["findings"],
        "contractEvaluation": _evaluation_payload(evaluation, context),
        "promotedFragments": [],
        "costUsd": usage.get("costUsd"),
        "durationMs": usage.get("durationMs"),
        "tokens": {
            "input": usage.get("inputTokens"),
            "output": usage.get("outputTokens"),
        },
    }
    receipt: dict[str, Any] = {
        "receiptSchemaVersion": 2,
        "acceptedAttempt": 1,
        "attempts": [attempt],
        "createdAt": checkpoint.get("at"),
        "model": {
            "requested": [route.model for route in configured_routes],
            "resolved": usage["model"],
        },
        "policy": {"sha256": None},
        "prompt": {
            "sha256": packet["promptDigest"],
            "characters": len(prompt),
            "packetSha256": packet_digest,
        },
        "result": "accepted",
        "reviewContract": "findings-json",
        "contract": receipt_contract_identity("findings-json"),
        "reviewId": result.review_id,
        "sourceClass": "proprietary",
        "source": {"files": expected_files},
        "sourceContext": snapshot_context,
        "extension.githubPullRequest": snapshot_context,
        "tool": {"name": "reviewctl", "version": __version__},
        "executionSettings": {
            "timeoutSeconds": profile.timeout_seconds,
            "maxAttempts": profile.max_attempts,
            "requireReviewedFiles": review_declaration_required,
        },
        "executionConfig": {
            "path": str(client.config.path) if client.config.path is not None else None,
            "sha256": client.config.digest,
        },
        "transport": next(iter(transports)) if len(transports) == 1 else "routed",
        "routes": routes,
        "routeProfile": {"name": profile_name},
        "providerPreferences": {},
        "fallbackRelationships": [],
        "consolidatedReview": _plain_json(
            consolidate(
                normalized_review,
                (),
                1,
                contract_context=context,
            ).to_dict()
        ),
        "response": {
            "sha256": response_binding["sha256"],
            "characters": response_binding["characters"],
            "costUsd": usage.get("costUsd"),
            "durationMs": usage.get("durationMs"),
            "provider": usage.get("provider"),
        },
        "findings": normalized_review["findings"],
        "verdict": normalized_review["verdict"],
    }
    if any(route.transport == "kiro" for route in configured_routes):
        receipt["extension.kiroUnresolvedIdentityWaiver"] = True
    if selected_route.transport == "kiro":
        receipt["extension.backendQualification"] = "unqualified"
        receipt["extension.mergeGateEligible"] = False
    receipt["sha256"] = _sha256(contract_canonical_json(receipt))
    violations = validate_v2_receipt(receipt)
    if violations:
        raise GitHubReceiptError(
            "promoted GitHub receipt failed V2 validation: " + ", ".join(violations)
        )
    try:
        return ArtifactStore(receipt_path.parent).write_bytes(
            "github-review-receipt.json", contract_canonical_json(receipt) + b"\n"
        )
    except (OSError, ValueError) as error:
        raise GitHubReceiptError(f"could not persist promoted GitHub receipt: {error}") from error
