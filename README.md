# workflow-automation-llm

`dispatcher` is a plan-driven supervisor, executor, and reviewer coordinator
for persistent OpenCode sessions. It owns configuration validation, policy
compilation, repository/evidence checks, workflow transitions, and durable
recovery state. Supervisors propose typed JSON commands; they do not authorize
or complete work by prose alone.

> **Safety boundary:** public configurations remain mock workflow test mode.
> Real OpenCode and repository-mutating execution are available only through a
> private, separately guarded command and have not been enabled here. The
> supported default runtime is deterministic fake OpenCode, preflight,
> inspection, recovery, and derived support tooling. The opt-in live smoke test
> is read-only and requires explicit environment gates.

## Current Status

| Capability | Status | Limit |
|---|---|---|
| Schema-v1 config, plans, commands, results, and state | Implemented | Unknown or missing fields fail closed |
| SQLite state, leases, recovery, and evidence | Implemented | `RUNNING` work is never retried automatically |
| Review profiles, budgets, and operator gates | Implemented | Measured worker usage only; live execution remains disabled |
| Bounded worktree barriers | Implemented | Same-repository writes require clean `commit_policy: required` repositories; patch-only barriers remain unsupported |
| Permission compilation | Implemented and fake-child tested | Live enforcement needs the opt-in compatibility suite |
| Observability and support export | Implemented | Retention applies only to derived artifacts, never SQLite state |
| Private reference migration | Deferred | Requires separate authorization; no private project data is present here |
| Real OpenCode execution | Guarded, not enabled | Requires private schema-v2 config and `dispatcher execute` |

## Documentation

| Document | Purpose |
|---|---|
| [`docs/compatibility.md`](docs/compatibility.md) | Installation, pinned OpenCode contract, and live-smoke gate |
| [`docs/config-schema.md`](docs/config-schema.md) | Public schema-v1 configuration and policy guidance |
| [`docs/normalized-plan-schema.md`](docs/normalized-plan-schema.md) | Immutable normalized plan and approval contract |
| [`docs/protocol.md`](docs/protocol.md) | Strict supervisor commands and typed worker results |
| [`docs/workflow-state-schema.md`](docs/workflow-state-schema.md) | Run, step, dispatch, batch, and recovery transitions |
| [`docs/operations.md`](docs/operations.md) | Safe operational command procedures and exit behavior |
| [`docs/migration.md`](docs/migration.md) | Migration from proof-of-concept artifacts to SQLite state |
| [`config/projects/example.yaml`](config/projects/example.yaml) | Sanitized, validating public example |
| [`markdown/plans/dispatcher-remediation-plan-2026-08-09.md`](markdown/plans/dispatcher-remediation-plan-2026-08-09.md) | Authoritative remediation checklist and release gates |
| [Phase 6](markdown/reports/dispatcher-phase-6-execution-report-2026-08-10.md), [Phase 7](markdown/reports/dispatcher-phase-7-execution-report-2026-08-10.md), [Phase 8A](markdown/reports/dispatcher-phase-8a-assurance-report-2026-08-10.md), [Phase 8B](markdown/reports/dispatcher-phase-8b-observability-report-2026-08-10.md) | Current implementation closure reports |

`docs/design.md` and `docs/roadmap.md` are historical design material. The
schema documents, operations guide, generated JSON schemas, and remediation
plan are authoritative for current behavior.

## Commands

```text
dispatcher preflight --config <project.yaml>
dispatcher execute --config <private-v2.yaml> --run-id <id> --plan <plan.yaml> \
  --repo-id <repo> --smoke-proof <proof.json> --smoke-model <provider/model> \
  --permission-digest <sha256> --stall-policy-digest <sha256> \
  --approval-ref <decision> --confirm-real-operation
dispatcher start --config <project.yaml> --run-record <run.json>
dispatcher status --config <project.yaml> [--run-id <id>] [--format text|json]
dispatcher resume --config <project.yaml> --run-id <id>
dispatcher recover --config <project.yaml> --run-id <id>
dispatcher cancel --config <project.yaml> --run-id <id> --dispatch-id <id> --actor-id <id>
dispatcher answer --config <project.yaml> --run-id <id> --request-id <id> --answer <value> --actor-id <id>
dispatcher support --config <project.yaml> --run-id <id>
dispatcher prune --config <project.yaml> --apply
dispatcher baseline inspect|approve ...
```

See [`docs/operations.md`](docs/operations.md) for required preconditions and
durable state effects. Authoritative-state `archive` is not implemented; never
delete or edit `dispatcher.sqlite3` manually.

## Development Verification

```bash
python -m pip install -e ".[dev]"
PYTHONPATH=src python -m pytest
ruff check src tests
mypy src
pip-audit --strict .
python -m build
twine check --strict dist/*
```

The ordinary test suite skips the live smoke. Run it only with a safe,
read-only model configuration:

```bash
DISPATCHER_LIVE_OPENCODE=1 DISPATCHER_LIVE_MODEL=<provider/model> \
  PYTHONPATH=src python -m pytest -m live_opencode
```
