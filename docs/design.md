# Design: an automated supervisor → executor → reviewer loop over `opencode` sessions

**Status:** historical design / brainstorm — updated 2026-08-09 after operator
review. It is not an operations guide and must not be used to infer supported
commands, state authority, or execution permissions. Schema-v1 contracts in `docs/config-schema.md`,
`docs/normalized-plan-schema.md`, `docs/protocol.md`, and
`docs/workflow-state-schema.md` supersede its legacy envelope, singular-root,
and reference-parser proposals. Real dispatch remains disabled.

Legacy references to `state.json`, prose envelopes, fixed model assignments,
or root-level template placeholders are historical context only. The current
SQLite authority, strict JSON protocol, repository registry, and operations
guide take precedence.
**Reference material examined:**
sanitized role, plan, and evidence files from a multi-step reference project,
the repository instructions,
the live `opencode` CLI (v1.18.11), the current
`~/.config/opencode/opencode.json`, and the model-syntax reference in
`~/.config/opencode/oh-my-openagent.json`.

---

## 1. What I understood you need

You currently run a multi-LLM pipeline **manually**: you start a supervisor
(`opencode` session, GPT-5.6 Sol) that holds the global view of a project,
then you copy its prompts into slave `opencode` sessions (executors like GPT-5.6
Terra, reviewers like Kimi K3 / Claude Sonnet / DeepSeek), paste their
responses back, and repeat for every step of the plan.

The pain point: **you are the dispatcher**. You forward prompts, track which
session is at which context %, decide when to escalate, and keep the evidence
trail coherent.

Your idea — which I agree with — is that this forwarding is **mechanical and
therefore automatable**, as long as:

- the *plan is already well specified* (from accurate specifications), so the
  supervisor does not need to invent structure;
- the *roles* (who executes, who reviews, when) are declared in one place;
- the *supervisor stays authoritative*: it decides what happens next, and the
  automation only carries messages, enforces policy constraints, and records
  everything.

What you want is a **thin, general orchestrator** ("dispatcher") that:

1. Keeps the supervisor conversation alive across the whole project
   (resume by session id, e.g. `opencode -s ses_01fff058dffeYF7TSqQtaO8FbR`).
2. Routes each supervisor decision to the correct executor or reviewer.
3. Captures each slave response (short chat answer + detailed `.md` evidence
   file) and forwards it to the supervisor with a fixed template.
4. Repeats until the plan is done, with a review rate controlled by a
   cost/speed profile, optional parallelism, and safe restart/resume.
5. Guards execution with a pre-flight safety net: credential checks (git auth,
   MCP env vars, a cheap per-model smoke test) and an authorization manifest
   compiled into opencode's native permission rules — so agents can't touch
   what the plan doesn't grant, and opencode stops interrupting for
   permissions.
