# Dispatcher Remediation Implementation Plan

**Plan date:** 2026-08-09
**Source review:**
[`markdown/reviews/dispatcher-project-review-2026-08-09.md`](../reviews/dispatcher-project-review-2026-08-09.md)
**Scope:** Repair the current proof of concept, define project-neutral contracts,
and deliver a safe sequential dispatcher before adding review automation and
parallel execution.
**Reference migration:** a private multi-step project with existing historical
work is deferred until the project-neutral framework and safety gates are reviewed.

## 1. Goal

Deliver a dispatcher that can execute a well-specified project plan through
persistent OpenCode sessions while mechanically enforcing:

- project and repository boundaries;
- plan dependencies and completion criteria;
- executor and reviewer result contracts;
- role and dispatch permissions;
- durable state and crash recovery;
- review and operator-gate obligations;
- immutable evidence and reviewed revisions; and
- explicit cost, context, retry, and concurrency limits.

The first releasable target is a safe sequential workflow. Parallel execution
is intentionally last because it depends on every prior correctness boundary.

## 2. Non-goals for the first releasable target

- Do not implement a general peer-to-peer agent framework.
- Do not let agents discover roles or repositories dynamically.
- Do not execute directly against live infrastructure during development.
- Do not infer authorization from free-form prompt text.
- Do not promise exactly-once external side effects where the external system
  has no idempotency primitive.
- Do not make a reference-project Markdown format the internal plan representation.
- Do not preserve the current configuration or state format solely for
  backward compatibility; no shipped external consumer has been identified.
- Do not enable batch or parallel execution before the sequential release gate.

## 3. Engineering rules

1. The supervisor proposes actions; the dispatcher validates and executes them.
2. The dispatcher, not the supervisor, owns safety and completion invariants.
3. YAML configuration contains all explicit runtime choices. Runtime code does
   not silently invent values for missing keys.
4. Source plans may remain Markdown, but execution uses a versioned normalized
   plan generated and approved before a run.
5. Strict machine contracts use versioned JSON schemas. Human prose is never
   parsed to grant permissions or mark work accepted.
6. State changes are transactional and correlated by immutable IDs.
7. Repository changes and review results are bound to immutable revisions or
   artifact hashes.
8. A malformed, unknown, or unsupported input fails closed before subprocess
   creation.
9. Every phase ends in an automated gate. Work on the next phase starts only
   after the gate passes.
10. The private reference project is a conformance fixture. Project-specific names, models,
    paths, step IDs, and transition rules remain in fixture configuration and
    import adapters.

## 4. Delivery sequence

| Phase | Outcome | Depends on |
|---|---|---|
| 0 | Unsafe real execution contained; test baseline established | None |
| 1 | Versioned generic config, plan, protocol, and state contracts | Phase 0 |
| 2 | Correct OpenCode adapter and permission boundary | Phase 1 |
| 3 | Transactional state, audit, locking, and recovery | Phases 1-2 |
| 4 | Correct sequential executor and reviewer workflow | Phase 3 |
| 5 | Multi-repository execution and immutable evidence | Phase 4 |
| 6 | Profiles, escalation, operator gates, and budgets | Phase 5 |
| 7 | Dependency scheduler and bounded parallel execution | Phase 6 |
| 8 | Migration, hardening, documentation, and release gate | Phases 0-7 |

## 5. Phase 0: Containment and test baseline

### DISP-000: Mark the implementation as a proof of concept

**Purpose:** Prevent operators from mistaking implemented-looking code and
configuration for active safety guarantees.

**Actions:**

- [x] Update `README.md` with an implemented/partial/design-only capability
  matrix.
- [x] Update `docs/roadmap.md` to reflect that source code already exists.
- [x] Mark permission enforcement, crash recovery, review policy, multi-repo,
  budget, operator gate, and parallelism as unavailable.
- [x] Add a prominent link from the README to the review report and this plan.
- [x] Make `dispatcher run` refuse real OpenCode execution during Phase 0 while
  preserving `--mock`, `preflight`, and `status` for development.
- [x] Add an automated test proving a non-mock run is rejected before calling
  OpenCode.

**Acceptance criteria:**

- [x] No documented feature is described as active unless an integration test
  proves it.
- [x] A user cannot accidentally start repository-mutating execution from the
  proof-of-concept branch.

### DISP-001: Establish the Python quality toolchain

**Purpose:** Turn the current mock script into a testable engineering baseline.

**Actions:**

- [x] Add test, lint, type-check, and build dependencies to `pyproject.toml`.
- [x] Configure `pytest`, Ruff, and strict-enough MyPy settings for the package.
- [x] Create `tests/unit`, `tests/contract`, `tests/integration`, and
  `tests/fault_injection` directories.
- [x] Add a CI workflow that runs lint, type checking, tests, and wheel build.
- [x] Add a clean-wheel smoke test that invokes `dispatcher --help` from a
  directory outside the source checkout.
- [x] Ensure generated caches, state, fixtures containing local IDs, and test
  worktrees are ignored appropriately.

**Acceptance criteria:**

- [x] CI runs on every pull request.
- [x] A clean checkout can install the package and run the test suite with one
  documented command.
- [x] Test collection fails if no tests are found.

### DISP-002: Repair preflight's immediate runtime defect

**Purpose:** Restore deterministic preflight behavior without enabling real
dispatch execution.

**Actions:**

- [x] Replace the invalid condition at `src/dispatcher/preflight.py:61` with a
  validated boolean lookup.
- [x] Pass the injected `run_session` implementation into `_check_models()`.
- [x] Catch and report unexpected check exceptions as preflight failures.
- [x] Ensure every check records a typed result and final audit outcome.
- [x] Add tests for enabled, disabled, skipped-smoke, mocked success, model
  failure, credential failure, Git failure, path failure, and disk failure.
- [x] Check disk space on the configured state, evidence, archive, and
  repository filesystems rather than only the process working directory.

**Acceptance criteria:**

- [x] Default preflight does not raise `NameError` or bypass the injected mock.
- [x] No preflight failure creates an OpenCode supervisor session.
- [x] Every failed check produces a stable actionable error.

