# Protocol Schema V1

The dispatcher accepts only schema-v1 machine contracts. Human prose may appear
in supplemental transcripts, evidence, and summaries, but it is never used to
authorize a session, advance a workflow step, accept work, or complete a run.

The executable schemas are generated from the Pydantic models in
`src/dispatcher/` and published under `schemas/`:

- `supervisor-command-v1.json`
- `executor-result-v1.json`
- `reviewer-result-v1.json`
- `workflow-state-v1.json`

## Supervisor commands

Each supervisor response must be exactly one JSON object, apart from surrounding
whitespace. The parser rejects Markdown fences, comments, duplicate keys,
unknown fields, a second object, and all trailing prose. The dispatcher does
not parse natural-language fallback commands.

Every command has `protocol_version: 1` and one `action`.

### Dispatch

```json
{
  "protocol_version": 1,
  "action": "dispatch",
  "step_id": "prepare-fixture",
  "target_role": "executor-role",
  "session_mode": "new",
  "repo_id": "application",
  "prompt": "Perform the approved task and return the schema-v1 executor result.",
  "rationale": "Optional explanation"
}
```

`target_role` must resolve to a configured executor or reviewer role. The
dispatcher determines role kind, repository, policy, and logical session from
the normalized plan and durable run state. `repo_id` is optional and, when
present, is only a consistency assertion: it must equal the normalized step's
repository ID and can never choose a working directory. A command cannot
include a raw OpenCode session ID, permission decision, or batch request.

### Ask Operator

```json
{
  "protocol_version": 1,
  "action": "ask_operator",
  "step_id": "prepare-fixture",
  "question": "Choose the approved fixture option.",
  "rationale": "The step cannot proceed without this decision."
}
```

The dispatcher persists the question before entering `WAITING_OPERATOR`.

### Halt

```json
{
  "protocol_version": 1,
  "action": "halt",
  "reason": "The source hash changed after plan approval."
}
```

### Request Completion

```json
{
  "protocol_version": 1,
  "action": "request_completion",
  "rationale": "All assigned work is believed complete."
}
```

This is a request, not an authority to end a run. The dispatcher allows
`SUCCEEDED` only after it evaluates every completion invariant.

## Executor results

Executor results are typed JSON objects discriminated by `outcome`:
`completed`, `blocked`, or `failed`. Every result includes:

- `result_version`, `dispatch_id`, `attempt`, and `step_id`;
- repository ID, base revision, and result revision or patch hash;
- evidence artifacts with content hashes;
- verification results;
- a concise summary; and
- an optional transcript reference.

`blocked` results require nonempty `blockers`. `failed` results require a stable
`failure_code`. The dispatcher rejects a result whose dispatch ID, attempt, step
ID, or repository ID differs from the active dispatch.

Before result acceptance, the dispatcher inspects the registered worktree
itself. Required evidence hashes and sizes must match its content manifest;
symlink evidence, changed external watch roots, changed repository identity,
or writes outside configured writable roots halt acceptance.

## Reviewer results

Reviewer results are typed JSON objects discriminated by `verdict`:
`accepted`, `changes_requested`, `blocked`, or `inconclusive`. Every result is
bound to an immutable review target containing the executor dispatch ID, attempt,
result revision or patch hash, and reviewed evidence hashes.

`accepted` cannot contain a blocking finding or required remediation.
`changes_requested` requires remediation. `blocked` requires blockers.
`inconclusive` requires a reason. A review result for any other immutable target
is retained only as supplemental evidence and cannot change step state.

## Protocol boundary

Schema validation alone does not execute work. Before a command can launch a
session, later phases validate the normalized plan step, dependency state,
repository lock, role eligibility, policy, session lineage, and durable
dispatch intent. The Phase 4 sequential facade implements these validations;
real OpenCode dispatch remains disabled until it is the sole execution path.
