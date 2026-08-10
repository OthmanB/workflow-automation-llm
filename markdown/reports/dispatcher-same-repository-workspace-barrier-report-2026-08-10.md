# Same-Repository Workspace Barrier Report

**Execution date:** 2026-08-10
**Scope:** Same-repository scheduler admission, child execution/review routing,
deterministic integration, and temporary Git cleanup for the approved
`commit_policy: required` worktree barrier.

## Implemented

- `dispatch_batch` recognizes independent same-repository executor children
  when `same_repository_mode: worktree_barrier` is configured.
- The scheduler rejects duplicate steps, dependencies, overlapping declared
  resources, active workspace groups, capacity violations, patch-only
  repositories, and non-fresh child sessions.
- Each child executes in its own temporary Git worktree from one durable base
  revision. Executor retries and reviewer dispatches route back to that same
  child worktree.
- The dispatcher waits for every child result and acknowledgement. Once every
  child step is accepted, it creates one temporary integration worktree, merges
  child branches in plan order, fast-forwards the source default branch once,
  and removes child/integration worktrees and branches.
- A merge failure leaves the source branch unchanged, records the workspace
  group as failed, and requires reconciliation before explicit cleanup.

## Limits

- Patch-only repositories and same-repository children with conflicting
  resources remain rejected.
- Worktree branches are local only and never pushed.
- Automatic force cleanup after failed integration is intentionally absent;
  reconciliation must preserve potentially useful child work before removal.
- Provider-stall cancellation/retry policy remains separate work.

## Verification

```text
PYTHONPATH=src .venv/bin/python -m pytest
206 passed, 1 skipped

ruff check src tests
All checks passed!

mypy src
Success: no issues found in 29 source files
```
