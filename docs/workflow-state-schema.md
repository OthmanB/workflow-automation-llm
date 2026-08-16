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
exports only. A `RUNNING` dispatch is never retried implicitly. The only
automatic process-loss recovery is exact adoption of a dispatcher-created
commit whose durable structured Git record remains `STAGED`; every fingerprint
mismatch requires operator reconciliation.

## Structured Git states

Required-commit executor work has a separate SQLite lifecycle because Git and
SQLite cannot commit atomically:

| State | Meaning |
|---|---|
| `PROPOSAL_RECEIVED` | The immutable executor proposal is durable. |
| `CHECKED` | Dispatcher checks and repository/evidence observations are durable; no commit is required. |
| `COMMIT_INTENT_PERSISTED` | The exact base, worktree, path set, candidate tree, identity digest, and message are durable before real-index staging. |
| `STAGED` | Exact real-index staging was observed before commit creation. |
| `COMMITTED` | Commit fingerprint, authoritative result, verification, and post-snapshot were atomically persisted. |
| `NO_COMMIT_FINALIZED` | A non-committing outcome was finalized. |
| `RECONCILIATION_REQUIRED` | An interrupted or mismatched external side effect cannot be adopted safely. |

Recovery never runs `git reset`, `git clean`, staging, commit, or verification.
It may inspect and adopt an exact `STAGED` commit only after the worker is known
to have exited and parent, tree, path set, identity, subject, worktree, clean
post-state, and durable evidence all match.

## Cluster Operation States

Cluster-operation state is a separate approval-bound SQLite journal, not a run
or worker state. Its exact states are `DISCOVERED`, `STATIC_VALIDATED`,
`SNAPSHOT_CAPTURED`, `APPROVED`, `SERVER_DRY_RUN_PASSED`, `MUTATION_STARTED`,
`MUTATED`, `PROBING`, `SUCCEEDED`, `ROLLBACK_STARTED`, `ROLLED_BACK`, `FAILED`,
and `RECONCILIATION_REQUIRED`. The complete successor table is normative in
[`cluster-operation-manifest-schema.md`](cluster-operation-manifest-schema.md).
The journal is keyed by `(run_id, operation_id, source_revision)`, uses its own
generation CAS, and accepts no raw command output, manifests, kubeconfig,
credentials, certificates/keys, or Secret values. Fixed-runner command evidence
is limited to bounded stdout/stderr hashes, duration, exit status, command kind,
and static action identity. It does not auto-reconcile a started mutation.

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