6. Handles underspecification during execution: `ask` (pauses for your answer,
   persisted) or `auto` ("take the best you think makes sense for this
   project").
7. Optionally detects non-actionable plan steps up front and has a planner
   produce an actionable breakdown (`.md`).

---

## 2. Key insight: this is a "conversation router", not an agent framework

A useful framing: each role (supervisor, each executor, each reviewer) is a
**persistent conversation** living inside `opencode`. The supervisor is the
only role allowed to talk to everyone; executors and reviewers only ever talk
*to the supervisor* (through you today, through the dispatcher tomorrow).

This is a classic **hub-and-spoke message-passing loop**, not a graph of
autonomous agents that need to discover each other. That has a big consequence:

> **You do not need LangGraph or any agent-graph library.** A small,
> deterministic loop + a persisted state file + subprocess calls to `opencode`
> is simpler, easier to debug, cheaper to run, and exactly matches the manual
> process you already trust. Frameworks would only add abstraction on top of
> what is really a "read a decision, run a CLI, write a transcript" loop.

The two non-trivial pieces are:

1. the **dispatch protocol** — the supervisor's answer must be
   machine-parseable enough for the dispatcher to know *who* to send *what*
   to (Section 5);
2. the **pre-flight safety net** — credentials and authorizations must be
   verified *before* the first dispatch so the loop never blocks on a missing
   permission or crashes on a missing credential (Section 10).

---

## 3. Architecture

```
                    ┌────────────────────────────┐
                    │         Dispatcher         │
                    │  routing · state · budget  │
                    │  opencode subprocess calls │
                    │  pre-flight · permissions  │
                    └─────────────┬──────────────┘
       dispatch (envelope)        │        response (chat + evidence .md)
  ┌───────────────────────────────┴───────────────────────────────┐
  │                                                               │
  ▼                                                               │
┌───────────────┐   ┌──────────────────┐   ┌──────────────────┐   │
│  Supervisor    │   │  Executors        │   │  Reviewers       │   │
│  (persistent)  │   │  (resumable)      │   │  (fresh)         │   │
│  opencode      │   │  opencode run     │   │  opencode run    │   │
│  -s <id>       │   │  -s <id> | new    │   │  new session     │   │
└───────────────┘   └──────────────────┘   └──────────────────┘   │
                                                                   ▼
                                      state/ sessions.json · state.json
                                      transcripts/ · audit.jsonl · evidence/
                                      decisions/ · permissions/
```

See [`docs/diagrams/architecture.mmd`](docs/diagrams/architecture.mmd).

Roles from the sanitized reference workflow:

| Role | Example model | Session lifecycle |
|---|---|---|
| Supervisor | GPT-5.6 Sol (xhigh) | One persistent session, resumed by id; ideally never closed |
| Executor primary | GPT-5.6 Terra (xhigh) | Per-task; **new** if past context may pollute, **resumed** for rework (context + cost) |
| Executor contingency | Claude Sonnet 5 (high/xhigh) | Fresh session when Terra stalls (fallback after 2 stalls) |
| Reviewer primary | Kimi K3 (max) | **Fresh** session per review (independence) |
| Reviewer escalation | DeepSeek V4 Flash (xhigh) | Fresh; cheap tie-break when Terra/Kimi disagree |
| Reviewer high-risk | Claude Sonnet 5 | Fresh; second independent opinion on flagged steps |
| Planner (optional) | default: supervisor, or a cheap model | Pre-flight only; produces an actionable plan breakdown |

### 3.1 The agents' MCP toolset (opaque capabilities)

Your `opencode` ships with: **context7** (docs), **github** (repo API, needs
`GITHUB_PERSONAL_ACCESS_TOKEN`), **playwright** (browser), **repomix** (repo
packing), **semble** (semantic memory), plus a simple-memory plugin and
auto-compaction.

Design stance: the dispatcher is **inner-working agnostic** — it never assumes
which tools an agent has; it forwards prompts and the agent uses whatever MCP
it has. The toolset matters in exactly two places:

1. **Per-role tool guardrails.** The config declares per-role allowed/denied
   tools (e.g. reviewer: deny `webfetch`/`bash`/`github`; executor: `github`
   only where authorized). These are (a) injected as a mandatory footer in
   every dispatch and (b) compiled into `opencode` permission rules (Section
   10). This makes a rule like the reviewer's "DO NOT CONTACT THE CLUSTER OR
   NETWORK" mechanical, not just prose.
2. **Pre-flight credential checks.** For MCP tools the plan will need, the
   pre-flight verifies the env var / server is present (e.g.
   `GITHUB_PERSONAL_ACCESS_TOKEN` for the github MCP, `CONTEXT7_API_KEY` for
   context7). A needed-but-misconfigured tool stops the run with a clear
   message instead of a mid-run crash.

Memory/context (semble, repomix, simple-memory) is the agents' own concern; the
dispatcher does not duplicate it. The supervisor bootstrap may *instruct* the
supervisor to use repomix to build a repo context bundle before planning — an
instruction, not a dependency.

---

## 4. The orchestration loop (your [1]–[8], formalized)

The loop is a supervisor-driven message loop. Each iteration ends with a
supervisor answer that is one of: a **dispatch** (to executor or reviewer), a
**done**, a **halt/ask** (needs the operator), or a **batch** (several parallel
dispatches).

```
while not plan_complete:
    # A. Get supervisor decision
    if first run:      send bootstrap to supervisor session (project, specs, plan, roles, profile)
    elif resumed:      send resume context + last state digest to supervisor session
    else:              forward the last slave response (template [3]) to supervisor session
    decision = run_supervisor_turn()                  # opencode run -s <sup_id>

    # B. Interpret decision
    route = parse_envelope(decision)                  # protocol.md
    match route.kind:
        dispatch -> route to executor/reviewer (below)
        batch    -> fan out to N slaves in parallel (parallelism rules)
        done     -> finalize; break
        halt/ask -> persist; pause for operator; continue on resume

    # C. Run slave
    slave_session = acquire_session(route)            # new / resume / fork
    snapshot_before = snapshot(evidence_dir, repo)
    response = opencode_run(slave_session, route.prompt, timeout, budget)
    new_files  = diff(snapshot_before, snapshot_after)   # evidence produced
    usage      = session_usage(slave_session)
    persist(transcript, evidence refs, session id, usage, audit)
    release_session(slave_session)                    # repo locks

    # D. Forward to supervisor (your template [3])
    msg = render_forwarding_template(route, response, new_files, usage)
    # -> next iteration: supervisor decides again
```

Equivalent view of your numbered steps:

| Your step | In the dispatcher |
|---|---|
| [1] first init | bootstrap supervisor → builds first knowledge map; supervisor's own decisions saved to `evidence/` + transcript |
| [1bis] restart/resume | load state; resume supervisor with `-s <id>`; send resume context digest |
| [2] send executor task | parse envelope → `opencode run` on executor session |
| [3] forward response | render template `response is, {chat} --- Provide next prompt. Informational only: {model} at {X}% context ({Y}k used)` → supervisor inbox |
| [4] catch supervisor response | parse envelope → decide slave + mode |
| [5] wait + catch | capture chat + `.md` evidence files (filesystem diff) |
| [6] send to supervisor | same as [3] |
| [7] next step | dispatch → executor; review → reviewer (profile-filtered) |
| [8] repeat | loop until `done` |

**Fresh vs resumed executor sessions** (confirmed): the envelope's `mode`
field carries `new` (fresh, when past context may pollute the action) or
`resume` (continue the stored session for rework — benefits from accurate
context and minimizes cost). The dispatcher stores every session id, so both
are trivial.

**Session titles:** every opencode session is started with
`--title "<role> · <step>"` (e.g. `reviewer · step-a review`), so
`opencode session list` stays self-describing and session-registry
reconciliation is trivial.

The full sequence is shown in
[`docs/diagrams/loop.mmd`](docs/diagrams/loop.mmd).

---

## 5. The dispatch protocol: how the supervisor tells the dispatcher what to do

This is the most important design decision. The supervisor must emit a
**machine-readable envelope** before the prompt body, so the dispatcher never
has to guess the destination.

### 5.1 Strict envelope (recommended, primary path)

Every supervisor turn that ends in a dispatch starts with:

```
<<dispatch>>
role: executor            # executor | reviewer | batch | done | halt | ask
target: terra             # role key from config (terra, sonnet, kimi, deepseek, ...)
mode: resume              # new | resume | fork
step: step-a              # plan step id (informational + used for locks/budget)
session: ses_...          # only when mode=resume/fork and id is known
parallel: false           # optional; request parallel execution (subject to policy)
<<end>>
---
<PROMPT BODY — forwarded verbatim to the target session>
```

Terminal envelopes:

```
<<dispatch>> role: done <<end>>        # plan complete; write final report
<<dispatch>> role: halt <<end>>        # stop; operator must intervene
<<dispatch>> role: ask <<end>>
---
<question to the operator, persisted as a decision request>
```

The prompt body is **forwarded verbatim** — it already contains all the
structure you rely on (Authority, Read first, Authorized work, Prohibited,
Write, Return), exactly like the Annex A examples.

### 5.2 Why an envelope instead of parsing the natural-language header

Your current prompts begin with lines like *"Executor: Claude Sonnet 5 xhigh,
performing the bounded configuration gate."* or *"Implement step-a only: ..."*.
We could parse those with heuristics (leading
`Executor:`/`Reviewer:`, model-name keywords, `Implement ... only`). That works
today, but it is fragile: a model rewording the header silently re-routes a
task. The envelope:

- makes routing deterministic (no guesswork);
- carries the **mode** (new/resume/fork), which you currently decide by hand;
- lets the dispatcher fail loudly (HALT + ask) when the envelope is missing
  or ambiguous, instead of mis-routing.

**Fallback:** if the envelope is absent, the dispatcher attempts the
natural-language heuristic (see [`docs/protocol.md`](docs/protocol.md)); if
that is also ambiguous it **halts and asks** — never guesses silently. The
halt behavior itself is configurable: `dispatch.halt_mode = ask_on_ambiguity`
(default) or `full_auto` (Section 14).

### 5.3 Teaching the supervisor the envelope

The envelope is not magic: the dispatcher injects it into the supervisor's
**bootstrap instructions** (a template, editable per project). The supervisor
is told: "Every time you decide the next action, reply first with a
`<<dispatch>>` block, then the full prompt to send." Because the supervisor's
session is persistent, this is taught once and reused for the whole project.

### 5.4 Normative prompt anatomy: MUST / SHALL / MAY

There is no fit-for-all prompt pattern, so we make the *structure* normative
rather than the wording. Each dispatch prompt is validated against a
**section contract** declared in config (`prompt_sections`), using RFC-2119
style qualifiers:

| Qualifier | Meaning | Enforcement |
|---|---|---|
| **MUST** | Section that must be present in every dispatch | Dispatcher checks presence before forwarding; missing → ask supervisor to complete (or HALT in strict mode) |
| **SHALL** | Conditional requirement inside a section ("Shall not mutate X") | Injected verbatim into the prompt; also matched against the permission manifest where mechanical |
| **MAY** | Optional section that may be adjusted | Never required; supervisor may omit |

Default MUST sections (matching your Annex A style): `role`, `step`, `repo`,
`authority` (read-first), `authorized`, `prohibited`, `write`, `return`.
SHALL covers things like `verification`, `scope`, `restrictions`. MAY covers
`background`, `hints`, `notes`. The full anatomy lives in
[`docs/protocol.md`](docs/protocol.md).

---

## 6. State, transcripts and resume (your [1bis], [cc])

### 6.1 What is persisted

| Store | Contents |
|---|---|
| `state/<project>/state.json` | run metadata: current step, per-step status, round counters, plan position, last decision hash |
| `state/<project>/sessions.json` | role key → session id, model, variant, status, last activity, context %, token/cost |
| `state/<project>/transcripts/*.md` | every message in the loop (S→E, E→S, S→R, R→S), with timestamps and hashes |
| `state/<project>/audit.jsonl` | append-only machine-readable log of every action (dispatch, response, evidence diff, cost, permission decisions) |
| `state/<project>/decisions/` | persisted operator Q&A (pre-flight gaps, `role: ask`, underspec questions) |
| `state/<project>/permissions/` | generated `opencode` permission config per role (from the manifest) |
| `evidence/` (in the repo, as today) | the `.md` files the agents themselves write (supervisor-go, handoff, review) |

Your question in [cc] — *"md file suffices for execution/audit history?"* — my
answer: **both**. The human-readable evidence trail stays `.md` (written by the
agents, exactly as now). But the dispatcher's own audit/history should be
`JSONL` (plus the transcripts in md): the machine needs structured records to
resume safely, and you get readable `.md` transcripts as a bonus.

### 6.2 Resume semantics

- The **supervisor session id** is the resume hash (`opencode -s <id>`), as you
  already use. It is stored in `sessions.json` and never guessed.
- On restart the dispatcher:
  1. loads `state.json` + `sessions.json`;
  2. verifies the supervisor session still exists (`opencode session list`);
  3. sends the supervisor a short **resume context** message: project name,
     current step, the last decision and last slave response (hashes + file
     refs), and the current per-step status table — *not* the full transcript,
     which the persistent session already holds;
  4. continues the loop.
- **Crash safety:** a dispatch is written to the audit log *before* the
  subprocess runs. On restart, any "in-flight" dispatch (logged but with no
  recorded response) is reported and either resumed on the same session with a
  "please finish" message, or handed to the operator.
- **Session recovery:** if a session is lost/deleted, `opencode export <id>`
  gives a JSON transcript that can be `opencode import`-ed into a rebuilt
  session — a documented recovery path, not a silent reset.

---

## 7. Cost / speed profiles (your [ee])

The profile controls the **review schedule**: which steps get a review pass,
and whether multiple reviewers may be used. It is implemented as
(1) a schedule generator and (2) a constraint filter on supervisor proposals.

| Profile | Review schedule | Intent |
|---|---|---|
| `economy` | Review only on failure/escalation or when the supervisor explicitly requests it; single reviewer by default | Lowest cost, fastest, lower confidence |
| `balanced` *(default)* | Review at critical steps (plan-marked high-risk / mandatory multi-review) + failure-triggered escalations | Middle ground: review where it matters, not everywhere |
| `thorough` | Review at every plan-defined step; multi-review steps always get both reviewers | Highest cost, slowest, highest confidence |

`multiple_reviewers: on|off` is an independent switch: even in `thorough`, you
can force single-reviewer only, or in `economy` allow two reviewers for a
step you personally flag.

The profile is a **constraint, not a replacement** for the supervisor: the
supervisor still decides the next action; the dispatcher flags (and in strict
mode refuses) proposals that violate the profile, e.g. "skipping a defined
review in `thorough` mode". Violations → HALT + ask, so the operator keeps
final authority.

**Repeated constraints:** because LLM sessions drift, the active constraints
(profile, permission manifest, mandatory MUST/MAY/SHALL sections, the envelope
format) are **re-injected in every prompt** — a mandatory header and footer on
every dispatch, not just the bootstrap. This is cheap insurance against
persistent drift.

Full definitions: [`config/profiles.yaml`](../config/profiles.yaml).

---

## 8. Parallelism (your "whether parallelism is allowed for each executor/reviewer")

- Per-role flag `parallelism: <n>` (0 = sequential only) on each executor and
  reviewer, plus a global `max_parallel` cap.
- When parallelism allows, the supervisor may emit a **batch** envelope
  (several `<<dispatch>>` blocks in one answer). The dispatcher:
  1. checks each task's **repo** (from the plan's "Scope (repo)" column);
  2. **refuses to parallelize tasks touching the same repo** (repo lock) —
     parallel `opencode` processes on one repo would race;
  3. runs the accepted tasks as concurrent `opencode run` processes, each on
     its own session;
  4. collects all responses and forwards them to the supervisor in **one**
     batched message (per-task chat summary + evidence paths + usage).
