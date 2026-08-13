# Step 12 Operator Recovery Completion Report

Date: 2026-08-12

## Outcome

The operator recovery consumer is exhaustive across all nine declared
`OperatorRequest.kind` values. Every allowed answer now has an explicit target
and state mutation, corrupt or future kinds fail loudly, solo and batch
reconciliation validate their durable references, and workspace reconciliation
performs non-force durable cleanup before the SQLite answer transaction.

The final non-live suite passed with 305 tests, 36 more than the 269-pass
baseline.

## Files Changed

This remediation changed exactly these files:

1. `src/dispatcher/state_store.py`
2. `src/dispatcher/cli.py`
3. `src/dispatcher/workspaces.py`
4. `tests/fault_injection/test_state_store.py`
5. `tests/integration/test_batch_execution.py`
6. `tests/integration/test_workspace_barrier.py`
7. `docs/operations.md`
8. `docs/protocol.md`
9. `markdown/reports/misc-fixes/step-12-operator-recovery-completion-2026-08-12.md`

All pre-existing uncommitted changes outside this scope were preserved.

## State Machine Citations

Common generation, active-request, allowed-answer, expiration, and actor-role
validation remains at `src/dispatcher/state_store.py:900-915`. One operator
event and decision identity are built at `src/dispatcher/state_store.py:916-925`.

| Kind | Allowed answers | Implemented behavior | Source |
|---|---|---|---|
| `risk_gate` | `approve`, `deny` | `approve` resolves the referenced step gate and resumes `RUNNING`; `deny` halts. | `src/dispatcher/state_store.py:926-944` |
| `escalation` | `reassign`, `halt` | `reassign` requires a blocked step and exact normalized-plan role, moves the step to `READY`, records the reassignment role, and resumes `RUNNING`; `halt` halts. | `src/dispatcher/state_store.py:945-969` |
| `review_waiver` | `waive`, `halt` | `waive` requires a waivable `REVIEW_REQUIRED` step, records the decision reference, accepts the step, and resumes `RUNNING`; `halt` halts. | `src/dispatcher/state_store.py:970-993` |
| `stall_recovery` | `retry`, `halt` | `retry` moves `BLOCKED` to `READY` or preserves `REVIEW_REQUIRED` while refreshing its durable event, then resumes `RUNNING`; `halt` halts. | `src/dispatcher/state_store.py:994-1017` |
| `underspecification` | `answer`, `halt` | `answer` is the fixed acknowledgement and resumes `RUNNING`; `halt` halts. | `src/dispatcher/state_store.py:1018-1024` |
| `budget` | `halt` | Explicitly requires `halt` and transitions to `HALTED`, independent of `resume_to`. | `src/dispatcher/state_store.py:1025-1028` |
| `reconciliation` | `reconcile`, `halt` | `reconcile` validates the known step, known matching dispatch, `FAILED`/`ABANDONED` dispatch state, and compatible step state; blocked steps become `READY`, review-required steps remain so with a refreshed event, and the run resumes. `halt` leaves the step unchanged and halts. | `src/dispatcher/state_store.py:1029-1064` |
| `batch_reconciliation` | `reconcile`, `halt` | `reconcile` validates a historical `FAILED` batch, nonempty failed IDs, each known batch-owned terminal dispatch, and each affected step. Deduplicated blocked steps become `READY`, review-required steps remain so, accepted siblings are untouched, the batch remains `FAILED`, and the run resumes. `halt` leaves batch and steps unchanged and halts. | `src/dispatcher/state_store.py:1065-1118` |
| `workspace_reconciliation` | `reconcile`, `halt` | `reconcile` independently requires the referenced group to be durably `CLEANED` before resuming `RUNNING`; `halt` halts without changing the group. | `src/dispatcher/state_store.py:1119-1135` |

The fail-loud exhaustiveness guard is at
`src/dispatcher/state_store.py:1136-1139`. The final run snapshot and exactly
one operator decision are committed under the same transaction at
`src/dispatcher/state_store.py:1140-1164`; `transition_run` clears the active
request whenever the target is not `WAITING_OPERATOR`.

## Workspace Ordering

The CLI detects only the matching active `workspace_reconciliation` request
with answer `reconcile`, invokes `WorkspaceCoordinator.cleanup` with the
current generation and `force=False`, then passes the returned generation to
`answer_operator_request` at `src/dispatcher/cli.py:655-677`.

The coordinator persists `CLEANUP_PENDING` before invoking the manager at
`src/dispatcher/workspaces.py:439-456`, persists `CLEANED` after successful Git
cleanup at `src/dispatcher/workspaces.py:457-466`, and persists `FAILED` while
raising on cleanup failure at `src/dispatcher/workspaces.py:467-478`. A
previously completed cleanup is idempotently returned at
`src/dispatcher/workspaces.py:437-438`, allowing a retry after a crash between
cleanup and answer commit.

Non-force cleanup performs a complete safety pass before any deletion. It
checks exact owned branch identity, dirty/untracked worktree content, and that
every existing owned branch is merged at
`src/dispatcher/workspaces.py:184-191` and
`src/dispatcher/workspaces.py:235-250`. Path and branch-prefix ownership checks
remain at `src/dispatcher/workspaces.py:252-274`. This prevents a later unsafe
branch from being discovered only after an earlier worktree was deleted.

No Git or filesystem side effect occurs inside
`StateStore.answer_operator_request`. Cleanup is completed in the CLI/service
layer first; the state-store method then performs only validated in-memory
transitions and one SQLite transaction. Cleanup failure therefore leaves the
run `WAITING_OPERATOR`, retains the same request, and creates no decision row.

## Tests Added Or Extended

