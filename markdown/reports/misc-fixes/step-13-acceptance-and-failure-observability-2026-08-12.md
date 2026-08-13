# Step 13 Acceptance And Failure Observability Report

Date: 2026-08-12

## Outcome

Step 2 is implemented. Executor and reviewer results now require exact,
duplicate-free correspondence between normalized plan criterion IDs and worker
check IDs. Successful executor/reviewer variants additionally require every
check to pass. Context-invalid results fail before durable completion or review
persistence and, through the worker coordinator, enter the existing operator
reconciliation path without being reshaped.

Every non-stall worker-boundary failure now preserves a stable category and a
bounded redacted detail in authoritative dispatch state. Batch execution emits
one warning for each failed child while preserving independent successful
sibling forwarding and final `wait_for_started` reconciliation semantics.

The final non-live suite passed with 346 tests, 41 more than the required
305-pass baseline.

## Files Changed

This remediation changed these files:

1. `src/dispatcher/sequential.py`
2. `src/dispatcher/execution.py`
3. `tests/unit/test_sequential.py`
4. `tests/unit/test_execution.py`
5. `tests/property/test_contracts.py`
6. `tests/integration/test_batch_execution.py`
7. `tests/integration/test_baseline_adoption.py`
8. `tests/integration/test_workspace_barrier.py`
9. `tests/fault_injection/test_sequential_execution.py`
10. `tests/fixtures/opencode/fake_cli.py`
11. `docs/protocol.md`
12. `docs/operations.md`
13. `markdown/reports/misc-fixes/step-13-acceptance-and-failure-observability-2026-08-12.md`

Existing uncommitted work outside this scope was preserved. No Pydantic result
or plan contract changed, so no generated schema was changed.

## Verification Enforcement

The contextual validator is
`src/dispatcher/sequential.py:2394-2430`. It reads criterion and check IDs
without normalization at lines 2399-2400, identifies duplicate, missing, and
unknown IDs independently and deterministically at lines 2401-2405, restricts
all-passed semantics to `ExecutorCompletedResult` and
`ReviewerAcceptedResult` at lines 2406-2410, and emits separately labelled
diagnostics at lines 2412-2430. Non-success variants therefore permit
`failed`/`skipped` statuses only when coverage remains exact.

Executor application now follows identity validation, plan-step resolution,
verification validation, evidence validation, repository inspection/snapshot
validation, and only then `COMPLETED` persistence at
`src/dispatcher/sequential.py:842-867`. The success transition to
`REVIEW_REQUIRED` or `ACCEPTED` remains after that gate at lines 871-890.

Reviewer application validates result identity and immutable review target,
resolves the plan step, validates verification, validates the repository, and
only then commits `COMPLETED` and the immutable review row at
`src/dispatcher/sequential.py:962-995`. Invalid accepted results therefore do
not create a review row or increment acceptance.

`WorkerResultValidationError` is a `SequentialWorkflowError` subtype, so
context-invalid worker results preserve the required public failure behavior
while receiving the stable `result_validation` category.

The validator treats an empty criterion set as requiring an empty verification
set. The current normalized plan schema still requires at least one criterion;
the contextual validator's total empty-set behavior is tested directly without
changing that independent plan contract.

## Worker Prompt

The prompt continues to embed the authoritative `schema_documents()` result at
`src/dispatcher/sequential.py:2167`. Exact required check IDs and the explicit
one-to-one/all-passed contract are rendered at lines 2168-2195. Both successful
and attention templates receive the step's acceptance criteria directly at
lines 2206-2238.

`_worker_response_template` accepts those criteria at
`src/dispatcher/sequential.py:2247-2260`. Executor success and blocked examples
derive exact IDs at lines 2285-2292 and 2315-2322. Reviewer accepted and
changes-requested examples do the same at lines 2338-2345 and 2360-2367. No
generic verification check-ID placeholder remains in these templates.

## Failure Categories

The exact mapping is implemented in `src/dispatcher/execution.py:492-511`:

