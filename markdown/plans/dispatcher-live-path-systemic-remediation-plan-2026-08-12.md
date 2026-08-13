# Dispatcher Live-Path Systemic Remediation Plan

**Date:** 2026-08-12  
**Status:** Proposed implementation plan  
**Source analysis:**
`markdown/reports/misc-fixes/step-11-dispatcher-live-path-systemic-analysis-2026-08-12.md`  
**Current baseline:** commit `72a4dea` plus the uncommitted Steps 1-10
worktree; non-live suite at **269 passed, 5 deselected**

## Goal

Close the remaining production and evidence gaps before the final independent
release-readiness review and before any real T2.2a operation:

1. Make every durable operator request resolvable or explicitly terminal.
2. Prevent a step from being accepted when a required verification check is
   missing, failed, skipped, duplicated, or renamed.
3. Preserve actionable, redacted worker-boundary failure details in durable
   state and operational logs.
4. Make the live disposable suite deterministic and capable of proving
   reconciliation, rework/resume, batch, worktree, and cancellation behavior.

## Non-Goals

- Do not weaken `commit_policy="required"` or clean-worktree validation.
- Do not reshape, repair, or reinterpret malformed model responses.
- Do not add implicit exceptions for failed or skipped verification checks.
- Do not broaden live tests to cluster, deployment, Docker, push, PR, or
  external-service behavior.
- Do not inspect private T2 state or `config/projects/local/`.

## Verified Open Findings

### M1: Operator request handling is incomplete

`OperatorRequest.kind` declares nine values in `src/dispatcher/workflow.py`,
but `StateStore.answer_operator_request` has explicit behavior for only four.
`reconciliation`, `batch_reconciliation`, `workspace_reconciliation`, and
`underspecification` currently inherit `request.resume_to` regardless of the
answer. `budget` works only accidentally because its sole answer and
`resume_to` are both `HALTED`.

Consequences:

- `halt` can be ignored for reconciliation and underspecification requests.
- Failed solo and batch steps remain permanently `BLOCKED` after
  `reconcile`.
- Workspace reconciliation cannot complete its durable cleanup lifecycle.
- Future request kinds can silently fall through instead of failing loudly.

### M2: Acceptance criteria are not enforced

`AcceptanceCriterion` is documented as a check that must pass before a step
is accepted. `VerificationResult` carries the corresponding `check_id` and
status, but no runtime code links them. A no-review step can currently reach
`ACCEPTED` with failed, skipped, missing, duplicate, or unrelated checks.

This was observed in the live batch artifact: `prepare-second` reported
`pytest` as `skipped` but was accepted because no review was required. That is
the same policy shape planned for T2.2a.

### M3/M4: Failure detail is discarded

`execute_worker` persists only the exception class name and `execute_batch`
silently consumes child exceptions. The latest batch artifact therefore
recorded `SequentialWorkflowError`, but not the actionable dirty-worktree
path that caused it.

### N1: The live fixture is non-deterministic

The fixture asks a model to ensure tests pass but does not provide a fixed
test or standard cache ignores. Terra created a legitimate test, ran it, and
pytest left an untracked bytecode file. Production correctly rejected the
dirty repository; the fixture caused the incidental residue.

## Design Decisions

These decisions are part of the plan and must not be left for an executor to
guess.

### Operator answers

Every declared request kind must have an explicit branch. A final exhaustive
failure branch must reject any unhandled kind.

