# Dispatcher Phase 8B Observability Report

**Execution date:** 2026-08-10
**Scope:** `DISP-801` structured operator visibility, sanitized support
artifacts, and derived-artifact retention. Health and readiness endpoints are
not applicable because no supervised deployment mode exists.

## Implemented

- Added required YAML `observability` controls for JSON log format and level,
  archive/delete retention mode, explicit archive directory, and explicit
  transcript/report/audit/support/archive limits.
- Replaced CLI human-only logging with redacted JSON log records. Each record
  has timestamp, level, module, function, message, and project/run/dispatch/
  step correlation fields when available.
- Added an authoritative `RunStatusSnapshot` that combines SQLite state,
  scheduler readiness reasons, active dispatches and batches, operator wait
  metadata, measured usage, and held leases.
- Added `dispatcher status --format json` alongside a concise text status view.
- Added `dispatcher support --run-id` for a private, derived support bundle:
  redacted status, report, audit export, and a manifest. It never copies the
  authoritative database, raw prompts, child environment, or source config.
- Added `dispatcher prune --apply`. It only archives or deletes configured
  derived artifacts, skips active-run artifacts, and never deletes SQLite rows,
  active state, or unresolved dispatch data.
- Audit JSONL exports now redact structured payload values before writing.

## Verification

```text
PYTHONPATH=src .venv/bin/python -m pytest
196 passed, 1 skipped

ruff check src tests
All checks passed!

mypy src
Success: no issues found in 28 source files
```

## Explicit Limits

- The Gitleaks CI gate introduced in Phase 8A remains the repository secret
  scanner; GitHub Advanced Security is unavailable and Gitleaks is not
  installed locally.
- Retention does not archive or delete authoritative run state. An
  operator-approved lifecycle for state archival remains separate work.
- `DISP-802` private reference migration and `DISP-803` public operations
  documentation remain open Phase 8 work.
