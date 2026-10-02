# reviewctl — Project Handoff

**Date:** 2026-08-23
**Repository:** local `reviewctl` checkout
**Purpose:** point-in-time operational handoff and roadmap

**2026-09-27 addendum:** this handoff's phase labels and evidence below are a
historical snapshot, not proof of today's implementation or CI state. The
[agentic work direction proposal](AGENTIC-CONTROL-PLANE-DIRECTION.md) contains
a separate, unadopted staged roadmap and a reconciliation of PRs #12/#19 and
issues #3/#4/#16. Core fixes for #3/#16 merged in #30 and those issues are
closed; #4 and the #12/#19 decisions remain open. The proposal recommends
preserving the existing review kernel while testing a sibling coordination
layer only after the core gates are stable and the product boundary is agreed.
Do not treat that proposal as completion of any phase below or as authority to
merge or change the current review gate.

## 2026-08-23 product iteration

**2026-10-02 development addendum:** the bounded
[Potzal integration](POTZAL-INTEGRATION.md) adds opt-in storage/retrieval of
canonical receipts and local journal observations on the feature branch.
Potzal's native object API is reused unchanged. It currently gives service-wide
credential authority, not project isolation, and raw reads do not enforce
release revocation. Signed exchange and journal federation below remain future
work. Integration into main, exact-commit review and live Amelia use require
their own evidence; local development does not establish those states.

The local product loop is now implemented on `main` as a project-first API and
CLI layer, without a BAML dependency and without a Potzal dependency:

```text
reviewctl init
    -> reviewctl doctor
    -> reviewctl review
    -> .reviewctl/journal.jsonl + receipt.json
    -> reviewctl status / findings / verify
```

The new `ReviewClient` owns configuration, explicit file boundaries, typed
contract preparation, ordered Pi routes, bounded per-route retries, partial
finding retention, private artifacts, append-only journal facts, and receipt
checksums. Pi remains a transport: credentials, provider access, and the raw
JSON event stream stay in Pi-owned execution and private local artifacts.

The first route is intentionally Pi-backed and can use the configured
OpenRouter models; `privacy_mode = "private"` permits an explicit remote
route, while `sensitive` rejects project-defined remote profiles and local
profiles cannot name an OpenRouter route. A single Markdown JSON fence is
normalized for interoperability, while the raw Pi stream remains retained.

Validated findings from an incomplete response are passed as bounded context
to the next retry/route and remain visible in the final result. Every attempt
and fallback transition is recorded. `reviewctl verify` checks the local
receipt checksum and detects accidental corruption; it is not a signature or
shared trust root. Signed exchange and federation remain future work.

The project journal now projects one current finding per stable `findingId`.
Repeated observations increase the observation count without resetting the
finding lifecycle. `reviewctl findings set-status` appends explicit
`finding_status_changed` events for `open`, `disputed`, `fixed`, `verified`,
and `dismissed`; the journal remains the canonical record and the projection
can be rebuilt from scratch.

This iteration adds the local journal envelope. `reviewctl init` writes an
explicit portable `project.id` and a private machine-local origin identity.
New events carry schema version, project/origin IDs, contiguous sequence,
previous-event digest, and canonical event digest. Existing JSONL events remain
readable as a legacy prefix, and `reviewctl journal verify` checks continuity
without rewriting bytes. Receipts and packet metadata carry the project/origin
identities and journal sequence.

Review dimensions are now bounded, sorted metadata. Project/profile settings
and `review --dimension NAME` contribute to one canonical set; receipts record
`dimensionSchemaVersion`, requested dimensions, and unresolved coverage rather
than claiming a model satisfied them. `findings --dimension NAME` and
`status --dimension NAME` query the rebuildable local projection. Custom names
must use `custom.<slug>` and are length/count limited.

Evidence from this iteration:

- full suite: `uv run pytest -q` — 100% pass;
- lint and whitespace: `uv run ruff check src tests`, `git diff --check` — pass;
- real Pi/Ox-alpha synthetic canary — accepted receipt, observed model/provider,
  private `0600` artifacts, offline verification passed;
- external Pi reviews: Ox-alpha identified and drove fixes for review-id
  traversal, local/remote route enforcement, timeout process cleanup, and
  malformed finding handling; Muse independently reviewed the post-fix shape.

