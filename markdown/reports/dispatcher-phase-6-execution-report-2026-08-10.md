# Dispatcher Phase 6 Execution Report

**Execution date:** 2026-08-10
**Plan:**
[`markdown/plans/dispatcher-remediation-plan-2026-08-09.md`](../plans/dispatcher-remediation-plan-2026-08-09.md)
**Scope:** Immutable review policy, deterministic review escalation, durable
operator gates, and measured budget enforcement for the sequential mock-only
coordinator.

## Status

Phase 6 is implemented for the dispatcher-owned sequential coordinator and
deterministic fake OpenCode subprocesses. Real model-backed execution remains

## Implemented

- Activation compiles one immutable `RunPolicy` from the selected profile,
  project review policy, normalized plan, and underspecification mode. The run
  persists the profile and policy digests plus a concrete obligation for every
  step before any worker dispatch.
- Mandatory plan review and project mandatory review are non-waivable. A
  selected profile can only add obligations. An operator can waive only a
  non-mandatory compiled review through a durable, typed decision, while the
  executor evidence remains attached to the accepted step.
- Reviewer roles, acceptance counts, and new-session independence are enforced
  for every proposed review dispatch. Critical balanced profiles and thorough
  profiles require their configured independent reviewers.
- A rework clears prior reviewer acceptances because they applied to the prior
  immutable executor result. Conflicting reviews use an unused reviewer role as
  a fresh tie-break on the same review target. Rework rounds, reviewer attempts,
  executor attempts, and findings remain durable.
- Escalation enters `WAITING_OPERATOR`. A reassignment answer records the
  normalized escalation executor role on the ready step, which the next executor
  dispatch must use. Counters are never reset.
- Risk gates, underspecification, escalation, budget limits, and review waivers
  are typed `OperatorRequest` records with allowed answers, context, expiration,
  and required-role fields. Answers are transactionally correlated with an
  operator decision row and survive process restart.
- `underspec_mode` is frozen into the run policy. Auto mode rejects an
  underspecification request rather than treating it as approval.
- Enabled budgets require measured OpenCode cost and usage. The dispatcher
  accumulates run, step, role, and session usage; checks cost before dispatch;
  checks cost and context after each worker result; and blocks a resumed session
  at its configured context threshold. Limit behavior is explicitly `halt` or a
  durable operator stop decision; unsupported automatic fork/compaction options
  are rejected by configuration validation.
- Forwarded executor and reviewer payloads now contain structured measured usage.
  Run reports include cumulative run and step usage.

## Verification

The Phase 6 matrix covers economy, balanced, thorough, mandatory review,
contradictory multi-review configuration, independent multi-review acceptance,
immutable tie-break targets, rework reset, role-bound escalation, durable risk

```text
PYTHONPATH=src pytest
175 passed

ruff check src tests
All checks passed!

mypy src
Success: no issues found in 26 source files
```

## Remaining Boundaries

- Live OpenCode allow/ask/deny enforcement remains a Phase 2 open item.
- Model-backed execution remains disabled; all verification uses deterministic
  fake OpenCode subprocesses.
- Budget accounting currently covers worker adapter results. A real supervisor
  model budget policy must be added before enabling model-backed orchestration.
- Automatic context compaction and session forking are intentionally not
  available in the sequential mock-only coordinator. Configuration only permits
  fail-closed `halt` or durable operator-decision behavior.
- The private historical baseline remains deferred.
