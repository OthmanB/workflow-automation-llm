# Step 2 — Expected-Revision Gate, SIGKILL Verification, and Decisions Duplicate-Key Protection

**Date:** 2026-08-11
**Executing model:** deepseek-v4-flash (fresh session, repo tool access)
**Scope:** Three independent defects in `src/dispatcher/`; the PID-reuse
cancellation work from the prior step (step-01) was present in the working
tree and was **not** modified or weakened.
**Status:** Implemented, uncommitted, for human review.

## What changed and why, per fix

### Fix 1 — Expected/pinned revision gate for `dispatcher execute`

`validate_real_operation_prerequisites` recorded `snapshot.revision` in the
returned audit dict but never compared it to anything, so `dispatcher execute`
would launch against ANY commit as long as the worktree was clean and on the
expected branch. The fix follows the exact `permission_digest` /
`stall_policy_digest` pattern already in the same function: the operator
supplies the expected value as an explicit CLI argument and the function
verifies it against a freshly computed ground-truth value with exact string
equality, failing closed on mismatch.

- `src/dispatcher/cli.py:83` — new required `--expected-revision` argument on
  the `execute` parser, same style as `--permission-digest` /
  `--stall-policy-digest`.
- `src/dispatcher/operation.py:67` — new required keyword-only parameter
  `expected_revision: str` on `validate_real_operation_prerequisites`.
- `src/dispatcher/operation.py:96-97` — the new check, placed immediately
  after `snapshot = inspect_repository(...)` (alongside the existing
  branch/clean checks; no unrelated checks reordered):
  ```python
  if snapshot.revision != expected_revision:
      raise RealOperationError("repository is not at the expected revision")
  ```
- `src/dispatcher/cli.py:254` — `_cmd_execute` reads `args.expected_revision`
  and passes it through.
- `docs/operations.md:38` (command block) and `docs/operations.md:45`
  (required-conditions bullet paragraph) — document the expected-revision
  check in the existing style.
- Existing calls in `tests/unit/test_operation.py` were updated to supply the
  new required parameter (both existing tests fail on earlier checks anyway;
  this is a deliberate signature-adjustment, not a weakened assertion).

### Fix 2 — Verify SIGKILL actually terminated the process before reporting success

`cancel_process_group` sent SIGKILL as its last escalation step and then
returned `True` unconditionally. If the target process survived (e.g.
uninterruptible I/O wait), the function reported success anyway — a false
positive an operator could rely on.

- `src/dispatcher/sessions.py:864-871` — after sending SIGKILL, a bounded
  `time.monotonic()`-based deadline loop (same style as the existing
  post-SIGINT wait in the same function: `os.kill(process_id, 0)` +
  `time.sleep(0.05)`) polls for up to `grace_seconds`. The process is
  confirmed gone via `ProcessLookupError` → `True`; if it survives the full
  window, the function now returns `False` (no raise), consistent with the
  existing "did not confirm termination" contract used by the caller
  (`_cmd_cancel` prints `process_stopped=False`).
- The post-SIGINT poll window of `grace_seconds` is the function's existing
  polling convention; reusing the same window for the post-SIGKILL check is
  the most literal match to the in-code convention (the task explicitly
  allowed "up to grace_seconds").
- All existing PID-reuse identity-verification logic before each signal
  (`_process_identity_matches` calls) is unchanged.
- The internal `_terminate_process_group` (Popen-based timeout cleanup) is
  untouched.

### Fix 3 — Duplicate-key protection for the `--decisions` baseline-approval JSON file

`_cmd_baseline` was the only remaining external, human-authored,
integrity-relevant JSON input parsed with plain `json.loads`. A duplicate key
in a decision object (e.g. two `"state"` fields, one `WAIVED` one `PENDING`)
was silently resolved last-key-wins — which governs whether a step's baseline
review is waived.