### DISP-003: Capture external compatibility fixtures

**Purpose:** Stop coding against invented OpenCode output formats.

**Actions:**

- [x] Record the exact supported OpenCode version in `pyproject.toml` metadata
  or a checked runtime compatibility declaration.
- [x] Capture sanitized JSONL fixtures for new session text, resumed session,
  forked session, usage, tool events, structured error, malformed line,
  nonzero exit, and timeout.
- [x] Capture sanitized JSON output for exact session listing and session
  export/import behavior.
- [x] Store fixture provenance, OpenCode version, and capture command beside
  each fixture.
- [x] Add a policy requiring a fixture refresh and adapter contract test when
  the supported OpenCode version changes.

**Acceptance criteria:**

- [x] Fixtures contain no credentials, private prompts, or real session IDs.
- [x] The current parser's incompatibility is represented by a failing test
  before Phase 2 implementation begins.

### Phase 0 gate

- [x] Real dispatch execution is blocked.
- [ ] Hosted CI is green. The local CI-equivalent gate passes; the workspace is
  not a Git repository, so no GitHub Actions run can be observed yet.
- [x] Preflight tests pass.
- [x] OpenCode compatibility fixtures are checked in and sanitized.
- [x] Documentation accurately states current capability.

## 6. Phase 1: Project-neutral contracts

### DISP-100: Define configuration schema version 1

**Purpose:** Replace permissive dictionaries and silent defaults with one
validated source of runtime choices.

**Actions:**

- [x] Add required `schema_version` to project configuration.
- [x] Implement typed configuration models with unknown fields forbidden.
- [x] Require explicit values for all active runtime controls, including
  timeout, rounds, state path, protocol mode, execution mode, profile source,
  permission default, and evidence policy.
- [x] Treat an absent optional feature block as disabled; document that
  behavior in the schema.
- [x] Validate enum values, numeric ranges, path types, unique role keys, model
  strings, and exactly one supervisor.
- [x] Resolve relative paths against the project configuration file directory,
  not the caller's working directory.
- [x] Validate state-path writability without creating ambiguous state for a
  failed run.
- [x] Load the selected profile from an explicit `profiles_file` path.
- [x] Reject configuration for features that the current release does not
  support.
- [x] Remove code-level runtime defaults from `src/dispatcher/config.py`.
- [x] Publish a machine-readable schema and generate human documentation from
  the same field definitions where practical.

**Acceptance criteria:**

- [x] Unknown keys and wrong types fail at startup with exact field paths.
- [x] Every omitted required value fails before preflight.
- [x] Starting from two different working directories resolves identical paths.
- [x] The minimal valid configuration is covered by a committed fixture.
- [x] Every invalid enum and boundary value has a parameterized test.

### DISP-101: Define the repository registry

**Purpose:** Represent multi-repository projects without path guessing.

**Actions:**

- [x] Replace singular repository assumptions with a required `repositories`
  map keyed by stable `repo_id`.
- [x] Define each repository's canonical root, expected remote identity,
  evidence roots, writable roots, default branch policy, and permission policy
  reference.
- [x] Define project-level specifications and plans separately from repository
  working directories.
- [x] Canonicalize and validate every root using resolved paths.
- [x] Reject duplicate roots, overlapping writable roots unless explicitly
  allowed, missing repositories, unexpected remotes, and symlink escapes.
- [x] Keep private project and sibling repository values only in the
  reference project configuration fixture.

**Acceptance criteria:**

- [x] A project with two sibling repositories validates successfully.
- [x] An unknown `repo_id` cannot reach subprocess creation.
- [x] Per-repository policy selection uses exact IDs, never substring matching.

### DISP-102: Define normalized plan schema version 1

**Purpose:** Give the dispatcher a generic, deterministic plan model independent
of Markdown layout.

**Actions:**

- [x] Define a normalized plan containing `schema_version`, `plan_id`, source
  file identities, source hashes, and ordered step records.
- [x] Require each step to define `step_id`, title, `repo_id`, `depends_on`,
  required inputs, produced outputs, resource locks, risk tags, authorization,
  acceptance criteria, evidence requirements, review obligations, and allowed
  retry/escalation policy.
- [x] Define graph validation for missing dependencies, duplicate IDs, cycles,
  invalid repository references, conflicting resources, and unreachable steps.
- [x] Separate immutable plan definition from mutable run status.
- [x] Persist the normalized plan and its digest in the run state.
- [x] Require operator approval when a normalized plan is newly generated or
  its source hashes change.
- [x] Implement an explicit YAML-sidecar importer first.
- [x] Treat Markdown import as an adapter interface with no authorization to
  guess missing required fields.
- [x] Implement the legacy Markdown importer as a reference adapter only.
- [x] Remove reference-project step regexes, role names, paths, and default executor choices
  from core parsing logic.

**Acceptance criteria:**

- [x] The core plan model accepts non-`T` step IDs and arbitrary configured role
  keys.
- [x] Two different source formats can produce the same normalized plan digest.
- [x] Missing authorization, evidence, dependency, or acceptance fields fail
  normalization rather than being inferred.
- [x] A cyclic or otherwise invalid dependency graph is rejected before any
  session starts.

### DISP-103: Define supervisor command protocol version 1

**Purpose:** Replace the comment-sensitive envelope and natural-language
execution fallback with a strict contract.

**Actions:**

- [x] Separate `action` from role identity.
- [x] Define actions for `dispatch`, `ask_operator`, `halt`, and
  `request_completion`.
- [x] Define a strict JSON schema containing protocol version, action, step ID,
  target role key, session mode, prompt payload, and optional rationale.
- [x] Do not accept supervisor-provided raw session IDs. Use logical session
  references resolved from dispatcher-owned state.
- [x] Require the JSON object at the beginning of the response and reject
  duplicate objects, unknown keys, comments, invalid types, and trailing action
  objects.
- [x] Keep batch out of protocol version 1. Reserve it for Phase 7.
- [x] Make natural-language parsing diagnostic-only: it may suggest a repair to
  the operator but must never launch a subprocess.
