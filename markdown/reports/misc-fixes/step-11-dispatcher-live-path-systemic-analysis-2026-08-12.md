# Step 11 — Dispatcher Live-Path Systemic Analysis

**Analysis date:** 2026-08-12
**Scope:** End-to-end trace of the four live disposable real-operation
scenarios (`tests/live/test_real_operation_disposable.py`), from plan
acceptance criteria through supervisor command, dispatch preparation,
OpenCode child environment/session storage, model prompt/response schema,
JSONL decoding, strict result parsing, identity/evidence/repository
validation, durable state transition, batch joining/retry/reconciliation,
completion obligations, and the live-test harness itself.
**Reviewed at:** commit `72a4dea` (branch `main`, 7 commits ahead of
`origin/main`) plus the current uncommitted worktree (`git status` shows
modifications to `cli.py`, `config.py`, `execution.py`, `importers.py`,
`operation.py`, `plan.py`, `preflight.py`, `results.py`, `sequential.py`,
`sessions.py`, `state_store.py`, `workflow.py`, docs, schemas, and tests;
untracked `src/dispatcher/yaml_io.py`, two new OpenCode fixture JSONL files,
`tests/integration/test_execute_command_disposable.py`,
`tests/unit/test_execution.py`, and the whole `markdown/reports/misc-fixes/`
directory).
**Method:** Independent read-only review. No files modified other than this
report, no commits, no branches, no `dispatcher execute`, no OpenCode
invocation, no credentials, no `config/projects/local/`, `config/state/`, or
repository-root `state/` inspected. Findings are backed by direct file:line
citations verified against source, by running
`.venv/bin/python -m pytest tests -q -m "not live_opencode"` →
**269 passed, 0 failed, 5 deselected**, and by direct inspection of the
disposable fixture's SQLite state (`dispatcher.sqlite3`: `runs`, `dispatches`,
`batches` (embedded in `runs.record_json`), `leases`, `audit_events`,
`operator_decisions`) and raw OpenCode JSONL/stderr transcripts under
`/private/.../pytest-865/test_real_cross_repository_dis0/`. Markdown reports
in `markdown/reports/misc-fixes/` and the readiness review in
`markdown/reviews/` were treated as claims only and independently
re-verified against code, tests, and the disposable artifacts.

---

# Systemic Verdict

**A combination, but with one dominant, previously-unflagged architectural
defect that the six-item failure history had not yet reached.**

- Failures **#1–#6** in the given history are **independent, genuinely
  closed defects**. Each was a distinct, narrow contract-parsing or
  session-identity bug (final-text-event selection, verdict vocabulary,
  finding schema, iterator exhaustion, OpenCode-home instability). All six
  fixes are present in the current worktree, are covered by non-live tests
  that fail without the fix and pass with it, and are independently
  verifiable by direct code reading (see Failure Timeline). These are not
  symptoms of one shared root cause — they are six different, correctly
  identified and correctly closed bugs in six different layers of the
  pipeline (JSONL decoding, schema vocabulary, schema completeness,
  supervisor-harness statefulness, session-home addressing).

- Failure **#7** (the still-open cross-repository batch failure) is where
  the pattern changes. Its *proximate* cause (a dirty worktree from a
  self-invented `pytest` run leaving `__pycache__/*.pyc`) is a **test-fixture
  /prompt-completeness defect** and the dispatcher's rejection of it is
  **correct, intentional fail-closed behavior** (`commit_policy="required"`
  correctly rejecting a non-clean snapshot). That part is not a production
  defect. **However**, tracing what happens *after* that rejection surfaces
  the deepest and most consequential problem the whole exercise was
  building toward:

  **`answer_operator_request` (`src/dispatcher/state_store.py:889-1004`) has
  no handling for three of the nine documented `OperatorRequest.kind` values
  — `"reconciliation"`, `"batch_reconciliation"`, and
  `"workspace_reconciliation"`.** These are exactly the three kinds raised by
  `fail_dispatch` and `prepare_workspace_batch`/`finalize_batch` whenever a
  worker boundary fails with an exception (repository-validation failure,
  adapter error, or any bug) — i.e., exactly the failure class this whole
  step-11 exercise is meant to prove is recoverable. Because these three
  `kind`s fall through the `elif` chain, the operator's chosen answer
  (`"reconcile"` **or** `"halt"`) is silently ignored: `target` stays fixed at
  `request.resume_to` (always `RunStatus.RUNNING` for these three kinds,
  never `HALTED`), and the underlying `BLOCKED` step is never transitioned
  back to `READY` (contrast with `stall_recovery`'s `"retry"` answer, which
  explicitly does `transition_step(step, StepStatus.READY, event)` at
  `state_store.py:990-995`). The practical consequence: **once any
  exception-driven dispatch failure occurs (solo or batch), the documented
  `dispatcher answer <run> <request-id> reconcile|halt` recovery path is a
  no-op that cannot un-stick the step and cannot actually halt the run.**
  The run resumes to `RUNNING` forever with a permanently `BLOCKED` step, no
  CLI `halt` command exists as an alternative, and `completion_obligations`
  will forever report `step_not_accepted` for that step — the run can never
  reach `SUCCEEDED` or `HALTED` through any currently-documented operator
  action.

- This one defect explains, in one mechanism, why the sequence of fixes
  kept surfacing *new* terminal failures instead of a smooth path to
  "all four scenarios pass": **every fix so far addressed how the
  dispatcher validates and records a worker's typed result — none of them
  touched what happens when that validation itself fails.** Combined with
  two smaller, related gaps in the same "post-execution" seam —
  (a) `fail_dispatch` discarding the actual exception message
  (`execution.py:262,272`: only `type(exc).__name__` is kept) and
  (b) executor-reported `verification[]` check statuses (`failed`/`skipped`)
  never being enforced anywhere before a step is marked `ACCEPTED`
  (`results.py`, `sequential.py:864-884`, `workflow.py:703-783`) — the
  systemic picture is: **the dispatcher's structural/identity validation
  layer (schema, snapshot identity, evidence hashing) is mature and strict;
  the operational-recovery and content-quality-enforcement layer built
  around it is materially incomplete.** This is one architectural
  immaturity with three visible symptoms, not three unrelated bugs.

