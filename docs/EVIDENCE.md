# Evidence Contract

## Receipt contents

The project API's `receipt.json` is a project-review checkpoint. It is canonical JSON for its local
integrity envelope, but it is not a canonical V1/V2 review receipt, formal evidence, or merge-grade
evidence. Verify it only with `verify_project_receipt`; that check covers the checkpoint digest (and,
when supplied, the digest returned by the same process).

The GitHub publication artifact is `github-review-receipt.json`. It is a canonical V2 review receipt and
must pass the global `reviewctl verify` command before publication. It contains the review ID, frozen
source provenance, file SHA-256 digests, prompt and policy digests, requested and resolved models,
attempt outcomes, token counts, latency, provider cost when available, structured findings, and a
receipt digest.

When `--response-contract document` and `--output-file` are supplied, the accepted Markdown response
is also written as a human-readable working document. The receipt binds that document by path,
character count, and SHA-256; unavailable or incomplete runs do not produce a document.

`reviewctl verify` recomputes the receipt digest. A rebase, altered finding, or altered source
provenance invalidates the prior receipt and requires a fresh review. The policy digest is the exact
policy bytes used for that historical decision: changing the policy later does not alter an existing
receipt, but a new review records the new policy digest.

Every attempt distinguishes an absent backend response from a present response. A non-null response is
retained even when it is empty or rejected, under the attempt directory. `rawResponse` records its
durable absolute path, SHA-256, and character count. This durable raw evidence supports diagnosis; it is never
copied into a completion prompt and never qualifies a rejected response for fragment promotion.

Receipt schema versions have deliberately different verification guarantees. V1 verification is
digest-only: it preserves the historical integrity check without retroactively imposing new structure.
V2 verification is structural and offline: after checking the digest, it validates attempt numbering,
contract identity, gates and evaluation state, fallback relationships, promoted-fragment identity, the
accepted attempt, and consolidation without calling a provider or model.

The project-oriented API also writes a file named `receipt.json` for caller compatibility, but that
artifact is a `project-review-checkpoint`, not a canonical V1/V2 receipt or merge-grade evidence.
`verify_project_receipt` checks its internal digest and can bind it to the expected digest returned in
the same process; it does not establish canonical receipt structure. Global `reviewctl verify` rejects
marked and recognizable historical project checkpoints instead of selecting that weaker checker from
their complete historical field signature. A single coincidentally named field remains compatible with
legacy V1. A completely rewritten unsigned document can still masquerade as legacy V1 after its author
removes a project-owned field and recomputes the digest; this is another reason V1 is integrity
compatibility, not provenance or authentication.

Project checkpoint history also lacks V2 attempt evidence. Therefore formal promotion fails closed when
a checkpoint represents multiple attempts; it cannot be promoted into a canonical or merge-grade review
artifact. Create and verify a canonical V2 receipt instead.

The receipt SHA-256 is tamper detection, not a digital signature and not a trust root. Structural
verification detects internally incompatible facts, including mixing native findings state into a legacy
contract receipt. It does not prove who authored the receipt or defend against an actor authorized to
rewrite every fact and recompute the digest; signatures and organizational trust policy remain separate
concerns.

## Sealed audit payload

The runner never copies source files into its artifact directory. It writes private temporary snapshots
only for the lifetime of the model process; `snapshotIntegrity` receipt provenance hashes those exact
bytes locally. Passing
`--seal-to` stores the exact assembled request and a sealed copy of the raw model response as
Age-encrypted payloads.
SQLite transport databases are deleted after extraction, including when Age sealing fails, because they
contain raw prompt and response data. The Codex transport similarly deletes its `--output-last-message`
file and any temporary JSON schema after extraction; the receipt keeps the Codex session identifier,
response hash, and validated structured findings.
For a literal `run --prompt` with no input files, the turn instead retains a private `prompt.txt`
as its synthetic source. Its receipt records that path and hash. Offline `reviewctl verify` validates
the receipt's structure and digest, **not** the current bytes or existence of `prompt.txt`. Compare
the retained file with the recorded SHA-256 separately if source integrity is required. Treat the
turn directory as sensitive.

