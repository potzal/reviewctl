# Review Evidence Architecture

`reviewctl` is a neutral review evidence control plane. It owns contracts,
attempt execution, and receipts. It does not own an organization's model roster,
a project's canonical history, or a federation peer's trust policy.

This describes the review architecture, not an adopted work-management product.
The [agentic work direction proposal](AGENTIC-CONTROL-PLANE-DIRECTION.md)
recommends testing an optional sibling work log with references to the current
review journal; it does **not** generalize `ReviewReceipt` or migrate
`ProjectReviewJournal`. Its staged roadmap and unresolved decisions are not
part of the implemented contract.

## Ownership planes

1. **Contract plane — reviewctl.** Typed inputs and outputs, schemas, output
   instructions, exact decoding, semantic validation, normalization, versions,
   and contract identity.
2. **Attempt and receipt plane — reviewctl.** Frozen-input provenance,
   invocation manifests, observed transport facts, response identity, attempt
   outcomes, canonical receipts, sealing, and verification.
3. **Project evidence plane — project owner.** One logical
   `ProjectReviewJournal` per stable project identity, containing imported
   receipts, finding lifecycle events, adjudications, waivers, fixes, and
   verification observations.
4. **Qualification plane — organization.** Model/provider qualification,
   tournaments, corpora, capability periods, policies, budgets, and council
   decisions.
5. **Federation and aggregate plane — participating authorities.** Signed,
   sanitized exports, compatible dimensions, idempotent imports, and
   reproducible projections. It is not canonical storage for private origin
   evidence.

## Backend execution boundary

reviewctl keeps the controller, adapter process, evidence handling, and setup
diagnostics local. Provider or model execution may be remote for `REMOTE_API`
backends. Local reviewctl execution does not enable remote controller or adapter
dispatch. Federation remains deferred and separate from backend execution and
setup synchronization.

The BAML-inspired typed boundary is native and has no BAML dependency. The
concepts are adopted natively, with no BAML runtime dependency; BAML is not a
library, service, generator, or runtime requirement.

The boundary has three provider-neutral types:

- **BackendRequest** carries the prepared prompt, frozen source paths, requested
  model, limits, and attempt location into an adapter.
- **BackendExecution** returns only observed transport outcome, response, and
  persisted evidence locations.
- **BackendCapabilities** declares what an adapter can report or enforce; it is
  not proof that the adapter is qualified for review.

Backend adapters only invoke a backend and persist its observed evidence;
adapters do not decide acceptance. The controller alone owns policy, contract
evaluation, acceptance, fallback, and receipt construction.

Private experimental policies may authorize local transport defaults under
`[transports.<name>]` for `kiro`, `gemini`, `pi`, and `codex`. An exact model
entry under `[models.<id>]` overrides that default. This permits a
runtime-owned local inventory without publishing a roster; it does not open
OpenRouter, resolve an otherwise unresolved identity, or qualify a backend.

The registered CLI adapters are explicitly unqualified: `llm`, `agy`,
`gemini`, `pi`, and `codex`; the direct `openrouter` transport is also
unqualified. They preserve route behavior without claiming conformance.
Availability is not qualification.

Gemini CLI is a separate native adapter from Antigravity (`agy`). It runs the
installed `gemini` executable in headless JSON mode with `--approval-mode plan`
and `--sandbox`, and sends the frozen packet over standard input from a
disposable working directory. The CLI's `response` and session identifier are
retained; its per-model statistics remain raw evidence. The requested model is
kept as requested identity because the CLI may resolve an alias to another
model, so the adapter deliberately does not claim resolved model identity or
qualification. Gemini's CLI does not expose a portable output-token cap, so
the receipt records the requested value as unenforced.

Kiro is a registered native agent-CLI adapter and remains unqualified. Its
default executable is `kiro-cli`; `KIRO_BIN` overrides that executable.
`reviewctl setup check --backend kiro` performs version-only local discovery:
it does not authenticate or call a model or provider. Availability and a valid
receipt do not qualify a model. Qualification and the current operating roster
remain the organization's private evidence responsibility.

Kiro owns its runtime model inventory. Read it from the installed CLI with
`kiro-cli chat --list-models --format json`, then select one exact returned ID
with `--transport kiro --model MODEL_ID` or `--route kiro:MODEL_ID`. `auto` is
rejected because resolved model identity is unobservable. Never copy that
inventory, model prices, credit values, or provider commands into this
repository or a project's instruction files.

