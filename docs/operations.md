# Operations Guide

The SQLite database at `state.directory/dispatcher.sqlite3` is authoritative.
Reports, audit JSONL, transcripts, and support bundles are derived artifacts.
Never edit or delete the database manually.

## Preflight

```bash
dispatcher preflight --config <project.yaml>
```

Runs configuration, path, repository, credential, disk, and optional model
checks. Exit `0` means the selected checks passed; exit `1` means preflight
failed. It does not create a run or launch an approved worker dispatch.
When model smoke testing is enabled, each model must return exactly `OK` after
trimming surrounding whitespace. `NOT OK`, extra text, empty output, or a JSON
wrapper fails preflight.

## Mock Run

```bash
dispatcher run --config <project.yaml> --mock --skip-smoke
```

This invokes the legacy mock harness only. It is useful for deterministic
development validation but is not the authoritative sequential coordinator and
does not authorize real OpenCode or repository-mutating execution. Omitting
`--mock` exits `2` before configuration loading.

## Live Smoke Proof

```bash
dispatcher smoke-proof --config <private-v2.yaml> --model <provider/model> \
  --output <proof.json>
```

Set `DISPATCHER_LIVE_OPENCODE=1` to enable this command. It runs the isolated,
read-only live OpenCode smoke test and writes its sanitized artifact only when
the fixed `LIVE_SMOKE_OK` response and all safety checks pass. This is the only
command that produces a valid live-smoke-proof artifact for the execute gate.

## Approve Real Operation

```bash
dispatcher permission-manifest --config <private-v2.yaml> --run-id <run-id> \
  --plan <plan.yaml> --repo-id <repo-id> --output <manifest.json>

dispatcher approve-real-operation --config <private-v2.yaml> --run-id <run-id> \
  --plan <plan.yaml> --repo-id <repo-id> --approval-ref <decision-ref> \
  --permission-digest supervisor=<sha256> \
  --permission-digest terra=<sha256> \
  --permission-digest reviewer=<sha256> \
  --output <approval.json>
```

The manifest command computes the exact supervisor, eligible-executor, and
compiled-reviewer role set for the first executable step. Each entry contains
the role kind, role-scoped actions, and digest of the generated OpenCode
permission JSON. The approval command requires the operator to supply that
exact role/digest set and writes it into the owner-only approval record. These
commands do not invoke OpenCode or make a network call. The record binds the
decision to the exact project, configuration, plan, run, repository, step, role
set, and role permissions.

The role set is not hand-selected. It always contains the configured
supervisor, every configured executor eligible for the step, and every role in
the compiled review obligation. Reviewer arguments are absent only when the
compiled step does not require review. Duplicate, missing, extra, malformed,
or stale role digests are rejected.

## Real Operation Gate

```bash
dispatcher execute --config <private-v2.yaml> --run-id <run-id> \
  --plan <plan.yaml> --repo-id <repo-id> \
  --smoke-proof <proof.json> --smoke-model <provider/model> \
  --permission-digest supervisor=<sha256> \
  --permission-digest terra=<sha256> \
  --permission-digest reviewer=<sha256> \
  --stall-policy-digest <sha256> \
  --expected-revision <commit-sha> \
  --approval-record <approval.json> --confirm-real-operation
```

This is the only command that can request real OpenCode execution. It rejects
public/mock configurations, stale plan or baseline hashes, unresolved recovery,
unclean or wrong-branch repositories, repositories not at the operator-supplied
expected revision (`--expected-revision`), missing or mismatched live-smoke
proof, any role-set or role-permission drift, stall-policy drift, missing preflight, and
missing operator confirmation before launching a process. It exact-matches the
approval record's project, configuration digest, plan digest, run, repository,
first pending step, and complete role permission manifest before launching a process. The command runs preflight
immediately before launch and records the validated approval record in the
audit log.

