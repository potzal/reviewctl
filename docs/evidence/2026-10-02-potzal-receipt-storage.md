# Potzal receipt storage: local evidence

Date: 2026-10-02. Reviewctl base: `30a9617fb9d96d13fd89e89b2e2af4327006bb58`.
Potzal initial native-API baseline: `ab05839817b716984e774b3a36eda3e0ecc9ba2a`.
Final build/canary source: `32be8af051fc833c6ab10bf890ad59f2c32e77b8`.
The intervening Potzal changes are docs/tests/CI, not native server code.
Scope: optional native-object byte adapter in a feature checkout. No Amelia
deployment, release, new federation implementation or formal review receipt.

## Verification

- Baseline deterministic selector: 2216 passed, 1 deselected.
- Initial local `uv run pytest -m 'unit or contract' --junitxml=test-results/junit.xml
  --durations=20 --cov=reviewctl --cov-branch --cov-report=term-missing`:
  **2452 passed, 2 deselected**, 100% coverage of 8665 statements and 3050
  branches. Threshold and exclusions unchanged.
- Initial focused adapter/config/CLI/byte-verifier suite: 282 passed.
- Final unchanged deterministic CI selector: **2484 passed, 2 deselected**,
  100% coverage of **8770 statements and 3062 branches**, in 211.43 s on macOS
  CPython 3.14.6. Threshold and exclusions unchanged. This is local execution
  of the CI selector, not a remote GitHub check.
- Final focused adapter/CLI suite: **238 passed**, 100% statement/branch
  coverage of 331 statements and 70 branches, with the same threshold.
- Final Linux adapter/CLI suite: **238 passed** on CPython 3.14.7 in 26.26 s.
  It used the pre-existing `python:3.14-slim` Docker image with network disabled,
  read-only repository/dependency mounts, a temporary `/tmp`, no added
  capabilities, no privilege escalation, disabled plugin autoload and no
  bytecode/cache writes. This is focused Linux evidence, not a full Linux suite
  or a deployed shared service.
- `uv run ruff check .`, `uv run ruff format --check .`, `git diff --check`:
  passed.
- `uv build`: source distribution and wheel built successfully.

The regression sequence observed missing byte-verifier/command behavior before
implementation, a missing bounded-read descriptor argument, and a missing
journal recovery action before their fixes. CI on `80fd802` then exposed two
native-stack-dependent nested JSON tests and a machine-local path in this public
document. Run `37018044917` failed; the initial local pass is not a CI claim.
The nested JSON tests now exercise native and real stdlib Python decoding in a
bounded-stack worker; valid native outcomes need not falsely raise recursion
errors. The worker restores its stack configuration, and both platforms pass.
The public guide now uses a portable placeholder instead of a machine path.
Real HTTP/TLS tests exercise slow upload, TLS handshake, headers and bodies,
conflict budget sharing, trusted roundtrip, rejection of an untrusted
certificate/mismatched hostname, descriptor/timer cleanup, and late DNS return
without starting an upload. Test-input mistakes (a matching certificate CN and
a platform-dependent non-listening port) were corrected without changing TLS
verification or excluding tests. Address fallback separately models a
deterministic OS refusal before a real slow HTTP connection.

## Real local Potzal canary

Build: `cargo build --locked -p potz-cli --bin potz` in the tested Potzal checkout.
Final binary SHA-256:
`cc2391af2e18f730527f81cac700684233d76eca18841bd1e69850ffe961c0fe`.
Run:

```sh
REVIEWCTL_TEST_POTZ_BINARY=/absolute/path/to/potzal/target/debug/potz \
  uv run pytest tests/test_potzal_receipt_canary.py -m live -s
```

Result: 1 passed. The canary used an ephemeral service, temporary checkout
directories, synthetic local credentials and a synthetic legacy unavailable
receipt. No model/provider call was made; no accepted review was manufactured.