- [x] Generate copyable supervisor examples from schema-valid fixtures.
- [x] Add parser conformance and fuzz tests.

**Acceptance criteria:**

- [x] Every documented example validates against the same schema used at
  runtime.
- [x] Malformed or unknown commands cannot become executable routes.
- [x] No core parser contains model-specific or private-project keywords.

### DISP-104: Define executor and reviewer result protocols

**Purpose:** Make completion, review, and escalation machine-enforceable.

**Actions:**

- [x] Define executor outcomes: `completed`, `blocked`, and `failed`.
- [x] Require executor results to include dispatch ID, attempt, step ID,
  repository coordinates, base revision, resulting revision or patch hash,
  evidence artifacts, verification results, and a concise summary.
- [x] Define reviewer verdicts: `accepted`, `changes_requested`, `blocked`, and
  `inconclusive`.
- [x] Require reviewer results to include reviewed revision or artifact hashes,
  findings, verification results, and required remediation.
- [x] Reject results that do not match the active dispatch, step, repository,
  attempt, or immutable review target.
- [x] Store free-form chat as supplemental transcript content, not workflow
  status.
- [x] Add valid, invalid, stale-attempt, wrong-revision, and duplicate-result
  fixtures.

**Acceptance criteria:**

- [x] The dispatcher can determine the next state without interpreting prose.
- [x] A reviewer cannot accept work different from the revision it was asked to
  review.
- [x] Late results from superseded attempts are rejected from active-state application.

### DISP-105: Define run and step state machines

**Purpose:** Establish one authoritative transition model before rebuilding the
loop.

**Actions:**

- [x] Define run states: `NEW`, `READY`, `RUNNING`, `WAITING_OPERATOR`,
  `HALTED`, `FAILED`, `SUCCEEDED`, and `CANCELLED`.
- [x] Define step states: `PENDING`, `READY`, `EXECUTING`, `EXECUTED`,
  `REVIEW_REQUIRED`, `REVIEWING`, `CHANGES_REQUESTED`, `BLOCKED`, `ACCEPTED`,
  `WAIVED`, and `FAILED`.
- [x] Define dispatch states: `PREPARED`, `RUNNING`, `COMPLETED`, `FAILED`,
  `FORWARDED`, `ACKNOWLEDGED`, and `ABANDONED`.
- [x] Define allowed transitions and required event data for each transition.
- [x] Define completion invariants: all required steps accepted or explicitly
  waived, all dependencies satisfied, all review obligations met, no dispatch
  in flight, all required evidence present, and all operator gates resolved.
- [x] Define retry, rework, escalation, halt, cancellation, and resumption
  semantics.
- [x] Define exit codes for each terminal run state.
- [x] Encode transition tests before implementing the new loop.

**Acceptance criteria:**

- [x] Every transition has a unit test for allowed and rejected predecessor
  states.
- [x] Premature completion is rejected with a structured list of unmet
  obligations.
- [x] `ask_operator` is non-terminal and crash-resumable.

### Phase 1 gate

- [x] Configuration, repository, plan, command, result, and state schemas are
  versioned and documented.
- [x] The legacy reference adapter normalizes an explicit sidecar without
  core-specific constants. Full historical baseline import remains Phase 4.
- [x] Schema and state-machine tests pass.
- [x] No real OpenCode execution is enabled yet.

## 7. Phase 2: OpenCode and permission boundaries

### DISP-200: Implement the versioned OpenCode event decoder

**Purpose:** Correctly decode supported OpenCode output and reject drift.

**Actions:**

- [x] Decode `sessionID`, text from supported `part` events, usage and cost from
  completion events, and structured error events.
- [x] Preserve event ordering and selected sanitized raw metadata.
- [x] Reject missing session IDs for new sessions.
- [x] Reject missing final assistant output for command types that require it.
- [x] Report malformed lines and unknown required event types explicitly.
- [x] Parse output incrementally rather than buffering unbounded JSONL.
- [x] Run the complete captured fixture corpus as contract tests.
- [x] Add a runtime version check with a clear unsupported-version error.

**Acceptance criteria:**

- [x] Every supported fixture yields the expected session, text, usage, cost,
  and error result.
- [x] Unknown incompatible output fails closed without advancing workflow state.

### DISP-201: Rebuild subprocess lifecycle management

**Purpose:** Bound resources and stop complete process trees on timeout.

**Actions:**

- [x] Replace `subprocess.run(capture_output=True)` with streaming `Popen`
  management.
- [x] Remove the positional `-` and send prompts through supported stdin
  behavior.
- [x] Start OpenCode in a dedicated process group.
- [x] On timeout or cancellation, terminate the process group, wait for a grace
  period, then kill remaining descendants.
- [x] Bound in-memory output and stream complete sanitized logs to private
  state storage.
- [x] Preserve redacted stderr and process exit metadata.
- [x] Make timeout values required YAML configuration.
- [x] Add tests with child and grandchild processes plus high-volume output.

**Acceptance criteria:**

- [x] No test descendant survives timeout cleanup.
- [x] Large output remains within a documented memory bound.
- [x] Nonzero exit and timeout produce typed failures.

### DISP-202: Correct session lifecycle operations

**Purpose:** Make new, resume, fork, list, and recovery behavior exact.

**Actions:**

- [x] Use exact structured session listing where available.
- [x] Compare exact session IDs rather than substrings.
- [x] Validate every session against the active project and dispatcher registry.
- [x] Implement fork with `--fork` and record parent-child lineage.
- [x] Reject resume when the logical session reference is missing or stale.
- [x] Define explicit recovery behavior for missing supervisor and slave
  sessions.
- [x] Record OpenCode version and working directory for every session.
- [x] Add new, resume, fork, stale, foreign, missing, and recovery tests.

**Acceptance criteria:**

- [x] A supervisor cannot select an arbitrary local OpenCode session.
- [x] Fork creates and records a distinct child session.
- [x] Resume never silently creates a new session.

### DISP-203: Apply permissions mechanically and fail closed

**Purpose:** Make the declared safety boundary real.

**Actions:**

- [x] Define explicit precedence: global default, project policy, repository
  policy, role-class policy, concrete-role policy, dispatch authorization.
