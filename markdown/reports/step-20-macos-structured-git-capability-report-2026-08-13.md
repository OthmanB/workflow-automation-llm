# Step 20: macOS Path-Scoped Writes And Structured Git Report

**Date:** 2026-08-13  
**Status:** Implemented; non-live verification complete

## Outcome

Step 20 replaces model-authored Git staging, commits, revision claims, evidence
metadata, and verification claims with dispatcher-owned authority.

Executors now receive exact `writable_paths`, may write files only within that
scope, do not run acceptance checks, and return
`dispatcher.executor_proposal.v2`. The dispatcher validates the dirty worktree,
runs the configured isolated checks, derives evidence hashes and sizes, creates
an exact-path commit when required, and emits the authoritative
`dispatcher.executor_result.v1` used by review and forwarding.

## Implemented Boundaries

- Normalized plan schema v2 requires strict repository-relative writable scopes.
- Executor OpenCode policies deny test runners and all Git mutation commands.
- Structured Git uses an isolated temporary index to calculate the candidate
  tree and stages only the exact sorted validated path set in the real index.
- Author/committer identity is explicit configuration with no environment or
  repository fallback.
- System/global config, hooks, signing, prompts, editor, and pager behavior are
  disabled; dangerous local process-executing Git configuration is rejected.
- SQLite schema v7 durably records proposal, checks, intent, staging, final
  commit/no-commit state, and reconciliation state.
- Same-repository worktree batches reject overlapping child writable scopes.
- Reviewers receive only dispatcher-generated revisions, evidence metadata, and
  authoritative verification.

## Crash Recovery

`dispatcher recover` recognizes a durable `STAGED` structured Git record. After
confirming the recorded worker is no longer active, it may adopt only a clean
`HEAD` whose parent, tree, changed path set, configured author/committer identity,
deterministic message, assigned worktree, and checked evidence exactly match the
durable intent. Adoption builds and persists one authoritative result without
rerunning Git mutation or verification.

Every mismatch moves the record to `RECONCILIATION_REQUIRED`. Recovery never
automatically runs `git reset`, `git clean`, staging, commit, or retry.

## Verification

Focused structured Git, recovery, and state tests:

```text
56 passed
```

Schema and contract tests after regeneration:

```text
83 passed
```

Complete non-live suite:

```text
473 passed, 10 deselected in 87.70s
```

Package-wide Ruff passed. MyPy reported no issues in 33 source files.
`git diff --check`, `pip check`, strict `pip-audit`, wheel/sdist build, and strict
Twine checks passed. Ten live tests collect successfully; they were not executed
because no `DISPATCHER_REAL_DISPOSABLE` or live model environment variables were
present.

## Remaining Gate

Real T2.2a remains gated on the final package checks and the intended live macOS
heterogeneous model matrix from a committed revision. This implementation does
not enable MCP access; exact method-level MCP capabilities remain separate work.

## Post-Review Hardening

The review of the uncommitted Step 20 worktree produced four hardening changes:

- Repository inspection now runs through one hardened Git boundary (system and
  global config disabled, fsmonitor and hooks neutralized, external
  diff/textconv disabled) and durably fingerprints executor-sensitive Git
  metadata (`config`, info exclude/attributes, hooks, HEAD, refs) so executor
  mutation of Git configuration or helpers is rejected before verification or
  commit. Ignored files are listed and new out-of-scope ignored writes are
  rejected, so exclude rules cannot hide unauthorized writes.
- Result application now persists the result payload, step transition,
  forwarding, review row, and structured Git final state in one transaction.
  A durable `COMPLETED` dispatch is recoverable by `dispatcher recover` and by
  sequential-continuation startup without rerunning Git, verification, or a
  model.
- Verification output bounding no longer limits files written inside the
  disposable workspace and no longer uses `preexec_fn`, keeping batch-threaded
  execution safe.
- Verification observations are bound to the exact dirty snapshot and candidate
  tree: mutation between proposal inspection, verification, staging, commit,
  or adoption is rejected, and adoption compares the durable dirty snapshot,
  metadata, and evidence against the adopted commit.
