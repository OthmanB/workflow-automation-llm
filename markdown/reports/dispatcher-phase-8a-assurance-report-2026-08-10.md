# Dispatcher Phase 8A Assurance Report

**Execution date:** 2026-08-10
**Scope:** `DISP-800` release-blocking automated assurance. This report does
not close observability, operations documentation, private-project migration,
or the final release gate.

## Implemented

- Added bounded Hypothesis properties for strict project configuration,
  normalized-plan dependency validation, strict protocol parsing, and valid and
  invalid step-state edges.
- Reopens the SQLite authority after each durable dispatch lifecycle boundary:
  `PREPARED`, `RUNNING`, `COMPLETED`, and `FORWARDED`. Each reopened store
  returns its deterministic recovery disposition without retrying work.
- Extended the deterministic fake OpenCode executable with a permission probe
  that reads the actual isolated `OPENCODE_CONFIG_CONTENT` environment.
- Proved that a compiled project/repository/role/dispatch policy reaches an
  executor child, executor write allow succeeds, reviewer write deny remains
  denied even when `--auto` is present, and an `ask` write never enables
  `--auto`.
- Added an explicitly marked `live_opencode` suite. It is skipped unless both
  `DISPATCHER_LIVE_OPENCODE=1` and `DISPATCHER_LIVE_MODEL` are supplied. Its
  only prompt is read-only and asks for `LIVE_SMOKE_OK`; it does not run in
  ordinary CI.
- Added CI checks for repository secret scanning, `pip-audit --strict .`, and
  strict Twine distribution metadata validation. The clean-wheel job now also
  runs `pip check`.

## Verification

```text
PYTHONPATH=src .venv/bin/python -m pytest
191 passed, 1 skipped

ruff check src tests
All checks passed!

mypy src
Success: no issues found in 27 source files

.venv/bin/pip-audit --strict .
No known vulnerabilities found
```

## Explicit Gates

- The live smoke suite was not run because no explicit live environment gate or
  model was supplied.
- GitHub Advanced Security is unavailable for this repository and Gitleaks is
  not installed locally. The added CI Gitleaks job must pass before a release.
- Real and repository-mutating OpenCode execution remains disabled.
- `DISP-801` observability, `DISP-802` private reference migration, and
  `DISP-803` operations documentation remain Phase 8 work.
