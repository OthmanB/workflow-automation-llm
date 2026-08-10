# Workspace Recovery and SQLite Migration Report

**Execution date:** 2026-08-10
**Scope:** Durable workspace-group migration and crash/failure recovery for
same-repository barriers.

## Implemented

- Advanced the authoritative SQLite schema to version 4.
- Added the `workspace_groups` table with run, repository, lifecycle state,
  base revision, integration branch, lease owner, generation, and immutable
  serialized group record.
- Snapshot writes update run state and workspace-group rows in the same SQLite
  transaction.
- Added workspace recovery classification:
  `ACTIVE`, `INTEGRATING`, and `FAILED` require operator reconciliation;
  `CLEANUP_PENDING` requires dispatcher-owned cleanup.
- Updated `dispatcher recover` to display unfinished workspaces alongside
  unfinished dispatches.
- Made cleanup restartable from a durable `CLEANUP_PENDING` state.

## Verification

The migration test downgrades a disposable database to version 2, then proves
the v3 dispatch-payload migration and v4 workspace-table migration both apply.
Workspace tests close and reopen SQLite after active provisioning and after
cleanup intent, verify the recovery disposition, and complete cleanup without
manual Git deletion.

## Safety Limit

Recovery never retries an incomplete workspace batch or force-deletes branches
automatically. The dispatcher records the state needed for an operator to
reconcile or explicitly request cleanup.
