# Dispatcher Stall and Cancellation Recovery Report

**Execution date:** 2026-08-11
**Scope:** Cancellation and bounded stall recovery. Real OpenCode remains
disabled.

## Implemented

- Added required YAML `execution.stall_policy` with maximum retries, cooldown,
  and exhaustion action.
- Added explicit provider/process error categories for timeout, interruption,
  connection, rate limit, context overflow, quota, authentication, permission,
  protocol, and unknown failures.
- Retryable interruptions create a new durable dispatch with a continuation
  instruction; the failed attempt and stall count remain in run history.
- Quota, authentication, permission, and unknown failures do not retry
  automatically.
- Added `dispatcher cancel`. It records cancellation intent and an audit event
  before checking/signalling the verified local process group.
- Added operator wait after stall exhaustion, with explicit retry or halt answers.
- Added process host/start metadata to durable dispatch state.

## Verification

```text
PYTHONPATH=src .venv/bin/python -m pytest
226 passed, 1 skipped

ruff check src tests
All checks passed!

mypy src
Success: no issues found in 29 source files
```

## Boundary

The real provider smoke and real-operation mode remain separate gates. Provider
classification uses exact known names first; unknown provider failures stop for
operator reconciliation rather than being guessed as retryable.