| Kind | Positive answer | Positive transition | Negative answer |
|---|---|---|---|
| `risk_gate` | `approve` | resolve gate; run `RUNNING` | `deny` → `HALTED` |
| `escalation` | `reassign` | blocked step → `READY`; run `RUNNING` | `halt` → `HALTED` |
| `review_waiver` | `waive` | review-required step → `ACCEPTED` | `halt` → `HALTED` |
| `stall_recovery` | `retry` | blocked step → `READY`; run `RUNNING` | `halt` → `HALTED` |
| `underspecification` | `answer` | explicit operator acknowledgement; run `RUNNING` | `halt` → `HALTED` |
| `budget` | none beyond `halt` | not applicable | `halt` → `HALTED` |
| `reconciliation` | `reconcile` | `BLOCKED` step → `READY`; `REVIEW_REQUIRED` remains review-required; run `RUNNING` | `halt` → `HALTED` |
| `batch_reconciliation` | `reconcile` | each failed child's blocked step → `READY`; review-required steps remain so; run `RUNNING` | `halt` → `HALTED` |
| `workspace_reconciliation` | `reconcile` | cleanup must complete durably before run resumes | `halt` → `HALTED` |

For solo and batch reconciliation, `reconcile` is an explicit operator
attestation. The state transaction must still validate that referenced
dispatches are terminal and affected steps are in compatible states. A later
dispatch remains protected by the existing clean repository and identity
checks before process launch.

Workspace reconciliation is different because Git worktree cleanup is a
side effect and cannot occur inside the SQLite answer transaction. The CLI
answer path must:

1. Detect `workspace_reconciliation` + `reconcile`.
2. Invoke `WorkspaceCoordinator.cleanup(force=False)`, which persists
   `CLEANUP_PENDING` before touching Git.
3. Refuse to resume if cleanup fails or the group is not `CLEANED`.
4. Reload the run generation and commit the operator answer only after the
   group is durably `CLEANED`.

`StateStore.answer_operator_request` must independently verify that the group
is `CLEANED` before accepting the workspace reconciliation answer, so direct
API callers cannot bypass cleanup.

### Verification enforcement

For every worker result variant:

- `verification` must contain exactly one entry for every
  `AcceptanceCriterion.criterion_id`.
- Duplicate `check_id` values are invalid.
- Missing and unknown `check_id` values are invalid.
- `ExecutorCompletedResult` requires every verification status to be
  `passed`.
- `ReviewerAcceptedResult` requires every verification status to be
  `passed`.
- Non-success outcomes may report `failed` or `skipped`, but must still use
  the exact criterion IDs so the record is complete and deterministic.
- There is no implicit optional-check allow-list. If optional checks are
  needed later, they require an explicit versioned plan-schema change.

An inconsistent success result is not repaired into another result type. It
must fail the worker-result boundary, preserve the exact redacted reason, and
enter the existing reconciliation path because the worker may already have
changed the repository.

The worker prompt must state the exact one-to-one criterion/check rule in
addition to embedding the authoritative response schema.

### Failure persistence

- Persist a stable category such as `adapter`, `result_validation`,
  `repository_validation`, or `workflow_validation`.
- Persist `redact_text(str(exc))[:5000]` as `failure_detail`.
- Keep the exception class in the event reason or category, but never use it
  as the only diagnostic.
- Log one warning per failed batch child with dispatch ID, category, and the
  same bounded redacted detail.
- Do not persist raw stderr, credentials, prompts, or unbounded exception
  content in authoritative state.

## Step 1: Complete Operator Recovery

**Priority:** Highest  
**Recommended model:** GPT-5.6 Sol  
**Session:** Fresh, seeded with the Step 11 analysis and this plan

### Scope

- Make `answer_operator_request` exhaustive across all nine request kinds.
- Implement solo and batch reconciliation state transitions.
- Add safe workspace cleanup-before-resume orchestration to the CLI answer
  path.
- Ensure every `halt` answer reaches `RunStatus.HALTED`.
- Add a final fail-loud branch for any unhandled/corrupt request kind.

### Likely files

- `src/dispatcher/state_store.py`
- `src/dispatcher/cli.py`
- `src/dispatcher/workspaces.py` only if a narrow coordinator helper is
  required
- `tests/fault_injection/test_state_store.py`
- `tests/integration/test_batch_execution.py`
- `tests/integration/test_workspace_barrier.py`
- `tests/unit/test_cli.py`
- `docs/operations.md`
- `docs/protocol.md`

