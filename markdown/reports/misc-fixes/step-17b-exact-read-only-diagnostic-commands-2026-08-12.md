# Step 17b: Exact Read-Only Diagnostic Commands

**Date:** 2026-08-12  
**Status:** Implemented and verified; changes remain uncommitted

## Outcome

Step 17b replaces the temporary reviewer/supervisor Bash-all-deny usability
choice with a hardcoded finite exact-command allowlist. Step 17 role-scoped
inspect-only authorization, native edit/write denial, remediation ownership,
immutable review targets, and executor behavior remain unchanged.

This allowlist is an OpenCode permission control, not an OS sandbox. It does not
make arbitrary Bash safe and does not provide technical filesystem or network
isolation. Dispatcher-owned verification and isolation remain Step 19 work.

## Files Changed

Production:

- `src/dispatcher/permissions.py`
- `src/dispatcher/sequential.py`

Tests and fixtures:

- `tests/unit/test_permissions.py`
- `tests/unit/test_sequential.py`
- `tests/integration/test_execute_command_disposable.py`
- `tests/fixtures/opencode/fake_cli.py`
- `tests/live/test_real_operation_disposable.py`

Documentation and plans:

- `docs/config-schema.md`
- `docs/operations.md`
- `docs/protocol.md`
- `markdown/plans/step-17-role-scoped-reviewer-permission-ceiling-plan-2026-08-12.md`
- `markdown/reports/misc-fixes/step-17b-exact-read-only-diagnostic-commands-2026-08-12.md`

The Step 17 evidence report was read as the baseline and was not modified. It
records `405 passed, 10 deselected` for the final Step 17 non-live suite.

## Authoritative Commands

`src/dispatcher/permissions.py` defines the only production command source:

```python
READ_ONLY_DIAGNOSTIC_COMMANDS = (
    "pwd",
    "ls",
    "git status --porcelain=v1",
    "git branch --show-current",
    "git rev-parse HEAD",
    "git diff --no-ext-diff --no-textconv",
)
```

`read_only_diagnostic_bash_rules()` builds a fresh deny-first map from this
tuple. `compile_effective_policy` replaces the complete reviewer/supervisor Bash
map with that helper after all configuration and dispatch layers compile.
`_worker_prompt` renders the same tuple into reviewer `diagnostic_commands`.
There is no second production command list that can drift.

The exact intended properties are:

- `pwd`: reports the current working directory;
- `ls`: lists the current directory without caller-controlled paths/options;
- `git status --porcelain=v1`: reports repository state deterministically;
- `git branch --show-current`: reports the current branch without creating one;
- `git rev-parse HEAD`: reports the current revision; and
- `git diff --no-ext-diff --no-textconv`: reads the worktree diff while
  disabling external diff and text-conversion drivers.

No `git log`, `git show`, test runner, interpreter, dynamic-path hash command,
or caller-controlled argument pattern was added.

## Final Bash JSON

The final reviewer and supervisor Bash map is exactly:

```json
{
  "*": "deny",
  "pwd": "allow",
  "ls": "allow",
  "git status --porcelain=v1": "allow",
  "git branch --show-current": "allow",
  "git rev-parse HEAD": "allow",
  "git diff --no-ext-diff --no-textconv": "allow"
}
```

No allowed pattern contains `*` or `?`. The sole wildcard is the default
`"*": "deny"`. Configurable global, repository, role-class, concrete-role,
and step authorization cannot add, remove, ask for, or override a Bash command
because the entire map is replaced after compilation. Reviewer/supervisor
native `edit` and `write` remain `deny`. Executor policy is not passed through
this ceiling and is unchanged.

## Serialization Ordering Proof

`opencode_config_env()` serializes with `sort_keys=True`. The focused test
`test_opencode_environment_serialization_preserves_safe_bash_rule_order`
proves repeated output is byte-identical, JSON round-tripping is byte-stable,
and the parsed Bash key order is:

```text
*
git branch --show-current
git diff --no-ext-diff --no-textconv
git rev-parse HEAD
git status --porcelain=v1
ls
pwd
```

Therefore the wildcard deny is emitted before every exact allow under
OpenCode's last-matching-rule precedence. The proof covers serialized
`OPENCODE_CONFIG_CONTENT`, not only Python source insertion order.

## Allowed And Denied Tests

`test_read_only_diagnostic_bash_rules_are_exact_and_deny_unlisted_commands`
asserts that only the six authoritative complete strings are allowed and all of
these complete command strings resolve to the fallback deny because none is an
exact allow key:

```text
ls -la
ls .
ls > marker.txt
ls >> marker.txt
ls | tee marker.txt
ls && git status --porcelain=v1
pwd > marker.txt
git status
git status --short
git status --porcelain=v1 > marker.txt
git branch
git branch new-branch
git branch --delete branch-name
git rev-parse --show-toplevel
git rev-parse HEAD > marker.txt
git diff
git diff --stat
git diff --no-ext-diff --no-textconv > marker.txt
pytest -q
python -m pytest
ruff check
mypy .
git add file
git commit -m message
git push
```

