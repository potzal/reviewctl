# Optional Potzal receipt storage

The `receipts` commands store canonical V1/V2 receipts as immutable,
project-associated artifacts in an explicitly configured Potzal evidence store.
They use the existing native object API. No model call or additional Python
dependency is needed. Ordinary review commands continue to work without Potzal.

## Configure one project

Use the same explicit `project.id` in both checkouts. Machine-local origin IDs
remain distinct. Add this to `reviewctl.toml`:

```toml
[project]
id = "my-project"
privacy_mode = "private"

[evidence.potzal]
endpoint = "https://evidence.example.com"
namespace = "reviews"
token_env = "REVIEWCTL_POTZAL_TOKEN"
timeout_seconds = 30
```

The token value comes from the named environment variable. It is never put in
this file, storage artifacts, the journal or diagnostics. The endpoint must be
an HTTPS origin without URL credentials, path, query or fragment. Literal
`http://127.0.0.1:<port>` and IPv6 loopback are accepted for a local canary.
Redirects are rejected, including redirects on authenticated reads.

`timeout_seconds` supplies one monotonic budget shared by the upload/read and
any conflict-reconciliation GET. Active socket I/O is cancelled at expiry,
including slow headers or bodies; the client does not leave an authenticated
upload running in a background worker. The operating-system DNS resolver is
synchronous and cannot be cancelled portably: it can delay the command's return
beyond the configured budget. Its result is checked before opening a socket,
so an expired resolution cannot start a late upload. This is not a hard wall-time
guarantee for DNS resolution or local CPU/filesystem work.

At the reviewed Potzal baseline (`ab05839`), `/v1/objects` authorizes access to
the **service**, not the project. A namespace does not create a project access
boundary. Use one owner's trusted authorization domain or a dedicated evidence
service; tenant isolation requires a later server contract. Raw CAS retrieval
also does not enforce release tombstones. This adapter does not claim receipt
revocation, project-scoped server grants or a profile-support promotion.

## Store and retrieve

```sh
reviewctl verify /path/to/canonical-receipt.json
reviewctl receipts push /path/to/canonical-receipt.json --project . --format json

# Use the returned objectDigest in another checkout of the same project.
reviewctl receipts pull sha256:OBJECT_DIGEST --project . --format json
reviewctl verify .reviewctl/shared/OBJECT_DIGEST/receipt.json
reviewctl journal verify --project . --format json
```

Replace `OBJECT_DIGEST` with the returned 64-character digest. `push` returns
`status: stored`, the object digest, exact receipt-byte digest and local source
path. `pull` returns `status: retrieved` and the downloaded private receipt
path. These statuses describe storage outcomes, not a review verdict.

The artifact envelope contains the exact Base64-encoded receipt bytes, their
digest and stable project association. The full envelope has its own CAS digest.
The publication namespace appends SHA-256(`project.id`) to the configured base;
its immutable release is `0.0.0+receipt.<object-digest-hex>`. Project association
is selected by the publisher; it does not authenticate receipt authorship.

The current offline canonical verifier needs no external files. Consequently
this adapter does not upload source, raw prompts/responses, logs, sealed
payloads, a project checkpoint or a journal. A receipt itself may still contain
paths, findings and confidential metadata; explicitly select an appropriate
private destination. Receipts up to 4 MiB and artifacts up to 8 MiB are allowed.

Canonical receipt semantics stay unchanged: V1 checks its digest; V2 checks
structure and digest. Project `receipt.json` checkpoints are rejected by the
global verifier and cannot be promoted through storage. Unavailable/incomplete
canonical receipts can be preserved for diagnosis and remain unavailable or
incomplete. A successful retrieval supplies no acceptance, qualification,
signature, organizational trust root or permission to merge.

## Retry and recovery for agents

Use `--format json` to inspect `diagnostic.code`, `retryable` and `next`.

| Code | Exit | Recovery |
| --- | --- | --- |
| `config_invalid` | 2 | Set an explicit stable project ID and valid store configuration. |
| `evidence_store_denied` | 4 | Resolve the named environment credential and its current Potzal authority. |
| `evidence_store_unavailable` | 3 | Check connectivity; retry the same operation and digest when available. |
| `evidence_store_missing` | 3 | Confirm the digest and destination with the publisher. |
| `evidence_store_failed` | 3 | Inspect the indicated protocol/local persistence problem; preserve local evidence. |
| `receipt_invalid` | 5 | Inspect receipt/artifact integrity or project mismatch. Do not treat it as approval. |
| `journal_corrupt` | 5 | Verify and reconcile the journal before retrying. |

Repeated POST receives a conflict from Potzal. The adapter fetches the expected
CAS object and accepts the retry only when all bytes match. This confirms
retrievability, not a matching release-catalog association. A network failure
after publication can leave the remote object stored; a retry reconciles it.
No automatic retry or alternative store is selected.

Every successful command appends a local `receipt_stored` or `receipt_retrieved`
observation. Repeating a command may append another observation of the same
object; it does not create another object. Existing journal facts and canonical
receipt bytes are preserved. If journal recording fails after storage, the
output retains the completed storage status/digest alongside a diagnostic and
nonzero exit. Retrying records a new observation.

Downloaded receipts live below `.reviewctl/shared/<digest>/receipt.json` with
private permissions. A repeated pull reuses identical bytes; differing existing
bytes are preserved and reported as a conflict. No imported finding/lifecycle
events are applied to the local journal.
Download persistence is tied to the original project and state-directory
identities and uses descriptor-relative writes. Replacing either directory
during retrieval fails before writing into the replacement; symlinked output
components and receipt targets are rejected.
The advertised output directory and receipt identities are checked again after
persistence, so a replacement cannot be reported as the verified downloaded
path. A failure preserves the pinned original artifact for diagnosis.

## Local proof and later increments

Build the chosen Potzal checkout, then run this opt-in local canary:

```sh
REVIEWCTL_TEST_POTZ_BINARY=/absolute/path/to/potzal/target/debug/potz \
  uv run pytest tests/test_potzal_receipt_canary.py -m live -s
```

It creates a temporary local Potzal service and two project directories with
the same ID, using synthetic local credentials and a synthetic legacy receipt.
It verifies exact bytes, repeated upload, the canonical verifier, journal
integrity, client rejection of a different project association and server
rejection of a wrong credential. No provider call or Amelia deployment occurs.

Signed bundles, offline trust manifests, journal federation, receipt discovery,
query projections, aggregation and attachments are later increments. See the
[bounded design](superpowers/specs/2026-10-02-potzal-receipt-storage-design.md)
and [implementation plan](superpowers/plans/2026-10-02-potzal-receipt-storage.md).
