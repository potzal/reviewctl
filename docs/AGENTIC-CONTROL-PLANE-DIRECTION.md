# Agentic work control-plane direction

**Status:** proposal for discussion, 2026-09-27

This document proposes a possible next product boundary for `reviewctl`. It is
not an implementation commitment and does not change current receipt, review,
or merge-gate semantics.

## Executive direction

Keep the existing review/evidence kernel, but allow the product to evolve from
"a control plane for bounded model reviews" into an **evidence-backed control
plane for human-agent software work**.

The review remains the differentiating trust boundary. The broader product adds
work intake, provenance, delegation, state, knowledge references, and next-step
coordination around that boundary.

The product should not become Jira, a document store, a hosted IDE, or a generic
workflow engine. It should own the verifiable graph that connects work to
sources, attempts, evidence, decisions, reviews, and final dispositions.

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

The current product already owns or models most of the hard trust primitives:

- frozen-input provenance;
- typed review contracts;
- attempt execution and fallback;
- immutable receipts;
- append-only project journal facts;
- finding identity and lifecycle;
- adjudications, waivers, fixes, and verification observations;
- rebuildable projections;
- optional future `ChangeAttempt` semantics separate from review approval.

The missing layer is operational: what should be worked on next, who or what is
working on it, what it came from, what evidence changed, and whether the next
step is another agent, a human, a review, a document update, or archive.

## Product identity

Proposed description:

> `reviewctl` is an evidence-backed control plane for software work performed by
> humans and agents. It coordinates bounded work, preserves provenance, and
> turns attempts, artifacts, reviews, and decisions into a verifiable project
> history.

The name can remain `reviewctl` while this is experimental. A rename should only
be considered if non-review work becomes a stable primary surface rather than a
supporting plane around review.

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

### 3. Journals are canonical; queues are projections

The append-only journal remains the source of truth. Inbox, backlog, active,
blocked, review, waiting, and done are projections over durable events rather
than mutable authority hidden in a task table.

### 4. External systems remain authoritative for their own objects

GitHub owns GitHub issues and pull requests. Dropbox, Drive, repositories, and
document stores own their files. `reviewctl` stores typed references, snapshots
or hashes where required, provenance, and lifecycle facts. It should not copy an
entire external product merely to coordinate work around it.

### 5. Humans retain delivery authority

Agents may propose, implement, review, refute, route, and escalate. A review
receipt or agent decision does not itself authorize merge, deployment, release,
or destructive external actions unless an explicit organization policy says so.

### 6. Bounded autonomy over infinite loops

An agent may iterate, but each lease has a budget and an observable exit. An
agent that cannot demonstrate progress must return the work, delegate it, or
escalate it rather than silently continuing forever.

## Proposed object model

### `WorkItem`

A durable unit of intent. It may originate from:

- a GitHub issue or pull request;
- a review finding;
- a human inbox entry;
- a document or ADR;
- another agent's delegation;
- CI/runtime evidence;
- an external system reference.

Important fields include stable identity, source/provenance links, project,
classification, current projection, constraints, requested outcome, and causal
parents.

### `EvidenceRef`

A typed reference to evidence. Examples: receipt, commit, diff, test run, file
hash, screenshot, external snapshot, runtime log, or signed bundle.

An `EvidenceRef` should distinguish a live external reference from a frozen
snapshot and should record what integrity claim is actually available.

### `KnowledgeRef`

A typed reference to project knowledge such as an ADR, specification, README,
runbook, external document, issue discussion, or research note. The knowledge
object may live elsewhere; `reviewctl` records identity, relationship, optional
snapshot/hash, and observed freshness.

### `AgentActor` / `HumanActor`

Actors have identities and capabilities. Capability does not imply authority.
A model, CLI, coding agent, person, CI runner, or service account may all be
actors with different allowed transitions.

### `WorkLease`

A bounded claim on a `WorkItem`:

```text
claimed -> working -> yielded | delegated | blocked | ready_for_review | failed
```

A lease has an owner, deadline/TTL, attempt budget, cost/resource budget,
heartbeat/progress evidence, and permitted mutation surface. Expiry returns the
item to policy-controlled scheduling; it does not fabricate completion.

### `ChangeAttempt`

