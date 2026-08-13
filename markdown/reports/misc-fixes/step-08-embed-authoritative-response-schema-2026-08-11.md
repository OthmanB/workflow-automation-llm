# Step 8 - Embed Authoritative Response Schema

**Date:** 2026-08-11  
**Status:** Implemented, verified, uncommitted, for human review.

## Real Findings

This closes a response-instruction defect class exposed by two consecutive
disposable live runs:

1. The first reviewer invented the top-level `"verdict": "rejected"` because
   the prompt showed only the `accepted` example and did not enumerate the
   closed verdict vocabulary. Step 7 added schema-derived `outcome_options` and
   `verdict_options`; the next reviewer correctly used `changes_requested`.
2. That next reviewer then invented a nested `ReviewFinding` shape with
   `severity: "medium"`, an extra `detail` field, and no `finding_id`. Strict
   validation correctly rejected all three errors. The prompt had shown only
   `"findings": []`, so it provided no exact nested-object guidance.

Both validators were correct and remain unchanged. The recurring cause was
that examples alone did not expose every closed enum, nested required field,
forbidden extra field, and discriminated-union branch.

## Implementation

`src/dispatcher/sequential.py:56` imports the existing
`schema_documents()` generator. `_worker_prompt` selects the role-specific
generated document at `src/dispatcher/sequential.py:2070-2072` and embeds the
live Pydantic-model JSON Schema as `response_json_schema` at
`src/dispatcher/sequential.py:2140`. Executor contexts receive
`executor-result-v1.json`; reviewer contexts receive
`reviewer-result-v1.json`. The field is generated from the same models used by
`parse_executor_result` and `parse_reviewer_result`, not copied from the
checked-in schema files or maintained separately.

`final_response_check` at `src/dispatcher/sequential.py:2141-2152` now tells
the worker to conform to `response_json_schema` exactly: no extra fields, no
missing required fields, and no values outside any enum. It retains the Step 7
exact outcome/verdict vocabulary instruction.

The existing happy-path `response_template` remains at
`src/dispatcher/sequential.py:2163-2178`. A second
`response_requires_attention_template` is generated through the same
`_worker_response_template` function at
`src/dispatcher/sequential.py:2179-2194`:

- The executor example is a valid `blocked` result with a populated `blockers`
  list, failed verification, valid evidence hashes, and valid repository
  coordinates (`src/dispatcher/sequential.py:2245-2275`).
- The reviewer example is a valid `changes_requested` result with a populated
  `ReviewFinding` containing exactly `finding_id`, `severity`, and `summary`,
  plus failed verification and required remediation
  (`src/dispatcher/sequential.py:2293-2315`).

No model in `results.py`, parser, decoder, required-response field list, or
acceptance rule was weakened or broadened.

## Prompt Size Decision

Before implementation, the published generated files were measured:

```text
429 lines, 10,806 bytes  schemas/executor-result-v1.json
581 lines, 14,871 bytes  schemas/reviewer-result-v1.json
```

The compact JSON representation actually embedded by `_worker_prompt` is
6,371 bytes for executor and 8,782 bytes for reviewer. This is roughly a few
thousand tokens per dispatch, not close to the project's approximate 50%
step-context budget by itself. It is meaningful overhead, but not clearly
excessive relative to the cost of failed live dispatches and the risk of
omitting an untested nested constraint.

The full schema was therefore retained rather than trimmed. A role-specific
schema already excludes the other worker role, while further trimming could
silently remove exactly the uncommon branch or nested enum that this change is
intended to expose. Correctness and permanent schema lockstep outweigh the
modest token saving. `schema_documents()` currently regenerates the documents
for each worker prompt; this keeps the source direct and measurable. Caching
could be considered separately if profiling demonstrates CPU cost, without
changing prompt content.

## Tests

New tests:

- `test_worker_prompt_embeds_authoritative_result_schemas` at
  `tests/unit/test_sequential.py:297-306` prepares executor and reviewer
  contexts and structurally compares each embedded schema with the matching
  current `schema_documents()` result. Any underlying model change therefore
  reaches both sides in lockstep, and wrong role selection or stale prompt
  wiring fails the test.
- `test_worker_attention_examples_are_valid_result_instances` at
  `tests/unit/test_sequential.py:309-326` passes both concrete attention
  examples through the real `parse_executor_result` and
  `parse_reviewer_result` functions, then asserts the blocked branch and exact
  correctly shaped finding.
- `test_reviewer_rejects_live_invalid_finding_shape` at
  `tests/unit/test_results.py:156-172` reproduces the live `medium` severity,
  missing `finding_id`, and extra `detail` field and asserts all three strict
  diagnostics. This documents that validation was already correct and remains
  fail-closed.

Verification:

```text
.venv/bin/python -m pytest tests/unit/test_sequential.py tests/unit/test_results.py tests/contract/test_schema_export.py -q -m "not live_opencode"
36 passed in 2.88s

.venv/bin/ruff check src/dispatcher/sequential.py tests/unit/test_sequential.py tests/unit/test_results.py
All checks passed!

.venv/bin/mypy src/dispatcher/results.py src/dispatcher/sequential.py
Success: no issues found in 2 source files

.venv/bin/python -m pytest tests -q -m "not live_opencode"
266 passed, 5 deselected in 39.38s
```

The final pytest count is strictly greater than Step 7's 263 passing tests.

## Expected Prevention

The prompt now carries the full executable contract for every dispatch. It
describes `ReviewFinding`, `VerificationResult.status`, `ArtifactRecord`,
blocked/failed/inconclusive branch-only fields, every enum, all required
fields, and `extra=forbid` constraints even when a live run has not exercised
them. The concrete attention examples complement rather than replace that
formal contract. Consequently, another field-level gap cannot silently arise
from an omitted happy-path example: changing the Pydantic model changes the
embedded schema automatically, and the structural synchronization test guards
role selection and prompt wiring.

## Scope Confirmation

No commit, push, or branch creation was performed. All changes, including this
report, remain uncommitted for human review. No live OpenCode invocation,
network or HTTP call, credential use, or external service call was made.
`config/projects/local/`, `config/state/`, and `state/` were neither read nor
modified.
