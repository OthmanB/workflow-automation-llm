# Roadmap

> **Historical roadmap:** implementation began before the original external
> review gate below was reconciled. The source tree is now treated as a proof
> of concept, and real OpenCode dispatch is disabled during remediation Phase
> 0. The authoritative current sequence is
> [`markdown/plans/dispatcher-remediation-plan-2026-08-09.md`](../markdown/plans/dispatcher-remediation-plan-2026-08-09.md),
> based on the findings in
> [`markdown/reviews/dispatcher-project-review-2026-08-09.md`](../markdown/reviews/dispatcher-project-review-2026-08-09.md).

## Phase 0 — Decisions (now resolved) and the design review gate

All design decisions are resolved (2026-08-09):

- Name: `dispatcher`.
- Envelope strict + fallback; `halt_mode` (`ask_on_ambiguity` | `full_auto`).
- Model strings verified via `opencode models` on this machine
  (`openai/gpt-5.6-sol`, `openai/gpt-5.6-terra`,
  `github-copilot/claude-sonnet-5`, `github-copilot/kimi-k3`,
  `deepseek/deepseek-v4-flash`). OpenAI models (Sol/Terra) use the `openai`
  provider only (company-paid; never `github-copilot`).
- `underspec_mode` default: `ask`.
- Multi-repo manifests, session titles, mock mode, compaction-aware sizing
  adopted.
- github-MCP write resolved via per-role defaults + per-repo overrides +
  `repo_ops`.

**Original gate before Phase 1:** an external design review (Claude Sonnet 5) of
`docs/design.md`, `docs/protocol.md`, `docs/config-schema.md`, and the
example config. Source code now exists, so this gate is retained as historical
context rather than an assertion about repository state.

## Phase 1 — PoC: sequential supervisor → one executor loop

Minimal but real: prove the primitive works end to end.

- `config.py` (load + validate a single-executor config).
- `sessions.py`: wrap `opencode run -m <model> --variant <v> --dir <dir>`
  capturing: session id, final chat, exit code, usage.
- `preflight.py` basics: model smoke test (`opencode run "Reply with exactly:
  OK"` per model) + paths + git auth check.
- `permissions.py` basics: render a minimal manifest → per-role opencode
  `permission` config; launch with `--auto`; verify `deny` still blocks.
- `loop.py`: bootstrap supervisor → dispatch → forward → repeat (sequential,
  one executor, no reviewers, no profiles).
- `--title "<role> · <step>"` on every session launch (self-describing
  sessions in `opencode session list`).
- `--mock` harness: a scripted responder plays executor/reviewer so routing,
  state, resume and permission compilation are testable without spending
  tokens.
- `audit.py` + `state.json` (basic resume: `-s <id>`).
- Manual validation on one read-only step of a disposable reference plan with
  the operator watching.

**Exit criteria:** the operator never hand-forwards a message for a single
step; supervisor session survives a restart; pre-flight blocks a bad model
string and a denied action before the run.

## Phase 2 — State, resume, transcripts, permissions

- Full `state.json` + `sessions.json` + `transcripts/*.md` + `decisions/`.
- Crash-safe dispatch (audit-before-run; in-flight recovery on restart).
- Session registry: `opencode session list` reconciliation + `export/import`
  recovery path.
- `dispatcher status` / `dispatcher resume` CLI.
- Full permission manifest → opencode compiler (per-role, per-repo,
  `repo_ops` semantic→bash, `external_directory`, `question` per underspec
  mode, MCP tool guardrails).
- Pre-flight layer 2: manifest-vs-plan consistency + persisted operator Q&A.

## Phase 3 — Reviewer loop + cost/speed profiles + underspec

- Add reviewer pool; envelope `role: reviewer`.
- Review schedule generator from `profiles.yaml` × plan step table;
  profile as constraint filter (skip-review flag → HALT in strict modes).
- Escalation ladder: rework rounds, tie-break reviewer, high-risk second
  reviewer, executor reassignment.
- `underspec_mode` (ask/auto): `question` permission wiring, `BLOCKER:`
  marker detection, persisted operator answers.
- Usage/context reporting in the forwarding template; context % in the
  session registry.
- Compaction-aware sizing: pre-resume check that an executor's current
  context % + step budget stays under the compaction trigger, else ask the
  supervisor to fork/consolidate.
- Validate against representative mandatory multi-review steps in a sanitized
  fixture.

## Phase 4 — Parallelism + batch + plan actionability

- Per-role `parallelism`, global `max_parallel`, per-repo locks.
- Batch envelope; concurrent `opencode run` (asyncio); batched forward to
  supervisor.
- Refuse same-repo parallelism; HALT on policy violation.
- Plan-actionability pre-flight (off/ask/auto): planner produces
  `<plan>-actionable.md`; normalize LLM-written plans into the tier-2 table
  format.

## Phase 5 — Hardening

- Natural-language fallback parsing + HALT-on-ambiguity (and `full_auto`
  retry mode).
- `operator_gate` for tagged risky steps; headless decisions inbox (later).
- Budget/cost caps; wall-clock timeouts; step-stuck handling.
- Evidence diff surfaced to supervisor; unexpected-write detection.
- Audit sanitization (secrets) mirroring `opencode export --sanitize`.
- Full conflict-detection suite (config schema checklist).
- Docs: runbook, troubleshooting, template customization guide.

---

## Intentionally deferred (complexity budget)

See design.md §18. Kept out of v1 on purpose:

- Parallelism + batch envelope (default `parallelism: 0`).
- Headless decisions inbox (interactive is enough for v1).
- Per-profile budget hints (single ≤50% rule until needed).
- `ask_on` list (superseded by `halt_mode`).
- `prompt_sections` config key (built-in MUST list; config override optional).

---

## Suggested order of value

The biggest win is **Phase 1** (no more manual forwarding for executor
rounds, plus the permission manifest so opencode stops asking you to authorize
every git op). The biggest correctness win is **Phase 3** (review discipline +
profiles + underspec handling). Parallelism and plan actionability (Phase 4)
are nice-to-haves that can stay off until the sequential loop is trusted.
