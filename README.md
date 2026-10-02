# reviewctl

Evidence-backed, organization-neutral control plane for bounded LLM code review.

`reviewctl` is owned as one shared tool, but it never owns a product's evidence, credentials,
or policy. Each organization runs the same CLI against its own policy file and private receipt
repository. It uses `llm` as a transport, not as the source of review governance.

## Install

Python 3.14 or newer is required.

```bash
uv tool install git+https://github.com/potzal/reviewctl.git
```

For local development, run `uv sync --all-groups` followed by `uv run pytest`.

## Optional runtime tools

The core CLI is Python-only and does **not** require Node.js. Transports and
evidence features call external tools only when selected:

| Feature | External requirement |
| --- | --- |
| `llm` transport | `llm` with the configured provider plugin |
| Codex transport | Codex CLI; macOS `sandbox-exec` for proprietary-source isolation |
| OpenRouter direct transport | `curl` and `OPENROUTER_API_KEY` |
| Gemini/Antigravity transport | `agy` |
| Gemini CLI transport | local `gemini` CLI; unqualified and advisory |
| Sealed evidence | `age` |

Missing optional tools produce a typed unavailable attempt; they never count
as an accepted review.

Canonical receipts can optionally be stored in a configured Potzal evidence
service with `reviewctl receipts push` and recovered with `reviewctl receipts
pull`. See [Potzal integration](docs/POTZAL-INTEGRATION.md) for project identity,
configuration, commands and the current service-scoped permission boundary.

## Run a review

For the simpler project-local workflow, initialize once, explicitly add an
organization-approved route to `reviewctl.toml`, and then use the project front
door:

```bash
reviewctl init --project .
reviewctl doctor --project . --format json
reviewctl review --project . --profile default \
  --prompt "Review this change and return actionable findings." \
  --file src/example.py --format json
reviewctl findings --project . --status open
```

The generated profile is deliberately local and route-free, so initialization
cannot send source to a provider. Project reviews keep a private append-only
journal under `.reviewctl/`, try explicitly configured routes, preserve
validated findings from incomplete attempts, and write compatibility
checkpoints. Those checkpoints are not canonical V1/V2 receipts and global
`reviewctl verify` rejects them. Use the `run` command below for persisted,
offline-verifiable formal review evidence.

The project journal deduplicates repeated observations by stable finding ID.
Use the lifecycle command to record a human or CI decision without rewriting
history:

```bash
reviewctl findings --project . --status open --format json
reviewctl findings set-status --project . \
  --id finding-<stable-id> --status fixed \
  --reason "patched in commit abc123" --format json
```

Supported statuses are `open`, `disputed`, `fixed`, `verified`, and
`dismissed`. The command appends a `finding_status_changed` event; it never
edits an earlier journal line. Re-observing a finding updates its observation
count but does not reset its lifecycle status.

```bash
reviewctl run \
  --review-id payment-idempotency \
  --prompt-file review-request.md \
  --model openrouter/moonshotai/kimi-k2.7-code \
  --file src/posting.py \
  --file tests/test_posting.py \
  --file migrations/004_posting.sql \
  --source-class proprietary \
  --policy org-policy.toml \
  --response-contract findings-json \
  --seal-to age1auditrecipient...
```

Each model attempt receives a fresh SQLite log database. The runner snapshots each selected file
before invoking a model and records its digest in the receipt provenance. A model declares only the
frozen snapshot paths it reviewed; it never supplies an unverifiable hash. A receipt is accepted only
when the persisted response is non-empty, complete, tied to the requested model, and tied to a
persisted conversation. Finding paths must name an attached file exactly. With `--seal-to`, the exact
request and response are encrypted with Age; the visible receipt contains only hashes, provenance,
result metadata, and structured findings.

Every run also writes a rotating JSONL runtime log. It records route selection, attempt result,
provider, duration, exit code, and a redacted diagnostic; it never records prompts or review-file
contents. By default it is `<artifact-root>/reviewctl.log`, with five 5 MiB backups. Use
`--log-file /path/to/reviewctl.log` to place it elsewhere. The receipt records the configured log
path and rotation policy.

`reviewctl verify` checks receipt integrity; it does not convert an unavailable
receipt into an accepted one. Automation must also require `result: accepted`
and a non-null `acceptedAttempt` before treating a run as formal review evidence.

To use a review as working documentation, use `--response-contract document` and add
`--output-file docs/reviews/<name>.md`. The accepted Markdown response is written there and the
receipt records its path, SHA-256, and character count. Failed or incomplete attempts never
overwrite the document. `findings-json` remains for merge gates; `product-review-json` remains for
structured product proposals.

For provider outages or account quotas, declare an ordered fallback route explicitly:

```bash
reviewctl run \
  --review-id ledger-review \
  --prompt-file review-request.md \
  --route agy:gemini-3.7-flash-high \
  --route openrouter:google/gemini-3.6-flash \
  --route llm:openrouter/deepseek/deepseek-v4-flash \
  --file src/posting.py \
  --source-class proprietary \
  --response-contract findings-json
```