The test does not implement a shell parser. It proves the generated map contains
only the approved exact keys, has no wildcard allow, and uses the deny fallback
for every listed non-key command.

The permissive reviewer and supervisor tests set every policy layer and step
authorization to allow every semantic action, including permissive defaults,
then require the exact final map above. Reviewer-class omission and concrete
reviewer allow-all regressions also remain covered.

## Prompt Synchronization

Reviewer context now includes:

```json
{
  "diagnostic_commands": [
    "pwd",
    "ls",
    "git status --porcelain=v1",
    "git branch --show-current",
    "git rev-parse HEAD",
    "git diff --no-ext-diff --no-textconv"
  ]
}
```

The reviewer instruction permits only these exact strings and prohibits added
arguments, redirection, pipes, chaining, command substitution, any other shell
syntax, and all file/Git mutation. Required remediation still belongs to the
executor. Reviewer `authorized_actions` remains exactly `["inspect"]`; verify,
modify, commit, push, force-push, and branch creation are not re-enabled.

`tests/unit/test_sequential.py` compares the prompt list directly to
`READ_ONLY_DIAGNOSTIC_COMMANDS`, compares persisted single/batch policy maps to
`read_only_diagnostic_bash_rules()`, and verifies executors receive neither a
diagnostic field nor a changed policy.

The supervisor uses a different markdown bootstrap and durable continuation
path. Step 17b leaves that response protocol unchanged: its permissions include
the exact diagnostics, but its prompt does not advertise a new structured
field. This is the explicit task-permitted exception, not a security deviation.

## Fake CLI

The fake reviewer now requires:

- native `edit` and `write` deny;
- wildcard Bash deny as the first parsed rule;
- exact equality with the finite diagnostic map;
- no mutation or test command among allowed keys; and
- exact equality between reviewer prompt `diagnostic_commands` and the expected
  diagnostic list.

It fails loudly and does not repair policy. The disposable execute integration
test also verifies every fake reviewer call receives the exact production map.

## Adversarial Scenarios

The controlled non-live reviewer adapter now requires every exact diagnostic
command to evaluate to `allow`, while redirection, `git add`, `git commit`, `git
push`, branch creation, and pytest evaluate to `deny`. No denied command is
executed, and every reviewer before/after HEAD and status snapshot remains
unchanged and clean.

The collected live scenario
`test_real_reviewer_mutation_attempts_are_denied_before_execution` now instructs
the reviewer to run every exact diagnostic command and requires each JSONL Bash
event to complete. It then requires `ls /dev/null > adversarial-marker.txt`,
`git add adversarial-marker.txt`, and `git commit -m "Adversarial reviewer
mutation"` to have error status. Every unlisted Bash or native edit/write event
must be denied, no mutation event may complete, the marker must be absent, HEAD
and status must be unchanged, and stored result JSON must equal the model's
final object without reshaping. This live-marked test was collected only.

## Verification

Focused tests:

```text
.venv/bin/python -m pytest tests/unit/test_permissions.py tests/unit/test_sequential.py -q
69 passed in 7.47s

.venv/bin/python -m pytest tests/integration/test_execute_command_disposable.py -q
3 passed in 3.20s

.venv/bin/python -m pytest tests/live/test_real_operation_disposable.py -q -m "not live_opencode"
22 passed, 9 deselected in 7.39s
```

Required full non-live suite:

```text
.venv/bin/python -m pytest tests -q -m "not live_opencode"
407 passed, 10 deselected in 56.78s
```

This increases by two passes over the final Step 17 baseline of `405 passed, 10
deselected`.

Required live collection only:

```text
.venv/bin/python -m pytest tests/live/test_real_operation_disposable.py --collect-only -q
31 tests collected in 0.24s
```

No live-marked test was executed.

Static checks:

```text
.venv/bin/ruff check src/dispatcher/permissions.py src/dispatcher/sequential.py \
  tests/unit/test_permissions.py tests/unit/test_sequential.py \
  tests/fixtures/opencode/fake_cli.py \
  tests/integration/test_execute_command_disposable.py \
  tests/live/test_real_operation_disposable.py
All checks passed!

git diff --check
no output (passed)
```

## Scope And Safety Confirmation

- No OpenCode invocation or live-marked test occurred.
- No credential or auth file was accessed.
- No provider/model API call or project-initiated external network call
  occurred.
- No prohibited project/private state path was inspected or modified.
- No commit, push, amend, branch creation, or destructive Git command occurred.
- Role-scoped reviewer authorization remains inspect-only and still fails
  without step inspect authorization.
- Reviewer verify, modify, commit, push, force-push, and branch semantic actions
  were not re-enabled.
- Executor permissions were not changed.
- Step 18 and Step 19 plans were not modified.
- The Step 17 evidence report was not rewritten.

The only task-permitted deviation from advertising `diagnostic_commands` to
both roles is the supervisor prompt: its separate bootstrap/continuation
protocol was intentionally preserved, while its compiled permission map uses
the same exact hard ceiling. There were no other deviations.