The adapter reuses the user's local Kiro subscription and login, so it can avoid
OpenRouter for models available through that account. The adapter does not use
OpenRouter and does not inherit ambient provider, AWS, or API-token variables.
For a formal invocation it performs a dynamic model check, creates a disposable
controlled working directory, passes a reduced environment, and installs a
workspace-local `reviewctl_readonly` agent with no tools, allowed tools, MCP
servers, inherited MCP configuration, or resources. Its exact configuration and
digest are retained in the request manifest. The adapter supplies the inline frozen packet over
standard input so bounded source is not constrained by the process argument-size limit.
Inventory, invocation, and session recovery share one total timeout; retained
evidence is written with mode `0600`. The request manifest links the raw model
inventory and its digest even when inventory discovery or validation fails.
The initial adapter accepts only `findings-json`. Kiro's terminal-rendered
`document`, `verdict`, and product output cannot be separated from UI framing
without rewriting possible response content, so those contracts fail before
artifacts or source transmission.
The child terminal is forced to `TERM=dumb` with standard no-color settings.
Only a boundary at byte zero is removed; a banner or prompt-like line later in
the stream is not a response boundary. Invalid UTF-8 or ANSI remaining inside
the JSON payload fails the attempt while raw stdout remains evidence.

This boundary provides advisory read-only behavior and tool control with
`sourceIsolation: unavailable`. The disposable directory and reduced process
environment are not OS sandbox enforcement. Proprietary Kiro source therefore
requires both `source_allowed = true` and `allow_unresolved_identity = true`
for the requested model before any source bytes are sent. The second decision
is an explicit, receipt-recorded waiver: it does not turn the requested model
into an observed identity or qualify the backend. Synthetic runs do not require
that policy or waiver.

For Kiro, receipt `result: accepted` means only that the response passed the
declared contract. The same receipt records
`extension.backendQualification: unqualified` and
`extension.mergeGateEligible: false`. Any merge gate must reject that explicit
indicator. Receipt verification rejects an accepted Kiro receipt if either
value is absent or changed; the advisory response remains available for
consolidation or later review instead of being discarded.
Legacy schema-v1 receipts cannot claim Kiro because that schema predates the
backend-qualification boundary.
For a proprietary schema-v2 route set containing Kiro, verification also
requires `extension.kiroUnresolvedIdentityWaiver: true` and rejects that field
on receipts where no such waiver applies.

Setup diagnostics observe only executable presence and version for registered
executable backends. Setup diagnostics never authenticate, call a model or
provider, or write configuration. The next gate is backend conformance before
Cursor, Claude Code, or another native backend can be added.

Registration and discovery are not support or qualification claims. Adapters
may be registered while remaining unqualified, and availability is not
qualification. Public documentation therefore contains no operating model
roster, prices, provider-specific invocation commands, or credentials. It does
not claim Cursor or Claude Code support.

## Partial review and bounded completion

An eligible typed response evaluates as **complete, incomplete, or invalid**.
Transport, timeout, model, provider, empty-response, and conversation pre-gates
run before contract evaluation. Rejected responses never promote fragments.
Only a response that passes those gates and is incomplete with independently
valid findings may contribute promoted fragments.

The controller can use those findings as bounded input to a later attempt. The
completion context is bound to the target contract, never contains the raw
response, and never inherits approval. It carries only revalidated typed
fragments, their provenance, and the target contract's missing-field manifest.
Absence is not a dispute: a reviewer that does not repeat an earlier finding
has not contradicted or resolved it.

maxAttempts applies independently to each route. Retrying one route does not
consume another route's allowance. A same-route retry and a route fallback are
recorded as different relationships.

acceptedAttempt names a real complete accepted attempt. A partial response,
promoted fragment, fallback, or consolidated projection can never occupy that
field or manufacture approval. The legacy verdict and findings remain the view
of that accepted attempt. The consolidated view preserves partial or
unconfirmed findings with provenance; approval is stricter because any such
finding prevents the consolidated view from claiming approval.

New receipts use schema v2, which adds offline structural verification of
attempts, contract identities, fallback relationships, promoted fragments, and
consolidation. Schema v1 remains digest-only for compatibility; schema v2 adds
offline structural verification rather than changing historical v1 meaning.

## Current deployment boundary

reviewctl is local-first: its controller, policy decision, evidence assembly,
and verification run on the user's machine. Project-owned evidence stores are
a compatible future destination for receipts. Federation is optional future
work, and Potzal is not a dependency. Potzal or another store may later carry
signed bundles without owning reviewctl semantics.

Editable execution is deferred for formal review. Cursor, Claude Code, and
other interactive editors are also deferred until a backend passes explicit
conformance and organization qualification. No current receipt claims those
capabilities.

## Canonical terms

- **ReviewContract:** versioned native definition of one typed review response.
- **PreparedContract:** provider-neutral instructions and schema compiled for
  one review context, with a canonical digest.
