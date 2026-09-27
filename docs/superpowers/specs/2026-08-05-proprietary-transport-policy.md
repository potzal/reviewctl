# Proprietary Transport Policy

> Historical proposal, not current policy or receipt contract. Current
> behavior is documented in [Evidence](../../EVIDENCE.md) and
> [Project integration](../../PROJECT-INTEGRATION.md). In particular,
> prompt-only receipts now include a path-and-hash prompt source entry rather than
> the empty `source.files` list requested below. Do not use the old verification
> item 6 as an acceptance gate for current code.

## Problem

`reviewctl` records a policy decision per model through `source_allowed`, but
the runner rejects proprietary packets for the direct OpenRouter and native
Antigravity transports before it reads that decision. This makes an approved
model unusable and turns an organization-owned policy into a hard-coded
provider ban.

## Decision

Every configured model may run on every built-in review transport: `llm`,
`openrouter`, `agy`, and `codex`. A policy is optional, non-blocking receipt
metadata.

The runner does not deny a packet because its policy is absent, lacks a model
entry, or marks `source_allowed = false`. The existing operational safeguards
remain required:

- an explicit policy file and its digest in the receipt
- a persisted request/response attempt receipt
- rejection of empty or invalid provider responses

Every supported response contract remains available. Synthetic prompt-only
reviews may omit `--file`; their receipt records an empty file list.

## Compatibility

Existing policies need no changes. When supplied, a policy digest remains in
the receipt regardless of its `source_allowed` values. A later, stricter
enforcement mode can be added explicitly; it is not enabled now because it
would keep current review work blocked.

## Verification

Tests will prove:

1. a proprietary packet without a policy runs and records `policy.sha256: null`;
2. a proprietary packet with a non-authorizing policy runs and records its
   policy digest;
3. a proprietary OpenRouter packet invokes the transport and records
   its receipt;
4. a proprietary Antigravity packet invokes the transport and
   records its receipt;
5. a proprietary packet accepts a non-findings contract;
6. a synthetic prompt-only packet records an empty file list;
7. the full test suite passes.
