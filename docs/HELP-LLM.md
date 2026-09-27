# reviewctl Help for LLMs

Use `reviewctl explore` for resumable Pi conversations and product or
architecture exploration. Use `reviewctl run` for a bounded formal review.

For the project-local product path, use `reviewctl init`, `reviewctl review`,
`reviewctl status`, `reviewctl findings`, and `reviewctl doctor`. This path is
backed by the same Pi transport and writes a project journal plus verifiable
receipts:

```bash
reviewctl init --project .
reviewctl doctor --project . --format json
reviewctl review --project . --profile default \
  --prompt "Review the selected change and return actionable findings." \
  --file src/example.py --format json
reviewctl verify .reviewctl/reviews/<review-id>/receipt.json
```

The project config is private by default. `privacy_mode = "private"` permits
an explicitly configured remote route; `privacy_mode = "sensitive"` rejects
remote profiles and requires a local route. `doctor` only inspects config and
the local Pi executable; it does not authenticate, call a model, or print
credentials. Do not put API keys or model inventories in `reviewctl.toml`.

Project routes are best-effort and ordered. A timeout, empty response,
transport failure, or incomplete contract may use the next route. Valid
findings from an incomplete response are retained as partial evidence and
passed to the next bounded completion attempt. They do not become an accepted
review until a later response satisfies the contract. Inspect `attempts`,
`fallbackRelationships`, and `diagnostic` in the receipt instead of retrying
blindly.

`max_attempts` is a per-route retry allowance, capped at three. A profile with
two routes and `max_attempts = 2` can therefore make at most four bounded
attempts; each attempt and transition is recorded in the receipt.

Pi-backed profiles accept `thinking = "off"`, `"minimal"`, `"low"`, `"medium"`,
`"high"`, `"xhigh"`, or `"max"`. The value is forwarded to Pi and recorded in
request evidence. It controls reasoning effort and does not enforce an output
token limit; the default is `minimal`.

The project-scoped GitHub pull-request flow uses Pi by default and also
registers Codex. It freezes the PR snapshot, reuses the project profile, and
creates a local publication plan. Select a profile with a `codex:MODEL` route
to use Codex; its snapshots are staged outside the project and the Codex
source-root sandbox proves the files it reviewed:

```bash
reviewctl github review --repo OWNER/REPO --pr NUMBER --project PATH \
  --profile default --format json
```

For a formal review through another direct backend, use the transport entry
point explicitly and verify its receipt:

```bash
reviewctl run --review-id ID --transport codex --model MODEL \
  --prompt-file FILE --file SOURCE
reviewctl verify RECEIPT.json
```

An unavailable project-scoped receipt is a configuration/transport diagnostic,
not an approval. Do not treat `reviewctl doctor`'s global profile listing as
evidence that every profile is registered in the project API.

Project findings are a rebuildable projection over the append-only journal.
Repeated observations reuse a stable `findingId`; they do not create duplicate
current findings or reopen a finding that is already `fixed`, `verified`,
`dismissed`, or `disputed`. Status decisions are explicit and auditable:

```bash
reviewctl findings --project . --status open --format json
reviewctl findings set-status --project . \
  --id finding-<stable-id> --status fixed \
  --reason "patched in commit abc123" --format json
```

Allowed statuses are `open`, `disputed`, `fixed`, `verified`, and `dismissed`.
An invalid transition or unknown ID returns `invalid_request` with exit status
2. The command appends `finding_status_changed`; it never rewrites the journal.

Project diagnostics use stable codes and exit statuses: `invalid_request`,
`config_invalid`, or `route_invalid` → 2; `transport_unavailable`, `timeout`,
`empty_response`, or `contract_failed` → 3; `privacy_denied` → 4; and
`receipt_invalid` or `journal_corrupt` → 5. Diagnostics are deliberately safe:
they never include prompts, source, credentials, or raw provider responses.

## Exploration

```bash
reviewctl explore start --id ID --model MODEL --cwd PATH --prompt "QUESTION"
reviewctl explore resume --id ID --prompt "NEXT QUESTION"
reviewctl explore show --id ID
reviewctl explore promote --id ID --output PATH
```

