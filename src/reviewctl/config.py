"""Project-first TOML configuration for reviewctl."""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from reviewctl.dimensions import normalize_dimensions
from reviewctl.errors import ConfigError
from reviewctl.filesystem import (
    confined_directory_descriptor,
    confined_relative_regular_descriptor,
    read_confined_bytes,
)

SUPPORTED_TRANSPORTS = frozenset({"llm", "codex", "openrouter", "agy", "gemini", "kiro", "pi"})
PRIVACY_RANK = {"personal": 0, "private": 1, "sensitive": 2}
VISIBILITIES = frozenset({"public", "private", "unknown"})
EXECUTION_MODES = frozenset({"local", "remote"})
TOOL_MODES = frozenset({"none", "read-only"})
THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})
DEFAULT_USER_CONFIG = Path("~/.config/reviewctl/config.toml")
PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class PotzalSettings:
    """Explicit private evidence destination; credentials remain in the environment."""

    endpoint: str
    namespace: str
    token_env: str
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        try:
            endpoint = urlsplit(self.endpoint)
            port = endpoint.port
        except (ValueError, TypeError, AttributeError) as error:
            raise ConfigError("evidence.potzal.endpoint must be an HTTP(S) origin") from error
        if (
            not isinstance(self.endpoint, str)
            or any(character.isspace() or ord(character) < 32 for character in self.endpoint)
            or endpoint.scheme not in {"https", "http"}
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
            or port == 0
            or (endpoint.scheme == "http" and endpoint.hostname not in {"127.0.0.1", "::1"})
        ):
            raise ConfigError(
                "evidence.potzal.endpoint requires HTTPS or literal loopback HTTP, "
                "without credentials, path, query, or fragment"
            )
        object.__setattr__(self, "endpoint", self.endpoint.rstrip("/"))
        if not isinstance(self.namespace, str) or not re.fullmatch(
            r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*",
            self.namespace,
        ):
            raise ConfigError("evidence.potzal.namespace must be a lowercase Potzal namespace")
        if not isinstance(self.token_env, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.token_env
        ):
            raise ConfigError("evidence.potzal.token_env must name an environment variable")
        if self.timeout_seconds is None:
            raise ConfigError("evidence.potzal.timeout_seconds must be a positive integer")
        _positive_int(self.timeout_seconds, "evidence.potzal.timeout_seconds", 30, maximum=300)


@dataclass(frozen=True)
class Route:
    transport: str
    model: str


@dataclass(frozen=True)
class ProjectSettings:
    name: str
    visibility: str
    privacy_mode: str
    project_id: str
    portable_project_id: bool
    required_dimensions: tuple[str, ...]


@dataclass(frozen=True)
class ReviewProfile:
    name: str
    routes: tuple[str, ...]
    response_contract: str
    timeout_seconds: int
    max_attempts: int
    max_output_tokens: int | None
    execution: str
    tools: str
    thinking: str
    dimensions: tuple[str, ...]

    @property
    def parsed_routes(self) -> tuple[Route, ...]:
        return tuple(parse_route(route) for route in self.routes)


@dataclass(frozen=True)
class ReviewConfig:
    project: ProjectSettings
    profiles: Mapping[str, ReviewProfile]
    path: Path | None
    digest: str
    potzal: PotzalSettings | None = None

    def profile(self, name: str) -> ReviewProfile:
        try:
            return self.profiles[name]
        except KeyError as error:
            raise ConfigError(f"profile {name!r} is not configured") from error


def parse_route(value: str) -> Route:
    """Parse one explicit transport:model route."""
    if not isinstance(value, str):
        raise ConfigError("route must be a string")
    transport, separator, model = value.partition(":")
    if not separator or not transport or transport not in SUPPORTED_TRANSPORTS or not model.strip():
        raise ConfigError(
            "route must use transport:model with a known transport and non-empty model"
        )
    model = model.strip()
    if transport == "pi":
        provider, provider_separator, provider_model = model.partition("/")
        if not provider_separator or not provider or not provider_model:
            raise ConfigError("Pi route model must use provider/model")
    return Route(transport=transport, model=model)


def _config_path(path: Path) -> Path:
    candidate = Path(os.path.abspath(path.expanduser()))
    try:
        with confined_directory_descriptor(candidate):
            return candidate / "reviewctl.toml"
    except OSError:
        return candidate


def _descriptor_bytes(descriptor: int) -> bytes:
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        return stream.read()


