# Step 7 - Explicit Outcome And Verdict Vocabulary

**Date:** 2026-08-11  
**Status:** Implemented, verified, uncommitted, for human review.

## Finding And Scope

A disposable live reviewer run returned `"verdict": "rejected"`. This is not
one of the four discriminated-union tags accepted by `ReviewerResult`, so the
existing strict validation correctly failed closed. That validation was not
changed, and `rejected` was not added as a synonym or accepted value.

The worker JSON context previously supplied only the `accepted` reviewer
template example and the `completed` executor template example. It did not
enumerate each role's closed discriminator vocabulary, leaving a model to infer
valid words for a non-accepting review outcome.

## Implementation

`src/dispatcher/results.py:201-220` adds `_discriminator_options`, which
introspects the actual `ExecutorResult` and `ReviewerResult` discriminated
union aliases. It follows the union variants in declaration order, reads each
variant's discriminator field from `model_fields`, and returns that field's
`Literal` arguments. The derived, ordered constants are:

- `EXECUTOR_OUTCOME_OPTIONS`: `("completed", "blocked", "failed")`
- `REVIEWER_VERDICT_OPTIONS`: `("accepted", "changes_requested", "blocked", "inconclusive")`

This has no hand-maintained second vocabulary. Adding a new discriminator
variant to either accepted result union automatically changes the corresponding
constant and therefore the worker context. The helper also fails at module load
if a union variant lacks a string `Literal` discriminator or introduces a
duplicate value, preventing a silent malformed prompt vocabulary.

`src/dispatcher/sequential.py:40-43` imports these constants, and
`src/dispatcher/sequential.py:2131-2145` adds exactly one role-specific JSON
context field:

- Executor contexts contain `"outcome_options": ["completed", "blocked", "failed"]`.
- Reviewer contexts contain `"verdict_options": ["accepted", "changes_requested", "blocked", "inconclusive"]`.

The same lines strengthen `final_response_check`: the model is told that its
`outcome` or `verdict` MUST be exactly one of the listed derived values and
that no other word, synonym, or variation is acceptable. Existing
`required_response_fields`, response templates, decoder behavior, and strict
result validation remain unchanged.

## Tests

No pre-existing test asserted an exact worker-context key set; a focused search
for `_worker_prompt`, `required_response_fields`, `final_response_check`, and
`response_contract_rule` found no such assertion. The new test deliberately
asserts the exact new key and excludes the other role's key instead of weakening
or replacing an existing shape assertion.

New tests in `tests/unit/test_sequential.py`:

- `test_worker_prompt_lists_exact_schema_discriminator_options`
  (`tests/unit/test_sequential.py:251-290`) prepares real executor and reviewer
  dispatches, decodes their rendered context JSON, asserts the exact ordered
  options lists, asserts the other role's key is absent, and checks the
  no-synonym final-response instruction.
- `test_discriminator_option_constants_track_result_union_literals`
  (`tests/unit/test_sequential.py:293-304`) independently walks the
  `ExecutorResult` and `ReviewerResult` union `Literal` annotations and
  compares them to the exported constants. This test would fail if a constants
  implementation became stale relative to the result schema. In the shipped
  implementation the constants are generated from the same metadata, so drift
  is prevented at runtime as well as cross-checked by the test.

Verification:

```text
.venv/bin/python -m pytest tests/unit/test_sequential.py tests/unit/test_results.py -q -m "not live_opencode"
32 passed in 1.96s

.venv/bin/ruff check src/dispatcher/results.py src/dispatcher/sequential.py tests/unit/test_sequential.py
All checks passed!

.venv/bin/python -m pytest tests -q -m "not live_opencode"
263 passed, 5 deselected in 39.07s
```

The final pytest summary is strictly greater than the prior Step 6 count of
261 passed tests.

## Scope Confirmation

No commit, push, or branch creation was performed. All changes, including this
report, remain uncommitted for human review. No live OpenCode invocation,
network or HTTP call, credential use, or external service call was made.
`config/projects/local/`, `config/state/`, and `state/` were neither read nor
modified.