- `src/dispatcher/cli.py:546-551` — new module-private helper
  `_reject_duplicate_decisions_keys(pairs)` following the established
  per-module pattern (the same shape as `execution._reject_duplicate_keys`,
  `protocol._reject_duplicate_object_keys`, and
  `sessions._reject_duplicate_json_keys`), raising
  `BaselineError("duplicate JSON key in decisions file: <key>")`.
- `src/dispatcher/cli.py:592-594` — the `json.loads` call site now passes
  `object_pairs_hook=_reject_duplicate_decisions_keys`.
- Behavior for well-formed decisions files is unchanged.
- **Judgment call:** the task preferred reusing an existing helper "if one is
  written generically enough". The three existing helpers are functionally
  generic but module-private (`_`-prefixed), each raising a module-specific
  exception type (`OpenCodeProtocolError`, plain `ValueError`,
  `DuplicateKeyError`). Cross-importing a private name from `execution` or
  `protocol` into the CLI would pull a heavy dependency chain into cli.py's
  module imports and bypass the codebase's own convention of one small
  private helper per module (three copies already exist). Adding the fourth,
  local, `BaselineError`-raising helper matches the established convention
  exactly and keeps the error type specific to baseline CLI errors (which the
  task explicitly suggested: "e.g. via the existing BaselineError").

## Public function signatures changed

```python
# src/dispatcher/operation.py — new required keyword-only parameter
def validate_real_operation_prerequisites(
    *,
    config: Config,
    store: StateStore,
    record: RunRecord,
    plan_path: str | Path,
    repo_id: str,
    smoke_proof_path: str | Path,
    smoke_model: str,
    permission_digest: str,
    stall_policy_digest: str,
    expected_revision: str,          # NEW
    approval_ref: str,
    confirm: bool,
) -> dict[str, Any]

# src/dispatcher/sessions.py — return semantics unchanged (bool), but the
# False case is now reachable after SIGKILL: previously the post-SIGKILL path
# always returned True; it now returns True only once the process is confirmed
# gone via ProcessLookupError, and False when it survives the full poll window.
def cancel_process_group(
    process_id: int,
    expected_host: str,
    grace_seconds: int,
    expected_create_time: float,
) -> bool

# src/dispatcher/cli.py — new module-private helper (same per-module pattern
# as execution._reject_duplicate_keys, protocol._reject_duplicate_object_keys,
# sessions._reject_duplicate_json_keys)
def _reject_duplicate_decisions_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]
```

## New tests and final suite result

New tests (one per fix):

