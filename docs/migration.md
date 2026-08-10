# Migration from the Proof of Concept

## Authority Change

Legacy `state.json`, `sessions.json`, Markdown transcripts, and JSONL audit
files are not recovery authorities. The SQLite database at
`state.directory/dispatcher.sqlite3` is authoritative for run, step, dispatch,
session, lease, review, evidence, and operator-decision state.

Derived artifacts may be regenerated or retained according to the explicit
`observability.retention` policy. Never delete, edit, or archive the SQLite
database outside a separately approved lifecycle design.

## Project Configuration

Replace ad-hoc paths and implicit defaults with a schema-v1 project YAML:

- register every repository with its root, remote, evidence roots, writable
  roots, commit policy, and permission policy;
- register supervisor, executor, and reviewer roles by stable role key;
- select a schema-v1 review profile;
- declare budgets, bounded concurrency, and observability retention explicitly;
- use `execution.mode: mock_only` until a future real-execution gate is
  approved.

Validate the result against `schemas/project-config-v1.json` and the public
example before starting a run.

## Plans and Baselines

Convert a plan to `NormalizedPlan` schema-v1 with source hashes, dependencies,
inputs/outputs, repository and resource ownership, authorization, acceptance,
evidence, review, and retry policy. A plan requires an explicit approval before
run creation.

Historical work is not automatically trusted. Baseline inspection records facts
only; an operator must supply one PENDING, ACCEPTED, or WAIVED decision for each
step. Accepted requires current required evidence and review proof, while Waived
requires its own operator decision reference. Start a new run with
`--use-approved-baseline` only after that immutable approval exists. Do not
import private reference data into this public repository.

Approvals are append-only. If repository revision, evidence, review proof, plan,
or sources change, the previous approval no longer validates; inspect again and
record a new explicit approval. Historical review proof follows the public
convention `<evidence-root>/reviews/<step-id>.*`.

## Operational Migration

Use `start`, `status`, `resume`, `recover`, `answer`, `support`, and `prune` as
documented in [`operations.md`](operations.md). Existing `run` behavior remains
mock-only. Real OpenCode compatibility is a separately gated, read-only smoke
suite and does not authorize repository mutation.
