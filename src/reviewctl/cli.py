"""Command line interface for independent, bounded review receipts."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, cast
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

try:
    import resource
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    resource = None

from reviewctl import __version__
from reviewctl.backends import (
    BackendCapabilities,
    BackendDescriptor,
    BackendEvidence,
    BackendExecution,
    BackendFamily,
    BackendRegistry,
    BackendRequest,
    DiscoveryKind,
    PersistedResponse,
    ReadOnlyCapability,
    SourceIsolation,
)
from reviewctl.config import THINKING_LEVELS
from reviewctl.contracts import (
    FINDINGS_SCHEMA,
    REVIEW_DECLARATION_CONTRACTS,
    REVIEWED_FILES_SCHEMA,
    ContractCompletionRequest,
    ContractContext,
    ContractEvaluation,
    EvaluationContext,
    EvaluationStatus,
    PreparedContract,
    exact_json_object,
    get_contract,
    require_string_json_object_keys,
    valid_review_basename,
)
from reviewctl.contracts import (
    canonical_json as contract_canonical_json,
)
from reviewctl.errors import Diagnostic, ReviewctlError, exit_code_for
from reviewctl.filesystem import (
    confined_directory_descriptor,
    confined_regular_descriptor,
    confined_relative_directory_descriptor,
    read_confined_bytes,
    read_confined_text,
)
from reviewctl.project_cli import add_project_commands
from reviewctl.range_review import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_MAX_CHUNK_BYTES,
    RangeReviewError,
    build_range_aggregate,
    build_range_manifest,
    manifest_sha256,
    range_identity,
    validate_range_manifest,
    verify_range_aggregate,
)
from reviewctl.review_flow import (
    FallbackRelationship,
    PromotedFragment,
    build_completion_context,
    consolidate,
    promote_fragments,
    receipt_contract_identity,
    render_completion_prompt,
    validate_v2_receipt,
)
from reviewctl.setup import BackendInstallation, LocalExecutionTopology, discover_topology
from reviewctl.transport_canary import (
    CANARY_SOURCE,
    CANARY_SOURCE_NAME,
    build_canary_report,
    canary_prompt,
)

MAX_FILES = 3
MAX_FRAGMENT_BYTES = 128 * 1024
MAX_AGY_STDOUT_BYTES = 4 * 1024 * 1024
MAX_AGY_STDERR_BYTES = 100_000
MAX_KIRO_STDOUT_BYTES = 4 * 1024 * 1024
MAX_KIRO_STDERR_BYTES = 100_000
MAX_LLM_DATABASE_BYTES = 4 * 1024 * 1024
MAX_LLM_STDOUT_BYTES = 4 * 1024 * 1024
MAX_LLM_STDERR_BYTES = 100_000
MAX_OPENROUTER_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_OPENROUTER_STDERR_BYTES = 100_000
MAX_OPENROUTER_STATUS_BYTES = 32
MAX_PI_EXPLORATION_STDOUT_BYTES = 4 * 1024 * 1024
MAX_PI_EXPLORATION_STDERR_BYTES = 100_000
# Pi JSON mode can emit a multi-megabyte event stream when reasoning is enabled;
# retain a finite capture bound while allowing normal long review sessions.
MAX_PI_LEGACY_STDOUT_BYTES = 256 * 1024 * 1024
MAX_PI_LEGACY_STDERR_BYTES = 100_000
# Pi persists its session/runtime state separately from the captured streams.
# Keep this process-wide filesystem guard independent from stream limits.
MAX_PI_PROCESS_FILE_BYTES = 512 * 1024 * 1024
MAX_CODEX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CODEX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_CODEX_STDERR_BYTES = 100_000
MAX_RANGE_CHILD_STDOUT_BYTES = 256 * 1024
MAX_RANGE_CHILD_STDERR_BYTES = 100_000
DEFAULT_MAX_OUTPUT_TOKENS = 16_384
REVIEW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)*$")
FINDING_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
PRODUCT_CONSTRAINT_DISPOSITIONS = {"satisfied", "rejected", "assumed"}
PRODUCT_SCORE_FIELDS = {
    "delivery",
    "domainIntegrity",
    "operationalCorrectness",
    "problemFidelity",
    "scopeDiscipline",
}
RESPONSE_CONTRACTS = {
    "document",
    "verdict",
    "findings-json",
    "product-review-json",
    "product-judge-json",
}
DEFAULT_REVIEW_TIMEOUT_SECONDS = 90
DEFAULT_REVIEW_MAX_ATTEMPTS = 1
TOURNAMENT_TRANSPORTS = {"llm", "codex", "openrouter", "agy", "kiro", "pi"}
TOURNAMENT_COST_MODES = {"metered", "account-included", "subscription"}
ROUTE_TRANSPORTS = {"llm", "codex", "openrouter", "agy", "gemini", "kiro", "pi"}
PI_THINKING_LEVELS = THINKING_LEVELS
OPENROUTER_REASONING_EFFORTS = frozenset({"low", "medium", "high"})
LOCAL_POLICY_TRANSPORTS = frozenset({"codex", "gemini", "kiro", "pi"})
REQUIRED_LOCAL_POLICY_TRANSPORTS = frozenset({"gemini", "kiro", "pi"})
RETRIABLE_REVIEW_RESULTS = {
    "timeout",
    "transport-failed",
    "missing-response",
    "empty",
    "missing-conversation",
    "incomplete",
}
PROVIDER_PREFERENCE_KEYS = {
    "allow_fallbacks",
    "data_collection",
    "require_parameters",
    "only",
    "order",
    "sort",
}
CODEX_FINDINGS_SCHEMA = {
    **FINDINGS_SCHEMA,
    "required": ["verdict", "findings", "reviewedFiles"],
    "properties": {
        **FINDINGS_SCHEMA["properties"],
        "reviewedFiles": REVIEWED_FILES_SCHEMA,
    },
}
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_ESCAPE_BYTES = rb"\x1b\[[0-?]*[ -/]*[@-~]"
KIRO_RESPONSE_PREFIX = re.compile(
    rb"^(?:" + ANSI_ESCAPE_BYTES + rb")*> (?:" + ANSI_ESCAPE_BYTES + rb")*"
)
KIRO_LEADING_UI = re.compile(rb"^(?:" + ANSI_ESCAPE_BYTES + rb")*")
KIRO_TRAILING_UI = re.compile(rb"(?:" + ANSI_ESCAPE_BYTES + rb")+[\r\n]*$")
KIRO_RAW_CREDITS_FOOTER = re.compile(
    rb"\n(?:" + ANSI_ESCAPE_BYTES + rb"|[ \t\r\n])*"
    rb"\xe2\x96\xb8 Credits: [0-9]+(?:\.[0-9]+)?"
    rb"(?: \xe2\x80\xa2 Time: [0-9]+(?:\.[0-9]+)?(?:ms|s|m|h)"
    rb"(?: [0-9]+(?:\.[0-9]+)?(?:ms|s|m|h))*)?"
    rb"(?:" + ANSI_ESCAPE_BYTES + rb"|[ \t\r\n])*$"
)
KIRO_SESSION_ID = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
KIRO_REVIEW_AGENT = {
    "name": "reviewctl_readonly",
    "description": "Ephemeral no-tools review agent managed by reviewctl.",
    "prompt": None,
    "mcpServers": {},
    "tools": [],
    "toolAliases": {},
    "allowedTools": [],
    "resources": [],
    "toolsSettings": {},
    "includeMcpJson": False,
    "model": None,
}

_STRING_SCHEMA = {"type": "string", "minLength": 1}
_STRING_LIST_SCHEMA = {"type": "array", "items": _STRING_SCHEMA}
_NON_EMPTY_STRING_LIST_SCHEMA = {**_STRING_LIST_SCHEMA, "minItems": 1}
_PRODUCT_OBJECT_SCHEMAS = {
    "interactionFlow": {
        "type": "object",
        "required": ["actor", "action", "outcome"],
        "additionalProperties": False,
        "properties": {
            "actor": _STRING_SCHEMA,
            "action": _STRING_SCHEMA,
            "outcome": _STRING_SCHEMA,
        },
    },
    "domainEntities": {
        "type": "object",
        "required": ["name", "purpose"],
        "additionalProperties": False,
        "properties": {"name": _STRING_SCHEMA, "purpose": _STRING_SCHEMA},
    },
    "stateTransitions": {
        "type": "object",
        "required": ["from", "to", "guard"],
        "additionalProperties": False,
        "properties": {"from": _STRING_SCHEMA, "to": _STRING_SCHEMA, "guard": _STRING_SCHEMA},
    },
    "architecture": {
        "type": "object",
        "required": ["boundary", "owns", "commands", "events", "readModels"],
        "additionalProperties": False,
        "properties": {
            "boundary": _STRING_SCHEMA,
            "owns": _STRING_SCHEMA,
            "commands": _STRING_LIST_SCHEMA,
            "events": _STRING_LIST_SCHEMA,
            "readModels": _STRING_LIST_SCHEMA,
        },
    },
    "operationalControls": {
        "type": "object",
        "required": ["control", "approach"],
        "additionalProperties": False,
        "properties": {"control": _STRING_SCHEMA, "approach": _STRING_SCHEMA},
    },
    "constraintChecks": {
        "type": "object",
        "required": ["constraintId", "disposition", "rationale"],
        "additionalProperties": False,
        "properties": {
            "constraintId": _STRING_SCHEMA,
            "disposition": {"type": "string", "enum": sorted(PRODUCT_CONSTRAINT_DISPOSITIONS)},
            "rationale": _STRING_SCHEMA,
        },
    },
}
PRODUCT_REVIEW_SCHEMA = {
    "type": "object",
    "required": [
        "summary",
        "userJobs",
        "mvp",
        "nonGoals",
        "interactionFlow",
        "domainEntities",
        "stateTransitions",
        "architecture",
        "operationalControls",
        "constraintChecks",
        "risks",
        "acceptanceTests",
        "openQuestions",
    ],
    "additionalProperties": False,
    "properties": {
        "summary": _STRING_SCHEMA,
        "userJobs": _NON_EMPTY_STRING_LIST_SCHEMA,
        "mvp": _NON_EMPTY_STRING_LIST_SCHEMA,
        "nonGoals": _NON_EMPTY_STRING_LIST_SCHEMA,
        "interactionFlow": {
            "type": "array",
            "minItems": 1,
            "items": _PRODUCT_OBJECT_SCHEMAS["interactionFlow"],
        },
        "domainEntities": {
            "type": "array",
            "minItems": 1,
            "items": _PRODUCT_OBJECT_SCHEMAS["domainEntities"],
        },
        "stateTransitions": {
            "type": "array",
            "minItems": 1,
            "items": _PRODUCT_OBJECT_SCHEMAS["stateTransitions"],
        },
        "architecture": {
            "type": "array",
            "minItems": 1,
            "items": _PRODUCT_OBJECT_SCHEMAS["architecture"],
        },
        "operationalControls": {
            "type": "array",
            "minItems": 1,
            "items": _PRODUCT_OBJECT_SCHEMAS["operationalControls"],
        },
        "constraintChecks": {
            "type": "array",
            "minItems": 1,
            "items": _PRODUCT_OBJECT_SCHEMAS["constraintChecks"],
        },
        "risks": _NON_EMPTY_STRING_LIST_SCHEMA,
        "acceptanceTests": _NON_EMPTY_STRING_LIST_SCHEMA,
        "openQuestions": {"type": "array", "items": {"type": "string"}},
    },
}
PRODUCT_JUDGE_SCHEMA = {
    "type": "object",
    "required": ["scores", "hardConstraintViolations", "rationale"],
    "additionalProperties": False,
    "properties": {
        "scores": {
            "type": "object",
            "required": sorted(PRODUCT_SCORE_FIELDS),
            "additionalProperties": False,
            "properties": {
                field: {"type": "integer", "minimum": 0, "maximum": 4}
                for field in sorted(PRODUCT_SCORE_FIELDS)
            },
        },
        "hardConstraintViolations": _STRING_LIST_SCHEMA,
        "rationale": _STRING_SCHEMA,
    },
}


def codex_schema(schema: dict[str, object]) -> dict[str, object]:
    """Add frozen-file read proof to a portable structured response schema."""
    properties = dict(schema["properties"])
    properties["reviewedFiles"] = CODEX_FINDINGS_SCHEMA["properties"]["reviewedFiles"]
    return {
        **schema,
        "required": [*schema["required"], "reviewedFiles"],
        "properties": properties,
    }


def response_schema(
    contract: str,
    *,
    codex: bool = False,
    prepared_contract: PreparedContract | None = None,
) -> dict[str, object] | None:
    """Return the strict JSON schema for one supported response contract."""
    if prepared_contract is not None:
        return prepared_contract.schema
    if contract == "findings-json":
        return (
            get_contract(contract)
            .prepare(ContractContext(review_declaration_required=codex))
            .schema
        )
    schema = {
        "product-review-json": PRODUCT_REVIEW_SCHEMA,
        "product-judge-json": PRODUCT_JUDGE_SCHEMA,
    }.get(contract)
    if schema is None:
        return None
    return codex_schema(schema) if codex else schema


@dataclass(frozen=True)
class ReviewRoute:
    """One ordered transport/model route for a review attempt."""

    transport: str
    model: str


@dataclass(frozen=True)
class TournamentCandidate:
    """One independently auditable tournament participant."""

    council_eligible: bool
    family: str
    identifier: str
    model: str
    cost_mode: str
    pricing: tuple[float, float] | None
    provider_preferences: dict[str, object] | None
    transport: str
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class CodexIsolation:
    """Ephemeral Codex home and macOS profile for a proprietary review."""

    environment: dict[str, str]
    home: Path
    profile: Path


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def positive_integer(value: str) -> int:
    """Parse a CLI integer that must be strictly positive."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def write_private_exclusive(
    path: Path,
    contents: bytes,
    *,
    label: str = "raw response evidence",
    expected_parent_identity: tuple[int, int] | None = None,
) -> None:
    with ExitStack() as descriptors:
        try:
            descriptor = descriptors.enter_context(
                confined_regular_descriptor(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    expected_parent_identity=expected_parent_identity,
                )
            )
        except FileExistsError as error:
            if label == "raw response evidence":
                message = (
                    f"{label} collision at {path}: "
                    "the adapter already created the reserved raw-response.txt path; "
                    "configure the adapter to use a different evidence path"
                )
            else:
                message = f"{label} collision at {path}: the reserved path already exists"
            raise RuntimeError(message) from error
        except OSError as error:
            raise RuntimeError(f"{label} path is unsafe at {path}: {error}") from error
        remaining = memoryview(contents)
        while remaining:
            written = os.write(descriptor, remaining)
            if written == 0:
                raise OSError(f"could not finish writing raw response evidence at {path}")
            remaining = remaining[written:]
        if expected_parent_identity is not None:
            try:
                with confined_regular_descriptor(
                    path,
                    os.O_RDONLY,
                    expected_parent_identity=expected_parent_identity,
                ) as persisted_descriptor:
                    written_metadata = os.fstat(descriptor)
                    persisted_metadata = os.fstat(persisted_descriptor)
                    if (written_metadata.st_dev, written_metadata.st_ino) != (
                        persisted_metadata.st_dev,
                        persisted_metadata.st_ino,
                    ):
                        raise OSError("filesystem evidence identity changed")
            except OSError as error:
                raise RuntimeError(f"{label} path is unsafe at {path}: {error}") from error


