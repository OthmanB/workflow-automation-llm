# Step 17d: Observation Capabilities And Terminal Failure

**Date:** 2026-08-12  
**Status:** Implemented and verified; all changes remain uncommitted

## Outcome

Step 17d gives reviewers one authoritative observation contract, renders the
same constraints in the supervisor Markdown bootstrap, and makes every durable
failed step terminal for its run in the same state-store save. The Step 17/17b
reviewer and supervisor mutation ceilings remain intact.

## Observation Contract

`src/dispatcher/permissions.py` defines both production sources:

```python
READ_ONLY_NATIVE_TOOLS = ("read", "glob", "grep")
READ_ONLY_DIAGNOSTIC_COMMANDS = (
    "pwd",
    "ls",
    "git status --porcelain=v1",
    "git branch --show-current",
    "git rev-parse HEAD",
    "git diff --no-ext-diff --no-textconv",
)
```

The `inspect` permission mapping is generated from
`READ_ONLY_NATIVE_TOOLS`. Reviewer prompt rendering and supervisor bootstrap
rendering consume both constants. Reviewer worker context contains exactly:

```json
{
  "observation_tools": {
    "native": ["read", "glob", "grep"],
    "diagnostic_commands": [
      "pwd",
      "ls",
      "git status --porcelain=v1",
      "git branch --show-current",
      "git rev-parse HEAD",
      "git diff --no-ext-diff --no-textconv"
    ],
    "mcp": []
  }
}
```

There is no separate top-level `diagnostic_commands` field. Executor context is
unchanged and does not receive `observation_tools`.

## Prompt Changes

Reviewer instructions now distinguish the two observation mechanisms:

- native `read`, `glob`, and `grep` inspect contents and locate files;
- exact shell diagnostics inspect only current-directory, branch, revision,
  status, and diff metadata;
- added shell arguments, redirection, chaining, pipes, substitutions, and other
  shell syntax are prohibited;
- tests and interpreters are not permitted;
- file and Git mutation remain prohibited; and
- required remediation is reported for an executor.

The supervisor's Markdown bootstrap has an Observation Capabilities section
rendered from the same constants. It preserves the existing schema-v1 JSON
response protocol while prohibiting target-repository writes and reviewer-like
test execution.

Reviewer reports remain dispatcher-owned durable reviews, transcripts, and
reports outside the immutable target repository. Reviewers and supervisors do
not create report files in target repositories.

## MCP Deferral

No MCP server or tool was enabled. Step 19 must implement exact method-level
capability manifests, credential/data scope, and external-network policy before
dispatcher workers can use Context7, GitHub, Playwright, Repomix, or Semble.

An entire mixed read/write MCP namespace is never a valid reviewer read grant.
Playwright is not intrinsically read-only. GitHub write, merge, comment, and
update methods must never enter reviewer read capability. Repomix
generation/write methods must be separated from read, pack, and query methods.
User OpenCode/MCP configuration is not copied into isolated worker HOME/XDG
directories.

## Corrected Rework Prompts

The live and non-live disposable prompts now direct the first reviewer to use
native inspection on the immutable result, evidence, and fixed test source,
return `changes_requested`, and require the executor to create and commit
`review-marker.txt`. The reviewer is explicitly forbidden from performing that
remediation.

The second reviewer is directed to use native inspection on
`review-marker.txt`, `result.txt`, `evidence/real-evidence.md`, and
`test_real_output.py`; use exact diagnostics only for HEAD/status metadata; rely
on executor verification rather than running pytest; and accept when immutable
contents satisfy the criterion. The deterministic fake reviewer reads those
files and rejects missing or incorrect contents. The fake full-loop test wraps
reviewers with before/after HEAD and status snapshots to prove no reviewer
mutation.

The adversarial mutation prompt remains adversarial. It still requires all
exact diagnostics to complete, all redirection/staging/commit attempts to be
denied, and no repository mutation.

## Terminal Failure Invariant

`SequentialWorkflow._replace_step` is the centralized authoritative boundary.
When it receives a failed step it:

1. replaces the step in the run snapshot;
2. transitions a `RUNNING` or `WAITING_OPERATOR` run to `FAILED` with a
   dispatcher event whose sequence immediately follows the step event;
3. clears any operator request through `transition_run` terminal semantics; and
4. persists both changes in one `StateStore.save_run` transaction and one new
   generation.

