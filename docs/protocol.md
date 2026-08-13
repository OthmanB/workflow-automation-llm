# Protocol Schemas

The dispatcher accepts only versioned machine contracts. Human prose may appear
in supplemental transcripts, evidence, and summaries, but it is never used to
authorize a session, advance a workflow step, accept work, or complete a run.

The executable schemas are generated from the Pydantic models in
`src/dispatcher/` and published under `schemas/`:

- `supervisor-command-v1.json`
- `executor-proposal-v2.json`
- `executor-result-v1.json`
- `reviewer-result-v1.json`
- `workflow-state-v1.json`

## Supervisor commands

Each supervisor response must be exactly one JSON object, apart from surrounding
whitespace. The parser rejects Markdown fences, comments, duplicate keys,
unknown fields, a second object, and all trailing prose. The dispatcher does
not parse natural-language fallback commands.

Single-step commands use `protocol_version: 1`. The only protocol-v2 command is
the explicit all-or-none batch request described below.

### Dispatch

```json
{
  "protocol_version": 1,
  "action": "dispatch",
  "step_id": "prepare-fixture",
  "target_role": "executor-role",
  "session_mode": "new",
  "repo_id": "application",
  "prompt": "Perform the approved task and return the executor proposal-v2 object.",
  "rationale": "Optional explanation"
}
```

`target_role` must resolve to a configured executor or reviewer role. The
dispatcher determines role kind, repository, policy, and logical session from
the normalized plan and durable run state. `repo_id` is optional and, when
present, is only a consistency assertion: it must equal the normalized step's
repository ID and can never choose a working directory. A command cannot
include a raw OpenCode session ID or permission decision.

Before permission compilation, the dispatcher scopes the step's ordered
authorization to the selected role. Executors retain ordered step actions
except model-owned `verify`, which is removed; no action is added. Reviewers receive only `inspect`, and a reviewer dispatch
fails before launch when the step does not authorize `inspect`. Supervisor
turns are independently scoped to `inspect`. Single and batch preparation use
the same scoped value for the worker prompt, compiled policy, and durable
dispatch payload.

The semantic permission vocabulary is fixed to `inspect`, `modify`, `verify`,
`commit`, `push`, `force_push`, and `create_branch`. `commit` authorizes only the
dispatcher-owned structured capability; it never creates a model Git allow
rule. Every configured
role-class policy must explicitly decide every action; omission is invalid
rather than implicit inheritance. Global, project, repository, role-class, and
concrete-role layers use ordered override precedence, after which dispatch
authorization narrows actions and the reviewer/supervisor hard ceilings apply.

Before a real operation is approved or launched, the dispatcher compiles a
role-keyed permission manifest for the first executable step. It includes the
supervisor, every eligible executor, and every reviewer in the compiled review
obligation. Approval and execute independently require the exact role set and
permission digests; a missing, added, or changed role fails before launch.

Normalized plan schema v2 binds every acceptance criterion to a dispatcher-
owned argv check. Commands execute with `shell=False`, a fixed repository
working directory, explicit timeout/output/exit-code bounds, and
`network_policy: deny`. Successful results advance only when authoritative
dispatcher records cover the exact criterion IDs and all pass. Model
verification remains an exact-ID self-report; disagreement fails the worker
boundary and is never repaired.

Authoritative check records persist separately from model JSON and are included
in forwarding. Reviewer prompts receive the executor's dispatcher-generated
records and do not run tests themselves.

### Dispatch Batch

```json
{
  "protocol_version": 2,
  "action": "dispatch_batch",
  "children": [
    {
      "step_id": "prepare-a",
      "target_role": "executor-a",
      "session_mode": "new",
      "prompt": "Perform the approved independent task."
    },
    {
      "step_id": "prepare-b",
      "target_role": "executor-b",
      "session_mode": "new",
      "prompt": "Perform the second approved independent task."
    }
  ]
}
```