def canonical_json(value: object) -> bytes:
    require_string_json_object_keys(value)
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact_diagnostic(value: str, *, limit: int = 4000) -> str:
    """Keep provider diagnostics useful without persisting credentials or huge bodies."""
    text = value.replace("\x00", "")
    patterns = (
        (r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+", r"\1[REDACTED]"),
        (r"(?i)(bearer\s+)[^\s]+", r"\1[REDACTED]"),
        (r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED_KEY]"),
        (r"\b(?:key|token|secret)[=:]\s*[^\s,;]+", "[REDACTED_CREDENTIAL]"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text[:limit]


def configure_runtime_logger(
    path: Path, *, max_bytes: int = 5 * 1024 * 1024, backup_count: int = 5
) -> logging.Logger:
    """Create the bounded JSONL diagnostic log used by one reviewctl installation."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("reviewctl.runtime")
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def log_event(logger: logging.Logger, event: str, **fields: object) -> None:
    """Write structured diagnostics without prompts, source contents, or raw credentials."""
    safe_fields = {
        key: redact_diagnostic(value) if key == "diagnostic" and isinstance(value, str) else value
        for key, value in fields.items()
    }
    payload = {"at": utc_now(), "event": event, **safe_fields}
    logger.info(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def parse_route(value: str) -> ReviewRoute:
    """Parse an ordered `transport:model` route specification."""
    transport, separator, model = value.partition(":")
    if not separator or transport not in ROUTE_TRANSPORTS or not model.strip():
        raise ValueError(
            "routes must use transport:model with transport in "
            "llm, codex, openrouter, agy, gemini, kiro, pi"
        )
    return ReviewRoute(transport=transport, model=model.strip())


def load_execution_config(
    parser: argparse.ArgumentParser,
    config_value: str | None,
    *,
    required: bool,
) -> tuple[Path, dict[str, Any], bytes] | None:
    """Read one execution config once for parsing and receipt identity."""
    config_path = Path(
        os.path.abspath(Path(config_value or "~/.config/reviewctl/config.toml").expanduser())
    )
    try:
        raw = read_confined_bytes(config_path)
    except FileNotFoundError:
        if required:
            parser.error(f"reviewctl config does not exist: {config_path}")
        return None
    except OSError as error:
        parser.error(f"could not read reviewctl config {config_path}: {error}")
    try:
        config = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        parser.error(f"could not read reviewctl config {config_path}: {error}")
    return config_path, config, raw


def execution_default_settings(
    parser: argparse.ArgumentParser, config: dict[str, Any], transport: str
) -> dict[str, int]:
    """Validate defaults for one transport from an already loaded config snapshot."""
    defaults = config.get("defaults")
    transport_defaults = defaults.get(transport) if isinstance(defaults, dict) else None
    if transport_defaults is None:
        return {}
    if not isinstance(transport_defaults, dict):
        parser.error(f"defaults.{transport} must be a TOML table")
    settings: dict[str, object] = {}
    for key, minimum, maximum in (
        ("timeout_seconds", 1, None),
        ("max_attempts", 1, 3),
    ):
        value = transport_defaults.get(key)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            parser.error(f"defaults.{transport}.{key} must be a positive integer")
        if maximum is not None and value > maximum:
            parser.error(f"defaults.{transport}.{key} must be from {minimum} to {maximum}")
        settings[key] = value
    return settings


def load_route_profile(
    parser: argparse.ArgumentParser, config_value: str | None, profile: str
) -> tuple[tuple[ReviewRoute, ...], dict[str, object]]:
    """Load one ordered route profile from a user-owned TOML config file."""
    loaded = load_execution_config(parser, config_value, required=True)
    config_path, config, raw = cast(tuple[Path, dict[str, Any], bytes], loaded)
    profiles = config.get("profiles")
    profile_config = profiles.get(profile) if isinstance(profiles, dict) else None
    route_specs = profile_config.get("routes") if isinstance(profile_config, dict) else None
    if (
        not isinstance(route_specs, list)
        or not route_specs
        or not all(isinstance(value, str) and value.strip() for value in route_specs)
    ):
        parser.error(f"profile {profile!r} must define a non-empty routes array")
    try:
        routes = tuple(parse_route(value) for value in route_specs)
    except ValueError as error:
        parser.error(f"profile {profile!r}: {error}")
    route_transports = {route.transport for route in routes}
    transport_default_key = next(iter(route_transports)) if len(route_transports) == 1 else ""
    default_settings = execution_default_settings(parser, config, transport_default_key)
    settings: dict[str, object] = {}
    for key, minimum, maximum in (
        ("timeout_seconds", 1, None),
        ("max_attempts", 1, 3),
    ):
        value = profile_config.get(key) if isinstance(profile_config, dict) else None
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            parser.error(f"profile {profile!r}: {key} must be a positive integer")
        if maximum is not None and value > maximum:
            parser.error(f"profile {profile!r}: {key} must be from {minimum} to {maximum}")
        settings[key] = value
    thinking = profile_config.get("thinking") if isinstance(profile_config, dict) else None
    if thinking is not None:
        normalized_thinking = thinking.strip() if isinstance(thinking, str) else thinking
        if (
            not isinstance(normalized_thinking, str)
            or normalized_thinking not in PI_THINKING_LEVELS
        ):
            parser.error(
                f"profile {profile!r}: thinking must be one of "
                f"{', '.join(sorted(PI_THINKING_LEVELS))}"
            )
        settings["thinking"] = normalized_thinking
    reasoning_effort = (
        profile_config.get("reasoning_effort") if isinstance(profile_config, dict) else None
    )
    if reasoning_effort is not None:
        if (
            not isinstance(reasoning_effort, str)
            or reasoning_effort not in OPENROUTER_REASONING_EFFORTS
        ):
            parser.error(
                f"profile {profile!r}: reasoning_effort must be one of "
                f"{', '.join(sorted(OPENROUTER_REASONING_EFFORTS))}"
            )
        settings["reasoning_effort"] = reasoning_effort
    require_reviewed_files = (
        profile_config.get("require_reviewed_files") if isinstance(profile_config, dict) else None
    )
    if require_reviewed_files is not None:
        if type(require_reviewed_files) is not bool:
            parser.error(f"profile {profile!r}: require_reviewed_files must be boolean")
        settings["require_reviewed_files"] = require_reviewed_files
    return routes, {
        "name": profile,
        "path": str(config_path),
        "sha256": sha256_bytes(raw),
        "settings": settings,
        "defaultSettings": default_settings,
    }


def load_transport_defaults(
    parser: argparse.ArgumentParser, config_value: str | None, transport: str
) -> tuple[dict[str, int], dict[str, str] | None]:
    """Load optional execution defaults for direct transport invocations."""
    loaded = load_execution_config(parser, config_value, required=bool(config_value))
    if loaded is None:
        return {}, None
    config_path, config, raw = loaded
    return execution_default_settings(parser, config, transport), {
        "path": str(config_path),
        "sha256": sha256_bytes(raw),
    }


def review_routes(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> tuple[tuple[ReviewRoute, ...], dict[str, object] | None]:
    """Resolve explicit routes or preserve the legacy single-transport CLI."""
    route_specs = getattr(args, "routes", [])
    profile = getattr(args, "profile", None)
    models = getattr(args, "models", [])
    if profile:
        if models or route_specs:
            parser.error("use --profile or --model/--route, not both")
        return load_route_profile(parser, getattr(args, "config", None), profile)
    if route_specs:
        if models:
            parser.error("use --route or --model, not both")
        try:
            routes = tuple(parse_route(value) for value in route_specs)
        except ValueError as error:
            parser.error(str(error))
        return routes, None
    return tuple(ReviewRoute(args.transport, model) for model in models), None


def fail(parser: argparse.ArgumentParser, message: str) -> None:
    parser.error(message)


def llm_help_payload() -> dict[str, object]:
    """Return stable machine-readable usage guidance for coding agents."""
    return {
        "tool": "reviewctl",
        "purpose": (
            "Explore ideas with Pi, then promote bounded questions to formal review receipts."
        ),
        "commands": {
            "explore": {
                "start": "reviewctl explore start --id ID --model MODEL --cwd PATH --prompt TEXT",
                "resume": "reviewctl explore resume --id ID --prompt TEXT",
                "show": "reviewctl explore show --id ID",
                "promote": {
                    "usage": "reviewctl explore promote --id ID --output PATH",
                    "approval": "never",
                },
            },
            "run": {
                "usage": (
                    "reviewctl run --review-id ID --transport TRANSPORT --model MODEL "
                    "--prompt-file FILE --file SOURCE"
                ),
                "verification": "reviewctl verify RECEIPT.json",
                "approval": (
                    "only when receipt.result is accepted, acceptedAttempt names the accepted "
                    "attempt, receipt verification succeeds, and material findings are "
                    "independently checked"
                ),
            },
            "transport-canary": {
                "usage": "reviewctl transport-canary --profile NAME",
                "purpose": (
                    "make one synthetic provider-backed request through a configured profile"
                ),
                "result": ("writes transport-canary.json only beside one valid canonical receipt"),
                "mutatesProfile": False,
            },
            "setup": {
                "discover": "reviewctl setup discover --format json",
                "show": "reviewctl setup show --format json",
                "check": "reviewctl setup check --backend NAME --format json",
            },
            "github": {
                "review": (
                    "reviewctl github review --repo OWNER/REPO --pr NUMBER "
                    "--project PATH --profile default"
                ),
                "projectTransport": "pi",
                "projectTransports": ["pi", "codex"],
                "note": (
                    "Pi is the default project transport and Codex is available for a "
                    "codex:MODEL profile route. Other direct backends use reviewctl run."
                ),
            },
            "help-llm": "reviewctl help-llm --format json",
            "receipts": {
                "push": "reviewctl receipts push RECEIPT --project PATH --format json",
                "pull": "reviewctl receipts pull sha256:DIGEST --project PATH --format json",
                "config": "[evidence.potzal] with endpoint, namespace and token_env",
                "authority": "current native-object credentials are service-scoped",
                "approval": "storage preserves a receipt; it never grants review acceptance",
                "errors": "inspect diagnostic.code, retryable and next; preserve local evidence",
            },
        },
        "backendSemantics": {
            "availabilityIsNotQualification": True,
            "setupIsLocalOnly": True,
            "setupCallsModels": False,
        },
        "defaults": {
            "explorationRoot": "~/.cache/reviewctl/explorations",
            "explorationTools": "read,grep,find,ls",
            "explorationIsResumable": True,
            "piOutputTokenLimitEnforced": False,
        },
        "errors": {
            "exitCodes": {
                "0": {
                    "meaning": "completed",
                    "next": (
                        "follow the selected command's next step; run and transport-canary create "
                        "receipts"
                    ),
                },
                "1": {
                    "meaning": "unavailable-or-invalid",
                    "next": "inspect the persisted attempt before changing inputs or routes",
                },
                "2": {
                    "meaning": "invocation-error",
                    "next": "correct the named CLI argument, file, config, or policy error",
                },
            },
            "attemptResults": {
                "accepted": {"meaning": "all gates passed", "inspect": ["receipt.json"]},
                "timeout": {
                    "meaning": "transport exceeded the effective timeout",
                    "inspect": ["attempt.json:diagnostic", "receipt.json:executionSettings"],
                },
                "transport-failed": {
                    "meaning": "transport exited unsuccessfully",
                    "inspect": [
                        "attempt.json:exitCode",
                        "attempt.json:diagnostic",
                        "attempt.json:evidence.stderr when non-null",
                    ],
                },
                "missing-response": {
                    "meaning": "transport produced no persisted response record",
                    "inspect": ["attempt.json:evidence", "attempt.json:diagnostic"],
                },
                "model-mismatch": {
                    "meaning": "resolved model differs from the requested model",
                    "inspect": ["attempt.json:model"],
                },
                "provider-mismatch": {
                    "meaning": "observed provider violates the requested provider policy",
                    "inspect": ["attempt.json:provider", "attempt.json:providerPreferences"],
                },
                "empty": {
                    "meaning": "transport persisted an empty response",
                    "inspect": ["attempt.json:evidence", "attempt.json:diagnostic"],
                },
                "missing-conversation": {
                    "meaning": "transport response has no durable conversation identifier",
                    "inspect": ["attempt.json:conversationId", "attempt.json:evidence"],
                },
                "incomplete": {
                    "meaning": "response failed the selected response contract",
                    "inspect": [
                        "attempt.json:contractEvaluation.completionRequest",
                        "attempt.json:promotedFragments",
                        "receipt.json:fallbackRelationships",
                        "attempt.json:rawResponse",
                    ],
                },
            },
            "contractViolations": {
                "prepared-contract": (
                    "prepared contract identity or packet context did not authenticate"
                ),
                "invalid-json": "payload is not exact JSON or contains duplicate object keys",
                "top-level-not-object": "structured payload is not a JSON object",
                "response-fields": "top-level fields differ from the prepared schema",
                "review-declaration": "reviewedFiles does not match the frozen packet",
                "verdict": "verdict is missing, mistyped, or outside the allowed vocabulary",
                "findings-shape": "findings is not an array",
                "finding-fields": "a finding has missing or additional fields",
                "finding-value": "a finding has an invalid severity, line, or blank string",
                "finding-path": "a finding path is outside the frozen packet",
                "verdict-invariant": "verdict and finding count disagree",
            },
            "redaction": "diagnostics are bounded and credential-shaped values are redacted",
        },
        "nextActions": {
            "incomplete": {
                "inspect": [
                    "attempt.json:contractEvaluation.completionRequest",
                    "attempt.json:promotedFragments",
                    "receipt.json:fallbackRelationships",
                    "attempt.json:rawResponse",
                ]
            },
            "invalid": {
                "inspect": [
                    "attempt.json:contractEvaluation.violations",
                    "attempt.json:evaluationError",
                    "attempt.json:rawResponse",
                ]
            },
            "accepted": {
                "inspect": [
                    "receipt.json:verdict",
                    "receipt.json:findings",
                    "receipt.json:consolidatedReview",
                ],
                "run": "reviewctl verify RECEIPT.json",
            },
        },
        "rules": [
            "Exploration responses are working material, not approvals.",
            "Promote only the bounded question and selected source files to formal review.",
            "Do not attach a full conversation as a substitute for source files.",
            "A missing, empty, unavailable, or unverified receipt is not an approval.",
        ],
    }


def help_llm(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Print concise Markdown or JSON guidance intended for LLM tool discovery."""
    payload = llm_help_payload()
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0
    print(
        "# reviewctl\n\n"
        "Use `reviewctl explore` for resumable Pi conversations and product or architecture "
        "exploration. Use `reviewctl run` for a bounded formal review.\n\n"
        "## Exploration\n\n"
        "```bash\n"
        'reviewctl explore start --id ID --model MODEL --cwd PATH --prompt "QUESTION"\n'
        'reviewctl explore resume --id ID --prompt "NEXT QUESTION"\n'
        "reviewctl explore show --id ID\n"
        "reviewctl explore promote --id ID --output PATH\n"
        "```\n\n"
        "Exploration sessions are resumable. Every turn retains its request and manifest; "
        "Pi event, response, stderr, and session artifacts exist only when Pi produces "
        "them. Runner failures are recorded in turn.json:diagnostic. A response is "
        "exploratory working material, not an approval.\n\n"
        "## Formal review\n\n"
        "```bash\n"
        "reviewctl run --review-id ID --transport TRANSPORT --model MODEL "
        "--prompt-file FILE --file SOURCE\n"
        "reviewctl verify RECEIPT.json\n"
        "```\n\n"
        "A formal result requires receipt.result=accepted, a non-null acceptedAttempt, "
        "successful receipt verification, and independent checking of material findings. "
        "Hash verification alone proves integrity, not acceptance.\n\n"
        "## Optional receipt storage\n\n"
        "```bash\n"
        "reviewctl receipts push RECEIPT.json --project PATH --format json\n"
        "reviewctl receipts pull sha256:DIGEST --project PATH --format json\n"
        "```\n\n"
        "Configure an explicit project.id and [evidence.potzal] destination first. "
        "Credentials come from token_env and currently authorize the service, not a project. "
        "Storage preserves receipt bytes and never grants review acceptance. On failure "
        "inspect diagnostic.code, retryable and next; preserve local evidence.\n\n"
        "## Provider-backed transport canary\n\n"
        "```bash\n"
        "reviewctl transport-canary --profile NAME\n"
        "```\n\n"
        "This makes one bounded synthetic request through a configured profile and writes "
        "`transport-canary.json` only beside one valid receipt. It never changes profiles, "
        "routes, policies, credentials, or provider settings. Treat an unavailable canary as "
        "a transport diagnostic, not qualification or approval.\n\n"
        "## GitHub pull-request review\n\n"
        "The project-scoped GitHub flow uses Pi by default and also registers Codex:\n\n"
        "```bash\n"
        "reviewctl github review --repo OWNER/REPO --pr NUMBER --project PATH "
        "--profile default\n"
        "```\n\n"
        "Select `--profile` with a `codex:MODEL` route when the project should use Codex. "
        "Other direct backends remain available through `reviewctl run --transport TRANSPORT`; "
        "an unavailable receipt is not an approval.\n\n"
        "## Diagnose failures\n\n"
        "Do not retry blindly. A run that exits 1 still persists a receipt and attempt evidence. "
        "Read `attempt.json` fields `result`, `diagnostic`, `validationError`, and "
        "`contractEvaluation.violations`; then inspect only the referenced request, response, "
        "session, or stderr artifact. Exit 2 means the CLI rejected an argument or local input "
        "before a review could run. Diagnostics are bounded and credential-shaped values are "
        "redacted. After any correction, run `reviewctl verify RECEIPT.json` on the new receipt.\n"
    )
    return 0


def setup_installation_payload(installation: BackendInstallation) -> dict[str, object]:
    """Serialize one observed backend installation with stable public field names."""
    return {
        "name": installation.name,
        "requestedExecutable": installation.requested_executable,
        "resolvedExecutable": installation.resolved_executable,
        "version": installation.version,
        "availability": installation.availability,
        "qualification": installation.qualification,
        "diagnostics": list(installation.diagnostics),
        "probePerformed": installation.probe_performed,
    }


def setup_topology_payload(topology: LocalExecutionTopology) -> dict[str, object]:
    """Serialize local setup observations without exposing process environment data."""
    return {
        "schemaVersion": topology.schema_version,
        "localOnly": topology.local_only,
        "modelProbePerformed": topology.model_probe_performed,
        "backends": [setup_installation_payload(backend) for backend in topology.backends],
    }


def sanitize_setup_human_text(value: str) -> str:
    """Render terminal controls as inert, single-line escaped text."""
    escaped_controls = {"\r": r"\r", "\n": r"\n", "\t": r"\t"}
    rendered = []
    for character in value:
        code_point = ord(character)
        if code_point <= 0x1F or 0x7F <= code_point <= 0x9F:
            rendered.append(escaped_controls.get(character, f"\\x{code_point:02x}"))
        elif 0xD800 <= code_point <= 0xDFFF:
            rendered.append(f"\\u{code_point:04x}")
        else:
            rendered.append(character)
    return "".join(rendered)


def print_setup_topology(topology: LocalExecutionTopology, output_format: str) -> None:
    """Print stable JSON or concise human-readable local setup observations."""
    if output_format == "json":
        print(
            json.dumps(
                setup_topology_payload(topology), ensure_ascii=True, indent=2, sort_keys=True
            )
        )
        return
    print(f"local-only: {'yes' if topology.local_only else 'no'}")
    print(f"model probes: {'yes' if topology.model_probe_performed else 'no'}")
    for backend in topology.backends:
        details = (
            f"{backend.name}: availability={backend.availability} "
            f"qualification={backend.qualification}"
        )
        if backend.version:
            details = f"{details} version={backend.version}"
        print(sanitize_setup_human_text(details))
        for diagnostic in backend.diagnostics:
            print(f"  diagnostic: {sanitize_setup_human_text(diagnostic)}")


def run_setup(args: argparse.Namespace) -> int:
    """Discover local backend executables and report non-qualifying setup state."""
    topology = discover_topology(build_backend_registry(), environ=os.environ)
    selected_names = set(args.backends)
    selected = tuple(
        backend
        for backend in topology.backends
        if not selected_names or backend.name in selected_names
    )
    selected_topology = LocalExecutionTopology(
        topology.schema_version,
        topology.local_only,
        topology.model_probe_performed,
        selected,
    )
    print_setup_topology(selected_topology, args.format)
    if args.setup_command != "check":
        return 0
    checked = (
        selected
        if selected_names
        else tuple(backend for backend in selected if backend.availability != "not-applicable")
    )
    return 0 if all(backend.availability == "available" for backend in checked) else 1


def validate_request(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> tuple[str, list[Path]]:
    if not REVIEW_ID.fullmatch(args.review_id):
        fail(parser, "invalid review id")
    if (
        not getattr(args, "models", [])
        and not getattr(args, "routes", [])
        and not getattr(args, "profile", None)
    ):
        fail(parser, "at least one --model, --route, or --profile is required")
    if len(args.files) == 0 and getattr(args, "source_class", "proprietary") == "proprietary":
        fail(parser, "at least one --file is required")
    if len(args.files) > MAX_FILES:
        fail(parser, f"at most {MAX_FILES} review files are allowed")
    if args.prompt and args.prompt_file:
        fail(parser, "use either --prompt or --prompt-file")
    if not args.prompt and not args.prompt_file:
        fail(parser, "one of --prompt or --prompt-file is required")

    prompt = Path(args.prompt_file).read_text() if args.prompt_file else args.prompt
    if not prompt.strip():
        fail(parser, "review prompt must not be empty")

    files = [Path(value).resolve() for value in args.files]
    for file in files:
        if not file.is_file():
            fail(parser, f"review file does not exist: {file}")
        if file.stat().st_size > MAX_FRAGMENT_BYTES:
            fail(parser, f"review file exceeds {MAX_FRAGMENT_BYTES} bytes: {file}")
    if not all(valid_review_basename(file.name) for file in files):
        fail(parser, "review files must have safe printable basenames")
    if len({file.name for file in files}) != len(files):
        fail(parser, "review files must have unique basenames")
    return prompt, files


def review_root(artifact_root: Path, review_id: str) -> tuple[Path, tuple[int, int]]:
    turn_name = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}.{secrets.token_hex(3)}"
    root = Path(os.path.abspath(artifact_root.expanduser()))
    review_directory = root / review_id
    directory = review_directory / turn_name
    with confined_directory_descriptor(root, create=True) as root_descriptor:
        with confined_relative_directory_descriptor(
            root_descriptor, (review_id,), create=True
        ) as review_descriptor:
            os.mkdir(turn_name, mode=0o700, dir_fd=review_descriptor)
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            turn_descriptor = os.open(turn_name, flags, dir_fd=review_descriptor)
            try:
                metadata = os.fstat(turn_descriptor)
                identity = (metadata.st_dev, metadata.st_ino)
            finally:
                os.close(turn_descriptor)
    return directory, identity


def git_metadata(cwd: Path) -> dict[str, str | None]:
    def value(*command: str) -> str | None:
        result = subprocess.run(
            ["git", *command], cwd=cwd, text=True, capture_output=True, check=False
        )
        return result.stdout.strip() if result.returncode == 0 else None

    return {
        "head": value("rev-parse", "HEAD"),
        "remote": value("config", "--get", "remote.origin.url"),
        "repositoryRoot": value("rev-parse", "--show-toplevel"),
    }


def source_git_metadata(files: list[Path]) -> dict[str, str | None]:
    """Return provenance only when every reviewed file has one Git checkout."""
    roots: set[Path] = set()
    for file in files:
        result = subprocess.run(
            ["git", "-C", str(file.parent), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {"head": None, "remote": None, "repositoryRoot": None}
        roots.add(Path(result.stdout.strip()).resolve())
    if len(roots) != 1:
        return {"head": None, "remote": None, "repositoryRoot": None}
    return git_metadata(roots.pop())


def load_policy_evidence(path: str) -> tuple[dict[str, Any], bytes]:
    import tomllib

    raw = read_confined_bytes(Path(os.path.abspath(Path(path).expanduser())))
    return tomllib.loads(raw.decode("utf-8")), raw


def load_policy(path: str) -> dict[str, Any]:
    return load_policy_evidence(path)[0]


def policy_sha256(path: str) -> str:
    """Return the digest of the policy bytes applied to a review."""
    return sha256_bytes(read_confined_bytes(Path(os.path.abspath(Path(path).expanduser()))))


def normalize_provider_preferences(value: object) -> dict[str, object] | None:
    """Validate OpenRouter provider routing preferences for reproducible reviews."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - PROVIDER_PREFERENCE_KEYS:
        raise ValueError("provider preferences contain unsupported fields")
    normalized: dict[str, object] = {}
    for key in ("only", "order"):
        item = value.get(key)
        if item is None:
            continue
        if (
            not isinstance(item, list)
            or not item
            or not all(isinstance(provider, str) and provider for provider in item)
        ):
            raise ValueError(f"provider.{key} must be a non-empty string list")
        normalized[key] = item
    allow_fallbacks = value.get("allow_fallbacks")
    if allow_fallbacks is not None:
        if not isinstance(allow_fallbacks, bool):
            raise ValueError("provider.allow_fallbacks must be boolean")
        normalized["allow_fallbacks"] = allow_fallbacks
    require_parameters = value.get("require_parameters")
    if require_parameters is not None:
        if not isinstance(require_parameters, bool):
            raise ValueError("provider.require_parameters must be boolean")
        normalized["require_parameters"] = require_parameters
    data_collection = value.get("data_collection")
    if data_collection is not None:
        if data_collection not in {"allow", "deny"}:
            raise ValueError("provider.data_collection must be allow or deny")
        normalized["data_collection"] = data_collection
    sort = value.get("sort")
    if sort is not None:
        if sort not in {"price", "throughput", "latency"}:
            raise ValueError("provider.sort must be price, throughput, or latency")
        normalized["sort"] = sort
    return normalized or None


def provider_preferences_from_args(args: argparse.Namespace) -> dict[str, object] | None:
    """Construct explicit provider policy from `run` command options."""
    return normalize_provider_preferences(
        {
            "allow_fallbacks": args.provider_allow_fallbacks,
            "data_collection": args.provider_data_collection,
            "require_parameters": args.provider_require_parameters,
            "only": args.provider_only or None,
            "order": args.provider_order or None,
            "sort": args.provider_sort,
        }
    )


def provider_slug(value: str) -> str:
    """Normalize an OpenRouter provider display name for pinned-route comparison."""
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def resolved_provider_matches(
    provider_preferences: dict[str, object] | None, provider: str | None
) -> bool:
    """Require a response to come from an explicitly pinned OpenRouter provider."""
    if provider_preferences is None:
        return True
    requested = provider_preferences.get("only")
    if not isinstance(requested, list):
        return True
    return isinstance(provider, str) and provider_slug(provider) in {
        provider_slug(item) for item in requested if isinstance(item, str)
    }


def openrouter_model_id(model: str) -> str:
    """Remove the local transport prefix before addressing an OpenRouter endpoint."""
    return model.removeprefix("openrouter/")


def endpoint_price_per_million(endpoint: dict[str, object], field: str) -> float | None:
    """Read one OpenRouter endpoint price, expressed in USD per million tokens."""
    pricing = endpoint.get("pricing")
    value = pricing.get(field) if isinstance(pricing, dict) else None
    try:
        return float(value) * 1_000_000
    except TypeError, ValueError:
        return None


def assess_pinned_provider_endpoint(
    candidate: TournamentCandidate, endpoints: list[dict[str, object]]
) -> dict[str, object]:
    """Assess whether a pinned tournament route can honor its JSON contract today."""
    preferences = candidate.provider_preferences or {}
    requested = preferences.get("only")
    if not isinstance(requested, list) or len(requested) != 1 or not isinstance(requested[0], str):
        raise ValueError("provider preflight requires exactly one pinned provider per candidate")
    provider = requested[0]
    endpoint = next(
        (
            item
            for item in endpoints
            if isinstance(item.get("provider_name"), str)
            and provider_slug(str(item["provider_name"])) == provider_slug(provider)
        ),
        None,
    )
    reasons: list[str] = []
    if endpoint is None:
        reasons.append("provider-not-found")
    else:
        if endpoint.get("status") != 0:
            reasons.append("inactive")
        parameters = endpoint.get("supported_parameters")
        if not isinstance(parameters, list) or "response_format" not in parameters:
            reasons.append("missing-response-format")
        if not isinstance(parameters, list) or "structured_outputs" not in parameters:
            reasons.append("missing-structured-outputs")
        expected = candidate.pricing
        actual = (
            (
                endpoint_price_per_million(endpoint, "prompt"),
                endpoint_price_per_million(endpoint, "completion"),
            )
            if expected is not None
            else None
        )
        if (
            actual is None
            or expected is None
            or any(
                actual_value is None or abs(actual_value - expected_value) > 0.000001
                for actual_value, expected_value in zip(actual, expected, strict=True)
            )
        ):
            reasons.append("price-mismatch")
    return {
        "candidate": candidate.identifier,
        "model": openrouter_model_id(candidate.model),
        "provider": provider,
        "result": "accepted" if not reasons else "rejected",
        "reasons": reasons,
    }


def fetch_openrouter_model_endpoints(
    *, api_key: str | None, model: str, timeout_seconds: int
) -> tuple[int, str, dict[str, object] | None]:
    """Fetch one live OpenRouter endpoint inventory without exposing the API key."""
    if not api_key:
        return 127, "OPENROUTER_API_KEY is not configured", None
    request = urlrequest.Request(
        f"https://openrouter.ai/api/v1/models/{urlparse.quote(model, safe='/')}/endpoints",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urlrequest.urlopen(request, timeout=timeout_seconds) as response:
            raw_payload = response.read(MAX_OPENROUTER_RESPONSE_BYTES + 1)
            if len(raw_payload) > MAX_OPENROUTER_RESPONSE_BYTES:
                return 502, "OpenRouter provider preflight response exceeded bounded capture", None
            payload = json.loads(raw_payload)
    except urlerror.HTTPError as error:
        error_body = error.read(MAX_OPENROUTER_RESPONSE_BYTES + 1)
        if len(error_body) > MAX_OPENROUTER_RESPONSE_BYTES:
            return 502, "OpenRouter provider preflight response exceeded bounded capture", None
        return error.code, error_body.decode(errors="replace"), None
    except urlerror.URLError as error:
        return 502, str(error.reason), None
    except TimeoutError:
        return 124, "provider preflight timed out", None
    except json.JSONDecodeError:
        return 502, "OpenRouter returned invalid endpoint JSON", None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return 502, "OpenRouter returned an invalid endpoint response", None
    return 0, "", data


def write_provider_preflight(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Snapshot and validate explicit OpenRouter provider routes before a tournament."""
    plan_path = Path(args.plan).resolve()
    if not plan_path.is_file():
        parser.error(f"tournament plan does not exist: {plan_path}")
    try:
        plan = load_policy(str(plan_path))
        candidates = parse_tournament_candidates(plan)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    selected = [
        candidate
        for candidate in candidates
        if candidate.transport == "openrouter" and candidate.provider_preferences is not None
    ]
    if not selected:
        parser.error("provider preflight requires pinned OpenRouter tournament candidates")
    root = tournament_report_path(plan, plan_path).parent
    endpoint_data: dict[str, dict[str, object]] = {}
    fetch_errors: dict[str, str] = {}
    for candidate in selected:
        model = openrouter_model_id(candidate.model)
        if model in endpoint_data or model in fetch_errors:
            continue
        code, error, payload = fetch_openrouter_model_endpoints(
            api_key=os.environ.get("OPENROUTER_API_KEY"),
            model=model,
            timeout_seconds=args.timeout_seconds,
        )
        if code == 0 and payload is not None:
            endpoint_data[model] = payload
        else:
            fetch_errors[model] = error
    assessments: list[dict[str, object]] = []
    for candidate in selected:
        model = openrouter_model_id(candidate.model)
        if model in fetch_errors:
            preferences = candidate.provider_preferences or {}
            requested = preferences.get("only")
            provider = requested[0] if isinstance(requested, list) and len(requested) == 1 else None
            assessments.append(
                {
                    "candidate": candidate.identifier,
                    "model": model,
                    "provider": provider,
                    "result": "rejected",
                    "reasons": ["endpoint-fetch-failed"],
                }
            )
            continue
        endpoints = endpoint_data[model].get("endpoints")
        if not isinstance(endpoints, list) or not all(isinstance(item, dict) for item in endpoints):
            parser.error(f"OpenRouter returned invalid endpoints for {model}")
        assessments.append(assess_pinned_provider_endpoint(candidate, endpoints))
    result = "accepted" if all(item["result"] == "accepted" for item in assessments) else "rejected"
    snapshot = {
        "assessments": assessments,
        "endpoints": endpoint_data,
        "errors": fetch_errors,
        "generatedAt": utc_now(),
        "planSha256": sha256_bytes(plan_path.read_bytes()),
        "result": result,
    }
    output = root / "provider-preflight.json"
    output.write_bytes(canonical_json(snapshot) + b"\n")
    print(output)
    return 0 if result == "accepted" else 4


def parse_candidate_pricing(value: object) -> tuple[float, float] | None:
    """Validate one metered candidate's declared OpenRouter list pricing."""
    if not isinstance(value, dict) or set(value) != {
        "input_per_million_usd",
        "output_per_million_usd",
    }:
        return None
    input_price = numeric_value(value["input_per_million_usd"])
    output_price = numeric_value(value["output_per_million_usd"])
    if input_price is None or output_price is None or input_price < 0 or output_price < 0:
        return None
    return input_price, output_price


def parse_tournament_candidates(plan: dict[str, Any]) -> list[TournamentCandidate]:
    """Parse the mixed-transport participant list used by product tournaments."""
    raw_blended_price_cap = plan.get("max_blended_price_usd")
    blended_price_cap = (
        numeric_value(raw_blended_price_cap) if raw_blended_price_cap is not None else None
    )
    if raw_blended_price_cap is not None and (blended_price_cap is None or blended_price_cap <= 0):
        raise ValueError("max_blended_price_usd must be a positive number")
    raw_candidates = plan.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("candidate tournament requires a non-empty candidates list")
    candidates: list[TournamentCandidate] = []
    identifiers: set[str] = set()
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            raise ValueError("tournament candidate must be an object")
        identifier = raw.get("id")
        family = raw.get("family")
        model = raw.get("model")
        transport = raw.get("transport")
        cost_mode = raw.get("cost_mode")
        council_eligible = raw.get("council_eligible", True)
        if not all(isinstance(item, str) and item.strip() for item in (identifier, family, model)):
            raise ValueError("tournament candidate requires non-empty id, family, and model")
        if not isinstance(council_eligible, bool):
            raise ValueError("tournament candidate council_eligible must be boolean")
        if not REVIEW_ID.fullmatch(identifier) or identifier in identifiers:
            raise ValueError("tournament candidate ids must be unique valid review ids")
        if transport not in TOURNAMENT_TRANSPORTS or cost_mode not in TOURNAMENT_COST_MODES:
            raise ValueError("tournament candidate has unsupported transport or cost mode")
        try:
            provider_preferences = normalize_provider_preferences(raw.get("provider"))
        except ValueError as error:
            raise ValueError(f"candidate {identifier}: {error}") from error
        if provider_preferences and transport != "openrouter":
            raise ValueError("candidate provider preferences require openrouter transport")
        pricing = parse_candidate_pricing(raw.get("pricing"))
        raw_max_output_tokens = raw.get("max_output_tokens")
        if raw_max_output_tokens is not None and (
            not isinstance(raw_max_output_tokens, int)
            or isinstance(raw_max_output_tokens, bool)
            or raw_max_output_tokens <= 0
        ):
            raise ValueError("tournament candidate max_output_tokens must be a positive integer")
        if cost_mode == "metered":
            if transport != "openrouter" or pricing is None:
                raise ValueError("metered candidates require openrouter transport and pricing")
            if (
                blended_price_cap is not None
                and (2 * pricing[0] + pricing[1]) / 3 > blended_price_cap
            ):
                raise ValueError("metered candidate exceeds max blended price")
        elif pricing is not None:
            raise ValueError(
                "account-included and subscription candidates must not declare pricing"
            )
        identifiers.add(identifier)
        candidates.append(
            TournamentCandidate(
                council_eligible=council_eligible,
                family=family,
                identifier=identifier,
                model=model,
                cost_mode=cost_mode,
                pricing=pricing,
                provider_preferences=provider_preferences,
                transport=transport,
                max_output_tokens=raw_max_output_tokens,
            )
        )
    return candidates


def select_council_judges(
    candidates: list[TournamentCandidate], candidate: TournamentCandidate
) -> tuple[TournamentCandidate, TournamentCandidate]:
    """Choose a distinct Codex judge and a second non-self family judge."""
    eligible = [
        item
        for item in candidates
        if item.identifier != candidate.identifier and item.family != candidate.family
    ]
    codex = next((item for item in eligible if item.transport == "codex"), None)
    external = next((item for item in eligible if item.transport != "codex"), None)
    if codex is None or external is None:
        raise ValueError(
            "council requires one non-self Codex judge and one non-self external judge"
        )
    return codex, external


def build_product_council_plan(
    candidates: list[TournamentCandidate], package: dict[str, Any], mapping: dict[str, Any]
) -> dict[str, Any]:
    """Assign independent judges without adding candidate identity to the public plan."""
    if package.get("format") != "reviewctl.product-blind.v1":
        raise ValueError("council requires a product blind package")
    entries = package.get("entries")
    if not isinstance(entries, list):
        raise ValueError("blind package requires an entries list")
    candidates_by_id = {candidate.identifier: candidate for candidate in candidates}
    assignments: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("blind package entries must be objects")
        blind_id = entry.get("blindId")
        case = entry.get("case")
        if not isinstance(blind_id, str) or not blind_id or not isinstance(case, str) or not case:
            raise ValueError("blind package entries require blindId and case")
        identity = mapping.get(blind_id)
        if not isinstance(identity, dict):
            raise ValueError("blind package mapping is missing an identity")
        candidate_id = identity.get("candidate")
        if identity.get("case") != case or not isinstance(candidate_id, str):
            raise ValueError("blind package mapping does not match its public entry")
        candidate = candidates_by_id.get(candidate_id)
        if candidate is None:
            raise ValueError("blind package candidate is not declared in the tournament plan")
        codex, external = select_council_judges(candidates, candidate)
        assignments.append(
            {
                "blindId": blind_id,
                "case": case,
                "judges": [
                    {"id": codex.identifier, "model": codex.model, "transport": codex.transport},
                    {
                        "id": external.identifier,
                        "model": external.model,
                        "transport": external.transport,
                    },
                ],
            }
        )
    return {
        "entries": assignments,
        "format": "reviewctl.product-council-plan.v1",
        "responseContract": "product-judge-json",
    }


def build_blind_product_package(
    report: dict[str, Any], *, salt: bytes
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Strip candidate identity from accepted product responses before council review."""
    if not salt:
        raise ValueError("blind package requires a non-empty salt")
    raw_runs = report.get("runs")
    if not isinstance(raw_runs, list):
        raise ValueError("tournament report requires a runs list")
    entries: list[dict[str, Any]] = []
    mapping: dict[str, dict[str, str]] = {}
    for run in raw_runs:
        if (
            not isinstance(run, dict)
            or run.get("result") != "accepted"
            or run.get("councilEligible") is False
        ):
            continue
        candidate = run.get("candidate")
        case = run.get("case")
        receipt_path = run.get("receipt")
        if not all(isinstance(item, str) and item for item in (candidate, case, receipt_path)):
            raise ValueError("accepted product run requires candidate, case, and receipt")
        receipt = json.loads(
            read_confined_text(Path(receipt_path)),
            object_pairs_hook=exact_json_object,
            parse_constant=reject_nonstandard_json_constant,
            parse_float=parse_finite_json_float,
        )
        if receipt.get("result") != "accepted" or not valid_receipt(receipt):
            raise ValueError("accepted product run requires a verified accepted receipt")
        response = receipt.get("review")
        if not isinstance(response, dict):
            raise ValueError("accepted product receipt requires a structured review")
        blind_id = sha256_bytes(salt + f"{case}\0{candidate}".encode())[:16]
        if blind_id in mapping:
            raise ValueError("blind package collision")
        response_digest = receipt.get("response", {}).get("sha256")
        entries.append(
            {
                "blindId": blind_id,
                "case": case,
                "response": response,
                "responseSha256": response_digest if isinstance(response_digest, str) else None,
            }
        )
        mapping[blind_id] = {"candidate": candidate, "case": case, "receipt": receipt_path}
    return {"format": "reviewctl.product-blind.v1", "entries": entries}, mapping


def write_blind_product_package(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Create public anonymous responses and a separately stored private identity map."""
    report_path = Path(args.report).resolve()
    output_path = Path(args.output).resolve()
    mapping_path = Path(args.mapping_output).resolve()
    if not report_path.is_file():
        parser.error(f"tournament report does not exist: {report_path}")
    if output_path == mapping_path:
        parser.error("blind package output and private mapping output must differ")
    try:
        report = json.loads(report_path.read_text())
    except json.JSONDecodeError as error:
        parser.error(f"tournament report is not JSON: {error.msg}")
    if not isinstance(report, dict):
        parser.error("tournament report must be a JSON object")
    try:
        package, mapping = build_blind_product_package(report, salt=secrets.token_bytes(32))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(canonical_json(package) + b"\n")
    mapping_path.touch(exist_ok=True)
    mapping_path.chmod(0o600)
    mapping_path.write_bytes(canonical_json(mapping) + b"\n")
    print(output_path)
    return 0


def write_product_council_plan(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Persist public judge assignments from public blinded and private mapping artifacts."""
    plan_path = Path(args.plan).resolve()
    package_path = Path(args.blind_package).resolve()
    mapping_path = Path(args.mapping).resolve()
    output_path = Path(args.output).resolve()
    if not plan_path.is_file() or not package_path.is_file() or not mapping_path.is_file():
        parser.error("council plan, blind package, and private mapping must exist")
    if output_path in {package_path, mapping_path}:
        parser.error("council plan output must not overwrite its input artifacts")
    try:
        plan = load_policy(str(plan_path))
        package = json.loads(package_path.read_text())
        mapping = json.loads(mapping_path.read_text())
        candidates = parse_tournament_candidates(plan)
        if not isinstance(package, dict) or not isinstance(mapping, dict):
            raise ValueError("council artifacts must be JSON objects")
        council_plan = build_product_council_plan(candidates, package, mapping)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(canonical_json(council_plan) + b"\n")
    print(output_path)
    return 0


def write_range_manifest(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Freeze one Git range without invoking a model or granting approval."""
    output = Path(args.output).expanduser().resolve()
    try:
        manifest = build_range_manifest(
            Path(args.repository),
            args.base,
            args.head,
            context_lines=args.context_lines,
            max_chunk_bytes=args.max_chunk_bytes,
            allow_empty=args.allow_empty,
        )
    except RangeReviewError as error:
        parser.error(str(error))
    manifest = {
        **manifest,
        "evidenceStatus": "planning-only",
    }
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        write_private_exclusive(
            output,
            canonical_json(manifest) + b"\n",
            label="range manifest",
        )
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    print(output)
    return 0


def load_range_json(path: Path) -> tuple[bytes, object]:
    """Load one range artifact with duplicate-key and non-standard JSON rejection."""

    def reject_nonstandard_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    raw = read_confined_bytes(path)
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=exact_json_object,
        parse_constant=reject_nonstandard_constant,
    )
    return raw, value


def _range_child_process(command: list[str], *, timeout_seconds: int) -> tuple[int, bytes, bytes]:
    """Run a child review with bounded stdout/stderr capture."""
    checkout_root = Path(__file__).resolve().parents[2]
    process = subprocess.Popen(
        command,
        cwd=checkout_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    captures: dict[str, bytes] = {}

    def drain(stream: object, name: str, limit: int) -> None:
        if stream is None:  # pragma: no cover - Popen with PIPE always supplies both streams
            captures[name] = b""
            return
        retained = bytearray()
        while True:
            chunk = stream.read(64 * 1024)  # type: ignore[union-attr]
            if not chunk:
                break
            if len(retained) <= limit:
                retained.extend(chunk[: limit + 1 - len(retained)])
        captures[name] = bytes(retained)

    stdout_reader = threading.Thread(
        target=drain,
        args=(process.stdout, "stdout", MAX_RANGE_CHILD_STDOUT_BYTES),
        daemon=True,
        name="reviewctl-range-stdout",
    )
    stderr_reader = threading.Thread(
        target=drain,
        args=(process.stderr, "stderr", MAX_RANGE_CHILD_STDERR_BYTES),
        daemon=True,
        name="reviewctl-range-stderr",
    )
    stdout_reader.start()
    stderr_reader.start()
    try:
        process.wait(timeout=timeout_seconds)
        exit_code = process.returncode
    except subprocess.TimeoutExpired:
        terminate_process_group(process)
        reap_process_without_blocking(process)
        exit_code = 124
    stdout_reader.join(timeout=5)
    stderr_reader.join(timeout=5)
    stdout = captures.get("stdout", b"")
    stderr = captures.get("stderr", b"")
    if len(stdout) > MAX_RANGE_CHILD_STDOUT_BYTES or len(stderr) > MAX_RANGE_CHILD_STDERR_BYTES:
        return (
            502,
            stdout[:MAX_RANGE_CHILD_STDOUT_BYTES],
            stderr[:MAX_RANGE_CHILD_STDERR_BYTES],
        )
    if exit_code == 124:
        return 124, stdout, stderr or b"range chunk review timed out"
    return exit_code, stdout, stderr


def _range_chunk_record(
    *, index: int, chunk: dict[str, Any], review_id: str, receipt_path: Path | None
) -> dict[str, Any]:
    """Create a complete aggregate record, including an explicit missing state."""
    expected_review_id = f"{review_id}.chunk-{index}"
    record: dict[str, Any] = {
        "index": index,
        "chunkId": chunk.get("patchSha256"),
        "patchSha256": chunk.get("patchSha256"),
        "reviewId": expected_review_id,
        "receipt": "",
        "receiptFileSha256": "",
        "receiptSha256": "",
        "result": "missing",
        "verdict": None,
        "findings": [],
    }
    if receipt_path is None:
        return record
    try:
        raw, receipt = load_range_json(receipt_path)
    except OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError:
        return record
    if not isinstance(receipt, dict):
        return record
    record.update(
        {
            "receipt": str(receipt_path),
            "receiptFileSha256": sha256_bytes(raw),
            "receiptSha256": receipt.get("sha256", ""),
            "result": receipt.get("result", "missing"),
            "verdict": receipt.get("verdict"),
            "findings": receipt.get("findings", []),
        }
    )
    return record


def run_range_review(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Review every frozen manifest chunk through the normal formal run command."""
    if not args.review_id:
        parser.error("formal range-review requires --review-id")
    if not args.model:
        parser.error("formal range-review requires --model")
    if not args.aggregate_output:
        parser.error("formal range-review requires --aggregate-output")
    manifest_path = Path(args.manifest).expanduser().resolve()
    aggregate_path = Path(args.aggregate_output).expanduser().resolve()
    try:
        _, manifest = load_range_json(manifest_path)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        parser.error(f"could not read range manifest: {error}")
    manifest_violations = validate_range_manifest(manifest)
    if manifest_violations:
        parser.error("invalid range manifest: " + ", ".join(manifest_violations))
    if not isinstance(manifest, dict):  # pragma: no cover - validator returns above
        parser.error("range manifest must be a JSON object")
    if not REVIEW_ID.fullmatch(args.review_id):
        parser.error("invalid range review id")
    if args.prompt and args.prompt_file:
        parser.error("use either --prompt or --prompt-file")
    if not args.prompt and not args.prompt_file:
        parser.error("one of --prompt or --prompt-file is required")
    try:
        prompt = Path(args.prompt_file).read_text() if args.prompt_file else args.prompt
    except (OSError, UnicodeError) as error:
        parser.error(f"could not read review prompt: {error}")
    if not prompt or not prompt.strip():
        parser.error("review prompt must not be empty")
    if args.response_contract != "findings-json":
        parser.error("range-review formal mode requires --response-contract findings-json")
    if not 1 <= args.max_attempts <= 3:
        parser.error("max attempts must be an integer from 1 to 3")
    if args.max_output_tokens <= 0:
        parser.error("max output tokens must be positive")
    chunks = manifest["chunks"]
    if not chunks:
        parser.error("formal range-review requires a manifest with at least one chunk")
    artifact_root = Path(args.artifact_root).expanduser().resolve()
    timeout_seconds = args.timeout_seconds or DEFAULT_REVIEW_TIMEOUT_SECONDS
    records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="reviewctl-range-") as directory:
        root = Path(directory)
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):  # pragma: no cover - validator above
                parser.error(f"invalid range chunk {index}")
            try:
                patch = base64.b64decode(chunk["patch"].encode("ascii"), validate=True)
            except (KeyError, UnicodeEncodeError, ValueError) as error:  # pragma: no cover
                parser.error(f"invalid range chunk {index}: {error}")
            if len(patch) > MAX_FRAGMENT_BYTES:
                parser.error(
                    f"range chunk {index} exceeds {MAX_FRAGMENT_BYTES} bytes; rebuild with a "
                    "smaller --max-chunk-bytes"
                )
            patch_path = root / f"chunk-{index:04d}.patch"
            prompt_path = root / f"chunk-{index:04d}.prompt.txt"
            context_path = root / f"chunk-{index:04d}.context.json"
            patch_path.write_bytes(patch)
            context_path.write_bytes(
                canonical_json(
                    {
                        "rangeReviewSchemaVersion": 1,
                        "manifestSha256": manifest_sha256(manifest),
                        "range": range_identity(manifest),
                        "chunkIndex": index,
                        "chunkCount": len(chunks),
                        "chunkId": chunk["patchSha256"],
                    }
                )
                + b"\n"
            )
            identity_text = json.dumps(
                range_identity(manifest), sort_keys=True, separators=(",", ":")
            )
            chunk_prompt = (
                f"{prompt}\n\n"
                "This is a formal review of one immutable chunk from a larger Git range. "
                "Review only the supplied patch and do not infer facts outside it.\n"
                f"Range identity: {identity_text}\n"
                f"Manifest SHA-256: {manifest_sha256(manifest)}\n"
                f"Chunk index: {index}\n"
                f"Chunk SHA-256: {chunk['patchSha256']}\n"
                f"Chunk paths: {json.dumps(chunk['paths'], ensure_ascii=True)}\n"
                "Return the findings-json contract. The reviewedFiles value must be exactly "
                f'["chunk-{index:04d}.patch"].'
            )
            prompt_path.write_text(chunk_prompt)
            command = [
                sys.executable,
                "-m",
                "reviewctl",
                "run",
                "--review-id",
                f"{args.review_id}.chunk-{index}",
                "--prompt-file",
                str(prompt_path),
                "--file",
                str(patch_path),
                "--range-context-file",
                str(context_path),
                "--model",
                args.model,
                "--transport",
                args.transport,
                "--source-class",
                args.source_class,
                "--response-contract",
                "findings-json",
                "--artifact-root",
                str(artifact_root),
                "--timeout-seconds",
                str(timeout_seconds),
                "--max-output-tokens",
                str(args.max_output_tokens),
                "--max-attempts",
                str(args.max_attempts),
            ]
            if getattr(args, "reasoning_effort", None) is not None:
                command.extend(("--reasoning-effort", args.reasoning_effort))
            if args.policy:
                command.extend(("--policy", str(Path(args.policy).expanduser().resolve())))
            before_receipts = receipt_fingerprints(artifact_root)
            return_code, _, _ = _range_child_process(command, timeout_seconds=timeout_seconds + 10)
            receipt_path = tournament_receipt_path(
                artifact_root,
                before_receipts,
                f"{args.review_id}.chunk-{index}",
            )
            record = _range_chunk_record(
                index=index,
                chunk=chunk,
                review_id=args.review_id,
                receipt_path=receipt_path,
            )
            if return_code != 0:
                record["result"] = "unavailable"
                record["verdict"] = None
                record["findings"] = []
            records.append(record)
    aggregate = build_range_aggregate(
        manifest,
        args.review_id,
        records,
        receipt_loader=lambda path: load_range_json(Path(path).expanduser().resolve()),
    )
    try:
        aggregate_path.parent.mkdir(parents=True, exist_ok=True)
        write_private_exclusive(
            aggregate_path,
            canonical_json(aggregate) + b"\n",
            label="range aggregate evidence",
        )
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    print(aggregate_path)
    return 0 if aggregate["result"] == "accepted" else 1


def range_review_command(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Dispatch the backward-compatible manifest mode or formal execution mode."""
    if args.manifest is not None:
        if args.repository or args.base or args.head or args.output:
            parser.error("formal range-review cannot combine --manifest with builder arguments")
        return run_range_review(parser, args)
    if args.review_id or args.aggregate_output or args.model:
        parser.error("formal range-review requires --manifest")
    if not args.repository or not args.base or not args.head or not args.output:
        parser.error("manifest builder requires --repository, --base, --head, and --output")
    return write_range_manifest(parser, args)


def verify_range_aggregate_command(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    aggregate_path = Path(args.aggregate).expanduser().resolve()
    try:
        _, manifest = load_range_json(manifest_path)
        _, aggregate = load_range_json(aggregate_path)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        parser.error(f"could not read range evidence: {error}")

    def load_receipt(path: str) -> tuple[bytes, object] | None:
        try:
            return load_range_json(Path(path).expanduser().resolve())
        except OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError:
            return None

    violations = verify_range_aggregate(
        manifest,
        aggregate,
        load_receipt,
        receipt_validator=validate_v2_receipt,
    )
    payload = {
        "manifest": str(manifest_path),
        "aggregate": str(aggregate_path),
        "valid": not violations,
        "violations": list(violations),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if not violations else exit_code_for("receipt_invalid")


@contextmanager
def frozen_review_files(files: list[Path]) -> Iterator[tuple[list[dict[str, str]], list[Path]]]:
    """Provide immutable, private snapshots for a single model invocation round."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="reviewctl-input-") as directory:
        root = Path(directory)
        source: list[dict[str, str]] = []
        snapshots: list[Path] = []
        for file in sorted(files, key=lambda item: item.name):
            contents = file.read_bytes()
            snapshot = root / file.name
            snapshot.write_bytes(contents)
            source.append(
                {
                    "name": file.name,
                    "path": str(file),
                    "sha256": sha256_bytes(contents),
                }
            )
            snapshots.append(snapshot)
        yield source, snapshots


def review_source_roots(files: list[Path]) -> list[Path]:
    """Return source roots denied to Codex while it reviews their snapshots."""
    roots: list[Path] = []
    for file in files:
        result = subprocess.run(
            ["git", "-C", str(file.parent), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=False,
        )
        root = Path(result.stdout.strip()).resolve() if result.returncode == 0 else file.parent
        root = root.resolve()
        if root not in roots:
            roots.append(root)
    return roots


def sandbox_profile_path(path: Path) -> str:
    """Encode one canonical path as a sandbox-exec profile string literal."""
    return json.dumps(str(path.resolve()))


def account_home() -> Path:
    """Return the login account home without trusting the HOME environment variable."""
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except (ImportError, KeyError, OSError) as error:
        raise RuntimeError("Codex isolation could not resolve the login account home") from error


def codex_process_environment(
    source_environ: Mapping[str, str], overrides: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build a Codex child environment from an explicit non-secret operational allowlist.

    Credentials remain file-based through CODEX_AUTH_FILE; API keys, cloud credentials,
    tokens, proxy URLs, and arbitrary ambient variables are intentionally excluded.
    """
    allowed_keys = (
        "PATH",
        "SYSTEMROOT",
        "HOME",
        "CODEX_HOME",
        "CODEX_AUTH_FILE",
        "CODEX_CA_CERTIFICATES",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    )
    configured = overrides or {}
    return {
        key: configured[key] if key in configured else source_environ[key]
        for key in allowed_keys
        if key in configured or key in source_environ
    }


@contextmanager
def codex_isolation(
    source_roots: list[Path], *, auth_path: Path | None = None
) -> Iterator[CodexIsolation]:
    """Create a minimal Codex home and deny reads from reviewed source roots.

    macOS sandbox-exec cannot provide a portable whole-filesystem boundary. This
    targeted profile guarantees that the original proprietary checkout cannot be
    read after frozen snapshots are created; the prompt and Codex working
    directory point only at those snapshots.
    """
    sandbox = shutil.which("sandbox-exec")
    if sandbox is None:
        raise RuntimeError("proprietary Codex reviews require macOS sandbox-exec")
    configured_auth = os.environ.get("CODEX_AUTH_FILE")
    source_auth = auth_path or (
        Path(configured_auth) if configured_auth else account_home() / ".codex" / "auth.json"
    )
    if not source_auth.is_file():
        raise RuntimeError(f"Codex isolation requires auth file: {source_auth}")

    with tempfile.TemporaryDirectory(prefix="reviewctl-codex-") as directory:
        home = Path(directory)
        copied_auth = home / "auth.json"
        shutil.copyfile(source_auth, copied_auth)
        copied_auth.chmod(0o600)
        profile = home / "source-root-deny.sb"
        source_denies = "\n".join(
            "\n".join(
                [
                    f"(deny file-read* (subpath {sandbox_profile_path(root)}))",
                    f"(deny file-write* (subpath {sandbox_profile_path(root)}))",
                ]
            )
            for root in source_roots
        )
        home_write_deny = f"(deny file-write* (subpath {sandbox_profile_path(account_home())}))"
        denies = f"{source_denies}\n{home_write_deny}"
        profile.write_text(f"(version 1)\n(allow default)\n{denies}\n")
        environment = codex_process_environment(
            os.environ,
            {"CODEX_HOME": str(home), "HOME": str(home), "TMPDIR": str(home)},
        )
        environment.pop("CODEX_AUTH_FILE", None)
        yield CodexIsolation(environment=environment, home=home, profile=profile)


def policy_entry(
    policy: dict[str, Any], model: str, *, transport: str | None = None
) -> dict[str, Any]:
    models = policy.get("models")
    if type(models) is dict and model in models:
        entry = models[model]
        return entry if type(entry) is dict else {}
    if transport in LOCAL_POLICY_TRANSPORTS:
        transports = policy.get("transports")
        if type(transports) is dict and transport in transports:
            entry = transports[transport]
            return entry if type(entry) is dict else {}
    return {}


def source_allowed(policy: dict[str, Any], model: str, *, transport: str | None = None) -> bool:
    return policy_entry(policy, model, transport=transport).get("source_allowed") is True


def unresolved_identity_waived(
    policy: dict[str, Any], model: str, *, transport: str | None = None
) -> bool:
    return policy_entry(policy, model, transport=transport).get("allow_unresolved_identity") is True


def reap_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait()
    except ChildProcessError, OSError:
        pass


def reap_process_without_blocking(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=0)
    except subprocess.TimeoutExpired:
        threading.Thread(
            target=reap_process,
            args=(process,),
            daemon=True,
            name=f"reviewctl-reap-{process.pid}",
        ).start()
    except ChildProcessError, OSError:
        pass


def terminate_process_group(process: subprocess.Popen[bytes], *, grace_seconds: float = 5) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        reap_process_without_blocking(process)
        return
    if grace_seconds <= 0:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        reap_process_without_blocking(process)
        return
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            reap_process_without_blocking(process)
        except ChildProcessError, OSError:
            pass


def _communicate_kiro_bounded(
    process: subprocess.Popen[bytes],
    *,
    input_bytes: bytes | None,
    timeout_seconds: float,
    stdout_limit: int,
    stderr_limit: int,
) -> tuple[bytes, bytes, bool, bool, bool]:
    """Drain Kiro pipes without constraining files that Kiro writes internally."""
    streams = (
        (getattr(process, "stdout", None), stdout_limit),
        (getattr(process, "stderr", None), stderr_limit),
    )

    def bounded(value: object, limit: int) -> tuple[bytes, bool]:
        data = value if isinstance(value, bytes) else b""
        return data[:limit], len(data) > limit

    # Keep compatibility with lightweight fake processes used by the unit tests.
    # Real Popen instances below always expose both pipe streams.
    if any(stream is None for stream, _ in streams):
        try:
            stdout, stderr = process.communicate(input=input_bytes, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            terminate_process_group(process, grace_seconds=0)
            stdout, stdout_truncated = bounded(error.output, stdout_limit)
            stderr, stderr_truncated = bounded(error.stderr, stderr_limit)
            return stdout, stderr, True, stdout_truncated or stderr_truncated, False
        stdout, stdout_truncated = bounded(stdout, stdout_limit)
        stderr, stderr_truncated = bounded(stderr, stderr_limit)
        return stdout, stderr, False, stdout_truncated or stderr_truncated, False

    captures: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    output_exceeded = threading.Event()
    capture_failed = threading.Event()
    capture_interrupted = threading.Event()
    terminated = threading.Event()
    termination_lock = threading.Lock()
    reader_finished = {"stdout": threading.Event(), "stderr": threading.Event()}

    def terminate_once() -> None:
        with termination_lock:
            if terminated.is_set():
                return
            terminated.set()
        terminate_process_group(process, grace_seconds=0)

    def drain(stream: object, name: str, limit: int) -> None:
        if stream is None:  # pragma: no cover - guarded by the branch above
            return
        retained = captures[name]
        try:
            while True:
                chunk = stream.read(64 * 1024)  # type: ignore[union-attr]
                if not chunk:
                    return
                if len(retained) <= limit:
                    retained.extend(chunk[: limit + 1 - len(retained)])
                if len(retained) > limit:
                    output_exceeded.set()
                    terminate_once()
        except OSError, ValueError:
            capture_failed.set()
            terminate_once()
            return
        finally:
            reader_finished[name].set()

    (stdout_stream, _), (stderr_stream, _) = streams
    stdout_reader = threading.Thread(
        target=drain,
        args=(stdout_stream, "stdout", stdout_limit),
        daemon=True,
        name="reviewctl-kiro-stdout",
    )
    stderr_reader = threading.Thread(
        target=drain,
        args=(stderr_stream, "stderr", stderr_limit),
        daemon=True,
        name="reviewctl-kiro-stderr",
    )
    stdout_reader.start()
    stderr_reader.start()

    stdin_stream = getattr(process, "stdin", None)
    if input_bytes is not None and stdin_stream is not None:

        def write_input() -> None:
            try:
                stdin_stream.write(input_bytes)  # type: ignore[union-attr]
                stdin_stream.close()  # type: ignore[union-attr]
            except BrokenPipeError, OSError, ValueError:
                return

        threading.Thread(
            target=write_input,
            daemon=True,
            name="reviewctl-kiro-stdin",
        ).start()

    operation_deadline = time.monotonic() + timeout_seconds
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_once()
    # A descendant that inherits either pipe can keep it open after the Kiro
    # process exits. Treat that as a failed capture and terminate the whole
    # isolated process group instead of returning a silently incomplete review.
    # A normal child exit can race with the reader threads consuming bytes
    # already buffered in the pipes. Give that drain a short bounded window;
    # only a pipe that remains open after the window requires forced cleanup.
    normal_drain_deadline = time.monotonic() + (0 if timed_out else 1)
    for reader in (stdout_reader, stderr_reader):
        reader.join(timeout=max(0, normal_drain_deadline - time.monotonic()))
    if not all(event.is_set() for event in reader_finished.values()):
        capture_interrupted.set()
        terminate_once()
    # Allow a short, bounded post-termination drain so bytes already written
    # before a timeout are retained without permitting reader threads to extend
    # the operation indefinitely.
    cleanup_deadline = operation_deadline + 1
    join_deadline = min(cleanup_deadline, time.monotonic() + 5)
    for reader in (stdout_reader, stderr_reader):
        reader.join(timeout=max(0, join_deadline - time.monotonic()))
    capture_incomplete = (
        capture_failed.is_set()
        or capture_interrupted.is_set()
        or not all(event.is_set() for event in reader_finished.values())
    )
    return (
        bytes(captures["stdout"][:stdout_limit]),
        bytes(captures["stderr"][:stderr_limit]),
        timed_out,
        output_exceeded.is_set(),
        capture_incomplete,
    )


def packet_prompt(prompt: str, files: list[Path], response_contract: str = "verdict") -> str:
    """Add stable file names so structured findings are comparable across models."""
    supplied = ", ".join(file.name for file in files)
    if response_contract == "document":
        return (
            f"{prompt}\n\n"
            f"Supplied source files: {supplied}. "
            "Write the requested result as a coherent Markdown document. Use only the supplied "
            "files and prompt; do not invent facts, requirements, or citations."
        )
    if response_contract in {"product-review-json", "product-judge-json"}:
        briefing_scope = (
            f"Supplied synthetic briefing files: {supplied}. "
            if files
            else "No briefing files were supplied; use only the prompt. "
        )
        return (
            f"{prompt}\n\n"
            f"{briefing_scope}"
            "Use only the stated briefing. Do not invent product requirements, integrations, "
            "or business facts not present in it."
        )
    return (
        f"{prompt}\n\n"
        f"Supplied review files: {supplied}. "
        "For every finding, set path to the exact supplied basename, "
        "never an inferred path or symbol. "
        "Report only defects demonstrable from the supplied files; "
        "do not infer missing schema, fields, or surrounding behavior."
    )


def invoke_llm(
    *,
    llm_bin: str,
    prompt: str,
    model: str,
    database: Path,
    files: list[Path],
    max_output_tokens: int,
    response_contract: str,
    timeout_seconds: int,
    prepared_contract: PreparedContract | None = None,
) -> tuple[int, str]:
    if resource is None:
        return 126, "LLM bounded output capture unsupported on this platform"
    if prepared_contract is not None:
        prompt = f"{prompt}\n\n{prepared_contract.output_instructions}"
    command = [
        llm_bin,
        "prompt",
        prompt,
        "-m",
        model,
        "-d",
        str(database),
        "--log",
        "--no-stream",
        "--usage",
        "-o",
        "max_tokens",
        str(max_output_tokens),
    ]
    for file in files:
        command.extend(["-f", str(file)])
    if schema := response_schema(response_contract, prepared_contract=prepared_contract):
        command.extend(["--schema", json.dumps(schema, separators=(",", ":"))])

    capture_file_limit = max(MAX_LLM_DATABASE_BYTES, MAX_LLM_STDOUT_BYTES, MAX_LLM_STDERR_BYTES) + 1

    def limit_output_files() -> None:
        resource.setrlimit(  # pragma: no cover - runs only in the pre-exec child
            resource.RLIMIT_FSIZE,
            (capture_file_limit, capture_file_limit),
        )

    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            command,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
            preexec_fn=limit_output_files,
        )
        try:
            communicated_stdout, communicated_stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            terminate_process_group(process)
            return 124, "review attempt timed out"

        def bounded_output(value: object, stream, limit: int) -> tuple[bytes, bool]:
            if not isinstance(value, bytes):
                stream.seek(0)
                value = stream.read(limit + 1)
            return value[:limit], len(value) > limit

        _, stdout_truncated = bounded_output(communicated_stdout, stdout_file, MAX_LLM_STDOUT_BYTES)
        stderr, stderr_truncated = bounded_output(
            communicated_stderr, stderr_file, MAX_LLM_STDERR_BYTES
        )
        if stdout_truncated or stderr_truncated:
            return 502, "LLM transport output exceeded bounded capture"
        return process.returncode, stderr.decode(errors="replace")


def openrouter_packet(
    prompt: str,
    files: list[Path],
    response_contract: str = "findings-json",
    *,
    prepared_contract: PreparedContract | None = None,
) -> str:
    """Embed bounded frozen fragments in the direct OpenRouter request."""
    fragments = "\n\n".join(
        f"--- BEGIN {file.name} ---\n{file.read_text()}\n--- END {file.name} ---" for file in files
    )
    if prepared_contract is not None:
        contract = (
            f"{prepared_contract.output_instructions}\n"
            f"JSON Schema:\n{canonical_json(prepared_contract.schema).decode()}"
        )
    elif response_contract == "findings-json":
        contract = (
            get_contract(response_contract)
            .prepare(ContractContext(file_names=tuple(file.name for file in files)))
            .output_instructions
        )
    elif response_contract == "product-review-json":
        contract = (
            "Return only JSON matching the supplied product-design schema. Address every stated "
            "non-negotiable in `constraintChecks`; do not turn a simulation into execution."
        )
    elif response_contract == "product-judge-json":
        contract = (
            "Return only JSON matching the supplied council-judgment schema. Score the proposal, "
            "not its prose style, and list every violated non-negotiable by its exact identifier."
        )
    elif response_contract == "document":
        contract = "Return only the requested Markdown document, with no preamble or JSON wrapper."
    else:
        return f"{prompt}\n\n{fragments}"
    return f"{prompt}\n\n{contract}\n\n{fragments}"


def kiro_process_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Retain local Kiro login paths while excluding ambient provider credentials."""
    allowed = (
        "PATH",
        "SYSTEMROOT",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    )
    environment = {key: source[key] for key in allowed if key in source}
    environment["KIRO_LOG_NO_COLOR"] = "1"
    environment["NO_COLOR"] = "1"
    environment["CLICOLOR"] = "0"
    environment["TERM"] = "dumb"
    return environment


def normalize_kiro_output(stdout: bytes, response_contract: str) -> str:
    """Decode known Kiro UI framing for the JSON-only review contract."""
    if response_contract != "findings-json":
        raise ValueError("Kiro output normalization supports only findings-json")
    prefix = KIRO_RESPONSE_PREFIX.match(stdout)
    if prefix is None:
        return ""
    payload = stdout[prefix.end() :]
    footer = KIRO_RAW_CREDITS_FOOTER.search(payload)
    if footer is not None:
        payload = payload[: footer.start()]
    payload = KIRO_TRAILING_UI.sub(b"", payload)
    payload = KIRO_LEADING_UI.sub(b"", payload)
    if payload.startswith(b"json\n"):
        candidate = KIRO_LEADING_UI.sub(b"", payload[len(b"json\n") :])
        if candidate.lstrip().startswith((b"{", b"[")):
            payload = candidate
    if re.search(ANSI_ESCAPE_BYTES, payload):
        raise ValueError("Kiro returned a styled response payload")
    return payload.decode().replace("\r\n", "\n").strip("\r\n")


def kiro_model_inventory(payload: bytes) -> tuple[tuple[str, ...], str]:
    """Read only exact Kiro model identifiers observed from the installed CLI."""
    try:
        value = json.loads(payload, parse_constant=reject_nonstandard_json_constant)
    except (json.JSONDecodeError, UnicodeError, ValueError) as error:
        raise ValueError("Kiro returned malformed model inventory") from error
    if type(value) is not dict:
        raise ValueError("Kiro returned malformed model inventory")
    models = value.get("models")
    default_model = value.get("default_model")
    if type(models) is not list or type(default_model) is not str or not default_model.strip():
        raise ValueError("Kiro returned malformed model inventory")
    identifiers: list[str] = []
    for item in models:
        identifier = item.get("model_id") if type(item) is dict else None
        if (
            type(identifier) is not str
            or not identifier.strip()
            or identifier != identifier.strip()
        ):
            raise ValueError("Kiro returned malformed model inventory")
        identifiers.append(identifier)
    if not identifiers or len(identifiers) != len(set(identifiers)):
        raise ValueError("Kiro returned malformed model inventory")
    if default_model not in identifiers:
        raise ValueError("Kiro returned malformed model inventory")
    return tuple(identifiers), default_model


def kiro_session_id(payload: bytes, cwd: Path) -> str:
    """Require one reproducible UUID session for the isolated Kiro working directory."""
    try:
        value = json.loads(payload, parse_constant=reject_nonstandard_json_constant)
    except json.JSONDecodeError, UnicodeError, ValueError:
        return ""
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        return ""
    sessions = value[0].get("sessions")
    if (
        value[0].get("cwd") != str(cwd.resolve())
        or type(sessions) is not list
        or len(sessions) != 1
    ):
        return ""
    session = sessions[0]
    identifier = session.get("sessionId") if type(session) is dict else None
    if type(identifier) is not str or not KIRO_SESSION_ID.fullmatch(identifier):
        return ""
    return identifier


def invoke_kiro(
    *,
    kiro_bin: str,
    prompt: str,
    model: str,
    files: list[Path],
    max_output_tokens: int,
    response_contract: str,
    timeout_seconds: int,
    request_path: Path,
    models_path: Path,
    response_path: Path,
    session_path: Path,
    diagnostic_path: Path,
    evidence_parent_identity: tuple[int, int] | None = None,
    prepared_contract: PreparedContract | None = None,
) -> tuple[int, str, PersistedResponse]:
    """Run Kiro from an empty directory and retain its runtime-owned evidence."""
    blank = PersistedResponse("", None, None, None, model, None, None, "")
    if response_contract != "findings-json":
        return 502, "Kiro transport currently supports only findings-json", blank
    environment = kiro_process_environment(os.environ)
    stderr_chunks: list[bytes] = []
    started = time.monotonic()
    deadline = started + timeout_seconds

    def persist_stderr() -> str:
        stderr = b"".join(stderr_chunks).decode(errors="replace")
        write_private_exclusive(
            diagnostic_path,
            redact_diagnostic(stderr, limit=100_000).encode(),
            expected_parent_identity=evidence_parent_identity,
        )
        return stderr

    def run_process(
        command: list[str],
        cwd: Path,
        input_bytes: bytes | None = None,
    ) -> tuple[int, bytes, bytes, str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 124, b"", b"", "review attempt timed out"

        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE if input_bytes is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            (
                stdout,
                stderr,
                timed_out,
                output_exceeded,
                capture_incomplete,
            ) = _communicate_kiro_bounded(
                process,
                input_bytes=input_bytes,
                timeout_seconds=remaining,
                stdout_limit=MAX_KIRO_STDOUT_BYTES,
                stderr_limit=MAX_KIRO_STDERR_BYTES,
            )
            if output_exceeded:
                return 502, stdout, stderr, "Kiro transport output exceeded bounded capture"
            if timed_out:
                return 124, stdout, stderr, "review attempt timed out"
            if capture_incomplete:
                return 502, stdout, stderr, "Kiro transport capture did not reach EOF"
            return process.returncode, stdout, stderr, ""
        except FileNotFoundError:
            return 127, b"", b"", f"Kiro transport executable not found: {kiro_bin}"
        except subprocess.SubprocessError as error:
            return 126, b"", b"", f"Kiro bounded output capture failed: {error}"
        except OSError as error:
            return 127, b"", b"", f"Kiro transport could not execute: {error}"

    with tempfile.TemporaryDirectory(prefix="reviewctl-kiro-") as directory:
        cwd = Path(directory).resolve()
        agent_dir = cwd / ".kiro" / "agents"
        agent_dir.mkdir(parents=True, mode=0o700)
        agent_path = agent_dir / "reviewctl_readonly.json"
        agent_bytes = canonical_json(KIRO_REVIEW_AGENT) + b"\n"
        write_private_exclusive(agent_path, agent_bytes)
        inline_packet = openrouter_packet(
            prompt,
            files,
            response_contract,
            prepared_contract=prepared_contract,
        )
        command = [
            kiro_bin,
            "chat",
            "--no-interactive",
            "--agent",
            "reviewctl_readonly",
            "--model",
            model,
            "--wrap",
            "never",
        ]
        inventory_command = [kiro_bin, "chat", "--list-models", "--format", "json"]
        code, inventory_stdout, inventory_stderr, transport_error = run_process(
            inventory_command, cwd
        )
        write_private_exclusive(
            models_path,
            inventory_stdout,
            expected_parent_identity=evidence_parent_identity,
        )
        write_private_exclusive(
            request_path,
            canonical_json(
                {
                    "command": command,
                    "agentConfig": {
                        "sha256": sha256_bytes(agent_bytes),
                        "value": KIRO_REVIEW_AGENT,
                    },
                    "inventoryCommand": inventory_command,
                    "inventoryExitCode": code,
                    "model": model,
                    "models": {
                        "path": str(models_path),
                        "sha256": sha256_bytes(inventory_stdout),
                    },
                    "outputTokenLimitEnforced": False,
                    "prompt": inline_packet,
                    "requestedMaxOutputTokens": max_output_tokens,
                    "responseContract": response_contract,
                }
            )
            + b"\n",
            expected_parent_identity=evidence_parent_identity,
        )
        stderr_chunks.append(inventory_stderr)
        if code != 0:
            stderr = persist_stderr()
            return code, transport_error or stderr or "Kiro model inventory failed", blank
        try:
            models, _default_model = kiro_model_inventory(inventory_stdout)
        except ValueError as error:
            persist_stderr()
            return 502, str(error), blank
        if model == "auto":
            persist_stderr()
            return (
                502,
                "Kiro model auto is rejected because resolved identity is unobservable",
                blank,
            )
        if model not in models:
            persist_stderr()
            return 502, f"Kiro model is not listed by the installed CLI: {model}", blank

        code, stdout, invocation_stderr, transport_error = run_process(
            command, cwd, input_bytes=inline_packet.encode()
        )
        write_private_exclusive(
            response_path,
            stdout,
            expected_parent_identity=evidence_parent_identity,
        )
        stderr_chunks.append(invocation_stderr)
        if code != 0:
            stderr = persist_stderr()
            return code, transport_error or stderr or "Kiro review invocation failed", blank

        session_command = [kiro_bin, "chat", "--list-sessions", "--format", "json"]
        code, session_stdout, session_stderr, transport_error = run_process(session_command, cwd)
        write_private_exclusive(
            session_path,
            session_stdout,
            expected_parent_identity=evidence_parent_identity,
        )
        stderr_chunks.append(session_stderr)
        stderr = persist_stderr()
        if code != 0:
            return code, transport_error or stderr or "Kiro session inventory failed", blank
        session = kiro_session_id(session_stdout, cwd)
        if not session:
            if b"Monthly request limit reached" in invocation_stderr:
                return 429, "Kiro monthly request limit reached", blank
            return 502, "Kiro returned no coherent session for the review directory", blank
        try:
            normalized_response = normalize_kiro_output(stdout, response_contract)
        except UnicodeDecodeError:
            return 502, "Kiro returned non-UTF-8 terminal output", blank
        except ValueError as error:
            return 502, str(error), blank
        return (
            0,
            stderr,
            PersistedResponse(
                session,
                None,
                round((time.monotonic() - started) * 1000),
                None,
                model,
                None,
                None,
                normalized_response,
            ),
        )


def numeric_value(value: object) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except OverflowError:
        return None
    return numeric if math.isfinite(numeric) else None


def token_value(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def gemini_process_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Retain Gemini CLI login/configuration while excluding unrelated credentials."""
    allowed = (
        "PATH",
        "SYSTEMROOT",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "GEMINI_CLI_HOME",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_APPLICATION_CREDENTIALS",
    )
    environment = {key: source[key] for key in allowed if key in source}
    environment["NO_COLOR"] = "1"
    environment["CLICOLOR"] = "0"
    environment["TERM"] = "dumb"
    return environment


def gemini_usage(payload: Mapping[str, object]) -> tuple[int | None, int | None]:
    """Sum the CLI's per-model token counters without trusting absent values."""
    stats = payload.get("stats")
    models = stats.get("models") if isinstance(stats, dict) else None
    if not isinstance(models, dict):
        return None, None
    input_tokens = 0
    output_tokens = 0
    saw_input = False
    saw_output = False
    for details in models.values():
        tokens = details.get("tokens") if isinstance(details, dict) else None
        if not isinstance(tokens, dict):
            continue
        input_value = token_value(tokens.get("input", tokens.get("prompt")))
        output_value = token_value(tokens.get("candidates", tokens.get("output")))
        if input_value is not None:
            input_tokens += input_value
            saw_input = True
        if output_value is not None:
            output_tokens += output_value
            saw_output = True
    return (
        input_tokens if saw_input else None,
        output_tokens if saw_output else None,
    )


def invoke_gemini(
    *,
    gemini_bin: str,
    prompt: str,
    model: str,
    files: list[Path],
    max_output_tokens: int,
    response_contract: str,
    timeout_seconds: int,
    request_path: Path,
    response_path: Path,
    session_path: Path,
    diagnostic_path: Path,
    evidence_parent_identity: tuple[int, int] | None = None,
    prepared_contract: PreparedContract | None = None,
) -> tuple[int, str, PersistedResponse]:
    """Run Gemini CLI headlessly with a read-only plan and durable JSON evidence."""
    blank = PersistedResponse("", None, None, None, "", None, None, "")
    packet = openrouter_packet(
        prompt,
        files,
        response_contract,
        prepared_contract=prepared_contract,
    )
    command = [
        gemini_bin,
        "--model",
        model,
        "--prompt",
        "Read the complete review packet from standard input. Do not use tools or edit files.",
        "--output-format",
        "json",
        "--approval-mode",
        "plan",
        "--sandbox",
        "--skip-trust",
    ]
    write_private_exclusive(
        request_path,
        canonical_json(
            {
                "command": command,
                "model": model,
                "maxOutputTokens": max_output_tokens,
                "outputTokenLimitEnforced": False,
                "responseContract": response_contract,
                "files": [str(file) for file in files],
                "prompt": packet,
                "approvalMode": "plan",
                "sandbox": True,
                "session": str(session_path),
            }
        )
        + b"\n",
        label="Gemini request evidence",
        expected_parent_identity=evidence_parent_identity,
    )
    started = time.monotonic()
    environment = gemini_process_environment(os.environ)
    stdout = b""
    stderr = b""
    with tempfile.TemporaryDirectory(prefix="reviewctl-gemini-") as directory:
        try:
            process = subprocess.Popen(
                command,
                cwd=directory,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(input=packet.encode(), timeout=timeout_seconds)
            except subprocess.TimeoutExpired as error:
                terminate_process_group(process)
                stdout = error.output if isinstance(error.output, bytes) else b""
                stderr = error.stderr if isinstance(error.stderr, bytes) else b""
                stdout_after, stderr_after = process.communicate()
                stdout += stdout_after
                stderr += stderr_after
                if stdout:
                    write_private_exclusive(
                        response_path,
                        stdout,
                        expected_parent_identity=evidence_parent_identity,
                    )
                if stderr:
                    write_private_exclusive(
                        diagnostic_path,
                        redact_diagnostic(stderr.decode(errors="replace"), limit=100_000).encode(),
                        expected_parent_identity=evidence_parent_identity,
                    )
                return 124, "review attempt timed out", blank
        except FileNotFoundError:
            return 127, f"Gemini transport executable not found: {gemini_bin}", blank
        except OSError as error:
            return 127, f"Gemini transport could not execute: {error}", blank

    if stdout:
        write_private_exclusive(
            response_path,
            stdout,
            expected_parent_identity=evidence_parent_identity,
        )
    diagnostic = redact_diagnostic(stderr.decode(errors="replace"), limit=100_000)
    if diagnostic:
        write_private_exclusive(
            diagnostic_path,
            diagnostic.encode(),
            expected_parent_identity=evidence_parent_identity,
        )
    if process.returncode != 0:
        return process.returncode, diagnostic, blank
    try:
        payload = json.loads(
            stdout,
            object_pairs_hook=exact_json_object,
            parse_constant=reject_nonstandard_json_constant,
            parse_float=parse_finite_json_float,
        )
    except json.JSONDecodeError, UnicodeDecodeError, ValueError:
        return 502, "Gemini returned invalid JSON", blank
    if not isinstance(payload, dict):
        return 502, "Gemini returned a non-object response", blank
    conversation_id = payload.get("session_id")
    response_text = payload.get("response")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return 502, "Gemini returned no session identifier", blank
    if not isinstance(response_text, str):
        return 502, "Gemini returned no response", blank
    response_text = normalize_pi_response(response_text, response_contract)
    write_private_exclusive(
        session_path,
        canonical_json(
            {
                "session_id": conversation_id,
                "observedModels": payload.get("stats", {}).get("models", {})
                if isinstance(payload.get("stats"), dict)
                else {},
            }
        )
        + b"\n",
        label="Gemini session evidence",
        expected_parent_identity=evidence_parent_identity,
    )
    input_tokens, output_tokens = gemini_usage(payload)
    return (
        0,
        diagnostic,
        PersistedResponse(
            conversation_id=conversation_id,
            cost_usd=None,
            duration_ms=round((time.monotonic() - started) * 1000),
            input_tokens=input_tokens,
            model=model,
            output_tokens=output_tokens,
            provider="google-gemini-cli",
            response=response_text,
        ),
    )


def positive_timeout_seconds(value: str) -> int:
    """Parse a CLI timeout that preserves the same deadline across transports."""
    try:
        timeout_seconds = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout seconds must be an integer") from error
    if timeout_seconds <= 0:
        raise argparse.ArgumentTypeError("timeout seconds must be positive")
    return timeout_seconds


def invoke_openrouter(
    *,
    api_key: str | None,
    prompt: str,
    model: str,
    files: list[Path],
    max_output_tokens: int,
    reasoning_effort: str | None = None,
    provider_preferences: dict[str, object] | None = None,
    response_contract: str,
    timeout_seconds: int,
    request_path: Path,
    response_path: Path,
    evidence_parent_identity: tuple[int, int] | None = None,
    prepared_contract: PreparedContract | None = None,
) -> tuple[int, str, PersistedResponse]:
    """Call OpenRouter directly and persist source-safe request and raw response evidence."""
    blank = PersistedResponse("", None, None, None, "", None, None, "")
    if not api_key:
        return 127, "OPENROUTER_API_KEY is not configured", blank
    model_id = openrouter_model_id(model)
    payload: dict[str, object] = {
        "model": model_id,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": openrouter_packet(
                    prompt, files, response_contract, prepared_contract=prepared_contract
                ),
            }
        ],
    }
    payload["max_tokens"] = max_output_tokens
    if reasoning_effort is not None:
        payload["reasoning"] = {"effort": reasoning_effort}
    schema = prepared_contract.schema if prepared_contract else response_schema(response_contract)
    if schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": response_contract, "strict": True, "schema": schema},
        }
    if provider_preferences:
        payload["provider"] = provider_preferences
    write_private_exclusive(
        request_path,
        canonical_json(payload) + b"\n",
        label="OpenRouter request evidence",
        expected_parent_identity=evidence_parent_identity,
    )
    started = time.monotonic()
    curl_bin = os.environ.get("CURL_BIN", "curl")
    curl_config = (
        f'header = "Authorization: Bearer {api_key}"\nheader = "Content-Type: application/json"\n'
    ).encode()
    command = [
        curl_bin,
        "--config",
        "-",
        "--silent",
        "--show-error",
        "--max-time",
        str(timeout_seconds),
        "--connect-timeout",
        str(timeout_seconds),
        "--request",
        "POST",
        "--data-binary",
        f"@{request_path}",
    ]
    if resource is None:
        return 126, "OpenRouter bounded output capture unsupported on this platform", blank
    capture_file_limit = (
        max(
            MAX_OPENROUTER_RESPONSE_BYTES,
            MAX_OPENROUTER_STDERR_BYTES,
            MAX_OPENROUTER_STATUS_BYTES,
        )
        + 1
    )

    def limit_output_files() -> None:
        resource.setrlimit(  # pragma: no cover - runs only in the pre-exec child
            resource.RLIMIT_FSIZE,
            (capture_file_limit, capture_file_limit),
        )

    with tempfile.TemporaryDirectory(prefix="reviewctl-openrouter-") as scratch_directory:
        scratch_response = Path(scratch_directory).resolve() / "response.json"
        command.extend(
            [
                "--output",
                str(scratch_response),
                "--write-out",
                "%{http_code}",
                "https://openrouter.ai/api/v1/chat/completions",
            ]
        )
        try:
            with tempfile.TemporaryFile() as status_file, tempfile.TemporaryFile() as stderr_file:
                completed = subprocess.run(
                    command,
                    input=curl_config,
                    stdout=status_file,
                    stderr=stderr_file,
                    check=False,
                    timeout=timeout_seconds + 1,
                    preexec_fn=limit_output_files,
                )
                status_file.seek(0)
                http_status_bytes = status_file.read(MAX_OPENROUTER_STATUS_BYTES + 1)
                stderr_file.seek(0)
                stderr = stderr_file.read(MAX_OPENROUTER_STDERR_BYTES + 1)
        except FileNotFoundError:
            return 127, f"OpenRouter transport executable not found: {curl_bin}", blank
        except subprocess.TimeoutExpired:
            return 124, "review attempt timed out", blank
        except subprocess.SubprocessError as error:
            return 126, f"OpenRouter bounded output capture failed: {error}", blank
        except OSError as error:
            return 127, f"OpenRouter transport could not execute: {error}", blank
        if (
            len(http_status_bytes) > MAX_OPENROUTER_STATUS_BYTES
            or len(stderr) > MAX_OPENROUTER_STDERR_BYTES
        ):
            return 502, "OpenRouter transport output exceeded bounded capture", blank
        raw_response = b""
        if scratch_response.exists():
            try:
                with confined_regular_descriptor(scratch_response, os.O_RDONLY) as descriptor:
                    with os.fdopen(os.dup(descriptor), "rb") as stream:
                        raw_response = stream.read(MAX_OPENROUTER_RESPONSE_BYTES + 1)
            except OSError as error:
                return 502, f"OpenRouter response evidence was unsafe: {error}", blank
        if len(raw_response) > MAX_OPENROUTER_RESPONSE_BYTES:
            return 502, "OpenRouter transport output exceeded bounded capture", blank
    if raw_response:
        write_private_exclusive(
            response_path,
            raw_response,
            label="OpenRouter response evidence",
            expected_parent_identity=evidence_parent_identity,
        )
    if completed.returncode == 28:
        return 124, "review attempt timed out", blank
    http_status_text = http_status_bytes.decode(errors="replace").strip()
    http_status = int(http_status_text) if http_status_text.isdigit() else None
    if completed.returncode != 0:
        message = raw_response.decode(errors="replace") or stderr.decode(errors="replace")
        status = http_status if http_status is not None and http_status >= 400 else 502
        return status, message or "OpenRouter transport failed", blank
    if http_status is None:
        return 502, "OpenRouter transport did not report an HTTP status", blank
    if http_status >= 400:
        return http_status, raw_response.decode(errors="replace"), blank
    try:
        payload_response = json.loads(
            raw_response,
            object_pairs_hook=exact_json_object,
            parse_constant=reject_nonstandard_json_constant,
            parse_float=parse_finite_json_float,
        )
    except json.JSONDecodeError, UnicodeDecodeError, ValueError:
        return 502, "OpenRouter returned invalid JSON", blank
    if not isinstance(payload_response, dict):
        return 502, "OpenRouter returned a non-object response", blank
    provider_error = payload_response.get("error")
    if provider_error is not None:
        error_payload = provider_error if isinstance(provider_error, dict) else {}
        error_code = error_payload.get("code")
        error_message = error_payload.get("message")
        status = error_code if isinstance(error_code, int) and 400 <= error_code <= 599 else 502
        message = (
            error_message if isinstance(error_message, str) else "OpenRouter returned an error"
        )
        return status, message, blank
    choices = payload_response.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(choice, dict):
        return 502, "OpenRouter returned malformed choices", blank
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    usage = payload_response.get("usage")
    usage_values = usage if isinstance(usage, dict) else {}
    response_id = payload_response.get("id")
    response_model = payload_response.get("model")
    response_provider = payload_response.get("provider")
    return (
        0,
        "",
        PersistedResponse(
            conversation_id=response_id if isinstance(response_id, str) else "",
            cost_usd=numeric_value(usage_values.get("cost")),
            duration_ms=round((time.monotonic() - started) * 1000),
            input_tokens=token_value(usage_values.get("prompt_tokens")),
            model=response_model if isinstance(response_model, str) else "",
            output_tokens=token_value(usage_values.get("completion_tokens")),
            provider=response_provider if isinstance(response_provider, str) else None,
            response=content if isinstance(content, str) else "",
        ),
    )


def invoke_agy(
    *,
    agy_bin: str,
    prompt: str,
    model: str,
    files: list[Path],
    max_output_tokens: int,
    response_contract: str,
    timeout_seconds: int,
    request_path: Path,
    response_path: Path,
    evidence_parent_identity: tuple[int, int] | None = None,
    prepared_contract: PreparedContract | None = None,
) -> tuple[int, str, PersistedResponse]:
    """Run a native Antigravity model in an empty sandbox with durable JSON evidence."""
    blank = PersistedResponse("", None, None, None, "", None, None, "")
    if resource is None:
        return 126, "Antigravity bounded output capture unsupported on this platform", blank
    packet = openrouter_packet(
        prompt,
        files,
        response_contract,
        prepared_contract=prepared_contract,
    )
    request_payload: dict[str, object] = {
        "command": "agy",
        "maxOutputTokens": max_output_tokens,
        "model": model,
        "prompt": packet,
        "responseContract": response_contract,
        "sandbox": True,
    }
    write_private_exclusive(
        request_path,
        canonical_json(request_payload) + b"\n",
        label="AGY request evidence",
        expected_parent_identity=evidence_parent_identity,
    )
    command = [
        agy_bin,
        "--model",
        model,
        "--output-format",
        "json",
        "--print-timeout",
        f"{timeout_seconds}s",
        "--disable-slash-commands",
        "--sandbox",
    ]
    if schema := response_schema(response_contract, prepared_contract=prepared_contract):
        command.extend(["--json-schema", json.dumps(schema, separators=(",", ":"))])
    capture_file_limit = max(MAX_AGY_STDOUT_BYTES, MAX_AGY_STDERR_BYTES) + 1

    def limit_output_files() -> None:
        resource.setrlimit(  # pragma: no cover - runs only in the pre-exec child
            resource.RLIMIT_FSIZE,
            (capture_file_limit, capture_file_limit),
        )

    try:
        with tempfile.TemporaryDirectory(prefix="reviewctl-agy-") as sandbox:
            packet_path = Path(sandbox) / "review-packet.txt"
            packet_path.write_text(packet)
            command.extend(
                [
                    "--add-dir",
                    sandbox,
                    "--print",
                    "Read only the complete review packet from review-packet.txt in the current "
                    "directory. Follow its review instructions and return only the requested "
                    "response.",
                ]
            )
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                process = subprocess.Popen(
                    command,
                    cwd=sandbox,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                    preexec_fn=limit_output_files,
                )
                try:
                    communicated_stdout, communicated_stderr = process.communicate(
                        timeout=timeout_seconds
                    )
                except subprocess.TimeoutExpired:
                    terminate_process_group(process)
                    return 124, "review attempt timed out", blank

                def bounded_output(value: object, stream, limit: int) -> tuple[bytes, bool]:
                    if not isinstance(value, bytes):
                        stream.seek(0)
                        value = stream.read(limit + 1)
                    return value[:limit], len(value) > limit

                stdout, stdout_truncated = bounded_output(
                    communicated_stdout, stdout_file, MAX_AGY_STDOUT_BYTES
                )
                stderr, stderr_truncated = bounded_output(
                    communicated_stderr, stderr_file, MAX_AGY_STDERR_BYTES
                )
            if stdout_truncated or stderr_truncated:
                return 502, "Antigravity transport output exceeded bounded capture", blank
    except OSError as error:
        return 127, str(error), blank
    except subprocess.SubprocessError as error:
        return 126, f"Antigravity bounded output capture failed: {error}", blank
    write_private_exclusive(
        response_path,
        stdout,
        label="AGY response evidence",
        expected_parent_identity=evidence_parent_identity,
    )
    stderr_text = stderr.decode(errors="replace")
    if process.returncode != 0:
        return process.returncode, stderr_text, blank
    try:
        payload = json.loads(
            stdout,
            object_pairs_hook=exact_json_object,
            parse_constant=reject_nonstandard_json_constant,
            parse_float=parse_finite_json_float,
        )
    except json.JSONDecodeError, ValueError:
        return 502, "agy returned invalid JSON", blank
    if not isinstance(payload, dict):
        return 502, "agy returned a non-object response", blank
    status = payload.get("status")
    if status != "SUCCESS":
        return 502, f"agy returned status {status}", blank
    usage = payload.get("usage")
    usage_values = usage if isinstance(usage, dict) else {}
    duration_seconds = numeric_value(payload.get("duration_seconds"))
    conversation_id = payload.get("conversation_id")
    if "structured_output" not in payload:
        response = payload.get("response")
    else:
        structured_output = payload["structured_output"]
        if not isinstance(structured_output, dict):
            return 502, "agy returned invalid structured output", blank
        response = canonical_json(structured_output).decode()
    return (
        0,
        "",
        PersistedResponse(
            conversation_id=conversation_id if isinstance(conversation_id, str) else "",
            cost_usd=None,
            duration_ms=round(duration_seconds * 1000) if duration_seconds is not None else None,
            input_tokens=token_value(usage_values.get("input_tokens")),
            model=model,
            output_tokens=token_value(usage_values.get("output_tokens")),
            provider="google-antigravity",
            response=response if isinstance(response, str) else "",
        ),
    )


def pi_content_text(content: object) -> str:
    """Extract only text blocks from one Pi assistant message."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    )


def pi_usage(usage: object) -> tuple[float | None, int | None, int | None]:
    """Normalize Pi's nested usage object into the portable receipt fields."""
    if not isinstance(usage, dict):
        return None, None, None
    cost = usage.get("cost")
    cost_value = cost.get("total") if isinstance(cost, dict) else cost
    return (
        numeric_value(cost_value),
        token_value(usage.get("input")),
        token_value(usage.get("output")),
    )


def pi_resolved_model(requested: str, provider: str | None, resolved: str) -> str:
    """Rebuild Pi's provider/model identity from its split assistant metadata."""
    if not resolved:
        return ""
    if "/" not in requested:
        return resolved
    if not provider or "/" in provider:
        return ""
    return resolved if resolved.startswith(f"{provider}/") else f"{provider}/{resolved}"


def pi_persisted_response(
    stdout: bytes,
    requested_model: str,
    duration_ms: int,
    *,
    include_response: bool = True,
) -> PersistedResponse:
    """Recover the metadata Pi emitted, including from a partial event stream."""
    session_id = ""
    assistant_message: dict[str, object] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "session" and isinstance(event.get("id"), str):
            session_id = event["id"]
        if event.get("type") == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                assistant_message = message
        if event.get("type") == "agent_end":
            messages = event.get("messages")
            if isinstance(messages, list):
                for message in messages:
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        assistant_message = message
    if assistant_message is None:
        return PersistedResponse(session_id, None, duration_ms, None, "", None, None, "")
    cost_usd, input_tokens, output_tokens = pi_usage(assistant_message.get("usage"))
    provider = assistant_message.get("provider")
    resolved_model = assistant_message.get("model")
    response = pi_content_text(assistant_message.get("content")) if include_response else ""
    return PersistedResponse(
        conversation_id=session_id,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        input_tokens=input_tokens,
        model=(
            pi_resolved_model(requested_model, provider, resolved_model)
            if isinstance(resolved_model, str)
            else ""
        ),
        output_tokens=output_tokens,
        provider=provider if isinstance(provider, str) else None,
        response=response,
    )


def pi_timeout_diagnostic(stderr: bytes) -> str:
    details = stderr.decode(errors="replace").strip()
    return f"review attempt timed out: {details}" if details else "review attempt timed out"


def pi_system_prompt(
    response_contract: str,
    *,
    prepared_contract: PreparedContract | None = None,
) -> str:
    """Replace Pi's coding-agent prompt with the selected review contract."""
    schema = response_schema(response_contract, prepared_contract=prepared_contract)
    if schema is not None:
        instructions = f"{prepared_contract.output_instructions}\n" if prepared_contract else ""
        return (
            f"{instructions}You are a bounded review transport. Read only the supplied files. "
            "Return exactly one JSON object and no Markdown fences, commentary, or alternative "
            "review format. The object must satisfy this JSON Schema:\n"
            f"{canonical_json(schema).decode()}"
        )
    if response_contract == "document":
        return "You are a bounded document transport. Return only the requested Markdown document."
    return (
        "You are a bounded review transport. Return one complete verdict beginning with VERDICT:."
    )


def reject_nonstandard_json_constant(constant: str) -> None:
    """Reject NaN and infinity values accepted by Python but forbidden by JSON."""
    raise ValueError(f"non-standard JSON constant: {constant}")


def parse_finite_json_float(value: str) -> float:
    """Reject finite-parser overflow while decoding strict JSON evidence."""
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number: {value}")
    return parsed


def normalize_pi_response(response: str, response_contract: str) -> str:
    """Remove only one outer JSON fence that Pi may add despite the contract."""
    if response_contract not in {"findings-json", "product-review-json", "product-judge-json"}:
        return response
    match = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", response, flags=re.DOTALL)
    if match is None:
        return response
    candidate = match.group(1)
    try:
        value = json.loads(candidate, parse_constant=reject_nonstandard_json_constant)
    except json.JSONDecodeError, ValueError:
        return response
    return candidate if isinstance(value, dict) else response


def invoke_pi(
    *,
    pi_bin: str,
    prompt: str,
    model: str,
    files: list[Path],
    max_output_tokens: int,
    response_contract: str,
    timeout_seconds: int,
    request_path: Path,
    response_path: Path,
    session_path: Path,
    diagnostic_path: Path,
    evidence_parent_identity: tuple[int, int] | None = None,
    thinking: str = "minimal",
    prepared_contract: PreparedContract | None = None,
) -> tuple[int, str, PersistedResponse]:
    """Run Pi in JSON mode and retain its complete event stream and session."""
    blank = PersistedResponse("", None, None, None, "", None, None, "")
    if resource is None:
        return 126, "Pi bounded output capture unsupported on this platform", blank
    command = [
        pi_bin,
        "--mode",
        "json",
        "--print",
        "--no-tools",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-approve",
        "--thinking",
        thinking,
        "--system-prompt",
        pi_system_prompt(response_contract, prepared_contract=prepared_contract),
        "--model",
        model,
        "--session",
        str(session_path),
    ]
    command.extend(f"@{file}" for file in files)
    command.append(
        f"{prompt}\n\nReturn only the requested {response_contract} response. "
        "Do not edit files, run commands, or use information outside the supplied files."
    )
    write_private_exclusive(
        request_path,
        canonical_json(
            {
                "command": "pi",
                "mode": "json",
                "model": model,
                "requestedMaxOutputTokens": max_output_tokens,
                "thinking": thinking,
                "outputTokenLimitEnforced": False,
                "responseContract": response_contract,
                "files": [str(file) for file in files],
                "prompt": prompt,
                "session": str(session_path),
            }
        )
        + b"\n",
        label="Pi request evidence",
        expected_parent_identity=evidence_parent_identity,
    )
    started = time.monotonic()
    stdout = b""
    stderr = b""
    capture_file_limit = (
        max(
            MAX_PI_PROCESS_FILE_BYTES,
            MAX_PI_LEGACY_STDOUT_BYTES,
            MAX_PI_LEGACY_STDERR_BYTES,
        )
        + 1
    )

    def limit_output_files() -> None:
        resource.setrlimit(  # pragma: no cover - runs only in the pre-exec child
            resource.RLIMIT_FSIZE,
            (capture_file_limit, capture_file_limit),
        )

    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=files[0].parent if files else None,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
                preexec_fn=limit_output_files,
            )
            timed_out = False
            try:
                communicated_stdout, communicated_stderr = process.communicate(
                    timeout=timeout_seconds
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_process_group(process)
                communicated_stdout, communicated_stderr = process.communicate()

            def bounded_output(value: object, stream, limit: int) -> tuple[bytes, bool]:
                if isinstance(value, bytes):
                    return value[:limit], len(value) > limit
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                if size > limit:
                    stream.seek(size - limit)
                    return stream.read(limit), True
                stream.seek(0)
                return stream.read(limit), False

            stdout, stdout_truncated = bounded_output(
                communicated_stdout, stdout_file, MAX_PI_LEGACY_STDOUT_BYTES
            )
            stderr, stderr_truncated = bounded_output(
                communicated_stderr, stderr_file, MAX_PI_LEGACY_STDERR_BYTES
            )
        if stdout_truncated or stderr_truncated:
            return 502, "Pi transport output exceeded bounded capture", blank
        if timed_out:
            if stdout:
                write_private_exclusive(
                    response_path,
                    stdout,
                    label="Pi response evidence",
                    expected_parent_identity=evidence_parent_identity,
                )
            if stderr:
                write_private_exclusive(
                    diagnostic_path,
                    redact_diagnostic(stderr.decode(errors="replace"), limit=100_000).encode(),
                    label="Pi diagnostic evidence",
                    expected_parent_identity=evidence_parent_identity,
                )
            return (
                124,
                pi_timeout_diagnostic(stderr),
                pi_persisted_response(
                    stdout,
                    model,
                    round((time.monotonic() - started) * 1000),
                    include_response=False,
                ),
            )
    except FileNotFoundError:
        return 127, f"Pi transport executable not found: {pi_bin}", blank
    except subprocess.SubprocessError as error:
        return 126, f"Pi bounded output capture failed: {error}", blank
    except OSError as error:
        return 127, f"Pi transport could not execute: {error}", blank

    if stdout:
        write_private_exclusive(
            response_path,
            stdout,
            label="Pi response evidence",
            expected_parent_identity=evidence_parent_identity,
        )
    if stderr:
        write_private_exclusive(
            diagnostic_path,
            redact_diagnostic(stderr.decode(errors="replace"), limit=100_000).encode(),
            label="Pi diagnostic evidence",
            expected_parent_identity=evidence_parent_identity,
        )
    persisted = pi_persisted_response(stdout, model, round((time.monotonic() - started) * 1000))
    if not persisted.response:
        return (
            process.returncode,
            stderr.decode(errors="replace"),
            persisted,
        )
    return (
        process.returncode,
        stderr.decode(errors="replace"),
        PersistedResponse(
            conversation_id=persisted.conversation_id,
            cost_usd=persisted.cost_usd,
            duration_ms=persisted.duration_ms,
            input_tokens=persisted.input_tokens,
            model=persisted.model,
            output_tokens=persisted.output_tokens,
            provider=persisted.provider,
            response=normalize_pi_response(persisted.response, response_contract),
        ),
    )


DEFAULT_EXPLORATION_TOOLS = "read,grep,find,ls"


def exploration_path(root: str | Path, exploration_id: str) -> Path:
    """Resolve one named exploration below its user-owned root."""
    if not REVIEW_ID.fullmatch(exploration_id):
        raise ValueError("invalid exploration id")
    root_path = Path(root).expanduser().resolve()
    return root_path / exploration_id


def read_exploration_prompt(prompt: str | None, prompt_file: str | None) -> str:
    """Load one non-empty prompt for an exploratory turn."""
    if prompt and prompt_file:
        raise ValueError("use either --prompt or --prompt-file")
    if not prompt and not prompt_file:
        raise ValueError("one of --prompt or --prompt-file is required")
    value = Path(prompt_file).read_text() if prompt_file else prompt or ""
    if not value.strip():
        raise ValueError("exploration prompt must not be empty")
    return value


def invoke_pi_exploration(
    *,
    pi_bin: str,
    prompt: str,
    model: str,
    tools: str,
    cwd: Path,
    timeout_seconds: int,
    session_path: Path,
    events_path: Path,
) -> tuple[int, str, str, PersistedResponse]:
    """Run one full-tool Pi turn while preserving its resumable session."""
    blank = PersistedResponse("", None, None, None, "", None, None, "")
    command = [
        pi_bin,
        "--mode",
        "json",
        "--print",
        "--model",
        model,
        "--tools",
        tools,
        "--no-approve",
        "--session",
        str(session_path),
        prompt,
    ]
    started = time.monotonic()
    stdout = b""
    stderr = b""
    if resource is None:
        return 126, "Pi exploration bounded output capture unsupported", "", blank
    capture_file_limit = max(MAX_PI_EXPLORATION_STDOUT_BYTES, MAX_PI_EXPLORATION_STDERR_BYTES) + 1

    def limit_output_files() -> None:
        resource.setrlimit(  # pragma: no cover - runs only in the pre-exec child
            resource.RLIMIT_FSIZE,
            (capture_file_limit, capture_file_limit),
        )

    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
                preexec_fn=limit_output_files,
            )
            timed_out = False
            try:
                communicated_stdout, communicated_stderr = process.communicate(
                    timeout=timeout_seconds
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_process_group(process)
                communicated_stdout, communicated_stderr = process.communicate()

            def bounded_output(value: object, stream, limit: int) -> tuple[bytes, bool]:
                if not isinstance(value, bytes):
                    stream.seek(0)
                    value = stream.read(limit + 1)
                return value[:limit], len(value) > limit

            stdout, stdout_truncated = bounded_output(
                communicated_stdout, stdout_file, MAX_PI_EXPLORATION_STDOUT_BYTES
            )
            stderr, stderr_truncated = bounded_output(
                communicated_stderr, stderr_file, MAX_PI_EXPLORATION_STDERR_BYTES
            )
        if stdout:
            events_path.write_bytes(stdout)
        if stdout_truncated or stderr_truncated:
            return 502, "Pi exploration output exceeded bounded capture", "", blank
        if timed_out:
            return (
                124,
                pi_timeout_diagnostic(stderr).replace("review attempt", "exploration turn", 1),
                stderr.decode(errors="replace"),
                pi_persisted_response(
                    stdout,
                    model,
                    round((time.monotonic() - started) * 1000),
                    include_response=False,
                ),
            )
    except FileNotFoundError:
        return 127, f"Pi exploration executable not found: {pi_bin}", "", blank
    except subprocess.SubprocessError as error:
        return 126, f"Pi exploration bounded output capture failed: {error}", "", blank
    except OSError as error:
        return 127, f"Pi exploration could not execute: {error}", "", blank

    return (
        process.returncode,
        stderr.decode(errors="replace"),
        stderr.decode(errors="replace"),
        pi_persisted_response(stdout, model, round((time.monotonic() - started) * 1000)),
    )


def load_exploration(root: str | Path, exploration_id: str) -> tuple[Path, dict[str, object]]:
    """Load and validate the manifest for one named exploration."""
    path = exploration_path(root, exploration_id)
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"exploration does not exist: {exploration_id}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read exploration manifest: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("id") != exploration_id:
        raise ValueError(f"invalid exploration manifest: {manifest_path}")
    return path, manifest


def exploration_manifest_path(path: Path) -> Path:
    return path / "manifest.json"


def write_exploration_manifest(path: Path, manifest: dict[str, object]) -> None:
    exploration_manifest_path(path).write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    )


def run_exploration_turn(
    parser: argparse.ArgumentParser, args: argparse.Namespace, *, starting: bool
) -> int:
    """Start or resume one Pi exploration turn."""
    try:
        prompt = read_exploration_prompt(args.prompt, args.prompt_file)
        root = Path(args.exploration_root).expanduser().resolve()
        if starting:
            path = exploration_path(root, args.id)
            if path.exists():
                fail(parser, f"exploration already exists: {args.id}")
            model = args.model
            if not model:
                fail(parser, "--model is required for explore start")
            cwd = Path(args.cwd).expanduser().resolve()
            if not cwd.is_dir():
                fail(parser, f"exploration cwd does not exist: {cwd}")
            path.mkdir(parents=True)
            (path / "turns").mkdir()
            manifest: dict[str, object] = {
                "format": "reviewctl.exploration.v1",
                "id": args.id,
                "model": model,
                "tools": args.tools,
                "cwd": str(cwd),
                "session": None,
                "createdAt": utc_now(),
                "updatedAt": utc_now(),
                "turns": 0,
                "status": "created",
            }
        else:
            path, manifest = load_exploration(root, args.id)
            model = args.model or manifest.get("model")
            if not isinstance(model, str) or not model:
                fail(parser, "exploration manifest has no model; pass --model")
            cwd_value = args.cwd or manifest.get("cwd")
            cwd = (
                Path(cwd_value).expanduser().resolve() if isinstance(cwd_value, str) else Path.cwd()
            )
            if not cwd.is_dir():
                fail(parser, f"exploration cwd does not exist: {cwd}")
            if args.tools is None and isinstance(manifest.get("tools"), str):
                tools = manifest["tools"]
            else:
                tools = args.tools or DEFAULT_EXPLORATION_TOOLS
        tools = args.tools if starting else tools
        session_path = path / "session.jsonl"
        turn_number = int(manifest.get("turns", 0)) + 1
        turn_path = path / "turns" / f"{turn_number:03d}"
        turn_path.mkdir(parents=True, exist_ok=False)
        request_path = turn_path / "request.md"
        events_path = turn_path / "events.jsonl"
        response_path = turn_path / "response.md"
        stderr_path = turn_path / "stderr.log"
        request_path.write_text(prompt)
        exit_code, diagnostic, transport_stderr, persisted = invoke_pi_exploration(
            pi_bin=os.environ.get("PI_BIN", "pi"),
            prompt=prompt,
            model=model,
            tools=tools,
            cwd=cwd,
            timeout_seconds=args.timeout_seconds,
            session_path=session_path,
            events_path=events_path,
        )
        if persisted.response:
            response_path.write_text(persisted.response)
        if transport_stderr:
            stderr_path.write_text(redact_diagnostic(transport_stderr, limit=100_000))
        turn_manifest = {
            "turn": turn_number,
            "model": persisted.model or model,
            "provider": persisted.provider,
            "conversationId": persisted.conversation_id,
            "costUsd": persisted.cost_usd,
            "durationMs": persisted.duration_ms,
            "exitCode": exit_code,
            "diagnostic": redact_diagnostic(diagnostic, limit=100_000),
            "status": "completed" if exit_code == 0 and persisted.response else "unavailable",
        }
        (turn_path / "turn.json").write_text(
            json.dumps(turn_manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        )
        manifest.update(
            {
                "model": model,
                "tools": tools,
                "cwd": str(cwd),
                "session": (
                    str(session_path)
                    if session_path.is_file() and session_path.stat().st_size > 0
                    else None
                ),
                "updatedAt": utc_now(),
                "turns": turn_number,
                "status": turn_manifest["status"],
                "lastTurn": str(turn_path),
            }
        )
        write_exploration_manifest(path, manifest)
        print(turn_path)
        return 0 if turn_manifest["status"] == "completed" else 1
    except ValueError as error:
        fail(parser, str(error))
    return 1


def show_exploration(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    try:
        _, manifest = load_exploration(args.exploration_root, args.id)
    except ValueError as error:
        fail(parser, str(error))
    print(json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def promote_exploration(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    try:
        path, manifest = load_exploration(args.exploration_root, args.id)
    except ValueError as error:
        fail(parser, str(error))
    last_turn = manifest.get("lastTurn")
    if not isinstance(last_turn, str):
        fail(parser, "exploration has no completed turn to promote")
    response_path = Path(last_turn) / "response.md"
    if not response_path.is_file() or not response_path.read_text().strip():
        fail(parser, "exploration has no non-empty response to promote")
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        if not output.is_dir():
            fail(parser, f"promotion output is not a directory: {output}")
        if any(output.iterdir()):
            fail(parser, f"promotion output is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "exploration.md").write_text(response_path.read_text())
    prompt = (
        "Treat exploration.md as exploratory working material; it is not an approval or a source "
        "of truth. Independently verify its claims against the attached source files and tests. "
        "Report only findings supported by the frozen inputs.\n\n"
        f"Exploration id: {args.id}\n"
        "The formal review must not treat the exploratory response as a merge decision."
    )
    (output / "prompt.md").write_text(prompt + "\n")
    promoted_manifest = {
        "format": "reviewctl.exploration-promotion.v1",
        "exploration": str(path),
        "id": args.id,
        "sourceManifest": manifest,
        "explorationResponse": str(output / "exploration.md"),
        "formalPrompt": str(output / "prompt.md"),
        "status": "working-material",
    }
    (output / "manifest.json").write_text(
        json.dumps(promoted_manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    )
    print(output)
    return 0


def codex_prompt(
    prompt: str,
    response_contract: str,
    *,
    review_declaration_required: bool = True,
    prepared_contract: PreparedContract | None = None,
) -> str:
    """Add the output contract Codex must satisfy without expanding source scope."""
    if response_contract == "findings-json":
        prepared = prepared_contract or get_contract(response_contract).prepare(
            ContractContext(review_declaration_required=review_declaration_required)
        )
        contract = (
            "Read the frozen files in the current working directory before reviewing. "
            f"{prepared.output_instructions}"
        )
    elif response_contract == "product-review-json":
        contract = (
            "Read the frozen briefing files in the current working directory before designing. "
            "Return only JSON matching the supplied product-design schema. List every frozen "
            "snapshot you actually reviewed in reviewedFiles; do not return a design if you cannot "
            "read a file. The runner records the authoritative source hashes."
        )
    elif response_contract == "product-judge-json":
        contract = (
            "Read the frozen briefing and anonymous candidate response in the current working "
            "directory. Return only JSON matching the supplied council-judgment schema. List every "
            "frozen snapshot you actually reviewed in reviewedFiles. The runner records the "
            "authoritative source hashes."
        )
    elif response_contract == "document":
        contract = (
            "Read the frozen files in the current working directory. Return only the requested "
            "Markdown document, with no JSON wrapper or preamble."
        )
    else:
        contract = "Return a complete verdict beginning with VERDICT: and ending with punctuation."
    return f"{prompt}\n\n{contract} Read only the supplied review files."


def invoke_codex(
    *,
    codex_bin: str,
    prompt: str,
    model: str,
    response_contract: str,
    source_roots: list[Path] | None,
    timeout_seconds: int,
    workspace: Path,
    prepared_contract: PreparedContract | None = None,
    evidence_dir: Path | None = None,
    evidence_parent_identity: tuple[int, int] | None = None,
) -> tuple[int, str, PersistedResponse | None]:
    """Run Codex against the isolated snapshots and recover its final response."""
    isolation: CodexIsolation | None = None
    try:
        if source_roots:
            isolation_context = codex_isolation(source_roots)
            isolation = isolation_context.__enter__()
        else:
            isolation_context = None
    except (RuntimeError, OSError) as error:
        if evidence_dir is not None:
            write_private_exclusive(
                evidence_dir / "request.json",
                canonical_json(
                    {"command": None, "model": model, "responseContract": response_contract}
                ),
                label="Codex request evidence",
                expected_parent_identity=evidence_parent_identity,
            )
            write_private_exclusive(
                evidence_dir / "stdout.log",
                b"",
                label="Codex stdout evidence",
                expected_parent_identity=evidence_parent_identity,
            )
            write_private_exclusive(
                evidence_dir / "stderr.log",
                str(error).encode(),
                label="Codex stderr evidence",
                expected_parent_identity=evidence_parent_identity,
            )
            write_private_exclusive(
                evidence_dir / "output-state.json",
                canonical_json({"exists": False}),
                label="Codex output state",
                expected_parent_identity=evidence_parent_identity,
            )
        return (
            127,
            str(error),
            None,
        )

    temporary_root = isolation.home if isolation else workspace
    output_path = temporary_root.resolve() / "codex-response.md"
    schema_path: Path | None = None
    sandbox_arguments = (
        ["--dangerously-bypass-approvals-and-sandbox"] if isolation else ["--sandbox", "read-only"]
    )
    command = [
        codex_bin,
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        *sandbox_arguments,
        "--model",
        model,
        "-C",
        str(workspace),
        "--output-last-message",
        str(output_path),
    ]
    try:
        if schema := response_schema(
            response_contract,
            codex=source_roots is not None,
            prepared_contract=prepared_contract,
        ):
            if prepared_contract is not None:
                # Codex Structured Outputs requires every declared property. Project
                # only that constraint; keep the portable contract and schema intact.
                schema = {**schema, "required": list(schema["properties"])}
            schema_path = output_path.with_name("codex-response.schema.json")
            schema_path.write_bytes(canonical_json(schema))
            command.extend(["--output-schema", str(schema_path)])
        command.append(
            codex_prompt(
                prompt,
                response_contract,
                review_declaration_required=source_roots is not None,
                prepared_contract=prepared_contract,
            )
        )
        if isolation:
            # Codex's own seatbelt cannot be nested inside macOS sandbox-exec.
            # The outer profile already denies the original proprietary checkout;
            # use Codex's documented external-sandbox mode for the inner process.
            command = ["sandbox-exec", "-f", str(isolation.profile), *command]

        if evidence_dir is not None:
            write_private_exclusive(
                evidence_dir / "request.json",
                canonical_json(
                    {
                        "command": [*command[:-1], "<inline-prompt>"],
                        "model": model,
                        "responseContract": response_contract,
                        "promptSha256": sha256_bytes(command[-1].encode()),
                    }
                ),
                label="Codex request evidence",
                expected_parent_identity=evidence_parent_identity,
            )

        started = time.monotonic()
        timed_out = False
        process_environment = (
            isolation.environment
            if isolation
            else codex_process_environment(
                os.environ,
                {"HOME": os.environ.get("HOME") or str(account_home())},
            )
        )

        try:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=process_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            diagnostic = f"Codex launch failed: {error}"
            if evidence_dir is not None:
                for name, contents in (("stdout.log", b""), ("stderr.log", diagnostic.encode())):
                    write_private_exclusive(
                        evidence_dir / name,
                        contents,
                        label=f"Codex {name} evidence",
                        expected_parent_identity=evidence_parent_identity,
                    )
                write_private_exclusive(
                    evidence_dir / "output-state.json",
                    canonical_json({"exists": False}),
                    label="Codex output state",
                    expected_parent_identity=evidence_parent_identity,
                )
            return (
                127,
                diagnostic,
                None,
            )
        response_oversized = threading.Event()
        response_monitor_stop = threading.Event()

        def monitor_response_file() -> None:
            """Stop a Codex process before its final-response file fills the disk."""
            while not response_monitor_stop.wait(0.05):
                try:
                    oversized = output_path.stat().st_size > MAX_CODEX_RESPONSE_BYTES
                except FileNotFoundError:
                    continue
                if not oversized:
                    continue
                response_oversized.set()
                process_pid = getattr(process, "pid", None)
                if isinstance(process_pid, int):
                    terminate_process_group(process)
                return

        response_monitor = threading.Thread(
            target=monitor_response_file,
            daemon=True,
            name="reviewctl-codex-response",
        )
        response_monitor.start()

        def bounded_pipe_capture(stream: object, limit: int, captured: dict[str, object]) -> None:
            """Drain a child pipe while retaining bounded head and tail context."""
            head_limit = min(64 * 1024, limit // 2)
            tail_limit = limit - head_limit
            head = bytearray()
            tail = bytearray()
            total = 0
            if stream is None:  # pragma: no cover - pipe mode starts only with both streams
                captured.update(value=b"", truncated=False)
                return
            while True:
                chunk = stream.read(64 * 1024)  # type: ignore[union-attr]
                if not chunk:
                    break
                total += len(chunk)
                if len(head) < head_limit:
                    take = min(head_limit - len(head), len(chunk))
                    head.extend(chunk[:take])
                    chunk = chunk[take:]
                if chunk:
                    tail.extend(chunk)
                    if len(tail) > tail_limit:
                        del tail[: len(tail) - tail_limit]
            captured.update(
                value=bytes(head + tail),
                truncated=total > limit,
            )

        stdout_stream = getattr(process, "stdout", None)
        stderr_stream = getattr(process, "stderr", None)
        pipe_mode = stdout_stream is not None and stderr_stream is not None
        stdout_capture: dict[str, object] = {}
        stderr_capture: dict[str, object] = {}
        if pipe_mode:
            stdout_reader = threading.Thread(
                target=bounded_pipe_capture,
                args=(stdout_stream, MAX_CODEX_STDOUT_BYTES, stdout_capture),
                daemon=True,
                name="reviewctl-codex-stdout",
            )
            stderr_reader = threading.Thread(
                target=bounded_pipe_capture,
                args=(stderr_stream, MAX_CODEX_STDERR_BYTES, stderr_capture),
                daemon=True,
                name="reviewctl-codex-stderr",
            )
            stdout_reader.start()
            stderr_reader.start()
        if not pipe_mode:
            try:
                communicated_stdout, communicated_stderr = process.communicate(
                    timeout=timeout_seconds
                )
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                communicated_stdout = b""
                communicated_stderr = b""
                exit_code = 124
                timed_out = True
        else:
            try:
                process.wait(timeout=timeout_seconds)
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                exit_code = 124
                timed_out = True
        if pipe_mode:
            stdout_reader.join(timeout=5)
            stderr_reader.join(timeout=5)
            communicated_stdout = cast(bytes, stdout_capture.get("value", b""))
            stdout_capture_truncated = bool(stdout_capture.get("truncated"))
            communicated_stderr = cast(bytes, stderr_capture.get("value", b""))
            stderr_capture_truncated = bool(stderr_capture.get("truncated"))
        else:
            stdout_capture_truncated = False
            stderr_capture_truncated = False
        response_monitor_stop.set()
        response_monitor.join(timeout=5)

        def bounded_output(
            value: object, limit: int, truncated: bool = False
        ) -> tuple[bytes, bool]:
            if isinstance(value, bytes):
                return value[:limit], truncated or len(value) > limit
            return b"", truncated

        stdout, stdout_truncated = bounded_output(
            communicated_stdout, MAX_CODEX_STDOUT_BYTES, stdout_capture_truncated
        )
        stderr, stderr_truncated = bounded_output(
            communicated_stderr, MAX_CODEX_STDERR_BYTES, stderr_capture_truncated
        )
        if evidence_dir is not None:
            for name, contents in (("stdout.log", stdout), ("stderr.log", stderr)):
                write_private_exclusive(
                    evidence_dir / name,
                    contents,
                    label=f"Codex {name} evidence",
                    expected_parent_identity=evidence_parent_identity,
                )
            write_private_exclusive(
                evidence_dir / "output-state.json",
                canonical_json({"exists": output_path.is_file()}),
                label="Codex output state",
                expected_parent_identity=evidence_parent_identity,
            )
        stderr_text = "review attempt timed out" if timed_out else stderr.decode(errors="replace")
        truncated_streams = [
            name
            for name, truncated in (("stdout", stdout_truncated), ("stderr", stderr_truncated))
            if truncated
        ]
        if truncated_streams:
            truncation_note = "Codex transport output truncated: " + ", ".join(truncated_streams)
            stderr_text = f"{stderr_text}\n{truncation_note}".strip()
        transport_output = f"{stdout.decode(errors='replace')}\n{stderr_text}"
        session = re.search(r"session id:\s*([^\s]+)", transport_output)
        resolved_model = re.search(r"^model:\s*([^\s]+)", transport_output, flags=re.MULTILINE)
        if output_path.is_file() and not timed_out:
            with confined_regular_descriptor(output_path, os.O_RDONLY) as descriptor:
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    raw_response = stream.read(MAX_CODEX_RESPONSE_BYTES + 1)
            if response_oversized.is_set() or len(raw_response) > MAX_CODEX_RESPONSE_BYTES:
                return (
                    502,
                    "Codex final response exceeded bounded capture",
                    PersistedResponse("", None, None, None, "", None, "openai-codex", ""),
                )
            try:
                response_text = raw_response.decode("utf-8")
            except UnicodeDecodeError:
                return (
                    502,
                    "Codex final response is not valid UTF-8",
                    PersistedResponse("", None, None, None, "", None, "openai-codex", ""),
                )
        else:
            return exit_code, stderr_text, None
        return (
            exit_code,
            stderr_text,
            PersistedResponse(
                conversation_id=session.group(1) if session else "",
                cost_usd=None,
                duration_ms=round((time.monotonic() - started) * 1000),
                input_tokens=None,
                model=resolved_model.group(1) if resolved_model else "",
                output_tokens=None,
                provider=None,
                response=response_text,
            ),
        )
    finally:
        try:
            output_path.unlink(missing_ok=True)
            if schema_path:
                schema_path.unlink(missing_ok=True)
        finally:
            if isolation:
                assert isolation_context is not None
                isolation_context.__exit__(None, None, None)


def load_response(database: Path | bytes) -> PersistedResponse | None:
    if isinstance(database, Path):
        try:
            database_bytes = read_confined_bytes(database)
        except OSError:
            return None
    else:
        database_bytes = database
    if not database_bytes:
        return None
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.deserialize(database_bytes)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(responses)")}
            required = {"response", "conversation_id", "model"}
            if not required <= columns:
                return None
            input_tokens = "input_tokens" if "input_tokens" in columns else "NULL"
            output_tokens = "output_tokens" if "output_tokens" in columns else "NULL"
            duration_ms = "duration_ms" if "duration_ms" in columns else "NULL"
            response_json = "response_json" if "response_json" in columns else "NULL"
            row = connection.execute(
                "SELECT response, conversation_id, model, "
                f"{input_tokens}, {output_tokens}, {duration_ms}, {response_json} "
                "FROM responses ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        transport = json.loads(row[6]) if row[6] else {}
    except json.JSONDecodeError:
        transport = {}
    usage = transport.get("usage", {}) if isinstance(transport, dict) else {}
    cost = usage.get("cost") if isinstance(usage, dict) else None
    provider = transport.get("provider") if isinstance(transport, dict) else None
    return PersistedResponse(
        response=row[0] or "",
        conversation_id=row[1] or "",
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        duration_ms=row[5] if isinstance(row[5], int) else None,
        model=row[2] or "",
        input_tokens=row[3],
        output_tokens=row[4],
        provider=provider if isinstance(provider, str) else None,
    )


def execute_llm_backend(request: BackendRequest) -> BackendExecution:
    database = request.attempt_dir / "transport.sqlite3"
    with tempfile.TemporaryDirectory(prefix="reviewctl-llm-") as scratch_directory:
        scratch_database = Path(scratch_directory).resolve() / "transport.sqlite3"
        exit_code, diagnostic = invoke_llm(
            llm_bin=os.environ.get("LLM_BIN", "llm"),
            prompt=request.prompt,
            model=request.model,
            database=scratch_database,
            files=list(request.files),
            max_output_tokens=request.max_output_tokens,
            response_contract=request.response_contract,
            timeout_seconds=request.timeout_seconds,
            prepared_contract=request.prepared_contract,
        )
        response = None
        if os.path.lexists(scratch_database):
            try:
                scratch_bytes = read_confined_bytes(scratch_database)
                response = load_response(scratch_bytes)
                write_private_exclusive(
                    database,
                    scratch_bytes,
                    label="LLM database evidence",
                    expected_parent_identity=request.evidence_parent_identity,
                )
            except OSError as error:
                return BackendExecution(
                    502,
                    f"LLM database evidence was unsafe: {error}",
                    None,
                    BackendEvidence(database=database),
                )
    return BackendExecution(
        exit_code,
        diagnostic,
        response,
        BackendEvidence(database=database),
    )


def execute_codex_backend(request: BackendRequest) -> BackendExecution:
    response_path = request.attempt_dir / "response.md"
    request_path = request.attempt_dir / "request.json"
    stderr_path = request.attempt_dir / "stderr.log"
    with ExitStack() as workspace_context:
        workspace = (
            request.files[0].parent
            if request.files
            else Path(
                workspace_context.enter_context(
                    tempfile.TemporaryDirectory(prefix="reviewctl-codex-prompt-")
                )
            )
        )
        exit_code, diagnostic, response = invoke_codex(
            codex_bin=os.environ.get("CODEX_BIN", "codex"),
            prompt=request.prompt,
            model=request.model,
            response_contract=request.response_contract,
            source_roots=list(request.source_roots) or None,
            timeout_seconds=request.timeout_seconds,
            workspace=workspace,
            prepared_contract=request.prepared_contract,
            evidence_dir=request.attempt_dir,
            evidence_parent_identity=request.evidence_parent_identity,
        )
    if response is not None:
        write_private_exclusive(
            response_path,
            response.response.encode(),
            label="Codex response evidence",
            expected_parent_identity=request.evidence_parent_identity,
        )
    return BackendExecution(
        exit_code,
        diagnostic,
        response,
        BackendEvidence(
            request=request_path if request_path.is_file() else None,
            response=response_path if response is not None else None,
            stderr=stderr_path if stderr_path.is_file() else None,
        ),
    )


def execute_kiro_backend(request: BackendRequest) -> BackendExecution:
    request_path = request.attempt_dir / "request.json"
    models_path = request.attempt_dir / "models.json"
    response_path = request.attempt_dir / "response.log"
    session_path = request.attempt_dir / "session.json"
    final_response_path = request.attempt_dir / "response.md"
    diagnostic_path = request.attempt_dir / "stderr.log"
    exit_code, diagnostic, response = invoke_kiro(
        kiro_bin=os.environ.get("KIRO_BIN", "kiro-cli"),
        prompt=request.prompt,
        model=request.model,
        files=list(request.files),
        max_output_tokens=request.max_output_tokens,
        response_contract=request.response_contract,
        timeout_seconds=request.timeout_seconds,
        request_path=request_path,
        models_path=models_path,
        response_path=response_path,
        session_path=session_path,
        diagnostic_path=diagnostic_path,
        evidence_parent_identity=request.evidence_parent_identity,
        prepared_contract=request.prepared_contract,
    )
    final_evidence = None
    if response.response:
        write_private_exclusive(
            final_response_path,
            response.response.encode(),
            expected_parent_identity=request.evidence_parent_identity,
        )
        final_evidence = final_response_path
    return BackendExecution(
        exit_code,
        diagnostic,
        response,
        BackendEvidence(
            request=request_path,
            response=response_path,
            session=session_path,
            final_response=final_evidence,
            stderr=diagnostic_path,
        ),
    )


def execute_openrouter_backend(request: BackendRequest) -> BackendExecution:
    request_path = request.attempt_dir / "request.json"
    response_path = request.attempt_dir / "response.json"
    exit_code, diagnostic, response = invoke_openrouter(
        api_key=os.environ.get("OPENROUTER_API_KEY"),
        prompt=request.prompt,
        model=request.model,
        files=list(request.files),
        max_output_tokens=request.max_output_tokens,
        reasoning_effort=request.reasoning_effort,
        provider_preferences=request.provider_preferences,
        response_contract=request.response_contract,
        timeout_seconds=request.timeout_seconds,
        request_path=request_path,
        response_path=response_path,
        evidence_parent_identity=request.evidence_parent_identity,
        prepared_contract=request.prepared_contract,
    )
    return BackendExecution(
        exit_code,
        diagnostic,
        response,
        BackendEvidence(request=request_path, response=response_path),
    )


def execute_agy_backend(request: BackendRequest) -> BackendExecution:
    request_path = request.attempt_dir / "request.json"
    response_path = request.attempt_dir / "response.json"
    exit_code, diagnostic, response = invoke_agy(
        agy_bin=os.environ.get("AGY_BIN", "agy"),
        prompt=request.prompt,
        model=request.model,
        files=list(request.files),
        max_output_tokens=request.max_output_tokens,
        response_contract=request.response_contract,
        timeout_seconds=request.timeout_seconds,
        request_path=request_path,
        response_path=response_path,
        evidence_parent_identity=request.evidence_parent_identity,
        prepared_contract=request.prepared_contract,
    )
    return BackendExecution(
        exit_code,
        diagnostic,
        response,
        BackendEvidence(request=request_path, response=response_path),
    )


def execute_gemini_backend(request: BackendRequest) -> BackendExecution:
    request_path = request.attempt_dir / "request.json"
    response_path = request.attempt_dir / "response.json"
    session_path = request.attempt_dir / "session.json"
    final_response_path = request.attempt_dir / "response.md"
    diagnostic_path = request.attempt_dir / "stderr.log"
    exit_code, diagnostic, response = invoke_gemini(
        gemini_bin=os.environ.get("GEMINI_BIN", "gemini"),
        prompt=request.prompt,
        model=request.model,
        files=list(request.files),
        max_output_tokens=request.max_output_tokens,
        response_contract=request.response_contract,
        timeout_seconds=request.timeout_seconds,
        request_path=request_path,
        response_path=response_path,
        session_path=session_path,
        diagnostic_path=diagnostic_path,
        evidence_parent_identity=request.evidence_parent_identity,
        prepared_contract=request.prepared_contract,
    )
    if response.response:
        write_private_exclusive(
            final_response_path,
            response.response.encode(),
            expected_parent_identity=request.evidence_parent_identity,
        )
    return BackendExecution(
        exit_code,
        diagnostic,
        response,
        BackendEvidence(
            request=request_path,
            response=response_path,
            session=session_path,
            final_response=final_response_path if response.response else None,
            stderr=diagnostic_path,
        ),
    )


def execute_pi_backend(request: BackendRequest) -> BackendExecution:
    request_path = request.attempt_dir / "request.json"
    events_path = request.attempt_dir / "events.jsonl"
    session_path = request.attempt_dir / "session.jsonl"
    final_response_path = request.attempt_dir / "response.md"
    stderr_path = request.attempt_dir / "stderr.log"
    with tempfile.TemporaryDirectory(prefix="reviewctl-pi-") as scratch_directory:
        scratch_session = Path(scratch_directory).resolve() / "session.jsonl"
        exit_code, diagnostic, response = invoke_pi(
            pi_bin=os.environ.get("PI_BIN", "pi"),
            prompt=request.prompt,
            model=request.model,
            files=list(request.files),
            max_output_tokens=request.max_output_tokens,
            response_contract=request.response_contract,
            timeout_seconds=request.timeout_seconds,
            request_path=request_path,
            response_path=events_path,
            session_path=scratch_session,
            diagnostic_path=stderr_path,
            evidence_parent_identity=request.evidence_parent_identity,
            thinking=request.thinking,
            prepared_contract=request.prepared_contract,
        )
        if os.path.lexists(scratch_session):
            try:
                scratch_bytes = read_confined_bytes(scratch_session)
                write_private_exclusive(
                    session_path,
                    scratch_bytes,
                    label="Pi session evidence",
                    expected_parent_identity=request.evidence_parent_identity,
                )
            except OSError as error:
                return BackendExecution(
                    502,
                    f"Pi session evidence was unsafe: {error}",
                    response,
                    BackendEvidence(
                        request=request_path,
                        response=events_path,
                        session=session_path,
                        stderr=stderr_path,
                    ),
                )
    if response.response:
        write_private_exclusive(
            final_response_path,
            response.response.encode(),
            label="Pi final response evidence",
            expected_parent_identity=request.evidence_parent_identity,
        )
    return BackendExecution(
        exit_code,
        diagnostic,
        response,
        BackendEvidence(
            request=request_path,
            response=events_path,
            session=session_path,
            final_response=final_response_path,
            stderr=stderr_path,
        ),
    )


def build_backend_registry() -> BackendRegistry:
    registry = BackendRegistry()
    registry.register(
        BackendDescriptor(
            "agy",
            BackendFamily.AGENT_CLI,
            DiscoveryKind.EXECUTABLE,
            "AGY_BIN",
            "agy",
            BackendCapabilities(
                ReadOnlyCapability.ADVISORY,
                False,
                True,
                True,
                False,
                True,
                False,
                True,
                False,
                SourceIsolation.UNAVAILABLE,
            ),
            "unqualified",
        ),
        execute_agy_backend,
    )
    registry.register(
        BackendDescriptor(
            "codex",
            BackendFamily.AGENT_CLI,
            DiscoveryKind.EXECUTABLE,
            "CODEX_BIN",
            "codex",
            BackendCapabilities(
                ReadOnlyCapability.SANDBOXED,
                False,
                True,
                True,
                False,
                True,
                False,
                True,
                True,
                SourceIsolation.EXTERNAL_SANDBOX,
            ),
            "unqualified",
        ),
        execute_codex_backend,
    )
    registry.register(
        BackendDescriptor(
            "gemini",
            BackendFamily.AGENT_CLI,
            DiscoveryKind.EXECUTABLE,
            "GEMINI_BIN",
            "gemini",
            BackendCapabilities(
                ReadOnlyCapability.ADVISORY,
                False,
                True,
                False,
                True,
                True,
                True,
                True,
                False,
                SourceIsolation.UNAVAILABLE,
            ),
            "unqualified",
        ),
        execute_gemini_backend,
    )
    registry.register(
        BackendDescriptor(
            "kiro",
            BackendFamily.AGENT_CLI,
            DiscoveryKind.EXECUTABLE,
            "KIRO_BIN",
            "kiro-cli",
            BackendCapabilities(
                ReadOnlyCapability.ADVISORY,
                False,
                False,
                False,
                False,
                True,
                False,
                True,
                True,
                SourceIsolation.UNAVAILABLE,
            ),
            "unqualified",
        ),
        execute_kiro_backend,
    )
    registry.register(
        BackendDescriptor(
            "llm",
            BackendFamily.GENERIC_MODEL_CLI,
            DiscoveryKind.EXECUTABLE,
            "LLM_BIN",
            "llm",
            BackendCapabilities(
                ReadOnlyCapability.ADVISORY,
                False,
                True,
                True,
                True,
                True,
                True,
                True,
                False,
                SourceIsolation.UNAVAILABLE,
            ),
            "unqualified",
        ),
        execute_llm_backend,
    )
    registry.register(
        BackendDescriptor(
            "openrouter",
            BackendFamily.PROVIDER_GATEWAY,
            DiscoveryKind.REMOTE_API,
            "",
            "",
            BackendCapabilities(
                ReadOnlyCapability.UNSUPPORTED,
                False,
                True,
                True,
                True,
                True,
                True,
                True,
                False,
                SourceIsolation.UNAVAILABLE,
            ),
            "unqualified",
        ),
        execute_openrouter_backend,
    )
    registry.register(
        BackendDescriptor(
            "pi",
            BackendFamily.AGENT_CLI,
            DiscoveryKind.EXECUTABLE,
            "PI_BIN",
            "pi",
            BackendCapabilities(
                ReadOnlyCapability.ADVISORY,
                False,
                True,
                True,
                True,
                True,
                True,
                True,
                False,
                SourceIsolation.UNAVAILABLE,
            ),
            "unqualified",
        ),
        execute_pi_backend,
    )
    return registry


def response_is_complete(response: str) -> bool:
    stripped = response.strip()
    if re.fullmatch(r"VERDICT:\s*(?:approved|changes-requested)[.!?]?", stripped, re.IGNORECASE):
        return True
    return len(stripped) >= 20 and "VERDICT" in stripped.upper() and stripped[-1] in ".]}"


def non_empty_strings(value: object, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, str) and item.strip() for item in value)
    )


def exact_object(value: object, fields: set[str]) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == fields
        and all(isinstance(value[field], str) and value[field].strip() for field in fields)
    )


def exact_object_list(value: object, fields: set[str]) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(exact_object(item, fields) for item in value)
    )


def valid_architecture(value: object) -> bool:
    fields = {"boundary", "owns", "commands", "events", "readModels"}
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, dict)
            and set(item) == fields
            and isinstance(item["boundary"], str)
            and bool(item["boundary"].strip())
            and isinstance(item["owns"], str)
            and bool(item["owns"].strip())
            and non_empty_strings(item["commands"], allow_empty=True)
            and non_empty_strings(item["events"], allow_empty=True)
            and non_empty_strings(item["readModels"], allow_empty=True)
            for item in value
        )
    )


def valid_constraint_checks(value: object) -> bool:
    fields = {"constraintId", "disposition", "rationale"}
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, dict)
            and set(item) == fields
            and isinstance(item["constraintId"], str)
            and bool(item["constraintId"].strip())
            and item["disposition"] in PRODUCT_CONSTRAINT_DISPOSITIONS
            and isinstance(item["rationale"], str)
            and bool(item["rationale"].strip())
            for item in value
        )
    )


def validate_product_review(value: dict[str, Any]) -> dict[str, Any] | None:
    fields = set(PRODUCT_REVIEW_SCHEMA["required"])
    if (
        set(value) != fields
        or not isinstance(value["summary"], str)
        or not value["summary"].strip()
    ):
        return None
    required_lists = ("userJobs", "mvp", "nonGoals", "risks", "acceptanceTests")
    if not all(non_empty_strings(value[field]) for field in required_lists):
        return None
    if not non_empty_strings(value["openQuestions"], allow_empty=True):
        return None
    if not exact_object_list(value["interactionFlow"], {"actor", "action", "outcome"}):
        return None
    if not exact_object_list(value["domainEntities"], {"name", "purpose"}):
        return None
    if not exact_object_list(value["stateTransitions"], {"from", "to", "guard"}):
        return None
    if not valid_architecture(value["architecture"]):
        return None
    if not exact_object_list(value["operationalControls"], {"control", "approach"}):
        return None
    return value if valid_constraint_checks(value["constraintChecks"]) else None


def validate_product_judge(value: dict[str, Any]) -> dict[str, Any] | None:
    if set(value) != {"scores", "hardConstraintViolations", "rationale"}:
        return None
    scores = value["scores"]
    if (
        not isinstance(scores, dict)
        or set(scores) != PRODUCT_SCORE_FIELDS
        or not all(
            isinstance(score, int) and not isinstance(score, bool) and 0 <= score <= 4
            for score in scores.values()
        )
    ):
        return None
    if not non_empty_strings(value["hardConstraintViolations"], allow_empty=True):
        return None
    return value if isinstance(value["rationale"], str) and value["rationale"].strip() else None


def validate_read_proof(value: dict[str, Any], expected_file_hashes: dict[str, str]) -> bool:
    reviewed_files = value.get("reviewedFiles")
    if not isinstance(reviewed_files, list):
        return False
    reviewed_paths: set[str] = set()
    for reviewed in reviewed_files:
        if not isinstance(reviewed, str) or not reviewed:
            return False
        # Codex receives a private frozen snapshot and may report its absolute
        # sandbox path. Frozen inputs have unique basenames, so normalize that
        # path form before comparing the declared files to receipt provenance.
        normalized = reviewed.strip()
        if not normalized:
            return False
        if normalized in expected_file_hashes:
            proof_path = normalized
        else:
            snapshot_path = Path(normalized)
            if (
                not snapshot_path.is_absolute()
                or not snapshot_path.parent.name.startswith("reviewctl-input-")
                or snapshot_path.name not in expected_file_hashes
            ):
                return False
            proof_path = snapshot_path.name
        if proof_path in reviewed_paths:
            return False
        # The model declares only the frozen snapshots it reviewed. The runner
        # preserves the immutable SHA-256 values in the receipt provenance.
        reviewed_paths.add(proof_path)
    return reviewed_paths == set(expected_file_hashes)


def validate_review_response(
    response: str,
    contract: str,
    *,
    expected_file_hashes: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    if contract == "verdict":
        return (
            {"verdict": "unstructured", "findings": []} if response_is_complete(response) else None
        )
    if contract == "document":
        stripped = response.strip()
        return {"document": stripped} if len(stripped) >= 20 else None
    if contract == "findings-json":
        context = ContractContext(
            file_names=tuple(expected_file_hashes or ()),
            review_declaration_required=expected_file_hashes is not None,
        )
        prepared = get_contract(contract).prepare(context)
        return get_contract(contract).evaluate(response, prepared, context).value
    try:
        value = json.loads(response)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    if contract == "product-review-json":
        if expected_file_hashes is not None:
            expected_fields = {*PRODUCT_REVIEW_SCHEMA["required"], "reviewedFiles"}
            if set(value) != expected_fields or not validate_read_proof(
                value, expected_file_hashes
            ):
                return None
            value = {key: item for key, item in value.items() if key != "reviewedFiles"}
        return validate_product_review(value)
    if contract == "product-judge-json":
        if expected_file_hashes is not None:
            expected_fields = {*PRODUCT_JUDGE_SCHEMA["required"], "reviewedFiles"}
            if set(value) != expected_fields or not validate_read_proof(
                value, expected_file_hashes
            ):
                return None
            value = {key: item for key, item in value.items() if key != "reviewedFiles"}
        return validate_product_judge(value)
    return None


def review_validation_error(
    response: str,
    contract: str,
    *,
    expected_file_hashes: dict[str, str] | None = None,
) -> str | None:
    """Explain a rejected structured response without changing the acceptance contract."""
    if contract == "findings-json":
        context = ContractContext(
            file_names=tuple(expected_file_hashes or ()),
            review_declaration_required=expected_file_hashes is not None,
        )
        prepared = get_contract(contract).prepare(context)
        evaluation = get_contract(contract).evaluate(response, prepared, context)
        return findings_validation_error(response, evaluation)
    if (
        validate_review_response(response, contract, expected_file_hashes=expected_file_hashes)
        is not None
    ):
        return None
    if contract == "document":
        return "document: response is empty or shorter than 20 characters"
    if contract == "verdict":
        return "verdict: response is incomplete"
    try:
        value = json.loads(response)
    except json.JSONDecodeError:
        return f"{contract}: invalid JSON"
    if not isinstance(value, dict):
        return f"{contract}: top-level response must be an object"

    if expected_file_hashes is not None:
        if contract == "product-review-json":
            expected_fields = {*PRODUCT_REVIEW_SCHEMA["required"], "reviewedFiles"}
        elif contract == "product-judge-json":
            expected_fields = {*PRODUCT_JUDGE_SCHEMA["required"], "reviewedFiles"}
        else:
            expected_fields = {"verdict", "findings", "reviewedFiles"}
        if set(value) != expected_fields:
            return f"{contract}: response fields do not match the required schema"
        if not validate_read_proof(value, expected_file_hashes):
            return f"{contract}: reviewedFiles proof does not match frozen inputs"

    return f"{contract}: response does not satisfy the required schema"


def first_duplicate_json_key(response: str) -> str | None:
    """Return the first duplicate object key without weakening strict decoding."""
    duplicate: str | None = None

    def collect(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal duplicate
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen and duplicate is None:
                duplicate = key
            seen.add(key)
        return dict(pairs)

    try:
        json.loads(
            response,
            object_pairs_hook=collect,
            parse_constant=reject_nonstandard_json_constant,
        )
    except ValueError, RecursionError:
        return None
    return duplicate


def findings_validation_error(response: str, evaluation: ContractEvaluation) -> str | None:
    """Render one native findings evaluation using the stable CLI diagnostics."""
    if not evaluation.violations:
        return None
    violation = evaluation.violations[0]
    if violation == "invalid-json":
        duplicate = first_duplicate_json_key(response)
        if duplicate is not None:
            return f"findings-json: duplicate JSON key {duplicate!r}"
        return "findings-json: invalid JSON"
    if violation == "top-level-not-object":
        return "findings-json: top-level response must be an object"
    if violation == "response-fields":
        return "findings-json: response fields do not match the required schema"
    if violation == "review-declaration":
        return "findings-json: reviewedFiles proof does not match frozen inputs"
    if violation == "verdict":
        value = json.loads(response)
        verdict = value.get("verdict")
        if not isinstance(verdict, str):
            return "findings-json: verdict must be a string"
        return f"findings-json: invalid verdict {verdict!r}; expected approved or changes-requested"
    return "findings-json: findings do not satisfy the required schema or verdict invariant"


def persisted_receipt_valid(
    path: Path,
    *,
    expected_sha256: str | None = None,
    expected_parent_identity: tuple[int, int] | None = None,
) -> bool:
    """Verify the digest of the exact receipt bytes persisted by a review run."""

    def reject_nonstandard_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    try:
        receipt = json.loads(
            read_confined_text(path, expected_parent_identity=expected_parent_identity),
            object_pairs_hook=exact_json_object,
            parse_constant=reject_nonstandard_constant,
        )
        if not isinstance(receipt, dict):
            return False
        recorded = receipt.pop("sha256", None)
        return (
            isinstance(recorded, str)
            and (expected_sha256 is None or recorded == expected_sha256)
            and recorded == sha256_bytes(contract_canonical_json(receipt))
        )
    except OSError, UnicodeError, TypeError, ValueError, OverflowError:
        return False


def seal(
    path: Path,
    contents: bytes,
    recipient: str,
    *,
    expected_parent_identity: tuple[int, int] | None = None,
) -> str:
    target = path.with_suffix(path.suffix + ".age")
    result = subprocess.run(
        ["age", "-r", recipient],
        input=contents,
        text=False,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "age failed")
    write_private_exclusive(
        target,
        result.stdout,
        label="sealed review evidence",
        expected_parent_identity=expected_parent_identity,
    )
    return target.name


def run_review(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    prompt, files = validate_request(parser, args)
    range_context: dict[str, Any] | None = None
    range_context_path = getattr(args, "range_context_file", None)
    if range_context_path:
        try:
            _, loaded_context = load_range_json(Path(range_context_path).expanduser().resolve())
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
            parser.error(f"could not read range receipt context: {error}")
        if type(loaded_context) is not dict or set(loaded_context) != {
            "rangeReviewSchemaVersion",
            "manifestSha256",
            "range",
            "chunkIndex",
            "chunkCount",
            "chunkId",
        }:
            parser.error("range receipt context has an invalid shape")
        range_context = loaded_context
    routes, route_profile = review_routes(parser, args)
    if any(route.transport == "pi" and "/" not in route.model for route in routes):
        parser.error("pi review models must use provider/model identity")
    if any(route.transport == "kiro" and route.model == "auto" for route in routes):
        parser.error("Kiro review model auto is rejected because resolved identity is unobservable")
    if (
        any(route.transport == "kiro" for route in routes)
        and args.response_contract != "findings-json"
    ):
        parser.error(
            "Kiro transport currently supports only --response-contract findings-json; "
            "terminal-rendered document and verdict output cannot be verified without rewriting"
        )
    route_transports = {route.transport for route in routes}
    transport_default_key = next(iter(route_transports)) if len(route_transports) == 1 else ""
    if route_profile:
        raw_defaults = route_profile.get("defaultSettings")
        transport_defaults = raw_defaults if isinstance(raw_defaults, dict) else {}
        execution_config = {
            "path": str(route_profile["path"]),
            "sha256": str(route_profile["sha256"]),
        }
    else:
        transport_defaults, execution_config = load_transport_defaults(
            parser, getattr(args, "config", None), transport_default_key
        )
    review_prompt = packet_prompt(prompt, files, args.response_contract)
    packet_digest = sha256_bytes(review_prompt.encode())
    try:
        provider_preferences = provider_preferences_from_args(args)
    except ValueError as error:
        parser.error(str(error))
    if provider_preferences and not any(route.transport == "openrouter" for route in routes):
        if getattr(args, "routes", []):
            parser.error("provider preferences require at least one openrouter route")
        parser.error("provider preferences require --transport openrouter")
    policy_digest: str | None = None
    loaded_policy: dict[str, Any] | None = None
    if args.policy:
        try:
            loaded_policy, policy_bytes = load_policy_evidence(args.policy)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            parser.error(f"could not read policy {args.policy}: {error}")
        policy_digest = sha256_bytes(policy_bytes)
    local_policy_routes = [route for route in routes if route.transport in LOCAL_POLICY_TRANSPORTS]
    kiro_routes = [route for route in local_policy_routes if route.transport == "kiro"]
    if args.source_class == "proprietary":
        scoped_routes = local_policy_routes if loaded_policy is not None else []
        if kiro_routes and loaded_policy is None:
            parser.error("proprietary Kiro reviews require --policy")
        if (
            any(
                route.transport in REQUIRED_LOCAL_POLICY_TRANSPORTS for route in local_policy_routes
            )
            and loaded_policy is None
        ):
            parser.error("proprietary local transport reviews require --policy")
        for route in scoped_routes:
            if loaded_policy is None or not source_allowed(
                loaded_policy, route.model, transport=route.transport
            ):
                parser.error(f"policy does not allow {route.transport} model {route.model}")
            if route.transport == "kiro" and not unresolved_identity_waived(
                loaded_policy, route.model, transport=route.transport
            ):
                parser.error(
                    f"policy must explicitly allow unresolved Kiro model identity for {route.model}"
                )
    kiro_identity_waiver = args.source_class == "proprietary" and bool(kiro_routes)
    artifact_root = Path(args.artifact_root)
    log_path = (
        Path(args.log_file).expanduser()
        if getattr(args, "log_file", None)
        else artifact_root / "reviewctl.log"
    )
    logger = configure_runtime_logger(log_path)
    turn_dir, turn_identity = review_root(artifact_root, args.review_id)
    attempts_dir = turn_dir / "attempts"
    with confined_directory_descriptor(
        turn_dir, expected_identity=turn_identity
    ) as turn_descriptor:
        with confined_relative_directory_descriptor(
            turn_descriptor, ("attempts",), create=True
        ) as attempts_descriptor:
            metadata = os.fstat(attempts_descriptor)
            attempts_identity = (metadata.st_dev, metadata.st_ino)
    attempt_identities: dict[Path, tuple[int, int]] = {}
    codex_source_roots = (
        review_source_roots(files)
        if any(route.transport == "codex" for route in routes)
        and args.source_class == "proprietary"
        else None
    )
    snapshots_context = frozen_review_files(files)
    source_files, snapshots = snapshots_context.__enter__()
    if not source_files:
        prompt_source: dict[str, str] = {
            "name": Path(args.prompt_file).name if args.prompt_file else "prompt.txt",
            "sha256": sha256_bytes(prompt.encode()),
        }
        if args.prompt_file:
            prompt_source["path"] = str(Path(args.prompt_file))
        else:
            prompt_path = turn_dir / "prompt.txt"
            write_private_exclusive(
                prompt_path,
                prompt.encode(),
                label="literal prompt source",
                expected_parent_identity=turn_identity,
            )
            prompt_source["path"] = str(prompt_path)
        source_files.append(prompt_source)
    snapshot_hashes = {file.name: sha256_bytes(file.read_bytes()) for file in snapshots}
    native_contract = (
        get_contract(args.response_contract) if args.response_contract == "findings-json" else None
    )
    source = {
        "files": source_files,
        "git": source_git_metadata(files),
    }
    attempts: list[dict[str, Any]] = []
    accepted: PersistedResponse | None = None
    accepted_capabilities: BackendCapabilities | None = None
    accepted_review: dict[str, Any] | None = None
    accepted_attempt: int | None = None
    promoted_fragments: tuple[PromotedFragment, ...] = ()
    fallback_relationships: list[FallbackRelationship] = []
    completion_request = None
    previous_attempt: int | None = None
    previous_route_index: int | None = None
    previous_gate_result: str | None = None
    consolidation_context: ContractContext | None = None

    profile_settings = route_profile.get("settings", {}) if route_profile else {}
    configured_timeout = (
        profile_settings.get("timeout_seconds")
        if isinstance(profile_settings, dict)
        else transport_defaults.get("timeout_seconds")
    )
    if configured_timeout is None:
        configured_timeout = transport_defaults.get("timeout_seconds")
    configured_attempts = (
        profile_settings.get("max_attempts")
        if isinstance(profile_settings, dict)
        else transport_defaults.get("max_attempts")
    )
    if configured_attempts is None:
        configured_attempts = transport_defaults.get("max_attempts")
    timeout_seconds = (
        args.timeout_seconds
        if args.timeout_seconds is not None
        else configured_timeout or DEFAULT_REVIEW_TIMEOUT_SECONDS
    )
    max_attempts = (
        args.max_attempts
        if args.max_attempts is not None
        else configured_attempts or DEFAULT_REVIEW_MAX_ATTEMPTS
    )
    configured_thinking = (
        profile_settings.get("thinking") if isinstance(profile_settings, dict) else None
    )
    thinking = configured_thinking if isinstance(configured_thinking, str) else "minimal"
    configured_reasoning_effort = (
        profile_settings.get("reasoning_effort") if isinstance(profile_settings, dict) else None
    )
    explicit_reasoning_effort = getattr(args, "reasoning_effort", None)
    reasoning_effort = (
        explicit_reasoning_effort
        if isinstance(explicit_reasoning_effort, str)
        else configured_reasoning_effort
        if isinstance(configured_reasoning_effort, str)
        else None
    )
    configured_reviewed_files = (
        profile_settings.get("require_reviewed_files")
        if isinstance(profile_settings, dict)
        else None
    )
    explicit_reviewed_files = getattr(args, "require_reviewed_files", None)
    if explicit_reviewed_files is not None and type(explicit_reviewed_files) is not bool:
        parser.error("require reviewed files must be boolean")
    if configured_reviewed_files is not None and type(configured_reviewed_files) is not bool:
        parser.error("profile require_reviewed_files must be boolean")
    require_reviewed_files = (
        explicit_reviewed_files
        if explicit_reviewed_files is not None
        else bool(configured_reviewed_files)
    )
    if require_reviewed_files and args.response_contract not in REVIEW_DECLARATION_CONTRACTS:
        parser.error("--require-reviewed-files is supported only for findings-json")
    # Strict structured-output schemas require every declared property. Keep
    # reviewedFiles mandatory for Codex and OpenRouter across a fallback chain.
    effective_reviewed_files = require_reviewed_files or (
        args.response_contract == "findings-json"
        and any(route.transport in {"codex", "openrouter"} for route in routes)
    )
    if not snapshots and (effective_reviewed_files or codex_source_roots is not None):
        parser.error("reviewed-files declarations require at least one actual --file")
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 3:
        parser.error("max attempts must be an integer from 1 to 3")
    requested_models = [route.model for route in routes]
    log_event(
        logger,
        "review_started",
        review_id=args.review_id,
        routes=[{"model": route.model, "transport": route.transport} for route in routes],
        source_class=args.source_class,
    )
    backend_registry = build_backend_registry()
    number = 0
    for route_index, route in enumerate(routes):
        for _ in range(max_attempts):
            number += 1
            transport = route.transport
            model = route.model
            transport_model = (
                model.removeprefix("openrouter/") if transport == "openrouter" else model
            )
            contract_context = (
                ContractContext(
                    file_names=tuple(item["name"] for item in source_files),
                    review_declaration_required=effective_reviewed_files,
                )
                if native_contract
                else None
            )
            prepared_contract = (
                native_contract.prepare(contract_context)
                if native_contract and contract_context
                else None
            )
            if contract_context is not None:
                consolidation_context = contract_context
            attempt_prompt = review_prompt
            completion_fragments: tuple[PromotedFragment, ...] = ()
            if prepared_contract is not None and contract_context is not None:
                completion_fragments = tuple(
                    fragment
                    for fragment in promoted_fragments
                    if fragment.contract_context == contract_context
                    and fragment.prepared_digest == prepared_contract.digest
                )
            if (
                completion_fragments
                and completion_request is not None
                and prepared_contract is not None
                and contract_context is not None
            ):
                target_missing_fields = ("verdict", "findings")
                if contract_context.review_declaration_required:
                    target_missing_fields += ("reviewedFiles",)
                target_completion_request = ContractCompletionRequest(
                    prepared_digest=prepared_contract.digest,
                    packet_digest=packet_digest,
                    missing_fields=target_missing_fields,
                    invalid_fragment_indexes=completion_request.invalid_fragment_indexes,
                    violations=completion_request.violations,
                )
                completion_context = build_completion_context(
                    target_completion_request,
                    completion_fragments,
                    allowed_file_names=contract_context.file_names,
                    review_declaration_required=(contract_context.review_declaration_required),
                )
                attempt_prompt = render_completion_prompt(review_prompt, completion_context)
            if (
                previous_attempt is not None
                and previous_route_index is not None
                and previous_gate_result is not None
            ):
                relationship_kind = (
                    "retry" if previous_route_index == route_index else "route-fallback"
                )
                relationship = FallbackRelationship(
                    from_attempt=previous_attempt,
                    to_attempt=number,
                    kind=relationship_kind,
                    reason=previous_gate_result,
                    promoted_fragment_ids=tuple(
                        fragment.fragment_id
                        for fragment in promoted_fragments
                        if fragment in completion_fragments
                    ),
                )
                fallback_relationships.append(relationship)
                log_event(
                    logger,
                    "attempt_retry" if relationship_kind == "retry" else "route_fallback",
                    from_attempt=previous_attempt,
                    to_attempt=number,
                    from_model=routes[previous_route_index].model,
                    from_transport=routes[previous_route_index].transport,
                    to_model=model,
                    to_transport=transport,
                    reason=previous_gate_result,
                    review_id=args.review_id,
                )
            attempt_dir = attempts_dir / f"{number:02d}"
            with confined_directory_descriptor(
                attempts_dir, expected_identity=attempts_identity
            ) as attempts_descriptor:
                os.mkdir(attempt_dir.name, mode=0o700, dir_fd=attempts_descriptor)
                with confined_relative_directory_descriptor(
                    attempts_descriptor, (attempt_dir.name,)
                ) as attempt_descriptor:
                    metadata = os.fstat(attempt_descriptor)
                    attempt_identity = (metadata.st_dev, metadata.st_ino)
            attempt_identities[attempt_dir] = attempt_identity
            database: Path | None = None
            request_path: Path | None = None
            response_path: Path | None = None
            session_path: Path | None = None
            final_response_path: Path | None = None
            diagnostic_path: Path | None = None
            log_event(
                logger,
                "attempt_started",
                attempt=number,
                model=model,
                review_id=args.review_id,
                route_index=route_index,
                transport=transport,
            )
            backend = backend_registry.require(transport)
            capabilities = backend.descriptor.capabilities
            execution = backend.execute(
                BackendRequest(
                    prompt=attempt_prompt,
                    model=transport_model,
                    response_contract=args.response_contract,
                    files=tuple(snapshots),
                    attempt_dir=attempt_dir,
                    timeout_seconds=timeout_seconds,
                    max_output_tokens=args.max_output_tokens,
                    source_class=args.source_class,
                    source_roots=tuple(codex_source_roots or ()),
                    provider_preferences=provider_preferences,
                    evidence_parent_identity=attempt_identity,
                    thinking=thinking,
                    reasoning_effort=reasoning_effort if transport == "openrouter" else None,
                    prepared_contract=prepared_contract,
                )
            )
            exit_code = execution.exit_code
            stderr = execution.diagnostic
            persisted = execution.response
            database = execution.evidence.database
            request_path = execution.evidence.request
            response_path = execution.evidence.response
            session_path = execution.evidence.session
            final_response_path = execution.evidence.final_response
            diagnostic_path = execution.evidence.stderr
            raw_response: dict[str, object] | None = None
            if persisted is not None:
                raw_response_path = attempt_dir / "raw-response.txt"
                raw_response_bytes = persisted.response.encode(errors="surrogatepass")
                write_private_exclusive(
                    raw_response_path,
                    raw_response_bytes,
                    expected_parent_identity=attempt_identity,
                )
                raw_response = {
                    "path": str(raw_response_path),
                    "sha256": sha256_bytes(raw_response_bytes),
                    "characters": len(persisted.response),
                }
            contract_evaluation = None
            evaluation_error: dict[str, str] | None = None
            review = None
            validation_error = None
            if exit_code == 124:
                gate_result = "timeout"
            elif exit_code != 0:
                gate_result = "transport-failed"
            elif persisted is None:
                gate_result = "missing-response"
            elif capabilities.resolved_model_identity and persisted.model != transport_model:
                gate_result = "model-mismatch"
            elif capabilities.resolved_provider_identity and not resolved_provider_matches(
                provider_preferences, persisted.provider
            ):
                gate_result = "provider-mismatch"
            elif not persisted.response.strip():
                gate_result = "empty"
            elif not persisted.conversation_id:
                gate_result = "missing-conversation"
            else:
                if native_contract and prepared_contract and contract_context:
                    try:
                        contract_evaluation = native_contract.evaluate(
                            persisted.response,
                            prepared_contract,
                            contract_context,
                            evidence=EvaluationContext(packet_digest=packet_digest),
                        )
                    except (ValueError, UnicodeError, OverflowError) as error:
                        exception_name = type(error).__name__
                        evaluation_error = {
                            "type": exception_name,
                            "message": "response data could not be evaluated safely",
                        }
                        validation_error = (
                            "findings-json: response data could not be evaluated safely "
                            f"({exception_name})"
                        )
                        gate_result = "contract-invalid"
                    else:
                        review = contract_evaluation.value
                        validation_error = (
                            findings_validation_error(persisted.response, contract_evaluation)
                            if review is None
                            else None
                        )
                        if contract_evaluation.status is EvaluationStatus.COMPLETE:
                            gate_result = "accepted"
                        elif contract_evaluation.status is EvaluationStatus.INCOMPLETE:
                            gate_result = "contract-incomplete"
                        else:
                            gate_result = "contract-invalid"
                else:
                    expected_file_hashes = (
                        snapshot_hashes
                        if transport == "codex" and args.source_class == "proprietary"
                        else None
                    )
                    review = validate_review_response(
                        persisted.response,
                        args.response_contract,
                        expected_file_hashes=expected_file_hashes,
                    )
                    validation_error = (
                        review_validation_error(
                            persisted.response,
                            args.response_contract,
                            expected_file_hashes=expected_file_hashes,
                        )
                        if review is None
                        else None
                    )
                    gate_result = "incomplete" if review is None else "accepted"

            result = (
                "incomplete"
                if gate_result in {"contract-incomplete", "contract-invalid"}
                else gate_result
            )
            newly_promoted: tuple[PromotedFragment, ...] = ()
            if native_contract and contract_evaluation is not None and raw_response is not None:
                newly_promoted = promote_fragments(
                    contract_evaluation,
                    contract_context=contract_context,
                    gate_result=gate_result,
                    attempt=number,
                    route_index=route_index,
                    raw_response_digest=str(raw_response["sha256"]),
                )
                if newly_promoted and contract_evaluation.completion_request is not None:
                    promoted_fragments = (*promoted_fragments, *newly_promoted)
                    completion_request = contract_evaluation.completion_request

            attempt = {
                "number": number,
                "routeIndex": route_index,
                "database": str(database) if database else None,
                "evidence": {
                    "request": (
                        str(request_path)
                        if request_path is not None and request_path.is_file()
                        else None
                    ),
                    "response": (
                        str(response_path)
                        if response_path is not None and response_path.is_file()
                        else None
                    ),
                    "session": (
                        str(session_path)
                        if session_path is not None
                        and session_path.is_file()
                        and session_path.stat().st_size > 0
                        else None
                    ),
                    "finalResponse": (
                        str(final_response_path)
                        if final_response_path is not None and final_response_path.is_file()
                        else None
                    ),
                    "stderr": (
                        str(diagnostic_path)
                        if diagnostic_path is not None and diagnostic_path.is_file()
                        else None
                    ),
                },
                "diagnostic": redact_diagnostic(stderr),
                "exitCode": exit_code,
                "isolation": ("macos-source-root-deny" if codex_source_roots else None),
                "model": {
                    "requested": model,
                    "resolved": (
                        persisted.model
                        if persisted is not None and capabilities.resolved_model_identity
                        else None
                    ),
                },
                "provider": {
                    "requested": provider_preferences.get("only", [])
                    if provider_preferences
                    else [],
                    "resolved": (
                        persisted.provider
                        if persisted is not None and capabilities.resolved_provider_identity
                        else None
                    ),
                },
                "providerPreferences": provider_preferences,
                "rawResponse": raw_response,
                "attemptRequestSha256": sha256_bytes(attempt_prompt.encode()),
                "result": result,
                "route": {"model": model, "transport": transport},
                "validationError": validation_error,
                "transport": transport,
                "stderrSha256": sha256_bytes(stderr.encode()),
                "costUsd": persisted.cost_usd if persisted else None,
                "durationMs": persisted.duration_ms if persisted else None,
                "tokens": {
                    "input": persisted.input_tokens if persisted else None,
                    "output": persisted.output_tokens if persisted else None,
                },
                "conversationId": persisted.conversation_id if persisted else None,
                "findings": review.get("findings", []) if review else [],
            }
            if (
                args.response_contract in {"product-review-json", "product-judge-json"}
                and result == "accepted"
                and review is not None
            ):
                contract_identity = receipt_contract_identity(args.response_contract)
                attempt["contractOutput"] = {
                    "name": contract_identity["name"],
                    "version": contract_identity["version"],
                    "status": "complete",
                    "normalizedSha256": sha256_bytes(contract_canonical_json(review)),
                    "contractContext": {
                        "fileNames": [item["name"] for item in source_files],
                        "reviewDeclarationRequired": (
                            require_reviewed_files
                            or (transport == "codex" and args.source_class == "proprietary")
                        ),
                    },
                }
            if native_contract:
                attempt["promotedFragments"] = [fragment.to_dict() for fragment in newly_promoted]
            if contract_evaluation:
                attempt["contractEvaluation"] = {
                    "name": contract_evaluation.name,
                    "version": contract_evaluation.version,
                    "preparedSha256": contract_evaluation.prepared_digest,
                    "payloadSha256": contract_evaluation.payload_digest,
                    "normalizedSha256": contract_evaluation.normalized_digest,
                    "normalizedValue": contract_evaluation.value,
                    "contractContext": {
                        "fileNames": list(contract_context.file_names),
                        "reviewDeclarationRequired": (contract_context.review_declaration_required),
                    },
                    "violations": list(contract_evaluation.violations),
                    "status": contract_evaluation.status.value,
                    "fragments": [
                        {
                            "fragmentId": fragment.fragment_id,
                            "fingerprint": fragment.fingerprint,
                            "kind": fragment.kind.value,
                            "value": fragment.value,
                            "payloadDigest": fragment.payload_digest,
                            "scope": list(fragment.scope),
                        }
                        for fragment in contract_evaluation.valid_fragments
                    ],
                    "coverage": (
                        {
                            "requiredFields": list(contract_evaluation.coverage.required_fields),
                            "coveredFields": list(contract_evaluation.coverage.covered_fields),
                            "missingFields": list(contract_evaluation.coverage.missing_fields),
                        }
                        if contract_evaluation.coverage is not None
                        else None
                    ),
                    "completionRequest": (
                        {
                            "preparedDigest": (
                                contract_evaluation.completion_request.prepared_digest
                            ),
                            "packetDigest": contract_evaluation.completion_request.packet_digest,
                            "missingFields": list(
                                contract_evaluation.completion_request.missing_fields
                            ),
                            "invalidFragmentIndexes": list(
                                contract_evaluation.completion_request.invalid_fragment_indexes
                            ),
                            "violations": list(contract_evaluation.completion_request.violations),
                        }
                        if contract_evaluation.completion_request is not None
                        else None
                    ),
                }
            if evaluation_error is not None:
                attempt["evaluationError"] = evaluation_error
            attempts.append(attempt)
            write_private_exclusive(
                attempt_dir / "attempt.json",
                canonical_json(attempt) + b"\n",
                label="attempt metadata evidence",
                expected_parent_identity=attempt_identity,
            )
            log_event(
                logger,
                "attempt_finished",
                attempt=number,
                diagnostic=redact_diagnostic(stderr),
                duration_ms=persisted.duration_ms if persisted else None,
                exit_code=exit_code,
                model=model,
                provider=persisted.provider if persisted else None,
                result=result,
                review_id=args.review_id,
                transport=transport,
            )
            if result == "accepted":
                accepted = persisted
                accepted_capabilities = capabilities
                accepted_review = review
                accepted_attempt = number
                break
            previous_attempt = number
            previous_route_index = route_index
            previous_gate_result = gate_result
            if result not in RETRIABLE_REVIEW_RESULTS:
                break
        if accepted is not None:
            break

    output_metadata: dict[str, object] | None = None
    output_file = getattr(args, "output_file", None)
    if accepted and output_file:
        output_path = Path(output_file).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(accepted.response)
        output_metadata = {
            "path": str(output_path),
            "sha256": sha256_bytes(accepted.response.encode()),
            "characters": len(accepted.response),
        }

    receipt: dict[str, Any] = {
        "receiptSchemaVersion": 2,
        "acceptedAttempt": accepted_attempt,
        "attempts": attempts,
        "createdAt": utc_now(),
        "model": {
            "requested": requested_models,
            "resolved": (
                accepted.model
                if accepted is not None
                and accepted_capabilities is not None
                and accepted_capabilities.resolved_model_identity
                else None
            ),
        },
        "policy": {"sha256": policy_digest},
        "prompt": {
            "sha256": sha256_bytes(prompt.encode()),
            "characters": len(prompt),
            "packetSha256": packet_digest,
        },
        "result": "accepted" if accepted else "unavailable",
        "reviewContract": args.response_contract,
        "contract": receipt_contract_identity(args.response_contract),
        "reviewId": args.review_id,
        "sourceClass": args.source_class,
        "source": source,
        "tool": {"name": "reviewctl", "version": __version__},
        "executionSettings": {
            "timeoutSeconds": timeout_seconds,
            "maxAttempts": max_attempts,
        },
        "executionConfig": execution_config,
        "transport": (
            routes[0].transport if len({route.transport for route in routes}) == 1 else "routed"
        ),
        "routes": [{"model": route.model, "transport": route.transport} for route in routes],
        "routeProfile": route_profile,
        "providerPreferences": provider_preferences,
        "output": output_metadata,
        "logging": {
            "path": str(log_path),
            "rotation": {"maxBytes": 5 * 1024 * 1024, "backupCount": 5},
        },
    }
    if effective_reviewed_files:
        receipt["executionSettings"]["requireReviewedFiles"] = True
    if kiro_identity_waiver:
        receipt["extension.kiroUnresolvedIdentityWaiver"] = True
    if range_context is not None:
        receipt["extension.rangeReview"] = range_context
    if accepted_attempt is not None and attempts[accepted_attempt - 1]["transport"] == "kiro":
        receipt["extension.backendQualification"] = "unqualified"
        receipt["extension.mergeGateEligible"] = False
    if native_contract:
        assert consolidation_context is not None
        receipt["fallbackRelationships"] = [
            relationship.to_dict() for relationship in fallback_relationships
        ]
        receipt["consolidatedReview"] = consolidate(
            accepted_review,
            promoted_fragments,
            accepted_attempt,
            contract_context=consolidation_context,
        ).to_dict()
    if accepted:
        receipt["response"] = {
            "sha256": sha256_bytes(accepted.response.encode()),
            "characters": len(accepted.response),
            "conversationId": accepted.conversation_id,
            "costUsd": accepted.cost_usd,
            "durationMs": accepted.duration_ms,
            "provider": (
                accepted.provider
                if accepted_capabilities is not None
                and accepted_capabilities.resolved_provider_identity
                else None
            ),
        }
        if args.response_contract == "findings-json":
            receipt["findings"] = accepted_review["findings"] if accepted_review else []
            receipt["verdict"] = accepted_review["verdict"] if accepted_review else "unstructured"
        elif args.response_contract in {"product-review-json", "product-judge-json"}:
            receipt["review"] = accepted_review
    try:
        if args.seal_to:
            request_payload = canonical_json(
                {
                    "routes": [
                        {"model": route.model, "transport": route.transport} for route in routes
                    ],
                    "prompt": review_prompt,
                    "source": source,
                }
            )
            receipt["sealed"] = {
                "request": seal(
                    turn_dir / "request.json",
                    request_payload,
                    args.seal_to,
                    expected_parent_identity=turn_identity,
                ),
                "response": seal(
                    turn_dir / "response.md",
                    (accepted.response if accepted else "").encode(),
                    args.seal_to,
                    expected_parent_identity=turn_identity,
                ),
            }

        receipt["sha256"] = sha256_bytes(contract_canonical_json(receipt))
        expected_receipt_sha256 = receipt["sha256"]
        receipt_path = turn_dir / "receipt.json"
        write_private_exclusive(
            receipt_path,
            canonical_json(receipt) + b"\n",
            label="review receipt evidence",
            expected_parent_identity=turn_identity,
        )
        if not persisted_receipt_valid(
            receipt_path,
            expected_sha256=expected_receipt_sha256,
            expected_parent_identity=turn_identity,
        ):
            log_event(
                logger,
                "review_finished",
                accepted_attempt=accepted_attempt,
                attempts=len(attempts),
                result="receipt_invalid",
                review_id=args.review_id,
            )
            print(turn_dir)
            print(
                "reviewctl: receipt_invalid: persisted review receipt failed verification",
                file=sys.stderr,
            )
            return exit_code_for("receipt_invalid")
        log_event(
            logger,
            "review_finished",
            accepted_attempt=accepted_attempt,
            attempts=len(attempts),
            result=receipt["result"],
            review_id=args.review_id,
        )
        print(turn_dir)
        return 0 if accepted else 1
    finally:
        snapshots_context.__exit__(None, None, None)
        for attempt_dir, attempt_identity in attempt_identities.items():
            try:
                with confined_directory_descriptor(
                    attempt_dir, expected_identity=attempt_identity
                ) as attempt_descriptor:
                    try:
                        os.unlink("transport.sqlite3", dir_fd=attempt_descriptor)
                    except FileNotFoundError:
                        pass
            except OSError:
                pass


def run_transport_canary(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Exercise one configured profile through the ordinary receipt pipeline."""
    routes, profile = load_route_profile(parser, args.config, args.profile)
    artifact_root = Path(os.path.abspath(Path(args.artifact_root).expanduser()))
    review_id = f"transport-canary-{sha256_bytes(args.profile.encode())[:12]}"
    before = receipt_fingerprints(artifact_root)
    with tempfile.TemporaryDirectory(prefix="reviewctl-transport-canary-") as temporary_directory:
        source_path = Path(temporary_directory) / CANARY_SOURCE_NAME
        source_path.write_text(CANARY_SOURCE, encoding="utf-8")
        review_arguments = [
            "run",
            "--review-id",
            review_id,
            "--profile",
            args.profile,
            "--config",
            args.config,
            "--prompt",
            canary_prompt(),
            "--file",
            str(source_path),
            "--artifact-root",
            str(artifact_root),
            "--source-class",
            "synthetic",
            "--response-contract",
            "findings-json",
            "--require-reviewed-files",
            "--max-attempts",
            "1",
            "--max-output-tokens",
            str(args.max_output_tokens),
        ]
        if args.timeout_seconds is not None:
            review_arguments.extend(("--timeout-seconds", str(args.timeout_seconds)))
        review_args = parser.parse_args(review_arguments)
        exit_code = run_review(parser, review_args)

    receipt_path = tournament_receipt_path(artifact_root, before, review_id)
    if receipt_path is None:
        return exit_code
    receipt = json.loads(read_confined_text(receipt_path))
    parent_metadata = receipt_path.parent.stat(follow_symlinks=False)
    report = build_canary_report(
        profile=profile,
        routes=[{"model": route.model, "transport": route.transport} for route in routes],
        receipt_path=receipt_path,
        receipt=receipt,
        exit_code=exit_code,
    )
    report_path = receipt_path.with_name("transport-canary.json")
    write_private_exclusive(
        report_path,
        canonical_json(report) + b"\n",
        label="transport canary report",
        expected_parent_identity=(parent_metadata.st_dev, parent_metadata.st_ino),
    )
    print(report_path)
    return exit_code


def valid_receipt(receipt: dict[str, Any]) -> bool:
    """Verify the hash embedded in an in-memory receipt without mutating it."""
    recorded = receipt.get("sha256")
    unsigned = {key: value for key, value in receipt.items() if key != "sha256"}
    try:
        reproduced = sha256_bytes(canonical_json(unsigned))
    except TypeError, ValueError, UnicodeError, OverflowError:
        return False
    return isinstance(recorded, str) and recorded == reproduced


def legacy_receipt_declares_transport(receipt: dict[str, Any], transport: str) -> bool:
    """Detect a transport claim in the routing positions of a legacy receipt."""
    if receipt.get("transport") == transport:
        return True
    routes = receipt.get("routes")
    if type(routes) is list and any(
        type(route) is dict and route.get("transport") == transport for route in routes
    ):
        return True
    attempts = receipt.get("attempts")
    if type(attempts) is not list:
        return False
    for attempt in attempts:
        if type(attempt) is not dict:
            continue
        route = attempt.get("route")
        if attempt.get("transport") == transport or (
            type(route) is dict and route.get("transport") == transport
        ):
            return True
    return False


PROJECT_CHECKPOINT_KIND = "project-review-checkpoint"
PROJECT_CHECKPOINT_FIELDS = frozenset(
    {
        "projectId",
        "originId",
        "journalSequence",
        "privacyMode",
        "dimensionCoverage",
        "fallbackRelationships",
    }
)


def project_checkpoint_shape(value: object) -> bool:
    """Recognize project checkpoints only to reject them from global verification."""
    if type(value) is not dict:
        return False
    if value.get("artifactKind") == PROJECT_CHECKPOINT_KIND:
        return True
    if "projectCheckpointSchemaVersion" in value:
        return True
    if "receiptSchemaVersion" in value:
        return False
    return PROJECT_CHECKPOINT_FIELDS.issubset(value)


def receipt_bytes_violations(contents: bytes) -> tuple[str, ...]:
    """Apply the canonical offline verifier to unchanged receipt bytes."""

    def reject_nonstandard_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    try:
        receipt = json.loads(
            contents.decode("utf-8"),
            object_pairs_hook=exact_json_object,
            parse_constant=reject_nonstandard_constant,
        )
    except UnicodeError, ValueError:
        violations = ("json-receipt",)
    else:
        if project_checkpoint_shape(receipt):
            violations = ("project-checkpoint-not-review-receipt",)
        elif not isinstance(receipt, dict):
            violations = ("receipt-object",)
        elif "receiptSchemaVersion" not in receipt:
            if not valid_receipt(receipt):
                violations = ("receipt-digest",)
            elif legacy_receipt_declares_transport(receipt, "kiro"):
                violations = ("backend-qualification",)
            else:
                violations = ()
        elif receipt.get("receiptSchemaVersion") == 2 and not isinstance(
            receipt.get("receiptSchemaVersion"), bool
        ):
            violations = validate_v2_receipt(receipt)
        else:
            violations = ("receipt-schema-version",)
    return violations


def verify_receipt(args: argparse.Namespace) -> int:
    receipt_path = Path(args.receipt)
    try:
        violations = receipt_bytes_violations(read_confined_bytes(receipt_path))
    except OSError:
        violations = ("json-receipt",)
    valid = not violations
    print(
        json.dumps(
            {
                "receipt": str(receipt_path),
                "valid": valid,
                "violations": list(violations),
            },
            sort_keys=True,
        )
    )
    return 0 if valid else 1


def policy_check(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    model = policy_entry(policy, args.model, transport=getattr(args, "transport", None))
    source_allowed = bool(model.get("source_allowed", False))
    decision = {
        "advisory": not args.enforce,
        "model": args.model,
        "sourceAllowed": source_allowed,
        "syntheticOnly": not source_allowed,
        "zdr": model.get("zdr", "unknown"),
        "dataCollection": model.get("data_collection", "unknown"),
        "mode": "enforced" if args.enforce else "advisory",
    }
    if args.transport is not None:
        decision["transport"] = args.transport
        decision["allowUnresolvedIdentity"] = bool(model.get("allow_unresolved_identity", False))
    print(json.dumps(decision, sort_keys=True))
    return 0 if source_allowed or not args.enforce else 3


def estimate_tokens(prompt: str, files: list[Path]) -> int:
    return max(1, (len(prompt.encode()) + sum(file.stat().st_size for file in files) + 3) // 4)


def score_findings(
    *, expected: list[dict[str, Any]], findings: list[dict[str, Any]]
) -> dict[str, int | float]:
    """Score a bounded review against an adjudicated location and severity rubric."""
    matched_expected: set[int] = set()
    line_accurate = 0
    for finding in findings:
        filename = Path(str(finding["path"])).name
        same_line = [
            index
            for index, item in enumerate(expected)
            if int(item.get("line_start", item.get("line")))
            <= finding["line"]
            <= int(item.get("line_end", item.get("line")))
            and (
                Path(str(item["path"])).name == filename
                or (isinstance(item.get("symbol"), str) and item["symbol"] in str(finding["path"]))
            )
        ]
        if same_line:
            line_accurate += 1
        for index in same_line:
            if (
                index not in matched_expected
                and FINDING_SEVERITY_RANK[str(finding["severity"]).lower()]
                >= FINDING_SEVERITY_RANK[str(expected[index]["severity"]).lower()]
            ):
                matched_expected.add(index)
                break
    matched = len(matched_expected)
    return {
        "expected": len(expected),
        "matched": matched,
        "falsePositives": len(findings) - matched,
        "lineAccurate": line_accurate,
        "precision": matched / len(findings) if findings else 1.0,
        "recall": matched / len(expected) if expected else 1.0,
    }


def tournament_report_path(plan: dict[str, Any], plan_path: Path) -> Path:
    configured = plan.get("artifact_root")
    root = Path(configured) if configured else Path("tournament-artifacts")
    if not root.is_absolute():
        root = plan_path.parent / root
    root.mkdir(parents=True, exist_ok=True)
    return root / "tournament.json"


def write_tournament_report(
    path: Path,
    *,
    budget: float,
    estimated_spend: float,
    actual_spend: float,
    result: str,
    runs: list[dict[str, Any]],
) -> None:
    """Atomically persist the current tournament state after every attempt."""
    payload = {
        "actualSpendUsd": actual_spend,
        "budgetUsd": budget,
        "estimatedSpendUsd": estimated_spend,
        "result": result,
        "runs": runs,
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_bytes(canonical_json(payload) + b"\n")
    temporary.replace(path)


def receipt_attempt_cost(receipt: dict[str, Any]) -> float | None:
    """Return total provider spend for all recorded attempts in one review receipt."""
    attempts = receipt.get("attempts")
    if not isinstance(attempts, list):
        return None
    costs = [
        float(cost)
        for attempt in attempts
        if isinstance(attempt, dict)
        and isinstance((cost := attempt.get("costUsd")), int | float)
        and not isinstance(cost, bool)
    ]
    return sum(costs) if costs else None


def select_tournament_cases(
    parser: argparse.ArgumentParser, plan: dict[str, Any], args: argparse.Namespace
) -> list[dict[str, Any]]:
    """Select named cases and an optional phase without silently widening the corpus."""
    cases = plan.get("cases", [])
    if not isinstance(cases, list):
        parser.error("tournament plan cases must be a list")
    stage = getattr(args, "stage", None)
    if stage is not None:
        cases = [case for case in cases if isinstance(case, dict) and case.get("stage") == stage]
        if not cases:
            parser.error("tournament plan does not contain the requested --stage")
    if args.case_ids:
        selected = set(args.case_ids)
        cases = [case for case in cases if isinstance(case, dict) and case.get("id") in selected]
        if len(cases) != len(selected):
            parser.error("tournament plan does not contain every requested --case")
    return cases


def tournament_case_files(case: dict[str, Any], plan_path: Path) -> list[Path]:
    files = case.get("files")
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ValueError("tournament case requires a string files list")
    return [
        (Path(item) if Path(item).is_absolute() else plan_path.parent / item).resolve()
        for item in files
    ]


def receipt_fingerprints(root: Path) -> dict[Path, str]:
    """Capture safe content identities for receipts already present in a run root."""
    if not root.exists():
        return {}
    fingerprints: dict[Path, str] = {}
    for path in root.glob("**/receipt.json"):
        try:
            fingerprints[path] = sha256_bytes(read_confined_bytes(path))
        except OSError:
            continue
    return fingerprints


def changed_receipt_paths(root: Path, before: dict[Path, str]) -> list[Path]:
    """Return receipts created or changed by one tournament invocation."""
    paths: list[Path] = []
    for path in root.glob("**/receipt.json") if root.exists() else ():
        try:
            fingerprint = sha256_bytes(read_confined_bytes(path))
        except OSError:
            paths.append(path)
            continue
        if before.get(path) != fingerprint:
            paths.append(path)
    return sorted(paths)


def tournament_receipt_path(root: Path, before: dict[Path, str], review_id: str) -> Path | None:
    """Select exactly one valid receipt belonging to the current tournament run."""
    changed = changed_receipt_paths(root, before)
    if len(changed) != 1:
        return None
    path = changed[0]
    try:
        receipt = json.loads(
            read_confined_text(path),
            object_pairs_hook=exact_json_object,
            parse_constant=reject_nonstandard_json_constant,
        )
    except OSError, UnicodeError, ValueError:
        return None
    if (
        not isinstance(receipt, dict)
        or receipt.get("reviewId") != review_id
        or not persisted_receipt_valid(path)
    ):
        return None
    return path


def run_candidate_tournament(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    plan: dict[str, Any],
    plan_path: Path,
    budget: float,
    cases: list[dict[str, Any]],
    candidates: list[TournamentCandidate],
    max_output_tokens: int,
) -> int:
    """Run a mixed-transport synthetic cohort while charging only metered candidates."""
    response_contract = str(plan.get("response_contract", "product-review-json"))
    if response_contract not in {"product-review-json", "product-judge-json"}:
        parser.error("candidate tournaments require a product response contract")
    if not cases:
        parser.error("tournament plan requires candidates and cases")
    max_attempts = plan.get("max_attempts", 1)
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 3:
        parser.error("candidate tournament max_attempts must be an integer from 1 to 3")
    timeout_seconds = plan.get("timeout_seconds", 90)
    assert isinstance(timeout_seconds, int) and not isinstance(timeout_seconds, bool)
    assert timeout_seconds > 0
    report_path = tournament_report_path(plan, plan_path)
    runs: list[dict[str, Any]] = []
    estimated_spend = 0.0
    actual_spend = 0.0
    missing_receipt = False
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            parser.error("tournament cases require an id")
        files = tournament_case_files(case, plan_path)
        prompt = case.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            parser.error("tournament cases require a prompt")
        input_tokens = estimate_tokens(packet_prompt(prompt, files, response_contract), files)
        for candidate in candidates:
            requested_max_output_tokens = candidate.max_output_tokens or max_output_tokens
            candidate_max_output_tokens = requested_max_output_tokens
            estimate: float | None = None
            if candidate.cost_mode == "metered":
                assert candidate.pricing is not None
                estimate = (
                    input_tokens * candidate.pricing[0] / 1_000_000
                    + candidate_max_output_tokens * candidate.pricing[1] / 1_000_000
                ) * max_attempts
                if estimated_spend + estimate > budget:
                    write_tournament_report(
                        report_path,
                        budget=budget,
                        estimated_spend=estimated_spend,
                        actual_spend=actual_spend,
                        result="budget-exhausted",
                        runs=runs,
                    )
                    print(report_path)
                    return 4
                estimated_spend += estimate
            case_root = report_path.parent / "receipts"
            namespace = argparse.Namespace(
                artifact_root=str(case_root),
                files=[str(file) for file in files],
                max_output_tokens=candidate_max_output_tokens,
                max_attempts=max_attempts,
                models=[candidate.model],
                policy=None,
                prompt=prompt,
                prompt_file=None,
                provider_allow_fallbacks=(
                    candidate.provider_preferences.get("allow_fallbacks")
                    if candidate.provider_preferences
                    else None
                ),
                provider_data_collection=(
                    candidate.provider_preferences.get("data_collection")
                    if candidate.provider_preferences
                    else None
                ),
                provider_require_parameters=(
                    candidate.provider_preferences.get("require_parameters")
                    if candidate.provider_preferences
                    else None
                ),
                provider_only=(
                    candidate.provider_preferences.get("only", [])
                    if candidate.provider_preferences
                    else []
                ),
                provider_order=(
                    candidate.provider_preferences.get("order", [])
                    if candidate.provider_preferences
                    else []
                ),
                provider_sort=(
                    candidate.provider_preferences.get("sort")
                    if candidate.provider_preferences
                    else None
                ),
                review_id=f"tournament.{case['id']}.{candidate.identifier}",
                response_contract=response_contract,
                seal_to=None,
                source_class="synthetic",
                timeout_seconds=timeout_seconds,
                transport=candidate.transport,
            )
            before = receipt_fingerprints(case_root)
            exit_code = run_review(parser, namespace)
            receipt_path = tournament_receipt_path(case_root, before, str(namespace.review_id))
            if receipt_path is None:
                missing_receipt = True
                runs.append(
                    {
                        "actualCostUsd": None,
                        "candidate": candidate.identifier,
                        "case": case["id"],
                        "councilEligible": candidate.council_eligible,
                        "costMode": candidate.cost_mode,
                        "estimatedCostUsd": estimate,
                        "exitCode": exit_code,
                        "family": candidate.family,
                        "maxOutputTokens": candidate_max_output_tokens,
                        "model": candidate.model,
                        "outputTokenLimitEnforced": candidate.transport in {"llm", "openrouter"},
                        "receipt": None,
                        "requestedMaxOutputTokens": requested_max_output_tokens,
                        "result": "missing-receipt",
                        "transport": candidate.transport,
                    }
                )
                write_tournament_report(
                    report_path,
                    budget=budget,
                    estimated_spend=estimated_spend,
                    actual_spend=actual_spend,
                    result="running",
                    runs=runs,
                )
                continue
            receipt = json.loads(read_confined_text(receipt_path))
            actual_cost = receipt_attempt_cost(receipt)
            if candidate.cost_mode == "metered" and actual_cost is not None:
                actual_spend += actual_cost
            runs.append(
                {
                    "actualCostUsd": actual_cost,
                    "candidate": candidate.identifier,
                    "case": case["id"],
                    "councilEligible": candidate.council_eligible,
                    "costMode": candidate.cost_mode,
                    "estimatedCostUsd": estimate,
                    "exitCode": exit_code,
                    "family": candidate.family,
                    "maxOutputTokens": candidate_max_output_tokens,
                    "model": candidate.model,
                    "outputTokenLimitEnforced": candidate.transport in {"llm", "openrouter"},
                    "requestedMaxOutputTokens": requested_max_output_tokens,
                    "receipt": str(receipt_path),
                    "result": str(receipt["result"]),
                    "transport": candidate.transport,
                }
            )
            write_tournament_report(
                report_path,
                budget=budget,
                estimated_spend=estimated_spend,
                actual_spend=actual_spend,
                result="running",
                runs=runs,
            )
    aggregate_result = "incomplete" if missing_receipt else "completed"
    write_tournament_report(
        report_path,
        budget=budget,
        estimated_spend=estimated_spend,
        actual_spend=actual_spend,
        result=aggregate_result,
        runs=runs,
    )
    print(report_path)
    return 1 if missing_receipt else 0


def run_tournament(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    plan_path = Path(args.plan)
    plan = load_policy(str(plan_path))
    budget = numeric_value(plan.get("budget_usd", 0))
    max_output_tokens = plan.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    timeout_seconds = plan.get("timeout_seconds", 90)
    if (
        budget is None
        or budget <= 0
        or not isinstance(max_output_tokens, int)
        or isinstance(max_output_tokens, bool)
        or max_output_tokens <= 0
    ):
        parser.error("tournament plan requires positive budget_usd and max_output_tokens")
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        parser.error("tournament timeout_seconds must be a positive integer")
    cases = select_tournament_cases(parser, plan, args)
    if "candidates" in plan:
        try:
            candidates = parse_tournament_candidates(plan)
        except ValueError as error:
            parser.error(str(error))
        return run_candidate_tournament(
            parser,
            args,
            plan=plan,
            plan_path=plan_path,
            budget=budget,
            cases=cases,
            candidates=candidates,
            max_output_tokens=max_output_tokens,
        )
    transport = str(plan.get("transport", "llm"))
    try:
        provider_preferences = normalize_provider_preferences(plan.get("provider"))
    except ValueError as error:
        parser.error(str(error))
    if provider_preferences and transport != "openrouter":
        parser.error("tournament provider preferences require transport = openrouter")
    if transport not in {"llm", "codex", "openrouter", "agy"}:
        parser.error("tournament plan requires a supported transport")
    if transport == "agy":
        parser.error("agy does not support tournament budgets because it cannot cap output tokens")
    models = plan.get("models", {})
    if not models or not cases:
        parser.error("tournament plan requires models and cases")
    legacy_max_attempts = plan.get("max_attempts", 1)
    if not isinstance(legacy_max_attempts, int) or not 1 <= legacy_max_attempts <= 3:
        parser.error("tournament plan max_attempts must be an integer from 1 to 3")
    legacy_models: list[tuple[str, float, float, int, int]] = []
    for model, pricing in models.items():
        if not isinstance(pricing, dict):
            parser.error("tournament model pricing must be an object")
        input_price = numeric_value(pricing.get("input_per_million_usd"))
        output_price = numeric_value(pricing.get("output_per_million_usd"))
        if input_price is None or output_price is None or input_price < 0 or output_price < 0:
            parser.error("tournament model requires finite nonnegative pricing")
        raw_candidate_max_output_tokens = pricing.get("max_output_tokens")
        if raw_candidate_max_output_tokens is not None and (
            not isinstance(raw_candidate_max_output_tokens, int)
            or isinstance(raw_candidate_max_output_tokens, bool)
            or raw_candidate_max_output_tokens <= 0
        ):
            parser.error("tournament model max_output_tokens must be a positive integer")
        requested_max_output_tokens = raw_candidate_max_output_tokens or max_output_tokens
        effective_max_output_tokens = requested_max_output_tokens
        legacy_models.append(
            (
                model,
                input_price,
                output_price,
                requested_max_output_tokens,
                effective_max_output_tokens,
            )
        )

    report_path = tournament_report_path(plan, plan_path)
    runs: list[dict[str, Any]] = []
    estimated_spend = 0.0
    actual_spend = 0.0
    missing_receipt = False
    for case in cases:
        files = [
            (Path(item) if Path(item).is_absolute() else plan_path.parent / item).resolve()
            for item in case["files"]
        ]
        prompt = str(case["prompt"])
        input_tokens = estimate_tokens(packet_prompt(prompt, files), files)
        for (
            model,
            input_price,
            output_price,
            requested_max_output_tokens,
            candidate_max_output_tokens,
        ) in legacy_models:
            estimate = (
                input_tokens * input_price / 1_000_000
                + candidate_max_output_tokens * output_price / 1_000_000
            ) * legacy_max_attempts
            if estimated_spend + estimate > budget:
                write_tournament_report(
                    report_path,
                    budget=budget,
                    estimated_spend=estimated_spend,
                    actual_spend=actual_spend,
                    result="budget-exhausted",
                    runs=runs,
                )
                print(report_path)
                return 4
            estimated_spend += estimate
            case_root = report_path.parent / "receipts"
            namespace = argparse.Namespace(
                artifact_root=str(case_root),
                files=[str(file) for file in files],
                max_attempts=legacy_max_attempts,
                max_output_tokens=candidate_max_output_tokens,
                models=[model],
                policy=None,
                prompt=prompt,
                prompt_file=None,
                provider_allow_fallbacks=(
                    provider_preferences.get("allow_fallbacks") if provider_preferences else None
                ),
                provider_data_collection=(
                    provider_preferences.get("data_collection") if provider_preferences else None
                ),
                provider_require_parameters=(
                    provider_preferences.get("require_parameters") if provider_preferences else None
                ),
                provider_only=(
                    provider_preferences.get("only", []) if provider_preferences else []
                ),
                provider_order=(
                    provider_preferences.get("order", []) if provider_preferences else []
                ),
                provider_sort=(provider_preferences.get("sort") if provider_preferences else None),
                review_id=f"tournament.{case['id']}.{model.replace('/', '-')}",
                response_contract=str(plan.get("response_contract", "verdict")),
                seal_to=None,
                source_class="synthetic",
                timeout_seconds=timeout_seconds,
                transport=transport,
            )
            before = receipt_fingerprints(case_root)
            result = run_review(parser, namespace)
            receipt_path = tournament_receipt_path(case_root, before, str(namespace.review_id))
            if receipt_path is None:
                missing_receipt = True
                runs.append(
                    {
                        "case": case["id"],
                        "actualCostUsd": None,
                        "estimatedCostUsd": estimate,
                        "exitCode": result,
                        "maxOutputTokens": candidate_max_output_tokens,
                        "model": model,
                        "outputTokenLimitEnforced": transport in {"llm", "openrouter"},
                        "receipt": None,
                        "requestedMaxOutputTokens": requested_max_output_tokens,
                        "result": "missing-receipt",
                        "score": None,
                    }
                )
                write_tournament_report(
                    report_path,
                    budget=budget,
                    estimated_spend=estimated_spend,
                    actual_spend=actual_spend,
                    result="running",
                    runs=runs,
                )
                continue
            receipt = json.loads(read_confined_text(receipt_path))
            receipt_result = str(receipt["result"])
            actual_cost = receipt_attempt_cost(receipt)
            if actual_cost is None:
                cost = receipt.get("response", {}).get("costUsd")
                actual_cost = float(cost) if isinstance(cost, int | float) else None
            if actual_cost is not None:
                actual_spend += actual_cost
            findings = receipt.get("findings", [])
            runs.append(
                {
                    "case": case["id"],
                    "actualCostUsd": actual_cost,
                    "estimatedCostUsd": estimate,
                    "exitCode": result,
                    "maxOutputTokens": candidate_max_output_tokens,
                    "model": model,
                    "outputTokenLimitEnforced": transport in {"llm", "openrouter"},
                    "requestedMaxOutputTokens": requested_max_output_tokens,
                    "receipt": str(receipt_path),
                    "result": receipt_result,
                    "score": score_findings(
                        expected=list(case.get("expected_findings", [])),
                        findings=findings if isinstance(findings, list) else [],
                    ),
                }
            )
            write_tournament_report(
                report_path,
                budget=budget,
                estimated_spend=estimated_spend,
                actual_spend=actual_spend,
                result="running",
                runs=runs,
            )
    aggregate_result = "incomplete" if missing_receipt else "completed"
    write_tournament_report(
        report_path,
        budget=budget,
        estimated_spend=estimated_spend,
        actual_spend=actual_spend,
        result=aggregate_result,
        runs=runs,
    )
    print(report_path)
    return 1 if missing_receipt else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reviewctl")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="execute bounded isolated review attempts")
    run.add_argument("--review-id", required=True)
    run.add_argument("--prompt")
    run.add_argument("--prompt-file")
    run.add_argument("--model", dest="models", action="append", default=[])
    run.add_argument(
        "--route",
        dest="routes",
        action="append",
        default=[],
        help="ordered fallback route: transport:model (repeatable)",
    )
    run.add_argument(
        "--profile",
        help="named ordered route profile from --config",
    )
    run.add_argument(
        "--config",
        help="TOML route config (default: ~/.config/reviewctl/config.toml)",
    )
    run.add_argument("--file", dest="files", action="append", default=[])
    run.add_argument("--artifact-root", default="~/.cache/reviewctl")
    run.add_argument(
        "--log-file",
        help="rotating JSONL diagnostic log (defaults to <artifact-root>/reviewctl.log)",
    )
    run.add_argument(
        "--output-file",
        help="write the accepted model response to this document path",
    )
    run.add_argument(
        "--range-context-file",
        help="internal immutable range identity to embed in the v2 receipt",
    )
    run.add_argument("--timeout-seconds", type=positive_timeout_seconds, default=None)
    run.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    run.add_argument(
        "--reasoning-effort",
        choices=sorted(OPENROUTER_REASONING_EFFORTS),
        default=None,
        help="OpenRouter reasoning effort; overrides profile reasoning_effort",
    )
    run.add_argument("--max-attempts", type=int, default=None)
    run.add_argument(
        "--require-reviewed-files",
        action="store_true",
        default=None,
        help="require the model to declare every supplied file in reviewedFiles",
    )
    run.add_argument("--policy")
    run.add_argument("--provider-only", action="append", default=[])
    run.add_argument("--provider-order", action="append", default=[])
    run.add_argument(
        "--no-provider-fallbacks",
        dest="provider_allow_fallbacks",
        action="store_false",
        default=None,
    )
    run.add_argument("--provider-data-collection", choices=("allow", "deny"))
    run.add_argument(
        "--provider-require-parameters",
        dest="provider_require_parameters",
        action="store_true",
        default=None,
    )
    run.add_argument("--provider-sort", choices=("price", "throughput", "latency"))
    run.add_argument("--seal-to")
    run.add_argument("--source-class", choices=("proprietary", "synthetic"), default="synthetic")
    run.add_argument("--response-contract", choices=sorted(RESPONSE_CONTRACTS), default="verdict")
    run.add_argument(
        "--transport",
        choices=("llm", "codex", "openrouter", "agy", "gemini", "kiro", "pi"),
        default="llm",
    )
    run.set_defaults(handler=lambda namespace: run_review(parser, namespace))

    transport_canary = commands.add_parser(
        "transport-canary",
        help="exercise one configured profile through a synthetic formal receipt",
    )
    transport_canary.add_argument("--profile", required=True)
    transport_canary.add_argument(
        "--config",
        help="TOML route config (default: ~/.config/reviewctl/config.toml)",
    )
    transport_canary.add_argument("--artifact-root", default="~/.cache/reviewctl/canaries")
    transport_canary.add_argument("--timeout-seconds", type=positive_timeout_seconds)
    transport_canary.add_argument(
        "--max-output-tokens", type=positive_integer, default=DEFAULT_MAX_OUTPUT_TOKENS
    )
    transport_canary.set_defaults(handler=lambda namespace: run_transport_canary(parser, namespace))

    range_review = commands.add_parser(
        "range-review",
        help="freeze a Git range or formally review its immutable manifest",
    )
    range_review.add_argument("--repository")
    range_review.add_argument("--base")
    range_review.add_argument("--head")
    range_review.add_argument("--output")
    range_review.add_argument(
        "--context-lines",
        type=positive_integer,
        default=DEFAULT_CONTEXT_LINES,
        help="unified diff context lines (default: 3)",
    )
    range_review.add_argument(
        "--max-chunk-bytes",
        type=positive_integer,
        default=DEFAULT_MAX_CHUNK_BYTES,
        help="maximum bytes per complete-file chunk (default: 131072)",
    )
    range_review.add_argument(
        "--allow-empty",
        action="store_true",
        help="write a zero-chunk manifest when base and head have no diff",
    )
    range_review.add_argument("--manifest", help="consume a previously frozen range manifest")
    range_review.add_argument("--review-id", help="parent review id for formal chunk receipts")
    range_review.add_argument("--prompt")
    range_review.add_argument("--prompt-file")
    range_review.add_argument("--model", help="model identifier for each chunk review")
    range_review.add_argument(
        "--transport",
        choices=("llm", "codex", "openrouter", "agy", "gemini", "kiro", "pi"),
        default="llm",
    )
    range_review.add_argument("--policy")
    range_review.add_argument(
        "--source-class",
        choices=("proprietary", "synthetic"),
        default="proprietary",
    )
    range_review.add_argument(
        "--response-contract",
        choices=sorted(RESPONSE_CONTRACTS),
        default="findings-json",
    )
    range_review.add_argument("--aggregate-output")
    range_review.add_argument("--artifact-root", default="~/.cache/reviewctl/range-reviews")
    range_review.add_argument("--timeout-seconds", type=positive_timeout_seconds, default=None)
    range_review.add_argument(
        "--max-output-tokens", type=positive_integer, default=DEFAULT_MAX_OUTPUT_TOKENS
    )
    range_review.add_argument(
        "--reasoning-effort",
        choices=sorted(OPENROUTER_REASONING_EFFORTS),
        default=None,
        help="OpenRouter reasoning effort forwarded to each formal chunk review",
    )
    range_review.add_argument("--max-attempts", type=positive_integer, default=1)
    range_review.set_defaults(handler=lambda namespace: range_review_command(parser, namespace))

    range_verify = commands.add_parser(
        "range-verify",
        help="fail-closed verification of a formal range aggregate and its chunk receipts",
    )
    range_verify.add_argument("--manifest", required=True)
    range_verify.add_argument("--aggregate", required=True)
    range_verify.set_defaults(
        handler=lambda namespace: verify_range_aggregate_command(parser, namespace)
    )

    explore = commands.add_parser(
        "explore", help="run resumable Pi conversations and prepare formal review handoffs"
    )
    explore_commands = explore.add_subparsers(dest="explore_command", required=True)
    explore_start = explore_commands.add_parser("start", help="start a named Pi exploration")
    explore_start.add_argument("--id", required=True)
    explore_start.add_argument("--model", required=True)
    explore_start.add_argument("--prompt")
    explore_start.add_argument("--prompt-file")
    explore_start.add_argument("--cwd", default=".")
    explore_start.add_argument("--tools", default=DEFAULT_EXPLORATION_TOOLS)
    explore_start.add_argument("--timeout-seconds", type=positive_timeout_seconds, default=900)
    explore_start.add_argument("--exploration-root", default="~/.cache/reviewctl/explorations")
    explore_start.set_defaults(
        handler=lambda namespace: run_exploration_turn(parser, namespace, starting=True)
    )

    explore_resume = explore_commands.add_parser("resume", help="continue a named Pi exploration")
    explore_resume.add_argument("--id", required=True)
    explore_resume.add_argument("--model")
    explore_resume.add_argument("--prompt")
    explore_resume.add_argument("--prompt-file")
    explore_resume.add_argument("--cwd")
    explore_resume.add_argument("--tools")
    explore_resume.add_argument("--timeout-seconds", type=positive_timeout_seconds, default=900)
    explore_resume.add_argument("--exploration-root", default="~/.cache/reviewctl/explorations")
    explore_resume.set_defaults(
        handler=lambda namespace: run_exploration_turn(parser, namespace, starting=False)
    )

    explore_show = explore_commands.add_parser("show", help="show a named exploration manifest")
    explore_show.add_argument("--id", required=True)
    explore_show.add_argument("--exploration-root", default="~/.cache/reviewctl/explorations")
    explore_show.set_defaults(handler=lambda namespace: show_exploration(parser, namespace))

    explore_promote = explore_commands.add_parser(
        "promote", help="prepare the latest exploration response for formal review"
    )
    explore_promote.add_argument("--id", required=True)
    explore_promote.add_argument("--output", required=True)
    explore_promote.add_argument("--exploration-root", default="~/.cache/reviewctl/explorations")
    explore_promote.set_defaults(handler=lambda namespace: promote_exploration(parser, namespace))

    setup = commands.add_parser(
        "setup",
        help="inspect local backend executable availability",
        epilog="Use a setup subcommand with --format human or --format json.",
    )
    setup.set_defaults(backends=())
    setup_commands = setup.add_subparsers(dest="setup_command", required=True)
    for name, help_text in (
        ("discover", "discover the observed local backend topology"),
        ("show", "show the observed local backend topology"),
    ):
        setup_command = setup_commands.add_parser(name, help=help_text)
        setup_command.add_argument("--format", choices=("human", "json"), default="human")
        setup_command.set_defaults(handler=run_setup)
    setup_check = setup_commands.add_parser(
        "check", help="check local executable backend availability"
    )
    setup_check.add_argument(
        "--backend",
        dest="backends",
        action="append",
        choices=tuple(descriptor.name for descriptor in build_backend_registry().descriptors()),
        default=[],
        help="backend name to check (repeatable; defaults to all local executables)",
    )
    setup_check.add_argument("--format", choices=("human", "json"), default="human")
    setup_check.set_defaults(handler=run_setup)

    help_llm_parser = commands.add_parser(
        "help-llm", help="print concise usage guidance for coding agents"
    )
    help_llm_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    help_llm_parser.set_defaults(handler=lambda namespace: help_llm(parser, namespace))

    verify = commands.add_parser("verify", help="verify a receipt hash")
    verify.add_argument("receipt")
    verify.set_defaults(handler=verify_receipt)

    policy = commands.add_parser("policy-check", help="check a model privacy profile")
    policy.add_argument("--policy", required=True)
    policy.add_argument("--model", required=True)
    policy.add_argument(
        "--transport",
        choices=tuple(sorted(LOCAL_POLICY_TRANSPORTS)),
        help="resolve a local transport default when no exact model entry exists",
    )
    policy.add_argument(
        "--enforce",
        action="store_true",
        help="return failure for a model without source_allowed=true",
    )
    policy.set_defaults(handler=policy_check)

    tournament = commands.add_parser("tournament", help="run a bounded synthetic model tournament")
    tournament.add_argument("--plan", required=True)
    tournament.add_argument("--case", dest="case_ids", action="append", default=[])
    tournament.add_argument("--stage")
    tournament.set_defaults(handler=lambda namespace: run_tournament(parser, namespace))

    provider_preflight = commands.add_parser(
        "provider-preflight", help="snapshot and validate pinned OpenRouter tournament providers"
    )
    provider_preflight.add_argument("--plan", required=True)
    provider_preflight.add_argument("--timeout-seconds", type=positive_timeout_seconds, default=30)
    provider_preflight.set_defaults(
        handler=lambda namespace: write_provider_preflight(parser, namespace)
    )

    blind_package = commands.add_parser(
        "blind-package", help="separate anonymous product responses from candidate identities"
    )
    blind_package.add_argument("--report", required=True)
    blind_package.add_argument("--output", required=True)
    blind_package.add_argument("--mapping-output", required=True)
    blind_package.set_defaults(
        handler=lambda namespace: write_blind_product_package(parser, namespace)
    )

    council_plan = commands.add_parser(
        "council-plan", help="assign blinded product proposals to independent reviewers"
    )
    council_plan.add_argument("--plan", required=True)
    council_plan.add_argument("--blind-package", required=True)
    council_plan.add_argument("--mapping", required=True)
    council_plan.add_argument("--output", required=True)
    council_plan.set_defaults(
        handler=lambda namespace: write_product_council_plan(parser, namespace)
    )
    add_project_commands(commands)
    from reviewctl.receipt_store_cli import add_receipt_store_commands

    add_receipt_store_commands(commands)
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except RuntimeError as error:
        print(f"reviewctl: {error}", file=sys.stderr)
        return 1
    except ReviewctlError as error:
        diagnostic = Diagnostic("config_invalid", str(error))
        print(f"reviewctl: {diagnostic.code}: {diagnostic.message}", file=sys.stderr)
        return exit_code_for(diagnostic.code)


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run_cli(argv))