- [x] Compile the effective policy for the exact repository and dispatch.
- [x] Apply the generated policy through `OPENCODE_CONFIG_CONTENT` in the child
  environment.
- [x] Preserve the global default in the generated OpenCode payload.
- [x] Treat missing permission configuration as a startup error for real runs.
- [x] Never enable `--auto` while any applicable operation is `ask` unless an
  explicit supported mediation flow exists.
- [x] Map semantic repository operations to tested OpenCode patterns.
- [x] Validate dispatch authorization against structured plan authorization,
  not free-form `Authorized` prose.
- [x] Remove pool-name ambiguity so executor and reviewer class overrides apply
  to concrete roles.
- [ ] Add harmless allow, ask, and deny integration tests for each role class
  and repository policy.

**Acceptance criteria:**

- [ ] Every effective OpenCode policy exactly matches the compiled policy
  fixture.
- [ ] A denied command remains denied with auto approval enabled.
- [ ] An ask command is never silently approved.
- [ ] Reviewer write and shell restrictions are proven by integration tests.

### DISP-204: Minimize child credentials and protect local state

**Purpose:** Reduce damage if an agent or plugin reads its environment or state.

**Actions:**

- [x] Build a minimal allowlisted subprocess environment.
- [x] Pass only credentials required by the exact role and dispatch.
- [ ] Prefer short-lived credential brokers or scoped tokens where available.
- [x] Create state directories with owner-only permissions.
- [x] Create state, transcript, audit, result, and generated policy files with
  owner-only permissions.
- [x] Remove prompt-prefix debug logging or apply structured redaction.
- [x] Redact credentials, credential-bearing URLs, headers, and known secret
  patterns before persistence.
- [x] Add child-environment and file-mode tests.

**Acceptance criteria:**

- [x] A test agent cannot read unrelated parent environment variables.
- [x] State artifacts are inaccessible to other local users under supported
  operating systems.
- [x] Secret fixtures never appear in logs, transcripts, audit, or errors.

### Phase 2 gate

- [x] OpenCode fixture tests pass for the pinned version.
- [x] Session lifecycle tests pass.
- [ ] Permission integration tests prove allow, ask, and deny behavior.
- [x] Process-tree timeout and credential-isolation tests pass.
- [x] Real execution remains disabled until transactional state is complete.

## 8. Phase 3: Transactional state and recovery

### DISP-300: Adopt one transactional state store

**Purpose:** Eliminate inconsistent `state.json` and `sessions.json` checkpoints.

**Actions:**

- [x] Record an architecture decision to use SQLite from the Python standard
  library as the authoritative runtime store.
- [x] Define tables for runs, normalized plans, steps, dispatches, sessions,
  reviews, artifacts, operator decisions, locks, and audit events.
- [x] Enable appropriate durability settings and document filesystem
  assumptions.
- [x] Add schema versioning and explicit migrations.
- [x] Store immutable IDs and foreign-key relationships.
- [x] Keep Markdown transcripts and JSONL audit exports as derived artifacts,
  not authoritative state.
- [x] Stop execution on transaction or durability failure.
- [x] Add migration tests and simulated partial-write tests.

**Acceptance criteria:**

- [x] A committed transition cannot leave run, step, dispatch, and session state
  at different generations.
- [x] Database corruption and unsupported schema versions fail with recovery
  instructions.

### DISP-301: Add single-writer and resource locking

**Purpose:** Prevent concurrent dispatchers from sharing state or repositories.

**Actions:**

- [x] Acquire a run lease before bootstrap or resume.
- [x] Include owner process, host, run ID, acquisition time, and heartbeat.
- [x] Define stale-lease detection and operator-approved recovery.
- [x] Add repository and declared resource locks keyed by normalized IDs.
- [x] Reject a second dispatcher before it starts an OpenCode subprocess.
- [x] Add two-process contention and stale-owner fault tests.

**Acceptance criteria:**

- [x] Exactly one dispatcher owns a run and repository resource at a time.
- [x] Lock recovery cannot occur silently while the prior owner may still run.

### DISP-302: Implement correlated audit and transcript storage

**Purpose:** Make every action reconstructable without filename collisions.

**Actions:**

- [x] Assign run, event, dispatch, attempt, causation, correlation, and session
  IDs.
- [x] Store request, response, evidence, config, plan, tool-version, and
  transcript hashes.
- [x] Use unique sequence or UUID transcript names with exclusive creation.
- [ ] Record terminal completion, halt, failure, cancellation, and recovery
  events.
- [x] Export a deterministic human-readable run report from authoritative state.
- [ ] Add optional tamper-evident event chaining if audit requirements demand
  it.
- [ ] Add same-second, repeated-prompt, and duplicate-result tests.

**Acceptance criteria:**

- [ ] Every dispatch has exactly one current attempt and correlated result or
  explicit unresolved state.
- [x] No transcript can overwrite another transcript.
- [ ] A run can be reconstructed from the state store and hashed artifacts.

### DISP-303: Implement crash-safe dispatch transitions

**Purpose:** Reconcile external side effects after process loss.

**Actions:**

- [x] Commit `PREPARED` with exact prompt, policy, repository revision,
  expected result, and idempotency key before subprocess launch.
- [x] Commit `RUNNING` with process and session metadata after launch.
- [x] Commit `COMPLETED` or `FAILED` with the exact result before forwarding.
- [x] Persist the complete supervisor forwarding payload before sending it.
- [x] Commit `FORWARDED` and `ACKNOWLEDGED` separately.
- [x] On startup, classify every unresolved dispatch by last durable state.
- [x] Never automatically retry a potentially non-idempotent `RUNNING`
  dispatch.
- [x] Require operator reconciliation or an external idempotency proof before
  retrying uncertain side effects.
- [ ] Add crash injection at every transition boundary.

**Acceptance criteria:**

- [ ] Every injected crash resumes into a deterministic recovery state.
- [ ] Completed results are never lost before supervisor forwarding.
- [x] Potentially completed external side effects are never blindly repeated.

### DISP-304: Separate start, resume, recover, and answer operations