- Separately and independently, the live-test harness itself has a
  **representativeness gap** (single `DISPATCHER_LIVE_MODEL` for every role;
  the four scenarios never invoke the actual `dispatcher execute` CLI/audit
  path) that limits what "4 scenarios pass" can ever prove, regardless of
  the above fixes.

**Conclusion:** proceed to remediate the reconciliation-dispatch gap and the
verification-enforcement gap as the two mandatory blockers before any further
live proof attempt; treat the cross-repository batch failure's proximate
trigger as a fixture/prompt fix, not a dispatcher fix; do not weaken
`commit_policy="required"`.

---

# Mandatory Findings

## M1 — Operator reconciliation answers do not resolve the failure they are asked to reconcile, and silently ignore "halt"

- **Severity:** Blocker (production, safety- and liveness-critical)
- **File/line:** `src/dispatcher/state_store.py:889-1004`
  (`answer_operator_request`), specifically the unconditional
  `updated_record = record` / `target = request.resume_to` initialization at
  lines 924-925 and the terminal `updated = transition_run(updated_record,
  target, event)` at line 1004, with no `elif` branch for
  `request.kind in {"reconciliation", "batch_reconciliation",
  "workspace_reconciliation"}`. Compare the four `kind`s that *are* handled:
  `risk_gate` (926), `escalation` (943), `review_waiver` (966),
  `stall_recovery` (988). `OperatorRequest.kind`'s full Literal (nine values)
  is defined at `src/dispatcher/workflow.py:196-204`.
- **Direct evidence:**
  - Static: reading `answer_operator_request` end-to-end (lines 915-1004)
    shows no code path sets `target = RunStatus.HALTED` or mutates
    `updated_record.steps[...]` for these three kinds, regardless of which
    of the request's own `allowed_answers` (`["reconcile", "halt"]`,
    `sequential.py:1177,1246,1432`) the operator supplies.
  - Dynamic (disposable fixture): the SQLite run record for
    `real-disposable-project` shows `state: WAITING_OPERATOR`,
    `operator_request.kind: "batch_reconciliation"`,
    `operator_request.allowed_answers: ["reconcile", "halt"]`, and step
    `prepare-fixture` at `state: BLOCKED` (`steps` table / `record_json`).
    `operator_decisions` table has 0 rows (no answer has been given yet in
    this artifact, so this specific run has not hit the bug — but the
    static code path proves it would, and no code anywhere else in the
    repository performs the missing transition).
  - Test coverage: `tests/integration/test_batch_execution.py:120-125` only
    asserts `operator_request.kind == "batch_reconciliation"` and that the
    batch/dispatches are `FAILED` — it never calls
    `answer_operator_request(answer="reconcile"|"halt", ...)` and inspects
    the resulting step/run state. A repo-wide grep of `tests/` for
    `"reconciliation"` / `"batch_reconciliation"` / `"workspace_reconciliation"`
    combined with `answer_operator_request` returns **zero** matches for any
    of the three kinds.
- **Root cause:** `OperatorRequest.kind` was extended to nine literal values
  (`workflow.py:196-204`) as reconciliation/batch/workspace features were
  added, but `answer_operator_request`'s dispatch table
  (`state_store.py:926-1002`) was never extended to match — an additive
  feature drift where the *producer* of a new `kind` was built without a
  corresponding *consumer* branch, and nothing enforces exhaustiveness (no
  `else: raise` guard on unrecognized/unhandled kinds).
- **Production vs. test-only:** **Production defect.** This is the only
  documented recovery mechanism (`dispatcher answer`, `cli.py:646-666`) for
  the exact failure class (`fail_dispatch`-triggered `BLOCKED` steps, solo
  or batched) that real T2.2a operation must be able to survive.
- **Smallest safe remediation:** add three `elif` branches to
  `answer_operator_request` mirroring the existing `stall_recovery` pattern:
  - `"reconciliation"` / `"batch_reconciliation"`: on `"reconcile"`,
    transition the request's step(s) from `BLOCKED` to `READY` (for
    `batch_reconciliation`, iterate `record.batches[context_ref]
    .failed_dispatch_ids` → their `step_id`s); on `"halt"`, set
    `target = RunStatus.HALTED`.
  - `"workspace_reconciliation"`: on `"reconcile"`, require (and record) that
    the operator has completed the workspace cleanup contract before
    resuming (at minimum, re-verify workspace state via the existing
    `WorkspaceCoordinator` before setting `RUNNING`); on `"halt"`,
    `HALTED`.
  - Add a final `else: raise StateStoreCorruptionError(f"no answer handling
    for operator request kind {request.kind!r}")` so any future new `kind`
    fails loudly instead of silently no-op-ing.
- **Exact tests/proof required:**
  - Unit tests (`tests/fault_injection/test_state_store.py` or
    `tests/unit/test_sequential.py`) for each of the three kinds: answering
    `"reconcile"` returns the step to `READY` (solo and batch) and the run to
    `RUNNING`; answering `"halt"` transitions the run to `HALTED` and leaves
    the step `BLOCKED`/terminal.
  - Extend `tests/integration/test_batch_execution.py`'s existing
    `batch_reconciliation` test to call `answer_operator_request` and assert
    the failed child's step becomes `READY` and is re-dispatchable.
  - A live proof (see Live Evidence Plan) that deliberately forces a
    solo *and* a batch `fail_dispatch` (e.g., a fixture with a
    pre-existing untracked file), then answers `"reconcile"` through the
    real `dispatcher answer` CLI, and shows the run reaching `SUCCEEDED`.

