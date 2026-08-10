# Operations Guide

The SQLite database at `state.directory/dispatcher.sqlite3` is authoritative.
Reports, audit JSONL, transcripts, and support bundles are derived artifacts.
Never edit or delete the database manually.

## Preflight

```bash
dispatcher preflight --config <project.yaml>
```

Runs configuration, path, repository, credential, disk, and optional model
checks. Exit `0` means the selected checks passed; exit `1` means preflight
failed. It does not create a run or launch an approved worker dispatch.

## Mock Run

```bash
dispatcher run --config <project.yaml> --mock --skip-smoke
```

This invokes the legacy mock harness only. It is useful for deterministic
development validation but is not the authoritative sequential coordinator and
does not authorize real OpenCode or repository-mutating execution. Omitting
`--mock` exits `2` before configuration loading.

## Start

```bash
dispatcher start --config <project.yaml> --run-record <run.json>
```

Requires a schema-valid `NEW` run record whose project ID and config digest
match the selected project. Persists generation one in SQLite. Exit `0` means
the run was persisted; exit `2` means validation, ownership, or state checks
failed. It does not launch a session.

## Status

```bash
dispatcher status --config <project.yaml> --run-id <run-id> --format json
```

The JSON view reports run state and generation, ready and blocked steps with
reasons, active dispatches and batches, waiting operator metadata, usage, and
leases. The text view is a concise rendering of the same derived snapshot. It
does not mutate run state.

## Resume and Recovery

```bash
dispatcher resume --config <project.yaml> --run-id <run-id>
dispatcher recover --config <project.yaml> --run-id <run-id>
```

`resume` validates one non-terminal, sole active run and does not create a
supervisor session. `recover` classifies unresolved dispatches. A `RUNNING`
dispatch requires operator reconciliation because external side effects may
have completed; it is never automatically retried.

`recover` also reports unfinished workspace groups. `ACTIVE`, `INTEGRATING`,
and `FAILED` groups require reconciliation because temporary child branches may
contain unintegrated work. `CLEANUP_PENDING` means cleanup intent was persisted
before a crash and can be resumed safely by the dispatcher-owned workspace
manager. Do not remove Git worktrees or branches by hand.

## Cancel And Stall Recovery

```bash
dispatcher cancel --config <project.yaml> --run-id <run-id> \
  --dispatch-id <dispatch-id> --actor-id <operator-id>
```

`cancel` records the request before signalling the verified local process group.
It checks the recorded host and process identity and never signals a process on
another host. The managed process receives an interrupt first, followed by the
configured termination sequence if it does not stop.

Timeouts, interruptions, temporary connection failures, temporary rate limits,
and context overflow may retry according to `execution.stall_policy`. Each retry
creates a new dispatch and preserves the old attempt. Provider quota/billing,
authentication/permission, and unknown failures stop for operator action.
After the configured retry count, the run asks the operator or halts. The
project cost/token budget remains a separate optional safety limit.

## Operator Answer

```bash
dispatcher answer --config <project.yaml> --run-id <run-id> \
  --request-id <request-id> --answer <value> --actor-id <operator-id>
```

The request ID, allowed answer, expiration, and required role are validated
inside one SQLite transaction. A successful answer moves the run to the
request's exact durable resume state. Duplicate, expired, or unauthorized
answers fail with exit `2`.

## Support and Retention

```bash
dispatcher support --config <project.yaml> --run-id <run-id>
dispatcher prune --config <project.yaml> --apply
```

`support` writes a private bundle containing redacted status, report, audit,
and manifest files. It never copies the database, raw prompts, child
environment, or source configuration.

`prune` requires `--apply` and follows the explicit `observability.retention`
YAML policy. It archives or deletes only derived artifacts, skips active-run
artifacts, and never removes SQLite rows or unresolved dispatch data.

## Baseline

```bash
dispatcher baseline inspect --config <project.yaml> --plan <plan.yaml>
dispatcher baseline approve --config <project.yaml> --plan <plan.yaml> \
  --observation <observation.json> --decisions <decisions.json> \
  --approval-decision-ref <decision-id>
dispatcher start --config <project.yaml> --run-record <run.json> \
  --use-approved-baseline
```

Inspection is read-only and records observed revisions, evidence hashes, and
review-proof files. Approval requires an explicit PENDING, ACCEPTED, or WAIVED
decision for every step. Accepted requires the current evidence and review proof
required by compiled policy; Waived requires its own operator decision reference.
For generic historical review proof, place a proof file at
`<evidence-root>/reviews/<step-id>.*`; the inspector hashes it without trusting
its prose content. Approval records accepted reviewer role keys explicitly.
For established legacy evidence, the inspector also observes
`<evidence-root>/<lowercase-step-id>*review*` files; an operator still decides
whether those hashes constitute sufficient review proof.
`start --use-approved-baseline` hydrates those durable states into a new run.
Private reference migration remains separately authorized work and is not
performed by the public example.

When the source document has a table of steps, pass the source table and an
explicit ownership map:

```bash
dispatcher baseline inspect --config <project.yaml> --plan <sidecar.yaml> \
  --source-markdown <roles-table.md> --ownership-map <ownership-map.yaml>
```

The importer requires one registered repository per step. Explicit repository
names in the source must agree with the sidecar and map; rows without a
repository fail unless the ownership map supplies one. The map is a project
local input and is not a baseline decision.

## Unsupported Lifecycle Operations

There is no authoritative-state `archive` command. Do not simulate archival by
deleting state files. Halt, cancellation, and recovery decisions must be
recorded through dispatcher-owned workflow and operator-gate paths.