**Purpose:** Remove implicit state reuse and blocking stdin from the core loop.

**Actions:**

- [x] Define `start` to require no active run for the selected project.
- [x] Define `resume` to require a resumable non-terminal run.
- [ ] Define `recover` to inspect unresolved dispatches and stale sessions.
- [x] Define `answer` to persist an operator response to a waiting run.
- [x] Require an explicit archive or new-run operation after terminal states.
- [x] Make `WAITING_OPERATOR` durable before returning control to the operator.
- [ ] Keep interactive prompting as a CLI wrapper over persisted decisions.
- [ ] Add stale state, missing state, terminal state, EOF, repeated answer, and
  answer-after-cancellation tests.

**Acceptance criteria:**

- [ ] `start` never resumes old state.
- [ ] `resume` never creates a new supervisor silently.
- [ ] An operator question survives process termination and can be answered by
  a later command.

### Phase 3 gate

- [x] Transactional state and migrations pass fault tests.
- [x] Single-writer locking passes concurrent-process tests.
- [ ] Every dispatch crash window has a deterministic recovery test.
- [x] Start, resume, recover, and answer semantics are documented and tested.

## 9. Phase 4: Correct sequential workflow

### DISP-400: Build the supervisor bootstrap from normalized inputs

**Purpose:** Give the supervisor exact authoritative context instead of
directory hints.

**Actions:**

- [x] Include project ID, repository registry, exact specification files,
  selected plan files, normalized plan digest, source hashes, role registry,
  profile obligations, and current accepted baseline.
- [x] Include only schema-valid protocol examples generated from fixtures.
- [x] Include explicit dispatcher-enforced constraints and unsupported actions.
- [x] Persist the rendered bootstrap and its hash before creating the session.
- [x] Refuse bootstrap when source hashes or normalized plan approval are
  missing.
- [x] Package templates inside the wheel and test installed-path resolution.

**Acceptance criteria:**

- [x] The supervisor never has to guess the selected plan or roles document.
- [x] The installed wheel renders the same bootstrap as the source checkout.

### DISP-401: Implement validated sequential dispatch

**Purpose:** Replace route heuristics with a deterministic execution path.

**Actions:**

- [x] Parse the strict supervisor command.
- [x] Resolve the step from the normalized plan.
- [x] Verify step readiness, target role eligibility, repository, dependencies,
  resources, session mode, review obligation, retry limit, and permission
  policy.
- [x] Derive working directory, repository policy, prompt constraints, evidence
  requirements, and logical session from dispatcher state.
- [x] Create one correlated dispatch attempt and execute through the Phase 2
  adapter.
- [x] Apply the typed result without interpreting free-form chat.
- [x] Persist the next supervisor message before the next supervisor turn.
- [x] Reject unsupported `batch` or parallel requests explicitly.

**Acceptance criteria:**

- [x] Unknown target, step, mode, session, repository, or unmet dependency fails
  before subprocess creation.
- [x] A successful executor result transitions only the intended step.
- [x] A failed result does not advance plan position.

### DISP-402: Implement reviewer dispatch and verdict handling

**Purpose:** Make independent review a first-class state transition.

**Actions:**

- [x] Create fresh reviewer sessions by default.
- [x] Bind each review to exact repository and artifact coordinates.
- [x] Validate reviewer independence rules from compiled policy.
- [x] Apply `accepted`, `changes_requested`, `blocked`, and `inconclusive`
  verdicts according to the state machine.
- [x] Resume the correct executor task for rework using logical session lineage.
- [x] Preserve every review attempt and superseded verdict for audit.
- [x] Reject acceptance when the reviewed revision no longer matches current
  work.

**Acceptance criteria:**

- [x] A reviewer cannot mutate repositories under the tested reviewer policy.
- [x] Changes requested return the step to a deterministic rework state.
- [x] Acceptance applies only to the exact reviewed revision.

### DISP-403: Implement dispatcher-owned completion guard

**Purpose:** Prevent the supervisor from ending an incomplete or invalid run.

**Actions:**

- [x] Treat supervisor completion as a request, not a terminal command.
- [x] Evaluate every completion invariant from DISP-105.
- [x] Return a structured unmet-obligations response when completion is denied.
- [x] Generate the final report from authoritative run, step, review, evidence,
  and audit data.
- [x] Commit `SUCCEEDED` and a terminal audit event only after report generation
  succeeds.
- [x] Add premature, complete, waived-step, unresolved-review, missing-evidence,
  in-flight-dispatch, and failed-step tests.

**Acceptance criteria:**

- [x] No supervisor response alone can bypass required work or review.
- [x] Final reports identify exact accepted revisions and evidence hashes.

### DISP-404: Implement per-step rounds and failure semantics

**Purpose:** Replace the current global multiplied loop bound with explicit
step-level controls.

**Actions:**

- [x] Track executor attempts, review attempts, rework rounds, and stalls per
  step.
- [x] Read all limits from validated YAML or normalized plan policy.
- [x] Define which failures are retryable, blocked, escalated, or terminal.
- [x] Require operator approval before retrying uncertain external side effects.
- [x] Remove implicit mode changes such as resume-to-new fallback.
- [x] Add boundary and exhaustion tests for every counter.

**Acceptance criteria:**

- [x] One stuck step cannot consume the allowance of unrelated steps.
- [x] Exceeded limits produce deterministic escalation or halt states.

### DISP-405: Import the existing private-project baseline

**Purpose:** Continue the real project without repeating or blindly trusting
completed work.

**Actions:**

- [x] Add `dispatcher baseline inspect` to read the normalized plan, Git
  history, and configured evidence sources without changing state.
- [x] Produce a read-only baseline observation containing each step's revision,
  repository revision, evidence hashes, review evidence, and detected gaps.
- [x] Never infer that a continuous historical prefix is accepted solely because a later checkpoint is
  present; validate every preceding step independently.
- [x] Add `dispatcher baseline approve` to record explicit operator decisions and an immutable baseline
  before the first new run.
- [x] Mark unsupported or unverifiable historical work as `PENDING` or
  explicitly `WAIVED`, never silently accepted.