| Exception boundary | Stable category |
|---|---|
| `OpenCodeAdapterError` | Existing `exc.category` unchanged, including `timeout`, `interrupted`, `connection`, `rate_limit`, `context_overflow`, `quota`, `authentication`, `permission`, `protocol`, or `unknown` |
| `ExecutionCoordinatorError`, `ResultError`, or `WorkerResultValidationError`, including the exception cause/context chain | `result_validation` |
| `RepositoryValidationError`, including when wrapped by `SequentialWorkflowError` | `repository_validation` |
| `SequentialWorkflowError`, `StateStoreError`, or `TransitionError` | `workflow_validation` |
| Any other exception | `internal` |

No category is derived from exception message text. Cause/context traversal is
bounded by object identity at `src/dispatcher/execution.py:514-522`.

Retryable adapter categories remain exactly `timeout`, `interrupted`,
`connection`, `rate_limit`, and `context_overflow`. They still enter only
`handle_stall`, preserve cooldown/retry/exhaustion behavior, and do not pass
through the generic failure recorder at `src/dispatcher/execution.py:246-274`.

## Redaction And Persistence

The shared boundary helper performs the required operation exactly before any
persistence or warning:

```text
detail = redact_text(str(exc))[:5000]
```

This is at `src/dispatcher/execution.py:508`; an empty exception message falls
back to its class name at lines 509-510 so the non-null detail remains
actionable. The event reason adds only a stable category prefix to the already
sanitized detail and is bounded to 5,000 characters at lines 525-527.

`execute_worker` passes the sanitized category/detail to `fail_dispatch` for
both non-retryable adapter and generic failures at
`src/dispatcher/execution.py:246-287`. `fail_dispatch` applies a second
redaction/truncation boundary at `src/dispatcher/sequential.py:1206-1207`, then
passes both fields through the existing `commit_dispatch_transition` plumbing
at lines 1221-1230. Existing dispatch, step, and reconciliation transitions are
unchanged at lines 1231-1275. Lease release is guaranteed by the `finally` block
at lines 1276-1277.

No raw exception object, stderr, model output, prompt, auth file, or private
environment value enters durable failure fields or warning arguments.

## Batch Warnings

Each future is paired with its prepared child at
`src/dispatcher/execution.py:301-304`. A child exception is sanitized by the
same helper and produces exactly one warning containing the exact dispatch ID,
stable category, and bounded redacted detail at lines 305-325. The exception is
not re-raised, successful children are not warned, and final batch reconciliation
continues at lines 326-331.

## Tests

### Verification Coverage

- `test_executor_verification_requires_exact_criterion_coverage`
- `test_reviewer_verification_requires_exact_criterion_coverage`
- `test_empty_acceptance_criteria_require_empty_verification`
- `test_empty_acceptance_criteria_reject_nonempty_verification`
- `test_executor_dispatch_transitions_only_its_ready_step_and_completion_is_guarded`
- `test_reviewer_acceptance_is_fresh_policy_bound_and_revision_bound`

The executor and reviewer coverage tests are parameterized over missing,
unknown/extra, duplicate, and renamed IDs. Duplicate-ID cases retain the full
expected ID set and still fail.

### Success Status

- `test_executor_dispatch_transitions_only_its_ready_step_and_completion_is_guarded`
- `test_completed_executor_with_exact_passed_coverage_requires_review`
- `test_completed_executor_rejects_non_passing_verification_without_reshaping`
- `test_reviewer_acceptance_is_fresh_policy_bound_and_revision_bound`
- `test_accepted_reviewer_rejects_non_passing_verification_before_review_persistence`

The invalid-success tests cover `failed` and `skipped` statuses, both no-review
and review-required executor paths, unchanged `completed`/`accepted`
discriminators, absence of accepted state, and absence of reviewer rows.

### Non-Success Variants

- `test_executor_non_success_variants_allow_non_passing_exact_coverage`
- `test_executor_non_success_variant_still_requires_exact_coverage`
- `test_reviewer_non_success_variants_allow_non_passing_exact_coverage`

These parameterized tests cover executor `blocked`/`failed` and reviewer
`changes_requested`/`blocked`/`inconclusive` with exact IDs and allowed
non-passing statuses. The executor missing-coverage case proves non-success
variants still fail closed.

