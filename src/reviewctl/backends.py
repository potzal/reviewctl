"""Provider-neutral contracts for local review backend execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from reviewctl.contracts import PreparedContract


class BackendFamily(StrEnum):
    AGENT_CLI = "agent-cli"
    PROVIDER_GATEWAY = "provider-gateway"
    GENERIC_MODEL_CLI = "generic-model-cli"
    AGENT_PROTOCOL = "agent-protocol"


class DiscoveryKind(StrEnum):
    EXECUTABLE = "executable"
    REMOTE_API = "remote-api"


class ReadOnlyCapability(StrEnum):
    ENFORCED = "enforced"
    SANDBOXED = "sandboxed"
    ADVISORY = "advisory"
    UNSUPPORTED = "unsupported"


class SourceIsolation(StrEnum):
    BACKEND_ENFORCED = "backend-enforced"
    EXTERNAL_SANDBOX = "external-sandbox"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class BackendCapabilities:
    review_read_only: ReadOnlyCapability
    editable_execution: bool
    structured_output: bool
    resolved_model_identity: bool
    resolved_provider_identity: bool
    conversation_identity: bool
    usage_reporting: bool
    timeout_control: bool
    tool_control: bool
    source_isolation: SourceIsolation

    @property
    def output_token_limit_enforced(self) -> bool:
        """Whether the backend enforces the requested output-token ceiling.

        This remains a runtime capability until the descriptor schema can grow
        without breaking consumers that compare its serialized shape exactly.
        """
        return False


@dataclass(frozen=True)
class BackendDescriptor:
    name: str
    family: BackendFamily
    discovery_kind: DiscoveryKind
    executable_env: str
    default_executable: str
    capabilities: BackendCapabilities
    qualification: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PersistedResponse:
    conversation_id: str
    cost_usd: float | None
    duration_ms: int | None
    input_tokens: int | None
    model: str
    output_tokens: int | None
    provider: str | None
    response: str


@dataclass(frozen=True)
class BackendEvidence:
    database: Path | None = None
    request: Path | None = None
    response: Path | None = None
    session: Path | None = None
    final_response: Path | None = None
    stderr: Path | None = None


@dataclass(frozen=True)
class BackendRequest:
    prompt: str
    model: str
    response_contract: str
    files: tuple[Path, ...]
    attempt_dir: Path
    timeout_seconds: int
    max_output_tokens: int
    source_class: str
    source_roots: tuple[Path, ...]
    provider_preferences: dict[str, object] | None
    evidence_parent_identity: tuple[int, int] | None = None
    cwd: Path | None = None
    pi_bin: str | None = None
    tools: str = "none"
    thinking: str = "minimal"
    reasoning_effort: str | None = None
    prepared_contract: PreparedContract | None = None


@dataclass(frozen=True)
class BackendExecution:
    exit_code: int
    diagnostic: str
    response: PersistedResponse | None
    evidence: BackendEvidence


BackendExecutor = Callable[[BackendRequest], BackendExecution]


@dataclass(frozen=True)
class RegisteredBackend:
    descriptor: BackendDescriptor
    execute: BackendExecutor


class BackendRegistry:
    def __init__(self) -> None:
        self._backends: dict[str, RegisteredBackend] = {}

    def register(self, descriptor: BackendDescriptor, execute: BackendExecutor) -> None:
        if descriptor.name in self._backends:
            raise ValueError(f"backend {descriptor.name!r} is already registered")
        self._backends[descriptor.name] = RegisteredBackend(descriptor, execute)

    def require(self, name: str) -> RegisteredBackend:
        try:
            return self._backends[name]
        except KeyError as error:
            raise KeyError(f"unknown backend {name!r}") from error

    def descriptors(self) -> tuple[BackendDescriptor, ...]:
        return tuple(self._backends[name].descriptor for name in sorted(self._backends))