- [x] Store importer version and source hashes.
- [ ] Add a sanitized fixture representing the independently verified historical
  checkpoint. Deferred until the generic framework has completed review.

**Acceptance criteria:**

- [ ] Every historical step receives explicit independently justified status.
- [ ] The first new dispatch targets only a ready step after the approved
  baseline.
- [x] Changing historical evidence invalidates the affected baseline record.

### Phase 4 gate

- [x] A sequential executor-reviewer-rework-completion scenario passes end to
  end in a disposable local Git fixture.
- [x] Every premature completion case is rejected.
- [x] Failed subprocesses never advance workflow state.
- [ ] The private reference baseline imports without executing work.
- [ ] Real execution may be enabled only for disposable local repositories and
  non-sensitive test credentials after this gate.

## 10. Phase 5: Multi-repository execution and evidence integrity

### DISP-500: Route by authoritative repository identity

**Purpose:** Execute each step in the correct repository with the correct
policy.

**Actions:**

- [x] Derive `repo_id` from the normalized step.
- [x] Treat a supervisor-provided repository value as a consistency assertion,
  not authority.
- [x] Select working directory, remote expectations, permission policy,
  evidence roots, and resource locks from the repository registry.
- [x] Verify repository identity and clean/expected baseline before dispatch.
- [x] Refuse unregistered nested, sibling, or symlink-resolved directories.
- [x] Add two-repository integration fixtures and wrong-repository tests.

**Acceptance criteria:**

- [x] A step cannot execute in a different repository than its normalized plan
  declaration.
- [x] Per-repository permissions are selected by exact ID.

### DISP-501: Bind work to immutable repository coordinates

**Purpose:** Ensure executors and reviewers discuss the same work product.

**Actions:**

- [x] Record base branch, base SHA, working branch, worktree identity, and
  expected remote before execution.
- [x] Record resulting head SHA or content-addressed patch after execution.
- [x] Require review dispatches to reference those exact coordinates.
- [x] Detect repository movement between execution, review, and acceptance.
- [x] Reject acceptance after unreviewed changes.
- [x] Define behavior for uncommitted work and require a patch hash if commits
  are prohibited.
- [x] Add moving-head, wrong-branch, uncommitted-change, and stale-review tests.

**Acceptance criteria:**

- [x] Accepted work is reproducibly identifiable after the run.
- [x] Two reviewers in a multi-review step always review the same coordinates.

### DISP-502: Replace filename evidence diff with content manifests

**Purpose:** Track created, modified, and deleted artifacts accurately.

**Actions:**

- [x] Snapshot authorized roots with relative path, file type, size, metadata,
  and cryptographic hash.
- [x] Record created, modified, deleted, and unexpected paths.
- [x] Use attempt-specific evidence identities or immutable hashes.
- [x] Validate required evidence against the executor or reviewer result.
- [x] Detect writes outside authorized roots using repository status and
  configured external roots.
- [x] Do not attribute concurrent unrelated changes without a matching isolated
  worktree or dispatch correlation.
- [x] Add create, modify, delete, rename, symlink, unexpected-write, and
  concurrent-write tests.

**Acceptance criteria:**

- [x] Modified evidence is never omitted.
- [x] Unexpected repository writes halt acceptance.
- [x] Final reports include content hashes for every required artifact.

### Phase 5 gate

- [x] A disposable two-repository plan executes sequentially with exact
  repository routing.
- [x] Reviews are revision-bound.
- [x] Evidence manifests detect all tested change types.
- [x] Repository and external-path escape tests pass.

## 11. Phase 6: Policy, review profiles, and operator controls

### DISP-600: Compile profile and plan review obligations

**Purpose:** Turn profile configuration into enforceable per-step requirements.

**Actions:**

- [x] Define explicit precedence among mandatory plan review, project policy,
  selected profile, per-step override, and operator waiver.
- [x] Reject contradictory review configuration at startup.
- [x] Compile a concrete review obligation for every step before execution.
- [x] Store the compiled obligation and source policy hashes in run state.
- [x] Make supervisor review proposals subject to the compiled obligation.
- [x] Add economy, balanced, thorough, multi-review on/off, mandatory review,
  and conflicting-policy tests.

**Acceptance criteria:**

- [x] A mandatory review cannot be skipped by the supervisor or a weaker
  profile.
- [x] Every step's required reviewer count and independence class are visible
  before execution starts.

### DISP-601: Implement rework and escalation policy

**Purpose:** Make review rejection and stalls deterministic.

**Actions:**

- [x] Implement configured rework rounds.
- [x] Implement tie-break review only against the same immutable artifact.
- [x] Implement high-risk second review according to compiled obligations.
- [x] Implement executor reassignment while preserving attempt lineage.
- [x] Define exhaustion outcomes and operator intervention points.
- [x] Add disagreement, repeated rejection, inconclusive review, blocked
  reviewer, reassignment, and exhaustion tests.

**Acceptance criteria:**

- [x] Every review verdict has one deterministic next transition.
- [x] Escalation cannot reset counters or lose prior findings.

### DISP-602: Implement durable operator gates and underspecification

**Purpose:** Pause safely without blocking the orchestration process on stdin.

**Actions:**

- [x] Represent risky actions and underspecification questions as typed operator
  decisions.
- [x] Enter `WAITING_OPERATOR` before returning from the active command.
- [x] Persist question, allowed answers, context, expiration, and required role.
- [x] Apply answers through the `answer` command and resume from the exact
  blocked transition.
- [x] Compile `underspec_mode` into permission and workflow policy.
- [x] Remove subjective exceptions such as "genuine project-level
  clarification."
- [x] Add crash-while-waiting, duplicate answer, invalid answer, denied action,
  and answer-to-terminal-route tests.

**Acceptance criteria:**

- [x] No question is lost on process exit.
- [x] Full-auto mode cannot silently approve an `ask` permission.
- [x] Operator decisions are correlated and auditable.

### DISP-603: Enforce budget and context limits

**Purpose:** Turn documented limits into dispatcher-owned controls.

**Actions:**

- [x] Require explicit run cost cap, per-step attempt cap, wall-clock timeout,
  and context policy when the feature is enabled.