Every child must be independently valid and target a distinct step. The
dispatcher validates dependencies, inputs, capacities, fresh session mode,
repository/resource locks, and all child policies before it persists or starts
any child. Batch workers receive independent dispatch IDs under one durable
batch ID. Same-repository parallel work requires `worktree_barrier`, distinct
child worktrees, and non-overlapping writable scopes; otherwise it is rejected.

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

### Review Waiver

`request_review_waiver` is available only for a `REVIEW_REQUIRED` step whose
compiled review obligation is non-mandatory and explicitly waivable. The
operator receives a durable `waive` or `halt` choice; an accepted waiver keeps
the executor evidence and records the operator decision reference. Mandatory
plan or project review cannot be waived.

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

## Operator Requests

The dispatcher, not the supervisor, creates nine typed requests. Each request
persists allowed answers, context, expiration, required role, and intended
resume state. `dispatcher answer` validates and applies one explicit transition;
no kind falls through to its stored resume state.

| Kind | Answers | Result |
|---|---|---|
| `risk_gate` | `approve`, `deny` | Resolve the step gate and resume, or halt. |
| `escalation` | `reassign`, `halt` | Return the blocked step to executor `READY` with the configured role, or halt. |
| `review_waiver` | `waive`, `halt` | Accept a waivable `REVIEW_REQUIRED` step with the decision reference, or halt. |
| `stall_recovery` | `retry`, `halt` | Retry a blocked executor step or redispatch review for a review-required step, or halt. |
| `underspecification` | `answer`, `halt` | Acknowledge with the fixed token and resume, or halt. No free-form answer is carried. |
| `budget` | `halt` | Halt explicitly. |
| `reconciliation` | `reconcile`, `halt` | Attest to one failed/abandoned dispatch, make its blocked step retryable while preserving review boundaries, or halt unchanged. |
| `batch_reconciliation` | `reconcile`, `halt` | Validate failed children, make each affected step retryable once while preserving accepted siblings and the historical `FAILED` batch, or halt unchanged. |
| `workspace_reconciliation` | `reconcile`, `halt` | Complete non-force durable owned-workspace cleanup before resuming, or halt without cleanup. |

Workspace cleanup occurs outside the SQLite answer transaction because it has
Git side effects. The CLI persists `CLEANUP_PENDING` before deletion and records
the answer only after the group reaches `CLEANED`. Cleanup failure leaves the
run in `WAITING_OPERATOR` with the same active request and no operator decision.
Solo and batch reconciliation are operator attestations, not substitutes for
the repository identity and cleanliness checks performed before every later
process launch.

## Durable Forwarding Continuation

Worker forwarding and supervisor acknowledgement are separate durable
transitions. After activation or reopening, the sequential coordinator first
recovers every durable `COMPLETED` dispatch by reconstructing its forwarding
from the stored result, authoritative verification, and dispatch state, then
selects only dispatches still in `FORWARDED`, ordered by their forwarding
transition event sequence and then dispatch ID. `PREPARED`, `RUNNING`,
`ACKNOWLEDGED`, `FAILED`, and `ABANDONED` dispatches are not replayed.
Completion persistence is atomic: the result payload, step state, forwarding,
review row, and structured Git final state commit in one transaction, so a
crash cannot strand a durable result without its forwarding.

The next supervisor turn receives one dispatcher-owned JSON envelope:

```text
{
  "kind": "orchestration_resume",
  "bootstrap": "<complete rendered bootstrap>",
  "pending_forwardings": [
    {
      "dispatch_id": "dispatch-example",
      "payload": {
        "kind": "executor_result",
        "dispatch_id": "dispatch-example"
      }
    }
  ]
}
```

Keys and pending entries are serialized deterministically. Each payload is the
authoritative sanitized forwarding stored in SQLite, parsed as exactly one JSON
object. Missing, empty, malformed, duplicate-key, wrong-identity, and
role-inconsistent payloads fail closed before supervisor delivery, and no
pending forwarding is acknowledged when this validation fails. The envelope
does not reconstruct pre-redaction content or include worker prompts, stderr,
credentials, or private environment data. With no pending forwarding, the
original bootstrap prompt is preserved exactly.