## M2 — Executor-reported `verification[]` check outcomes are structurally valid but never enforced before ACCEPTED

- **Severity:** Blocker (production, release-blocking especially for
  no-review steps)
- **File/line:** `src/dispatcher/results.py:31-36` (`VerificationResult`,
  no cross-validator against `AcceptanceCriterion`); `src/dispatcher/plan.py:80-85`
  (`AcceptanceCriterion.criterion_id`, defined but never read back at
  result-processing time — only referenced for prompt serialization at
  `sequential.py:2164-2166`); `src/dispatcher/sequential.py:864-884`
  (`apply_executor_result`'s `ExecutorCompletedResult` branch, which decides
  `StepStatus.ACCEPTED` vs `REVIEW_REQUIRED` purely from
  `self._review_obligation(record, step).required`, never inspecting
  `result.verification`); `src/dispatcher/workflow.py:703-783`
  (`completion_obligations`, which checks evidence `artifact_id` coverage
  and `review_acceptances` counts but never `check_id`/`criterion_id`
  correspondence or verification `status`).
- **Direct evidence:** live disposable artifact
  (`.../executors/executor-terra-prepare-second/opencode-events/*.stdout.jsonl`):
  the model's own final JSON result contained
  `"verification":[{"check_id":"result-content","status":"passed",...},
  {"check_id":"working-tree","status":"passed",...},
  {"check_id":"pytest","status":"skipped","summary":"pytest . collected no
  tests."}]`, `"outcome":"completed"`. The dispatch is `FORWARDED` and step
  `prepare-second` is `ACCEPTED` in the run record — i.e. a step whose own
  self-reported verification includes a `skipped` check was accepted
  outright, because this plan step has `review.required = false` (the
  exact T2.2a-shaped configuration: no reviewer, so nothing downstream ever
  looks at `verification` either).
- **Root cause:** `VerificationResult` and `AcceptanceCriterion` were
  designed as a matched pair (`check_id` ↔ `criterion_id`,
  `results.py:34`/`plan.py:83`) but only the executor-facing *prompt*
  serializes both (`sequential.py:2164-2169`); no runtime consumer was ever
  written to read `result.verification` back and gate on it. This is a
  contract-vs-enforcement gap, not a parsing bug — every field is
  schema-valid; nothing downstream ever inspects the content.
- **Production vs. test-only:** **Production defect.** No test anywhere in
  `tests/` constructs an executor/reviewer result with a `failed` or
  `skipped` `VerificationResult` and asserts rejection or escalation — every
  fixture across `test_results.py`, `test_sequential.py`,
  `test_batch_execution.py`, `test_baseline_adoption.py`,
  `test_workspace_barrier.py` uses `"status": "passed"` exclusively,
  confirming this is an untested, not merely under-tested, code path.
- **Smallest safe remediation:** in `apply_executor_result` (and the
  reviewer path, `apply_reviewer_result`), before allowing the
  `ExecutorCompletedResult`/`ReviewerAcceptedResult` branch to proceed to
  `ACCEPTED`/`REVIEW_REQUIRED`, require: (a) `result.verification` is
  non-empty and every entry has `status == "passed"` (or matches an
  explicit, plan-declared allow-list of optionally-skippable `check_id`s —
  do **not** invent implicit leniency), independent of whether `review` is
  required; (b) optionally, cross-check `{v.check_id for v in
  result.verification}` covers `{c.criterion_id for c in
  step.acceptance_criteria}` exactly, matching the documented contract in
  `plan.py`. A `failed`/`skipped` verification should route through the
  same `on_failed` retry/escalate/halt policy already used for
  `ExecutorFailedResult` (`sequential.py:891-901`), not silently accept.
- **Exact tests/proof required:** unit test asserting an
  `ExecutorCompletedResult` with one `status: "skipped"` or `status:
  "failed"` verification entry is rejected/escalated on both the
  review-required and no-review paths; regenerate the disposable
  cross-repository fixture (with a deterministic pre-seeded test, see M3)
  and confirm the sibling would now correctly require attention instead of
  silently accepting a step whose own check reported "collected no tests."

## M3 — `fail_dispatch` discards the actual exception message; only `type(exc).__name__` survives to durable state

