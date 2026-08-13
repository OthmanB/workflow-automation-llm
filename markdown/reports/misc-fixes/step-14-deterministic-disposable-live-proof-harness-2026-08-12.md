# Step 14 Deterministic Disposable Live Proof Harness Report

Date: 2026-08-12

## Outcome

Step 3 of the systemic remediation plan is implemented. The disposable live
harness is now deterministic and hygienic: every disposable repository is
seeded with committed cache ignores and fixed pytest files before its initial
commit, acceptance criterion IDs and verification commands are fixed, the
executor and reviewer prompts are fully deterministic, role model IDs are
independently configurable, and four new live scenarios cover
review/rework/resume, solo reconciliation, batch reconciliation, and the halt
branch through the real `dispatcher answer` CLI. A one-shot, thread-safe,
test-only failure injector forces repository-validation failures without
touching model output or production code.

The final non-live suite passed with 366 tests, 20 more than the 346-pass
baseline, with 9 live scenarios deselected (up from 5).

## 1. Exact Files Changed

1. `tests/live/test_real_operation_disposable.py`
2. `docs/operations.md`
3. `markdown/reports/misc-fixes/step-14-deterministic-disposable-live-proof-harness-2026-08-12.md`

All other pre-existing uncommitted work was preserved untouched.

## 2. No Production Dispatcher Change

`git status` and the step diff confirm that no file under `src/dispatcher/`
was modified by this step. `commit_policy="required"`, clean-worktree
validation, snapshot identity/evidence checks, verification enforcement,
operator-answer state transitions, and all other production behavior are
unchanged. No production defect blocked this step.

## 3. Fixture Seed Files And Criterion IDs

Every disposable repository, including the dynamically created sibling
repository and the same-repository worktree base, is seeded before
`_commit_initial` with a committed `.gitignore`:

```text
__pycache__/
*.py[cod]
.pytest_cache/
```

Fixed pytest files are seeded per scenario:

| Seed | File | Fails until |
|---|---|---|
| `first` | `test_real_output.py` | `result.txt` contains exactly `REAL_DISPOSABLE_OK` and `evidence/real-evidence.md` exists |
| `second` | `test_real_second_output.py` | `result-second.txt` contains exactly `REAL_DISPOSABLE_OK` and `evidence/real-evidence-second.md` exists |

Scenario mapping: the sequential and cancellation fixtures seed only `first`;
the cross-repository batch seeds `first` in the primary and `second` in the
sibling; the same-repository worktree batch seeds both in the base repository
with each step prompt running only its own test file; the reconciliation and
halt scenarios seed `first`.

Acceptance criterion IDs are fixed and distinct per step:

| Step | Criterion ID | Required verification command |
|---|---|---|
| `prepare-fixture` | `verify-real-output` | `python -m pytest -q test_real_output.py` |
| `prepare-second` | `verify-real-second-output` | `python -m pytest -q test_real_second_output.py` |

`_plan` writes these criterion IDs and descriptions naming the exact command;
executor prompts name the exact files/content, the exact pytest command, the
exact criterion ID, the evidence path, the commit requirement, the
no-network/deployment prohibition, the `git status --porcelain` empty
requirement, and the return-only-JSON requirement. Reviewer prompts name the
same criterion IDs and test commands. Model-created tests are never part of
the proof.

## 4. Role-Model Resolution

`resolve_live_models()` is a pure helper in the live test file:

| Variable | Role | Behavior |
|---|---|---|
| `DISPATCHER_LIVE_MODEL` | All roles | Explicit all-role fallback |
| `DISPATCHER_LIVE_SUPERVISOR_MODEL` | Supervisor | Overrides the fallback for the supervisor role |
| `DISPATCHER_LIVE_EXECUTOR_MODEL` | Executors | Overrides the fallback for every executor role |
| `DISPATCHER_LIVE_REVIEWER_MODEL` | Reviewers | Overrides the fallback for every reviewer role |

If the fallback exists it applies to every role not overridden; an empty
role-specific value falls back. If the fallback is absent, all three
role-specific variables must be present and missing variables are listed in a
`ValueError`; there is no silent `openai/gpt-4.1` default. Supervisor roles
are configured from the supervisor value, executor roles from the executor
value, and reviewer roles from the reviewer value. The cancellation
subprocess is explicitly given the resolved executor model through
`DISPATCHER_LIVE_EXECUTOR_MODEL` in its child environment and reads only that
variable.

Documented limitation: the live harness replaces `run_supervisor_turn` with a
deterministic reactive Python supervisor, so configuring
`DISPATCHER_LIVE_SUPERVISOR_MODEL` does not prove that model was invoked.
`docs/operations.md` states this explicitly and forbids claiming real
supervisor-model coverage.

Unmarked tests: `test_resolve_live_models_all_role_fallback`,
`test_resolve_live_models_all_three_role_specific`,
`test_resolve_live_models_partial_role_overrides_with_fallback`,
`test_resolve_live_models_missing_required_values_fail_loudly`,
`test_resolve_live_models_assigns_roles_into_fixture_config`.

