# workflow-automation-llm

An orchestration layer ("**dispatcher**") that automates the
supervisor → executor → reviewer loop over **persistent `opencode` sessions**,
so the operator no longer has to copy supervisor decisions into slave sessions
by hand.

> **Proof-of-concept safety notice:** real OpenCode dispatch execution is
> disabled while the sequential integration gate is being closed. Only
> deterministic fixtures, `--mock`, preflight, and state-inspection commands
> are available for development and inspection. Do not rely on the current
> permission, resume, review, or completion behavior for repository-mutating
> work.

The system assumes the project it automates already has accurate specifications
and a well-specified plan. It is a *router between LLM conversations*,
not another agent framework: the LLMs run inside `opencode`; this project only
decides who talks to whom, with what prompt, and records everything.

## Target behavior

- One **supervisor** LLM (persistent session, never closed, resume by session
  id) that keeps the global view of the plan.
- One or more **executor** LLM sessions that receive scoped tasks.
- One or more **reviewer** LLM sessions that independently review work.
- A YAML config per project: paths (specs, plans, evidence, archive),
  role assignments, review policy, cost/speed profile, and parallelism rules.
- A loop that: asks the supervisor for a decision → routes it to the right
  executor/reviewer → captures the response (chat + evidence `.md`) →
  forwards it to the supervisor → repeats until the plan is done.
- Restart/resume: every session id and every transcript is persisted, so a
  run can be stopped and continued (`opencode -s <session_id>`).
- Conflict detection: if the allowed role pool does not match what the plan
  requires, the run stops and asks the operator to resolve the mismatch.
- **Pre-flight safety net**: before the first dispatch, credentials are
  verified (git auth, MCP env vars, a cheap per-model smoke test) and an
  authorization manifest is compiled into opencode's native `permission`
  rules — so agents can't touch anything the plan doesn't grant, and opencode
  stops interrupting you for permissions.
- **Underspec handling**: `ask` mode (pauses for your answer, persisted) or
  `auto` mode ("take the best you think makes sense for this project").
- **Plan actionability pre-flight**: optionally detects non-actionable plan
  steps and has a planner produce an actionable breakdown (`.md`).

## Documentation

| Document | Contents |
|---|---|
| [`docs/design.md`](docs/design.md) | Requirements understanding, architecture, the loop, state/resume, profiles, parallelism, conflict detection, open questions |
| [`docs/protocol.md`](docs/protocol.md) | The machine-readable **dispatch envelope** and the response/forwarding contract between supervisor and dispatcher |
| [`docs/config-schema.md`](docs/config-schema.md) | Full YAML configuration schema with annotations |
| [`docs/normalized-plan-schema.md`](docs/normalized-plan-schema.md) | Generic normalized-plan contract and import adapters |
| [`docs/workflow-state-schema.md`](docs/workflow-state-schema.md) | Run, step, dispatch states, and completion invariants |
| [`docs/roadmap.md`](docs/roadmap.md) | Implementation phases and the decisions to confirm |
| [`config/profiles.yaml`](config/profiles.yaml) | Cost/speed profile definitions (economy / balanced / thorough) |
| [`config/projects/example.yaml`](config/projects/example.yaml) | Sanitized local example configuration |
| [`markdown/reviews/dispatcher-project-review-2026-08-09.md`](markdown/reviews/dispatcher-project-review-2026-08-09.md) | Severity-ranked architecture and implementation review |
| [`markdown/plans/dispatcher-remediation-plan-2026-08-09.md`](markdown/plans/dispatcher-remediation-plan-2026-08-09.md) | Gated remediation implementation plan |
| [`markdown/reports/dispatcher-phase-0-execution-report-2026-08-09.md`](markdown/reports/dispatcher-phase-0-execution-report-2026-08-09.md) | Phase 0 changes, verification evidence, and remaining gate |
| [`markdown/reports/dispatcher-phase-1-execution-report-2026-08-09.md`](markdown/reports/dispatcher-phase-1-execution-report-2026-08-09.md) | Phase 1 contract implementation and verification evidence |

Diagrams: [`docs/diagrams/architecture.mmd`](docs/diagrams/architecture.mmd),
[`docs/diagrams/loop.mmd`](docs/diagrams/loop.mmd).

## Status

**Proof of concept under remediation.** The project now has strict schema-v1
contracts, a pinned OpenCode adapter, a private SQLite state authority, and a
plan-driven sequential workflow facade. Real OpenCode execution remains
deliberately blocked until every workflow gate is wired through that facade.

| Capability | Status | Current limitation |
|---|---|---|
| Mock supervisor/executor loop | Implemented for development | Canned scenario; not an acceptance test for real OpenCode |
| Schema-v1 project configuration | Implemented | Strict mock-only contract; real execution remains disabled |
| Normalized plan, protocol, and state contracts | Implemented | The Phase 4 facade validates them; the legacy mock loop is not authoritative |
| Basic path, Git, credential, and disk preflight | Partial | Phase 0 repairs pass; strict configuration remains Phase 1 |
| Real OpenCode dispatch | Disabled | No real launch is permitted until the Phase 4 facade is the only execution path |
| Permission enforcement | Partial | Effective policy is compiled and supplied to isolated child environments; live enforcement tests remain open |
| Durable resume and crash recovery | Partial | SQLite generations, leases, recovery classification, and state commands exist; legacy loop integration remains open |
| Reviewer and completion enforcement | Partial | The Phase 4 facade enforces typed results, reviewer verdicts, and completion; the legacy loop does not use it |
| Historical baseline | Partial | Read-only inspect and explicit approval keep unverifiable work pending by default |
| Multi-repository execution | Unavailable | All sessions currently use one project root |
| Profiles, budgets, and operator gates | Design only | Configuration is not mechanically enforced |
| Batch and parallel execution | Design only | Explicitly deferred until sequential correctness is proven |

## Development verification

Install development dependencies and run the local quality checks:

```bash
python -m pip install -e ".[dev]"
ruff check src tests
mypy src
pytest
python -m build
```

Real dispatch remains blocked. A mock run uses:

```bash
dispatcher run --config config/projects/example.yaml --mock --skip-smoke
```
