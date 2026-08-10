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