## 5. Live Scenarios

All eight scenarios are `@pytest.mark.live_opencode` and were only collected,
never executed.

1. `test_real_sequential_disposable_repository_operation` — one step,
   executor + reviewer against real OpenCode, fixed test and evidence, clean
   terminal state, no leases, exactly one worktree.
2. `test_real_cross_repository_disposable_batch_operation` — two
   repositories, both children accepted and acknowledged, batch `JOINED`, run
   not `WAITING_OPERATOR`, both repositories clean.
3. `test_real_same_repository_worktree_barrier_promotes_and_cleans` — both
   tests seeded in one base, batch `JOINED`, group `CLEANED`, exactly one
   `git worktree list` entry remains, both fixed tests pass in the promoted
   repository.
4. `test_real_cancellation_leaves_disposable_repository_and_recovery_state_safe`
   — process-creation-time identity check, interrupt category,
   `operator_reconciliation_required`, clean repository, no leases; the child
   subprocess uses the resolved executor model.
5. `test_real_review_rework_resume_cycle_accepts_after_remediation` — first
   reviewer is instructed to return a schema-valid `changes_requested`
   requiring `review-marker.txt` while still reporting exact criterion IDs;
   reactive supervisor dispatches rework with `session_mode=resume`; second
   reviewer accepts; asserts `executor_attempts == 2`, `reviewer_attempts ==
   2`, `review_acceptances == 1`, two executor dispatches, the second executor
   attempt reusing the first runtime session ID, `review-marker.txt` present,
   repository clean, fixed test passing.
6. `test_real_solo_reconciliation_via_cli_resumes_and_succeeds` — one-shot
   injector forces `repository_validation`; run `WAITING_OPERATOR` with
   `reconciliation`; operator removes the residue, resets the disposable
   repository to its recorded initial revision, verifies clean; real CLI
   `answer ... reconcile` returns 0; run `RUNNING`, step `READY`; the same
   coordinator/run continues; replacement succeeds; final run `SUCCEEDED`;
   one operator decision; failed dispatch historical with actionable detail;
   no leases; clean.
7. `test_real_batch_reconciliation_via_cli_retries_only_failed_child` —
   injector targets exactly one child; batch `FAILED`, run
   `WAITING_OPERATOR` with `batch_reconciliation`, accepted sibling preserved;
   operator reconciles only the failed child; CLI `answer ... reconcile`;
   only the failed step returns to `READY`; replacement batch/session is
   distinct and succeeds; original batch remains `FAILED`; sibling attempt
   count stays 1, retried child becomes 2; sibling files/commit unchanged;
   final run `SUCCEEDED`; one operator decision; no leases; both clean.
8. `test_real_halt_via_cli_is_terminal_and_preserves_historical_state` —
   same forced failure; operator answers `halt` without reconciling; asserts
   return 0, run `HALTED`, step remains `BLOCKED`, request cleared, exactly
   one operator decision, no new dispatch, repository content and HEAD
   untouched, historical failure category/detail available.

## 6. Deterministic Failure-Injection Mechanism

`OneShotResidueInjector` is a test-only wrapper around the real
`dispatcher.sessions.run_session` (or a fake in non-live proofs). It calls the
wrapped runner unchanged and returns the real unmodified `SessionResult`.
Only for the first invocation whose resolved `workdir` matches the configured
target does it write `forced-reconciliation-residue.tmp` after the runner
exits and before production repository validation runs, making the snapshot
dirty deterministically. Selection is recorded under a lock at call entry
(calls/injections counters plus the last workdir/title), is one-shot, and is
thread-safe for batch execution. It never alters stdout, chat response, or
result JSON.

Unmarked tests: `test_one_shot_injector_targets_exactly_one_dispatch`,
`test_one_shot_injector_is_one_shot`,
`test_one_shot_injector_is_thread_safe_for_non_targets`,
`test_one_shot_injector_returns_real_result_unmodified`.

## 7. Reconciliation/Halt CLI Invocation Path

`_answer_command()` is a pure builder producing the exact argv:

```text
answer --config <project.yaml> --run-id <run-id> --request-id <request-id>
       --answer reconcile|halt --actor-id operator
```

`_answer_via_cli()` loads the durable request ID and invokes
`dispatcher.cli.main(...)` in-process (the harness store stays open; the CLI
uses its own WAL connection, matching existing CLI answer tests). The solo
and batch scenarios then call `_run_bounded_orchestration` again on the same
coordinator/run: the current generation is reloaded, leftover FORWARDED
dispatches are acknowledged via the production `acknowledge_forwarding`, the
production `activate` accepts the `RUNNING` run, and the deterministic
reactive supervisor continues. No production coordinator API was modified.
`test_answer_command_builder_is_exact` covers the argv construction.

## 8. Non-Live Test Names And Results

Focused run of the live file with live tests deselected:

```text
.venv/bin/python -m pytest tests/live/test_real_operation_disposable.py -q -m "not live_opencode"
21 passed, 8 deselected in 5.48s
```

Unmarked tests added or extended:

- `test_reactive_supervisor_command_decisions` (extended: first attempt,
  review dispatch, rework resume, operator-reconciliation replacement with a
  fresh `new` attempt, reviewer prompt selection by attempt, retry
  exhaustion, batch initial dispatch, batch replacement dispatch for the
  failed step only, completion after all accepted)
- `test_resolve_live_models_all_role_fallback`
- `test_resolve_live_models_all_three_role_specific`
- `test_resolve_live_models_partial_role_overrides_with_fallback`
- `test_resolve_live_models_missing_required_values_fail_loudly`
- `test_resolve_live_models_assigns_roles_into_fixture_config`
- `test_repository_fixture_seeding_commits_gitignore_and_fixed_tests`
- `test_fixed_tests_fail_until_result_files_exist`
- `test_step_specs_define_exact_criterion_ids_and_commands`
- `test_plan_uses_exact_criterion_ids_and_commands`
- `test_executor_prompts_are_deterministic_and_hygienic`
- `test_reviewer_prompts_name_exact_criteria_and_commands`
- `test_one_shot_injector_targets_exactly_one_dispatch`
- `test_one_shot_injector_is_one_shot`
- `test_one_shot_injector_is_thread_safe_for_non_targets`
- `test_one_shot_injector_returns_real_result_unmodified`
- `test_answer_command_builder_is_exact`
- `test_solo_reconciliation_full_loop_with_fake_runner`
- `test_halt_full_loop_with_fake_runner`
- `test_review_rework_resume_full_loop_with_fake_runner`
- `test_batch_reconciliation_full_loop_with_fake_runner`

The four full-loop tests use an in-process deterministic stand-in for
`run_session` that parses the exact worker prompt payload, performs and
commits the authorized repository work, and returns schema-valid
executor/reviewer results, proving the entire harness mechanics (injection,
CLI answer, repository reconciliation, continuation, batch replacement, and
resume session reuse) without any model.

## 9. Full Non-Live Pytest Summary

```text
.venv/bin/python -m pytest tests -q -m "not live_opencode"
366 passed, 9 deselected in 54.31s
```

The pass count increased from the 346-pass baseline by 20 tests; the
deselected count increased by 4 because four new live scenarios were added.

## 10. Live Collect-Only Summary

```text
.venv/bin/python -m pytest tests/live/test_real_operation_disposable.py --collect-only -q
29 tests collected in 0.17s
```

All eight live scenarios collect successfully.

## 11. Static Checks

```text
.venv/bin/ruff check tests/live/test_real_operation_disposable.py
All checks passed!

git diff --check
no output (pass)
```

## 12. Live Scenarios Were Not Executed

No `live_opencode`-marked scenario was executed. The suite was run only with
`-m "not live_opencode"`, live tests were collected with `--collect-only`,
and `DISPATCHER_REAL_DISPOSABLE=1` was never set. Executing the live proof
requires a human with the dedicated OpenCode credential store and the role
model variables from `docs/operations.md`.

## 13. Constraints Confirmed

No commit was created or amended, no branch was created, and nothing was
pushed. No live OpenCode or live-marked test was run. No network or HTTP call
was made. No credentials were used or accessed. No `config/projects/local/`,
`config/state/`, repository-root `state/`, private T2 state, or other
prohibited path was inspected or modified. No file under `src/dispatcher/`
was modified, and no production verification, cleanliness, identity,
evidence, or operator-answer enforcement was weakened. All changes and this
report remain uncommitted.

## 14. Deviations From The Prompt

- `_decide_next_command` now distinguishes an operator-reconciliation
  replacement (executor attempts already used, `rework_rounds == 0`) from
  reviewer rework (`rework_rounds > 0`): the replacement dispatches a fresh
  `new` attempt with the original deterministic prompt, because the operator
  hard-reset the disposable repository and a resumed session would reference
  discarded work. The pre-existing `test_reactive_supervisor_command_decisions`
  unit test was updated to set `rework_rounds` explicitly for its rework
  cases so both rework-resume and reconciliation-replacement behavior remain
  covered.
- `_run_bounded_orchestration` performs a test-only preamble that
  acknowledges leftover `FORWARDED` dispatches before resuming a paused run.
  A run that stops at `WAITING_OPERATOR` mid-loop cannot have acknowledged
  its successful sibling's forwarding, and production completion obligations
  treat `FORWARDED` as in-flight; this preamble restores the equivalent of
  the in-loop acknowledgement the interrupted orchestration would have
  performed. Production APIs are unchanged.
- `_reconcile_disposable_repository` recreates the empty `evidence/`
  directory after `git reset --hard`, because git removes directories
  emptied by the reset and `load_config` requires each configured evidence
  root to exist as a directory. This is disposable-fixture-only behavior.
- A small `_non_live_project` helper creates the parent directory required by
  `create_fixture_project` for the non-live loop tests.
- `tests/helpers.py` was not modified; all disposable-specific helpers were
  kept local to `tests/live/test_real_operation_disposable.py` per the scope
  preference. No focused `tests/fixtures/` changes were required.