Each Codex attempt retains a private `request.json` (command arguments with the inline prompt
redacted and requested model/contract; launched attempts also record a prompt hash), bounded
`stdout.log` and `stderr.log`, and
`output-state.json` recording whether the final-response file existed before cleanup. These
diagnostics survive an isolation/preflight or process failure; the transient response/schema and
isolated home are still removed. The presence of logs or an output file is not a valid review or
source-read proof. The receipt points to the request and stderr evidence where available, and
the other files are in the same `attempts/NN` directory. A preflight failure
has `command: null` and no prompt hash because no Codex command was assembled
or sent.

For proprietary Codex reviews on macOS, the runner also creates a temporary `CODEX_HOME` with only the
authentication file required by Codex and applies `sandbox-exec` rules that deny reads and writes to
every original review source root and deny writes throughout the invoking user's real home. This proves
that Codex cannot reopen the reviewed checkout after snapshotting or write into the user's home. It is
not a whole-host sandbox claim: paths outside the source roots and real home remain outside this boundary.
Codex's internal seatbelt is bypassed only for this externally sandboxed path, because macOS does not
permit nested seatbelt application; synthetic and non-isolated Codex runs retain `--sandbox read-only`.

On timeout, `reviewctl` discards any partially written Codex final message before validation. A timeout
can record duration and transport failure, but it can never contribute a partial verdict or finding to a
receipt.

The structured findings contract accepts exactly two outcomes: `approved` with no findings, or
`changes-requested` with one or more findings. A model that reports unavailable context, refuses to
read the files, or returns an ambiguous natural-language verdict produces an unavailable receipt rather
than an approval.

For typed findings, contract evaluation is `complete`, `incomplete`, or `invalid`. Promotion is allowed
only from an eligible incomplete attempt and only for findings that independently pass the full finding
validator. Invalid responses and responses rejected by a pre-gate promote nothing. Completion receives
validated fragments and a target-bound gap manifest, not the raw response. It cannot inherit a prior
verdict or approval, and absence of a finding in a later response is not a dispute.

acceptedAttempt must identify a real complete accepted attempt. The legacy verdict and findings are
bound to that attempt. Unconfirmed findings remain visible in the consolidated view with provenance;
they are not silently discarded merely because a later response is complete. Consequently the
consolidated approval invariant is stricter than the legacy accepted-attempt view.

For transport contexts that require a `reviewDeclaration`, the structured response lists every frozen
basename it declares it reviewed. `reviewctl` compares that set exactly with the frozen packet. The
model never supplies the authoritative source hashes; the runner records them from local bytes.

The receipt also distinguishes the `invocationManifest` assembled by reviewctl from a
`providerRequestObserved` request. Direct transports can persist a sanitized provider-native request.
An opaque intermediary CLI can expose only the invocation reviewctl controlled unless it returns
stronger observation evidence. The packet digest identifies reviewctl's intermediate assembled prompt;
it does not by itself attest what a remote provider received.

Snapshot integrity, a matching declaration, and an observed request are useful but narrower claims.
None proves cognition, attention, semantic understanding, correct reasoning, or correctness of a
finding. Model findings remain proposals until independently reproduced or otherwise verified.

Evidence repositories retain the visible receipt and sealed payloads. Their commits must be signed by
the owning organization. Release signing and reproducible-build provenance apply to the `reviewctl`
distribution itself, not to a model's opinion.

## Retention and sharing

The organization defines retention in its evidence repository. Cross-organization syntheses should use
an abstract taxonomy, for example `idempotency uniqueness violated`, never source snippets or business
identifiers. A human approves every vault entry after secret and PII scanning.