Immediately before `smoke-proof` and `execute`, the dispatcher atomically copies
the active operator OpenCode credential store from
`$XDG_DATA_HOME/opencode/auth.json` (or
`$HOME/.local/share/opencode/auth.json`) into the configured private dispatcher
state with mode `0600`. Switch the desired OpenCode account before invoking
either command. The selected credentials are snapshotted at command start and
remain stable for that invocation; their contents are never parsed or logged by
the dispatcher.

Real execution requires an available verification isolation backend.
`darwin_seatbelt_v1` is the supported local macOS backend for dispatcher-owned
checks; it denies check-process network access and writes outside the disposable
verification workspace. `linux_bwrap_v1` is an optional future Linux backend
with an unshared network namespace. Backend absence fails before OpenCode
launch.

Every acceptance criterion is rerun by the dispatcher from its schema-v2 argv
definition. Authoritative status and transcript hashes persist separately from
model output. Seatbelt constrains dispatcher-owned checks, not the
provider-connected OpenCode parent process; executor write/commit authority is
separately constrained by repository/worktree validation and must not be
confused with check-process isolation.

This command has not been run by this project yet. A real-operation configuration
must remain private and must never be added to the public example.

## Worker Response Safety

Every executor and reviewer response is parsed as one JSON object and then
validated against its full result schema. The response must contain the exact
`response_contract` value for its role. Missing, renamed, null, or empty required
fields are rejected. Prose before or after JSON, Markdown fences, invented
success fields, incorrect evidence hashes, and incorrect evidence sizes are also
rejected. The dispatcher never extracts JSON from prose or repairs a model
response. The run stops or enters recovery before workflow state advances.

Verification is plan-contextual in addition to schema-valid. Each result must
report exactly one `verification` entry for every normalized acceptance
criterion, and each `check_id` must exactly equal its `criterion_id`. Duplicate,
missing, renamed, and extra IDs fail the worker boundary. Executor `completed`
and reviewer `accepted` results require all statuses to be `passed`; blocked,
failed, changes-requested, blocked-review, and inconclusive variants may use
`failed` or `skipped` only with the same exact coverage. Free-form criterion
descriptions and result summaries are never interpreted as equivalent checks,
and an inconsistent success response is not repaired into a non-success type.

## Role Permission Ceilings

Dispatch authorization is role-scoped before permission compilation. Executors
receive the normalized step's ordered actions except `verify`, which is owned
by the dispatcher structured-check runner; no action is added.
Reviewers receive only `inspect`; if the step lacks `inspect`, preparation fails
before process launch. Supervisor turns are inspect-only. Single and batch paths
compile, render, and persist the same scoped value.

After all configurable policy layers and dispatch authorization are compiled,
reviewer and supervisor policies unconditionally force `edit: deny`, `write:
deny`, and replace the entire Bash map with this exact non-overridable map:

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

The default deny is serialized before the exact allows. No allowed pattern
contains `*` or `?`, and repository, role-class, concrete-role, and step policy
cannot add or override a command. Reviewers inspect the immutable repository,
fixed check source, executor result, and evidence; they report remediation for
an executor and do not run tests.

| Exact command | Intended diagnostic property |
|---|---|
| `pwd` | Reports the current working directory. |
| `ls` | Lists the current directory without caller-controlled paths or options. |
| `git status --porcelain=v1` | Reports repository state deterministically. |
| `git branch --show-current` | Reports the current branch without creating one. |
| `git rev-parse HEAD` | Reports the current revision. |
| `git diff --no-ext-diff --no-textconv` | Reads the worktree diff while disabling external diff and text-conversion drivers. |

The reviewer prompt publishes one `observation_tools` object whose `native`
field is exactly `["read", "glob", "grep"]`, whose `diagnostic_commands` field
is exactly the list above, and whose `mcp` field is exactly the ordered MCP
tool list configured for that reviewer role (empty when none are assigned).
Native
tools inspect file contents and locate files; shell diagnostics are only for
current directory, branch, revision, status, and diff metadata. The reviewer
may not add shell arguments, redirection, chaining, pipes, or substitutions,
run tests, or mutate files or Git state. Required remediation belongs to an
executor. Assigned research MCP tools may inspect library documentation, pack
code for analysis, or search code, but they never authorize edits, checks, or
Git mutation.

