# Potzal Receipt Storage Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for the coupled adapter/CLI slice; request independent spec and quality reviews before integration.

**Goal:** Store canonical receipts as optional immutable artifacts and recover their exact bytes in another project checkout.

**Architecture:** One bounded native-object adapter packages and verifies receipts. Project CLI commands use explicit project configuration and append storage observations to the existing journal. Potzal's server code is reused unchanged.

**Tech Stack:** Python standard library, pytest, Potzal native HTTP object API.

## Task 1 — Adapter and project settings

Files: create `src/reviewctl/receipt_store.py`, `tests/test_receipt_store.py`; extend `src/reviewctl/config.py` and config tests.

- [x] Add tests before implementation for opt-in configuration and invalid endpoints/credentials/project IDs.
- [x] Implement `PotzalSettings` plus optional `ReviewConfig.potzal` with strict endpoint, namespace, token-environment and timeout validation.
- [x] Add canonical byte verifier shared by `reviewctl verify` and the artifact adapter, preserving V1/V2 behavior and checkpoint rejection.
- [x] Implement deterministic JSON envelope, native POST/GET, content digests, bounded reads, no redirects, typed safe errors and byte-confirmed conflict reconciliation.
- [x] Run focused and full tests and retain red/green results in `docs/evidence/2026-10-02-potzal-receipt-storage.md`.

## Task 2 — CLI, local persistence and journal

Files: create `src/reviewctl/receipt_store_cli.py`, `tests/test_receipt_store_cli.py`; extend `src/reviewctl/cli.py`, `src/reviewctl/errors.py`.

- [x] Add command tests covering two checkouts, repeated upload/download, invalid project mapping, local file safety and store failures.
- [x] Register `receipts push` and `receipts pull`; validate project configuration and receipts before network use, with explicit token-env credentials.
- [x] Write downloaded receipts privately below `.reviewctl/shared/<digest>/receipt.json` and append `receipt_stored`/`receipt_retrieved` observations with digest and namespace.
- [x] Return diagnostics with stable exit codes and actionable recovery; leave review-result and journal semantics unchanged.
- [x] Run focused CLI tests and the full deterministic selector, including journal and front-door tests.

## Task 3 — Proof and handoff

Files: `docs/POTZAL-INTEGRATION.md`, `docs/ARCHITECTURE.md`, `docs/HANDOFF.md`, README and agent CLI guidance; bounded real-daemon canary.

- [x] Document exact configuration, commands, verifier guarantees, service-scoped permissions, retry outcomes and future federation boundary.
- [x] Run a real local Potzal daemon with synthetic credentials and two temporary projects; verify same receipt bytes, retry and denied-token behavior. Record source SHAs and results without credentials.
- [x] Run CI selector unchanged: `uv run pytest -m 'unit or contract' --cov=reviewctl --cov-branch --cov-report=term-missing`; preserve 100% threshold. Run Ruff check/format and `uv build`.
- [x] Request independent spec review, then quality review; fix reproduced findings and rerun affected checks.
- [x] Commit the finite integration and prepare a reviewable PR: [#31](https://github.com/potzal/reviewctl/pull/31). Live Amelia use and server project isolation are separate pending gates.

## Review-driven closure

- [x] Reproduce and fix project/state replacement before download persistence.
- [x] Reproduce and fix output/file replacement after opening but before reporting success.
- [x] Cancel slow active HTTP/TLS I/O under one monotonic budget, including conflict GET; document the synchronous DNS exception.
- [x] Make deep JSON regressions independent of native decoder stack assumptions, remove the machine-specific public-doc path, and verify the focused suite on macOS/Linux.
- [x] Repeat the full deterministic selector at the 100% line/branch gate, lint, formatting, package build and real local daemon canary.
- [ ] Confirm remote CI at the final PR head; integration into main is not implied by this plan's local verification.