- Object digest:
  `sha256:11185d0076e45d8ebbf9f39ddfac119d9fbc190f7982c60ea5a2bd594ad27533`.
- Exact receipt-byte SHA-256:
  `f0b8bbf618a117d51227775f832f586da6902fb7304ccec245c78a2a9f5bf1a8`.
- Identical bytes across two checkouts, repeated publication reconciliation,
  canonical receipt verification and local journal verification: passed.
- Wrong project association: rejected by the client after authorized service
  retrieval. This is not server-enforced project isolation.
- Wrong credential: rejected by the real Potzal daemon.
- The canary repeated on the final source with identical artifact digest.

## Delivery state

Candidate PR: [#31](https://github.com/potzal/reviewctl/pull/31).
Implementation corrections are separately committed as
`58c1ea1b73fd60b005672258fba8d7846c52e750`; documentation records the reviewed
source hashes below. Main integration and a real two-machine/Amelia run are
separate from these local checks. PRs #12/#19 are not closed or replaced by this
optional storage increment.

## Independent advisory reviews

Astra architecture sidecar (`01a0fcd5-1dcd-7830-b48f-84b2d9a9dd16`) inspected
Potzal's actual source and recommended the existing native object API. It
identified provider-wide authorization, missing raw-CAS release revocation and
the 409/CAS-byte-versus-catalog distinction; the design documents those limits.

Independent Astra spec reviewer (`01a0fce7-4f5f-7783-9a34-1f98ff7a2724`) inspected
the diff/new files against the bounded design. It found one P2: inherited
journal diagnostics could omit `next` after successful storage. A failing
regression reproduced it. The handler now preserves existing guidance or adds
a journal-specific recovery action while retaining storage status/digests.
The reviewer rechecked four in-memory cases and reported spec compliant, with
no remaining scope findings. This was an advisory source review, not a formal
receipt or full test execution by that reviewer.
It also inspected the later path/deadline fixes and confirmed the bounded scope,
non-approval semantics, native API permission limits and documented DNS caveat.

Independent Astra quality reviewer (`01a0fcee-68bc-7d23-b188-a94b1e7e4654`)
reproduced a P1 disclosure through project-directory replacement during network
access and a P2 inactivity-timeout overrun. The path fix pins original project
and state identities and uses descriptor-relative persistence. A follow-up P2
found that an output pathname replaced after opening could still be advertised
as verified; post-write directory/file identity checks correct that case.
Eight automated replacement regressions failed before their fixes and pass now.
The reviewer independently ran 19 CLI tests and 16 after-open replacement cases
without disclosure or descriptor leaks, and separately exercised HTTP/TLS
deadline, certificate/hostname and cleanup behavior. Its final disposition on
the source hashes below was ready for the bounded files, with no remaining
findings. This is an advisory source/runtime review, not a formal receipt or
merge authorization. Local proof, remote CI and main integration remain distinct.

## Verified source identities

SHA-256 of the reviewed implementation files at the final local test snapshot:

| File | SHA-256 |
| --- | --- |
| `src/reviewctl/receipt_store.py` | `c8e8be237bc9cc3b2881440f860f1bf46c78f0dfb5b2df9adde37b02466a836e` |
| `src/reviewctl/receipt_store_cli.py` | `2d9fcca59eb59b687bdd1a156bbdceb7c6ef56b4ff9225eab1ea21049b06c773` |
| `src/reviewctl/config.py` | `cb6868196ba441345516d12d304d2ae59faafd61e949cb695c467f2c23790ecd` |
| `src/reviewctl/cli.py` | `1720191b24b5f4dd3a7d4e00a1a6b1b6c81f5e0c3b99f62a76739bedef38690c` |

Read [the integration guide](../POTZAL-INTEGRATION.md) before configuring a
shared service. Server project isolation, revocation-aware retrieval, signed
exchange, discovery and live Amelia proof are independent pending increments.
