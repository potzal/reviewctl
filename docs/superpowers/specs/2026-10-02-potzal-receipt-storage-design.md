# Optional Potzal receipt storage

Status: implemented and locally verified on the feature branch; independent
quality review and main integration remain separate gates.

## Goal

Publish a canonical review receipt as an immutable project-associated artifact,
retrieve its exact bytes from another checkout, and run the existing offline
receipt verifier. Review execution remains local-first. Configuration is opt-in.

## Existing seams and choice

Source baseline: reviewctl `30a9617`, Potzal `ab05839`.

Potzal's local `potz publish/pull` operates on the local CAS. Its OCI adapter
currently restricts image config and layer media types. The existing native
object API accepts arbitrary bytes: POST `/v1/objects` with `Digest`,
`Potzal-Namespace`, and `Potzal-Release`, and GET `/v1/objects/<digest>`.
Use this native object API; neither shelling out to a local CAS nor changing the
OCI domain is required for this increment.

The object API currently authorizes `Resource::Provider("local-daemon")`.
Credentials are service-scoped, not project-scoped. The adapter checks project
association before accepting retrieved evidence, but does not promise server
tenant isolation. This first profile is for one owner's trusted evidence store.
Project-scoped server grants and release revocation are later work: raw object
GET checks CAS integrity and current read authorization, not release tombstones.

## Artifact and verification

An artifact is deterministic JSON with `artifactKind`, `schemaVersion`, stable
`projectId`, the SHA-256 of the exact receipt bytes, and their Base64 encoding.
No receipt fields or historical bytes are rewritten. Its external CAS digest
binds the whole envelope. The publisher selects the project association; this
is not an authenticated assertion by the receipt's original author.

Accept only canonical V1/V2 receipts under the existing global verification
rules. Reject project checkpoints; never promote them by storing them. V1
verification remains digest-only, V2 remains structural and offline. No extra
source, prompt, response, log, or journal files are needed by that verifier.
Receipts themselves may contain paths or findings and must be treated as private
evidence. Publication is an explicit command to an explicitly configured store.

## Commands and configuration

`reviewctl receipts push RECEIPT --project PROJECT` publishes and records an
immutable storage observation in the local project journal. `reviewctl receipts
pull sha256:DIGEST --project PROJECT` retrieves, verifies, saves under
`.reviewctl/shared/`, and records a retrieval observation. Both commands have
`--format text|json`; failures carry stable diagnostics and a next action.

Configure `[evidence.potzal]` in `reviewctl.toml`: `endpoint`, `namespace`,
`token_env`, and optional `timeout_seconds`. An explicit `[project].id` is
required. The publication namespace appends the SHA-256 of the project ID to
the configured namespace; release name is `0.0.0+receipt.<artifact-sha256>`.
Credentials are read from the named environment variable, never written to
config, artifacts, journal or diagnostics. HTTPS is required except literal
loopback HTTP for local testing. Redirects are rejected.
One monotonic network budget spans active socket I/O and the conflict GET.
Synchronous operating-system DNS resolution can delay return beyond the budget;
expiration is checked before sockets open, and no late background upload is
started. This limitation must remain visible rather than claiming a hard
end-to-end wall-time guarantee.

Repeated publication accepts a 409 only after fetching the requested CAS object
and comparing its exact bytes. This proves the artifact remains retrievable;
it does not attest a release-catalog entry. Transport failure after a POST may
leave the remote artifact stored; repeating the command reconciles it.

Retrieval checks bounded response size, requested object digest, artifact schema,
project ID, embedded receipt byte digest and canonical receipt verification
before writing a private file. Existing local bytes are reused only if identical.
Writes remain descriptor-confined to the original project/state identities;
replacing either pathname during network access cannot redirect persistence.
Journal observations record storage facts, never review acceptance or approval.
An existing journal is only appended to; no imported journal events are replayed.

## Acceptance and scope

Automated tests cover round-trip bytes, repeated upload, project mismatch,
corruption, invalid/checkpoint receipts, unauthorized and unavailable stores,
redirect rejection, output confinement, and machine-readable diagnostics.
A local canary uses the real Potzal daemon and two temporary project directories
with the same explicit project ID. This does not claim an Amelia deployment.

Signed federation, trust manifests, journal synchronization, discovery/query,
aggregations, arbitrary attachments, multiuser isolation and deployment remain
separate increments. Potzal is optional; no mandatory runtime dependency is added.
