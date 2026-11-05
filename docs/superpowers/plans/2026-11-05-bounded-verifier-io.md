# Bounded verifier file reads

Approved scope: retain descriptor confinement and impose a 64 MiB default per
regular-file read. Inspect descriptor size before allocation, read at most the
limit plus one byte, and reject oversized or size-changing inputs before parsing.
Only trusted Python callers may explicitly choose another limit; receipt metadata
never selects it. Apply the same contract to text reads. No backend calls, Rust
changes, installation changes, or new dependencies.

- [x] RED: sparse oversized input, growth/shrink after size inspection, exact and
  empty boundaries, explicit override validation, FIFO/symlink regressions.
- [x] Implement bounded reads and patch release metadata.
- [x] Check normal repository prompt/evidence and all affected callers.
- [x] Run focused and reasonable deterministic full tests plus Ruff.
- [x] Document native qualification limits and freeze exact file hashes.

Verified locally on Linux with Python 3.14: 2682 deterministic tests passed,
1 skipped and 2 deselected; branch coverage 100%. Focused callers passed 175
tests before the final two repository-evidence regressions; final filesystem
and repository set passed 72. Ruff and offline lock validation passed. Native
artifact rebuild/qualification remains the integrator responsibility; no
installed package, provider, backend service or previous artifact was changed.
