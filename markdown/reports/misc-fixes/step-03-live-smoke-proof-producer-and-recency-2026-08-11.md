# Step 3 - Live Smoke-Proof Producer and Recency Check

**Date:** 2026-08-11  
**Status:** Implemented, uncommitted, for human review.  
**Scope:** Added the missing real live-smoke proof producer and rejected stale
or timezone-naive proofs. The cancellation, revision-gate, post-SIGKILL
verification, and duplicate-key changes already present in the working tree
were not modified or weakened.

## What Changed

### Fix 1 - Real smoke-proof producer

The new `dispatcher smoke-proof` command is registered at
`src/dispatcher/cli.py:114-119` and dispatched at `src/dispatcher/cli.py:734`.
Its usage string is:

```text
dispatcher smoke-proof --config <project.yaml> --model <model-id> --output <path>
```

Its full argument list is:

- `--config` (required): project YAML configuration path.
- `--model` (required): exact model identifier to use for the smoke call.
- `--output` (required): destination for the private JSON proof artifact.

The command handler at `src/dispatcher/cli.py:395-419` checks
`DISPATCHER_LIVE_OPENCODE == "1"` before loading configuration or invoking a
runner. Otherwise it prints
`smoke-proof: set DISPATCHER_LIVE_OPENCODE=1 to run the live OpenCode smoke suite`
to stderr and returns `2`.

The injectable producer is `src/dispatcher/cli.py:334-392`. It uses the real
`dispatcher.sessions.run_session` by default and otherwise accepts a fake
callable. It creates separate `TemporaryDirectory` instances for the empty
workdir and adapter state directory, then makes the same harmless call as the
existing live test: the fixed read-only prompt, deny-by-default permission
configuration, pinned timeout values, no session ID, and a new session. It
passes the workdir to `snapshot_dirs` and trims the returned response before
constructing `LiveSmokeProof` at `src/dispatcher/cli.py:363-375`.

Successful proofs are written with `model_dump_json` and
`atomic_write_private_text` at `src/dispatcher/cli.py:393`. A result that does
not pass the required response/session/workdir/version expectations exits
non-zero and overwrites the requested output with an explicit `passed=false`
proof, including when the marker was present but another expectation failed.
The producer never writes a `passed=true` proof for an unmet expectation. If
the runner raises before returning a `SessionResult`, no proof is written.

### Fix 2 - Smoke-proof freshness

`src/dispatcher/operation.py:41` defines the self-contained threshold
`LIVE_SMOKE_PROOF_MAX_AGE_SECONDS = 1800` (30 minutes). After the existing
smoke-proof matching checks, `src/dispatcher/operation.py:117-120` rejects a
naive or otherwise timezone-less `completed_at` with a clear
`timezone-aware` error, and rejects proofs older than the threshold with:

```text
live smoke proof is stale; rerun the live smoke test
```

No `Config` or `PreflightDefinition` field was added.

## Function Signatures

The new public producer signature is:

```python
def produce_live_smoke_proof(
    config: Config,
    *,
    model: str,
    output: str | Path,
    run_session: Callable[..., SessionResult] = real_run_session,
) -> LiveSmokeProof
```

The new module-private CLI handler signature is:

```python
def _cmd_smoke_proof(
    args: argparse.Namespace,
    *,
    run_session: Callable[..., SessionResult] = real_run_session,
) -> int
```

`validate_real_operation_prerequisites` keeps its existing signature; the
freshness logic is internal to that function and introduces no new public
configuration parameter.

## Tests and Verification

New tests:

- `tests/unit/test_cli.py:26` - `test_smoke_proof_command_refuses_without_live_environment`
- `tests/unit/test_cli.py:63` - `test_smoke_proof_command_writes_successful_proof`
- `tests/unit/test_cli.py:109` - `test_smoke_proof_command_rejects_nonmatching_result`
- `tests/unit/test_cli.py:138` - `test_smoke_proof_command_reports_runner_failure_without_proof`
- `tests/unit/test_operation.py:204` - `test_real_operation_rejects_stale_live_smoke_proof`
- `tests/unit/test_operation.py:218` - `test_real_operation_accepts_recent_live_smoke_proof_for_freshness_gate`
- `tests/unit/test_operation.py:232` - `test_real_operation_rejects_naive_live_smoke_proof_timestamp`

