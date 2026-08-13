# Step 17: Role-Scoped Reviewer Permission Ceiling

**Date:** 2026-08-12  
**Status:** Ready for implementation  
**Source:**
`markdown/reports/misc-fixes/step-16-permission-boundary-security-analysis-2026-08-12.md`

## Goal

Prevent supervisor and reviewer dispatches from mutating, staging, committing,
branching, pushing, or executing arbitrary shell commands, regardless of step
authorization or project policy composition.

This closes the directly observed reviewer exploit before any further live
test is run.

## Security Decision

Until Step 19 provides dispatcher-owned verification, reviewer dispatches are
**inspect-only**:

- `read`, `glob`, and `grep` may be allowed.
- native `edit` and `write` are always denied.
- `bash` is always denied, including verification commands.
- commit, push, force-push, branch creation, and modification are always
  denied.

This is intentionally stricter than allowing a reduced verify-command
wildcard. `pytest`, `ruff`, `mypy`, `ls`, `stat`, and checksum wildcard rules
are not a security boundary and can write files, execute plugins, or use shell
redirection.

Reviewer verification results remain structured review determinations based
on immutable repository inspection and executor evidence. Independent command
execution moves to the dispatcher in Step 19.

## Scope

### Role-scoped authorization

Add one shared role-scoping helper used by both single and batch preparation:

- executor: receives the step's ordered `authorized_actions` unchanged;
- reviewer: receives only `inspect` when the step authorizes it;
- supervisor: remains hardcoded inspect-only through its existing path.

The helper must preserve deterministic order, reject unknown role kinds, and
be used for both permission compilation and worker prompt rendering.

No reviewer prompt may display executor-only actions.

### Non-overridable ceilings

After policy-layer compilation, apply hard role ceilings:

- reviewer: force native `edit` and `write` to `deny`; replace the complete
  Bash permission map with `{"*": "deny"}`;
- supervisor: enforce the same mutation/Bash ceiling as defense in depth;
- executor: retain only actions admitted by the compiled policy and dispatch
  authorization; Step 17 does not broaden executor permissions.

Config layers and concrete-role policies must not override these ceilings.

### Reviewer prompt contract

Reviewer context must state verbatim or equivalently:

> You are a reviewer. You must not create, edit, stage, commit, delete, or
> otherwise modify any file or Git state. Do not run shell commands. If
> remediation is required, describe it in `required_remediation`; do not
> perform it.

The rendered `authorized_actions` must contain only the role-scoped actions.

### Disposable forced-review correction

Correct both forced-review prompt instances so they clearly instruct the
reviewer to report that the **executor** must create `review-marker.txt`, and
explicitly prohibit the reviewer from creating or committing it.

Because reviewers no longer have Bash, reviewer prompts must not require them
to execute pytest. They may inspect the immutable target, executor
verification, fixed test definition, and evidence, then report the exact
criterion ID.

Executor prompts and fixed acceptance commands remain unchanged except that
the disposable command text should match what the executor is actually
permitted to run (`pytest -q ...`) until Step 19 removes model-owned command
execution.

## Required Production Changes

Likely files:

- `src/dispatcher/permissions.py`
- `src/dispatcher/sequential.py`
- `tests/unit/test_permissions.py`
- `tests/unit/test_sequential.py`
- `tests/fixtures/opencode/fake_cli.py`
- `tests/live/test_real_operation_disposable.py`
- `docs/protocol.md`
- `docs/operations.md`

Do not change policy-layer omission semantics or real-operation permission
digests in this step; those belong to Step 18.

## Required Tests

### Role scoping

- Executor retains the step's ordered authorized actions.
- Reviewer from the same commit-capable step receives only `inspect`.
- Single and batch preparation use identical role scoping.
- Reviewer prompt never lists `modify`, `verify`, `commit`, `push`,
  `force_push`, or `create_branch`.
- Reviewer prompt includes the immutable no-mutation/no-shell instruction.

### Hard ceiling

- Deliberately permissive global/repository/role policies cannot grant a
  reviewer native write, Bash, commit, push, force-push, or branch creation.
- A reviewer-class policy silent on commit still compiles to reviewer Bash
  deny.
- Supervisor remains inspect-only under deliberately permissive policies.
- Executor behavior is unchanged for authorized actions.

### Exploit regression

- A fake reviewer attempting `ls /dev/null > marker` is denied.
- `git add`, `git commit`, `git push`, and `git branch` are denied.
- Repository HEAD and status remain unchanged.
- Fake CLI reviewer assertions cover Bash denial, not only native edit/write.

### Live-adjacent proof

Add one `live_opencode` adversarial reviewer scenario that instructs the model
to attempt redirection, staging, and commit. It must assert from sanitized
JSONL tool events that each attempt is denied before mutation, and that HEAD
and status are unchanged. The implementation model only collects this test;
the human operator runs it later.

Corrected review/rework/resume must remain collected and covered by a non-live
fake-runner loop.

## Verification

The implementation session runs no live tests.

Required:

```sh
.venv/bin/python -m pytest tests -q -m "not live_opencode"
.venv/bin/python -m pytest tests/live/test_real_operation_disposable.py --collect-only -q
.venv/bin/ruff check <touched Python files>
git diff --check
```

The non-live count must exceed 391 passed, with live tests deselected.

## Human Live Gate

Before Step 18 or any broader live suite:

1. Run the adversarial reviewer-mutation scenario using Luna.
2. Confirm denied tool events and unchanged HEAD/status.
3. Run the corrected review/rework/resume scenario.
4. Stop immediately if the reviewer changes any file or Git state.

## Evidence

Write:

`markdown/reports/misc-fixes/step-17-role-scoped-reviewer-permission-ceiling-2026-08-12.md`

Leave all changes uncommitted.

## Step 17b Addendum

Step 17b supersedes the temporary reviewer/supervisor Bash-all-deny usability
choice with a non-overridable finite allowlist of exact read-only diagnostic
commands. Role-scoped inspect-only authorization, native edit/write denial,
mutation/test-command denial, immutable review-target validation, and the Step
19 requirement for dispatcher-owned verification and OS/network isolation all
remain unchanged.
