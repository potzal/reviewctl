# Agentic work control-plane direction

**Status:** revised proposal for discussion, 2026-09-27; not adopted

This document proposes a possible next product boundary for `reviewctl`. It is
not an implementation commitment and does not change current receipt, review,
or merge-gate semantics.

## Executive direction

Keep the existing review/evidence kernel narrow. Evaluate an **optional sibling
work-coordination layer** that links human-agent work to that kernel. Whether
this should ever become one product remains an explicit decision, not an effect
of merging this document.

The review remains the differentiating trust boundary. A sibling could add work
intake, provenance, next actions and later bounded delegation around it. It
must not change what a `ReviewReceipt` or `ProjectReviewJournal` means.

Neither module should become Jira, a document store, a hosted IDE, or a generic
workflow engine. The proposed layer would own only links and decisions that
existing issue/document tools cannot explain, and it must be removable without
invalidating review receipts.

A useful shorthand is:

```text
idea / issue / finding / request
        -> WorkItem
        -> bounded attempt(s)
        -> evidence + artifacts
        -> review
        -> decision / disposition
        -> next action or done
```

Every arrow should be explainable after the fact.

## Why this is adjacent to the current architecture

The current product implements some hard trust primitives:

- frozen-input provenance;
- typed review contracts;
- attempt execution and fallback;
- canonical V2 receipts and offline verification (including the GitHub V2
  promotion integrated by [PR #26](https://github.com/potzal/reviewctl/pull/26));
- an append-only local project review journal and a rebuildable finding
  projection, including explicit finding status events;
- a separate, explicit OpenRouter GitHub review opt-in integrated by
  [PR #28](https://github.com/potzal/reviewctl/pull/28), advisory rather than
  general backend qualification.

Adjudication/waiver facts beyond the existing finding statuses, a general
knowledge graph, `WorkItem`, `WorkLease`, `ChangeAttempt`, multi-user authority
and federation are **not implemented**. [Architecture](ARCHITECTURE.md) and
[handoff](HANDOFF.md) contain both present behavior and future planes; their
future vocabulary is not implementation evidence.

The missing layer is operational: what should be worked on next, who or what is
working on it, what it came from, what evidence changed, and whether the next
step is another agent, a human, a review, a document update, or archive.

## Product identity

Possible description **only if a later product decision adopts the layer**:

> A reviewctl-compatible work layer coordinates bounded human-agent work and
> links its provenance, artifacts, reviews, and decisions without replacing
> source systems or reviewctl's evidence contract.

Do not rename or broaden `reviewctl` now. Compare a sibling module/application
with keeping work intake in existing issue tools before naming any new product.

## Core principles

### 1. Evidence first

A state transition is not trusted because an agent narrated it. Important
transitions point to evidence: commits, diffs, receipts, test results, documents,
logs, screenshots, external issue snapshots, or explicit human decisions.

### 2. Provenance is first-class

Every material object should be able to answer:

- Where did this come from?
- Which earlier object caused it?
- Who or what acted on it?
- What exact input was visible?
- What evidence was produced?
- What policy allowed the next transition?

### 3. Keep journals separate; queues are projections

The existing `ProjectReviewJournal` remains canonical only for review facts.
A work pilot, if approved, owns a **separate** append-only work log with stable
references to review events and receipts. Inbox, active, blocked, review and
done are rebuildable projections of that work log, not new states in the
review journal. No migration or generalization is implied.

### 4. External systems remain authoritative for their own objects

GitHub owns GitHub issues and pull requests. Dropbox, Drive, repositories, and
document stores own their files. The proposed sibling would store typed
references, observed versions or hashes, provenance and work lifecycle facts.
Current `reviewctl` stores review-specific source and receipt provenance only;
neither should copy an external product merely to coordinate work around it.

### 5. Humans retain delivery authority

Agents may propose, implement, review, refute, route, and escalate. A review
receipt or agent decision does not authorize merge, deployment, release, or
destructive external actions. Those require an independently authorized human
decision under the applicable project and organization policy; no model may
approve or merge its own work.

### 6. Bounded autonomy over infinite loops

A later agent worker may iterate only under an explicit budget and observable
exit. A worker that cannot demonstrate progress must return the work or
escalate, rather than silently continuing forever. The first pilot has no
autonomous worker or lease.

## Proposed object model

### `WorkItem` — proposed for a sibling pilot

A durable unit of intent. It may originate from:

- a GitHub issue or pull request;
- a review finding;
- a human inbox entry;
- a document or ADR;
- another agent's delegation;
- CI/runtime evidence;
- an external system reference.

The minimal pilot fields are a stable local ID, project ID, source kind and
source version/hash, causal parent IDs, actor, classification, requested
outcome and next action. Current queue state is projected, not stored as
independent authority. An external issue remains authoritative at its URL and
observed version; an idea records the human/agent who supplied it.

### `EvidenceRef` — proposed reference, not a new receipt

A typed reference to evidence. Examples: receipt, commit, diff, test run, file
hash, screenshot, external snapshot, runtime log, or signed bundle.

An `EvidenceRef` should distinguish a live external reference from a frozen
snapshot and should record what integrity claim is actually available.

### `KnowledgeRef` — proposed interchangeable adapter reference

A typed reference to project knowledge such as an ADR, specification, README,
runbook, external document, issue discussion, or research note. The knowledge
object may live elsewhere; the proposed layer records its owner, identity,
observed version/hash and retrieval time. A derived summary is a claim with
cited source versions, not verified evidence merely because an agent wrote it.

### `AgentActor` / `HumanActor` — future identity design

Actors have identities and capabilities. Capability does not imply authority.
A model, CLI, coding agent, person, CI runner, or service account may all be
actors with different allowed transitions.

### `WorkLease` — later experiment, not pilot prerequisite

A bounded claim on a `WorkItem`:

```text
claimed -> working -> yielded | delegated | blocked | ready_for_review | failed
```

A lease has an owner, deadline/TTL, attempt budget, cost/resource budget,
heartbeat/progress evidence, and permitted mutation surface. Expiry returns the
item to policy-controlled scheduling; it does not fabricate completion.

### `ChangeAttempt` — future change-evidence contract

A change-producing attempt is deliberately separate from review. It binds an
actor to input state and produces patch/commit/artifact evidence. A successful
change attempt requires a fresh review when policy requires review.

### `ReviewReceipt` — implemented, unchanged

Keep the current strong meaning. A receipt proves what `reviewctl` can actually
observe and verify. It must not be diluted into a generic "task finished"
record.

### `Decision` / `Adjudication` — partly designed

The current journal has explicit finding status changes. A general decision
object, adjudication protocol and automated transition authority do not yet
exist. The pilot may record a human disposition in its own log; automated
judgments remain suggestions until separately verified and authorized.

## GTD-inspired work projections

A simple operational surface can borrow GTD vocabulary without importing a
personal productivity system wholesale:

```text
Inbox
  -> Clarify
  -> Ready
  -> Active
  -> Waiting / Blocked
  -> Review
  -> Done / Archive
```

These are views, not the canonical event model.

Illustrative projections, **not currently shipped**:

- a raw idea enters `Inbox`;
- an agent clarifies it into one or more bounded work items;
- policy makes one item `Ready`;
- a human marks one item active in the first pilot; a future qualified worker
  could claim a bounded lease;
- missing credentials produce `Waiting` rather than retries forever;
- a patch plus evidence requests a review when policy requires it;
- verified review evidence plus an authorized disposition produces `Done`;
- a rejected idea becomes `Archive` with its reasoning preserved.

## Dynamic knowledge plane

The knowledge plane should behave more like a maintained graph than a wiki that
must contain everything.

Possible relationships (not a shipped schema) include:

```text
WorkItem --derived-from--> Issue
WorkItem --requires--> ADR
ArtifactRef --derived-from--> SourceSnapshot
ReviewReceipt --reviews--> exact candidate
Finding --supported-by--> EvidenceRef
Decision --dispositions--> Finding
Document --supersedes--> Document
WorkItem --produces--> Document
```

Agents may propose summaries, links, tags, stale-document notices, or new ADRs.
Each derived artifact must cite exact source identity and observed version,
retain provenance and uncertainty, and never silently replace the canonical
source or count as an independently verified fact.

## Agent-runtime control

The **later** worker protocol should make agents replaceable workers rather than
authorities. No worker protocol or lease authority is implemented today.
OpenHands, Codex, Claude Code, Pi, or another runtime can implement the same
work protocol.

A worker can:

- claim an eligible item;
- inspect bounded context;
- create a plan or change attempt;
- attach evidence;
- split or delegate work;
- declare a blocker;
- request a human decision;
- request review;
- yield the lease.

A worker cannot simply declare its own work approved.

### Infinite-loop containment

For each lease, policy should be able to constrain:

- maximum wall-clock time;
- maximum attempts/iterations;
- model/tool budget;
- repeated-state detection;
- required progress evidence;
- maximum recursive delegation depth;
- escalation threshold.

If the same state recurs without new evidence, the safe outcome is an explicit
`needs_decision` or `blocked` state, not another invisible loop.

## Jev as a decision-layer experiment

[TypeSafe's announcement](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
calls Jev an early-access System One model. Its
[API reference](https://docs.typesafe.ai/api) documents bounded `choice`,
`score` and yes/no (`noul`) questions over supplied state. This verifies the
**interface claim**, not accuracy, calibration, cost or suitability for this
project. It is a candidate for a narrow **shadow decision experiment**, not a
reviewer, coding agent or authority. Pin an exact published model version,
not a mutable alias, if an experiment is approved.

Candidate experiments:

- classify an inbox item;
- select a work queue/lane;
- score task risk or ambiguity;
- decide retry vs delegate vs escalate;
- rank candidate agents for a bounded task;
- decide whether evidence is sufficient to request formal review;
- triage findings for human attention.

Guardrails:

- start in shadow mode against recorded historical work;
- pin model/version and preserve request/response evidence;
- calibrate thresholds on project data rather than vendor benchmarks;
- deterministic code keeps permissions, budgets, arithmetic, and destructive
  action authority;
- low confidence escalates rather than silently choosing;
- Jev never creates review approval or merge authority.

No TypeSafe account/API experiment or project benchmark was run for this
proposal. The vendor's claims about quality and speed are not reviewctl
measurements; see [research notes](RESEARCH-AGENTIC-CONTROL-PLANE-20260927.md).

This is valuable even if Jev is later replaced: the architectural seam is a
provider-neutral typed `DecisionPolicy`, not a Jev-specific dependency.

## Relevant ideas from Gentleman Programming / Gentle AI

[Gentle AI's own architecture](https://github.com/Gentleman-Programming/gentle-ai/blob/main/docs/architecture/organic-rdd.md)
describes Receipt-Driven Development (RDD) over a frozen candidate, bounded
review/correction and a separate delivery authority. It is a useful
first-party **design comparison** for several ideas close to `reviewctl`:

- Receipt-Driven Development (RDD): evidence instead of narrative completion;
- review follows a concrete candidate rather than replacing implementation;
- bounded review and correction budgets;
- explicit status/next-transition contracts for orchestration;
- human ownership of delivery;
- risk-sensitive review rather than applying maximum ceremony to every task.

The lesson to test is not "copy Gentle AI". The useful design pressure is:
**one owner for next-step semantics, bounded convergence, and receipts bound to
exact candidates**. Its own evolution also illustrates the cost of exposing too
much safety machinery as user ceremony, so the control plane should keep common
flows simple and make exceptional states recoverable.

The cited documents do not establish that Gentle AI implements `WorkItem`, a
project-wide issue queue, a maintained wiki, or reviewctl-equivalent receipt
verification. We did not run its software. Its current documented defaults
may differ from a particular installed release.

## Existing issue, queue and knowledge surfaces first

GitHub issues/PRs already own the initial source objects. Before building a
new task or wiki store, compare an adapter to
[Forgejo issues/projects/wiki](https://forgejo.org/docs/latest/user/getting-started/issue-tracking-basics/),
[OpenProject work packages/wiki](https://www.openproject.org/docs/user-guide/work-packages/),
and a document-only backend such as [BookStack](https://www.bookstackapp.com/).
These cover different slices; none is presumed to own reviewctl receipts or
work provenance. The first pilot should use **references and versioned
snapshots** against one existing issue system, with a document-reference
adapter interface. No database or UI selection is part of this PR.

The [Google Code Wiki announcement](https://developers.googleblog.com/introducing-code-wiki-accelerating-your-code-understanding/)
claims regenerated code-linked documentation; GitHub's
[Copilot Memory documentation](https://docs.github.com/en/copilot/concepts/agents/copilot-memory)
describes cited reusable facts. Neither proves a trustworthy, automatically
maintained project decision wiki. Agent-authored knowledge must cite source
versions and be labeled derived/unverified until a human or independent check
confirms it. Detailed source boundaries and alternative deployments are in
[the research notes](RESEARCH-AGENTIC-CONTROL-PLANE-20260927.md).

## Proposed ownership boundary

| Concern | Proposed owner | Status |
| --- | --- | --- |
| Frozen packet, typed review contract, attempt, acceptance, `ReviewReceipt`, offline verification | `reviewctl` | Implemented; retain current semantics |
| `ProjectReviewJournal` and finding projection | Project's existing `reviewctl` instance | Implemented subset; do not migrate or generalize |
| Work intake, causal provenance, next action, work queue and disposition | Optional sibling over its **own** append-only log | Proposed pilot, not implemented |
| Issues, pull requests and project documents | Existing source systems and interchangeable document adapters | External authority; adapter selection open |
| Review obligation, waiver and delivery approval | Project/organization policy and authorized humans | Existing gates remain; generalized policy proposed |
| Opaque bundle storage/distribution | Optional Potzal adapter or equivalent | Deferred; never required for local use |

This is a recommendation to **test a sibling**, not to build it in this PR.
The separation is falsifiable: if a pilot cannot preserve stable links to the
review kernel without copying receipt authority, reconsider the product seam.
Do not turn either side into Jira, a document store, an IDE, an autonomous
merge/deploy authority or a generic workflow engine.

## Relationship with Potzal

Potzal could later store, authorize and distribute opaque, verifiable bundles.
It would not interpret review events or decide whether a review passed. No
Potzal dependency, collaboration protocol or signed exchange is in the first
increment.

```text
existing issue/document system ──versioned refs──> optional work log
                                                   │ stable references
                                                   v
                                      reviewctl review journal + receipt
                                                   │ optional future export
                                                   v
                                      Potzal or another bundle distributor
```

If exchange is later pursued, specify bundle/event IDs, parent IDs, monotonic
origin sequence, idempotent import and conflict reporting; signature key IDs,
rotation/revocation and replay protection; per-object classification, allowed
recipients, redaction/encryption and metadata exposure; offline verification,
quarantine and import cursors. Import must add facts or report conflict, never
silently mutate an existing journal. None of this is implemented by this
proposal.

## First finite increment: a manual provenance slice

**Dependency:** stabilize existing review defects and settle the #12 product
boundary before authorizing even this pilot. Implement in a sibling or an
isolated prototype, with an adapter to current `reviewctl`; no schema changes
to `ReviewReceipt` or `ProjectReviewJournal`.

```text
issue / finding / idea
  -> WorkItem with source identity + observed version/hash + causal parents
  -> explicit next action (clarify / ready / blocked)
  -> one referenced artifact or bounded attempt
  -> applicable review, if project policy requires it
  -> authorized disposition, with reason and evidence refs
  -> rebuildable next-action/done projection
```

The WorkItem stores local ID, project, source kind/ID/version, observing actor,
classification, requested outcome, causal parents and next-action reason. A
finding remains in the review journal; the sibling points to it. An artifact
reference identifies a document version, patch or commit; it is **not** a new
`ChangeAttempt` protocol. A required review must refer to the exact candidate
and a verified current receipt. If review is inapplicable, the authorized
disposition records the policy reason; an unavailable reviewer does not
silently turn a required review into an optional one.

Pilot acceptance criteria:

1. Replay the sibling log to the same work projection; duplicate event import
   is idempotent or rejected visibly. The review journal is unchanged.
2. Three fixtures cover issue, finding and idea intake through disposition,
   with exact source/version and causal links. Missing or stale sources,
   conflicting writes and a crash between attempt and disposition yield
   explicit recoverable states, not a fabricated `Done`.
3. A single writer per item with expected revision rejects concurrent writes;
   this is not multi-user collaboration or a lease service. Recovery can
   reconcile a persisted artifact without rerunning an external action.
4. One human-started attempt has a finite wall-time and cost/attempt cap. No
   automatic recursive delegation or retry loop. Ambiguity, exhausted budget,
   failed review or missing evidence escalates to a human with a concrete
   question and current evidence.
5. The producer of a change cannot mark their own work approved. Only an
   authorized human/policy role records disposition; this never grants merge
   or deployment permission. A model suggestion remains advisory.
6. Derived summaries/knowledge retain source IDs, observed versions and
   retrieval time, are labeled unverified, and never satisfy review evidence
   merely by existing.

## Review policy question raised by PR #19

[PR #19](https://github.com/potzal/reviewctl/pull/19) proposes mandatory GitHub
Codex review before merge. Its exact-head requirement and explicit disposition
of every material finding are valuable and must survive a provider-neutral
policy design. The current [project integration gate](PROJECT-INTEGRATION.md)
remains in force until a replacement is agreed; this proposal does not waive
it.

A candidate policy would configure eligible independent reviewer **classes**
per project/risk, require a **completed** review of the final exact candidate
SHA, inspect every review comment and thread, record verified evidence and an
explicit, technically supported disposition of **every reviewer comment**
(including comments judged non-material) and every material finding, and block
on unavailable/incomplete results or unresolved material threads. If an
organization wants an exception,
only a named authorized human can grant an explicit, reasoned, time-bounded,
audited waiver; there is no default waiver or automatic self-approval. This is
policy design, not a CLI change or an adopted replacement for #19.

## Evaluation before autonomy

Build a curated, adjudicated set of real/synthetic changes with known defects
and non-defects, including privacy-boundary and transport-failure cases.
Compare baseline human/current review with candidate backends under fixed
model versions and equivalent source packets. Blindly adjudicate findings.
Report true positives, false positives per change, omitted defects/recall,
severity calibration, p50/p95 wall latency and **total** cost (model calls,
retries, human triage, CI, failures and maintenance), segmented by project
classification and change risk. Track how often a provider or transport fails
without producing a usable review. Do not optimize price per token alone.

Decision models such as Jev start in shadow mode on recorded cases. Compare
their suggestions to human dispositions and deterministic policy; measure
calibration, drift and abstention. They never grant permissions, waive a gate,
or replace a formal review. Escalate rather than auto-route when evidence is
missing. Do not promote a model based on vendor claims or one demonstration.

## Staged roadmap and exit gates

| Stage | Dependency and bounded deliverable | Exit evidence |
| --- | --- | --- |
| 0. Core reconciliation | Preserve unique #12 implementation, investigate its high-thinking Pi capture/canary, reproduce/fix #3 and #16, decide #4 non-formal fallback, settle #12/#19 policy conflict; do not redo #26/#28 | [Separate code PR #30](https://github.com/potzal/reviewctl/pull/30), CI selector at 100% branch coverage without exclusions, focused regressions, offline receipt verification; high-thinking real subprocess canary and explicit policy disposition still needed |
| 1. Optional manual work pilot | Stage 0 and product-boundary approval; one sibling adapter to existing issue/document references and current reviewctl | Six acceptance criteria above, replay/recovery fixtures and one traceable end-to-end example; no lease/multiuser/automatic permission |
| 2. Evidence-based adapters | Stage 1 demonstrates a need; compare existing issue/queue/wiki tools and document backends, then run quality/cost experiments | Recorded trade-off, reproducible corpus and metrics, privacy review, provider-neutral adapter contract; abandon if existing tools suffice |
| 3. Bounded workers and sharing, only if justified | Stage 2 evidence and separate security/product decision | Explicit worker/`ChangeAttempt` contract, budgets, concurrency and trust protocol; optional Potzal federation or UI cannot alter local receipts |

Stages 1–3 are options, not scheduled commitments. The existing
[review-kernel roadmap](HANDOFF.md#roadmap) and
[GitHub/Pi roadmap](superpowers/specs/2026-08-24-github-pi-review-roadmap.md)
remain their respective historical plans; this document does not mark their
future items complete.

## Open-work disposition (snapshot at main `6509de9`, 2026-09-27)

These are recommendations, not closures. Diffs, comments, tests and main were
compared; #26 and #28 are already integrated. A new document cannot itself
resolve an older implementation or policy conflict.

Reproduction and verification trace: on main
[`6509de9`](https://github.com/potzal/reviewctl/commit/6509de91550425b4d8e6c49f09743d553224c001)
and #29 head
[`00579f9`](https://github.com/potzal/reviewctl/commit/00579f9d371a56405d66c9eb81650eca1aba6a22),
the exact CI selector `uv run pytest -m "unit or contract" --cov=reviewctl
--cov-branch --cov-report=term-missing` reported 99.94%, missing
`project_cli.py:428-432,446`; [#29 run 36299614354](https://github.com/potzal/reviewctl/actions/runs/36299614354)
also failed that gate on Linux with 2,204 passed, 1 skipped and 1 deselected.
The regression tests committed in
[#30 `f2bc867`](https://github.com/potzal/reviewctl/commit/f2bc867)
(`tests/test_run.py` prompt-only receipt, Codex isolation preflight/process
failures; `tests/test_github_cli.py` OpenRouter opt-in path) were first run
against main behavior and exposed #16's `receipt-source` failure and #3's
missing evidence pointer. After the separate fix, the same local selector
reported 2,209 passed, 1 deselected and 100.00% branch coverage on macOS.
This is local evidence, not a claimed provider canary or remote CI approval.

| Item | Disposition now | Evidence and next action |
| --- | --- | --- |
| [PR #12](https://github.com/potzal/reviewctl/pull/12) | **Partially resolved; keep open.** | Main has much Pi/thinking behavior but its CLI thinking whitespace normalization and Pi request-evidence pointer were absent. [Code PR #30](https://github.com/potzal/reviewctl/pull/30) preserves those; it is not yet integrated. #12 also identifies a high-thinking Pi bounded-capture failure and asks for a real subprocess canary; current unit fixtures do **not** establish that this is fixed. Investigate and run a bounded canary separately before closing. Its narrow review-only scope conflicts with #29's sibling/work direction; obtain an explicit product decision. |
| [PR #19](https://github.com/potzal/reviewctl/pull/19) | **Vigente; keep open.** | Its revised head blocks when GitHub Codex is unavailable and preserves exact-head review plus finding disposition. Evaluate the provider-neutral policy above with owners; do not weaken the current project gate or close #19 by labeling it superseded. |
| [Issue #3](https://github.com/potzal/reviewctl/issues/3) | **Partially resolved.** | Main classifies the macOS Codex isolation failure as `transport-failed`, but a reproduced failed run loses command context, stdout/stderr and output-state evidence after cleanup. [Code PR #30](https://github.com/potzal/reviewctl/pull/30) contains the fix/regressions, not yet integrated; close only after integration. |
| [Issue #4](https://github.com/potzal/reviewctl/issues/4) | **Vigente; explicit non-formal fallback still needed.** | `reviewctl explore` is a Pi, read-only material path, not the requested local Codex/Luna fallback with source hashes and `local-fallback` label. Define a project-side, source-hashed exploratory artifact and recovery procedure first; it must say `not a formal review` and never satisfy a required receipt gate. A new core transport is not justified by this issue alone. |
| [Issue #16](https://github.com/potzal/reviewctl/issues/16) | **Vigente.** | Main's accepted literal `run --prompt` receipt fails `verify` with `receipt-source` because there is no persisted source path. [Code PR #30](https://github.com/potzal/reviewctl/pull/30) stores private prompt bytes and tests offline verification, but is not integrated; do not close yet. |

The #29 thread had one issue comment about a separate Potzal PR #21 at this
snapshot, and no inline review comments or submitted reviews. That comment
does not supply review of this product direction.

## Independent advisory scope review

On 2026-09-27, two independent subagent passes inspected the documentary
scope; neither ran `reviewctl` or produced a formal receipt. The standards
pass checked the local #29 diff against architecture, handoff and project
integration rules. It found an unsafe implied agent merge exception, an
incorrect present-tense ownership claim for work facts, and insufficiently
traceable reproduction claims. Those were corrected with an explicit human
delivery boundary, sibling ownership and the main/#29/#30 test trace above.
The spec pass first inspected the published original `00579f9`, then the
revised local document against the requested PR/issue reconciliation, finite
pilot, research and gate preservation. Its final remaining comment was that
every reviewer comment, not only material findings, needs a technically
supported disposition under a provider-neutral #19 policy; this text was
amended accordingly. These are **advisory documentary reviews**, not approval
of a code change, product adoption or an invented `ReviewReceipt`.

## Decisions still required

1. Do owners accept a sibling pilot at all, or should issue/document systems
   own all work intake? This resolves the explicit #12/#29 product conflict.
2. What reviewer classes and human waiver authority, if any, would replace
   #19's GitHub-Codex-specific gate? Existing gates continue meanwhile.
3. Which issue and document backend should a *reversible* first pilot observe,
   with what data classification and version/retention boundary?
4. Who can record a human disposition, and when is a formal review applicable
   versus an explicitly non-formal exploration artifact?
5. What evaluation threshold would justify a worker protocol, decision model,
   maintained knowledge layer, collaboration or optional Potzal exchange?

No answer to these questions is inferred from merging a documentation PR.
