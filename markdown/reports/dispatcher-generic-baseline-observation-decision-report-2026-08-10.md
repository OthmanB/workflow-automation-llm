# Generic Baseline Observation and Decision Report

**Execution date:** 2026-08-10
**Scope:** Generic adoption of pre-existing project work. No private project
configuration, evidence, or historical baseline was inspected.

## Implemented

- Replaced baseline-v1 proposed statuses with baseline-v2 read-only
  observations plus explicit per-step PENDING, ACCEPTED, or WAIVED decisions.
- Historical observations record repository revision, required evidence hashes,
  review-proof hashes, and gaps without inferring completion.
- Accepted decisions require all current evidence and, when compiled policy
  requires review, review proof plus explicit configured reviewer role keys.
- Waived and Pending decisions require individual reason and operator decision
  references. Every observed plan step requires exactly one decision.
- Added `start --use-approved-baseline`, which hydrates approved Accepted,
  Waived, and Pending states into a new run before normal activation.
- Added SQLite schema v5 with append-only `baseline_approvals`. Changed evidence
  invalidates the prior approval and requires a newly observed approval; prior
  approvals are retained rather than overwritten.

## Generic Review-Proof Convention

The generic inspector recognizes review proof files at
`<evidence-root>/reviews/<step-id>.*`. Their hashes are observed; the dispatcher
does not infer their prose content as acceptance.

## Verification

```text
PYTHONPATH=src .venv/bin/python -m pytest
209 passed, 1 skipped

ruff check src tests
All checks passed!

mypy src
Success: no issues found in 29 source files
```

## Next Boundary

Applying this model to the private T2 project remains separately authorized.
Only after that approval can a real historical baseline be inspected, decided,
and used to continue from the first dependency-ready Pending step.
