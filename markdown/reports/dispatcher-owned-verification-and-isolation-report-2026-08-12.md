# Dispatcher-Owned Verification And Isolation Report

**Date:** 2026-08-12  
**Status:** Phase A implemented; macOS local verification backend accepted

## Decision

The accepted architecture is recorded in:

`markdown/decisions/dispatcher-verification-isolation-decision-2026-08-12.md`

The dispatcher owns structured acceptance checks and their durable results.
OpenCode permissions remain defense in depth rather than an OS security
boundary.

## Implemented

### Normalized plan schema v2

Every acceptance criterion now requires one `VerificationCheck` containing:

- argv array;
- repository working-directory identity;
- timeout;
- output bound;
- expected exit codes; and
- mandatory `network_policy: deny`.

Shell command strings, inherited network, empty argv, and duplicate/empty exit
codes are invalid. The published schema is now
`schemas/normalized-plan-v2.json`; schema-v1 normalized plans are intentionally
unsupported.

### Dispatcher-owned verification runner

`src/dispatcher/verification.py` provides:

- `AuthoritativeVerification` records;
- bounded subprocess execution with `shell=False`;
- fresh disposable repository copies;
- controlled HOME/TMPDIR/environment;
- pytest plugin/cache/bytecode suppression;
- timeout process-group termination;
- output limits;
- stdout/stderr/transcript SHA-256 values;
- redacted bounded failure summaries; and
- explicit isolation backends.

### macOS local isolation

`darwin_seatbelt_v1` is the supported local macOS verification backend. It
denies network and writes outside the disposable workspace/root for
verification subprocesses. Machine tests prove a socket bind and an external
file write are denied. The real-operation gate accepts it when
`/usr/bin/sandbox-exec` is available.

### Production isolation interface

`linux_bwrap_v1` builds a bubblewrap command with:

- all namespaces unshared;
- network namespace unshared;
- disposable `/workspace` bind;
- read-only runtime/system roots;
- no host HOME mount;
- private `/tmp` and `/tmp/home`; and
- controlled PATH.

The backend fails closed when Linux or `bwrap` is unavailable. It could not be
executed on the current macOS host; command construction and fail-closed
availability are tested.

### Workflow authority

Successful executor and reviewer results trigger dispatcher-owned checks before
state advancement. The dispatcher requires:

- exact criterion/check coverage;
- all authoritative statuses passed; and
- no disagreement between model self-report and dispatcher status.

Authoritative records persist in the SQLite dispatch payload separately from
model JSON and are included in forwarding. State schema migration v6 adds
`authoritative_verification_json`.

Reviewer prompts receive the executor's authoritative records and never run
tests. Executor `verify` semantic authorization is removed. Executors receive
only exact evidence hash/size diagnostic commands for declared evidence paths;
wildcard test-runner verification remains denied.

Explicit `mock_workflow_test` mode uses deterministic synthetic authoritative
records so direct workflow unit tests remain isolated from real subprocesses.
Real-operation mode has no such fallback.

### Real-operation gate

`dispatcher execute` now requires an available verification backend.
Unavailable `darwin_seatbelt_v1` or `linux_bwrap_v1` fails before OpenCode
launch.

## Tests

New coverage includes:

- strict argv/network schema validation;
- pass/fail/timeout/output-bound records;
- real macOS network denial;
- real macOS external-write denial;
- Linux bwrap namespace/mount command construction;
- unavailable backend fail-closed behavior;
- SQLite schema migration v6;
- durable authoritative verification persistence;
- executor/reviewer disagreement rejection;
- schema-v2 export equality; and
- production gate rejection of development isolation.

## Verification

Focused verification/state/execute/schema suite:

```text
58 passed in 7.76s
```

Full non-live suite:

```text
440 passed, 10 deselected in 64.46s
```

Live collection:

```text
31 tests collected in 0.23s
```

Initial disposable real OpenCode proof under the supported macOS backend:

```text
test_real_sequential_disposable_repository_operation PASSED
test_real_reviewer_mutation_attempts_are_denied_before_execution PASSED
test_real_review_rework_resume_cycle_accepts_after_remediation PASSED
3 passed in 285.35s
```

After removing executor test-runner permission and adding exact evidence-only
diagnostics, the same three scenarios passed again:

```text
3 passed in 208.98s
```

Final complete disposable suite after adopting `darwin_seatbelt_v1` as the
macOS local backend:

```text
9 passed, 22 deselected in 494.13s (0:08:14)
```

The completed scenarios cover sequential execution, cross-repository batch,
same-repository worktree barrier, cancellation, review/rework/resume, reviewer
mutation denial, solo reconciliation, batch reconciliation, and halt.

Static/package checks:

```text
ruff: passed
git diff --check: passed
pip check: passed
```

## Deliberately Incomplete

Step 19 remains incomplete in one important trust-boundary area:

1. Executors still create their own Git commits because the current strict
   executor result contract requires the resulting revision and evidence
   metadata. Moving commit ownership requires a versioned executor proposal
   contract and dispatcher-generated authoritative result; this was not
   silently emulated or repaired.
2. OpenCode parent filesystem confinement and provider-only egress have not
been machine-proven.
3. Exact method-level MCP capability manifests remain unimplemented; MCP stays
disabled.
4. The full disposable matrix has not yet been run with the intended
heterogeneous T2 model matrix.

## Release Consequence

The macOS host now satisfies the accepted local verification isolation gate.
The first real T2.2a operation remains **NO-GO** only until executor commit
authority is converted from wildcard model Bash to a structured,
repository-scoped dispatcher Git capability and the intended model matrix is
proven.

Required closure:

- implement the versioned executor proposal/dispatcher commit transition;
- prove the structured commit boundary on macOS; and
- rerun the complete disposable suite from a committed revision using the
  intended model matrix.