- This matches your instinct: parallelism is per-role and per-run, and it must
  never let two agents write the same repository at once.

---

## 9. Conflict detection and startup validation (your [aa]–[ee] vs allowed lists)

At startup the dispatcher validates the configuration and refuses to run on a
mismatch, with a readable message and a pause for the operator.

Checks:

1. **Allowed pool vs used roles.** The config declares the *allowed* role pool
   (`roles.allowed_supervisors/executors/reviewers`). The plan + roles file
   (your [dd]) declare the roles actually used. The dispatcher computes:
   - used roles that are **not** in the allowed pool → **conflict**;
   - allowed roles never used → warning only (harmless superset).
   Any conflict → **stop + ask** (edit config or roles file).
2. **Exactly one supervisor.** No more, no less.
3. **Unique role keys** across executors and reviewers; `target:` values in
   envelopes must resolve to a configured role.
4. **Model strings resolvable** by `opencode` (best-effort: `opencode models`)
   plus the model smoke test in Section 10.
5. **Paths exist** (specifications, plans, evidence, archive) and are
   directories/files as declared.
6. **Plan parse.** The plan/roles file is parsed best-effort for the step
   table (step id, repo, multi-review flag, exit evidence). If parsing is
   ambiguous the config may carry an explicit `steps:` override map (curated
   by you) so the dispatcher does not depend on markdown table formatting.
   **Steps-map policy:** because plans may be written by an LLM (and tables
   drift), an explicit `steps:` map in YAML is recommended; if absent, the
   dispatcher falls back to the **tier-2 default parser** — the exact table
   format of `tier-2-execution-roles.md`, which already encodes your
   failure-transition matrices (escalation ladder, rework rounds, tie-break,
   reassignment). The plan-actionability pre-flight (Section 12) can normalize
   an LLM-written plan into that format before parsing.

