# Same-Repository Workspace State Report

**Execution date:** 2026-08-10
**Scope:** First approved same-repository parallelism slice: durable temporary
worktree ownership and Git lifecycle management. Scheduler admission, child
dispatch routing, review/rework, integration merges, and promotion remain
separate work.

## Implemented

- Added explicit YAML worktree policy under `execution.concurrency`:
  `same_repository_mode`, `worktree_root`, and `worktree_branch_prefix`.
- Added durable workspace-group records in authoritative run state with
  `PREPARED`, `ACTIVE`, `CLEANUP_PENDING`, `CLEANED`, and `FAILED` states.
- Added `WorktreeManager` for clean `commit_policy: required` repositories. It
  creates child branches/worktrees from one captured base revision, validates
  branch lineage, and removes only dispatcher-owned worktrees and branches.
- Added `WorkspaceCoordinator`, which persists intent before Git side effects,
  holds a durable repository lease throughout the group, persists cleanup intent
  before removal, and records failure state for recovery.
- Added workspace-group visibility to run reports and JSON status snapshots.

## Safety Limits

- Same-repository scheduler admission remains disabled. Existing batches still
  reject same-repository children.
- Patch-only (`commit_policy: prohibited`) repositories are rejected.
- Non-forced cleanup deletes only merged branches. Force cleanup is explicit and
  reserved for a later recovery/operator design.
- No temporary branch is pushed; worktrees and branches are owned beneath the
  configured root/prefix and are removed after successful cleanup.

## Verification

```text
PYTHONPATH=src .venv/bin/python -m pytest
203 passed, 1 skipped

ruff check src tests
All checks passed!

mypy src
Success: no issues found in 29 source files

pip-audit --strict .
No known vulnerabilities found
```