The executor prompt publishes a `research_tools` object with the same exact
MCP list; MCP research may inform implementation but cannot expand
`writable_paths` or replace dispatcher checks. The supervisor Markdown
bootstrap renders the same native, diagnostic, and MCP lists
without changing the schema-v1 response contract. Supervisors may observe the
durable state and target repository through those capabilities but may not write
target repositories. Reviewer results, reviews, transcripts, and reports are
stored by the dispatcher outside the immutable repository.

If schema-v2 project configuration omits `mcp`, workers inherit the operator's
normal OpenCode configuration and environment while keeping dispatcher-owned
OpenCode data/session directories. Empty or omitted role `mcp_tools` receive the
default Context7, Repomix, and Semble catalog; nonempty lists narrow it. The
dispatcher permission map still denies unlisted methods, so inherited GitHub,
Playwright, and `repomix_generate_skill` tools are not exposed.

An explicit project `mcp` section takes precedence: only its server registry is
emitted, only `environment_passthrough` variables are copied, and role lists are
authoritative. An explicit empty server registry disables MCP. Roles with MCP
tools require a deny-default permission policy so unlisted methods stay denied.

This project operates as a trusted personal research tool. OpenCode permissions
describe and constrain the intended role tool surface but are not presented as
a hostile-tenant or operating-system security boundary. Executors retain only
compiled inspection and file-write capabilities. Test runners and every Git
mutation command remain denied; semantic `commit` is consumed only by the
dispatcher-owned structured Git capability. MCP results are model context and
never replace dispatcher-owned verification, evidence, review, or Git actions.

## Start

```bash
dispatcher start --config <project.yaml> --run-record <run.json>
```

Requires a schema-valid `NEW` run record whose project ID and config digest
match the selected project. Persists generation one in SQLite. Exit `0` means
the run was persisted; exit `2` means validation, ownership, or state checks
failed. It does not launch a session.

## Status

```bash
dispatcher status --config <project.yaml> --run-id <run-id> --format json
```

The JSON view reports run state and generation, ready and blocked steps with
reasons, active dispatches and batches, waiting operator metadata, usage, and
leases. The text view is a concise rendering of the same derived snapshot. It
does not mutate run state.

Failed and abandoned worker dispatches durably retain a stable
`failure_category` and an actionable `failure_detail`. Details are credential
redacted before persistence and limited to 5,000 characters. Transition event
reasons use the same bounded redacted diagnostic, so operators do not need raw
model output or stderr to identify ordinary contract and repository failures.

An active step entering `FAILED` makes the run `FAILED` in the same SQLite
snapshot and clears any operator request. This covers executor result-policy
exhaustion, reviewer rework or blocked/inconclusive exhaustion, and stall
exhaustion configured with `on_exhausted: fail`. Event sequences remain
monotonic: the run-failure event follows the step-failure event. The sequential
coordinator returns a non-accepted decision immediately after that save; it does
not send repeated `completion_denied` prompts or use the supervisor turn limit
as terminal-failure handling.

## Resume and Recovery

```bash
dispatcher resume --config <project.yaml> --run-id <run-id>
dispatcher recover --config <project.yaml> --run-id <run-id>
```

`resume` validates one non-terminal, sole active run and does not create a
supervisor session. `recover` classifies unresolved dispatches and attempts one
narrow automatic recovery: when durable structured Git state is `STAGED` and
the worker process is known to have exited, it adopts `HEAD` only if the commit
exactly matches the durable base parent, candidate tree, configured author and
committer identity, deterministic subject, changed path set, assigned worktree,
clean post-state, and checked evidence. Adoption atomically creates the
dispatcher-authoritative result and forwarding without running Git mutation or
verification again.

Any mismatch is persisted as `RECONCILIATION_REQUIRED`; dirty or ambiguous
pre-commit states are never reset, cleaned, staged, committed, or retried by
recovery. Other `RUNNING` dispatches require operator reconciliation because
external side effects may have completed.