- [x] Consume normalized usage and cost from the OpenCode adapter.
- [x] Persist cumulative run, step, role, and session usage.
- [x] Check limits before dispatch and after each result.
- [x] Define halt, fork, compact, or operator decision behavior for each limit.
- [x] Replace unresolved forwarding placeholders with structured measured
  values.
- [x] Add threshold, missing-usage, over-budget, resumed-session, and cumulative
  accounting tests.

**Acceptance criteria:**

- [x] A run cannot exceed a configured cost cap through a new dispatch.
- [x] Missing required usage data fails closed rather than reporting zero.

### Phase 6 gate

- [x] Review profile and escalation matrices pass.
- [x] Operator questions and gates survive restart.
- [x] Budget and context limits are enforced from measured data.
- [x] Completion remains impossible while any policy obligation is unresolved.

## 12. Phase 7: Dependency scheduling and bounded parallelism

### DISP-700: Implement the readiness scheduler

**Purpose:** Dispatch only steps whose dependencies and resources permit work.

**Actions:**

- [x] Compute ready steps from accepted dependencies and normalized inputs.
- [x] Validate resource availability, repository locks, role capacity, global
  capacity, and operator gates.
- [x] Keep scheduling deterministic for identical state.
- [x] Explain why blocked steps are not ready.
- [x] Add fan-in, fan-out, cross-repository dependency, resource conflict, and
  no-ready-step tests.

**Acceptance criteria:**

- [x] A dependent step never starts before every required predecessor is
  accepted.
- [x] Different repositories do not imply independence unless the plan graph
  and resource declarations agree.

### DISP-701: Add batch protocol version 2

**Purpose:** Introduce batch only after individual dispatch is safe.

**Actions:**

- [x] Define a versioned batch command containing independently valid child
  dispatch requests.
- [x] Validate the entire batch before starting any child.
- [x] Define partial-start, partial-failure, cancellation, and result-join
  semantics.
- [x] Assign one parent correlation ID and independent child dispatch IDs.
- [x] Return a structured batch result to the supervisor.
- [x] Reject batches that exceed configured capacity or conflict on resources.

**Acceptance criteria:**

- [x] An invalid child prevents all child starts.
- [x] Every started child has an independent recoverable state.
- [x] Batch results cannot hide a failed child.

### DISP-702: Implement bounded concurrent execution

**Purpose:** Run independent work without repository or state races.

**Actions:**

- [x] Use the scheduler's ready set and resource locks.
- [x] Enforce per-role and global concurrency limits from validated YAML.
- [x] Prefer isolated worktrees for concurrent repository work where supported.
- [x] Persist every child transition independently.
- [x] Define coordinated cancellation and timeout behavior.
- [x] Add crash, timeout, one-child failure, lock conflict, and concurrent state
  update tests.

**Acceptance criteria:**

- [x] Concurrent runs cannot write the same locked resource.
- [x] A dispatcher crash preserves each child's exact recoverable state.
- [x] Sequential behavior remains available and uses the same transition model.

Same-repository parallelism remains deliberately disabled. The current state
model has no durable worktree branch/merge lifecycle, so isolated worktree
execution is deferred rather than risking incorrect dependency propagation.

### Phase 7 gate

- [x] Dependency scheduling tests pass.
- [x] Batch schema and all-or-none validation pass.
- [x] Concurrent fault-injection tests pass without lost state or leaked
  processes.
- [x] Parallel execution remains disabled in project YAML until explicitly
  enabled and reviewed.

## 13. Phase 8: Migration, hardening, and release

### DISP-800: Complete the automated assurance matrix

**Purpose:** Protect every safety and correctness boundary before release.

**Actions:**

- [x] Add property tests for configuration, plan, and protocol parsing.
- [x] Add state-machine model tests covering random valid and invalid event
  sequences.
- [x] Add crash injection around every durable transition.
- [x] Add two-process and multi-repository concurrency tests.
- [x] Add secret scanning to CI for fixtures, diffs, and generated reports.
- [x] Add dependency vulnerability and package integrity checks.
- [x] Add deterministic end-to-end tests using disposable Git repositories and
  a fake OpenCode executable.
- [x] Add a separately gated live OpenCode smoke suite with harmless read-only
  prompts.

**Acceptance criteria:**

- [x] Every finding in the review maps to at least one regression test.
- [x] Live smoke tests are optional for ordinary CI but required for an
  OpenCode-version compatibility release.

### DISP-801: Add observability and support artifacts

**Purpose:** Make unattended runs diagnosable without exposing secrets.

**Actions:**

- [x] Emit structured logs with timestamp, level, module, function, project ID,
  run ID, dispatch ID, and step ID.
- [x] Add status views for run, ready steps, active dispatches, waiting
  decisions, budgets, locks, and terminal outcome.
- [ ] Add health and readiness checks for a supervised deployment mode if one
  is introduced.
- [x] Export sanitized run reports, audit JSONL, and evidence manifests.
- [x] Add bounded retention and archival configuration in YAML.
- [x] Ensure observability failures that threaten state integrity are fatal;
  secondary presentation failures may be reported without changing workflow
  truth.

**Acceptance criteria:**

- [x] An operator can correlate every log, transcript, dispatch, result, review,
  and artifact from the run ID.
- [x] Sanitized support bundles pass secret scanning.

### DISP-802: Migrate the private reference project

**Purpose:** Prove generality using the first real project without embedding it
in core code.

**Actions:**

- [ ] Create a versioned project configuration using the repository registry.
- [ ] Create or generate the normalized plan through the reference import adapter.
- [ ] Review every normalized dependency, resource, authorization, acceptance,
  evidence, and review obligation.
- [ ] Import and approve every historical baseline step independently.
- [ ] Run preflight and a no-dispatch plan validation report.
- [ ] Run a fully mocked continuation from the first pending step to completion.
- [ ] Run a disposable-clone, read-only live OpenCode validation.
- [ ] Run one operator-observed, low-risk sequential executor-reviewer step only
  after all prior gates pass.
- [ ] Record discrepancies as importer or generic-engine issues, not hardcoded
  private-project exceptions.