An already `FAILED` run is not transitioned again. A newly failed step is
rejected rather than persisted into `NEW`, `READY`, `HALTED`, `SUCCEEDED`, or
`CANCELLED`.

Centralization is safe because every production failed-step creator is reached
only while applying an active executor/reviewer dispatch or an active dispatch
stall. `_replace_step` also serves non-failure updates, so its explicit state
guard prevents any future pre-activation caller from creating an invalid
failed-step/run combination.

`execution.stall_policy.on_exhausted` now accepts explicit `fail`. That action
transitions the exhausted blocked/review-required step to `FAILED`; the shared
boundary then fails the run atomically. Existing `ask` and `halt` behavior is
unchanged.

## FAILED Path Audit

Every current `StepStatus.FAILED` creator in `src/dispatcher/sequential.py` was
audited:

- executor `failed` result with terminal `on_failed` policy;
- executor `blocked` result through `_retry_or_terminal_step` after retry/policy
  exhaustion;
- reviewer `changes_requested` after rework/executor-attempt exhaustion or a
  terminal rework policy;
- reviewer `blocked` after reviewer retry/policy exhaustion;
- reviewer `inconclusive`, which uses the same terminal blocked-policy path;
- explicit stall exhaustion configured with `on_exhausted: fail`; and
- `_retry_or_terminal_step`, the shared executor blocked-result creator.

All these paths persist through `_replace_step`. No other source path writes a
failed step.

## Coordinator Stop

After worker application returns a `FAILED` run, `run_to_completion()` takes its
existing non-`RUNNING` worker-policy branch and returns a non-accepted
`CompletionDecision` immediately. A typed durable-step check in the
completion-denied branch provides defense in depth for an inconsistent legacy
snapshot. Neither path parses obligation strings.

The coordinator test uses `max_turns=1`, observes exactly one supervisor call
for the initial dispatch, receives no `completion_denied` prompt, returns the
non-accepted decision, and does not raise the max-turn exception.

## New Tests

- `test_read_only_native_tools_are_exact_and_compile_as_inspection_only`
- `test_executor_terminal_failure_makes_run_failed`
- `test_executor_blocked_policy_exhaustion_makes_run_failed`
- `test_reviewer_changes_requested_retry_exhaustion_makes_run_failed`
- `test_reviewer_blocked_retry_exhaustion_makes_run_failed`
- `test_reviewer_inconclusive_result_makes_run_failed`
- `test_failed_step_and_run_persist_atomically_with_monotonic_sequence`
- `test_stall_exhaustion_configured_to_fail_makes_run_failed`
- `test_run_to_completion_returns_immediately_after_terminal_worker_failure`

Existing prompt, permission-ceiling, schema, fake-loop, and adversarial tests
were strengthened without weakening their prior assertions. In particular,
`test_bootstrap_is_self_contained_and_persisted`,
`test_reviewer_prompt_is_inspect_only_and_directs_remediation_to_executor`,
`test_single_and_batch_dispatches_apply_identical_reviewer_role_ceiling`, and
`test_review_rework_resume_full_loop_with_fake_runner` cover the corresponding
observation and no-mutation contracts.

## Verification

Focused production, schema, fake-runner, and disposable non-live tests:

```text
170 passed, 9 deselected in 20.52s
```

Required full non-live suite:

```text
418 passed, 10 deselected in 59.16s
```

This exceeds the `409 passed, 10 deselected` baseline by nine passing tests.

Required live-file collection only:

```text
31 tests collected in 0.28s
```

The collection includes
`test_real_reviewer_mutation_attempts_are_denied_before_execution` and
`test_real_review_rework_resume_cycle_accepts_after_remediation`.

Ruff on all touched Python files:

```text
All checks passed!
```

Repository whitespace validation:

```text
git diff --check
# no output; exit 0
```

## Safety And Deviations

No live-marked test, OpenCode live session, provider/model API, network MCP,
credential, auth file, or prohibited state was accessed. No commit, push,
amend, or branch operation was performed. Existing uncommitted Step 17/17b/17c
and unrelated work was preserved.

The only implementation extension beyond prompt/state handling is the explicit
`stall_policy.on_exhausted: fail` configuration value and generated schema enum,
which is required to prove the requested configured-to-fail stall invariant.
There were no other deviations.
