# Local work and pull-request reconciliation — 2026-10-02

Baseline: main `2d416af3298dfcf4d6ebb98dd39d742ef3264fd6` after
[PR #31](https://github.com/potzal/reviewctl/pull/31).
This is an integration audit, not a new product or governance decision.

## Disposition

| Work | Disposition | Evidence and next action |
| --- | --- | --- |
| [PR #12](https://github.com/potzal/reviewctl/pull/12), `3fbd324` | Partially resolved; retain open | Main includes thinking configuration, CLI normalization, API forwarding and Pi request evidence. Its product document and real high-thinking Pi canary obligation remain unresolved; do not merge the old tree or call capture-limit fixtures a real canary. |
| [PR #19](https://github.com/potzal/reviewctl/pull/19), `3ab53c2` | Retain open | Completed exact-head review and disposition of every comment are useful. Mandatory GitHub Codex versus a provider-neutral eligible reviewer remains an owner decision; no replacement gate or waiver was adopted. Its earlier unavailable-gate finding was corrected in the PR, not silently ignored. |
| Local GitHub hardening, `f351174` | Preserve and extract a bounded slice | Source, publisher, promotion and CLI fixes remain unique versus baseline. Retain single-attempt promotion. Do not adopt the branch's retry reconstruction, `github inspect`, decision-model documents or broader product language in this slice. |
| Local governance work on the #12 checkout | Archive privately; not adopted | Four standards blockers and two spec gaps were reported by independent read-only reviewers. Preserve source, tests and documentation for redesign rather than merge or discard them. |
| Local evidence/receipt documentation | Duplicate or stale; archive | The main checkpoint/canonical distinction is already present. The old Pi verification example points at the wrong receipt filename and omits later main diagnostic documentation. Do not overwrite main with it. |
| Local Kiro identity research | Historical research; preserve privately | Dated vendor/protocol observations are not current runtime qualification. No code or identity policy changed on that basis. |
| SDD-lite change-packet proposal, `0d997f1` | Preserve, unadopted | Optional planning/receipt binding is a separate product decision, not a dependency of this integration. |
| Older Kiro capture branch, `f5bfede` | Retain historical source | Main already has bounded pipe capture and subsequent read-error, interrupted-capture and normal-drain fixes. Replacing it with the older branch would lose those regressions. |
| Merged remote branches for #5/#8/#9/#24 | Remove remote heads recoverably | Exact heads and merged commits were checked against GitHub; local work and evidence were saved before removal. No open PR was closed. |

[Issues #3/#16](../AGENTIC-CONTROL-PLANE-DIRECTION.md#open-work-disposition-updated-at-main-3b943ef-2026-09-27)
remain resolved through #30. Issue #4 remains open: exploration artifacts
must be explicitly non-formal and cannot satisfy a receipt gate.
The #29 work-layer direction remains an unadopted proposal. Potzal receipt
storage is optional; federation, signed exchange and a software factory are
not introduced here.

## Standards review

The archived governance work has four reported blockers:

1. Pi constructed transmitted text and hashed a separate reread, so the
   observation could describe different bytes.
2. Mandatory payload observations would reject the existing OpenRouter
   adapter, which does not emit that new field.
3. Gate diagnostics derived contract validity from checksum validity without
   evaluating the contract.
4. Malformed recovery-manifest field types could escape as `TypeError` instead
   of quarantine diagnostics.

These are findings about the unintegrated candidate, not defects claimed to
exist on main. Durability/binding ideas may be extracted later with their own
tests; no new sealing/governance format is adopted.

## Spec review

Two candidate-contract gaps were reproduced by the independent reviewer:
the attestation matcher did not bind an expected verification-record digest,
and qualification did not bind the model version carried by the lane and
policy. Neither probe demonstrated positive CLI merge eligibility.
The extra trust-anchor, signed-attestation, recovery and qualification system
also exceeds the committed #12 implementation and has no adoption decision.

## Bounded GitHub rescue and verification method

- Reproduce failures on baseline by applying regression tests first.
- Recheck visibility while freezing source and refuse an empty diff.
- Recheck base/head/visibility after review; preserve the historical receipt
  but withhold an executable plan if identity changed or lookup failed.
- Bind the publisher to snapshot base and visibility, including no-finding
  and duplicate-only paths. Preserve IDs and classify an identity change
  after a POST as a race; do not retry it automatically.
- Reject a known-private GitHub repository classified as public/personal
  before model invocation; allow explicitly configured private remote routes
  under existing policy, not a blanket remote ban.
- Bind checkpoint configuration, project, origin, privacy and findings to
  the controller result; multi-attempt canonical promotion stays closed.
- Run unchanged deterministic CI selection with 100% statement/branch
  coverage, lint, formatting and build, followed by exact-head CI and an
  independent advisory source review. Actual outcomes belong in the PR and
  release evidence, not predictions in this audit.

No real provider call, live GitHub publication, formal receipt, Amelia
deployment or release publication is claimed by these offline reviews.
Distributed concurrent publishers still require external serialization;
marker reconciliation alone is not an atomic deduplication guarantee.

## Recovery boundary

Before cleanup, committed refs were saved in a verified private Git bundle;
tracked WIP patches and untracked evidence were archived separately. Parked
WIP is also retained in named local recovery refs, so mutable stash positions
are not the only recovery handle. These private archives are not release
assets and must not be published with source or receipts.
