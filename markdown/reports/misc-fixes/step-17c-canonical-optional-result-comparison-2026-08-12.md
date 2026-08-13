# Step 17c: Canonical Optional Result Comparison

**Date:** 2026-08-12  
**Status:** Implemented and verified; changes remain uncommitted

## Files Changed

The Step 17c changes are limited to:

- `tests/live/test_real_operation_disposable.py`
- `tests/unit/test_results.py`
- `markdown/reports/misc-fixes/step-17c-canonical-optional-result-comparison-2026-08-12.md`

No existing uncommitted work from Steps 1-17b was reverted or rewritten.

## Live Assertion Correction

The incorrect assertion compared the persisted result directly with the raw
JSON text returned by the model:

```python
assert stored.result == json.loads(final_text)
```

The reviewer result contract allows `transcript_ref` to be omitted. Pydantic
validation therefore accepts the raw object and canonical `model_dump` adds
`"transcript_ref": None`.

The corrected assertion parses the raw object through the existing result
contract and compares persistence with that canonical representation:

```python
raw_result = json.loads(final_text)
canonical_result = parse_reviewer_result(raw_result).model_dump(mode="json")
assert stored.result == canonical_result
```

This still proves exact canonical contract persistence. It does not extract,
guess, rename, rewrite, repair, or otherwise reinterpret any response field.

## Parser Signature And Target

The current source defines:

```python
def parse_reviewer_result(payload: object) -> ReviewerResult
```

It accepts only the payload. The current `DispatchIntent` model has no
`review_target` field. The prepared dispatch stores the separate typed value as
`PreparedDispatch.review_target: ReviewTarget | None`, and production context
validation compares the parsed result with that immutable target before
persistence. The supported invocation is therefore
`parse_reviewer_result(raw_result)`, rather than the two-argument sketch.

## Prompt Wording

The adversarial reviewer prompt now says:

```text
Return one schema-valid review result. The dispatcher may canonicalize explicitly
optional defaults, but must not repair or reinterpret the response.
```

Mutation and permission-boundary instructions were not weakened.

## Why This Is Not Response Repair

`transcript_ref` is explicitly declared as `Identifier | None = None` in both
result base models. Omitting that optional field is valid input. Contract
validation applies the declared default, and canonical JSON persistence includes
the resulting `null` field. No missing required field is supplied, no value is
inferred from another field, and no synonym or malformed response is rewritten.
This is normal schema validation and default canonicalization, not response
repair or reshaping.

## Unit Regression Tests

The new focused tests use the existing valid payload builders:

- `test_executor_result_omitting_optional_transcript_ref_canonicalizes_to_none`
- `test_reviewer_result_omitting_optional_transcript_ref_canonicalizes_to_none`

Both remove only the optional `transcript_ref`, parse the result, call
`model_dump(mode="json")`, and assert that the canonical dictionary contains
`"transcript_ref": None`. Existing required-field rejection tests were left
unchanged.

## Verification

Focused result tests:

```text
.venv/bin/python -m pytest tests/unit/test_results.py -q
12 passed in 0.21s
```

Full non-live suite:

```text
.venv/bin/python -m pytest tests -q -m "not live_opencode"
409 passed, 10 deselected in 56.88s
```

The full non-live pass count is greater than the 407-pass baseline.

Live collection only:

```text
.venv/bin/python -m pytest tests/live/test_real_operation_disposable.py --collect-only -q
31 tests collected in 0.27s
```

No live-marked test was executed.

Ruff:

```text
.venv/bin/ruff check tests/unit/test_results.py tests/live/test_real_operation_disposable.py
All checks passed!
```

Diff validation:

```text
git diff --check
no output (passed)
```

## Scope And Safety Confirmation

- No file under `src/dispatcher/` was modified for Step 17c; the parser source
  was read only to confirm its actual signature.
- No production result model, parser, persistence, permission, or unrelated
  prompt was modified.
- No live test was run.
- No network call or credential was used.
- No commit, push, amend, branch creation, or destructive Git operation was
  performed in the working repository.
- No prohibited path was modified or used for edits.
- All changes remain uncommitted.

## Deviation

The requested invocation sketch included
`parse_reviewer_result(raw_result, reviewer.intent.review_target)`. Inspection
of the current source showed that the parser supports only one payload argument
and `reviewer.intent.review_target` does not exist on the current
`DispatchIntent`. The one-argument canonical parse was used to follow the
actual supported API; immutable review-target binding remains covered by the
existing dispatcher validation path.
