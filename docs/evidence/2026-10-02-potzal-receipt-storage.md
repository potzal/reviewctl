# Potzal receipt storage: local evidence

Date: 2026-10-02. Reviewctl base: `30a9617fb9d96d13fd89e89b2e2af4327006bb58`.
Potzal tested source: `ab05839817b716984e774b3a36eda3e0ecc9ba2a`.
Scope: optional native-object byte adapter in a feature checkout. No Amelia
deployment, release, new federation implementation or formal review receipt.

## Verification

- Baseline deterministic selector: 2216 passed, 1 deselected.
- Final `uv run pytest -m 'unit or contract' --junitxml=test-results/junit.xml
  --durations=20 --cov=reviewctl --cov-branch --cov-report=term-missing`:
  **2452 passed, 2 deselected**, 100% coverage of 8665 statements and 3050
  branches. Threshold and exclusions unchanged.
- Focused adapter/config/CLI/byte-verifier suite: 282 passed.
- `uv run ruff check .`, `uv run ruff format --check .`, `git diff --check`:
  passed.
- `uv build`: source distribution and wheel built successfully.

The regression sequence observed missing byte-verifier/command behavior before
implementation, a missing bounded-read descriptor argument, and a missing
journal recovery action before their fixes. The malformed deeply nested JSON
test was corrected after full-suite evidence showed Python 3.14's C decoder
could exceed the Python recursion limit; its final 100000-level input exercises
the real decoder stack guard within the receipt byte bound.

## Real local Potzal canary

Build: `cargo build --locked -p potz-cli --bin potz` in the tested Potzal checkout.
Run:

```sh
REVIEWCTL_TEST_POTZ_BINARY=/Users/luisfernando/Code/workspaces/potzal/target/debug/potz \
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

Independent quality review is being tracked separately until its final
disposition is recorded. Local proof and main integration remain distinct.

## Verified source identities

SHA-256 of the reviewed implementation files at the final local test snapshot:

| File | SHA-256 |
| --- | --- |
| `src/reviewctl/receipt_store.py` | `f218bc2c79da4ccc34360eee81216736551b80faa28637b172640e0e47743a22` |
| `src/reviewctl/receipt_store_cli.py` | `97797b22bc88f292912331370bebeab513b7f5ab29aa26dfce2cf0b4e1146740` |
| `src/reviewctl/config.py` | `cb6868196ba441345516d12d304d2ae59faafd61e949cb695c467f2c23790ecd` |
| `src/reviewctl/cli.py` | `1720191b24b5f4dd3a7d4e00a1a6b1b6c81f5e0c3b99f62a76739bedef38690c` |

Read [the integration guide](../POTZAL-INTEGRATION.md) before configuring a
shared service. Server project isolation, revocation-aware retrieval, signed
exchange, discovery and live Amelia proof are independent pending increments.
