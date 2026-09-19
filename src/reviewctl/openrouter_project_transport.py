"""OpenRouter transport for the project-scoped review API.

Project reviews (including `reviewctl github review`) resolve a transport from
the project's profile. Pi and Codex are agent CLIs that need a sandboxed
workspace; OpenRouter is a direct remote gateway that receives the already
validated snapshots inline in the request packet, so this adapter is a thin
bridge to the canonical OpenRouter backend the `run` command already uses.

The import of the canonical backend is deferred to `execute` to avoid an import
cycle: `reviewctl.cli` imports `project_cli`, which builds these transports.
This mirrors `CodexProjectTransport`.
"""

from __future__ import annotations

from reviewctl.backends import (
    BackendCapabilities,
    BackendExecution,
    BackendRequest,
    ReadOnlyCapability,
    SourceIsolation,
)


class OpenRouterProjectTransport:
    """Run the canonical OpenRouter backend for a project-scoped review.

    OpenRouter never sees the project checkout: the review files are embedded in
    the request packet by the canonical backend, and its evidence (request and
    response) is persisted under the attempt directory. No workspace staging or
    path normalization is required — the backend returns the logical file names
    it was given.
    """

    @classmethod
    def capabilities(cls) -> BackendCapabilities:
        # Must match the descriptor registered for the "openrouter" backend in
        # reviewctl.cli so profile validation and receipts agree across the two
        # entry points (`run` and `github review`).
        return BackendCapabilities(
            review_read_only=ReadOnlyCapability.UNSUPPORTED,
            editable_execution=False,
            structured_output=True,
            resolved_model_identity=True,
            resolved_provider_identity=True,
            conversation_identity=True,
            usage_reporting=True,
            timeout_control=True,
            tool_control=False,
            source_isolation=SourceIsolation.UNAVAILABLE,
        )

    def execute(self, request: BackendRequest) -> BackendExecution:
        # Import lazily: reviewctl.cli imports project_cli, which imports this
        # transport, so importing the backend at module load would be circular.
        from reviewctl.cli import execute_openrouter_backend

        return execute_openrouter_backend(request)
