# Dispatcher Phase 0 Execution Report

**Execution date:** 2026-08-09
**Plan:**
[`markdown/plans/dispatcher-remediation-plan-2026-08-09.md`](../plans/dispatcher-remediation-plan-2026-08-09.md)
**Scope:** DISP-000 through DISP-003 and the Phase 0 local verification gate.

## Status

Phase 0 implementation is complete in the workspace. The local CI-equivalent
gate passes. Hosted GitHub Actions remains unverified because this workspace is
not a Git repository and therefore has no remote workflow run to inspect.

Real OpenCode dispatch remains deliberately disabled. No model-backed command,
repository mutation, deployment, or infrastructure action was used to complete
Phase 0.

## DISP-000: Proof-of-concept containment

Completed work:

- Added an early CLI guard that rejects every non-mock `dispatcher run` before
  loading project configuration, running preflight, importing the orchestrator,
  or invoking OpenCode.
- Preserved `--mock`, `preflight`, and `status` commands.
- Added a regression test using a nonexistent configuration path to prove the
  real-run guard executes first.
- Replaced the inaccurate "no implementation" README status with a capability
  matrix distinguishing implemented, partial, unavailable, and design-only
  behavior.
- Marked the original roadmap as historical and linked the review and current
  remediation plan.

Safety result:

```text
ERROR    real OpenCode execution is disabled during remediation Phase 0; use --mock for proof-of-concept validation
```

The same result was verified from an installed wheel in a temporary directory
outside the source checkout.

## DISP-001: Quality and CI baseline

Completed work:

- Added development dependencies for build, MyPy, pytest, Ruff, and PyYAML
  typing.
- Added pytest, Ruff, and MyPy configuration to `pyproject.toml`.
- Added unit, contract, integration, and fault-injection suite locations.
- Added `.github/workflows/ci.yml` with read-only repository permissions and a
  15-minute job timeout.
- Added lint, type-check, test, package-build, clean-wheel install, and
  outside-checkout CLI smoke steps.
- Extended `.gitignore` for Python quality caches, package artifacts, coverage,
  and test worktrees.
- Added a local mock end-to-end test using a disposable Git repository and no
  OpenCode process.
- Removed stale imports and corrected timeout exception chaining exposed by the
  new lint baseline.

Environment note:

The workspace Python is externally managed under PEP 668. Development tools
were installed into the ignored project `.venv`; the system environment was
not overridden with `--break-system-packages`.

## DISP-002: Preflight repair

Completed work:

- Removed the undefined-name condition that caused default preflight to crash.
- Passed the injected session runner into model smoke checks.
- Added typed `passed`, `failed`, and `skipped` check records.
- Audited disabled, successful, and failed preflight outcomes.
- Converted unexpected check exceptions into hard preflight failures while
  retaining error type and actionable detail.
- Added Git timeout and operating-system error handling.
- Checked free space at configured project, state, evidence, and optional
  archive locations instead of only the process working directory.
- Made the mock harness return `OK` for smoke probes without a model call.

Covered scenarios:

- enabled preflight with injected smoke runner;
- disabled preflight;
- skipped smoke test;
- successful mock smoke test;
- failed model smoke test;
- missing credential;
- Git failure;
- missing path;
- insufficient disk space; and
- unexpected internal check exception.

## DISP-003: OpenCode compatibility evidence

Completed work:

- Recorded OpenCode `1.18.11` and source tag `v1.18.11` in `pyproject.toml`.
- Added sanitized fixtures under `tests/fixtures/opencode/1.18.11` for new,
  resumed, and forked sessions; text, reasoning, tool, step, usage, error, and
  malformed events; process nonzero exit and timeout; structured session list;
  and export/import behavior.
- Documented fixture provenance and refresh policy.
- Derived event and export/import structures from the exact tagged OpenCode
  source.
- Captured the local structured session-list shape and replaced every value
  with synthetic fixture data.
- Added tests that reject real-looking session IDs, user paths, and common
  secret markers.
- Added a strict expected-failure contract test for the current decoder's lack
  of OpenCode 1.18.11 nested `part` event support.

The decoder test is intentionally `xfail(strict=True)`. Phase 2's DISP-200 fix
will turn it into an unexpected pass, forcing the expected-failure marker to be
removed rather than allowing obsolete compatibility debt to remain hidden.

## Verification results

### Lint

```text
ruff check src tests
All checks passed!
```

### Type checking

```text
mypy src
Success: no issues found in 13 source files
```

### Tests

```text
pytest
22 passed, 1 xfailed
```

The expected failure is:

```text
DISP-200: the Phase 0 decoder does not support OpenCode 1.18.11 part events
```

### CI workflow syntax

```text
CI workflow YAML valid
```

### Package build

```text
Successfully built dispatcher-0.1.0.tar.gz and dispatcher-0.1.0-py3-none-any.whl
```

### Installed-wheel smoke test

The wheel was installed into a fresh virtual environment under the approved
temporary workspace. From outside the source checkout:

- `dispatcher --help` succeeded.
- A non-mock `dispatcher run` returned the Phase 0 safety error before reading
  the intentionally missing configuration file.

### OpenCode version

```text
opencode --version
1.18.11
```

## Remaining Phase 0 gate

The hosted-CI checkbox remains open in the remediation plan. To close it after
this directory is placed in a Git repository:

1. Push the Phase 0 changes to a branch.
2. Open or update a pull request.
3. Confirm the `CI / quality` job passes all lint, type, test, build, wheel
   installation, and outside-checkout smoke steps.
4. Record the workflow URL in this report.
5. Mark the hosted-CI Phase 0 gate complete.

No Phase 1 work should re-enable real OpenCode dispatch. The plan keeps real
execution disabled until the transactional and sequential correctness gates in
later phases are satisfied.