The authoritativeness model is deliberately: **supervisor proposes, profile +
conflict checks constrain, operator disposes.**

---

## 10. Pre-flight: credentials, authorization and the safety net (new)

You observed that during execution the supervisor kept asking you to authorize
repo access, branch creation, etc. That breaks automation. The fix is a
**pre-flight gate** with two layers, run before the first dispatch.

### 10.1 Layer 1 — credential/capability checks (avoid crashes)

Verify, and stop with a clear message on failure:

- all `paths.*` exist and are writable as declared;
- git: the repo remotes are reachable and the **required auth level** exists
  for the operations the plan will need (`git ls-remote` for read; write/push
  only if the plan authorizes it — see Layer 2);
- MCP prerequisites: env vars / local servers for the tools the plan needs
  (e.g. `GITHUB_PERSONAL_ACCESS_TOKEN`, `CONTEXT7_API_KEY`, `playwright`,
  `semble`, `repomix` availability);
- disk space for the archive/experiments directory;
- **model smoke test**: for every model in the allowed lists / requested for
  execution, run a trivial `opencode run "Reply with exactly: OK" -m <model>
  --variant <v>` and require exit 0 + the marker. This catches typos,
  misconfigured providers, and dead endpoints before the run, at negligible
  cost. (Your point stands: the *ultimate* responsibility is yours to ensure
  opencode references the models correctly — this guard just fails fast.)
  The syntax follows opencode's `provider/model` + `variant` convention,
  verified 2026-08-09 via `opencode models` (e.g. `openai/gpt-5.6-terra` +
  `xhigh`, `deepseek/deepseek-v4-flash` + `xhigh`). OpenAI models (Sol/Terra)
  use the `openai` provider only — that is the company-paid account, never
  `github-copilot`; `fallback_models` may mirror the escalation chain.

