# Inspecting Dispatcher OpenCode Sessions

Dispatcher OpenCode sessions use isolated per-project HOME and XDG directories.
The session ID shown by `dispatcher status` or a run report is therefore not
visible to a normal `opencode -s <session-id>` command, which searches the
operator's ordinary OpenCode database.

The authoritative workflow state remains `state.directory/dispatcher.sqlite3`.
OpenCode databases, event logs, exports, and Markdown transcripts are supporting
inspection artifacts; never edit them to change workflow state.

## Locate The Session Home

For a sequential executor or reviewer, the run report lists a state root with
this shape:

```text
opencode-dispatches/<run-id>/executors/<role-key>
opencode-dispatches/<run-id>/reviewers/<role-key>
```

For a batch or parallel dispatch, the session registry uses the dispatch's
logical session key instead of the bare role key:

```text
opencode-dispatches/<run-id>/executors/executor-<role-key>-<step-id>
opencode-dispatches/<run-id>/reviewers/reviewer-<role-key>-<step-id>
```

The run report prints the exact registry key for every row; use that value
verbatim when locating the state root.

Its OpenCode home is:

```text
<state.directory>/<state-root>/opencode-child/home
```

The supervisor row reports state root `.` and uses:

```text
<state.directory>/opencode-child/home
```

Session IDs are case-sensitive. Copy the complete ID from the run report or
`dispatcher status`; do not retype it.

## Export A Session

Export is preferable for forensic inspection because it does not invite a new
model turn. Set every HOME/XDG location to the isolated session home:

```bash
OC_HOME="<absolute-session-state-root>/opencode-child/home"
SESSION_ID="<session-id>"

HOME="$OC_HOME" \
XDG_CONFIG_HOME="$OC_HOME/.config" \
XDG_CACHE_HOME="$OC_HOME/.cache" \
XDG_DATA_HOME="$OC_HOME/.local/share" \
XDG_STATE_HOME="$OC_HOME/.local/state" \
opencode --pure export "$SESSION_ID"
```

`--pure` avoids loading external plugins while inspecting stored content. The
session database is selected by `XDG_DATA_HOME`, not by the shell's current
directory.

## Open The Session TUI

Use the same isolated environment and pass the working directory shown in the
run report:

```bash
OC_HOME="<absolute-session-state-root>/opencode-child/home"
SESSION_ID="<session-id>"
WORKING_DIRECTORY="<absolute-repository-or-worktree-path>"

HOME="$OC_HOME" \
XDG_CONFIG_HOME="$OC_HOME/.config" \
XDG_CACHE_HOME="$OC_HOME/.cache" \
XDG_DATA_HOME="$OC_HOME/.local/share" \
XDG_STATE_HOME="$OC_HOME/.local/state" \
opencode --pure "$WORKING_DIRECTORY" --session "$SESSION_ID"
```

Opening the TUI or sending a message can update OpenCode session metadata and
may start a provider call. Prefer export when only reviewing history. Never send
new instructions into a completed dispatcher session unless a recovery
procedure explicitly requires it.

## Artifact Locations

| Artifact | Purpose |
|---|---|
| `dispatcher.sqlite3` | Authoritative run, dispatch, review, evidence, and recovery state |
| `transcripts/<run-id>/` | Dispatcher-written supervisor bootstrap and response transcripts |
| `<state-root>/opencode-events/*.stdout.jsonl` | Worker assistant/tool event stream |
| `<state-root>/opencode-events/*.stderr.log` | Bounded worker stderr |
| `<state-root>/opencode-child/home/.local/share/opencode/opencode.db` | Private OpenCode session database |
| `reports/run-<run-id>.md` | Derived human-readable run summary |

The JSONL event stream does not necessarily contain the original worker input.
The exact dispatcher-generated prompt is stored in the authoritative dispatch
payload and is visible when the OpenCode session is exported or reopened.

## Sensitive Data

The isolated tree can contain `auth.json`, provider metadata, raw prompts, model
responses, and repository content. It is ignored runtime state, not evidence to
commit or publish. Do not copy `auth.json`, OpenCode databases, or unsanitized
exports into issues, pull requests, support bundles, or repository evidence.