- **Severity:** High (observability; compounds M1's diagnosability problem)
- **File/line:** `src/dispatcher/execution.py:260-263` and `:270-273`
  (`reason=f"worker execution failed: {type(exc).__name__}"` in both the
  non-retryable `OpenCodeAdapterError` branch and the generic
  `except Exception` branch); `src/dispatcher/sequential.py:1189-1262`
  (`fail_dispatch`, which accepts only `reason: str` and never threads
  `failure_category`/`failure_detail` into `commit_dispatch_transition`,
  unlike `handle_stall` at `sequential.py:1278-1286`, which does pass
  `failure_category=category, failure_detail=reason`).
- **Direct evidence:** disposable fixture SQLite, `dispatches` table,
  `prepare-fixture` dispatch record: `"failure_category": null,
  "failure_detail": null, "last_event": {"reason": "worker execution
  failed: SequentialWorkflowError", ...}`. The real message —
  `"committing repository policy rejects uncommitted executor changes:
  ['__pycache__/test_result.cpython-312-pytest-9.0.3.pyc']"` — exists only
  in the raw OpenCode stdout JSONL transcript (recoverable only by an
  operator who knows to go find and parse that file by hand) and is
  irrecoverably lost from every durable-state view (`dispatcher status`,
  `dispatcher recover`, the exported run report).
- **Root cause:** `fail_dispatch` was designed as a generic "boundary
  failed" recorder before `handle_stall`'s richer
  `failure_category`/`failure_detail` fields existed on `DispatchRecord`
  (`workflow.py:250-251`); the richer fields were added for the
  retryable-stall path only and never backfilled onto the non-retryable
  path.
- **Production vs. test-only:** **Production defect** (durable-state
  observability, not a functional-correctness bug — the run does correctly
  fail closed; an operator just cannot diagnose *why* without external log
  archaeology).
- **Smallest safe remediation:** extend `fail_dispatch`'s signature to
  accept an optional `category: str | None` and pass
  `failure_detail=reason[:5000]` (matching `handle_stall`'s truncation
  pattern) into `commit_dispatch_transition`; change both `except` blocks in
  `execution.py` to pass `reason=f"worker execution failed: {redact_text(str(exc))}"`
  (bounded/redacted) instead of only the exception class name.
- **Exact tests/proof required:** unit test asserting
  `DispatchRecord.failure_detail` is populated (non-null, contains the
  redacted original message) after a `fail_dispatch` call triggered by a
  `RepositoryValidationError`/`SequentialWorkflowError`; a
  secret-redaction test proving credentials embedded in a hypothetical
  exception message are stripped before persistence (reuse the existing
  `redact_text`/`redact_value` machinery already proven elsewhere in
  `sessions.py`).

## M4 — `execute_batch`'s bare `except Exception: continue` performs no logging at all (module has no logger)

- **Severity:** Medium (observability; compounds M3)
- **File/line:** `src/dispatcher/execution.py:288-294`. Confirmed: `grep
  "^import logging"` / `"logging\."` across `execution.py` returns zero
  matches — the module never imports or calls the standard `logging`
  module.
- **Direct evidence:** the comment at line 293
  (`# The worker boundary persisted its own failed dispatch state before
  raising.`) documents the design intent but the actual `except Exception:
  continue` binds no name and calls nothing — a batch of, say, 20 children
  with 3 failures produces no operational log line distinguishing "3
  failed" from "20 succeeded" at the point of failure; the only trace is
  post-hoc SQL/API inspection of `finalize_batch`'s aggregate.
- **Root cause:** same design-time gap as M3 — the assumption that durable
  state alone is sufficient observability was true for state *inspection*
  but not for real-time operational monitoring (dashboards, alerting,
  `journald`/log-aggregation pipelines an operator would actually be
  watching during a live T2.2a run).
- **Production vs. test-only:** Production defect, observability-only (does
  not affect correctness of the durable state, which M1/M3 already cover).
- **Smallest safe remediation:** add a module-level `logger =
  logging.getLogger(__name__)` and log
  `logger.warning("batch child %s failed: %s", child.dispatch.dispatch_id,
  exc)` (or use `self.config.observability.log_level`-aware structured
  logging consistent with `observability.py`) in the `except Exception as
  exc:` branch, in addition to (not instead of) the M3 durable-state fix.
- **Exact tests/proof required:** a test using `caplog`/`pytest`'s logging
  capture asserting a log record is emitted per failed batch child,
  containing the dispatch id and a redacted failure summary.

---

# Non-Blocking Findings

## N1 — Fixture `test_result.py`/`__pycache__` non-determinism (the failure #7 trigger)

- **Severity:** Non-blocking for the dispatcher itself; blocking for
  *proving* T2.2a readiness with this exact fixture until fixed.
- **File/line:** `tests/live/test_real_operation_disposable.py` prompts
  (e.g. line 85-86: `"Run local verification and commit the changes."`);
  `src/dispatcher/sequential.py:2068-2209` (`_worker_prompt`, which contains
  zero mention of repository hygiene/cleanliness beyond the schema fields);
  disposable fixture repository has no `.gitignore` (confirmed:
  `ls -la` on the fixture repo shows no `.gitignore`; `git status` shows
  `__pycache__/` untracked with no matching ignore rule).
- **Direct evidence:** the model legitimately invented `test_result.py`,
  ran `pytest` (per its own `verification` entries and the transcript), and
  correctly committed `result.txt`/`evidence/real-evidence.md`/
  `test_result.py` in one commit — but running `pytest` *after* that commit
  (to "verify" per the prompt's instruction) left `__pycache__/*.pyc`
  behind as an untracked artifact never mentioned to the model as something
  it must avoid or clean up.
- **Why non-blocking for the dispatcher:** `commit_policy="required"`
  correctly rejected the dirty snapshot — this is exactly the fail-closed
  behavior the control is designed to provide (`repository.py:209-214`).
  Weakening this check (e.g., ignoring `__pycache__` at the dispatcher
  level) would blur the line between "repository is clean" and "repository
  is clean except for things we've decided not to look at," which is
  precisely the kind of control-erosion the task instructs against.
- **Recommended remediation (test/fixture side only):**
  1. Seed a `.gitignore` (or set `core.excludesFile`) in
     `create_fixture_project`/`_real_project`/`_initialize_repository`
     covering `__pycache__/`, `*.pyc`, `.pytest_cache/` for every disposable
     repository (including the dynamically created `sibling` repo).
  2. Set `PYTHONDONTWRITEBYTECODE=1` (and `PYTEST_ADDOPTS=-p no:cacheprovider`
     or `--no-cache-clear`/`-p no:cacheprovider`) in the child environment
     for disposable live scenarios specifically, so tooling invoked by the
     model does not leave bytecode/cache residue regardless of what the
     model does.
  3. Add one explicit sentence to the disposable prompts (not to the
     production `_worker_prompt` schema, which is deliberately
     plan/step-driven, not scenario-specific): "Ensure `git status
     --porcelain` is completely empty, including caches or bytecode
     produced by any verification you run, before returning the result."
  4. Prefer pre-seeding a deterministic `test_result.py` in the fixture
     rather than depending on model discretion to invent one — this
     directly serves the "is the fixture deterministic enough to prove
     readiness" question below.
- **Assessment of fixture determinism:** **Not yet deterministic enough.**
  Because the executor is free to invent its own test file, tooling, and
  verification `check_id`s, repeated live runs are not directly comparable
  (different tests, different `verification` entries, different residue
  risk each time), and — per M2 — nothing downstream would catch a
  materially weaker self-invented check anyway. Pre-seeding the test file
  (or at minimum, a fixed `evidence`/`verification` vocabulary) removes one
  axis of run-to-run variability that is orthogonal to what T2.2a readiness
  is actually trying to prove (dispatcher control correctness, not model
  test-authoring creativity).

## N2 — Batch atomicity is by-design "wait for started, forward independently, gate on completion" — not a bug

- **Severity:** Non-blocking; documented design, correctly implemented and
  tested.
- **File/line:** `src/dispatcher/config.py:190`, `workflow.py:299`
  (`failure_mode: Literal["wait_for_started"]` — the only supported value,
  documented in `docs/config-schema.md:47`); `sequential.py:1374-1458`
  (`finalize_batch`).
- **Evidence it is intentional and correct:** disposable artifact directly
  confirms the documented semantics: `prepare-second` (the sibling) reached
  `FORWARDED`/step `ACCEPTED` **before** `finalize_batch` ever ran (each
  child releases its own lease independently inside
  `apply_executor_result`/`fail_dispatch`, `sequential.py:928,1261`); the
  batch aggregate (`batches[batch_id].state`) is set to `FAILED` only as a
  bookkeeping/reporting flag plus a run-level `WAITING_OPERATOR` gate
  (`sequential.py:1417-1441`) — it does not, and structurally cannot
  (`BATCH_TRANSITIONS`, `workflow.py:148-153`, has no transition out of
  `JOINED`/`FAILED`), roll back the sibling's already-committed,
  already-forwarded result. Leases are per-dispatch-id
  (`_dispatch_lease_owner_id`, `sequential.py:1988-1989`), so the `DELETE
  ... WHERE owner_id = ?` release (`state_store.py:488-498`) cannot
  accidentally touch a sibling's lease; the disposable fixture's `leases`
  table is empty (0 rows), confirming clean release for both children.
- **Why this does not block one real T2.2a step:** T2.2a's plan (per the
  task) has no cross-repository batch requirement in its first single
  step; this finding is scoped to the *test coverage* of a design decision
  that is already correct, not to a defect that would affect a solo T2.2a
  dispatch.
- **Recommendation:** none for the dispatcher; document
  `failure_mode="wait_for_started"`'s independent-forwarding consequence
  explicitly in `docs/operations.md`'s batch section (currently the term
  "batch" appears only once, in a generic status-fields sentence) so
  operators are not surprised that a "failed batch" can contain already-
  accepted, already-forwarded siblings.

## N3 — `audit_events` table is fully wired but empty for any run that bypasses the `dispatcher execute` CLI

- **Severity:** Non-blocking for a single T2.2a step exercised through the
  real CLI; directly relevant to Live Evidence Plan below.
- **File/line:** `append_audit_event` (`state_store.py`) is called from
  exactly two sites in the whole codebase: `cli.py:296` (inside
  `_cmd_execute`, one audit record at operation start) and
  `state_store.py:369` (inside `request_dispatch_cancellation`). No call
  site exists in `sequential.py` for ordinary transitions (`fail_dispatch`,
  `apply_executor_result`, `finalize_batch`, `answer_operator_request`).
- **Direct evidence:** disposable fixture's `audit_events` table has 0 rows
  even though the run went through a full batch failure/`WAITING_OPERATOR`
  cycle — because the four live disposable scenarios construct
  `SequentialWorkflow`/`SequentialExecutionCoordinator` directly and never
  invoke `cli.py`/`_cmd_execute` (matching the readiness review's B2
  finding, independently reconfirmed here).
- **Why non-blocking:** the `RunRecord`'s own event log
  (`last_event`/`sequence` on every step/dispatch/batch) already carries
  enough information for the correctness findings above; `audit_events` is
  an *additional* CLI-operation-scoped audit trail, not the dispatcher's
  only observability mechanism.
- **Recommendation:** fold into the Live Evidence Plan's requirement to
  exercise the real `dispatcher execute` CLI path at least once (this was
  already B2 in the prior readiness review, addressed for the CLI's gates
  themselves via the new `tests/integration/test_execute_command_disposable.py`,
  but never combined with a real OpenCode invocation).

---

# Failure Timeline

| # | Observed live failure | Independently verified root cause | Fix location | Closed? |
|---|---|---|---|---|
| 1 | Multiple OpenCode text events concatenated; only the final text event was valid contract JSON | `OpenCodeJsonlDecoder.finish()` previously joined/used all `text` events instead of only the last one | `sessions.py:200-222`, specifically `self._chat_parts[-1] if self._chat_parts else ""` (line 218); regression-tested by `tests/fixtures/opencode/1.18.11/run-narration-then-result.jsonl` (untracked, new) and the corresponding decoder test asserting "only the last text event is used" | **Yes** — verified by direct code reading of `finish()` and by the fixture's narration-then-JSON structure being handled correctly per `tests/unit/test_sessions.py`'s `test_decoder_uses_only_the_last_text_event_as_chat_response`. |
| 2 | Luna passed all four scenarios (after fix #1) | N/A (positive result) | N/A | N/A |
| 3 | Terra reviewer returned unsupported verdict `"rejected"` | No enumerated/enforced vocabulary for `verdict` was communicated to the model | `results.py:191-198` (`REVIEWER_VERDICT_OPTIONS` derived from the discriminated union itself, not hand-maintained) + `sequential.py:2145-2163` (`verdict_options` and `final_response_check` embedded in the prompt) | **Yes** — the reviewer result schema now only accepts `accepted\|changes_requested\|blocked\|inconclusive` (`results.py:191-197`), and the prompt explicitly enumerates them; a model returning `"rejected"` would fail Pydantic validation before reaching any state transition, and the prompt now tells it the exact vocabulary up front. |
| 4 | Terra returned a malformed finding object (missing `finding_id`, unsupported severity `"medium"`, extra detail) | `ReviewFinding` schema existed but was not embedded/enumerated in the prompt, so the model guessed fields/values | `sequential.py:2150` (`response_json_schema` from `schema_documents()`) + `results.py:120-126` (`ReviewFinding.severity: Literal["info","warning","blocking"]`, no `"medium"`) | **Yes** — the full JSON Schema is now embedded verbatim in the prompt (`response_json_schema`), and `severity`'s enum excludes `"medium"`; a model reusing an unsupported severity now fails Pydantic validation with a specific field/value error rather than silently being accepted. |
| 5 | Fixed supervisor command iterator exhausted with `StopIteration` (pytest collected no tests → `changes_requested`) | The prior disposable-test harness pre-built a fixed sequence of supervisor commands and iterated it; any extra turn (e.g., an unplanned rework round) exhausted the iterator | `tests/live/test_real_operation_disposable.py:333-376` (`_decide_next_command`), which recomputes the next command **from the durable `RunRecord` state on every turn** — no iterator, no fixed sequence | **Yes** — confirmed by reading `_run_real_scenario`'s `supervisor_turn` closure (lines 241-253), which calls `_decide_next_command(current, ...)` freshly each turn from `store.load_run(run_id)`, and by the dedicated `test_reactive_supervisor_command_decisions` unit test (lines 496-651) exercising rework/exhaustion/batch branches without any iterator. |
| 6 | Rework attempted resume but failed with "Session not found" because each dispatch attempt got a new OpenCode HOME/XDG root | `worker_opencode_state_dir` previously keyed the OpenCode home on `dispatch.dispatch_id` (fresh UUID per attempt) instead of the stable `(pool, session_registry_key)` identity already used by the session *registry* | `execution.py:417-425` (`worker_opencode_state_dir` now calls `session_registry_identity(dispatch)`, `sequential.py:1963-1971`, the same helper `record_session_id` already used) | **Yes** — confirmed by `tests/unit/test_execution.py:58-83` (home path is identical across `attempt=1`/`attempt=2` for the same step/role; differs correctly across run/role/batch-step) and by `tests/integration/test_sequential_git_e2e.py:90-129` (both dispatches share `runtime_session_id`/`logical_session_key`/child environment; a resumed attempt's `requested_session` equals the prior attempt's `session_id`). |
| 7 | Sequential resume/rework passed, but a cross-repository batch child failed after returning a valid executor result | (a) **Proximate:** dirty worktree from a self-invented, uninstructed-to-clean `pytest` run (`__pycache__/*.pyc`), correctly rejected by `commit_policy="required"` (`repository.py:209-214`) — a fixture/prompt defect, not a dispatcher defect. (b) **Deeper, newly discovered here:** even once diagnosed, there is **no functioning operator recovery path** for this exact failure class — see **M1** — and the failure detail itself is discarded before it reaches durable state — see **M3**. (c) **Also newly discovered here:** nothing would have caught the sibling's own `verification: [{"status":"skipped"}]` even if the batch had fully succeeded — see **M2**. | Not yet fixed. Requires the Proposed Remediation Plan below (fixture hygiene for (a); `answer_operator_request` completeness for (b); verification enforcement for (c)). | **No** — still open; this report supersedes prior narrower "fix the fixture" framing with the M1/M2/M3 findings above. |

---

# Proposed Remediation Plan

Grouped by architectural concern, at most three cohesive steps (no
one-field-at-a-time patching).

## Step A — Close the operator-recovery loop (M1)

- **Exact scope:** Make every `OperatorRequest.kind` that can be raised
  actually resolvable through `answer_operator_request`, and make
  unhandled kinds fail loudly instead of silently. This is the single
  highest-priority change: without it, no exception-driven failure (solo or
  batched) can ever be recovered from or intentionally halted through the
  documented interface, which makes every other fix in this plan
  unverifiable end-to-end (a live run that hits any validation failure has
  no way to reach a terminal state for the operator to observe).
- **Files likely affected:** `src/dispatcher/state_store.py`
  (`answer_operator_request`), `src/dispatcher/workflow.py` (possibly a
  small helper to resolve a batch's `failed_dispatch_ids` → `step_id`s),
  `tests/fault_injection/test_state_store.py`,
  `tests/unit/test_sequential.py`, `tests/integration/test_batch_execution.py`.
- **Model recommendation:** this is precise, small-surface-area,
  state-machine-shaped work with clear existing patterns to mirror
  (`stall_recovery`'s handling is the template) — well-suited to a
  continuation session with the full `state_store.py`/`workflow.py` context
  already loaded, rather than a fresh session that would need to re-derive
  the `kind` inventory and the exact `stall_recovery` pattern from scratch.
- **Fresh or continuation session:** **Continuation** (same session that
  did Steps 6–10, or one seeded with this report) — the fix depends on
  precisely reproducing the `stall_recovery` idiom already in the file.
- **Acceptance criteria:** every one of the nine `OperatorRequest.kind`
  values has an explicit branch (or an explicit, commented decision that
  the default fallthrough is correct, as is true today only for `"budget"`
  and arguably `"underspecification"` needs its own fix too — audit both
  while in the function); an unrecognized `kind` raises rather than
  silently no-ops; `"reconcile"`/`"retry"`-shaped answers return the
  associated step(s) to `READY`; `"halt"` always reaches `RunStatus.HALTED`.
- **Required non-live proof:** new unit tests per kind (reconcile → READY +
  RUNNING; halt → HALTED); extended `test_batch_execution.py` batch
  reconciliation test; full non-live suite green.
- **Required live proof:** one of the four disposable scenarios
  deliberately engineered to hit `fail_dispatch` (e.g., pre-seed an
  untracked file in the fixture before the executor runs, or reuse the
  M2 verification-failure path), then answer `"reconcile"` via the real
  `dispatcher answer` CLI, and observe the run reach `SUCCEEDED` (or
  `"halt"` reaching `HALTED`) — this is a new, currently-nonexistent
  scenario and should be added to the Live Evidence Plan below.

## Step B — Enforce verification-check content, not just shape (M2)

- **Exact scope:** Add a completion-time (or result-application-time) rule
  that inspects `ExecutorResult.verification`/`ReviewerResult.verification`
  content — not just its schema — before allowing `ACCEPTED`, uniformly
  whether or not review is required. Bundle with M3/M4 (failure-detail
  preservation) because both are about what happens to information that is
  present in a worker's result/exception but currently silently dropped
  before or at the acceptance boundary.
- **Files likely affected:** `src/dispatcher/sequential.py`
  (`apply_executor_result`, `apply_reviewer_result`, and a new helper e.g.
  `_validate_executor_verification`/`_validate_reviewer_verification`
  alongside the existing `_validate_executor_evidence`),
  `src/dispatcher/execution.py` (`execute_worker`'s two `except` blocks,
  `fail_dispatch` call sites — for M3/M4), `src/dispatcher/workflow.py`
  (`completion_obligations`, if criterion-coverage is added),
  `src/dispatcher/results.py` (if an explicit allow-list field for
  optionally-skippable checks is added to the plan/config schema — do not
  add silent leniency), plus the corresponding unit/integration tests
  (`tests/unit/test_results.py`, `tests/unit/test_sequential.py`,
  `tests/unit/test_execution.py`).
- **Model recommendation:** this changes an accept/reject decision boundary
  in the most safety-critical part of the system; do this with the most
  capable model available in the session and require a second independent
  read-through of the new gate before merging (the existing readiness
  review's methodology — no reshaping, no repair, fail closed — should be
  reapplied specifically to this new code).
- **Fresh or continuation session:** **Continuation**, for the same reason
  as Step A (needs the exact `apply_executor_result`/`apply_reviewer_result`
  control flow already traced in this report), but treat the new
  gate's design (allow-list vs. strict-all-passed) as a decision requiring
  explicit operator/owner sign-off before implementation, since it changes
  what "T2.2a acceptance" means.
- **Acceptance criteria:** an `ExecutorCompletedResult`/`ReviewerAcceptedResult`
  containing any `verification[].status in {"failed","skipped"}` (outside an
  explicit, plan-declared allow-list) is rejected/escalated through the
  existing `on_failed`/`on_changes_requested` retry policy, identically on
  review-required and no-review paths; `M3`'s `failure_detail` is populated
  with the specific failing check(s).
- **Required non-live proof:** unit tests for both no-review and
  review-required paths with a failed/skipped verification entry; full
  non-live suite green; property/fuzz test on `parse_executor_result`
  extended to include failing-verification payloads (closing N8 from the
  prior readiness review).
- **Required live proof:** rerun the cross-repository batch scenario (after
  Step C's fixture hygiene fix removes the *unintended* dirty-worktree
  trigger) and separately construct one disposable scenario where the model
  is explicitly asked to report a `skipped`/`failed` check, confirming the
  dispatcher now rejects/escalates instead of accepting it.

## Step C — Deterministic, hygienic disposable fixtures (N1)

- **Exact scope:** Remove the axis of non-determinism and residue risk from
  the live disposable fixtures themselves — pre-seed `.gitignore` and,
  where useful, a deterministic `test_result.py`; set
  `PYTHONDONTWRITEBYTECODE=1`/pytest cache suppression in the child
  environment for disposable scenarios; add one explicit hygiene sentence
  to the disposable prompts. Do **not** touch `commit_policy="required"` or
  any other production clean-worktree enforcement.
- **Files likely affected:** `tests/helpers.py` (`create_fixture_project`
  and friends), `tests/live/test_real_operation_disposable.py`
  (`_real_project`, `_initialize_repository`, `_commit_initial`, the
  `original_prompts` strings).
- **Model recommendation:** low-risk, test-only change; any competent model
  can do this safely, no special capability required.
- **Fresh or continuation session:** either; this is self-contained and
  does not depend on the deep dispatcher-internals context Steps A/B need.
- **Acceptance criteria:** rerunning the exact cross-repository batch
  scenario with an unmodified dispatcher no longer fails on residue from
  the executor's own verification tooling; `git status --porcelain` is
  empty in both repositories after a successful run without any dispatcher
  behavior change.
- **Required non-live proof:** none new (this only affects the
  `live_opencode`-marked tests), but confirm the non-live suite is
  unaffected (269/269 stays green).
- **Required live proof:** the four disposable scenarios, rerun after Steps
  A–C, all reaching `outcome.accepted is True` with clean repositories.

---

# Live Evidence Plan

A deterministic final disposable proof must cover, in one coherent test
session (not necessarily one test function):

1. **Real sequential execution** — `test_real_sequential_disposable_repository_operation`
   (existing): one step, executor + reviewer, `session_mode` new→(review). No
   change needed beyond Step C's prompt hygiene.
2. **Real review/rework/resume** — exercised implicitly by
   `test_reactive_supervisor_command_decisions` (non-live) plus a live pass
   of scenario 1 with a deliberately rejected first attempt (currently not
   explicitly forced in the live suite — recommend adding a variant where
   the reviewer is prompted to request changes at least once, to prove the
   `resume` session-mode path live, not just via the non-live reactive-
   command unit test).
3. **Real cross-repository batch** —
   `test_real_cross_repository_disposable_batch_operation` (existing),
   rerun after Steps A–C. Must show: both children `FORWARDED`/`ACCEPTED`
   independently, batch `JOINED` (not `FAILED`), no `WAITING_OPERATOR`.
   **New, additional scenario required:** a deliberately-failing batch child
   (e.g., pre-existing untracked file in one repo before dispatch) proving
   the **M1 fix** — batch `FAILED` → operator `dispatcher answer reconcile`
   → failed child's step returns to `READY` → re-dispatch → run reaches
   `SUCCEEDED`.
4. **Real same-repository worktree barrier** —
   `test_real_same_repository_worktree_barrier_promotes_and_cleans`
   (existing): confirms exactly one `git worktree list` entry remains after
   promotion/cleanup. No change needed beyond Step C.
5. **Real cancellation/recovery** —
   `test_real_cancellation_leaves_disposable_repository_and_recovery_state_safe`
   (existing): confirms `interrupted` category, clean repository,
   `operator_reconciliation_required` disposition from `classify_recovery`.
   **Recommend extending** this scenario to also call `dispatcher answer`
   (once M1 is fixed) and confirm the cancelled dispatch's step can be
   re-prepared — currently the scenario stops at classification, matching
   the non-mutating nature of `dispatcher recover` (N6 from the prior
   readiness review), but the *answer* path for a `"reconciliation"`-kind
   request raised by a cancellation-adjacent failure is exactly what M1
   fixes.
6. **Role matrix / documented limitation:** the current fixture assigns one
   `DISPATCHER_LIVE_MODEL` to *every* configured role (`supervisor`,
   `executors`, `reviewers` — `_real_project`, lines 275-277). This proves
   **schema/protocol compliance across roles for one model**, and proves
   the dispatcher's own control logic (session isolation, batch semantics,
   snapshot validation, retry/escalation) works correctly regardless of
   which model plays which role. It does **not** prove:
   - that a *different* model in the Terra-executor role behaves
     identically (e.g., its propensity to invent tests, its adherence to
     "no explanation/Markdown," its verdict/severity vocabulary discipline
     under real token-budget pressure);
   - any interaction effect from genuinely heterogeneous
     supervisor/executor/reviewer models (e.g., a weaker reviewer model
     failing to produce a schema-valid `changes_requested` where a stronger
     one would);
   - real behavior of `dispatcher execute`'s CLI/audit path (N3) — all five
     scenarios construct the coordinator directly, bypassing
     `_cmd_execute`/`append_audit_event` entirely.
   **Recommendation:** do not invoke it now, but a separate,
   matrix-configurable disposable test (parameterizing
   `DISPATCHER_LIVE_SUPERVISOR_MODEL` / `DISPATCHER_LIVE_EXECUTOR_MODEL` /
   `DISPATCHER_LIVE_REVIEWER_MODEL` independently, defaulting to the current
   single-model behavior when unset) is required before any claim of
   "T2 model-matrix readiness" — this is explicitly out of scope for a
   single T2.2a step and should be tracked as a distinct follow-up, not
   bundled into Steps A–C.

---

# Final Review Preconditions

Before a separate, final Claude Sonnet 5 release review begins, the
following must be true:

1. **M1 (operator reconciliation) is fixed, tested, and live-proven** —
   without this, no failure encountered during the final review's own
   observation window (however unlikely) can be recovered from or halted
   through the documented interface.
2. **M2 (verification-content enforcement) is fixed and tested** on both
   review-required and no-review paths, with the property/fuzz test
   extension covering failing-verification payloads.
3. **M3/M4 (failure-detail preservation and batch-child logging) are
   fixed**, so that if the final review's own live run hits any failure,
   the reviewer can diagnose it from durable state and/or logs without
   manual JSONL archaeology.
4. **The disposable fixtures are hygienic and deterministic (Step C)** —
   `.gitignore` seeded, bytecode/cache suppressed, and (recommended but not
   strictly required) a pre-seeded deterministic verification file — so
   that a rerun's pass/fail is attributable to dispatcher behavior, not to
   model discretion over test authoring.
5. **All four live disposable scenarios pass against the actual target
   model(s)** under the current strict, non-repairing contract, with a
   freshly dated, machine-produced (not hand-narrated) pass record —
   including the new M1-proving "deliberately-failing-then-reconciled"
   batch scenario.
6. **The non-live suite remains fully green**
   (`.venv/bin/python -m pytest tests -q -m "not live_opencode"`; currently
   269 passed, 0 failed, 5 deselected) with no reduction in test count
   relative to this baseline.
7. **The currently uncommitted worktree is committed** to a single,
   tagged, auditable revision (carrying forward N7 from the prior readiness
   review, still true today — the worktree spans steps 1–10 plus this
   report, all uncommitted) — the final review should review one commit,
   not a dirty tree.
8. **An explicit, operator-recorded decision** confirms the exact scope of
   the next real run (T2.2a only, no T2.2b, fixture-mode only) and the
   exact model/role matrix being used, given the role-matrix limitation
   documented above — the final reviewer should not have to infer this from
   test code.
9. **This report's Mandatory Findings (M1–M4) each have a corresponding,
   passing, named test** that the final reviewer can point to directly
   (file:line), not a narrative claim of "fixed" — consistent with this
   report's own method of treating prior narrative reports as claims only.