### 10.2 Layer 2 — authorization/safety manifest (the guard)

The config declares a **permission manifest**: which sensitive actions the
automation may perform, per role. It is deliberately *outside* the plan — a
safety net that says "what the plan is allowed to touch".

Manifest dimensions:

| Dimension | Examples |
|---|---|
| repo ops | `create_branch`, `commit`, `push`, `create_pr`, `force_push`, `delete` |
| filesystem | writable dirs (repo subtree, evidence, experiments), read-only dirs, `external_directory` allowlist |
| network/cluster | `webfetch`, `websearch`, cluster commands, `kubectl`, `helm` |
| tools | per-MCP-tool allow/deny per role |
| interaction | `question` (may the agent interrupt you?) |

Enforcement has **two mechanical layers** (this is the important part):

1. **opencode level (mechanical).** The dispatcher renders the manifest into
   opencode's native `permission` config — per role, via a generated
   per-project `opencode.json` and/or the `OPENCODE_CONFIG_CONTENT` inline
   config — and runs `opencode run --auto` where allowed. opencode supports
   granular rules per tool (`read`, `edit`, `bash`, `webfetch`, `websearch`,
   `task`, `question`, `external_directory`, ...) with patterns
   (`"git push *": "ask"`), and **`deny` rules are always enforced even under
   `--auto`**. This is what eliminates the interactive authorization prompts
   you saw: allowed ops run silently, denied ops are blocked by rule, and
   `ask` patterns are surfaced to you (or denied in full-auto mode).
2. **dispatch level (semantic).** Each dispatch's `Authorized`/`Prohibited`
   sections are validated by the dispatcher as a subset of the manifest. The
   supervisor's word alone is not enough; the manifest is.

**Semantic ops → mechanical patterns.** Repo operations are declared
semantically (`create_branch`, `commit`, `push`, `create_pr`, `force_push`)
and the dispatcher compiles them to the exact bash patterns opencode enforces
(`git push *`, ...), so you maintain one list instead of two.

