# Dispatcher Project Review

**Review date:** 2026-08-09
**Scope:** Architecture, specifications, dispatcher runtime, configuration,
OpenCode integration, security boundaries, persistence, recovery, testing, and
project neutrality.
**Reference project:** sanitized multi-step project with existing historical work.
**Review method:** Read-only structural and semantic analysis, targeted runtime
probes, and comparison of documented contracts against implementation.

## Executive assessment

The hub-and-spoke supervisor-to-executor-to-reviewer model is a sound basis for
automating the existing manual workflow. Keeping OpenCode as the agent runtime
and implementing the dispatcher as a deterministic routing and policy layer is
also an appropriate architectural boundary.

The current project is not yet safe for unattended execution against real
repositories. It is simultaneously:

- too thin to enforce the workflow guarantees it advertises;
- too coupled to the Tier 2 example in its proposed plan parsing and fallback
  behavior;
- blocked by concrete defects in preflight and OpenCode result decoding; and
- missing durable state, acceptance, review, and recovery semantics.

Repository-mutating execution should remain disabled until the critical issues
below are resolved and protected by automated tests.

## Critical findings

### 1. Permission enforcement is effectively bypassed

The orchestration loop compiles permission rules but passes only an
`auto_approve` boolean to the OpenCode subprocess. The generated OpenCode
configuration is never applied:

- `src/dispatcher/loop.py:215-240`
- `src/dispatcher/sessions.py:147-153`

`should_auto_approve()` checks only the global default and ignores nested
`ask` rules:

- `src/dispatcher/permissions.py:104-111`

With the example configuration's `default: allow`, operations such as
`git push`, `kubectl`, and `helm` may be auto-approved despite being configured
as `ask`:

- `config/projects/tier2-demo.yaml:122-143`

Pool-level overrides named `executor` and `reviewer` are also ignored because
the compiler only matches concrete role keys:

- `src/dispatcher/permissions.py:55-58`

**Impact:** The documented authorization boundary does not exist at runtime.
Agents may receive broader permissions than the project, role, repository, or
individual dispatch grants.

**Recommendation:** Inject `OPENCODE_CONFIG_CONTENT` into a minimal child
environment, default to deny, account for all nested rules before enabling
`--auto`, and compile the intersection of project, repository, role, and
dispatch authorization.

### 2. The OpenCode adapter cannot decode OpenCode 1.18.11 output

The JSON decoder expects `session_id`, top-level `text`, and top-level `usage`:

- `src/dispatcher/sessions.py:256-298`

OpenCode 1.18.11 emits `sessionID` and nested `part` events. A representative
1.18.11 event returns empty chat, session, and usage with the current decoder.
The command also appends a positional `-`, although the CLI reads piped stdin
without a sentinel:

- `src/dispatcher/sessions.py:131-153`

No compatible OpenCode version is pinned:

- `pyproject.toml:1-6`

**Impact:** A real supervisor response is decoded as empty, so the dispatcher
cannot parse the first decision or preserve the session ID.

**Recommendation:** Create a versioned adapter using captured JSONL fixtures,
recognize structured error events, remove the positional `-`, fail loudly on
unknown schemas, and pin a tested OpenCode version.

### 3. Default preflight crashes before a run starts

The model smoke-test condition references undefined names `F` and
`run_sessionalse`:

- `src/dispatcher/preflight.py:61-62`

The injected runner is also not passed to `_check_models()`. The affected path
is used by both normal execution and the preflight command:

- `src/dispatcher/cli.py:125-140`

**Impact:** Default `dispatcher run` and `dispatcher preflight` execution raises
`NameError` before orchestration starts.

**Recommendation:** Correct the boolean condition, pass the injected runner,
and cover enabled, disabled, skipped, mocked, and failed smoke-test paths.

### 4. Completion and review correctness are not enforced

A supervisor `role: done` response immediately returns success:

- `src/dispatcher/loop.py:140-144`

There is no check that:

- all required steps are accepted;
- dependencies are satisfied;
- required reviewers ran;
- reviewer findings were resolved;
- evidence exists and matches the reviewed revision;
- operator gates passed;
- no dispatch remains in flight; or
- subprocesses succeeded.

The protocol defines no machine-readable executor outcome or reviewer verdict:

- `docs/protocol.md:85-109`
- `docs/protocol.md:138-177`

**Impact:** The dispatcher can report successful completion after skipped work,
failed commands, unresolved review findings, or missing evidence.

**Recommendation:** Make the dispatcher own an explicit workflow state machine.
Reject `done` until every normalized plan obligation is satisfied or explicitly
waived by a recorded operator decision.

### 5. The multi-repository use case is not representable

Configuration defines a single `project.root`:

- `docs/config-schema.md:14-20`