A change-producing attempt is deliberately separate from review. It binds an
actor to input state and produces patch/commit/artifact evidence. A successful
change attempt requires a fresh review when policy requires review.

### `ReviewReceipt`

Keep the current strong meaning. A receipt proves what `reviewctl` can actually
observe and verify. It must not be diluted into a generic "task finished"
record.

### `Decision` / `Adjudication`

A typed decision records the question, available evidence, actor/policy,
result, uncertainty where applicable, and the transition it authorizes. Human
and automated decisions should remain distinguishable.

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

Examples:

- a raw idea enters `Inbox`;
- an agent clarifies it into one or more bounded work items;
- policy makes one item `Ready`;
- OpenHands claims a lease and it becomes `Active`;
- missing credentials produce `Waiting` rather than retries forever;
- a patch plus evidence moves it to `Review`;
- accepted review plus required human disposition produces `Done`;
- a rejected idea becomes `Archive` with its reasoning preserved.

## Dynamic knowledge plane

The knowledge plane should behave more like a maintained graph than a wiki that
must contain everything.

Useful relationships include:

```text
WorkItem --derived-from--> Issue
WorkItem --requires--> ADR
ChangeAttempt --modifies--> SourceSnapshot
ReviewReceipt --reviews--> ChangeAttempt
Finding --supported-by--> EvidenceRef
Decision --dispositions--> Finding
Document --supersedes--> Document
WorkItem --produces--> Document
```

Agents may propose summaries, links, tags, stale-document notices, or new ADRs.
Those derived artifacts must retain provenance and should never silently replace
the canonical source.

## Agent-runtime control

The control plane should make agents replaceable workers rather than authorities.
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

Jev, TypeSafe AI's current System One decision model, is interesting here
because its public interface is explicitly oriented toward typed bounded
judgments rather than prose generation. That makes it a candidate for a narrow
**decision layer**, not a reviewer or coding agent.

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

This is valuable even if Jev is later replaced: the architectural seam is a
provider-neutral typed `DecisionPolicy`, not a Jev-specific dependency.

## Relevant ideas from Gentleman Programming / Gentle AI

Gentle AI is useful as a comparison because it has independently converged on
several ideas already close to `reviewctl`:

- Receipt-Driven Development (RDD): evidence instead of narrative completion;
- review follows a concrete candidate rather than replacing implementation;
- bounded review and correction budgets;
- explicit status/next-transition contracts for orchestration;
- human ownership of delivery;
- risk-sensitive review rather than applying maximum ceremony to every task.

The lesson to adopt is not "copy Gentle AI". The useful design pressure is:
**one owner for next-step semantics, bounded convergence, and receipts bound to
exact candidates**. Its own evolution also illustrates the cost of exposing too
much safety machinery as user ceremony, so the control plane should keep common
flows simple and make exceptional states recoverable.

## What this product is not

Do not turn `reviewctl` into:

- Jira with a new UI;
- a general team project-management suite;
- the canonical store for every project document;
- a model marketplace or permanent model roster;
- an autonomous merge/deploy authority;
- a generic Temporal/Airflow-style workflow engine;
- an IDE or coding-agent runtime;
- Potzal itself.

Its durable value is the trustworthy coordination boundary between these
systems.

## Relationship with Potzal

Potzal may be an optional storage/distribution/UI integration for opaque or
signed evidence bundles, artifacts, and trusted metadata. It should not become
a required dependency for the local control plane, and `reviewctl` should not
inherit Potzal's artifact/package authority model as its work semantics.

The two products can meet through explicit adapters:

```text
reviewctl work/evidence journal
        <-> signed/opaque bundle boundary
Potzal storage, distribution, trust metadata, UI projection
```

## Incremental roadmap

### Phase A — preserve the review kernel

- no semantic weakening of receipts;
- close current receipt/source-isolation defects;
- keep review contracts provider-neutral;
- preserve local-first operation.

### Phase B — work journal and queue projection

- introduce `WorkItem` and provenance events;
- add `Inbox`, `Ready`, `Active`, `Blocked`, `Review`, `Done` projections;
- allow a finding to create/link a work item without copying it into a second
  authority store;
- add explicit `nextTransitions` output suitable for humans and agents.

### Phase C — knowledge references

- define typed GitHub/file/document references;
- support observed version/hash/freshness metadata;
- add `derived-from`, `requires`, `supersedes`, and `produces` relationships;
- keep external stores authoritative.

