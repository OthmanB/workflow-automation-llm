# Step 5 - Execute Command End-to-End Proof

**Date:** 2026-08-11  
**Status:** Implemented, verified, uncommitted, for human review.

## Result

Added `tests/integration/test_execute_command_disposable.py`, a normal
non-live integration suite that invokes `dispatcher.cli.main(["execute", ...])`
against the real CLI, real preflight and prerequisite gates, real sequential
coordinator, and real subprocess-backed session adapter. The only substitution
is the existing deterministic fake OpenCode executable copied into the test
temporary directory.

No production file under `src/dispatcher/` was changed for this task. The
`OPENCODE_BIN` monkeypatch was sufficient because
`src/dispatcher/cli.py:260-316` imports the unmodified
`dispatcher.sessions.run_session`, and `src/dispatcher/sessions.py:28` looks
up `OPENCODE_BIN` when launching the subprocess. The test patches that module
constant at `tests/integration/test_execute_command_disposable.py:50-56`; no
injection seam or alternative session runner was needed.

## Test Construction

- `tests/integration/test_execute_command_disposable.py:12-28` imports the
  existing fixture utilities and real dispatcher APIs. It reuses
  `create_fixture_project`, `config_values`, `valid_plan_values`, and
  `write_config` from `tests/helpers.py:27-325`, as requested.
- `_prepare_execute_fixture` at
  `tests/integration/test_execute_command_disposable.py:130-247` is new
  local test setup. It configures `real_operation`, a preflight block with
  `models_smoke_test: false` and `credentials: []`, and executor commit
  permission at lines 131-145.
- The same helper creates the one-step, review-required plan, retry/rework
  policy, YAML sidecar, approved pending baseline, approved plan, durable run,
  current revision, exact permission digest, and exact stall-policy digest at
  lines 147-199 and 228-247. The pending baseline is necessary because the
  existing execute gate validates the current approved baseline before it
  inspects the repository.
- It writes an exact fresh `LiveSmokeProof` at lines 201-216 and generates
  the `RealOperationApproval` directly with `approve_real_operation` at lines
  217-226. This tests execute's artifact consumption, not the already-tested
  artifact producer commands.
- `_execute_argv` at lines 250-274 supplies every execute flag, including
  both artifact paths, both calculated digests, expected commit revision, and
  `--confirm-real-operation`.
- `_install_fake_opencode` at lines 277-284 is a deliberately local copy of
  the small helper in `tests/integration/test_sequential_git_e2e.py:132-139`.
  It copies `tests/fixtures/opencode/fake_cli.py` into the test temporary
  directory and marks it executable. `_commit_initial_fixture` and `_git` at
  lines 287-309 are similarly local adaptations of that test's disposable
  Git setup. They were kept local rather than adding a new shared test helper.

## Assertions

`test_execute_command_completes_disposable_fake_opencode_run` at
`tests/integration/test_execute_command_disposable.py:45-75` is not marked
`@pytest.mark.live_opencode` and asserts all of the following:

- `main(_execute_argv(...))` returns `0` and prints the successful execute
  completion message.
- The temporary fake executable records exactly nine real subprocess calls,
  proving the actual session-spawning path ran through the deterministic
  stand-in.
- The state store's final `RunRecord` is `RunStatus.SUCCEEDED`.
- The disposable repository is clean and has exactly three commits: the
  initial commit plus the executor's two rework-cycle commits.
- `src/value.txt` is exactly `value=2\n` and `evidence/fixture.md` is exactly
  the expected second-attempt evidence content.
- A direct SQLite query of the authoritative `audit_events` table for this
  run (`sqlite3.connect(fixture.store.database_path)` at lines 67-74) includes
  the `real_operation_approved` event. This uses the same direct SQLite audit
  assertion pattern as `tests/integration/test_sequential_git_e2e.py:122-129`.

The two representative CLI-level negative tests are:

- `test_execute_command_requires_approval_record_argument`
  (`tests/integration/test_execute_command_disposable.py:78-108`) supplies a
  complete-looking argv except `--approval-record`, asserts argparse raises
  `SystemExit(2)`, and checks its exact required-argument diagnostic.
