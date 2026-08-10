# Disposable Real Operation Report

**Execution date:** 2026-08-11
**Scope:** Step 5 readiness proof using only temporary synthetic repositories.
No T2 repository or production project was used.

## Results

- Sequential scenario passed with a real OpenCode executor and reviewer:
  file edit, evidence file, Git commit, typed result validation, review
  acceptance, completion, and clean repository state.
- Cross-repository bounded batch passed with two real executor sessions running
  concurrently. Both synthetic repositories were edited, committed, validated,
  and left clean.
- Same-repository worktree barrier passed with two real executor sessions in
  separate temporary worktrees. Child branches integrated in order and all
  temporary worktrees/branches were removed.
- Cancellation/recovery passed with a real OpenCode child. The dispatcher
  interrupted the managed process, preserved a durable recovery disposition, and
  left the synthetic repository unchanged.
- Each concurrent OpenCode child now has isolated runtime state while using a
  separate dedicated credential source, preventing OpenCode database locks.
- Worker prompts now include configured evidence roots, reducing ambiguity about
  where required evidence files must be written.

## Important Result

The real model did perform the disposable file edits and Git commits. In some
runs its final chat response was a short natural-language success response
rather than the required schema-v1 result object. The disposable harness then
validated the actual files, evidence, and commit and shaped a typed result for
the workflow test. This proves real repository mutation and integration, but it
does **not** close the strict model-response gate for T2. Production real
operation must reject such a response, so the next readiness action is to make
the real model reliably return the strict typed result directly (or add a
provider-supported structured-output mechanism) before T2 execution.

## Safety

- The disposable suite is gated by `DISPATCHER_REAL_DISPOSABLE=1` and is skipped
  in ordinary CI.
- Credential values were not printed or stored in the repository.
- No real project, deployment, cluster, network service, push, pull request, or
  private baseline was used.
- The guarded `dispatcher execute` command was not invoked for this proof.

## Verification

```text
Disposable live scenarios: 4 passed
Public suite: 228 passed, 1 skipped
Ruff: passed
Mypy: passed
Dependency audit: passed
```

The next step is the separately approved first real T2.2a operation, not broad
real-operation enablement.
