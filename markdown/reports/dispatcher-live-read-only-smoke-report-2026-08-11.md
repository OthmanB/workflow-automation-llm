# Dispatcher Live Read-Only Smoke Report

**Execution date:** 2026-08-11
**Scope:** Step 3 of the real-operation readiness plan. No repository or T2
project was used.

## Successful Smoke

- Installed OpenCode version: `1.18.11`.
- Model: `openai/gpt-4.1`.
- Credential handling: a private temporary copy of the existing OpenCode
  credential store was used; credential values were never printed or recorded.
- Working directory: empty temporary directory outside every repository.
- Permissions: deny-all; automatic approval disabled.
- Prompt: fixed `LIVE_SMOKE_OK` response request with no tools or file access.
- Result: exact `LIVE_SMOKE_OK`, valid session identity, exit code 0, and no
  evidence or workdir files before or after execution.
- Repeat run: passed with the same conditions.

## Cancellation Smoke

- A separate managed OpenCode child was started with the same isolated
  credential handling and deny-all permissions.
- The dispatcher cancellation function sent an interrupt to the verified local
  process group and confirmed launcher cleanup.
- Result: typed `interrupted` failure, no completed response, no session identity
  persisted, and no workdir files.
- No background OpenCode process remained after the test.

## Boundary

This proves compatibility, credentials, permission isolation, process control,
and harmless cancellation. It does not enable real repository operation. The
public configuration remains mock workflow test mode, and no T2 project was
modified.