The journal-envelope and dimension work is committed as `f63ced9`, `784e2f1`,
and `8f717f8`. Muse found three concrete envelope defects in the first pass:
identity-creation races, configured identity override, and unbounded source
reads. They were reproduced with regression tests and fixed before Muse's
post-fix receipt approved the bounded slice. The dimensions review was also
approved by Muse. Ox-alpha and Qwen 3.8 were invoked through `reviewctl` for
both rounds; their receipts verify structurally but record `timeout`/
`unavailable`, so they are availability evidence rather than approvals.

The relevant private evidence records are named:

- `reviewctl-envelope-implementation/envelope-{ox,muse,qwen}`;
- `reviewctl-envelope-postfix/muse`;
- `reviewctl-dimensions-implementation/dimensions-{ox,muse,qwen}`.

The next product roadmap is [GitHub/Pi review roadmap](superpowers/specs/2026-08-24-github-pi-review-roadmap.md).
Its Phase 0–1 implementation is now the local-first `reviewctl github review`
dry-run path: provider-neutral snapshot/plan contracts, `gh` metadata plus
exact local commit materialization, typed PR context in packet/receipt/journal,
and reuse of the existing Pi-backed `ReviewClient`. The comment-only publisher
is now also implemented behind explicit `--publish --publish-event comment`,
with marker reconciliation, bounded pagination, stale-head checks, and
append-only publication events. The two implementation plans are
`superpowers/plans/2026-08-24-github-review-phase-0-1.md` and
`superpowers/plans/2026-08-24-github-comment-publisher.md`. Approval and
request-changes remain unsupported. Ox-alpha first reported four roadmap gaps
and its post-fix receipt approved the revised document.

The Phase 2 Ox-alpha implementation review was attempted through the formal
Pi transport twice (broad and narrow publisher scopes). Both receipts verify
structurally but ended as `timeout` with no response, so they are availability
evidence rather than approvals. The local publisher suite and API-contract
checks are the current acceptance evidence; a real GitHub canary still needs
an explicit disposable repository/PR target.

The two external review routes were advisory. The source, tests, runtime
canary, and verified receipt are the acceptance evidence.

## Current state

`reviewctl` is the local, evidence-backed control plane for bounded LLM
reviews. It owns the typed review contracts, frozen-input provenance, backend
invocation, acceptance gates, fallback/consolidation semantics, receipts, and
offline receipt verification.

The project has adopted the useful concepts behind BAML natively: typed
request/response boundaries, explicit contract preparation, exact decoding,
semantic validation, and transport-independent orchestration. It does not
depend on BAML or its runtime.

The current local backend set includes Codex, Gemini CLI, Antigravity, Pi,
Kiro, `llm`, and OpenRouter. Registration and availability are not
qualification. Kiro remains an advisory, unqualified backend and is not
eligible for a merge gate.

## Earlier backend closure: Kiro

The immediate blocker was Kiro stopping in a non-interactive review with:

```text
Tool approval required but --no-interactive was specified.
```

The native Kiro adapter now invokes:

```text
kiro-cli chat --no-interactive --trust-tools= ...
```

The frozen review packet is already sent inline by `reviewctl`, so the formal
adapter needs no Kiro file-reading or editing tools. `--trust-all-tools` is not
an acceptable workaround because it broadens the execution boundary.

The regression test observes the fake executable's actual argument vector.

## Verification evidence

- Focused Kiro tests pass.
- `uv run ruff check src/reviewctl/cli.py tests/test_run.py` passes.
- Full test suite `uv run pytest -q` passes.
- Real local Kiro smoke `kiro-file-smoke` with `claude-opus-5` produced an
  accepted receipt and `reviewctl verify` returned `valid: true`.
- Formal bounded review `kiro-approval-production-review` was approved and its
  receipt verified successfully.

Kiro still emits a non-blocking warning about its `--trust-tools` parser. It
did not prevent execution, acceptance, or receipt verification.

## Architectural boundaries

Keep these ownership rules stable:

| Concern | Owner |
|---|---|
| Review contracts, acceptance, fallback, consolidation, receipts | `reviewctl` |
| Model/provider qualification and operating roster | Organization evidence store |
| Project review journal and finding lifecycle | `reviewctl` append-only journal and projection |
| Portable operation identity and federation commitments | Cljedger, if integrated later |
| Storage or distribution of opaque signed bundles | Optional Potzal or another transport |
| Editable changes | Separate change-attempt backend, never a review receipt |

The canonical journal is append-only and readable for replay and verification;
it is not a write-only database. Projections may be rebuilt or discarded.