### Required tests

- Every allowed answer for every request kind has a named test with its exact
  expected transition (`deny` for risk gates and the halt-only budget path
  included).
- Solo reconciliation: failed dispatch + blocked step → `reconcile` → step
  `READY`, run `RUNNING`.
- Reviewer-boundary reconciliation: step remains `REVIEW_REQUIRED`, not
  incorrectly reset to executor `READY`.
- Batch reconciliation: all failed child step IDs are resolved exactly once;
  unrelated accepted siblings remain unchanged.
- Batch reconciliation with duplicate failed dispatches for one step does not
  double-transition the step.
- Workspace reconciliation refuses to resume before cleanup is `CLEANED`.
- Workspace cleanup failure leaves the run waiting and preserves the owned
  branches/worktrees.
- Workspace cleanup success then records the answer and resumes.
- Generation conflicts and stale request IDs remain fail-closed.
- Full non-live suite remains green with a higher pass count than 269.

### Completion evidence

Write:
`markdown/reports/misc-fixes/step-12-operator-recovery-completion-2026-08-12.md`

Do not run live tests in the executor session.

## Step 2: Enforce Acceptance and Preserve Failures

**Priority:** Highest  
**Recommended model:** GPT-5.6 Sol  
**Session:** Fresh; do not combine with Step 1's state-machine implementation

### Scope

- Add exact acceptance-criterion/verification coverage validation.
- Require passed verification for completed/accepted success variants.
- Update worker prompt instructions and valid examples to use exact criterion
  IDs.
- Persist bounded, redacted failure category/detail for every worker-boundary
  failure.
- Log every failed batch child without changing batch fail-closed semantics.

### Likely files

- `src/dispatcher/sequential.py`
- `src/dispatcher/execution.py`
- `src/dispatcher/results.py` only if success-result self-validation belongs
  there
- `src/dispatcher/workflow.py` or `state_store.py` only for failure-field
  plumbing already supported by `commit_dispatch_transition`
- `tests/unit/test_results.py`
- `tests/unit/test_sequential.py`
- `tests/unit/test_execution.py`
- `tests/integration/test_batch_execution.py`
- `tests/property/test_contracts.py`
- generated result schemas only if the Pydantic contract changes
- `docs/protocol.md`

### Required tests

- Exact criterion/check coverage succeeds for executor and reviewer results.
- Missing, unknown, and duplicate check IDs fail closed.
- `completed` with `failed` or `skipped` verification fails before acceptance
  on both review-required and no-review paths.
- `accepted` reviewer result with failed/skipped verification fails closed.
- Blocked/failed/changes-requested variants can report failed/skipped checks
  only when exact criterion IDs are still present.
- Invalid success results do not get reshaped into failed/blocked results.
- Failure records contain category and redacted bounded detail.
- Secret-like text in an exception is redacted before persistence and logs.
- Batch execution logs one warning per failed child with its dispatch ID.
- Existing exact JSON, duplicate-key, identity, revision, evidence-hash, and
  evidence-size tests remain unchanged and green.
- Full non-live suite remains green with a higher pass count than Step 1.

### Completion evidence

Write:
`markdown/reports/misc-fixes/step-13-acceptance-and-failure-observability-2026-08-12.md`

Do not run live tests in the executor session.

## Step 3: Deterministic Fixtures and Final Live Proof

**Priority:** After Steps 1 and 2  
**Recommended model:** DeepSeek V4 Flash 0731 for test-only implementation;
human operator for credential-backed proof  
**Session:** Fresh

### Scope

- Seed deterministic tests and standard cache ignores in every disposable
  repository, including dynamically created siblings.
- Update disposable prompts to name the exact test command, criterion IDs,
  required clean status, and evidence paths.
- Preserve production clean-worktree validation unchanged.
- Add explicit live scenarios for reconciliation and review/rework/resume.
- Add independent environment variables for supervisor, executor, and
  reviewer model IDs while retaining `DISPATCHER_LIVE_MODEL` as an explicit
  all-role fallback only for disposable compatibility.