`recover` additionally reconstructs forwarding for a durable `COMPLETED`
dispatch directly from its stored result, authoritative verification, and
dispatch/step state. It never reruns Git commands, verification checks, or a
model, and it applies a step-outcome transition only when the step still points
at that dispatch, so review rows and acceptance accounting are written exactly
once. New result application persists the result payload, step state,
forwarding, optional review row, and structured Git final state in one SQLite
transaction, eliminating the completed-but-unforwarded crash window.

When execution continues through the sequential coordinator, every durable
`FORWARDED` dispatch is loaded from SQLite and delivered to the next successful
supervisor turn in a deterministic `orchestration_resume` JSON envelope. The
envelope includes the complete normal bootstrap and sanitized parsed forwarding
objects ordered by forwarding event sequence and dispatch ID. Already
`ACKNOWLEDGED` history and every other dispatch state are excluded. A durable
`COMPLETED` dispatch is recovered into `FORWARDED` before the supervisor prompt
is built. Ordinary
continuation with no pending forwarding uses the exact normal bootstrap prompt.

The coordinator validates all pending stored payloads before delivery. Missing,
empty, malformed, duplicate-key, wrong-dispatch-ID, or wrong-role-kind payloads
stop continuation without acknowledging any pending item. Do not repair or
manually acknowledge such rows; preserve the database and investigate the
authoritative-state corruption. Forwardings become `ACKNOWLEDGED` only after
the supervisor call returns successfully, followed by readiness refresh and
command processing. A failed supervisor call leaves them `FORWARDED`. A crash
after receipt but before acknowledgement may replay the same dispatch ID on the
next continuation; this boundary is intentionally at-least-once, not an
exactly-once guarantee.

`recover` also reports unfinished workspace groups. `ACTIVE`, `INTEGRATING`,
and `FAILED` groups require reconciliation because temporary child branches may
contain unintegrated work. `CLEANUP_PENDING` means cleanup intent was persisted
before a crash and can be resumed safely by the dispatcher-owned workspace
manager. Do not remove Git worktrees or branches by hand.

## Cancel And Stall Recovery

```bash
dispatcher cancel --config <project.yaml> --run-id <run-id> \
  --dispatch-id <dispatch-id> --actor-id <operator-id>
```

`cancel` records the request before signalling the verified local process group.
It checks the recorded host and process identity and never signals a process on
another host. The managed process receives an interrupt first, followed by the
configured termination sequence if it does not stop.

Timeouts, interruptions, temporary connection failures, temporary rate limits,
and context overflow may retry according to `execution.stall_policy`. Each retry
creates a new dispatch and preserves the old attempt. Provider quota/billing,
authentication/permission, and unknown failures stop for operator action.
After the configured retry count, `on_exhausted` asks the operator, halts, or
fails the step and run according to `ask`, `halt`, or `fail`. The project
cost/token budget remains a separate optional safety limit.

For a batch, each failed child emits one warning containing its dispatch ID,
stable category, and bounded redacted detail. Successful children do not emit
failure warnings. Logging is additional observability: each child still commits
its own authoritative state, successful siblings remain forwarded independently,
and the final `wait_for_started` join determines whether batch reconciliation is
required.

## Operator Answer

```bash
dispatcher answer --config <project.yaml> --run-id <run-id> \
  --request-id <request-id> --answer <value> --actor-id <operator-id>
```

The request ID, allowed answer, expiration, and required role are validated
before the decision and resulting run snapshot are committed in one SQLite
transaction. Duplicate, expired, unauthorized, stale-generation, and
incompatible-state answers fail with exit `2` and do not create a decision.

Answer behavior is explicit for every request kind. Risk gates use `approve`
to resolve the step gate and resume, or `deny` to halt. Escalation uses
`reassign` to return its blocked step to `READY`, or `halt`. Review waiver uses
`waive` to accept a waivable review-required step with a decision reference,
or `halt`. Stall recovery uses `retry` to make a blocked step `READY` or make a
review-required step dispatchable for review again, or `halt`.
Underspecification uses the fixed `answer` acknowledgement token to resume, or
`halt`; it does not carry free-form answer content. Budget requests accept only
`halt`.

