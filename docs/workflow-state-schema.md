# Workflow State Schema V1

`src/dispatcher/workflow.py` defines the versioned state machine. Its
machine-readable representation is `schemas/workflow-state-v1.json`.

## Run states

| State | Allowed next states |
|---|---|
| `NEW` | `READY`, `WAITING_OPERATOR`, `FAILED`, `CANCELLED` |
| `READY` | `RUNNING`, `WAITING_OPERATOR`, `HALTED`, `FAILED`, `CANCELLED` |
| `RUNNING` | `WAITING_OPERATOR`, `HALTED`, `FAILED`, `SUCCEEDED`, `CANCELLED` |
| `WAITING_OPERATOR` | `READY`, `RUNNING`, `HALTED`, `FAILED`, `CANCELLED` |
| `HALTED`, `FAILED`, `SUCCEEDED`, `CANCELLED` | none |

`WAITING_OPERATOR` requires a durable question, allowed answers, context
reference, and resume destination. `SUCCEEDED` is allowed only when the
dispatcher finds no unmet completion obligations.

## Step states

| State | Key next states |
|---|---|
| `PENDING` | `READY`, `WAIVED` |
| `READY` | `EXECUTING`, `BLOCKED`, `WAIVED`, `FAILED` |
| `EXECUTING` | `EXECUTED`, `BLOCKED`, `FAILED` |
| `EXECUTED` | `REVIEW_REQUIRED`, `ACCEPTED` |
| `REVIEW_REQUIRED` | `REVIEWING`, `ACCEPTED` by approved non-mandatory review waiver, `BLOCKED`, `FAILED` |
| `REVIEWING` | `ACCEPTED`, `CHANGES_REQUESTED`, `BLOCKED`, `FAILED`, `REVIEW_REQUIRED` |
| `CHANGES_REQUESTED` | `READY`, `REVIEW_REQUIRED` for immutable tie-break, `BLOCKED`, `FAILED` |
| `BLOCKED` | `READY`, `WAIVED`, `FAILED` |
| `ACCEPTED`, `WAIVED`, `FAILED` | none |

A step waiver always records an operator decision reference. A non-mandatory
review waiver records a separate decision reference while preserving the
accepted executor evidence.

## Dispatch states

| State | Allowed next states |
|---|---|
| `PREPARED` | `RUNNING`, `ABANDONED` |
| `RUNNING` | `COMPLETED`, `FAILED`, `ABANDONED` |
| `COMPLETED` | `FORWARDED` |
| `FORWARDED` | `ACKNOWLEDGED`, `ABANDONED` |
| `FAILED`, `ACKNOWLEDGED`, `ABANDONED` | none |

Every dispatch has a durable intent with prompt hash, policy digest, repository
coordinate, result kind, and idempotency key. Phase 3 persists those rows in
the private SQLite authority at `state.directory/dispatcher.sqlite3`; the
legacy `run-record.json` checkpoint and Markdown/JSONL artifacts are derived
exports only. A `RUNNING` dispatch always requires operator reconciliation
after process loss and is never retried implicitly.

## Batch states

| State | Allowed next states |
|---|---|
| `PREPARED` | `RUNNING`, `FAILED` |
| `RUNNING` | `JOINED`, `FAILED` |
| `JOINED`, `FAILED` | none |

A batch correlates independently durable child dispatches. Children are
prepared all-or-none and retain their normal dispatch lifecycle. A failed child
is named in the batch result and creates one durable reconciliation request only
after every started child reaches a durable outcome.

## Workspace group states

| State | Meaning |
|---|---|
| `PREPARED` | Temporary worktree intent is durable; Git side effects have not started. |
| `ACTIVE` | Child branches/worktrees exist under a repository lease. |
| `CLEANUP_PENDING` | Cleanup intent is durable before worktrees or branches are removed. |
| `CLEANED` | Every owned worktree and branch was removed. |
| `FAILED` | Provisioning or cleanup needs explicit recovery. |

Workspace groups are scheduler-admitted only for independent, commit-required
same-repository executor children. Temporary child branches start from one base
revision, remain available through review/rework, merge in deterministic order
only after acceptance, and are removed after integration or reconciled cleanup.

## Run Policy and Usage

Activation persists an immutable `RunPolicy` with compiled review obligations,
profile digest, policy digest, and underspecification mode. Run state also
persists cumulative worker usage by run, step, role, and session. Enabled
budgets reject dispatches at their exact configured boundary and fail closed
when required measured usage is absent.

## Completion guard

The completion guard returns structured obligations for every pending condition:

- step not accepted or waived;
- dependency not accepted or waived;
- missing review acceptance;
- missing required evidence;
- unresolved operator gate;
- dispatch in flight; or
- unresolved operator request.

Supervisor `request_completion` never bypasses this guard.
