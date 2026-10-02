# GitHub pull-request reviews

The first GitHub integration is local-first and read-only. It resolves pull-
request metadata and the diff through the local `gh` authentication, verifies
that the checkout is at the exact pull-request head commit, reads bounded file
content from that commit, and then reuses the configured project review flow.
Pi remains a transport; it does not own GitHub identity, acceptance, receipts,
or publication.

## Dry-run command

```bash
reviewctl github review \
  --repo OWNER/REPO \
  --pr 123 \
  --project . \
  --profile default \
  --dimension security \
  --format json
```

The command never writes to GitHub. It produces a canonical V2
`github-review-receipt.json` and a deterministic `publicationPlan` for
inspection. The canonical artifact must pass global `reviewctl verify`; a
project `receipt.json` checkpoint verified with `verify_project_receipt` is not
an equivalent substitute. A successful plan is persisted as
`publication-plan.json` beside the canonical artifact; it is not a published
comment, an approval, or a request for changes.

To request the first supported external side effect explicitly:

```bash
reviewctl github review \
  --repo OWNER/REPO \
  --pr 123 \
  --project . \
  --publish \
  --publish-event comment \
  --format json
```

This can submit one grouped `COMMENT` review only after the canonical V2
`github-review-receipt.json` is accepted and passes global `reviewctl verify`,
and its frozen PR head still matches GitHub's current head. The publisher
reconciles stable finding markers in existing review comments and review
bodies, rechecks the head immediately before and after the POST, and records
stale-head races without retrying. It does not support `approve` or
`request-changes`.

Base SHA and repository visibility are also checked against the snapshot.
Even zero-findings and duplicate-only results require a fresh head check.
After the review, `sourceFreshness` reports whether base/head/visibility still
match; a changed or unavailable identity leaves the historical receipt intact
but makes the publication plan non-executable. Rerun for the current identity.
If identity changes after a POST, `stale_head_race` retains observed publication
IDs: inspect and reconcile those IDs rather than blindly retrying.
Concurrent publishers are not atomically deduplicated; serialize publication
externally when several workers share a PR.

The project profile controls the transport and privacy policy. The command
does not create a GitHub-specific Pi path or bypass the existing fallback,
contract, journal, and receipt behavior.

## What is frozen

The snapshot records:

- normalized `owner/repository` and pull-request number;
- base and head commit SHA;
- public/private visibility;
- changed-file status, path, size, and SHA-256 digest;
- normalized diff digest;
- sanitized source-operation evidence.

The source context copied into `packet.json`, `receipt.json`, and the
`review_started` journal event contains these identities and digests only. It
does not duplicate raw diff, source content, credentials, or provider output.
The bounded changed-file contents and diff are private review input used while
the request is running; temporary materialized files are removed afterward.

## Fail-closed diagnostics

The local source adapter refuses to continue when:

- the checkout `HEAD` differs from the PR head SHA (`github_checkout_stale`);
- GitHub does not prove public/private visibility (`github_visibility_unknown`);
- a private repository is configured as public or `privacy_mode=personal`
  (`privacy_denied`); set `visibility=private` and `privacy_mode=private` or
  `sensitive` before retrying, without weakening the source classification;
- base/head/visibility change during capture or review
  (`github_source_identity_changed`), or there are no reviewable changed files;
- metadata, paths, source encoding, file count, diff size, or file size exceed
  the bounded contract;
- `gh` or `git` fails, times out, or returns malformed data.

Diagnostics are typed and safe for an LLM or automation to consume. They do
not include command stderr, authorization headers, prompts, raw source, or
raw provider responses. Retry only after inspecting the diagnostic and fixing
the source condition; do not treat an unavailable receipt as approval.

Canonical promotion also checks the controller's configuration/project/origin/
privacy binding and exact checkpoint/result findings. It still refuses
multi-attempt checkpoints without bound V2 attempt evidence. A digest-valid
checkpoint is not an authorization or a substitute for that evidence.

## Publication boundary

The plan maps a finding to an inline target only when the path and right-side
line are present in the frozen diff; other findings remain summary-only. The
comment publisher consumes this plan rather than recomputing finding identity
or deciding whether a review was accepted. It fails closed if reconciliation
cannot prove pagination exhaustion, and it never changes finding lifecycle
state.