Solo `reconcile` validates the referenced failed or abandoned dispatch and
makes its blocked step `READY`; a review-required step stays
`REVIEW_REQUIRED`. Batch `reconcile` validates every failed child, applies the
same transition once per affected step, and leaves accepted siblings unchanged.
The historical batch remains `FAILED`; reconciliation authorizes fresh
replacement attempts rather than rewriting history as `JOINED`. A successful
sibling that is still `FORWARDED` remains accepted but is delivered and
acknowledged through normal continuation; acceptance never auto-acknowledges it.
Only affected failed steps are retried. `halt` leaves the failed step and batch
records unchanged and halts the run.

Workspace `reconcile` is cleanup-before-resume. The CLI durably records
`CLEANUP_PENDING`, then asks the workspace manager to remove only configured,
dispatcher-owned, clean worktrees and merged branches without force. The state
transaction accepts the answer only after the group is durably `CLEANED`.
Failed or unsafe cleanup returns exit `2`, preserves the active request and
`WAITING_OPERATOR` run, and does not record a decision; unmerged or dirty
workspace state must be reconciled manually before retrying. Workspace `halt`
does not delete branches or worktrees and leaves the group available for
inspection. This attestation does not reconcile arbitrary repository changes:
every later dispatch still performs repository identity and cleanliness checks
before launching a process.

## Disposable Live Proof

The disposable live suite proves the real sequential, batch, worktree,
cancellation, review/rework/resume, reconciliation, and halt paths against
temporary repositories only. It is explicitly disabled by default and requires
both a real OpenCode credential store and explicit opt-in:

```bash
export DISPATCHER_LIVE_SUPERVISOR_MODEL="<intended supervisor>"
export DISPATCHER_LIVE_EXECUTOR_MODEL="openai/gpt-5.6-terra"
export DISPATCHER_LIVE_REVIEWER_MODEL="<intended reviewer>"
export DISPATCHER_REAL_DISPOSABLE=1
.venv/bin/python -m pytest tests/live/test_real_operation_disposable.py -v -m live_opencode
```

### Model role variables

| Variable | Role | Default behavior |
|---|---|---|
| `DISPATCHER_LIVE_MODEL` | All roles | Explicit all-role fallback for cheap compatibility runs |
| `DISPATCHER_LIVE_SUPERVISOR_MODEL` | Supervisor | Overrides the fallback for the supervisor role |
| `DISPATCHER_LIVE_EXECUTOR_MODEL` | Executors | Overrides the fallback for every executor role |
| `DISPATCHER_LIVE_REVIEWER_MODEL` | Reviewers | Overrides the fallback for every reviewer role |

If `DISPATCHER_LIVE_MODEL` is set, it is used for every role unless a
role-specific variable overrides that role. If the fallback is absent, all
three role-specific variables must be present; the harness fails loudly and
lists the missing variables instead of defaulting to a model. Supervisor roles
are configured from the supervisor value, executor roles from the executor
value, and reviewer roles from the reviewer value. The cancellation
subprocess uses the resolved executor model.

### What the deterministic harness actually invokes

The disposable harness replaces the supervisor turn with a deterministic,
state-driven reactive Python supervisor. Executor and reviewer sessions run
through the real OpenCode adapter with the resolved role models. Passing this
suite therefore proves executor and reviewer model behavior and the
dispatcher's own control logic; it does not prove behavior of a real
supervisor model. Do not claim real supervisor-model coverage from this suite.

### Deterministic fixtures