**Per-repo manifests.** A project can span several registered repositories. The manifest
supports per-repo allow/deny overrides, matching the per-repo parallelism
lock, so each repo's blast radius stays isolated.

If the pre-flight finds a gap (the plan will need an action the manifest does
not grant), it **asks you to complete the definition**, and the answers are
**persisted** in `state/<project>/decisions/` so the next run does not re-ask.

> Concrete win: the `question` permission in opencode maps 1:1 onto your
> underspec modes (Section 11). In `ask` mode the agent may pause to ask you;
> in `auto` mode `question: deny` forces it to resolve internally.

### 10.3 Pre-flight configuration

```yaml
preflight:
  enabled: true
  models_smoke_test: true
  credentials: [GITHUB_PERSONAL_ACCESS_TOKEN, CONTEXT7_API_KEY]   # only those the plan needs
  git_auth: true
  disk_space_min_mb: 500
  plan_actionability: off         # off | ask | auto, off is the default (Section 12)
  planner_role: supervisor        # supervisor | <role key of a dedicated planner>
```

---

## 11. Underspecification during execution (new)

The reviewer is not an active writer, so this concerns the **supervisor and
executors**. Two configurable modes (`dispatch.underspec_mode`):

| Mode | Behavior | opencode lever |
|---|---|---|
| `ask` *(default)* | If an LLM hits an underspec it believes *requires* clarification, it may stop and ask. The question is persisted and surfaced to you; your answer is saved and injected into the next dispatch (or answered to the supervisor) | `question: allow` |
| `auto` | "Take the best you think makes sense for this project": the LLM resolves internally, documents the assumption in its handoff, and continues — full automation | `question: deny` + explicit instruction |

Detection: in `ask` mode, a question is detected via (a) opencode's `question`
tool pausing the run, or (b) a `BLOCKER: <question>` marker in the executor's
handoff when it stops early. In `auto` mode the executor never blocks; the
supervisor's review of the handoff catches bad assumptions at the next gate.

This rides the existing loop: an executor reports an underspec → dispatcher
forwards it to the supervisor → the supervisor either resolves it (new
dispatch) or emits `role: ask` (only permitted in `ask` mode).

---

## 12. Plan actionability pre-flight (new)

Even a well-constructed plan may contain steps that are not actionable. Before
the loop starts, an optional check runs a **planner** (default: the supervisor
itself, or a dedicated cheap planner role declared in config) that:

1. reads the plan + specs;
2. detects steps lacking actionable detail (no clear repo, files, exit
   evidence, or authorization list);
3. produces an actionable breakdown (step, repo, file list, exit evidence,
   authorized actions) saved to `markdown/plans/demo/<plan>-actionable.md`
   (or a configurable path), as `.md` as always.

`preflight.plan_actionability`:
- `off` *(default)* — trust the plan;
- `ask` — if gaps are found, ask you before proceeding with the refined plan;
- `auto` — proceed with the refined plan (you can review it via git later).

This also normalizes LLM-written plans into the reference table format the steps
parser expects (Section 9), so it complements the steps-map policy.

---

## 13. Budget and context guards

- **Per-step budget:** the reference roles file enforces "≤ 50% of executor
  context" per step. The dispatcher carries that as a hint in the dispatch
  prompt and reports usage after each run; a step approaching its stated bound
  triggers a handoff rather than a runaway run (the executor itself is
  instructed to stop-and-handoff; the dispatcher enforces a wall-clock
  timeout as a backstop).
- **Context window reporting:** after every slave run the dispatcher records
  context % and tokens used, and fills them into the forwarding template
  ("... at 34% context window usage (187k used)") so the supervisor can decide
  to fork/consolidate. Source: `opencode run --format json` usage events and
  `opencode stats --models --project`.
- **Compaction-aware sizing:** your `opencode.json` already runs auto-compaction
  (`auto`, `prune`), and the dispatcher never fights it. Instead it (a) sizes
  each step well under the compaction trigger (the ≤50% rule), (b) tracks
  per-session growth after every run, and (c) before *resuming* an executor
  session, checks whether its current context % plus the step's estimated
  budget would cross the trigger — if so it asks the supervisor to fork or
  consolidate instead of resuming mid-task. Goal: never get trapped in a
  compaction in the middle of an executor session.
- **Cost cap:** optional `budget.max_cost_usd` per step/run; on exceed →
  HALT + ask.

---

## 14. Operator interaction and error handling

The system is semi-autonomous. It pauses for the operator on:

- config/role conflicts (Section 9) and pre-flight gaps (Section 10);
- missing/ambiguous dispatch envelope (Section 5) — **only if**
  `dispatch.halt_mode = ask_on_ambiguity`; with `full_auto` the dispatcher
  retries once with a "please emit a dispatch envelope" nudge, then stops;
