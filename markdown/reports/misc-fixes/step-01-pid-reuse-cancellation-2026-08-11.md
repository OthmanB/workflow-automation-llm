# Step 1 — PID-Reuse-Safe Cancellation (closes Blocker B1)

**Date:** 2026-08-11
**Executing model:** GPT 5.6 Terra (fresh session, repo tool access)
**Source finding:** `markdown/reviews/dispatcher-t2-2a-real-operation-readiness-review-2026-08-11.md`, Blocker B1
**Status:** Implemented, uncommitted. Not independently deep-reviewed — see
"Verification status" below before relying on this for release evidence.

## Task given

Verify OS-level process creation time (via `psutil`) at signal time before
cancelling a dispatch, to close a PID-reuse exposure in
`cancel_process_group`. Full prompt text is reproduced in the supervisor
conversation; not duplicated here.

## Model's self-reported summary (unverified claims, quoted as given)

> Implemented PID-reuse protection and left all changes uncommitted.
> - Added psutil>=6,<7 and captured OS process creation time immediately
>   after worker spawn.
> - Persisted process_create_time through lifecycle, workflow transition,
>   state store, cancellation CLI, and generated workflow schema.
> - Cancellation now verifies PID identity before SIGINT, SIGTERM, and
>   SIGKILL; mismatches raise OpenCodeProcessIdentityError without
>   signalling.
> - Added real-process PID-reuse coverage and updated all lifecycle/
>   cancellation call sites.
> - Internal timeout cleanup remains safe: it signals through its direct
>   Popen reference rather than persisted PID state.
>
> New public APIs:
> ```python
> def cancel_process_group(
>     process_id: int,
>     expected_host: str,
>     grace_seconds: int,
>     expected_create_time: float,
> ) -> bool
>
> def StateStore.request_dispatch_cancellation(
>     self, *, run_id: str, expected_generation: int,
>     dispatch_id: str, actor_id: str,
> ) -> tuple[RunRecord, int, int, str, float]
>
> class OpenCodeProcessIdentityError(OpenCodeProcessError)
> ```
>
> Verification: 238 passed, 5 deselected in 31.29s. Ruff clean. `git diff
> --check` clean. No commit or branch created.

## Supervisor spot-check (independently verified, not just self-reported)

Performed directly against the repository immediately after the model
reported completion:

- `git log --oneline -3` → still at `72a4dea` (HEAD unchanged). **No commit
  was created.** Confirmed.
- `git status --porcelain` → 30 modified files + 3 untracked
  (`src/dispatcher/yaml_io.py`, `tests/fixtures/opencode/1.18.11/
  run-duplicate-key.jsonl` — both pre-existing from before this step — plus a
  new untracked `.opencode/` directory, see "Housekeeping" below).
- `git diff --stat HEAD` → matches the model's file list
  (`pyproject.toml +1`, `schemas/workflow-state-v1.json +13`,
  `src/dispatcher/sessions.py +66/-11`, `src/dispatcher/state_store.py
  +22/-4`, `src/dispatcher/workflow.py +6`, `src/dispatcher/cli.py +13/-6`,
  `src/dispatcher/sequential.py +2`, `src/dispatcher/execution.py +8/-4`, plus
  8 test files). `execution.py`'s change was not explicitly named in the
  original task prompt but is a legitimate, in-scope propagation of
  `process_create_time` through `SequentialExecutionCoordinator`'s
  `process_started` callback (confirmed by direct diff read).
- `git status --porcelain -- config/ state/` → empty. **No files under
  `config/` or `state/` were touched.** Confirmed.
- `grep -n "class OpenCodeProcessIdentityError" src/dispatcher/sessions.py`
  → present at line 84, subclassing `OpenCodeProcessError` as required.
- `grep -n "process_create_time" src/dispatcher/sessions.py` → captured via
  `psutil.Process(process.pid).create_time()` at line 642, immediately after
  spawn, and passed to `on_process_started(process.pid,
  process_create_time)` at line 654.
- `grep -n "raise OpenCodeProcessIdentityError" src/dispatcher/sessions.py`
  → present at line 819, inside `cancel_process_group`.
- `grep -n "def test_cancel_process_group_refuses_a_reused_pid_without_signalling"
  tests/unit/test_sessions.py` → present at line 364, and its body asserts
  `pytest.raises(OpenCodeProcessIdentityError, match="identity does not
  match")` (line 386). A dedicated PID-reuse test exists, matching the
  required acceptance criterion.
- Re-ran the full non-live suite independently:
  `.venv/bin/python -m pytest tests -q -m "not live_opencode"` →
  **238 passed, 5 deselected** (matches the model's reported count exactly).

## Housekeeping note (not part of the fix, flagged for cleanup)

A new untracked `.opencode/` directory (with its own `node_modules`,
`package.json`, and a `memory/2026-08-11.logfmt` file) appeared in the repo
root during this step. This is tooling/session residue from the harness used
to run the executing model, not part of the intended change. It is currently
untracked (won't be committed accidentally), but should be deleted
(`rm -rf .opencode`) before any future commit, or added to `.gitignore` if it
is expected to recur for future steps run through an OpenCode-compatible
harness.

## Verification status

The items above are **supervisor spot-checks**: they confirm the reported
file set, test count, exception type, and test existence are real, and that
no commit/config/state-directory violation occurred. They do **not**
constitute a correctness review of the PID-reuse-verification logic itself
(tolerance handling, `psutil.NoSuchProcess`/`AccessDenied` branches, the
internal-timeout-path exemption reasoning, or interaction with the escalation
ladder). That deeper verification is deferred to the final Claude Sonnet 5
review at the end of the remediation plan, per the standing project
instruction not to trust self-reports as proof.

## Outstanding from the original blocker

Closing B1 required, per the review, both an implementation fix and a
dedicated impostor-PID test — both are present per the checks above. This
step is considered functionally complete pending the final review's deeper
correctness pass.
