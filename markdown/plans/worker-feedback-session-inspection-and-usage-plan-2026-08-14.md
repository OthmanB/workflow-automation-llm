# Worker Feedback, Session Inspection, and Usage Accuracy Plan

**Date:** 2026-08-14
**Status:** Proposed implementation plan; owner GO required
**Prerequisites:** T2.3a completed; dispatcher-owned verification and structured Git active

## Purpose

Resolve the issues found while manually inspecting the T2.3a real-operation
state without weakening the dispatcher trust boundary:

1. remove stale executor-contract wording;
2. improve the minimum semantic quality of supervisor dispatch prompts while
   retaining dispatcher-generated machine context;
3. document how an operator inspects isolated OpenCode sessions and expose the
   required locations in run reports;
4. restore a bounded edit-check-fix loop for executors through dispatcher-owned
   verification feedback;
5. clarify reviewer verification behavior without granting raw shell execution;
6. correct or explicitly qualify incomplete model-usage and cost reporting; and
7. document safe cleanup of legacy proof-of-concept state.

The changes must preserve exact writable paths, dispatcher-owned commits,
network-denied checks, immutable review targets, and typed workflow state.

## Findings

### Contract wording

Single supervisor commands correctly use `protocol_version: 1`, while the
normalized plan and project configuration use schema version 2. The T2.3a
supervisor task nevertheless asked the executor for a "schema-v1 executor
result" even though executors now return `dispatcher.executor_proposal.v2`.
The later dispatcher-generated worker context supplied the correct contract, so
the run succeeded, but the contradictory instruction is unnecessary risk.

### Prompt context

Supervisor responses are compact routing commands, not complete worker prompts.
The dispatcher expanded the T2.3a commands into approximately 12 KB of executor
context and 16 KB of reviewer context containing exact paths, criteria,
evidence, schemas, repository coordinates, and verification records.

This machine context is sufficient for capable models when the repository plans
and specifications are strong, but the bootstrap currently encourages short
turns without requiring the supervisor's task field to preserve the most
important semantic constraints. The supervisor remains valuable for routing,
rework, escalation, and completion decisions, but the most capable model is not
automatically necessary for a deterministic first dispatch. Model selection
should be based on schema adherence and decision quality rather than prompt
length alone.

### Session inspection

Worker OpenCode sessions live in isolated per-run, per-role HOME/XDG trees under
`state.directory/opencode-dispatches/...`. A normal `opencode -s <id>` searches
the operator's ordinary OpenCode database and therefore cannot find them. The
current run report shows a session ID but not its role state root, working
directory, event logs, or an export/reopen command.

### Verification loop

Executors cannot run test runners, linters, type checkers, or arbitrary Bash.
The dispatcher runs plan-owned checks in a disposable, network-denied
workspace. This preserves authority and isolation, but a failed check currently
fails/blocks the dispatch and normally requires operator reconciliation instead
of returning diagnostics to the same executor for correction.

OpenCode command patterns alone are not a sufficient remedy. `pytest`, Ruff,
and MyPy can execute repository code or plugins, write files, accept output
paths, and use shell redirection. OpenCode permissions are defense in depth, not
an OS sandbox. Directly allowing those commands in the provider-connected
OpenCode process would reopen a previously documented trust-boundary defect.

### Usage accounting

The T2.3a run report records only the successfully adopted Kimi invocation,
while the isolated OpenCode session database contains greater cumulative usage
and cost from the interrupted/recovered session. The report label "Measured
Usage" does not currently communicate this limitation.

### Legacy state

`state/tier2-local-agent-gateway/` contains August 9 proof-of-concept JSON,
Markdown, and mock-session artifacts. It is not authoritative and is unrelated
to the current SQLite-backed T2.3a run. The top-level `state/` convention itself
remains valid for configured projects and should not be declared obsolete.

## Decisions

### Preserve capable workers for current Tier 2 execution

Until the feedback loop and prompt-quality checks below are proven in a real
disposable run, Tier 2 executor and reviewer roles should remain on the current
capable model class (for example Luna-equivalent or stronger models such as
Terra, DeepSeek, Kimi, and the other currently approved top-tier roles).

Do not hardcode a model-quality tier in dispatcher logic. Role/model selection
remains configuration. Document that supervisor selection prioritizes strict
command conformance, state-sensitive routing, and rework quality; a cheaper
supervisor may be used only after it passes the existing deterministic command
fixtures and a bounded smoke scenario.

### Keep authoritative verification outside model sessions

Do not add direct executor or reviewer Bash allows for `pytest`, Ruff, MyPy,
interpreters, or arbitrary commands. Reuse the existing structured checks and
isolation backend.