The exact required command was run from the repository root:

```text
.venv/bin/python -m pytest tests -q -m "not live_opencode"
```

Exact final pytest summary line:

```text
248 passed, 5 deselected in 33.10s
```

Step 2 reported `241 passed, 5 deselected`, so this result is strictly higher
by seven passing tests. Focused lint and diff checks were also clean:
`.venv/bin/ruff check` passed for all four touched source/test files and
`git diff --check` returned no output.

## Snapshot Cleanliness and Deviations

`run_session` snapshots each path in `snapshot_dirs` before the child process
and calls its existing `_new_evidence` mechanism afterward. That mechanism
reports newly present files through `SessionResult.evidence_written`. The
producer sets:

```python
workdir_clean = result.evidence_written == []
```

There is no separate unexpected-change field on `SessionResult`, and no second
directory-diff implementation was added. Thus the empty `evidence_written`
result from `snapshot_dirs=[workdir]` is the existing session-layer evidence
that the smoke workdir had no newly detected files. A separate temporary state
directory was used so adapter event logs are outside the snapshotted workdir;
the workdir itself remains fresh and empty when the runner starts.

## Constraint Confirmation

- No commit, push, or branch was created. `git log --oneline -1` remains
  `72a4dea feat: enforce exact worker response contracts`.
- `config/projects/local/`, `config/state/`, and `state/` were untouched;
  `git status --porcelain -- config/projects/local/ config/state/ state/`
  returned no entries.
- No real OpenCode or network call was made at any point. The environment was
  not set to `DISPATCHER_LIVE_OPENCODE=1`; all producer tests injected fake
  runners, and the full verification suite excluded `live_opencode` tests.
- No credentials were used.
- The pre-existing root `.opencode/` directory was ignored and untouched.

## Supervisor spot-check (independently verified, not just self-reported)

- `git log --oneline -3` → still at `72a4dea`. No commit created. Confirmed.
- `git status --porcelain -- config/ state/` → empty. Confirmed untouched.
- `git diff --stat HEAD -- src/dispatcher/operation.py src/dispatcher/cli.py
  src/dispatcher/preflight.py` → 3 files, 153 insertions/9 deletions.
  Consistent with a producer command + freshness-check change set.
- Read `src/dispatcher/sessions.py:357,424,888-899` directly to verify the
  `workdir_clean = result.evidence_written == []` claim is not a shortcut:
  confirmed `_new_evidence` performs a genuine before/after set-difference
  directory diff (`_snapshot_dir(directory) - before[directory]`) driven by
  `snapshot_dirs`, and `evidence_written` in `SessionResult` is populated
  directly from that diff (line 424/429). The producer's reliance on this
  existing mechanism is accurate, not invented.
- Confirmed `SUPPORTED_OPENCODE_VERSION` is imported from `sessions.py`
  (line 29 there, `"1.18.11"`) rather than duplicated as a new constant —
  good reuse.
- Read `src/dispatcher/operation.py:95-120` directly: confirmed the
  tz-aware guard (`smoke.completed_at.tzinfo is None or
  smoke.completed_at.utcoffset() is None`) runs **before** the age
  arithmetic, so a naive timestamp cannot reach the subtraction and produce
  a confusing `TypeError` — matches the required behavior exactly.
- Confirmed no new field was added to `Config`/`PreflightDefinition` (kept
  as a self-contained module constant in `operation.py`), avoiding the
  `config.preflight is None` edge case as instructed.
- Re-ran the full non-live suite independently:
  **248 passed, 5 deselected** — matches the model's reported count exactly
  (241 → 248, +7).

No corrections needed to the model's self-report for this step. The gate now
requires a *machine-produced* proof (no first-party way to hand-author
`passed=true` without actually invoking a real OpenCode call), which is the
core requirement of the original blocker. Deeper review (e.g., whether the
fixed 30-minute freshness window is the right operational value, whether a
constant vs. a configurable threshold is preferable long-term) is deferred to
the final Sonnet 5 review.
