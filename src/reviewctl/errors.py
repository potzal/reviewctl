"""Stable diagnostics shared by the Python API and CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ReviewctlError(Exception):
    """Base exception for user-actionable reviewctl failures."""


class ConfigError(ReviewctlError, ValueError):
    """The project or user configuration is invalid."""


class JournalOperationError(ReviewctlError, ValueError):
    """A journal mutation was rejected with a stable diagnostic."""

    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


ERROR_EXIT_CODES = {
    "invalid_request": 2,
    "config_invalid": 2,
    "route_invalid": 2,
    "privacy_denied": 4,
    "transport_unavailable": 3,
    "timeout": 3,
    "empty_response": 3,
    "contract_failed": 3,
    "receipt_invalid": 5,
    "journal_corrupt": 5,
    "journal_unavailable": 3,
    "github_checkout_stale": 2,
    "github_command_failed": 3,
    "github_metadata_invalid": 2,
    "github_path_invalid": 2,
    "github_source_not_utf8": 2,
    "github_source_binary": 2,
    "github_source_unsupported": 2,
    "github_source_identity_changed": 3,
    "github_source_timeout": 3,
    "github_source_too_large": 2,
    "github_visibility_unknown": 4,
    "github_publication_plan_invalid": 2,
    "github_publication_stale_head": 2,
    "github_publication_stale_head_race": 3,
    "github_publication_failed": 3,
    "github_publication_timeout": 3,
    "github_publication_response_invalid": 3,
    "publication_reconciliation_incomplete": 3,
    "evidence_store_denied": 4,
    "evidence_store_unavailable": 3,
    "evidence_store_missing": 3,
    "evidence_store_failed": 3,
}


@dataclass(frozen=True)
class Diagnostic:
    """Safe, machine-readable explanation of one failed or incomplete operation."""

    code: str
    message: str
    retryable: bool = False
    next: str | None = None
    artifacts: tuple[Path, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "next": self.next,
            "artifacts": [str(path) for path in self.artifacts],
        }


def exit_code_for(code: str) -> int:
    """Map a stable diagnostic code to a stable CLI exit status."""
    return ERROR_EXIT_CODES.get(code, 1)