Routes are tried in order only after a retriable delivery failure (`timeout`, transport error,
missing/empty response, or incomplete contract). An accepted review stops the route chain. Do not
declare a cross-provider route implicitly: the route list is part of the receipt, so the privacy and
cost decision remains visible and auditable.

For routes used repeatedly, keep them in a user-local TOML profile instead of repeating them on every
command. The default file is `~/.config/reviewctl/config.toml`:

```toml
[profiles.gemini]
routes = [
  "agy:gemini-3.7-flash-high",
  "openrouter:google/gemini-3.6-flash",
]

[profiles.code]
routes = [
  "codex:gpt-5.6-luna",
]

[profiles.openrouter-flash]
routes = [
  "openrouter:deepseek/deepseek-v4.1-flash",
]
reasoning_effort = "low"

[defaults.codex]
timeout_seconds = 600
max_attempts = 2
```

Pi-backed profiles may also set `thinking` to `off`, `minimal`, `low`, `medium`,
`high`, `xhigh`, or `max`. The selected level is forwarded to Pi and recorded
in request evidence; it controls reasoning effort, not an output-token cap.
The default is `minimal`.

OpenRouter routes use the separate `reasoning_effort` setting. Its supported
values are `low`, `medium`, and `high`; it is omitted from the request unless
selected. Use `--reasoning-effort low` on `reviewctl run` to override the
profile for one invocation. The effective OpenRouter request, including its
reasoning field when present, is retained as attempt evidence. `thinking`
remains a Pi-specific control and does not alter OpenRouter requests.

For a direct GLM route, pass `--reasoning-effort low` explicitly (or select a
profile that sets it). The same flag is forwarded by formal `range-review` to
every chunk. Do not treat an HTTP-success response with an empty `content`
field as a clean review: some providers can otherwise consume the entire
output budget in internal reasoning. Such a receipt is `unavailable`, not an
approval; inspect the persisted request before retrying.

Select a profile with `--profile gemini`. A profile cannot be combined with `--model` or
`--route`; the receipt records the profile name, config path, config SHA-256, and execution settings.
`defaults.<transport>` applies to direct `--transport` invocations and profile runs when the
profile does not override a setting. Explicit `--timeout-seconds` and `--max-attempts` values take
precedence. This makes a quota fallback or a long-running Codex review easy to use while keeping
the exact routing decision reviewable. The receipt records the config digest and effective settings.

`--policy` is optional review metadata. When provided, its SHA-256 is retained in the receipt; it does
not block a model, transport, or response contract. `policy-check` is advisory by default; use
`policy-check --enforce` only for a deliberate privacy gate.

The receipt also records the effective `executionSettings`, including the timeout and attempt limit
after applying profile values and any CLI overrides.

### Provider-backed transport canary

`setup check` only inspects a local executable. Before assigning a profile to a
formal review, exercise it through a single synthetic, provider-backed packet:

```bash
reviewctl transport-canary --profile gemini
```

The command freezes a tiny `canary.py`, requires the exact `findings-json`
approval declaration for that file, makes one bounded attempt, and writes a
canonical `receipt.json`. When exactly one valid receipt is produced, it also
writes `transport-canary.json` beside it with the profile name, config digest,
routes, receipt path, receipt digest, and result. It never changes a profile,
route, policy, credential, or provider setting. A missing or invalid receipt
produces no canary report; an unavailable-but-valid receipt remains a transport
diagnostic, not a qualification result.

### Interactive exploration with pi

Use the integrated exploration flow for product ideas, architecture questions,
repository research, and iterative review preparation:

```bash
reviewctl explore start \
  --id ledger-product-ideas \
  --model openai-codex/gpt-5.6-sol \
  --cwd ~/Code/workspaces/ledger \
  --prompt "Explore the product direction and identify questions we should validate."

reviewctl explore resume \
  --id ledger-product-ideas \
  --prompt "Now compare those ideas with the current repository and propose a bounded next step."

reviewctl explore show --id ledger-product-ideas
reviewctl explore promote --id ledger-product-ideas --output /tmp/ledger-product-review
```

Explorations are named, resumable Pi sessions. The default tool set is read-only
repository inspection (`read,grep,find,ls`); pass `--tools` explicitly when a
different capability set is appropriate. Selecting `bash`, `edit`, or `write`
deliberately expands the local execution boundary. Each turn stores its request and
turn manifest under `~/.cache/reviewctl/explorations`; Pi event, response, stderr,
and session artifacts are retained only when Pi actually emits or creates them.
Runner-generated failures remain in the turn manifest's `diagnostic` field.

`promote` creates `prompt.md`, `exploration.md`, and a manifest for the formal
handoff. The exploratory response is working material, not an approval or a
substitute for frozen source files. Run `reviewctl run` separately with the
bounded source files, response contract, privacy policy, and receipt
verification required for a formal review. For a bounded headless Pi attempt,
use `--route pi:<provider/model>`; that transport remains separate from these
full-tool sessions. See [Pi and reviewctl](docs/PI-INTEGRATION.md).