### Prompt Contract

- `test_worker_prompt_uses_exact_plan_criterion_ids_for_every_example`
- `test_worker_attention_examples_are_valid_result_instances`
- `test_worker_prompt_embeds_authoritative_result_schemas`
- `test_worker_prompt_lists_exact_schema_discriminator_options`

The primary prompt test uses two real plan criteria and checks ordered one-entry-
per-criterion templates, all-passed success examples, exact-ID attention
examples, explicit final-response instructions, and byte-for-structure equality
with `schema_documents()` output.

### Durable Failures

- `test_worker_failure_maps_known_boundaries_to_stable_categories`
- `test_worker_failure_recognizes_wrapped_repository_validation`
- `test_worker_failure_redacts_before_bounding_detail`
- `test_worker_failure_uses_exception_class_for_empty_detail`
- `test_verification_mismatch_through_worker_boundary_persists_reconciliation`
- `test_repository_validation_failure_persists_actionable_detail`
- `test_non_retryable_adapter_failure_persists_provider_category_and_redacted_detail`
- `test_worker_boundary_persists_failure_detail_bounded_to_5000_characters`
- `test_worker_process_failures_never_advance_the_step`
- `test_timeout_uses_one_bounded_cooldown_retry_then_completes`

These tests cover authoritative dispatch fields, event reasons, active
reconciliation, provider-category preservation, wrapped repository errors,
credential redaction, 5,000-character bounds, retryable stall preservation, and
absence of a second generic failure record for the retry path.

### Batch Logging

- `test_batch_preparation_is_all_or_none_and_failed_children_join_durably`
- `test_successful_batch_forwards_and_acknowledges_every_child`
- `test_failed_batch_child_warns_once_and_preserves_successful_sibling`

The tests prove one warning per failed child, exact dispatch IDs, stable category,
redacted detail in state/events/logs, no success warnings, completed final
reconciliation, and unchanged accepted/forwarded successful sibling state.

### Property Tests

- `test_completed_result_context_requires_exact_unique_all_passed_verification`
- `test_blocked_result_context_allows_any_status_only_with_exact_coverage`

Generated completed results include duplicate, missing, unknown, failed, and
skipped verification combinations. Parsing remains structural; the separate
plan-context validator decides exact coverage and success status semantics.

## Verification Results

Focused final results:

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/unit/test_sequential.py -q` | `51 passed in 5.81s` |
| `.venv/bin/python -m pytest tests/unit/test_sequential.py tests/unit/test_execution.py tests/property/test_contracts.py -q` | `71 passed in 7.76s` |
| `.venv/bin/python -m pytest tests/integration/test_batch_execution.py tests/fault_injection/test_sequential_execution.py -q` | `9 passed in 8.45s` |

Required full non-live command:

```text
.venv/bin/python -m pytest tests -q -m "not live_opencode"
346 passed, 5 deselected in 48.27s
```

Static checks:

```text
.venv/bin/ruff check src/dispatcher/sequential.py src/dispatcher/execution.py tests/unit/test_sequential.py tests/unit/test_execution.py tests/property/test_contracts.py tests/integration/test_batch_execution.py tests/integration/test_baseline_adoption.py tests/integration/test_workspace_barrier.py tests/fault_injection/test_sequential_execution.py tests/fixtures/opencode/fake_cli.py
All checks passed!

git diff --check
no output (pass)
```

## Constraints And Deviations

- No commit was created or amended, no branch was created, and nothing was pushed.
- No live OpenCode or live-marked test was run; the full suite explicitly deselected `live_opencode` tests.
- No network or HTTP call was made.
- No credentials were used or accessed.
- No `config/projects/local/`, `config/state/`, repository-root `state/`, private T2 state, or other prohibited path was inspected or modified.
- Strict one-object parsing, duplicate-key rejection, identity/revision/evidence checks, repository cleanliness, cancellation/process identity, and Step 1 operator recovery were not weakened.
- Step 3 disposable live-fixture behavior was not implemented.
- All implementation and report changes remain uncommitted.
- There were no deviations from the requested Step 2 scope.
