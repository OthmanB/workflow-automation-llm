# Dispatcher Phase 1 Execution Report

**Execution date:** 2026-08-09
**Plan:**
[`markdown/plans/dispatcher-remediation-plan-2026-08-09.md`](../plans/dispatcher-remediation-plan-2026-08-09.md)
**Scope:** DISP-100 through DISP-105 and the Phase 1 contract gate.

## Status

Phase 1 contract implementation is complete locally. The dispatcher now has
strict schema-v1 contracts for project configuration, repository registration,
normalized plans, supervisor commands, executor and reviewer results, workflow
state, plan approval, and completion obligations.

This is a contract milestone, not permission-safe production execution. Real
OpenCode dispatch remains disabled before configuration loading. The typed
`run-record.json` checkpoint is not transactional and must not be used as a
crash-recovery guarantee before Phase 3.

## DISP-100: Strict configuration

Implemented:

- Replaced mutable `dict` configuration with strict Pydantic v2 schema-v1
  models and forbidden unknown fields.
- Required explicit execution timeout, round limit, state path, protocol
  version, mock-only mode, profile source, policy references, and evidence
  policy.
- Removed runtime defaults from `config.py`.
- Resolved declared paths relative to the configuration file, not the process
  working directory.
- Validated profile files through a separate strict schema-v1 document.
- Added generated `schemas/project-config-v1.json` and
  `docs/config-schema.md`.
- Migrated `config/projects/tier2-demo.yaml` and `config/profiles.yaml` to
  schema-v1.

The current schema deliberately allows only `execution.mode: mock_only` and
sequential scheduling. Unsupported legacy settings fail closed as unknown
configuration fields.

## DISP-101: Repository registry

Implemented:

- Replaced `project.root` with stable `repositories.<repo_id>` entries.
- Required canonical root, exact remote, default branch, evidence roots,
  writable roots, policy reference, and explicit overlap permission.
- Validated existing paths, symlink containment, duplicate roots, writable-root
  overlap, exact Git remote URLs, and policy references.
- Added exact repository lookup APIs; substring-based policy selection is no
  longer part of the configuration contract.
- Added two-sibling-repository and path-escape regression coverage.

The sanitized reference configuration registers only the source repository that
has a defined repository-local evidence root. Additional repositories must be
registered with real roots and evidence policy before a multi-repository plan
can be approved in a later phase.

## DISP-102: Normalized plans and approval

Implemented:

- Added immutable normalized-plan schema-v1 with source file identities and
  hashes, ordered generic steps, dependencies, inputs, outputs, locks, risk,
  authorization, acceptance, evidence, review, and retry policy.
- Added deterministic semantic `plan_digest` and separate `source_digest`.
- Added explicit YAML-sidecar import that verifies source files and hashes.
- Added `PlanApproval`, requiring an operator decision reference and matching
  semantic/source digests before creation of a schema-v1 run record.
- Added source-change invalidation coverage.
- Added a Tier 2 Markdown reference adapter that validates table step IDs,
  titles, repositories, and Markdown hash against a complete generic sidecar.

The adapter never infers authorization, dependencies, evidence, or acceptance
rules from Markdown. The historical Tier 2 baseline remains unapproved until
the dedicated Phase 4 baseline import process.

## DISP-103 and DISP-104: Machine protocols

Implemented:

- Replaced executable `<<dispatch>>` and natural-language routing with exactly
  one schema-v1 JSON supervisor command.
- Added `dispatch`, `ask_operator`, `halt`, and `request_completion` actions.
- Rejected duplicate JSON keys, comments, unknown fields, raw session IDs,
  batch requests, malformed shapes, trailing prose, and a second JSON object.
- Resolved role kind from configuration, not supervisor prose.
- Added discriminated executor outcomes and reviewer verdicts with result
  identity and immutable review-target validation.
- Updated `docs/protocol.md` and bootstrap instructions; every documented JSON
  example validates through the runtime parser.

`request_completion` remains a safe stop in the legacy mock loop until Phase 4
wires the completion guard into durable execution state.

## DISP-105: Workflow state contracts

Implemented:

- Added explicit run, step, and dispatch states with allowed transitions.
- Required correlated transition events and dispatch intent data.
- Added durable operator request shape for `WAITING_OPERATOR`.
- Added structured completion obligations covering step status, dependencies,
  review acceptance, evidence, operator gates, active dispatches, and pending
  questions.
- Added terminal exit-code mapping and typed `run-record.json` persistence.
- Added generated `schemas/workflow-state-v1.json` and
  `docs/workflow-state-schema.md`.

## Verification

```text
ruff check src tests
All checks passed!

mypy src
Success: no issues found in 19 source files

pytest
91 passed, 1 xfailed

python -m build
Successfully built dispatcher-0.1.0.tar.gz and dispatcher-0.1.0-py3-none-any.whl
```

The strict expected failure remains the Phase 0 compatibility test for the
unimplemented OpenCode 1.18.11 nested-part decoder. It is tracked as DISP-200
and must become an unexpected pass when that adapter is implemented.

Additional checks passed:

- Published JSON Schemas exactly match their runtime Pydantic models.
- The schema-v1 reference configuration loads with its registered
  repository and selected profile.
- A fresh installed wheel loads its CLI outside the checkout.
- A fresh installed wheel rejects non-mock `run` before reading a missing
  configuration file.

## Deferred boundaries

The following controls intentionally remain unavailable and are not implied by
the Phase 1 contracts:

- OpenCode event decoding, process management, and session recovery.
- Mechanical permission compilation and enforcement.
- Transactional persistence, locking, idempotency, and crash reconciliation.
- Plan-to-dispatch scheduling, result application, review loop, and final
  completion execution.
- Multi-repository runtime routing and immutable revision/evidence collection.
- Profile obligations, escalation, budgets, operator answer commands, batches,
  and parallelism.

Hosted GitHub Actions remains unverified because this workspace is not a Git
repository. The local CI-equivalent commands above pass; a future branch push
must confirm the `CI / quality` workflow before recording a hosted green gate.