def _read(
    path: Path | None,
    *,
    expected_directory_identity: tuple[int, int] | None = None,
) -> tuple[dict[str, Any], bytes, Path | None]:
    if path is None:
        return {}, b"", None
    candidate = Path(os.path.abspath(path.expanduser()))
    try:
        with confined_directory_descriptor(
            candidate, expected_identity=expected_directory_identity
        ) as directory_descriptor:
            resolved = candidate / "reviewctl.toml"
            try:
                with confined_relative_regular_descriptor(
                    directory_descriptor, Path("reviewctl.toml"), os.O_RDONLY
                ) as descriptor:
                    raw = _descriptor_bytes(descriptor)
            except FileNotFoundError:
                return {}, b"", None
            except OSError as error:
                raise ConfigError(
                    f"could not read configuration {resolved}; it must be a regular file: {error}"
                ) from error
    except ConfigError:
        raise
    except OSError as error:
        if expected_directory_identity is not None:
            raise ConfigError(f"project directory identity changed: {candidate}") from error
        resolved = candidate
        try:
            raw = read_confined_bytes(resolved)
        except FileNotFoundError:
            return {}, b"", None
        except OSError as error:
            raise ConfigError(
                f"could not read configuration {resolved}; it must be a regular file: {error}"
            ) from error
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"could not read configuration {resolved}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigError(f"configuration {resolved} must contain a TOML table")
    return value, raw, resolved