- **ContractEvaluation:** exact decode and semantic-validation result for one
  raw attempt payload.
- **AcceptanceGate:** orchestration decision that combines transport, model,
  provider, conversation, and contract evidence. A valid contract payload alone
  is not acceptance.
- **ReviewReceipt:** immutable canonical evidence produced by one run.
- **ReviewEvent:** immutable statement appended to a project or organization
  journal.
- **ProjectReviewJournal:** canonical ordered events for one stable ProjectId.
- **QualificationJournal:** organization-owned capability and policy events.
- **EvidenceBlob:** sealed, content-addressed private artifact kept outside
  query rows.
- **Projection:** disposable query state rebuilt from canonical journals.
- **DimensionDefinition:** versioned meaning, privacy class, and compatibility
  rules for one analytical axis.
- **FederationBundle:** signed export from one authorized origin and export
  sequence range.
- **Supersession:** a new event correcting an earlier interpretation without
  rewriting history.

The canonical term is **append-only journal**, not “write-only database.” A
journal is readable for verification and replay; its derived projections may be
discarded and rebuilt.

## Local project journal envelope and dimensions

The local project journal is a JSONL append-only stream. `reviewctl init` gives
the project an explicit portable `project.id` in `reviewctl.toml` and creates a
machine-local `originId` in `.reviewctl/identity.json` with mode `0600`. New
events carry `schemaVersion`, `projectId`, `originId`, a contiguous origin
`sequence`, `previousEventSha256`, and an `eventSha256` over canonical sorted
compact JSON without the trailing newline. POSIX locking and fsync protect one
append; an unavailable lock is an operational error, never permission to write
without continuity evidence.

Older un-enveloped events remain readable as a legacy prefix. When a new
versioned event follows that prefix, its previous digest is the canonical digest
of the preceding parsed event. `reviewctl journal verify` is read-only and
reports continuity, identity, digest, sequence, and compatibility facts. This
integrity envelope is not a signature, trust root, export bundle, or proof of
federation.

Review dimensions are versioned metadata, not model claims. Common dimensions
are `correctness`, `architecture`, `security`, `privacy`, `financial`, `fiscal`,
`release`, `public-api`, and `ui-accessibility`. Project and profile
configuration can require dimensions; a review request may add more with
`--dimension`. Custom dimensions use the bounded `custom.<slug>` namespace.
Receipts record the canonical requested set and an explicit coverage object:
`observed` is empty and `unresolved` contains the requested set until a future
contract declares an independently verified observation. Findings and status
views can filter by dimension because the journal facts and rebuildable
projection retain the metadata.

The project journal remains local-first. Two machines may independently append
events for the same explicit `ProjectId`, but reviewctl does not yet merge,
sign, import, deduplicate, or resolve those streams. Signed allow-listed
exchange and optional storage/distribution remain a later federation layer.

## Identity

A project journal is bound to `OrganizationId + ProjectId`. The owning
organization assigns ProjectId. Paths, remotes, mirrors, repository renames,
and worktrees are aliases rather than project identity. A fork receives a new
ProjectId only when it becomes independently governed; lineage can name its
parent and source version.

Every future ReviewEvent must carry an EventId, exactly one project or
organization origin scope, an origin sequence, schema version, payload digest,
continuity proof, classification, actor, timestamps, and causal links. Reusing
an EventId with different bytes is invalid.

## Evidence and observability

Evidence capabilities are stated separately:

- `snapshotIntegrity`: reviewctl hashes the exact locally frozen input bytes.
- `reviewDeclaration`: where required, the model declares frozen basenames and
  reviewctl compares the declaration with the packet.
- `invocationManifest`: reviewctl records the command or sanitized request it
  assembled for a transport.
- `providerRequestObserved`: reviewctl directly observed the provider-native
  request; this is unavailable for opaque intermediary CLIs.

None of these capabilities proves cognition, attention, semantic understanding,
or correct reasoning. A finding becomes confirmed evidence only after
independent reproduction or equivalent verification.

## Retention and trust

Append-only metadata does not override privacy or legal obligations. Corrections
use supersession. Sensitive EvidenceBlobs may be cryptographically erased under
an authorized retention event while the minimum lawful audit statement remains.

Federation transfers signed, allow-listed facts rather than database access.
Trust roots bind OrganizationId to authorized signing keys, validity periods,
bundle classes, rotation, compromise, and revocation rules. A valid signature
from an unbound or unauthorized key is rejected. Published-stream continuity is
verifiable, but no bundle claims completeness over a private origin journal.

See [ADR 0001](adr/0001-append-only-journals.md),
[ADR 0002](adr/0002-signed-federation-bundles.md), and the
[evidence contract](EVIDENCE.md).
