# Dispatcher Phase 7 Execution Report

**Execution date:** 2026-08-10
**Plan:**
[`markdown/plans/dispatcher-remediation-plan-2026-08-09.md`](../plans/dispatcher-remediation-plan-2026-08-09.md)
**Scope:** Deterministic dependency scheduling, protocol-v2 batches, and bounded
cross-repository worker concurrency in the mock-only coordinator.

## Status

Phase 7 adds opt-in bounded parallel execution without changing the public
project configuration, which remains `execution.scheduling: sequential`. Real
model-backed execution remains disabled.

## Implemented

- `execution.concurrency` requires explicit global capacity, batch size, exact
  per-worker-role capacities, and a declared `wait_for_started` failure policy.
  Sequential YAML requires a global capacity of one.
- The scheduler exposes deterministic, plan-ordered readiness decisions and
  concrete exclusion reasons for state, operator gates, dependencies, declared
  input producers, and held resource or repository locks.
- Repository keys are dispatcher-wide exclusive resources. Independent steps
  run concurrently only when they target different registered repositories and
  have no common declared resource lock.
- Protocol-v2 `dispatch_batch` validates all children before any persistent
  child record or lease is created. Duplicate steps, unavailable capacity,
  unresolved prerequisites, non-new batch sessions, and resource conflicts fail
  the whole batch.
- A `BatchRecord` correlates independently durable child dispatch IDs. Every
  child receives a dispatch-scoped lease owner, preventing one child from
  releasing another child’s repository or resource lock.
- Prepared children run through the existing process/session/result lifecycle
  in a bounded thread pool. Workflow transitions remain serialized around the
  SQLite authority so concurrent child completion cannot overwrite a newer run
  generation.
- A joined batch forwards every child disposition. Failed children remain
  visible, are recorded in the batch aggregate, and produce one durable
  `batch_reconciliation` operator request after all started children finish.
- `wait_for_started` deliberately performs no automatic sibling cancellation:
  each started child reaches an independently recoverable terminal dispatch
  state, and the operator reconciles the joined batch. Session timeouts retain
  the existing process-group termination behavior.
- Run reports include a durable batch table.

## Verification

The Phase 7 tests cover dependency/input exclusion reasons, deterministic
ordering, role capacity, repository lock conflicts, protocol-v2 child
uniqueness, all-or-none preparation, independent successful child forwarding,
independent failed child persistence, batch join/reconciliation, and the
existing sequential timeout/recovery path.

```text
PYTHONPATH=src pytest
183 passed

ruff check src tests
All checks passed!

mypy src
Success: no issues found in 27 source files
```

## Remaining Boundary

Same-repository parallel execution is intentionally deferred. Creating isolated
worktrees is not sufficient without durable branch ownership, merge, and
dependency-propagation semantics; the current dispatcher therefore preserves a
repository-exclusive lease and permits concurrent work only across independent
repositories.
