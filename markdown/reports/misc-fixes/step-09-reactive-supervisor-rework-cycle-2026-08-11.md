# Step 09 — Reactive supervisor mock: model a genuine rework cycle in the live disposable test

Date: 2026-08-11
Scope: test-fixture only — `tests/live/test_real_operation_disposable.py`. Nothing under
`src/dispatcher/` was touched. All changes are left uncommitted in the working tree.

## Root cause being fixed

`test_real_sequential_disposable_repository_operation` drove the coordinator through a
hard-coded, fixed-length list of exactly 3 supervisor commands
(`_sequential_commands()`: dispatch executor → dispatch reviewer → request completion),
so a legitimate 4th supervisor decision (dispatch a rework after a `changes_requested`
verdict) raised `StopIteration` inside `next(responses)`.

## What changed (exact citations)

All citations are `tests/live/test_real_operation_disposable.py` unless noted.

1. **Reactive decision core** — `_decide_next_command(record, *, steps, original_prompts,
   reviewer_role, batch, reviewer_results)` at `:333`. It reads only the current durable
   run record and returns the next command string:
   - Step `READY` + `executor_attempts == 0` → first executor dispatch (`session_mode "new"`,
     original task prompt).
   - Step `READY` + `executor_attempts > 0` and below `plan_step.retry.max_executor_attempts`
     → rework dispatch; prompt embeds the reviewer's `required_remediation` and finding
     summaries via `_rework_prompt` (`:394`); `session_mode "resume"` for sequential,
     `"new"` for batch children (see derivation below).
   - Step `EXECUTED`/`REVIEW_REQUIRED` + `reviewer_attempts` below
     `max_reviewer_attempts` → reviewer dispatch (`session_mode "new"`, `_REVIEW_PROMPT`
     at `:330`).
   - Step `ACCEPTED`/`WAIVED`, or any step whose attempts are genuinely exhausted, or any
     terminal state → nothing is forced; `_completion_command` (`:427`) is returned so the
     coordinator's own completion-obligation denial / turn-limit halt takes over naturally
     (constraint 6 of the task: do not paper over a genuine exhaustion case).
2. **Durable reviewer-result lookup** — `_latest_reviewer_results(store, record)` at `:379`
   loads the newest reviewer dispatch's result payload per step via
   `store.load_dispatch_payload(...).result` — the same accessor production code uses in
   `src/dispatcher/sequential.py:1790` (`_review_target`).
3. **Mock wiring** — `_run_real_scenario` (`:214`) now binds a reactive `supervisor_turn`
   that reloads state each turn through `_self.store.load_run(run_id)` and delegates to
   `_decide_next_command`; the fixed `iter(commands)` list is gone.
4. **Batch envelope** — `_batch_command` (`:414`) emits protocol-v2 `dispatch_batch` with
   only the children that still need work, so batch scenarios can also re-dispatch after a
   rework instead of exhausting a 2-item fixed list (`_batch_commands()` deleted; both
   batch callers updated to the reactive signature at `:64`/`:97`-area call sites).
5. **Plan policy** — `_plan()` now sets `on_changes_requested = "retry"` (`:305`) so a
   `changes_requested` verdict actually moves the step to `READY` and a rework dispatch is
   legal. Retry limits are untouched: `max_executor_attempts: 2`,
   `max_reviewer_attempts: 2` (still `:303-304`), acceptance criteria unchanged. This
   mirrors the codebase's own rework-scenario convention
   (`tests/unit/test_sequential.py:86` sets the same trio for its review fixtures).
6. **`_dispatch`** (`:431`) gained a `session_mode` keyword (default `"new"`) so the
   rework resume mode is expressible; the cancellation test's existing call site is
   unchanged.

## Derivation of the rework dispatch shape from precedent

- **Session mode**: `tests/fixtures/opencode/fake_cli.py:151-168` (`_supervisor_response`)
  emits turn 3 as `_dispatch("terra", "resume", "Apply the requested fixture rework.")`
  and `tests/integration/test_sequential_git_e2e.py:91-92,108-109` asserts the rework
  executor call resumes the first attempt's session (`requested_session ==
  session_id` of attempt 1). The sequential rework therefore uses `session_mode: "resume"`.
- **Batch children cannot resume**: `src/dispatcher/sequential.py:789` registers batch
  sessions under `logical_session_key` (`executor-terra-<step_id>`), while
  `_owned_session_id`/`_validate_step_readiness` (`sequential.py:1923-1936, 1773-1777`)
  look them up by `role_key`; a batch rework with `resume` would raise
  "requested session mode has no dispatcher-owned session". Batch rework children
  therefore use `session_mode: "new"` (safe fresh session).
- **Reviewer sessions are always new**: enforced by
  `_validate_step_readiness` (`sequential.py:1751-1752`: "reviewer sessions must be new").
- **Rework prompt content**: fake_cli's "Apply the requested fixture rework." is expanded
  to carry the reviewer's `required_remediation` and finding `summary` values (shapes from
  `src/dispatcher/results.py:120-168`: `ReviewFinding` has `finding_id`/`severity`/`summary`;
  `ReviewerChangesRequestedResult.required_remediation` is required), while preserving the
  original task text and the "return only the required JSON result object" instruction.
- **Rework legality**: `sequential.py:1033-1047` only routes `changes_requested` to a
  reworkable `READY` state when `on_changes_requested == "retry"` (and rework rounds /
  executor attempts remain); with the inherited `"halt"` default
  (`tests/helpers.py:320`) the step went `FAILED` and no mock could legally dispatch a
  rework. Hence the `on_changes_requested = "retry"` plan change at `:305`.

## New isolated unit test

`test_reactive_supervisor_command_decisions` (`:496`) is unmarked (runs in the non-live
suite). It builds synthetic run records (via `valid_plan_values` + `NormalizedPlan` +
`new_run_record` + `transition_step`, mirroring `tests/unit/test_sequential.py:70-108`)
and asserts, for each decision branch:
- initial executor dispatch (`session_mode "new"`, original prompt),
- reviewer dispatch (`target_role "reviewer"`, `"new"`),
- rework dispatch (`session_mode "resume"`, prompt contains remediation text, finding
  summary, and the original task),
- accepted step → `request_completion`,
- exhausted executor attempts → `request_completion` (nothing forced),
- batch initial (`dispatch_batch` with both children, `"new"`),
- batch rework (`dispatch_batch` with only the pending child, `"new"`),
- batch complete → `request_completion`.

Result: `1 passed` (`.venv/bin/python -m pytest tests/live/test_real_operation_disposable.py -q -m "not live_opencode"`).

## Verification performed

- Syntax: `.venv/bin/python -m py_compile tests/live/test_real_operation_disposable.py` — clean.
- Lint: `.venv/bin/python -m ruff check tests/live/test_real_operation_disposable.py` — clean
  (one import-order fix applied via `ruff check --fix`, then re-checked).
- Full non-live suite final summary line:

  `267 passed, 5 deselected in 38.59s`

  (266 prior tests + 1 new unit test; the 4 live-disposable tests plus 1 live-opencode
  test remain deselected.)

## Explicit limitation

The live scenario itself (`test_real_sequential_disposable_repository_operation` and the
two batch live tests) was **not executed** by this task: it requires
`DISPATCHER_REAL_DISPOSABLE=1` and a real model credential (`DISPATCHER_LIVE_MODEL`),
which are out of scope. A human must rerun the live disposable tests with real
credentials to confirm the rework cycle completes end-to-end. The fix is verified here
only at the level of: (a) the decision logic against synthetic run-record states, and
(b) the full non-live regression suite.
