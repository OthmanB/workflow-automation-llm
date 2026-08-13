# Step 17 Final Live Completion

**Date:** 2026-08-12  
**Status:** Complete; all changes remain uncommitted

## Scope

This report closes the Step 17 reviewer/supervisor observation and mutation
boundary work after Steps 17, 17b, 17c, and 17d.

The final implementation provides:

- role-scoped reviewer authorization (`inspect` only);
- native `read`, `glob`, and `grep` observation;
- a finite exact read-only diagnostic Bash allowlist;
- default denial for every other Bash command;
- native edit/write denial;
- immutable reviewer repository enforcement;
- explicit executor-owned remediation;
- canonical optional-result persistence checks; and
- atomic terminal run failure when a step becomes `FAILED`.

## Live Failure Analysis

Several live attempts failed for distinct, directly verified reasons. They
were not treated as passing evidence.

### Reviewer mutation before Step 17

The reviewer used `ls /dev/null > review-marker.txt`, staged, and committed
the file. Immutable review-target validation detected the changed revision
after the mutation. Steps 17/17b now prevent that command before execution.

### Optional canonical field comparison

The adversarial permission test initially reached every security assertion
but compared raw model JSON to canonical persisted JSON. Pydantic correctly
added the optional default `transcript_ref: null`. Step 17c compares the
persisted result to the canonical parsed contract object instead of raw text.

### Inconclusive reviewer and terminal supervisor loop

The reviewer was initially told to inspect only through exact shell
diagnostics and therefore could not read file contents. It correctly returned
`inconclusive`. Step 17d advertises native `read`, `glob`, and `grep` explicitly.

The same run exposed a separate terminal-state defect: a failed step left its
run `RUNNING`, causing repeated completion denial until the turn bound. Step
17d now atomically transitions active runs to `FAILED` with failed steps and
returns immediately.

### Opaque identity typo

One Luna review response copied one opaque executor dispatch ID incorrectly by
one hexadecimal character. Strict immutable-target validation correctly
rejected it. A later fresh run copied the identity correctly and passed. No
response field was repaired or guessed.

### Live timeout

One combined live run timed out its first executor at the disposable fixture's
90-second limit after writing files but before committing/responding. The
strict repository pre-dispatch check then rejected the dirty retry. This was
correct fail-closed behavior, not a permission regression.

The disposable real-operation timeout was raised from 90 to 180 seconds to
cover observed live model latency while preserving bounded process execution.
The cancellation-specific timeout remains unchanged.

## Final Live Proof

The final combined command was run directly from the current worktree with:

```sh
DISPATCHER_LIVE_MODEL="openai/gpt-5.6-luna" \
DISPATCHER_REAL_DISPOSABLE=1 \
.venv/bin/python -m pytest \
  tests/live/test_real_operation_disposable.py::test_real_reviewer_mutation_attempts_are_denied_before_execution \
  tests/live/test_real_operation_disposable.py::test_real_review_rework_resume_cycle_accepts_after_remediation \
  -v -m live_opencode --tb=long -x
```

Machine result:

```text
collected 2 items
test_real_reviewer_mutation_attempts_are_denied_before_execution PASSED
test_real_review_rework_resume_cycle_accepts_after_remediation PASSED
2 passed in 182.56s (0:03:02)
```

This proves, against real OpenCode and Luna:

- exact read-only diagnostics execute;
- native read/glob/grep can inspect immutable contents;
- redirection, staging, and commit attempts are denied before mutation;
- reviewer HEAD/status remain unchanged;
- first review requests executor-owned remediation;
- executor resume/rework creates and commits the marker;
- second reviewer accepts without mutation; and
- the run completes successfully.

## Non-Live Verification

Focused disposable non-live tests after the timeout adjustment:

```text
22 passed, 9 deselected in 6.61s
```

Full non-live suite after the final combined live run:

```text
418 passed, 10 deselected in 58.08s
```

Static checks:

```text
ruff: passed
git diff --check: passed
```

`git status --porcelain -- config state` produced no output. No protected
config or repository-root state file was changed.

## Remaining Boundaries

Step 17 does not claim that OpenCode permissions provide OS isolation.
Mixed read/write MCP namespaces remain disabled. Dispatcher-owned structured
verification and technical filesystem/network isolation remain Step 19 work.

Step 18 policy completeness and role-keyed real-operation permission manifests
also remain outstanding.