The dispatcher transitions those IDs to `ACKNOWLEDGED` only after the
supervisor turn returns successfully, then refreshes readiness and parses the
returned command. A supervisor-call failure leaves every forwarding pending for
a later continuation. A crash after supervisor receipt but before durable
acknowledgement can replay the same forwarding, so delivery is at least once;
the dispatch ID is the idempotency identity. Once acknowledged, a forwarding
does not appear in later resume envelopes. Command parsing happens after
acknowledgement because that transition records successful delivery, not command
validity.

## Executor proposals

Executors return proposal-v2 JSON objects discriminated by `outcome`:
`completed`, `blocked`, or `failed`. Every proposal includes:

- `response_contract: "dispatcher.executor_proposal.v2"` exactly;
- `proposal_version`, `dispatch_id`, `attempt`, and `step_id`;
- repository ID and base revision only;
- evidence declarations without hashes or sizes;
- exact ordered criterion self-reports, each with `status: "not_run"`;
- a concise summary; and
- an optional transcript reference.

The response must be one JSON object, with no prose or Markdown fences. Required
strings cannot be empty. The dispatcher first parses JSON and then validates the
full schema; valid JSON with missing or incorrect fields is rejected.

`blocked` proposals require nonempty `blockers`. `failed` proposals require a
stable `failure_code`. The dispatcher rejects a proposal whose dispatch ID,
attempt, step ID, repository ID, or base revision differs from the active
dispatch.

For every executor proposal variant, `criterion_self_reports` must contain
exactly one entry for every `acceptance_criteria[].criterion_id` on the
normalized plan step. `check_id` is compared to `criterion_id` exactly;
duplicate, missing, renamed, and unknown IDs are rejected. Every self-report
status is `not_run`; models do not run acceptance checks.

Before materialization, the dispatcher inspects the registered worktree itself,
enforces the exact step `writable_paths`, and executes the structured checks.
For a required-commit repository it computes the candidate tree using an
isolated index, persists intent, stages only the exact validated paths, creates
the commit, and validates parent, tree, identity, subject, and clean post-state.
It then emits the authoritative `dispatcher.executor_result.v1` containing the
result revision or patch hash, evidence hashes/sizes, and passing verification.
Executors cannot supply those authoritative fields or invoke Git mutation.

## Reviewer results

Reviewer results are typed JSON objects discriminated by `verdict`:
`accepted`, `changes_requested`, `blocked`, or `inconclusive`. Every result is
bound to an immutable review target containing the executor dispatch ID, attempt,
result revision or patch hash, and reviewed evidence hashes. Every reviewer
result also includes:

- `response_contract: "dispatcher.reviewer_result.v1"` exactly;
- `result_version`, `dispatch_id`, `attempt`, and `step_id`;
- `repo_id` and `review_target`;
- `findings`, `verification`, and `required_remediation` lists;
- a non-empty `summary`; and
- the `verdict` discriminator.

`accepted` cannot contain a blocking finding or required remediation.
Every reviewer result must contain the exact
`response_contract: "dispatcher.reviewer_result.v1"` field. It must be one JSON
object with no prose or Markdown fences; required strings cannot be empty.
`changes_requested` requires remediation. `blocked` requires blockers.
`inconclusive` requires a reason. A review result for any other immutable target
is retained only as supplemental evidence and cannot change step state.

Every reviewer verdict uses the same exact one-to-one
`acceptance_criteria[].criterion_id` to `verification[].check_id` rule.
`accepted` requires all statuses to be `passed`; non-success verdicts may use
`failed` or `skipped` only while retaining complete, duplicate-free criterion
coverage. An inconsistent `accepted` result is rejected before the dispatch is
completed or a review row is recorded.