Pi's current CLI does not expose an output-token cap. Formal Pi request evidence
therefore records the requested value separately with
`outputTokenLimitEnforced: false`; do not treat that value as a budget guarantee.

### Gemini/Antigravity product review

```bash
reviewctl run \
  --review-id obc-product-gemini \
  --transport agy \
  --model gemini-3.7-flash-high \
  --prompt-file product-review.md \
  --file product-brief.md \
  --source-class proprietary \
  --response-contract product-review-json
```

Do not pass `--provider-only`, `--provider-order`, or other `--provider-*` options to `agy`; those
configure OpenRouter routing only.

### Prompt-only synthetic product review

```bash
reviewctl run \
  --review-id product-ideas \
  --transport agy \
  --model gemini-3.7-flash-high \
  --prompt-file product-review.md \
  --source-class synthetic \
  --response-contract product-review-json
```

Synthetic prompt-only rounds intentionally omit `--file`. The receipt still
records the prompt as a source entry with a path and SHA-256 hash. Offline
`reviewctl verify` checks the receipt's digest and structure; it does **not**
re-read the prompt file to compare its current bytes. With `--prompt-file`, the
source path is the supplied file; with a literal `--prompt`, a private
`prompt.txt` is retained in the turn directory. Neither is a reviewed code
file, and neither supplies Codex source-read proof.

## Commands

```bash
reviewctl verify receipt.json
reviewctl transport-canary --profile NAME
reviewctl policy-check --policy org-policy.toml --model <model-id>
reviewctl policy-check --policy org-policy.toml --transport kiro --model <model-id> --enforce
reviewctl tournament --plan /path/to/organization-tournament.toml
reviewctl tournament --plan /path/to/organization-tournament.toml --case <case-id>
reviewctl provider-preflight --plan /path/to/provider-comparison.toml
reviewctl blind-package --report organization-tournament-artifacts/tournament.json \
  --output council/public-proposals.json --mapping-output council/restricted-identity-map.json
reviewctl council-plan --plan /path/to/organization-tournament.toml \
  --blind-package council/public-proposals.json --mapping council/restricted-identity-map.json \
  --output council/assignments.json
```

Use Codex through the same receipt flow:

```bash
reviewctl run \
  --review-id bounded-change-codex \
  --transport codex \
  --model <approved-codex-model> \
  --prompt-file review-request.md \
  --file src/posting.py \
  --source-class proprietary \
  --policy org-policy.toml \
  --response-contract findings-json
```

For proprietary reviews on macOS, the Codex transport creates an ephemeral `CODEX_HOME`, copies only
the required Codex authentication file, and uses `sandbox-exec` to deny reads and writes against the
original source roots and writes throughout the invoking user's real home. Codex works from frozen
snapshots rather than the checkout. This is not a claim that an LLM process is isolated from every host
system path. The transport records the
Codex session identifier and removes the plaintext final message after extracting its hash and
validated findings. Because Codex cannot nest its own macOS seatbelt inside `sandbox-exec`, the
proprietary path uses Codex's external-sandbox bypass flag; the outer profile remains the enforced
source-read and home-write boundary. Codex `*-pro` availability depends on the organization's Codex account; use a successful
synthetic qualification before making a role mandatory.

The tournament command accepts only synthetic cases by policy. It estimates the maximum spend from
the assembled packet and attached file bytes before each request, then stops before crossing the
configured budget. A candidate may override the plan's `max_output_tokens`; the runner reserves that
candidate's requested cap and records both `requestedMaxOutputTokens` and `maxOutputTokens` alongside
the estimate and actual provider cost when available. `outputTokenLimitEnforced` makes clear whether the
transport actually enforces that ceiling. Tournament requests preserve the
declared output-token cap; they do not silently increase it for a model name.

For an isolated OpenRouter provider comparison, run `provider-preflight` immediately before the
tournament. It snapshots the live endpoint catalog, checks the pinned provider, declared price, active
status, `response_format`, and `structured_outputs` support, then records the snapshot beside the
receipts. The tournament still attests the resolved provider on every response; catalog metadata is not
proof that a provider can complete a particular structured request.

OpenAI/Codex reviews are an approved independent lane, run through the organization's Codex account
and retained in that organization's evidence repository. `reviewctl` currently controls the portable
`llm` packet transport; it does not route proprietary source through OpenRouter by default.

For coding agents and local automation, `reviewctl help-llm --format json`
returns machine-readable commands, failure results, contract violations, and
recovery guidance. The same material is available in
[Help for LLMs](docs/HELP-LLM.md).

Read [the architecture and canonical vocabulary](docs/ARCHITECTURE.md),
[the current project handoff and roadmap](docs/HANDOFF.md),
[the council policy](docs/COUNCIL.md), [evidence contract](docs/EVIDENCE.md),
[Pi and reviewctl integration](docs/PI-INTEGRATION.md),
[project-instruction integration guide](docs/PROJECT-INTEGRATION.md), and
[tournament guide](docs/TOURNAMENT.md) before adding the tool to CI. Model
qualification, operating rosters, provider experiments, and retained receipts
belong to the organization's private evidence store.
