"""Deterministic test-layer classification for reviewctl's local suite."""

from __future__ import annotations

from pathlib import Path

import pytest

_CONTRACT_MODULES = {
    "test_backends.py",
    "test_cli_front_door.py",
    "test_codex_project_transport.py",
    "test_github_cli.py",
    "test_github_contracts.py",
    "test_github_publisher.py",
    "test_github_source.py",
    "test_muse_transport.py",
    "test_pi_transport.py",
    "test_range_review.py",
    "test_review_flow.py",
    "test_run.py",
    "test_setup.py",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Classify every existing deterministic test without weakening selection."""
    for item in items:
        if any(marker in item.keywords for marker in ("unit", "contract", "platform", "live")):
            continue
        module = Path(str(item.path)).name
        marker = pytest.mark.contract if module in _CONTRACT_MODULES else pytest.mark.unit
        item.add_marker(marker)
