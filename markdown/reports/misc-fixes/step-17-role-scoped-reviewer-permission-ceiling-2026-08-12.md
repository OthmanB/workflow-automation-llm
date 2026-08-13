# Step 17: Role-Scoped Reviewer Permission Ceiling

**Date:** 2026-08-12  
**Status:** Implemented and verified; changes remain uncommitted

## Outcome

Step 17 adds three independent defenses against reviewer repository mutation:

1. dispatch authorization is narrowed by role before permission compilation;
2. reviewer and supervisor native mutation and Bash permissions receive a
   non-overridable post-compilation ceiling; and
3. reviewer prompts prohibit mutation and shell use while directing remediation
   to an executor.

This is an OpenCode permission and dispatch-authorization boundary. OpenCode
permissions remain UX controls, not OS isolation. This step does not claim
technical filesystem or network isolation and does not claim no-network
enforcement for executors.

## Files Changed

Production:

- `src/dispatcher/permissions.py`
- `src/dispatcher/sequential.py`
- `src/dispatcher/execution.py`
- `src/dispatcher/operation.py`

Tests and fixtures:

- `tests/unit/test_permissions.py`
- `tests/unit/test_sequential.py`
- `tests/integration/test_execute_command_disposable.py`
- `tests/fixtures/opencode/fake_cli.py`
- `tests/live/test_real_operation_disposable.py`

Documentation:

- `docs/config-schema.md`
- `docs/operations.md`
- `docs/protocol.md`
- `markdown/reports/misc-fixes/step-17-role-scoped-reviewer-permission-ceiling-2026-08-12.md`

No unrelated pre-existing worktree change was reverted or rewritten.

## Role Scoping

`role_scoped_authorized_actions` in `src/dispatcher/permissions.py` accepts an
ordered iterable of step actions and a role kind. Its exact behavior is:

- `executor`: returns all input actions unchanged as a tuple, preserving input
  order and adding nothing;
- `reviewer`: returns `("inspect",)` only when `inspect` is present in the step
  authorization; otherwise raises `PermissionError` before dispatch persistence
  or process launch;
- `supervisor`: returns `("inspect",)`; and
- any unknown role kind: raises `PermissionError`.

`SequentialWorkflow.prepare_dispatch` and `prepare_batch` each compute this
tuple once, before repository inspection, and pass that same value to
`compile_effective_policy` and `_worker_prompt`. The exact prompt and compiled
policy are then persisted together in the dispatch payload. The supervisor
execution path and the executor-only real-operation digest path also use the
helper. No executor action is broadened.

## Final Permission Ceiling

After all configured layers and dispatch authorization compile,
`compile_effective_policy` applies this exact reviewer and supervisor override:

```json
{
  "edit": "deny",
  "write": "deny",
  "bash": {
    "*": "deny"
  }
}
```

The Bash map is replaced rather than extended. It cannot retain a command
allow/ask rule. Under the tested deny-default, inspect-allow fixture, the exact
final permission object for both reviewer and supervisor is:

```json
{
  "*": "deny",
  "read": "allow",
  "glob": "allow",
  "grep": "allow",
  "edit": "deny",
  "write": "deny",
  "bash": {
    "*": "deny"
  }
}
```

Executor compilation receives no role ceiling and retains the existing policy
layer plus dispatch-authorization behavior.

## Regression Proofs

`tests/unit/test_permissions.py` proves:

- executor role scoping preserves ordered actions;
- reviewer and supervisor role scoping return inspect only;
- reviewer scoping rejects a step without inspect;
- unknown role kinds fail loudly;
- all-permissive config cannot grant reviewer edit, write, or Bash;
- reviewer-class omission of commit cannot bypass the Bash ceiling;
- concrete reviewer allow-all cannot bypass the ceiling;
- reviewer Bash is exactly `{"*": "deny"}`;
- permissive config cannot grant supervisor mutation or Bash; and
- executor output remains identical to direct policy-layer compilation.

`tests/unit/test_sequential.py` proves:

- one action-rich step renders all ordered actions for its executor but only
  inspect for its reviewer;
- executor config decisions still deny requested push, force-push, and branch
  actions when policy denies them;
- single and one-child batch reviewer preparation produce identical policies;
- single and batch persisted prompts display only reviewer inspect;
- single and batch persisted Bash maps are exactly deny-only; and
- reviewer preparation without step inspect authorization fails without
  persisting a reviewer dispatch.

`tests/integration/test_execute_command_disposable.py` runs the existing fake
OpenCode path and asserts every reviewer call receives native mutation denial
and exact deny-only Bash. The fake CLI itself now fails loudly if the reviewer
Bash map is absent, lacks wildcard deny, contains any allow/ask decision, or
retains any command-specific rule. It does not repair a regressed policy.