Every successful state-store answer uses the helper at
`tests/fault_injection/test_state_store.py:206-217`, which asserts generation
advancement, cleared request state, and exactly one decision row. Every
rejection below asserts zero decision rows. Workspace CLI tests independently
query the decision table through
`tests/integration/test_workspace_barrier.py:389-391`.

### Risk, Escalation, Waiver, Stall, Underspecification, And Budget

- `test_risk_gate_approve_resolves_step_gate_and_resumes_running`
- `test_risk_gate_deny_halts_the_run`
- `test_escalation_reassign_requires_blocked_step_and_resumes_ready`
- `test_escalation_halt_preserves_blocked_step_and_halts`
- `test_review_waiver_waive_accepts_step_and_records_decision_reference`
- `test_review_waiver_halt_preserves_review_required_step`
- `test_stall_recovery_retry_moves_blocked_step_to_ready`
- `test_stall_recovery_retry_preserves_review_required_and_refreshes_event`
- `test_stall_recovery_halt_preserves_step_and_halts`
- `test_underspecification_answer_acknowledges_and_resumes_running`
- `test_underspecification_halt_halts_the_run`
- `test_budget_halt_explicitly_halts_despite_nonhalting_resume_target`

### Solo Reconciliation

- `test_reconciliation_reconcile_moves_blocked_step_to_ready`
- `test_reconciliation_reconcile_preserves_review_required_and_refreshes_event`
- `test_reconciliation_halt_preserves_failed_step_and_halts`
- `test_reconciliation_rejects_unknown_dispatch_without_decision`
- `test_reconciliation_rejects_dispatch_for_another_step_without_decision`
- `test_reconciliation_rejects_nonterminal_dispatch_without_decision`

### Batch Reconciliation

- `test_batch_reconciliation_requeues_unique_failed_steps_and_preserves_accepted_sibling`
- `test_batch_reconciliation_deduplicates_two_failed_dispatches_for_one_step`
- `test_batch_reconciliation_preserves_review_required_failed_child`
- `test_batch_reconciliation_halt_preserves_children_and_failed_batch`
- `test_batch_reconciliation_rejects_unknown_batch_without_decision`
- `test_batch_reconciliation_rejects_nonfailed_batch_without_decision`
- `test_batch_reconciliation_rejects_empty_failed_list_without_decision`
- `test_batch_reconciliation_rejects_foreign_dispatch_without_decision`
- `test_batch_reconciliation_rejects_unknown_failed_dispatch_without_decision`
- `test_batch_reconciliation_rejects_nonterminal_failed_dispatch_without_decision`
- `test_batch_reconciliation_rejects_incompatible_step_state_without_decision`
- `test_batch_preparation_is_all_or_none_and_failed_children_join_durably` was extended to answer the real failed-batch request, requeue both failed children, and prove the historical batch remains `FAILED`.

### Workspace, Exhaustiveness, And Transaction Guards

- `test_workspace_reconciliation_direct_answer_rejects_group_before_cleaned`
- `test_workspace_reconciliation_cli_cleans_non_force_before_recording_answer`
- `test_workspace_reconciliation_cli_cleanup_failure_keeps_request_and_unsafe_git_state`
- `test_workspace_reconciliation_cli_halt_skips_cleanup_and_preserves_group`
- `test_unhandled_operator_request_kind_fails_loudly_without_decision`
- `test_operator_answer_rejects_stale_generation_without_decision`
- `test_operator_answer_rejects_wrong_request_id_without_decision`
- `test_operator_answer_rejects_expired_or_unauthorized_requests`
- `test_operator_answer_and_transcripts_are_durable_and_collision_free` now uses the valid fixed underspecification acknowledgement and verifies repeat answers are rejected.

## Verification

Focused final results:

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/fault_injection/test_state_store.py -q` | `48 passed in 3.48s` |
| `.venv/bin/python -m pytest tests/integration/test_batch_execution.py -q` | `2 passed in 0.57s` |
| `.venv/bin/python -m pytest tests/integration/test_workspace_barrier.py -q` | `5 passed in 4.16s` |
| `.venv/bin/python -m pytest tests/integration/test_workspaces.py -q` | `4 passed in 2.35s` |
| `.venv/bin/python -m pytest tests/unit/test_cli.py -q` | `10 passed in 1.15s` |
| `.venv/bin/python -m pytest tests/unit/test_sequential.py -q` | `25 passed in 2.99s` |

The first state-store focused run found two synthetic batch fixtures sharing an
idempotency key. The fixture helper was corrected to generate a unique durable
idempotency key per dispatch; the final focused and full results above passed.

Full required non-live command:

```text
.venv/bin/python -m pytest tests -q -m "not live_opencode"
305 passed, 5 deselected in 43.74s
```

Static checks:

```text
.venv/bin/ruff check src/dispatcher/state_store.py src/dispatcher/cli.py src/dispatcher/workspaces.py tests/fault_injection/test_state_store.py tests/integration/test_batch_execution.py tests/integration/test_workspace_barrier.py
All checks passed!

git diff --check
no output (pass)
```

## Constraints And Deviations

- No commit was created or amended, no branch was created, and nothing was pushed.
- No live OpenCode or live-marked test was run; the full suite explicitly deselected `live_opencode` tests.
- No network or HTTP call was made.
- No credentials were accessed.
- No prohibited local project/state path, repository-root state, or private T2 state was inspected or modified.
- No executor or reviewer response schema was changed.
- Repository identity, cleanliness, evidence, process-safety, and later launch checks were not weakened.
- All implementation and report changes remain uncommitted.
- There were no deviations from the requested Step 1 scope. The narrow non-force cleanup preflight in `workspaces.py` was necessary to satisfy the explicit fail-closed preservation requirement for unsafe/unmerged owned worktrees and branches.