### Fixture design

- Initial commits include `.gitignore` entries for `__pycache__/`, `*.pyc`,
  and `.pytest_cache/`.
- Initial commits include fixed tests that fail until the requested result
  files are created.
- Plan acceptance criterion IDs match the exact expected worker
  `verification[].check_id` values.
- Prompts require `git status --porcelain` to be empty before returning.
- Do not change the child process's production environment merely to hide
  fixture residue; repository-local ignores and deterministic tests are the
  source of truth.

### Likely files

- `tests/helpers.py`
- `tests/live/test_real_operation_disposable.py`
- focused test fixtures under `tests/fixtures/`
- `docs/operations.md` for the role-matrix environment variables and live
  proof procedure

### Required non-live tests

- Pure tests for fixture initialization and model-role selection.
- Reactive supervisor tests still cover first-attempt acceptance, one rework,
  retry exhaustion, batch completion, and reconciliation.
- Full non-live suite remains green with no reduced test count.

### Required human-run live proof

Run against disposable repositories only:

1. Sequential executor + reviewer acceptance.
2. Deliberately forced reviewer changes request, executor resume/rework, and
   reviewer acceptance.
3. Cross-repository batch with both children accepted and repositories clean.
4. Same-repository worktree barrier integration and cleanup.
5. Cancellation with durable intent and verified process identity.
6. Solo reconciliation: induce one safe validation failure, answer
   `reconcile` through the real CLI, retry, and succeed.
7. Batch reconciliation: induce one child failure, preserve the accepted
   sibling, answer `reconcile`, retry the failed step, and succeed.
8. Halt branch: answer `halt` to a disposable reconciliation request and
   verify terminal `HALTED` state.

Use separate role variables for the intended matrix:

```sh
export DISPATCHER_LIVE_SUPERVISOR_MODEL="<intended supervisor>"
export DISPATCHER_LIVE_EXECUTOR_MODEL="openai/gpt-5.6-terra"
export DISPATCHER_LIVE_REVIEWER_MODEL="<intended reviewer>"
export DISPATCHER_REAL_DISPOSABLE=1
.venv/bin/python -m pytest tests/live/test_real_operation_disposable.py -v -m live_opencode
```

Also run the complete non-live suite immediately before and after the live
proof. Capture machine output; do not use a narrative checklist as proof.

### Completion evidence

Write a new dated report; do not overwrite the stale pre-contract report:

`markdown/reports/dispatcher-disposable-real-operation-proof-2026-08-12.md`

The report must include exact commit SHA, dirty/clean status, role matrix,
commands, complete pytest summaries, scenario names, and sanitized durable
state assertions. It must not include credentials or raw private state.

## Final Release Sequence

1. Complete Steps 1-3 and retain their evidence reports.
2. Run the full non-live and disposable live proof suites.
3. Remove unrelated tooling residue such as a root `.opencode/` directory.
4. Review the complete diff for accidental private/config/state files.
5. Commit the intended changes as one auditable remediation revision (or a
   small ordered series if repository convention requires it).
6. Run the full non-live suite again from the committed revision.
7. Run a separate, fresh Claude Sonnet 5 final release-readiness review.
8. Record an explicit operator enablement decision naming T2.2a only, the
   exact config/plan/baseline/run/repository revision/model matrix, and stop
   conditions.

## Final Review Preconditions

- All nine operator request kinds have named passing tests.
- M1-M4 each have direct source tests, not only reports.
- A no-review step cannot accept failed/skipped/missing verification.
- Reconciliation and halt are proven through real CLI operator answers in
  disposable state.
- All disposable repositories finish clean.
- Sequential resume is live-proven.
- No private files, credentials, or local T2 state are in the diff.
- The worktree is committed and clean.
- The final reviewer receives the committed revision and fresh machine test
  evidence, not the accumulated narrative reports as proof.