Repository identity exists only as free text inside prompt bodies:

- `docs/protocol.md:272-285`

Every agent process runs in the same project root:

- `src/dispatcher/loop.py:232-238`

Per-repository permissions are selected by substring-matching that root rather
than by the dispatched step:

- `src/dispatcher/permissions.py:60-68`

**Impact:** The existing project cannot safely coordinate work across
the reference repository and its sibling repositories. The dispatcher cannot choose
the correct working directory, lock key, permission policy, or evidence root.

**Recommendation:** Add a `repositories` map with stable repository IDs,
canonical roots, remotes, evidence roots, resource locks, and policies. Require
`repo_id` in every normalized step and dispatch.

### 6. Failed subprocesses are treated as successful progress

`run_session()` returns nonzero exit codes without raising and discards stderr:

- `src/dispatcher/sessions.py:147-187`

The loop nevertheless registers the session, advances `current_step`, saves a
transcript, persists state, and forwards the result as normal:

- `src/dispatcher/loop.py:244-292`

**Impact:** Authentication failures, model errors, malformed output, or tool
crashes can be recorded as completed work. A later `done` response can produce
exit code 0 over failed execution.

**Recommendation:** Treat nonzero exit, missing session ID, malformed events,
structured OpenCode errors, and missing final assistant text as explicit failed
dispatch transitions. Preserve redacted stderr and apply a configured retry or
escalation policy without advancing the step.

### 7. Crash recovery can duplicate destructive work

A dispatch is audited before execution, but startup does not reconcile
unmatched dispatches. A completed response is persisted before the forwarding
message is created, and that forwarding message exists only in memory:

- `src/dispatcher/loop.py:226-292`

State and session files are written separately. Persistence failures are
swallowed, and no process lock prevents concurrent dispatchers:

- `src/dispatcher/loop.py:422-427`
- `src/dispatcher/state.py:49-75`

**Impact:** A crash after a commit, push, deployment, or other side effect can
cause the supervisor to issue the work again. Concurrent runs can overwrite
each other's state and interleave repository mutations.

**Recommendation:** Persist a transactional dispatch lifecycle such as
`PREPARED`, `RUNNING`, `COMPLETED`, `FORWARDED`, and `ACKNOWLEDGED`. Use durable
run, dispatch, attempt, causation, and idempotency IDs. Enforce a single-writer
lock and require operator reconciliation before retrying non-idempotent work.

## High findings

### 8. Core design is coupled to the legacy reference example

The proposed fallback parser uses the exact
`tier-2-execution-roles.md` format, and plan actionability normalizes other
projects into that format:

- `docs/design.md:419-429`
- `docs/design.md:552-571`

Runtime fallback parsing hardcodes `T...` step IDs, specific model names, and
Terra as the default executor:

- `src/dispatcher/dispatch.py:165-181`
- `src/dispatcher/dispatch.py:210-215`

The roadmap also treats reference-project step IDs and its table format as normative:

- `docs/roadmap.md:62-87`

**Impact:** Adding a project with different step IDs, roles, plan layout, or
repository vocabulary requires core code changes.

**Recommendation:** Define a versioned generic plan intermediate representation.
Treat Markdown formats, including the Tier 2 format, as optional import adapters.
Resolve role and step vocabulary entirely from normalized project data.

### 9. Most declared policy is inert

`config/profiles.yaml` is never loaded. `policy`, mandatory reviews,
escalation, `prompt_sections`, cost caps, `max_parallel`, and `operator_gate`
are accepted but not enforced. Profile mode is merely shown to the supervisor:

- `src/dispatcher/loop.py:443-452`
- `config/projects/tier2-demo.yaml:75-176`

**Impact:** Configuration creates false assurance. Operators can believe a
budget, review rule, or human gate is active when it has no runtime effect.

**Recommendation:** Reject unsupported settings during configuration loading.
As features are implemented, compile configuration into concrete dispatcher
obligations before execution rather than relying on supervisor memory.

### 10. The dispatcher does not reliably identify the selected plan

`plan_file` and `roles_file` are neither required nor validated:

- `src/dispatcher/config.py:144-160`

The supervisor bootstrap receives directories but not the selected plan, roles
document, specification file list, parsed steps, escalation policy, or current
status:

- `templates/bootstrap_supervisor.md:5-20`
- `src/dispatcher/loop.py:443-452`

This contradicts the documented bootstrap contract:

- `docs/protocol.md:181-200`

**Impact:** In a directory containing multiple plans or role documents, the
supervisor must guess which source is authoritative.

**Recommendation:** Resolve and validate exact source files at startup, hash
them, parse them into the normalized plan, and include their identities and
hashes in the run record and supervisor bootstrap.

### 11. A partially completed project cannot be imported safely

