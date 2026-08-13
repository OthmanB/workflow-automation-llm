# Step 10: Shared OpenCode Home for Resumed Worker Sessions

Date: 2026-08-11

## Scope

This change fixes worker OpenCode storage scoping only. It changes no plan,
permission, credential, result, or dispatcher transition behavior. It does not
touch protected local project/state directories, and all changes remain
uncommitted.

## Fix

`src/dispatcher/execution.py:194-198` now computes the worker state root with
`worker_opencode_state_dir(...)` instead of the unique dispatch UUID.
`worker_opencode_state_dir` at `src/dispatcher/execution.py:417-425` returns:

```
<config-state>/opencode-dispatches/<run-id>/<pool>/<session-registry-key>
```

The `<run-id>` segment keeps separate runs isolated. The pool/key portion is
the durable registry identity already used by the workflow.

To prevent the registry and OpenCode-home scopes from drifting, the new shared
`session_registry_identity(dispatch)` helper is defined at
`src/dispatcher/sequential.py:1963-1971` and is now used by the existing
session-binding path at `src/dispatcher/sequential.py:788-796` as well as the
worker-path helper. Its behavior exactly mirrors the prior registry logic:

- Non-batch dispatches use `executors` or `reviewers` plus `role_key`.
- Batch dispatches use `executors` or `reviewers` plus `logical_session_key`.

The rework executor therefore resolves the same path as its original attempt,
while a fresh dispatch UUID no longer incorrectly forces an empty OpenCode
database. `execute_worker` still passes the unchanged stable credential source
as `credential_state_dir=self.config.state_dir`
(`src/dispatcher/execution.py:215-216`), and keeps the same permission config,
snapshots, process lifecycle, and session ID arguments.

## Event Logs and Parameter Design

No `opencode_home_dir` parameter was added. `run_session` already treats
`state_dir` as the private child-environment root: `build_child_environment`
derives HOME and all XDG paths from it
(`src/dispatcher/sessions.py:538-588`), and event logs are written below the
same root (`src/dispatcher/sessions.py:358-366`). Reusing that root for an
already-shared registry session is the smallest safe design; a new parameter
would duplicate call-site plumbing while separating two values that must share
the same ownership lifetime for a resumed OpenCode session.

Sharing the event-log parent is safe. Each `run_session` call creates a fresh
UUID `run_token` (`sessions.py:359`) and writes distinct
`<run-token>.stdout.jsonl` and `<run-token>.stderr.log` files
(`sessions.py:360-361`). Attempts in one logical session are consequently
audit-grouped under one `opencode-events/` directory without filename
collisions. A repository-wide path search found only `execution.py` constructs
`opencode-dispatches`; no cleanup, retention, recovery-classification, or test
code assumes a dispatch-ID-keyed child directory. The only other event-path
references are the implementation and its permission-mode assertion in
`tests/unit/test_sessions.py`.

## Isolation Decision

Isolation is preserved by the resulting path dimensions:

- Different runs have different `<run-id>` directories.
- Executor and reviewer dispatches have different `<pool>` directories.
- Batch work for different steps has different `<logical_session_key>`
  directories.
- Sequential storage follows the existing authoritative `(pool, role_key)`
  session-registry contract exactly. Attempts sharing that registry identity
  are intentionally the only sequential attempts that share an OpenCode home;
  adding an independent path identity would allow the registry to approve a
  resume whose home could not contain that session.

This preserves the dispatcher's existing ownership boundary rather than
weakening it. The direct tests below cover run, role, and batch-step separation.

## Other `run_session` Call Sites

No analogous change was needed elsewhere:

- `src/dispatcher/preflight.py:246-259` invokes `run_session` with
  `mode="new"`, `session_id=None` for one-off model checks.
- `src/dispatcher/cli.py:391-405` invokes `run_session` with `mode="new"`,
  `session_id=None` inside a fresh `TemporaryDirectory` for each live-smoke
  proof.
- The existing loop and supervisor paths already use stable state roots for
  the sessions they resume; in particular the modern coordinator supervisor
  path was already correct.

Neither preflight nor the smoke-proof producer ever attempts a resume, so
changing their state paths would broaden the security/isolation change without
addressing this defect.

## Tests Added and Extended

New direct unit tests in `tests/unit/test_execution.py`:

- `test_worker_opencode_state_dir_reuses_home_for_sequential_rework`
  (`:58`) asserts two attempts of the same non-batch executor step resolve to
  the identical home root despite different dispatch IDs.
- `test_worker_opencode_state_dir_preserves_run_role_and_batch_step_isolation`
  (`:69`) asserts different run IDs, executor/reviewer roles, and different
  batch-step logical session keys resolve to distinct roots.

The existing fake-binary integration test
`test_fake_opencode_executes_narrated_rework_review_and_completion_in_disposable_git`
now records only the non-secret child HOME/XDG values in
`tests/fixtures/opencode/fake_cli.py:53-56` and asserts at
`tests/integration/test_sequential_git_e2e.py:110-129` that:

- the original executor and its resumed rework have identical complete
  HOME/XDG environments;
- the reviewer environment differs from the executor environment; and
- both HOME values match their expected run/pool/registry-key directory.

Focused verification: `3 passed` for the two new unit tests plus this
integration test. Syntax compilation and Ruff checks for every changed Python
file also passed.

Required full non-live suite final summary:

```
269 passed, 5 deselected in 39.93s
```

## Live Confirmation Required

This fix has **not** been confirmed against real OpenCode. No live OpenCode,
network, HTTP, or credential-backed command was run for this task. Structural
path and fake-binary environment coverage prove the dispatcher now presents a
stable HOME/XDG data directory to a rework resume, but a human must run the
following command with real credentials to definitively prove end-to-end
behavior:

```sh
DISPATCHER_LIVE_OPENCODE=1 DISPATCHER_REAL_DISPOSABLE=1 DISPATCHER_LIVE_MODEL=openai/gpt-5.6-terra pytest tests/live/test_real_operation_disposable.py -v -m live_opencode
```
