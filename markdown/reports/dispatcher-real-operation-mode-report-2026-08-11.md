# Dispatcher Real Operation Mode Report

**Execution date:** 2026-08-11
**Scope:** Step 4 of the readiness plan. This change adds the guarded mode and
command only; the command was not run.

## Implemented

- Project configuration is now schema v2.
- `execution.mode` is explicitly either `mock_workflow_test` or
  `real_operation`.
- Public examples and development fixtures remain `mock_workflow_test`.
- Added `dispatcher execute` as a separate command; the legacy `dispatcher run`
  command remains mock-only.
- Before launching any real process, `execute` checks exact config and plan
  hashes, plan approval, approved baseline, repository identity/branch/clean
  state, live-smoke proof, permission digest, stall-policy digest, preflight,
  recovery state, operator approval reference, and explicit confirmation.
- The approval reference is recorded in the authoritative audit log before the
  execution coordinator can launch a session.

## Not Enabled

- No real-operation configuration was created or used.
- `dispatcher execute` was not invoked.
- No repository, T2 project, model session, or deployment was changed.

## Verification

The rejection matrix confirms public mock mode and missing confirmation fail
before launch. Full execution verification remains a later readiness step using
disposable repositories only.
