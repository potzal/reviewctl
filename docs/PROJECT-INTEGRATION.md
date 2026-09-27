# Project Instruction Integration

`reviewctl` is a reusable review system. It is not a product component. Its
repository owns the review receipt contract, transports, privacy-policy format,
and neutral synthetic fixtures. Model auditions, operating rosters, provider
measurements, and tournament evidence belong to a private evidence store.

Interactive assistants such as `pi` are a separate exploration lane. They may
help shape a question or exercise a local REPL, but `reviewctl` remains the
owner of formal review execution, receipt verification, and the archived
evidence packet. See [Pi and reviewctl](PI-INTEGRATION.md) for the handoff.

The [agentic work direction proposal](AGENTIC-CONTROL-PLANE-DIRECTION.md)
discusses a possible provider-neutral exact-head review policy in response to
PR #19. It is **not adopted** and does not waive the required evidence below
or any stricter project/organization merge gate. PR #19 remains open pending
an explicit replacement decision.

## Ownership

| Location | Owns | Does not own |
| --- | --- | --- |
| Workstation or organization policy | Review behavior and evidence requirements | Model rosters or a product standard |
| `reviewctl` | Receipt format, transport behavior, policy format, and neutral synthetic fixtures | A consumer's domain rules, secrets, current qualifications, or tournament results |
| Project `AGENTS.md` / `CLAUDE.md` | Domain invariants, data boundary, review triggers, commands, and required evidence | Model prices, historical rankings, provider routes, or a copied roster |
| Project architecture docs | Decisions and evidence that are specific to that project | A second generic AI-team manual |

The current operating roster is private evidence, not a permanent instruction.
Consult the organization's evidence repository when selecting a specialized
review lane.

`reviewctl init` deliberately creates an inert default profile with no routes
and local execution. Initialization is not model selection: organization policy
or an explicit project-local configuration step must add an approved route
before a review can run. The generated file is therefore never an operating
roster and cannot send source remotely by default.

Projects may state when review is required and which commands to run, but they
must not embed Kiro model tables. Runtime model inventory, prices, credits,
provider commands, qualification, and tournament evidence stay outside project
instruction files.

## Minimal Project Contract

Keep this block small and adapt only the bracketed local details:

```markdown
## Review

For material changes to [domain invariants, security boundary, distributed
behavior, or public API], run the repository's verification commands and a
persisted `reviewctl` receipt. Record the reviewed commit/diff,
included files, manifest, and which findings were independently verified.

Review the change against [canonical standard or local architecture document].
Model output is advisory: tests, source inspection, and production-equivalent
runtime evidence decide the outcome. Follow the organization's review policy and
this guide; do not add a local model roster.
```

For a UI change, add the project's real visual checks: keyboard flow, a11y
checks, screenshots at required breakpoints, and a persisted visual review
receipt. The selected vision model must have been qualified for that lane; a
text-only tournament does not qualify a vision model.

## Migration Procedure

1. Keep the project-specific rules: standards, privacy limits, test commands,
   deployment constraints, and who verifies external claims.
2. Remove generic model tables, costs, rankings, provider-specific CLI snippets,
   Kiro model tables, historical audition reports, and mandatory multi-model
   vote rules from `AGENTS.md` and `CLAUDE.md`.
3. Replace them with the minimal project contract above.
4. Put new model evidence in the organization's private evidence repository,
   not in `reviewctl` or the consuming project.
5. Do not edit copies in linked worktrees or vendored dependencies. Update the
   canonical repository; refresh or remove stale worktrees separately.

`AGENTS.md` and `CLAUDE.md` may both exist for tool compatibility, but one must
be a symlink or a short pointer to the other. They must not diverge.

## Required Evidence

A merge decision records:

1. the immutable commit or diff being reviewed;
2. the exact tests, linters, type checks, and integration checks run;
3. a non-empty persisted receipt with its manifest accepted by the transport;
4. independent confirmation or rejection of every material model finding; and
5. for UI, screenshots and interaction/a11y evidence in addition to code review.

No model may approve, merge, or replace source verification on its own.