**Acceptance criteria:**

- [ ] No core source file contains private-project step IDs, repository names, local
  paths, or model-specific fallback choices.
- [ ] The approved baseline prevents re-execution of accepted historical steps.
- [ ] The first pending step follows dependency, repository, permission, review,
  and evidence policy.

### DISP-803: Publish release and operations documentation

**Purpose:** Make supported behavior and limits explicit.

**Actions:**

- [x] Publish installation and pinned OpenCode compatibility instructions.
- [x] Publish project configuration and normalized-plan authoring guides.
- [x] Publish start, resume, recover, answer, cancel, archive, and baseline
  procedures.
- [x] Publish permission-policy examples with tested allow, ask, and deny
  behavior.
- [x] Publish crash-recovery and uncertain-side-effect procedures.
- [x] Publish multi-repository, evidence, review, and parallelism constraints.
- [x] Publish a migration guide from the proof-of-concept state and config.
- [x] Update README and roadmap capability matrices from automated feature
  metadata or release checks where practical.

**Acceptance criteria:**

- [x] A new project can be configured without reading private-project-specific
  documentation.
- [x] Every operational command documents preconditions, state transitions,
  and exit codes.

### Final release gate

- [ ] All phase gates pass in CI.
- [ ] No critical or high review finding remains open without a documented,
  operator-approved exception.
- [ ] Secret scanning passes.
- [ ] The wheel installs and operates from a clean environment.
- [ ] The pinned OpenCode live compatibility suite passes.
- [ ] The private reference project baseline is approved and immutable.
- [ ] A disposable multi-repository end-to-end run completes with required
  review and evidence.
- [ ] Crash recovery and concurrent-start tests pass repeatedly.
- [ ] Repository-mutating execution remains opt-in through explicit validated
  YAML and documented operator approval.

## 14. Required test matrix

| Priority | Boundary | Required proof |
|---|---|---|
| P0 | OpenCode events | Captured version fixtures decode exactly; unknown schema fails |
| P0 | Permissions | Per-role and per-repo allow, ask, deny behavior is enforced |
| P0 | Process failure | Nonzero, malformed, timeout, and missing result never advance state |
| P0 | Completion | Missing step, review, evidence, gate, or dependency blocks success |
| P0 | Crash recovery | Every transition boundary resumes deterministically |
| P0 | Single writer | A second dispatcher cannot own the same run or repository |
| P0 | Protocol authorization | Unknown role, step, repo, mode, or session is rejected before launch |
| P1 | Baseline import | Historical steps require revisions, evidence, and operator approval |
| P1 | Review integrity | Verdict applies only to exact immutable work coordinates |
| P1 | Configuration | Unknown, missing, mistyped, and out-of-range values fail early |
| P1 | Secrets | Child environment, logs, state, audit, and reports expose no fixture secret |
| P1 | Evidence | Create, modify, delete, rename, escape, and unexpected writes are detected |
| P1 | Operator wait | Questions and gates survive crash and duplicate answers |
| P1 | Budget | Cost, context, timeout, and rounds stop new work at exact boundaries |
| P1 | Packaging | Installed wheel includes templates and behaves outside source checkout |
| P2 | Parallelism | Dependencies, capacities, resources, crashes, and partial failures are safe |
| P2 | Observability | Every artifact correlates to run, step, dispatch, attempt, and session |

## 15. Finding-to-work mapping

| Review finding | Primary remediation tasks |
|---|---|
| Permission enforcement bypassed | DISP-203, DISP-204 |
| OpenCode 1.18.11 decoding broken | DISP-003, DISP-200, DISP-201 |
| Preflight crashes | DISP-002 |
| Completion and review not enforced | DISP-104, DISP-105, DISP-402, DISP-403 |
| Multi-repository model absent | DISP-101, DISP-500, DISP-501 |
| Failed subprocess advances progress | DISP-200, DISP-201, DISP-401, DISP-404 |
| Crash recovery duplicates work | DISP-300 through DISP-304 |
| Reference-project coupling | DISP-102, DISP-103, DISP-802 |
| Declared policy is inert | DISP-100, DISP-600 through DISP-603 |
| Selected plan is ambiguous | DISP-102, DISP-400 |
| Partial project baseline absent | DISP-405, DISP-802 |
| Strict envelope invalid | DISP-103 |
| Dependencies and immutable artifacts absent | DISP-102, DISP-104, DISP-501, DISP-700 |
| Session identity unsafe | DISP-202, DISP-401 |
| Run lifecycle ambiguous | DISP-105, DISP-304 |
| No automated assurance | DISP-001, DISP-800 |
| Configuration incomplete | DISP-100 |
| Evidence tracking insufficient | DISP-501, DISP-502 |
| Audit and transcript durability insufficient | DISP-300, DISP-302 |
| Credential exposure broad | DISP-204 |
| Process containment incomplete | DISP-201 |
| Maturity documentation stale | DISP-000, DISP-803 |

## 16. Pull request slicing

Each task should normally be one pull request. A pull request must not combine a
schema change, state migration, OpenCode adapter rewrite, and new workflow
feature unless the change cannot be tested independently.

Recommended first pull requests:

1. `DISP-000`: capability matrix and temporary real-run block.
2. `DISP-001`: test, lint, type, build, and CI baseline.
3. `DISP-002`: preflight correction and tests.
4. `DISP-003`: sanitized OpenCode fixtures and failing contract tests.
5. `DISP-100`: strict configuration schema version 1.
6. `DISP-101`: repository registry schema.
7. `DISP-102`: normalized plan schema and YAML-sidecar importer.
8. `DISP-103`: strict supervisor command JSON schema and parser.
9. `DISP-104`: executor and reviewer result schemas.
10. `DISP-105`: state-machine definitions and model tests.

Every pull request must include:

- [ ] the task ID in its title or description;
- [ ] tests proving the acceptance criteria it addresses;
- [ ] documentation updates for changed contracts;
- [ ] a note about migration or compatibility impact;
- [ ] confirmation that no project-specific constant entered core code; and
- [ ] the exact verification commands and results.