This is directly relevant to the reference project, which is already complete
through a later historical checkpoint. State declares a `steps` map but never populates it:

- `src/dispatcher/state.py:17-25`

There is no configuration or import process for accepted baseline steps,
evidence hashes, reviewed revisions, waivers, or a start cursor.

**Impact:** A new supervisor can repeat completed work or skip it based on
inference from the repository and evidence files. A future completion guard
would also lack an authoritative baseline.

**Recommendation:** Add a baseline import phase that records every prior step
as `ACCEPTED`, `WAIVED`, or `PENDING`, bound to repository revisions and
evidence hashes. Require operator approval of the imported baseline before
dispatching new work.

### 12. The documented strict envelope is invalid under its parser

Copyable examples include inline comments:

- `templates/bootstrap_supervisor.md:25-34`
- `docs/protocol.md:23-34`

The parser includes those comments in field values. The documented
`role: executor # executor | reviewer` therefore produces an unknown role. The
parser also searches anywhere in the answer, accepts unknown fields, and does
not strictly validate mode, step, prompt body, or boolean values:

- `src/dispatcher/dispatch.py:57-123`

Batch is advertised and parsed but silently skipped by the loop:

- `src/dispatcher/loop.py:185-187`

**Recommendation:** Use versioned JSON plus JSON Schema, or strict YAML parsed
by a real YAML parser. Invalid envelopes must be protocol errors rather than
natural-language fallback candidates. Remove unsupported fields and actions
until they are implemented.

### 13. Dependency and immutable-artifact semantics are absent

The proposed step model contains repository and review settings but no
`depends_on`, required inputs, produced outputs, resource locks, or completion
predicates:

- `docs/config-schema.md:114-123`
- `docs/design.md:419-429`

Review dispatches do not identify a base revision, head revision, patch hash,
worktree, or artifact hash:

- `docs/protocol.md:23-48`

**Impact:** Future parallel scheduling can violate cross-repository
producer-consumer dependencies. Reviewers can examine different or moving work
products, making independent and tie-break reviews unauditable.

**Recommendation:** Add dependency and resource graphs to the normalized plan.
Bind every executor result and review request to immutable repository and
artifact coordinates.

### 14. Session identity and authorization are unsafe

Strict routes require only a nonempty target:

- `src/dispatcher/dispatch.py:103-123`

Unknown targets launch with an empty model, and supervisor-provided session IDs
are passed directly to OpenCode:

- `src/dispatcher/loop.py:190-240`

`fork` is implemented as resume because `--fork` is never supplied. The
registry stores only one session per role:

- `src/dispatcher/state.py:87-106`

**Impact:** A malformed or compromised supervisor can select an unconfigured
target or resume an unrelated local OpenCode session. Same-role parallel tasks
and per-task rework cannot be represented correctly.

**Recommendation:** Resolve session IDs only from a project-owned registry.
Store first-class session records keyed by session ID with run, dispatch, step,
attempt, role, repository, parent, and lifecycle metadata.

### 15. Run lifecycle semantics are ambiguous

`--resume` changes only a log message; existing state determines whether the
dispatcher resumes regardless of the flag:

- `src/dispatcher/loop.py:34-99`

Completed and halted runs have no persisted terminal status. Operator questions
are not persisted before blocking, and returned terminal routes are mishandled
recursively:

- `src/dispatcher/loop.py:153-183`

**Recommendation:** Define explicit `NEW`, `RUNNING`, `WAITING_OPERATOR`,
`HALTED`, `FAILED`, `SUCCEEDED`, and `CANCELLED` run states with validated
transitions. Make fresh start, resume, and recovery separate commands or modes.

## Additional findings

### 16. No automated assurance exists

There is no automated test suite or CI workflow. The mock harness models the
obsolete OpenCode output format and has no assertions:

- `src/dispatcher/mock_harness.py:23-47`

The current defects in preflight, JSON decoding, permission application,
completion, and transcript persistence therefore ship undetected.

### 17. Configuration validation is incomplete and contradictory

Documentation states that configuration has no silent defaults, while runtime
logic supplies several defaults:

- `docs/config-schema.md:8-10`
- `src/dispatcher/config.py:144-160`
- `src/dispatcher/config.py:229-247`

Validation permits unknown keys and does not consistently validate types,
ranges, enums, role-key uniqueness, writable paths, or unsupported features.
Relative paths are resolved against the process working directory:

- `src/dispatcher/config.py:98-103`

### 18. Evidence tracking is insufficient

Evidence snapshots compare only filename sets and detect only newly created
files:

- `src/dispatcher/sessions.py:139-167`
- `src/dispatcher/sessions.py:248-253`

Modified or deleted evidence is omitted, while unrelated concurrent file
creation may be falsely attributed to an agent. Evidence is not bound to a
dispatch ID or content hash.

