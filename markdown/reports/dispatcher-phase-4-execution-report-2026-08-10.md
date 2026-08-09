# Dispatcher Phase 4 Execution Report

**Execution date:** 2026-08-10
**Plan:**
[`markdown/plans/dispatcher-remediation-plan-2026-08-09.md`](../plans/dispatcher-remediation-plan-2026-08-09.md)
**Scope:** Normalized bootstrap, plan-driven sequential state transitions,
reviewer verdicts, completion, step-local retries, and historical baseline APIs.

## Status

`src/dispatcher/sequential.py` is the validated Phase 4 workflow facade. It
uses only normalized plans, strict supervisor commands, typed worker results,
compiled policies, durable dispatch intent/payloads, leases, and SQLite state.
`src/dispatcher/execution.py` connects it to the Phase 2 process adapter. The
legacy CLI loop remains separate and real model-backed execution remains blocked.

## Implemented

- Bootstrap rendering is packaged with the wheel and contains the exact project,
  repository registry, inspected specifications, approved plan sources and
  hashes, role source hashes, profile, current step baseline, and schema-valid
  protocol examples. Bootstrap is persisted as a hashed artifact.
- Sequential preparation rejects unknown steps/roles/sessions, unmet
  dependencies, retry exhaustion, review mismatches, unresolved dispatches, and
  non-new reviewer sessions before a worker can launch. It compiles the exact
  role/repository/step policy and persists `PREPARED` intent plus prompt/policy.
- Explicit `mark_running`, typed executor/reviewer application, durable
  forwarding, and acknowledgement methods maintain SQLite dispatch state. They
  never infer state from free-form chat.
- Executor evidence must exactly match declared IDs, paths, and media types.
  Reviewer targets are bound to executor dispatch/attempt/revision/artifact
  hashes, fresh review sessions, and current repository revision.
- Completion is dispatcher-owned. It returns structured unmet obligations,
  produces a report with result revisions and artifact hashes, then records
  `SUCCEEDED` only after report generation.
- Step counters and retry/rework outcomes are local to each normalized step.
  Resume/fork never silently become new sessions.
- `dispatcher baseline inspect` and `dispatcher baseline approve` provide a
  read-only historical candidate and explicit approval. Every unverifiable step
  remains `PENDING`; changed historical evidence invalidates approval.
- The README capability matrix now reflects the implemented adapter, policy,
  SQLite, workflow-facade, and baseline boundaries.
- A disposable Git fixture completes supervisor, executor, reviewer rejection,
  resumed executor rework, fresh reviewer acceptance, and guarded completion
  through nine fake OpenCode 1.18.11-compatible subprocess calls.
- Process start and session identification are distinct durable generations.
  Nonzero exits, malformed JSONL, timeout, and post-commit failure enter durable
  operator reconciliation without advancing the step.

## Verification

```text
ruff check src tests
All checks passed!

mypy src
Success: no issues found in 24 source files

pytest
147 passed

python -m build
Successfully built dispatcher-0.1.0.tar.gz and dispatcher-0.1.0-py3-none-any.whl

clean wheel template: byte-for-byte source match
```

No model-backed, real-project-mutating, deployment, or infrastructure command
was run. Repository writes occurred only inside disposable temporary Git fixtures.

## Remaining Phase 4 Work

- Live OpenCode allow/ask/deny enforcement remains a Phase 2 open item.
- Repository movement/worktree/patch handling belongs to Phase 5.
- The private reference-project historical fixture and its independent per-step
  decisions are explicitly deferred until the generic framework is reviewed.
- Exhaustive every-transition crash injection and legacy CLI-loop replacement
  are not complete. The real execution guard remains in place.