Restore the practical test loop as a dispatcher-mediated cycle:

1. executor edits authorized paths and returns proposal-v2;
2. dispatcher validates the repository snapshot and runs the plan checks;
3. passing checks continue to evidence derivation and structured commit;
4. failing checks are persisted with bounded redacted diagnostics;
5. dispatcher resumes the same logical executor session with exact failure
   feedback and the same writable scope;
6. executor performs a bounded rework proposal; and
7. dispatcher reruns every authoritative check from a fresh disposable copy.

The model never supplies or rewrites command argv. A retry cannot expand path,
repository, network, Git, or tool authority.

### Keep reviewers read-only

Reviewers continue to inspect immutable code, evidence, and executor
verification records. An accepted reviewer result already causes the dispatcher
to run the plan checks again before acceptance. Document this explicitly.

Do not grant reviewers direct test execution in this plan. If reviewer-specific
diagnostics beyond the approved criteria become necessary, add them later as
named dispatcher-owned checks or a typed check-request contract after a
separate design review. Reviewer shell access is not required to achieve an
independent verification rerun.

## Scope

### Included

- supervisor bootstrap and dispatch-example wording;
- exact executor proposal-v2 terminology everywhere;
- concise semantic requirements for supervisor task prompts;
- operator session-inspection quickstart;
- run-report role, working-directory, private state-root, and event-log
  references;
- dispatcher-mediated executor verification feedback and bounded rework;
- explicit reviewer verification-rerun documentation;
- complete or clearly qualified per-invocation usage accounting;
- legacy-state cleanup guidance;
- focused unit, integration, recovery, and disposable tests.

### Excluded

- direct model-owned Bash verification;
- arbitrary worker-supplied commands or command arguments;
- weakening `network_policy: deny`;
- model-owned commits, pushes, branches, evidence hashes, or result revisions;
- changing the accepted T2.3a repository commit or historical run state;
- automatically deleting any state directory;
- hardcoding Luna, Terra, DeepSeek, Kimi, or another provider/model;
- a broad prompt DSL or another general-purpose permission system;
- reviewer-authored or reviewer-selected test commands.

## Implementation

### 1. Correct protocol and prompt terminology

Update supervisor examples and generated task guidance to distinguish these
contracts explicitly:

- supervisor single dispatch: `supervisor-command-v1` / `protocol_version: 1`;
- executor response: `dispatcher.executor_proposal.v2`;
- dispatcher materialization: `dispatcher.executor_result.v1`; and
- reviewer response: `dispatcher.reviewer_result.v1`.

Remove every instruction that asks an executor for a generic "schema-v1
result". Keep the exact contract literal in the generated prompt and response
schema.

Add parsed-field tests proving the supervisor example and executor worker prompt
agree on proposal-v2. Do not add complete prompt snapshots.

### 2. Improve the supervisor task-quality floor

Revise the supervisor bootstrap so a dispatch task remains concise but must
identify:

- the exact bounded step and intended outcome;
- normative source files or source IDs;
- the highest-value preservation/non-action constraints;
- the expected implementation/evidence category; and
- any known failure or ambiguity that materially changes the work.

The supervisor must not duplicate dispatcher-owned paths, command argv, hashes,
permissions, session IDs, or response schemas. Those remain generated from
durable state.

Document the role split: the supervisor supplies semantic intent and routing;
the dispatcher supplies exact machine constraints. Add deterministic tests for
the generated bootstrap fields and examples, not subjective prose scoring.

Add a short model-selection note to the operations guide. A high-end supervisor
is useful for ambiguous routing and rework, but straightforward schema-valid
dispatches may use a less expensive configured model after compatibility
validation. No automatic model downgrade is introduced.

### 3. Add a session-inspection quickstart

Create `docs/session-inspection.md` and link it from `README.md` and
`docs/operations.md`.

Document:

- how `worker_opencode_state_dir()` derives executor and reviewer roots;
- the separate supervisor state root;
- the required isolated `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`,
  `XDG_DATA_HOME`, and `XDG_STATE_HOME` values;
- exact `opencode --pure export <session-id>` examples;
- exact TUI reopening examples with the repository working directory;
- session-ID case sensitivity and copy/paste validation;
- the difference between dispatcher transcripts, worker JSONL event logs,
  OpenCode's private database, and the authoritative dispatcher database;
- the risk that opening a TUI or sending a message can update session metadata;
  and
- the rule never to publish private `auth.json`, OpenCode databases, or raw
  unsanitized exports.