Exploration sessions are resumable. Every turn retains `request.md` and
`turn.json`; Pi events, responses, stderr, and session state exist only when Pi
actually produces the corresponding content. Runner failures such as a missing
executable are recorded in `turn.json:diagnostic`. A response is exploratory
working material, not an approval. The default tool set is read-only:
`read,grep,find,ls`.
Selecting `bash`, `edit`, or `write` explicitly expands that boundary and must
be a deliberate local decision.

## Formal review

```bash
reviewctl run --review-id ID --transport TRANSPORT --model MODEL \
  --prompt-file FILE --file SOURCE
reviewctl verify RECEIPT.json
```

## Provider-backed transport canary

`setup check` only probes local installation state. Before depending on a
configured profile, make one bounded synthetic request through its real route:

```bash
reviewctl transport-canary --profile NAME
```

The canary freezes a tiny file, requires a strict `findings-json` declaration,
and writes `transport-canary.json` only beside one valid receipt. It records
the selected profile, routes, config digest, receipt reference, and result. It
does not modify profiles, routes, policies, credentials, or provider settings.
An unavailable canary is a transport diagnostic, not qualification or approval.

For formal routes, `MODEL` must be qualified by the organization's private
policy and evidence store. This public guide intentionally contains no model
roster, prices, provider-specific invocation commands, or credentials.

For local experimental work, a private policy may authorize a transport's
runtime-owned model inventory without duplicating a model list:

```toml
[transports.kiro]
source_allowed = true
allow_unresolved_identity = true

[transports.gemini]
source_allowed = true

[transports.pi]
source_allowed = true

[transports.codex]
source_allowed = true
```

An exact `[models."MODEL_ID"]` entry overrides its transport default. This
scope is still advisory and does not authorize OpenRouter or make a backend a
merge gate. The policy file and digest remain part of the receipt.

Gemini, Pi, and Kiro proprietary routes require an explicit policy. Codex
retains its existing policy-optional proprietary route for backwards
compatibility; when a policy is supplied, its exact model and transport entries
are enforced for Codex too.

A formal result requires `receipt.result` to be `accepted`, `acceptedAttempt`
to name the accepted attempt, successful receipt verification, and independent
checking of material findings. A missing, empty, unavailable, rejected, or
unverified receipt is not an approval. Hash verification alone proves receipt
integrity, not acceptance.

For machine-readable guidance:

```bash
reviewctl help-llm --format json
```

## Partial review results

Contract evaluation happens only after the transport, timeout, model,
provider, empty-response, and conversation pre-gates succeed. Its status is:

- `complete`: the whole typed contract is valid; only a complete eligible
  attempt can become `acceptedAttempt`.
- `incomplete`: the whole contract is not valid, but one or more complete
  findings can be preserved for bounded fallback.
- `invalid`: no finding can be safely preserved, or contract evaluation could
  not complete. It never promotes fragments.

Fallback receives revalidated fragments and a completion manifest bound to the
target contract. It never receives the raw response, inherits approval, or
treats absence as a dispute. The legacy view remains bound to the real accepted
attempt. The consolidated view keeps partial or unconfirmed findings visible,
so it can be stricter than the legacy approval.

`maxAttempts` is a per-route limit. A route fallback starts the destination
route's own bounded allowance; it does not extend either route indefinitely.

## Local backend setup

Setup diagnostics are local, read-only, and non-qualifying. They inspect the
current machine's registered backend executables with version-only probes.
Setup diagnostics observe only executable presence and version for registered
executable backends. Setup diagnostics never authenticate, call a model or
provider, or write configuration. They do not create files. Diagnostic values
are bounded and credential-shaped values are redacted.

```bash
reviewctl setup discover --format json
reviewctl setup show --format json
reviewctl setup check --backend NAME --format json
```

`discover` and `show` print the same observed topology and return success.
`check` accepts repeatable `--backend` options; without them it checks every
local executable backend. Availability is not qualification. An executable can
be `available` while its review qualification remains `unqualified`.

Remote API backends may execute providers or models remotely, but setup never
credential-probes them. They have local availability `not-applicable` and do not
fail an unfiltered check of local executables. Explicitly selecting one reports
its non-qualifying state and exits `1`. Missing or unverified selected
executables also exit `1`.

