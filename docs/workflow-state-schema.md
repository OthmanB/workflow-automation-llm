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
| `REVIEW_REQUIRED` | `REVIEWING`, `BLOCKED`, `FAILED` |
| `REVIEWING` | `ACCEPTED`, `CHANGES_REQUESTED`, `BLOCKED`, `FAILED`, `REVIEW_REQUIRED` |
| `CHANGES_REQUESTED` | `READY`, `BLOCKED`, `FAILED` |
| `BLOCKED` | `READY`, `WAIVED`, `FAILED` |
| `ACCEPTED`, `WAIVED`, `FAILED` | none |

A waiver always records an operator decision reference.

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