- `test_execute_command_rejects_wrong_revision_without_launching_fake_opencode`
  (`tests/integration/test_execute_command_disposable.py:111-127`) supplies a
  real argv with an otherwise valid fixture but a wrong expected revision. It
  asserts CLI return code `2`, the exact `execute: FAILED - repository is not
  at the expected revision` stderr format, unchanged revision and clean
  repository status, and no fake `calls.jsonl` file. Therefore no OpenCode
  subprocess was launched after the early gate failed.

## Verification

```text
.venv/bin/python -m pytest tests/integration/test_execute_command_disposable.py -q -m "not live_opencode"
3 passed in 2.76s

.venv/bin/ruff check tests/integration/test_execute_command_disposable.py
All checks passed!

.venv/bin/python -m pytest tests -q -m "not live_opencode"
259 passed, 5 deselected in 36.75s
```

The final result has 259 passing tests, strictly more than Step 4's 256.

## Scope Confirmation

No commit, push, branch creation, or production-code change was made in this
task. All changes, including this report, remain uncommitted. The prohibited repository
paths `config/projects/local/`, `config/state/`, and `state/` were neither
read nor modified. All state used by this test is within each pytest temporary
fixture directory.

No live OpenCode, network, HTTP request, credential, or external service call
occurred. Positive execution patched `dispatcher.sessions.OPENCODE_BIN` to a
temporary copy of the deterministic local fixture; negative execution either
stopped in argparse or failed before the fake executable was invoked. The
normal full suite was explicitly run with `-m "not live_opencode"`.

The only judgment call was creating a valid pending baseline in the disposable
fixture. Although not separately enumerated in the task's setup list, this is
an existing mandatory execute prerequisite and was exercised rather than
mocked or bypassed. No other deviations were needed.

## Supervisor spot-check (independently verified, not just self-reported)

- `git log --oneline -3` → still at `72a4dea`. No commit created. Confirmed.
- `git status --porcelain -- config/ state/` → empty. Confirmed untouched.
- `git diff --stat HEAD -- src/dispatcher/` → **identical file list and line
  counts to the post-Step-4 diff** (cli.py +199/-?, operation.py +110,
  sessions.py +75, etc. — same numbers, unchanged). This directly confirms
  the report's central claim: zero production code was touched in this step.
- Read the full new test file
  (`tests/integration/test_execute_command_disposable.py`, 309 lines)
  directly, not just the report's excerpts. Confirmed:
  - The positive test calls the real `dispatcher.cli.main(...)` (imported
    directly from `dispatcher.cli`, not re-implemented) with a full argv,
    and only patches `sessions.OPENCODE_BIN` — genuinely the unmodified CLI
    path.
  - The baseline judgment call is correct and necessary: a `PENDING`
    decision is recorded for the target step (not `ACCEPTED`), since the
    step must still be genuinely executable, while some current baseline
    must exist for `validate_approved_baseline` to pass — this is a subtle,
    correct detail, not a shortcut.
  - The wrong-revision negative test asserts `not (fake_opencode.parent /
    "calls.jsonl").exists()` — concrete, falsifiable proof that no
    subprocess was ever launched after the early gate rejection, not merely
    an exit-code check.
  - The audit-event assertion queries the real SQLite `audit_events` table
    directly and checks for `real_operation_approved` — matches the
    project's own established direct-SQLite-audit-assertion convention from
    `test_sequential_git_e2e.py`.
- Ran the new test file in isolation:
  `tests/integration/test_execute_command_disposable.py` → **3 passed**
  (all three: the positive end-to-end run, the missing-argument case, and
  the wrong-revision case).
- Re-ran the full non-live suite independently: **259 passed, 5 deselected**
  — matches the model's reported count exactly (256 → 259, +3).

No corrections needed to the model's self-report for this step. This closes
Blocker B2 from the original review: `dispatcher execute` has now been
exercised end-to-end through its real, unmodified CLI entry point, with one
genuine full accepted run and two representative fail-closed rejections,
without any live network/credential use. Deeper review of the fixture's
representativeness of a true T2 target repository, and of whether additional
gate-specific CLI-level negative cases would still be valuable, is deferred
to the final Sonnet 5 review.