def _merge_tables(user: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    merged = dict(user)
    for key, value in project.items():
        if key == "profiles" and isinstance(value, dict):
            profiles = dict(merged.get("profiles", {}))
            for profile_name, profile_value in value.items():
                if not isinstance(profile_value, dict):
                    raise ConfigError(f"profiles.{profile_name} must be a TOML table")
                base = profiles.get(profile_name, {})
                if not isinstance(base, dict):
                    base = {}
                profiles[profile_name] = {**base, **profile_value}
            merged["profiles"] = profiles
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _string(value: object, name: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _positive_int(value: object, name: str, default: int, maximum: int | None = None) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be at most {maximum}")
    return value


def _project_identity(
    value: object, *, project_path: Path, resolved_project: Path | None
) -> tuple[str, bool]:
    if value is not None:
        if (
            not isinstance(value, str)
            or not value.strip()
            or not PROJECT_ID.fullmatch(value.strip())
        ):
            raise ConfigError(
                "project.id must contain only letters, numbers, dot, dash, or underscore"
            )
        return value.strip(), True
    config_path = resolved_project or _config_path(project_path)
    digest = hashlib.sha256(str(config_path.parent).encode("utf-8")).hexdigest()[:24]
    return f"project-{digest}", False


def _profile(
    name: str,
    value: object,
    privacy_mode: str,
    required_dimensions: tuple[str, ...],
) -> ReviewProfile:
    if not isinstance(value, dict):
        raise ConfigError(f"profiles.{name} must be a TOML table")
    raw_routes = value.get("routes", [])
    if not isinstance(raw_routes, list) or not all(isinstance(route, str) for route in raw_routes):
        raise ConfigError(f"profiles.{name}.routes must be an array of strings")
    routes = tuple(route.strip() for route in raw_routes)
    parsed_routes = tuple(parse_route(route) for route in routes)
    response_contract = _string(
        value.get("response_contract"),
        f"profiles.{name}.response_contract",
        "findings-json",
    )
    timeout_seconds = _positive_int(
        value.get("timeout_seconds"), f"profiles.{name}.timeout_seconds", 300
    )
    max_attempts = _positive_int(
        value.get("max_attempts"), f"profiles.{name}.max_attempts", 1, maximum=3
    )
    max_output_tokens = value.get("max_output_tokens", 8000)
    if max_output_tokens is not None:
        max_output_tokens = _positive_int(
            max_output_tokens, f"profiles.{name}.max_output_tokens", 8000
        )
    execution = _string(value.get("execution"), f"profiles.{name}.execution", "remote")
    if execution not in EXECUTION_MODES:
        raise ConfigError(f"profiles.{name}.execution must be local or remote")
    tools = _string(value.get("tools"), f"profiles.{name}.tools", "none")
    if tools not in TOOL_MODES:
        raise ConfigError(f"profiles.{name}.tools must be none or read-only")
    thinking = _string(value.get("thinking"), f"profiles.{name}.thinking", "minimal")
    if thinking not in THINKING_LEVELS:
        raise ConfigError(
            f"profiles.{name}.thinking must be one of {', '.join(sorted(THINKING_LEVELS))}"
        )
    if execution == "local" and any(
        route.transport in {"openrouter", "pi"} or route.model.startswith("openrouter/")
        for route in parsed_routes
    ):
        raise ConfigError(f"local profile {name!r} cannot use a remote provider-backed route")
    try:
        dimensions = normalize_dimensions(
            value.get("dimensions"),
            label=f"profiles.{name}.dimensions",
            default=required_dimensions,
        )
    except ValueError as error:
        raise ConfigError(str(error)) from error
    return ReviewProfile(
        name=name,
        routes=routes,
        response_contract=response_contract,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        max_output_tokens=max_output_tokens,
        execution=execution,
        tools=tools,
        thinking=thinking,
        dimensions=dimensions,
    )


def load_config(
    project_path: Path,
    *,
    user_path: Path | None = DEFAULT_USER_CONFIG,
    expected_project_identity: tuple[int, int] | None = None,
) -> ReviewConfig:
    """Load user defaults and project settings with a monotonic privacy floor."""
    project, project_raw, resolved_project = _read(
        project_path, expected_directory_identity=expected_project_identity
    )
    user, user_raw, resolved_user = _read(user_path)
    merged = _merge_tables(user, project)
    project_table = merged.get("project", {})
    if not isinstance(project_table, dict):
        raise ConfigError("project must be a TOML table")
    project_privacy = _string(project_table.get("privacy_mode"), "project.privacy_mode", "private")
    if project_privacy not in PRIVACY_RANK:
        raise ConfigError("project.privacy_mode must be personal, private, or sensitive")
    user_table = user.get("project", {})
    user_privacy = user_table.get("privacy_mode") if isinstance(user_table, dict) else None
    if user_privacy is not None:
        user_privacy = _string(user_privacy, "project.privacy_mode", "private")
        if user_privacy not in PRIVACY_RANK:
            raise ConfigError("project.privacy_mode must be personal, private, or sensitive")
        if PRIVACY_RANK[user_privacy] > PRIVACY_RANK[project_privacy]:
            project_privacy = user_privacy
    visibility = _string(project_table.get("visibility"), "project.visibility", "private")
    if visibility not in VISIBILITIES:
        raise ConfigError("project.visibility must be public, private, or unknown")
    project_name = _string(
        project_table.get("name"),
        "project.name",
        (resolved_project.parent.name if resolved_project else "review-project"),
    )
    project_id, portable_project_id = _project_identity(
        project_table.get("id"), project_path=project_path, resolved_project=resolved_project
    )
    try:
        required_dimensions = normalize_dimensions(
            project_table.get("required_dimensions"),
            label="project.required_dimensions",
            default=("correctness",),
        )
    except ValueError as error:
        raise ConfigError(str(error)) from error
    profiles_table = merged.get("profiles", {})
    if not isinstance(profiles_table, dict):
        raise ConfigError("profiles must be a TOML table")
    profiles = {
        name: _profile(name, profile, project_privacy, required_dimensions)
        for name, profile in profiles_table.items()
    }
    if project_privacy == "sensitive":
        for name, profile in profiles.items():
            if profile.execution != "local":
                raise ConfigError(f"sensitive project cannot use remote profile {name!r}")
    if not profiles:
        profiles["default"] = ReviewProfile(
            name="default",
            routes=(),
            response_contract="findings-json",
            timeout_seconds=300,
            max_attempts=1,
            max_output_tokens=8000,
            execution="local",
            tools="none",
            thinking="minimal",
            dimensions=required_dimensions,
        )
    raw_digest = hashlib.sha256(project_raw + b"\n" + user_raw).hexdigest()
    evidence = merged.get("evidence", {})
    if not isinstance(evidence, dict):
        raise ConfigError("evidence must be a TOML table")
    potzal = None
    if "potzal" in evidence:
        if not portable_project_id:
            raise ConfigError("evidence.potzal requires an explicit stable project.id")
        settings = evidence["potzal"]
        if not isinstance(settings, dict):
            raise ConfigError("evidence.potzal must be a TOML table")
        if set(settings) - {"endpoint", "namespace", "token_env", "timeout_seconds"}:
            raise ConfigError("evidence.potzal contains unknown settings")
        try:
            potzal = PotzalSettings(**settings)
        except TypeError as error:
            raise ConfigError(
                "evidence.potzal requires endpoint, namespace and token_env"
            ) from error
    return ReviewConfig(
        project=ProjectSettings(
            project_name,
            visibility,
            project_privacy,
            project_id,
            portable_project_id,
            required_dimensions,
        ),
        profiles=profiles,
        path=resolved_project or resolved_user,
        digest=raw_digest,
        potzal=potzal,
    )