Cljedger and signed Potzal exchange remain future integrations. The optional
receipt byte adapter adds local storage observations and remains removable;
`reviewctl` stays usable locally without either dependency. A future Cljedger
integration should anchor a `ReviewReceipt` without making Cljedger's commitment
model the authority for review acceptance.

## Roadmap

### Phase 0 — Stabilize the local review loop: complete

- Native typed contract boundary without a BAML dependency.
- Backend registry and local setup discovery.
- Best-match route selection with bounded fallback.
- Preservation and consolidation of valid fragments from incomplete reviews.
- Receipt schema v2 and offline structural verification.
- Local policy gates for proprietary source.
- Kiro native transport with isolated disposable execution, no trusted tools,
  runtime model discovery, and explicit unresolved-identity semantics.
- A flaky one-second fake-`llm` subprocess test was made load-tolerant at five
  seconds; production timeout behavior was not changed.

### Phase 1 — Operational closure: complete

- Project-first `init`, `review`, `status`, `findings`, `doctor`, and API flow
  are implemented and covered by focused tests.
- The read-only `doctor` surface reports route, privacy, contract, executable,
  and capability prerequisites without authenticating or calling a model.
- Pi timeout, empty, fenced-JSON, malformed-contract, and fallback paths have
  bounded diagnostics and persisted artifacts.
- Stable finding identity, deduplicated projection, and append-only lifecycle
  status changes are implemented and covered by focused tests.
- Portable project/origin envelope, continuity verification, identity locking,
  read-only journal verification, and bounded dimension metadata are implemented
  and covered by focused tests.
- Keep the exact Kiro invocation and approval failure in `HELP-LLM` recovery
  guidance if the warning or failure appears again.
- Run bounded conformance fixtures for every currently used local backend:
  timeout, empty output, malformed structured output, identity behavior,
  credential redaction, and tool/working-directory boundaries.

### Phase 2 — Project review journal: current

- Add organization-owned identity assignment and explicit migration tooling for
  projects that still use a local fallback project ID.
- Persist immutable receipt references, adjudications, waivers, and
  verification observations alongside the now-implemented finding lifecycle.
- Add dimension definitions with privacy classification and compatibility rules;
  current dimensions are metadata only.
- Add projections for adjudication and verification queries while keeping the
  finding projection rebuildable.
- Add deterministic fixtures for event identity, ordering, supersession,
  replay, and dimension compatibility.

### Phase 3 — Optional exchange and federation

- Implement reviewctl-owned signed export bundles with allow-listed facts.
- Verify trust roots, key rotation/revocation, origin sequence continuity,
  idempotent import, replay protection, conflict quarantine, and cursors.
- Add filesystem export/import first.
- Add a language-neutral adapter to Cljedger only after Cljedger's persistence,
  codec, and transport layers are available.
- Treat Potzal as an optional opaque bundle store/distributor; it must not own
  reviewctl semantics.

### Phase 4 — Editable execution

- Define a separate `ChangeAttempt` contract for Pi, Codex, Cursor, Claude, or
  another qualified editor.
- Require patch/commit evidence and a fresh formal review after every change.
- Never promote an editable attempt into a review approval.
- Qualify each editable backend independently for its source boundary and
  mutation detection.

## Known limitations

- Kiro is available locally but unqualified for merge-gate evidence.
- Kiro's source isolation is currently unavailable at the OS boundary.
- Kiro currently supports only `findings-json` through the native adapter.
- Cursor and Claude Code are not currently qualified native review backends.
- Federation, automatic synchronization, signed exchange, and multi-machine
  journal merge/conflict resolution are designed but not implemented in the
  core.
- Model names, prices, credits, and qualification results belong in private
  policy/evidence, not project instruction files.

## Workspace handoff

At handoff time the checkout contains pre-existing uncommitted changes in:

- `README.md`
- `src/reviewctl/cli.py`
- `tests/test_run.py`

The Kiro/Gemini changes share two of those files. Before committing any future
work, split intended hunks carefully; do not reset or overwrite the existing
work. The finding-lifecycle iteration is committed separately from those
pending changes.

## Canonical documentation

- [Architecture](ARCHITECTURE.md)
- [Help for LLMs](HELP-LLM.md)
- [Project integration](PROJECT-INTEGRATION.md)
- [Global best-match design](superpowers/specs/2026-08-12-global-best-match-review-design.md)
- [Append-only journal ADR](adr/0001-append-only-journals.md)
- [Signed federation ADR](adr/0002-signed-federation-bundles.md)
