# Test Layers

`reviewctl` separates deterministic implementation checks from environment and
provider checks. A receipt is evidence of one model invocation; it is neither a
test result nor merge approval by itself.

## Deterministic PR gate

Every deterministic test is classified as `unit` or `contract`. Pull requests
run both classes on Ubuntu with a locked dependency set, coverage, JUnit output,
and the twenty slowest test durations:

```bash
uv run pytest -m "unit or contract" \
  --junitxml=test-results/junit.xml --durations=20 \
  --cov=reviewctl --cov-branch --cov-report=term-missing
```

`unit` tests exercise local parsing, policy, receipt, and filesystem behavior.
`contract` tests exercise CLI boundaries, frozen packets, fake provider
executables, process lifecycle, and GitHub-facing adapters without using an
account, network provider, or repository secret.

## Synthetic canary

The scheduled and manual `Synthetic canary` workflow invokes the installed
controller through a disposable fake `llm` executable, writes a receipt, and
runs `reviewctl verify` on it. It exercises the end-to-end receipt path without
provider credentials, source transmission, or model spend.

Run it locally with:

```bash
uv run python scripts/synthetic_canary.py
```

This deterministic fake is distinct from a provider-backed profile canary. The
profile canary makes one real synthetic request through a user-configured
route, then writes `transport-canary.json` only when the resulting
`receipt.json` is valid:

```bash
reviewctl transport-canary --profile NAME
reviewctl verify /path/to/receipt.json
```

It is an operability observation for that profile at that time, not model
qualification or approval evidence. It never updates routes, policies, or
credentials.

## Platform and live checks

New tests that require an operating-system feature belong to `@pytest.mark.platform`.
Provider, account, repository, or secret-dependent checks belong to
`@pytest.mark.live`. They are deliberately excluded from PR CI. Run them only
with the explicit marker and record the environment, command, result, and any
receipt in the owning evidence repository.

```bash
uv run pytest -m platform
```

There are no generic `live` tests because provider credentials, acceptable
source boundaries, and expected outcomes belong to each organization's private
evidence store. No successful command alone establishes merge approval. Merge
approval requires the project policy's required tests and independently verified
review findings.

## Bounded verifier evidence reads

Version 0.3.4 bounds each descriptor-confined byte or text read to 64 MiB by
default. Oversized regular files are rejected from `fstat` before allocating a
read buffer; reads request at most the limit plus one byte and reject observed
size changes before JSON parsing or digest acceptance. FIFO and symlink rejection
continues to use descriptor confinement and nonblocking opens. The Python API's
`max_bytes` keyword is for explicitly trusted callers; receipt metadata and CLI
inputs cannot raise the limit. Existing callers, including configuration,
checkpoints, SQLite snapshots and repository prompt/response evidence, use the
default. Artifacts over that limit now fail closed.

Run the deterministic regressions without provider credentials:

```sh
uv run pytest tests/test_filesystem.py tests/test_run_repository.py
```

The fixtures include a sparse file larger than 64 MiB, a file that grows after
the initial descriptor-size check, shrinking files, exact/empty boundaries,
invalid overrides, FIFO/symlink rejection, and normal repository receipts. This
is a per-read byte bound, not a cumulative-memory quota, path authorization
allowlist, process sandbox, immutable-file guarantee, or model qualification.