Every disposable repository, including dynamically created siblings and
same-repository worktree bases, is seeded before its initial commit with a
committed `.gitignore` covering `__pycache__/`, `*.py[cod]`, and
`.pytest_cache/`, plus fixed pytest files (`test_real_output.py`,
`test_real_second_output.py`) that fail until the expected result and evidence
files exist. Executor prompts name the exact files and content, the exact
pytest command, the exact criterion ID, the evidence path, the commit
requirement, the prohibition on network/deployment tools, and the requirement
that `git status --porcelain` be completely empty before returning. Plan
criterion IDs are fixed (`verify-real-output`,
`verify-real-second-output`) and match the worker `verification[].check_id`
values exactly.

The disposable executor criterion currently uses `pytest -q <file>` because it
matches the existing `pytest *` permission rule. This is an interim fixture
alignment only, not the durable verification architecture. Reviewer prompts use
native `read`/`glob`/`grep` for contents, use the exact diagnostics above only
for Git/current-directory metadata, and do not request pytest.

### Reconciliation and halt scenarios

One-shot failure injection (`forced-reconciliation-residue.tmp`) is
test-only. It leaves one known untracked file after the worker exits so
production repository validation fails deterministically. The solo
reconciliation scenario then reconciles the disposable repository to its
recorded initial revision, answers `reconcile` through the real
`dispatcher answer` CLI, and continues the same run to success. The batch
reconciliation scenario does the same for one failed child while preserving
the accepted sibling. The halt scenario answers `halt` and verifies the run
reaches `HALTED` with historical failure state preserved and repository
content untouched.

### Prohibitions

These tests run against disposable temporary repositories only. They never
touch production repositories, push, or deploy. Model provider connectivity is
still required, and OpenCode permissions do not technically isolate executor
network access. The scenarios do not intentionally request other external
services. They do not weaken `commit_policy="required"`, repository
cleanliness checks, identity/evidence validation, or verification
enforcement.

## Support and Retention

```bash
dispatcher support --config <project.yaml> --run-id <run-id>
dispatcher prune --config <project.yaml> --apply
```

`support` writes a private bundle containing redacted status, report, audit,
and manifest files. It never copies the database, raw prompts, child
environment, or source configuration.

`prune` requires `--apply` and follows the explicit `observability.retention`
YAML policy. It archives or deletes only derived artifacts, skips active-run
artifacts, and never removes SQLite rows or unresolved dispatch data.

## Baseline

```bash
dispatcher baseline inspect --config <project.yaml> --plan <plan.yaml>
dispatcher baseline approve --config <project.yaml> --plan <plan.yaml> \
  --observation <observation.json> --decisions <decisions.json> \
  --approval-decision-ref <decision-id>
dispatcher start --config <project.yaml> --run-record <run.json> \
  --use-approved-baseline
```

Inspection is read-only and records observed revisions, evidence hashes, and
review-proof files. Approval requires an explicit PENDING, ACCEPTED, or WAIVED
decision for every step. Accepted requires the current evidence and review proof
required by compiled policy; Waived requires its own operator decision reference.
For generic historical review proof, place a proof file at
`<evidence-root>/reviews/<step-id>.*`; the inspector hashes it without trusting
its prose content. Approval records accepted reviewer role keys explicitly.
For established legacy evidence, the inspector also observes
`<evidence-root>/<lowercase-step-id>*review*` files; an operator still decides
whether those hashes constitute sufficient review proof.
`start --use-approved-baseline` hydrates those durable states into a new run.
Private reference migration remains separately authorized work and is not
performed by the public example.

When the source document has a table of steps, pass the source table and an
explicit ownership map:

```bash
dispatcher baseline inspect --config <project.yaml> --plan <sidecar.yaml> \
  --source-markdown <roles-table.md> --ownership-map <ownership-map.yaml>
```

The importer requires one registered repository per step. Explicit repository
names in the source must agree with the sidecar and map; rows without a
repository fail unless the ownership map supplies one. The map is a project
local input and is not a baseline decision.

## Unsupported Lifecycle Operations

There is no authoritative-state `archive` command. Do not simulate archival by
deleting state files. Halt, cancellation, and recovery decisions must be
recorded through dispatcher-owned workflow and operator-gate paths.
