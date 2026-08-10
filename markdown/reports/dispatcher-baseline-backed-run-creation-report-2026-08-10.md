# Baseline-Backed Run Creation Report

**Execution date:** 2026-08-10
**Scope:** Sanitized baseline adoption into a new dispatcher run. No private
project configuration, repository history, or evidence was inspected.

## Implemented

- Added `create_run_from_baseline`, which requires both a current plan approval
  and a validated immutable baseline approval.
- Added explicit `dispatcher start --use-approved-baseline` hydration.
- Hydration preserves Accepted evidence/reviewer decisions, records Waived
  decision references, and leaves Pending work pending until normal activation.
- Added a sanitized three-step adoption test: one evidenced Accepted step, one
  explicit Waived step, and one Pending step depending on both.
- The test proves activation makes only the dependency-ready Pending step
  dispatchable, applies a typed mock executor result, and completes the run
  without redispatching historical work.

## Verification

```text
PYTHONPATH=src .venv/bin/python -m pytest
210 passed, 1 skipped

ruff check src tests
All checks passed!

mypy src
Success: no issues found in 29 source files
```

## Next Boundary

The generic adoption workflow is ready. Applying it to the private T2 project
remains separately authorized and must start with a read-only baseline
inspection, explicit decisions for every historical step, and mocked continuation
from the first dependency-ready Pending step.