## Kiro backend

Kiro is supported by `run`, routes, and tournaments as a registered native
agent-CLI adapter, but it remains unqualified. Availability and a valid receipt
do not qualify a model. The organization owns qualification and its private
operating evidence.

The executable defaults to `kiro-cli`; set `KIRO_BIN` to override it. This
version-only check is local discovery and does not call a model or provider:

```bash
reviewctl setup check --backend kiro
```

Kiro owns the current runtime model inventory. Query the installed CLI, choose
an exact returned model ID, and pass it explicitly:

```bash
kiro-cli chat --list-models --format json
reviewctl run --review-id ID --transport kiro --model MODEL_ID \
  --prompt-file FILE --file SOURCE
reviewctl run --review-id ID --route kiro:MODEL_ID \
  --prompt-file FILE --file SOURCE
```

The supported selection forms are `--transport kiro --model MODEL_ID` and
`--route kiro:MODEL_ID`. `auto` is rejected because the resolved identity is
unobservable. Do not copy the returned model roster, prices, credits, or
provider commands into repository or project instruction documents.

Kiro currently supports only `--response-contract findings-json`. Other
contracts fail before artifacts or source transmission because terminal-rendered
document, verdict, and product output cannot be separated from Kiro UI framing
without rewriting possible model content.
The adapter forces a dumb, no-color terminal and accepts the Kiro response
boundary only at byte zero; a banner or later prompt-like line is not a
response boundary. It rejects invalid UTF-8 or ANSI inside the JSON payload
instead of repairing it; raw stdout is still retained.

The adapter reuses the user's local Kiro subscription and login. It does not use
OpenRouter and does not inherit ambient provider, AWS, or API-token variables.
It uses a disposable controlled working directory, reduced environment, and a
workspace-local `reviewctl_readonly` agent with no tools, allowed tools, MCP
servers, inherited MCP configuration, or resources. The request manifest
retains that exact agent configuration and digest. The frozen packet travels
over standard input; inventory, invocation, and session recovery share one
total timeout. Those controls are advisory read-only and tool controls with
source isolation unavailable; they are not OS sandbox enforcement.

If Kiro reports `Tool approval required but --no-interactive was specified`,
do not retry with `--trust-all-tools`. That would expand the formal review
boundary. Use the native `reviewctl run --transport kiro` path, which sends the
frozen packet inline and explicitly trusts no Kiro tools; then inspect the new
`attempt.json` and verify its new receipt.

Proprietary Kiro source requires both policy decisions below for the requested
model before bytes are sent:

```toml
[models."MODEL_ID"]
source_allowed = true
allow_unresolved_identity = true
```

The second setting is an explicit waiver because Kiro does not expose a
resolved model identity. `reviewctl` records that waiver in the receipt; it does
not qualify the backend or prove which model executed. Synthetic runs require
neither the policy nor the waiver. Potzal and federation remain unrelated and
optional.

An accepted Kiro receipt means the response passed `findings-json`; it is not a
qualified merge approval. The receipt records
`extension.backendQualification = "unqualified"` and
`extension.mergeGateEligible = false`. Merge automation must reject that flag,
and `reviewctl verify` rejects a Kiro receipt if either value is missing or
changed. Humans or a later qualified reviewer may still use the advisory
findings.
Legacy schema-v1 receipts that claim the Kiro transport fail verification;
Kiro receipts require schema v2 and its backend-qualification fields.
For proprietary routing that includes Kiro, `reviewctl verify` also requires
`extension.kiroUnresolvedIdentityWaiver = true`.

Handle Kiro agent errors as follows:

- Unknown or unlisted model: rerun
  `kiro-cli chat --list-models --format json` and select the exact returned ID.
- `auto`: select an explicit model ID.
- Missing or malformed session or inventory: inspect the attempt evidence and
  do not treat the result as approval.
- Missing executable: run `reviewctl setup check --backend kiro` and correct
  `KIRO_BIN` or the local installation.

## Gemini CLI backend

Gemini CLI is a separate registered transport from Antigravity (`agy`):

```bash
reviewctl setup check --backend gemini
reviewctl run --review-id ID --transport gemini --model MODEL_ID \
  --prompt-file FILE --file SOURCE --source-class synthetic
```