Extend run reports with a `Role` column and a `Session Inspection` section. Use
state-directory-relative paths where possible. Include export instructions but
do not embed credentials, permission JSON, prompt text, or shell-expanded
secrets.

### 4. Persist verification failures as controlled feedback

Refactor the completed-proposal path so a plan-check failure is a typed,
recoverable workflow result rather than a generic worker-boundary exception.

Persist, atomically where required:

- the exact executor proposal digest;
- the inspected repository snapshot before verification;
- every authoritative check result, including failures;
- bounded redacted stdout/stderr summaries and transcript hashes;
- the executor dispatch/session identity;
- retry count and the next permitted transition; and
- confirmation that no structured commit occurred.

Use existing normalized retry limits. A failed check may enter a dedicated
rework-ready state only when:

- the worker process exited normally with a valid completed proposal;
- repository inspection proves all writes remain within the approved scope;
- no Git mutation or unexpected external write is observed;
- every check was launched from plan-owned argv under the configured backend;
  and
- the executor retry budget remains.

Otherwise retain the current fail-closed reconciliation path.

### 5. Resume the executor with bounded failure context

Generate a deterministic rework prompt containing:

- original step/task identity;
- unchanged repository and writable paths;
- failed criterion IDs in plan order;
- exit code, timeout, truncation status, backend, and redacted bounded summary;
- verification transcript hashes;
- explicit instruction not to run or substitute checks; and
- the same proposal-v2 schema and evidence requirements.

Resume the same logical executor session when its private state and repository
identity still validate. If the session is unavailable or foreign, stop for
operator reconciliation rather than silently starting a context-free retry.

Each proposal reruns the full authoritative check set, not only previously
failed checks. Commit only after all checks pass against the exact final
snapshot.

### 6. Clarify reviewer verification

Keep reviewer OpenCode permissions read-only. Ensure reviewer prompts and the
run report show:

- executor-time authoritative verification;
- the immutable commit/evidence target;
- whether the post-review dispatcher rerun passed; and
- both transcript hashes when executor-time and acceptance-time checks are
  separate executions.

If the reviewer returns accepted and the fresh dispatcher rerun fails, persist
the failed acceptance-time verification and route the step back to bounded
executor rework rather than treating the reviewer as a failed external process.

### 7. Make usage accounting invocation-complete

First define the reporting contract:

- usage is incremental per OpenCode invocation, not merely the final adopted
  response and not an ambiguous cumulative session snapshot;
- supervisor, executor, reviewer, resumed, retried, malformed-result, failed,
  and recovery invocations are included when OpenCode returned usage data;
- each invocation is counted exactly once; and
- missing provider usage is shown as unknown, never silently zero.

Persist an idempotent invocation identity before result application can fail.
Prefer a small append-only invocation-usage table or equivalent immutable row
over repeatedly mutating aggregate counters. Derive run/step/role/session totals
from those rows or update aggregates transactionally with a uniqueness guard.

Update reports to distinguish:

- dispatcher-accounted invocation usage;
- OpenCode cumulative session metadata when available; and
- any known unaccounted delta.

This is the final, low-priority implementation phase and may be delivered after
the correctness and session-inspection phases, but the report must not continue
to imply complete cost accounting if only adopted results are counted.

### 8. Document legacy-state cleanup

Add an operations/migration note stating:

- legacy `state.json`, `sessions.json`, old Markdown transcripts, and mock JSONL
  audit files are not authoritative;
- a specific legacy project subtree may be archived or removed after confirming
  no active configuration points to it;
- the top-level `state/` convention remains valid and may be recreated; and
- `dispatcher.sqlite3` must never be manually removed as part of this cleanup.

Do not implement automatic deletion.

## State and Contract Impact

The terminology and documentation phases require no state migration.

The verification-feedback phase should reuse existing plan checks,
`AuthoritativeVerification`, logical sessions, retry limits, and structured Git
records. Before implementation, decide whether a new step/dispatch status is
actually necessary. Prefer an existing transition plus an explicit typed
verification-feedback record when it remains unambiguous and recoverable.

If a new persisted field, table, or enum is required:

- bump the SQLite schema once for the complete feedback/usage work;
- provide forward migration from current schema v7;
- reject downgrade/open with older dispatcher versions as today;
- add crash-boundary tests; and
- do not add a compatibility fallback that drops verification failures or usage.

Do not bump project configuration or normalized-plan schema solely for this
work unless a persisted contract cannot represent the required state safely.

## Expected Files

Likely implementation surface:

- `src/dispatcher/sequential.py`
- `src/dispatcher/execution.py`
- `src/dispatcher/state_store.py`
- `src/dispatcher/workflow.py` only if a new state/record is necessary
- `src/dispatcher/sessions.py`
- `src/dispatcher/permissions.py` only to assert test-runner denial remains
- `src/dispatcher/results.py` only if a typed feedback contract is necessary
- `src/dispatcher/observability.py` or report helpers
- `docs/session-inspection.md` (new)
- `docs/operations.md`
- `docs/protocol.md`
- `docs/migration.md`
- `README.md`
- focused unit, integration, recovery, and live-disposable fixtures
- generated schemas only if a public contract changes

## Test Matrix

### Terminology and prompts

1. single supervisor dispatch remains protocol v1;
2. executor task/example requests proposal-v2, never schema-v1 result;
3. reviewer task/example requests reviewer-result-v1;
4. generated executor context retains exact paths, evidence, criteria, and
   proposal schema;
5. bootstrap requires bounded semantic intent without duplicating authority.

### Session inspection and reports

6. sequential executor/reviewer state roots match
   `worker_opencode_state_dir()`;
7. report rows include role, working directory, and state-relative session root;
8. generated export/reopen examples quote paths and IDs safely;
9. report never includes auth contents or inline permission configuration;
10. documentation covers supervisor, executor, reviewer, export, and TUI cases.

### Executor verification feedback

11. passing proposal follows the existing verify/commit/review path;
12. failed pytest criterion persists exact failed authority and no commit;
13. failed Ruff criterion and failed MyPy criterion use the same plan-owned
    runner without granting worker Bash;
14. rework prompt includes ordered bounded diagnostics and unchanged scope;
15. same valid logical session resumes;
16. missing/foreign session requires reconciliation;
17. out-of-scope write or Git mutation requires reconciliation;
18. rework reruns the complete check set;
19. retry exhaustion follows normalized plan policy;
20. crash before/after failed-check persistence recovers exactly once;
21. passing rework commits only the final validated tree.

### Reviewer behavior

22. reviewer remains unable to invoke Bash test runners;
23. reviewer receives executor-time authoritative records;
24. accepted review triggers a fresh dispatcher check;
25. failed post-review rerun enters bounded executor rework with no acceptance;
26. report distinguishes executor-time and acceptance-time verification.

### Usage

27. successful invocation counts once;
28. malformed-result and failed-application invocation usage remains counted;
29. resumed session increments rather than replacing prior usage;
30. recovery cannot double count an invocation;
31. supervisor, executor, and reviewer totals reconcile to run totals;
32. missing usage renders unknown/unavailable rather than zero;
33. cumulative OpenCode/session discrepancy is surfaced when detectable.

### Regression

34. full non-live suite passes;
35. Ruff and MyPy pass;
36. schema generation is unchanged or intentionally regenerated;
37. a disposable real-operation fixture proves one forced test failure, bounded
    same-session correction, passing rerun, structured commit, review, and
    completion with network denied.

## Implementation Order

1. Fix stale terminology and add focused prompt tests.
2. Add session-inspection documentation and report paths.
3. Document supervisor model-selection and semantic task requirements.
4. Add failed-verification persistence without changing retry behavior.
5. Add bounded same-session executor rework and recovery tests.
6. Route failed post-review reruns through the same feedback mechanism.
7. Run one disposable failure-then-repair operation.
8. Implement invocation-complete usage accounting and report qualification.
9. Add legacy-state cleanup guidance.
10. Run complete verification and produce an implementation report.

Phases 1-3 are small. Phases 4-6 are a workflow/recovery change and must not be
treated as a permission-list edit. Phase 8 is independently deliverable and low
priority.

## Acceptance Criteria

This plan is complete only when:

- no executor-facing instruction requests a schema-v1 executor result;
- worker prompts preserve exact machine constraints and a useful semantic task;
- an operator can export or inspect every reported session using documented
  state-relative information;
- executors receive bounded failed-check feedback and can correct work without
  raw test-runner authority;
- every final acceptance check remains dispatcher-owned and network denied;
- reviewers remain read-only and receive both immutable code and authoritative
  check records;
- verification failure, retry, crash, and session-loss paths are durable and
  deterministic;
- usage reports are complete per invocation or explicitly disclose gaps; and
- legacy mock state can be identified and cleaned without touching SQLite
  authority.

## Execution Gate

Do not implement this plan until the owner gives an explicit GO.

On GO, begin with terminology and session inspection. Before implementing the
verification-feedback state transition, re-read the current workflow/recovery
tests and confirm the smallest durable representation. Stop and request a
decision if safe same-session dirty-worktree rework requires destructive Git
cleanup, broad worker shell access, or weakening the current isolation claims.