- `tests/unit/test_operation.py:84` —
  `test_real_operation_gates_on_the_expected_revision` — proves a mismatched
  `--expected-revision` raises `RealOperationError` ("repository is not at
  the expected revision") before any process could launch, and that a
  matching revision passes this specific check (the run then fails later at a
  still-missing prerequisite — the live smoke proof — proving the check does
  not silently accept a wrong revision). The fixture's `inspect_repository`
  and `validate_approved_baseline` are mocked so the test targets the new
  check in isolation.
- `tests/unit/test_sessions.py:395` —
  `test_cancel_process_group_confirms_sigkill_termination` — mocks a process
  that survives SIGINT/SIGTERM/SIGKILL (`os.kill(pid, 0)` keeps succeeding
  through the whole post-SIGKILL window) and asserts `cancel_process_group`
  returns `False` after sending the full SIGINT → SIGTERM → SIGKILL ladder;
  then mocks a process that exits immediately after SIGKILL and asserts
  `True` is still returned. The pre-existing
  `test_cancel_process_group_interrupts_and_escalates` (process exits at
  SIGINT → `True`) is kept unchanged and still passes.
- `tests/unit/test_cli.py:176` —
  `test_baseline_approve_rejects_duplicate_decision_keys` — writes a
  decisions JSON file with an intentionally duplicated `"state"` key and
  asserts the command exits `2` with
  "duplicate JSON key in decisions file: state" on stderr instead of silently
  accepting one of the two values.

Final pytest summary line (run from repo root):

```
241 passed, 5 deselected in 32.84s
```

(Step-01 baseline was 238 passed, 5 deselected — exactly +3 new tests.)

## Constraints verification

- **No commit, no push, no branch:** `git log --oneline -1` still shows
  `72a4dea feat: enforce exact worker response contracts`; all changes remain
  in the working tree.
- **`config/projects/local/`, `config/state/`, `state/` untouched:**
  `git status --porcelain -- config/projects/local/ config/state/ state/`
  returns zero entries.
- **PID-reuse cancellation logic untouched:** no changes to
  `_process_identity_matches` or any pre-signal identity verification; the
  only change in `cancel_process_group` is the post-SIGKILL polling loop.
- **No live OpenCode / network / credentials used:** only the non-live suite
  (`-m "not live_opencode"`) was executed.
- **`git diff --check` clean; ruff clean** on all touched files.
- `.opencode/` directory in the repo root left untouched (pre-existing tooling
  residue, per instructions).

## Deviations / judgment calls

1. **Fix 3 helper:** added a new local `_reject_duplicate_decisions_keys`
   rather than importing an existing private helper from `execution.py` /
   `protocol.py` — see rationale under Fix 3. The pattern is identical to the
   three existing helpers.
2. **Post-SIGKILL window:** used the full `grace_seconds` (the function's
   existing polling convention for the post-SIGINT wait) rather than a
   fraction; worst-case escalation latency is bounded to ~2× grace but the
   function now fails closed instead of reporting a false positive.
3. **Existing tests updated for the new required parameter:** the two
   pre-existing `test_operation.py` tests now pass a placeholder
   `expected_revision` value; both fail on earlier, unrelated checks, so no
   assertion was weakened — this is a mechanical signature update.

## Supervisor spot-check (independently verified, not just self-reported)

Performed directly against the repository after the model reported
completion:

- `git log --oneline -3` → still at `72a4dea`. **No commit created.**
  Confirmed.
- `git status --porcelain -- config/ state/` → empty. Confirmed untouched.
- `git diff --stat HEAD -- src/dispatcher/operation.py src/dispatcher/cli.py
  src/dispatcher/sessions.py docs/operations.md` → 4 files, 112
  insertions/21 deletions, consistent with a three-fix, four-touched-file
  change set.
- Read `src/dispatcher/sessions.py:808-871` in full (not just the reported
  post-SIGKILL slice). Confirmed the identity check
  (`_process_identity_matches`) now runs before **every** escalation stage
  (before SIGINT at line 834, before SIGTERM at line 847, before SIGKILL at
  line 858), each correctly distinguishing three outcomes: process gone
  (`False`, no raise), identity matches (`True`, proceed), identity mismatch
  (raises `OpenCodeProcessIdentityError`). The new post-SIGKILL confirmation
  loop (lines 864-871) composes correctly with this without altering it —
  no regression to the step-01 PID-reuse logic found.
- Read `src/dispatcher/cli.py:22,546-551,592-594` directly: confirmed
  `BaselineError` is imported at **module level** (line 22), so the
  module-level `_reject_duplicate_decisions_keys` helper (which is not
  nested inside `_cmd_baseline` and cannot see that function's local
  imports) correctly resolves the name — this was checked specifically
  because it is an easy mistake to make with this pattern, and it was done
  correctly here.
- Confirmed `expected_revision: str` at `operation.py:67` and the exact
  fail-closed comparison at `operation.py:96-97` as reported.
- Confirmed all three new test functions exist at the reported locations
  (`test_operation.py:84`, `test_sessions.py:395`, `test_cli.py:176`) and ran
  the duplicate-decisions-key test individually (`1 passed`) plus the full
  non-live suite independently: **241 passed, 5 deselected** — matches the
  model's reported count exactly (238 → 241, +3).

No corrections needed to the model's self-report for this step. Deeper
correctness review (e.g., whether `--expected-revision` should ideally be
derived automatically from baseline/step state rather than operator-supplied,
whether the post-SIGKILL escalation latency bound of ~2× `grace_seconds` is
acceptable) is deferred to the final Sonnet 5 review, per standing policy of
not treating any in-progress report as proof.
