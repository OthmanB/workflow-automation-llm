# Dispatcher Phase 3 Execution Report

**Execution date:** 2026-08-10
**Plan:**
[`markdown/plans/dispatcher-remediation-plan-2026-08-09.md`](../plans/dispatcher-remediation-plan-2026-08-09.md)
**Scope:** Transactional state, leases, correlated artifacts, recovery, and
explicit state-management operations.

## Status

SQLite is now the authoritative persistence boundary for the Phase 3 runtime
APIs. It uses private files, WAL, `synchronous=FULL`, foreign keys, generation
checks, and explicit schema migration tracking. JSON checkpoints, Markdown
transcripts, and JSONL audit exports are derived artifacts rather than recovery
authorities.

The existing supervisor loop remains mock-only and is intentionally not yet
wired to launch real dispatches through these APIs. Phase 4 must make each
validated dispatch transition use the store before enabling real execution.

## Implemented

- `src/dispatcher/state_store.py` provides atomic run snapshots across run,
  normalized-plan, step, dispatch, session, review, artifact, operator-decision,
  lease, audit-event, and tool-version tables.
- Snapshot writes use optimistic generations; a forced error rolls back every
  affected table. Corruption and unsupported future schemas fail with recovery
  guidance.
- Run and resource leases contain owner ID, PID, host, run ID, acquisition, and
  heartbeat timestamps. A stale lease needs an explicit approval reference;
  replacement is never silent.
- Prepared dispatch inputs store the prompt and policy beside the immutable
  intent. Recovery classifies `PREPARED`, `RUNNING`, `COMPLETED`, and `FORWARDED`
  attempts without retrying uncertain work.
- Transcript writes use sequence plus UUID names and exclusive creation.
  Correlated audit events and deterministic run/audit exports are derived from
  SQLite.
- `start`, `resume`, `recover`, and `answer` CLI commands persist or inspect
  explicit run identities without creating sessions or executing OpenCode.
- `state` configuration now requires heartbeat and stale-lease intervals, with
  strict validation that the stale threshold exceeds the heartbeat interval.

## Verification

```text
ruff check src tests
All checks passed!

mypy src
Success: no issues found in 21 source files

pytest
125 passed

python -m build
Successfully built dispatcher-0.1.0.tar.gz and dispatcher-0.1.0-py3-none-any.whl
```

Fault tests cover migrations, corruption, generation rollback, a separate
process contending for a lease, stale-lease approval, unresolved dispatch
classification, operator-answer persistence, transcript collisions, and
derived audit/tool-version records.

## Remaining Phase 3 Work

- The mock-only loop still writes legacy derived state and retains interactive
  `input()` handling. Phase 4 replaces it with plan-driven dispatches that use
  the SQLite authority.
- The loop does not yet acquire a lease before supervisor bootstrap/resume,
  normalize repository resource IDs, inspect stale sessions during recovery, or
  persist every dispatch `RUNNING`/`COMPLETED`/`FORWARDED` transition through a
  single execution facade. Those integration tasks remain unchecked in the
  remediation plan.
- Real OpenCode execution remains disabled. Hosted GitHub Actions cannot be
  verified because this workspace is not a Git repository.
