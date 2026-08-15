# Migration from the Proof of Concept

## Authority Change

Legacy `state.json`, `sessions.json`, Markdown transcripts, and JSONL audit
files are not recovery authorities. The SQLite database at
`state.directory/dispatcher.sqlite3` is authoritative for run, step, dispatch,
session, lease, review, evidence, and operator-decision state.

Derived artifacts may be regenerated or retained according to the explicit
`observability.retention` policy. Never delete, edit, or archive the SQLite
database outside a separately approved lifecycle design.

A legacy project subtree containing only `state.json`, `sessions.json`, old
Markdown transcripts, mock session IDs, and JSONL audit output may be archived
or removed after confirming that no active configuration points to it. This does
not make the top-level `state/` convention obsolete; configured projects may
recreate it. Never include `dispatcher.sqlite3` in legacy cleanup.

## Project Configuration

Replace ad-hoc paths and implicit defaults with a schema-v2 project YAML:

- register every repository with its root, remote, evidence roots, writable
  roots, commit policy, and permission policy;
- register supervisor, executor, and reviewer roles by stable role key;
- select a schema-v1 review profile;
- declare budgets, bounded concurrency, and observability retention explicitly;
- use `execution.mode: mock_workflow_test` until the private real-operation gate
  is approved.

Validate the result against `schemas/project-config-v1.json` and the public
example before starting a run.

Schema version 2 supports MCP without requiring project duplication. Omit `mcp`
to inherit the operator's normal OpenCode Context7, Repomix, and Semble setup;
add per-role lists only to narrow the default catalog. Add an explicit `mcp`
registry only when the project needs to replace the global server definitions or
disable MCP with an empty registry.

## Plans and Baselines

Convert a plan to `NormalizedPlan` schema-v2 with source hashes, dependencies,
inputs/outputs, repository and resource ownership, authorization, acceptance,
evidence, review, and retry policy. A plan requires an explicit approval before
run creation.

Schema-v2 authorization requires explicit repository-relative `writable_paths`.
Persisted plans created before this requirement are intentionally not resumed by
inferring a broad scope; create and approve a new plan and run. Required-commit
repositories also require explicit `execution.structured_git` author and
committer identity. Executors now return `dispatcher.executor_proposal.v2` and
must not run tests, stage, or commit. The dispatcher owns verification, evidence
metadata, and `dispatcher.executor_result.v1` materialization.

Historical work is not automatically trusted. Baseline inspection records facts
only; an operator must supply one PENDING, ACCEPTED, or WAIVED decision for each
step. Accepted requires current required evidence and review proof, while Waived
requires its own operator decision reference. Start a new run with
`--use-approved-baseline` only after that immutable approval exists. Do not
import private reference data into this public repository.

Approvals are append-only. If repository revision, evidence, review proof, plan,
or sources change, the previous approval no longer validates; inspect again and
record a new explicit approval. Historical review proof follows the public
convention `<evidence-root>/reviews/<step-id>.*`. Legacy step-prefixed review
files matching `<evidence-root>/<lowercase-step-id>*review*` are observed too,
but never automatically accepted.

## Operational Migration

Use `start`, `status`, `resume`, `recover`, `answer`, `support`, and `prune` as
documented in [`operations.md`](operations.md). Existing `run` behavior remains
mock-only. Real OpenCode compatibility is a separately gated, read-only smoke
suite and does not authorize repository mutation.