### 19. Audit and transcript durability are insufficient

Transcript filenames use whole-second timestamps and can overwrite each other:

- `src/dispatcher/state.py:109-126`

Audit records lack stable run, event, dispatch, attempt, correlation, and
artifact identifiers. Normal completion does not produce a terminal audit
event.

### 20. Credential and local-data exposure is broad

OpenCode subprocesses inherit the dispatcher's entire environment, including
credentials checked during preflight. State, sessions, transcripts, and
generated permission files are created without explicit restrictive modes.
Prompt prefixes are also logged at debug level:

- `src/dispatcher/sessions.py:135-153`
- `src/dispatcher/state.py:49-75`

### 21. Process containment is incomplete

`subprocess.run()` captures unbounded stdout and stderr in memory and manages
only the direct OpenCode process:

- `src/dispatcher/sessions.py:147-157`

Tool subprocess descendants may survive a timeout and continue mutating a
repository or external system.

### 22. Project maturity documentation is stale

The README says no implementation exists, while a runnable package is present:

- `README.md:53-56`

The roadmap says code must not exist before external review:

- `docs/roadmap.md:20-22`

The repository needs an explicit maturity matrix distinguishing implemented,
partially implemented, and design-only capabilities.

## Sound design elements

The following choices should be retained while correcting the issues above:

1. A centralized hub-and-spoke topology is simpler and more auditable than a
   peer-to-peer agent graph for this workflow.
2. Keeping OpenCode as the execution harness avoids duplicating model, tool,
   and session functionality.
3. Isolating CLI interaction in `sessions.py` provides the right adapter
   boundary, although the adapter needs contract tests and versioning.
4. YAML project configuration is appropriate if it is backed by a strict,
   versioned schema and unsupported fields fail closed.
5. Audit-before-run is a useful primitive once paired with correlated response
   events and startup reconciliation.
6. Argument-array subprocess invocation avoids direct shell-string injection.
7. `yaml.safe_load()` is used for configuration loading.

## Recommended correction order

### Phase A: Clarify scope and maturity

1. Label the current implementation as a non-mutating proof of concept.
2. Remove or reject configuration for features that are not implemented.
3. Reconcile README, design, protocol, roadmap, and actual source status.
4. Decide explicitly which invariants belong to the supervisor and which must
   be enforced by the dispatcher. Completion, authorization, review
   obligations, and recovery must belong to the dispatcher.

### Phase B: Define generic contracts

1. Define a versioned project and plan intermediate representation.
2. Add repository identities, steps, dependencies, resources, authorization,
   evidence, acceptance criteria, review obligations, and baseline status.
3. Define versioned supervisor command, executor result, reviewer verdict, and
   operator decision schemas.
4. Define the complete run and step transition tables.
5. Treat the legacy Markdown format as one import adapter and example fixture, not the
   dispatcher-native format.

### Phase C: Establish runtime safety

1. Repair and pin the OpenCode adapter.
2. Apply permission configurations mechanically and fail closed.
3. Validate targets, repositories, sessions, modes, and plan steps before
   subprocess creation.
4. Reject failed or malformed subprocess outcomes without advancing state.
5. Introduce transactional state, process locking, correlation IDs, and
   idempotent recovery.
6. Use a minimal child environment and restrictive state-file permissions.

### Phase D: Implement workflow correctness

1. Import and approve each existing historical step independently.
2. Implement dispatcher-owned completion guards.
3. Implement typed executor and reviewer outcomes.
4. Bind execution and reviews to immutable revisions and artifact hashes.
5. Implement review profiles and escalation only after acceptance transitions
   are deterministic.
6. Implement parallelism only after dependency and resource scheduling exists.

### Phase E: Add assurance

1. Add captured OpenCode JSONL contract fixtures.
2. Add permission allow, ask, and deny integration tests for every role class.
3. Add parser and schema conformance tests.
4. Add state-transition and premature-completion tests.
5. Add crash-window and non-idempotent recovery tests.
6. Add concurrent-start and process-locking tests.
7. Add subprocess timeout and process-tree cleanup tests.
8. Add evidence create, modify, delete, and attribution tests.
9. Add clean-wheel installation and alternate-working-directory tests.
10. Gate changes with CI linting, typing, tests, and package checks.

## Verification performed

The review performed the following read-only checks:

- confirmed the installed OpenCode version is `1.18.11`;
- confirmed the dispatcher CLI help loads;
- reproduced the default preflight `NameError`;
- reproduced failure to parse the documented commented envelope; and
- reproduced empty decoding of a representative OpenCode 1.18.11 JSON event.

No model-backed sessions, repository mutations, deployments, or infrastructure
operations were executed during this review.