The adapter uses the installed `gemini` CLI with headless JSON output,
`--approval-mode plan`, `--sandbox`, and a disposable working directory. The
frozen packet is supplied over standard input; no source path is passed to the
model command. The response JSON, session identifier, statistics, request, and
stderr are retained as attempt evidence.

Gemini may resolve a requested alias to a different model. The receipt records
the requested model and keeps the CLI's observed model statistics in raw
evidence; it does not claim resolved model identity or qualification. The CLI
does not provide a portable output-token cap, so
`outputTokenLimitEnforced = false` is recorded. A valid or accepted Gemini
receipt is advisory and is not merge-gate approval.

For local experimental proprietary work, the private policy must explicitly
authorize the transport before source bytes are sent:

```toml
[transports.gemini]
source_allowed = true
```

The direct Gemini CLI transport is not the same as `agy`, which remains the
Antigravity transport used by existing product-review routes.

## Diagnose failures

Errors are actionable for LLMs. Use the receipt fields instead of guessing or
retrying blindly:

- Incomplete: inspect `completionRequest`, `fallbackRelationships`, and
  `rawResponse`.
- If completion fails with `original prompt collides with completion framing`,
  remove the reserved `<reviewctl-completion-context>` marker (opening or
  closing form) from the original prompt and start a fresh bounded review.
- Invalid: inspect `violations`, `evaluationError`, and `rawResponse`.
- Accepted: inspect both the legacy and consolidated views, then run
  `reviewctl verify`.

`rawResponse` identifies the retained bytes by durable absolute path, SHA-256, and
character count. A missing response and a present empty response are different
facts.

Do not retry blindly. A failed formal run normally still prints its artifact
directory and persists `receipt.json` plus `attempts/NN/attempt.json`.

Read these fields in order:

1. `result` identifies delivery or acceptance failure, such as `timeout`,
   `transport-failed`, `model-mismatch`, `provider-mismatch`, `empty`,
   `missing-conversation`, or `incomplete`.
2. `diagnostic` is a bounded, redacted transport message. Never reconstruct or
   request credentials from a redacted value.
3. `validationError` explains the stable CLI contract boundary.
4. `contractEvaluation.violations` gives machine-readable `findings-json`
   violations such as `invalid-json`, `review-declaration`, `finding-path`,
   `verdict-invariant`, or `prepared-contract`.
5. `evidence` names the retained request, response, Pi event stream, session, or
   stderr files that exist for that attempt.

For a Codex `transport-failed` attempt, inspect its private `evidence.request`
and `evidence.stderr`, then `attempts/NN/stdout.log` and `output-state.json`
before retrying. The request manifest redacts the inline prompt; the output
state records only whether Codex produced a final-response file before
cleanup. A macOS isolation or authentication preflight error is a transport
failure, not `invalid-json` or evidence that the model reviewed the files.
If `reviewctl verify` reports `receipt-source` for a literal-prompt receipt,
the private turn-level `prompt.txt` or its bytes are missing/changed; restore
the original source from a trusted backup or run a fresh review. Do not edit
the old receipt or invent a source path.

Exit status `1` means unavailable or invalid evidence, not approval. Exit status
`2` means the command, config, policy, or local input was rejected before a
review could run. Correct the named cause, create a new receipt, and run:

```bash
reviewctl verify RECEIPT.json
```

Never edit an old receipt to make it pass verification.

Project receipts use a local SHA-256 checksum to detect accidental corruption;
it is not a signature and does not establish trust against a writer who can
rewrite the receipt. Signed federation bundles are separate future work.
Receipt v1 verification remains digest-only. Receipt v2 verification also
checks its structure offline, including attempt identity, fallback provenance,
promotion, accepted-attempt binding, and consolidation. Verification does not
call a model or provider.

reviewctl is local-first. Registered adapters can still be unqualified;
availability is not qualification. Cursor and Claude Code are not claimed as
supported backends. BAML is an architectural inspiration only, with no runtime
dependency. Editable formal execution, project evidence-store integration,
and federation are deferred. Federation is optional future work, and Potzal is
not a dependency.