- supervisor `halt`/`ask` envelopes — `ask` only in `underspec_mode: ask`;
- step stuck (rework rounds exhausted → escalation ladder → supervisor
  adjudicates → may reassign executor);
- budget/cost exceeded;
- optional `operator_gate: true` on high-risk external actions (e.g. steps
  that mutate a live cluster), so you can force a human confirmation before
  the dispatch is sent.

### 14.1 Clarifying the operator-gate question (your Q4)

You asked what the difference was between two options I'd posed. Rephrased:

- **Option A — supervisor-only authorization:** the dispatch's `Authorized`
  list is the only gate; if the supervisor authorizes a cluster mutation, it
  runs (subject to the permission manifest's deny rules).
- **Option B — operator gate:** an *additional* human checkpoint. For steps
  tagged `risky` in config, the dispatcher pauses and asks you before
  dispatching, even though the supervisor already authorized it.

The permission manifest gives a **finer-grained middle ground**: instead of a
blunt per-step gate, you set specific *actions* to `ask` (e.g. `"bash": {
"kubectl *": "ask", ... }`), so routine work runs under `--auto` while exactly
the sensitive verbs pause for you. Recommendation: use action-level `ask`
rules (no per-step gate), and keep `operator_gate` as an optional blunt
override for steps you personally don't trust yet.

### 14.2 Interaction modes

- **interactive** (default, attached terminal): prompts you directly
  (confirmed for v1; your dev environment is the laptop);
- **headless/background** (later): a `decisions/` inbox — on a pause the
  dispatcher writes a question `.md` and waits; you drop an answer `.md` and
  it resumes. This supports long unattended runs that still let you gate the
  risky bits.

---

## 15. Technology choices and extensibility

| Choice | Decision | Why |
|---|---|---|
| Language | Python 3.11+ | Matches your ecosystem; stdlib `subprocess`, `asyncio`, `json`, `sqlite3`; PyYAML for config |
| Orchestration | Plain loop + state machine | No LangGraph/agent framework: the "agents" are external `opencode` processes; a framework would add cost and indirection |
| Parallelism | `asyncio` + per-repo locks | Simple concurrency for independent `opencode run` subprocesses |
| State | JSON files + JSONL audit (optionally SQLite later) | Human-readable, diffable, trivially resumable |
| Config | YAML, validated at startup | Matches your YAML-first convention (see repo `AGENTS.md`) |
| LLM harness | `opencode run -s <id> -m <model> --variant <v> --dir <dir>` | Already your harness; non-interactive mode is the automation primitive |
| Testing | `--mock` scripted responder | Exercise routing, state, resume and permission compilation without spending tokens |

### Proposed layout

```
workflow-automation-llm/
  README.md
  docs/                     # this design, protocol, config schema, roadmap, diagrams
  config/
    profiles.yaml           # economy / balanced / thorough
    projects/<name>.yaml    # per-project config ([aa]–[ee])
  src/dispatcher/
    config.py               # load + validate config, conflict detection
    state.py                # state.json / sessions.json / decisions / resume
    sessions.py             # opencode run/resume/fork/export/stats wrapper (harness abstraction)
    dispatch.py             # envelope parse + natural-language fallback + routing
    forward.py              # forwarding template rendering ([3])
    permissions.py          # manifest -> opencode permission config compiler
    preflight.py            # credential/model/git/disk/actionability checks
    loop.py                 # orchestration loop + state machine + repo locks
    audit.py                # JSONL audit log
    cli.py                  # `dispatcher run|resume|status|export|preflight`
  templates/                # supervisor bootstrap, forwarding template, resume context
  state/                    # runtime (gitignored): per-project state, transcripts, audit
```

### Extensibility (your Q8)

The dispatcher must stay open for cases we haven't hit yet. Explicit
extension points:

- **New role kinds** (e.g. `planner`, future roles): a role is just a config
  entry + a session; the envelope's `role:` vocabulary is extensible.
- **New envelope kinds**: `batch`, `done`, `halt`, `ask` are the seed set;
  parsing is table-driven.
- **Pluggable guards**: each pre-flight check is a small, independent module
  (credentials, models, git, disk, plan-actionability) that can be added or
  skipped per project.
- **Harness abstraction**: all `opencode` interaction is isolated in one
  module (`sessions.py`); a different harness (or a direct API) can replace it
  without touching the loop.
- **Transport-agnostic**: the loop never assumes MCP details; per-role tool
  guardrails are data (config), not code.

### Naming (your Q7)

**Name chosen: `dispatcher`** (2026-08-09). It is centralized in the module
name + README; if it ever feels restrictive, renaming is a two-line change.

---

## 16. Open questions (updated after your review)

Resolved by your answers:

1. ✅ Envelope is OK (strict + fallback). `halt_mode` configurable
   (`full_auto` vs `ask_on_ambiguity`).
2. ✅ Model strings: resolved 2026-08-09 from `opencode models` on this
   machine — see the config example (`openai/gpt-5.6-sol`,
   `openai/gpt-5.6-terra`, `github-copilot/claude-sonnet-5`,
   `github-copilot/kimi-k3`, `deepseek/deepseek-v4-flash`). OpenAI models use
   the `openai` provider only (company-paid; never `github-copilot`). The
   model smoke test is part of pre-flight.
3. ✅ Steps map: explicit `steps:` in YAML recommended; fallback = tier-2
   roles-file parser (your transition matrices stay authoritative).
4. ✅ MUST / SHALL / MAY section contract adopted (Section 5.4); operator-gate
   clarified as action-level `ask` rules (Section 14.1).
5. ✅ Interactive operator prompts for v1.
6. ✅ Python (shell for glue).
7. ✅ Name: `dispatcher` (chosen 2026-08-09).
8. ✅ Harness boundary: dispatcher never runs infra verbs itself; extensible
   (Section 15).
9. ✅ Multi-repo permission manifests, session titles (`--title`), mock-mode
   testing, and compaction-aware task sizing adopted (2026-08-09).

All prior blockers are resolved (2026-08-09): model strings verified via
`opencode models`; `underspec_mode` default = `ask`. Next gate: an external
design review (Claude Sonnet 5) before Phase 1 implementation.

---

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Supervisor deviates from the envelope | Fallback parser + HALT-on-ambiguity; bootstrap instructions teach the format; sample envelopes in the prompt |
| Mis-routing a prompt (worst case: a reviewer gets an executor prompt) | Envelope carries role+target; the prompt body also states the role; dispatcher double-checks target ∈ expected role pool |
| Parallel runs collide on one repo | Per-repo lock; batch rejected if any two tasks share a repo |
| Supervisor context grows unbounded | Compact state digest instead of full transcripts; per-session usage reporting; supervisor instructed to consolidate/fork at high % |
| Permission prompts block automation | Manifest compiled to opencode `permission` rules; `--auto` for allowed ops; `deny` always enforced; `question` permission per underspec mode |
| Missing credentials/tools crash mid-run | Pre-flight layer 1 (credentials, git auth, MCP env, model smoke test) |
| Agent performs an action the manifest does not grant | Pre-flight layer 2 + dispatch-level Authorized/Prohibited subset validation |
| `opencode` CLI changes between versions | `sessions.py` isolates all CLI interaction behind one module; version pinned in docs |
| Markdown plan tables change format | Best-effort parse + explicit `steps:` override + plan-actionability normalization |
| Plan has non-actionable steps | Plan-actionability pre-flight (off/ask/auto) |
| Secrets leak into transcripts/audit | Audit sanitization (mirror `opencode export --sanitize`); prompts must not contain credentials (manifest enforces env-var indirection) |
| Agent writes evidence outside the expected dir | Evidence diff scans the configured evidence dir; unexpected writes are surfaced to the supervisor/operator |

---

## 18. Complexity budget: what stays, what is deferred, what is cut

Your concern is valid and it is the right final review question. Every feature
must pay for itself against the two real pain points — (1) manual message
forwarding, (2) authorization interruptions + mid-run crashes — or against an
explicit spec requirement. Anything else is deferred or cut.

| Tier | Feature | Why it stays / goes |
|---|---|---|
| Core (v1) | Envelope, loop, forward template, resume, evidence diff | Directly removes manual forwarding |
| Core (v1) | Permission manifest → opencode rules + `--auto` | Directly removes authorization interruptions; `deny` is the safety property |
| Core (v1) | Pre-flight layer 1 (paths, git auth, model smoke test) | Prevents mid-run crashes; cheap |
| Core (v1) | Sessions registry + `--title` | Makes resume trivial and sessions self-describing |
| Core (v1) | Profile (`mode` string) + multi-review switch | Your [ee] requirement, expressed as one value |
| Keep, simple | Underspec mode (`ask`/`auto`) | One config value → `question` permission |
| Keep, simple | Multi-repo manifest + `repo_ops` semantic layer | Needed for multi-repo projects; compiler keeps one source of truth |
| Keep, optional | Plan-actionability pre-flight (default `off`) | Safety net for LLM-written plans; off unless enabled |
| Keep, optional | Cost cap | One number |
| Defer | Parallelism + batch | `parallelism: 0` is the default; batch lands only when a real need appears |
| Defer | Headless decisions inbox | Interactive is enough for v1 |
| Defer | Per-profile budget hints | Use the single ≤50% rule until a real need appears |
| Cut / minimal | `ask_on` list | Superseded by `halt_mode` |
| Cut / minimal | `prompt_sections` config key | Default MUST list is built into the dispatcher template; config override optional |

Guiding rule for future features:

> A feature must map to a spec requirement, a named pain point, or an explicit
> safety property — otherwise it does not enter the configuration.

The config stays YAML-first and strict (no silent fallbacks), but the *number
of keys* is treated as a cost: optional features are opt-in, and the default
config is small (see the minimal example in `docs/config-schema.md`).