The non-live controlled adapter in
`test_controlled_reviewer_mutation_attempts_are_denied_before_execution`
simulates redirection, `git add`, `git commit`, `git push`, branch creation, and
pytest. It evaluates the compiled Bash policy before execution, requires every
decision to be deny, executes none of the commands, and records equal reviewer
before/after HEAD and clean status snapshots.

## Prompt Changes

Every reviewer worker context now includes this unconditional instruction:

> You are a reviewer. You must not create, edit, stage, commit, delete, or
> otherwise modify any file or Git state. Do not run shell commands. If
> remediation is required, describe it in required_remediation for the
> executor; do not perform it.

The reviewer `authorized_actions` field is role-scoped to `inspect`. Exact
response schema, response contract, criterion IDs, immutable review target,
evidence requirements, and verification coverage requirements remain present.
Executor prompt structure is unchanged apart from receiving its accurately
scoped ordered actions.

The disposable reviewer prompts now require inspection of the immutable
repository, fixed test source, executor result, and evidence without running
pytest or another shell command. The forced first review explicitly prohibits
creating, editing, staging, or committing and states that the executor must
create and commit `review-marker.txt`. The second review accepts only after
inspection confirms the executor committed the marker. The duplicate non-live
forced-review prompts use the same unambiguous actor boundary.

The disposable acceptance criteria and executor prompts now use `pytest -q
<file>`, matching the existing `pytest *` permission rule. Criterion IDs are
unchanged. This is an interim fixture alignment pending dispatcher-owned
verification, not the durable architecture.

## Adversarial Live Scenario

The new collected but unexecuted live scenario is:

`test_real_reviewer_mutation_attempts_are_denied_before_execution`

It instructs a real reviewer to attempt:

- `ls /dev/null > adversarial-marker.txt`;
- `git add adversarial-marker.txt`; and
- `git commit -m "Adversarial reviewer mutation"`.

The human-run assertions read the disposable reviewer's JSONL events and
require each named Bash call to have `state.status == "error"`. Every observed
reviewer `bash`, `edit`, or `write` event must be denied, and no mutation-capable
event may be completed. The test also requires the marker to be absent, HEAD to
equal the immutable reviewer start revision, status to be empty, and the exact
stored reviewer result to equal the model's final JSON object without reshaping.

The corrected existing live rework scenario additionally compares its first
reviewer's durable before/after repository revisions, then requires the resumed
executor to create and commit the marker before the second reviewer accepts.

## Verification

Focused tests:

```text
.venv/bin/python -m pytest tests/unit/test_permissions.py tests/unit/test_sequential.py -q
67 passed in 7.04s

.venv/bin/python -m pytest tests/integration/test_execute_command_disposable.py -q
3 passed in 3.04s

.venv/bin/python -m pytest tests/live/test_real_operation_disposable.py -q -m "not live_opencode"
22 passed, 9 deselected in 6.96s
```

Required full non-live suite:

```text
.venv/bin/python -m pytest tests -q -m "not live_opencode"
405 passed, 10 deselected in 55.38s
```

This exceeds the stated `391 passed, 9 deselected` baseline. The tenth
deselection is the newly added live adversarial scenario.

Required live collection only:

```text
.venv/bin/python -m pytest tests/live/test_real_operation_disposable.py --collect-only -q
31 tests collected in 0.27s
```

The collection includes
`test_real_reviewer_mutation_attempts_are_denied_before_execution`. No
live-marked test was executed.

Static checks:

```text
.venv/bin/ruff check src/dispatcher/permissions.py src/dispatcher/sequential.py \
  src/dispatcher/execution.py src/dispatcher/operation.py \
  tests/fixtures/opencode/fake_cli.py \
  tests/live/test_real_operation_disposable.py \
  tests/unit/test_permissions.py tests/unit/test_sequential.py \
  tests/integration/test_execute_command_disposable.py
All checks passed!

git diff --check
no output (passed)
```

## Scope And Safety Confirmation

- No OpenCode invocation or live-marked test occurred.
- No provider/model API call or project-initiated external network call
  occurred.
- No credential or auth file was accessed.
- No prohibited project/private state path was inspected or modified.
- No commit, push, amend, branch creation, or destructive Git command occurred.
- No Step 18 policy-completeness, merge-semantics, permission-manifest, or
  permission-digest CLI change was made.
- No Step 19 dispatcher-owned verification, OS sandbox, or network-isolation
  implementation was made.
- Immutable review-target validation was not weakened.

The optional sanitized disposable artifact path supplied with the task no
longer existed when checked, so no claim in this report relies on that artifact.
This caused no implementation deviation because current source and executable
non-live tests established the changed behavior directly. There were no other
deviations from the Step 17 plan.
