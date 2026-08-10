# Dispatcher Phase 8C Documentation Report

**Execution date:** 2026-08-10
**Scope:** `DISP-803` public contract and operations documentation. This is a
documentation-only slice; it does not enable real execution, migrate private
reference data, or add lifecycle commands.

## Published

- Rewrote the README around current schema-v1, SQLite-authoritative, mock-only
  behavior and linked the current normative guides and Phase 6–8 reports.
- Published installation/OpenCode compatibility guidance and the explicit
  read-only live-smoke gate.
- Updated configuration, protocol, normalized-plan, and workflow-state guides
  for profiles, review waivers, budgets, batches, observability, strict command
  parsing, and derived-artifact retention.
- Published operations for every supported CLI command: mock run, preflight,
  start, status, resume, recover, answer, support, prune, and baseline.
- Published tested allow/ask/deny policy guidance, repository/evidence/review/
  parallelism limits, uncertain-side-effect recovery, and proof-of-concept
  migration guidance.
- Added documentation contract tests for normative links, supported CLI
  commands, mock-only behavior, private migration boundaries, and the explicit
  unsupported cancel/archive boundary.
- Marked historical design and roadmap material as non-operational reference
  documents.

## Explicit Limits

- There is no `cancel` command and no authoritative-state `archive` command.
  The operations guide documents both as unsupported and prohibits manual SQLite
  modification.
- Real OpenCode and repository-mutating execution remain disabled. The live
  smoke suite remains opt-in and was not run.
- Private reference migration remains separately authorized and untouched.
- The final release gate remains open pending those external gates and release
  evidence.

## Verification

```text
PYTHONPATH=src .venv/bin/python -m pytest
199 passed, 1 skipped

ruff check src tests
All checks passed!

mypy src
Success: no issues found in 28 source files
```