Reviewer dispatches are inspect-only. Their prompt lists only `inspect`,
unconditionally prohibits file or Git mutation, and contains one authoritative
`observation_tools` object:

```text
{
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
```

Native `read`, `glob`, and `grep` inspect file contents and locate files. The
exact shell diagnostics are only for current-directory, branch, revision,
status, and diff metadata. The final permission
compilation forces native `edit` and `write` to `deny` and replaces the complete
reviewer Bash map after all configurable layers with one hardcoded fallback
deny plus exact allows for `pwd`, `ls`, `git status --porcelain=v1`, `git branch
--show-current`, `git rev-parse HEAD`, and `git diff --no-ext-diff
--no-textconv`. No allowed diagnostic pattern contains a wildcard or accepts
arguments, redirection, pipes, chaining, command substitution, or other shell
syntax. All other commands, including test runners, interpreters, staging,
commit, branch mutation, and push, remain denied. Required remediation is
reported for an executor to perform. Reviewers do not run tests. Reviewer
results and reports are persisted by the dispatcher outside the immutable
target repository; a reviewer never writes a report into that repository.

Reviewer verification is based on the immutable repository, fixed check source,
dispatcher-generated executor result, authoritative check records, and evidence
rather than reviewer-run tests. The supervisor Markdown bootstrap advertises
the same native inspection and exact diagnostic lists without changing its
schema-v1 JSON response contract. Supervisors must not write target repositories.

Dispatcher workers receive managed MCP definitions from schema-v2 project
configuration: a project `mcp` section (`environment_passthrough` plus a
`servers` registry) and an exact ordered `mcp_tools` list on every role. The
generated child OpenCode configuration contains the selected servers and exact
per-tool allow entries; prompts publish the same list. Isolated worker
HOME/XDG directories remain in use, with only `environment_passthrough`
variables passed from the parent (a missing name fails before process
creation). Unlisted MCP methods stay denied; roles with MCP tools require a
deny-default compiled permission policy.

MCP output is non-authoritative research context. It cannot expand executor
`writable_paths`, satisfy dispatcher acceptance checks, create evidence records,
accept a review, or perform dispatcher Git operations. GitHub, Playwright, and
`repomix_generate_skill` remain unavailable.

## Terminal failure invariant

Whenever an active durable step enters `FAILED`, the dispatcher replaces that
step and transitions a `RUNNING` or `WAITING_OPERATOR` run to `FAILED` in the
same authoritative state-store save. The run transition clears any operator
request and uses the next event sequence after the step failure. A failed step
cannot be newly persisted into `NEW`, `READY`, `HALTED`, `SUCCEEDED`, or
`CANCELLED`; an already `FAILED` run remains `FAILED`.

This applies to executor failed/blocked policy exhaustion, reviewer rework and
blocked/inconclusive exhaustion, and stall exhaustion configured with
`on_exhausted: fail`. Once worker application returns the terminal record, the
coordinator returns a non-accepted completion decision through its existing
non-`RUNNING` stop. It does not ask the supervisor to complete again, emit a
`completion_denied` loop, wait for the turn limit, or parse obligation strings
to infer terminality.

## Protocol boundary

Schema validation alone does not execute work. Before a command can launch a
session, later phases validate the normalized plan step, dependency state,
repository lock, role eligibility, policy, session lineage, and durable
dispatch intent. The Phase 4 sequential facade implements these validations;
real OpenCode dispatch remains disabled until it is the sole execution path.

Context-invalid worker results are not repaired or reshaped into another result
variant. They fail the worker boundary, preserve a bounded redacted category and
detail on the failed or abandoned dispatch, and require reconciliation when a
started worker may already have changed repository state.

This protocol targets one trusted operator running personal research workflows.
OpenCode permissions define the intended role capabilities but are not an
operating-system sandbox or a hostile-tenant guarantee. The protocol relies on
typed worker contracts and dispatcher-owned repository validation, checks,
evidence, and Git actions for workflow correctness.