### Phase D — bounded agent work

- add `WorkLease` and provider-neutral worker protocol;
- implement `ChangeAttempt` separately from `ReviewReceipt`;
- add budget, timeout, repeated-state, delegation-depth, and escalation rules;
- require fresh review of mutable outputs according to policy.

### Phase E — decision policy

- define provider-neutral typed decision contracts;
- first deterministic/reference implementation;
- shadow-evaluate Jev on routing/risk/retry/escalation decisions;
- graduate individual decisions only after calibration evidence exists.

### Phase F — collaborative projection

- optional UI over work/evidence/knowledge graph;
- multi-user actor identities and leases;
- adapters for GitHub and selected document stores;
- optional Potzal-backed distribution of bundles/projections without changing
  local semantics.

## Current open-work disposition proposal

This section records an observation as of 2026-09-27; it does not close or merge
anything.

### `potzal/reviewctl`

- **PR #12 — product scope / viability:** candidate to close as superseded. Its
  key local-first evidence-control-plane ideas are already represented by newer
  architecture and handoff documents, while its explicit prohibition on
  change-writing/work coordination conflicts with this proposal's experiment.
  Preserve any still-missing Pi thinking implementation separately if main does
  not already contain it.
- **PR #19 — mandatory GitHub Codex merge gate:** candidate to supersede with a
  provider-neutral merge/review policy. Exact-head review and explicit finding
  disposition are durable ideas; hard-coding GitHub Codex as the mandatory
  authority is not.
- **Issue #16 — literal prompt receipts:** remains core correctness debt.
- **Issue #4 — Luna fallback artifact:** remains useful only if the fallback
  lane is still operationally required; it must remain visibly non-formal.
- **Issue #3 — macOS Codex isolation diagnostics:** remains core evidence and
  diagnostic debt if reproducible on current main.

### `potzal/potzal`

- **PR #21 — Kubernetes storage lifecycle conformance:** active current work;
  appears to absorb earlier CI/JVM/F-Droid/OCI work and still has explicit live
  release gates.
- **PR #22 — console credential safety/accessibility:** active current work;
  manual authenticated UI/accessibility QA remains open.
- **PR #17 / #18:** candidates to close as superseded once #21 is confirmed to
  contain their required changes/evidence and its target/base strategy is made
  explicit.
- **PR #20 — npm JSON 404 behavior:** independent active defect; finish runtime
  verification or rebase before disposition.
- **PR #16 — generic artifact/discovery architecture:** separate design track;
  keep it isolated from the proposed `reviewctl` work-control semantics.
- **Issues #8, #11, #19:** remain directly relevant to current Kubernetes,
  product/UI, and per-user credential work respectively; close only against
  demonstrated completion, not because newer PRs exist.

## Date reconstruction note

A repository search found no PR updates and no commits on either repository for
2026-09-24 through 2026-09-25. The Astra-reviewed Potzal work discussed in this
planning session appears in PRs #21 and #22 created on 2026-09-26. This proposal
therefore treats 2026-09-26 as the relevant recent implementation slice while
still preserving the requested 24–25 audit result.

## Decisions to make before implementation

1. Is `WorkItem` part of `reviewctl` core or a sibling package/application over
   the same journal contracts?
2. Does the canonical journal generalize from `ProjectReviewJournal` to a
   broader `ProjectWorkJournal`, or should review remain a strict sub-journal?
3. Which external reference types are P0: GitHub, local files, generic URLs,
   Dropbox/Drive, or a repository docs tree?
4. Which transitions require human authority by default?
5. What is the minimum worker protocol that OpenHands/Pi/Codex can all satisfy?
6. Which decisions are safe enough for a model such as Jev to shadow first?
7. Should the UI live with `reviewctl`, in Potzal, or as a separate projection?

## Proposed next implementation slice

Do not start with UI or multi-agent execution. Start with one vertical slice:

```text
GitHub issue (reference)
  -> WorkItem in Inbox
  -> clarify to Ready
  -> human/agent claims bounded lease
  -> attach one ChangeAttempt or document artifact
  -> formal review receipt
  -> disposition
  -> Done projection
```

If that flow is clean, replayable, and understandable from the journal alone,
the product has a credible foundation for the broader control plane.