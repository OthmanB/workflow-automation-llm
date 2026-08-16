# Operations Guide

The SQLite database at `state.directory/dispatcher.sqlite3` is authoritative.
Reports, audit JSONL, transcripts, and support bundles are derived artifacts.
Never edit or delete the database manually.

OpenCode worker sessions use isolated HOME/XDG state and are not visible to a
normal `opencode -s` command. See [`session-inspection.md`](session-inspection.md)
for safe export, TUI inspection, state-root, and credential-handling guidance.

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

## Cluster Preflight

```bash
dispatcher cluster-preflight --config <project.yaml>
```

This command requires a schema-valid `cluster_preflight` section and prints a
sanitized structured JSON result. It returns `0` only when the configured
current context, namespaces, Helm chart-version floors, API resources, namespaced
`kubectl auth can-i` checks, Kubernetes client/server version floors, and the
Kubernetes-supported client/server compatibility relationship all pass. The
relationship requires matching majors and a client/server minor skew no greater
than one, independently of the configured floors. A readiness failure returns
`1` and still prints its failed structured result; an invalid or missing cluster
preflight configuration returns `2`.

The command parses the project schema without creating dispatcher state or
starting workers, then issues only fixed read-only commands for context/version
metadata, namespace/release/resource presence, and authorization inspection.
It never reads Secret data and never applies, deletes, patches, creates,
rolls out, or port-forwards Kubernetes resources. Command output is bounded and
only selected safe fields are retained in the JSON result.

When `cluster_preflight.kubectl_path` is set, it must be an absolute,
normalized path to an executable regular file and is used as argv element zero
for every kubectl command. The path is validated before cluster contact; omit it
to use `kubectl` from `PATH`. Helm remains resolved from `PATH`.

API discovery checks only the configured resource names returned by `kubectl
api-resources --output=name`, such as `pods`. A one-level auth subresource such
as `pods/portforward` is checked only through `kubectl auth can-i` and requires
that its parent resource be declared for discovery.

This verifies readiness only. It does not authorize deployment, rollback,
network access, a kubeconfig, or raw `kubectl`/Helm/port-forward access for a
worker. Those remain unavailable to workers. Actual deployment and rollback are
future dispatcher-owned typed capabilities with their own explicit scope,
approval, mutation, and recovery contracts.

Readiness requirements use capability floors and supported relationships. Exact
chart/image revisions, manifest digests, and rendered values are bound only by
an approval-time mutation snapshot and rollback-evidence contract.

## Static Cluster Operation Contracts

There is no dispatcher command for `kubectl` dry-run/apply, Helm upgrade/install,
rollback, or port-forward. `cluster_mutation`, an optional `cluster_operation`
plan reference, and the repository-owned operation manifest are validated
through public Python library APIs. Plan admission validates only the reference,
so an executor may create the manifest and declared files before dispatcher
structured Git commits them. The required post-commit
`validate_cluster_operations_for_plan()` API then reads the operation manifest
and checks declared repository paths without reading referenced
manifest/chart/values content or any Secret content; it fails closed for a
missing or invalid manifest and has no skip/fallback path.

The operation manifest accepts only the finite action names
`kubectl_server_dry_run`, `helm_upgrade_install`, `port_forward`, and
`tls_dc8_no_client_certificate_rejection`. It has no
argv, shell, URL/OCI chart source, `--set`, field-manager, inline Kubernetes
object, Secret value, or environment-expansion surface. Actual digests, source
revision, tool identity, server observations, rendered values, approval records,
mutation execution are separate dispatcher-only phases.

Phase 3 provides a code-tested `ClusterOperationRunner` execution boundary only.
Phase 4 provides a separate read-only `capture_cluster_operation_snapshot()`
library boundary. Phase 5 adds a Service-only loopback port-forward plus direct
no-client-certificate TLS/DC8 rejection check through dispatcher-private
production adapters. `_cmd_execute` creates those adapters only for a
real-operation approval with a cluster-operation envelope; they are invoked only
after source-manifest, snapshot, and lifecycle-approval validation. They never
enter worker prompts or permissions, and ordinary non-cluster runs do not create
them. This code has not deployed T2.5. Actual invocation remains gated by
committed T2.5 source artifacts, the exact approved envelope, a fresh snapshot
with source/tool/rollback bindings, real-operation approval, and an
operator-selected `dispatcher execute`.

### Cluster Operation Status and Approval

```bash
dispatcher cluster-operation status --config <project.yaml> --run-id <run-id> \
  --operation-id <operation-id> --source-revision <commit-sha> --format json

dispatcher cluster-operation snapshot --config <project.yaml> --run-id <run-id> \
  --step-id <step-id> --operation-id <operation-id> --source-revision <commit-sha> \
  --plan <normalized-plan.yaml> --real-operation-approval <approval.json> \
  --tier1-invariant-snapshot-digest <sha256> --output <sanitized-snapshot.json>

dispatcher cluster-operation approve --config <project.yaml> --run-id <run-id> \
  --operation-id <operation-id> --source-revision <commit-sha> \
  --snapshot <sanitized-snapshot.json> --owner-ref <owner-ref> \
  --allowed-action action-1=<sha256> [--allowed-action action-2=<sha256> ...] \
  --rollback-digest <sha256> --expires-at <UTC-ISO-8601>
```

`status` reads an already-existing local journal and does not create one.
`snapshot` requires that journal record to be `STATIC_VALIDATED`, revalidates the
provided plan's post-commit operation against the exact approval envelope, and
writes a private snapshot file without attaching it or modifying state. It uses
only target-pinned read-only `kubectl` context/version/resource/Secret-metadata
queries and Helm status/history metadata. It never requests Secret values or
issues apply, rollout, port-forward, Helm upgrade, Helm repo, or generic network
commands. `approve` accepts a pre-created, duplicate-free sanitized snapshot
JSON, attaches it only to a `STATIC_VALIDATED` record (or verifies an exact
existing captured snapshot), and writes an owner approval only when every
identity/action/rollback digest is exact and unexpired. There is no dry-run,
mutation, probe, rollback, or reconciliation command in this release.

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
   --scope-manifest-digest <manifest.digest> \
   [--expected-repository-revision <repo-id>=<commit-sha> ...] \
   --output <approval.json>
```

The manifest command computes the ordered autonomous scope beginning at the
first executable step. It includes every currently pending or ready step the
run can reach without another unresolved operator gate, in plan/dependency
order. Each scoped entry contains the role kind, role-scoped actions, digest of
the generated OpenCode permission JSON, and its structured-Git capability
including the step ID, writable paths, evidence paths, commit policy, and
identity digest. It also contains each complete `cluster_operation_envelopes`
entry before approval: run/step/repository, target/context, normalized manifest
path, exact action tuple, automatic rollback intent, operation/source roots,
plan/config digests, and envelope digest. The top-level `digest` attests to the
complete ordered scope. A multi-step scope or any scope with a cluster envelope
requires that digest through `--scope-manifest-digest` after the operator has
reviewed the entire manifest; it rejects absent or stale digests rather than
deriving later-step permissions or cluster authority silently. The role/digest
arguments still attest the first launch step and keep single-step non-cluster
invocation compatibility. A scope spanning repositories requires one
`--expected-repository-revision` per repository; each is inspected clean and is
bound to the approval record in first-scope-use order. Single-repository approvals
retain the existing command shape and can optionally bind the same explicit
revision. These commands do not invoke OpenCode or make a network call. The record
binds the decision to the exact project, configuration, plan, run, repository,
first step, complete scope, and, when supplied, ordered repository revisions.

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
   [--expected-revision <commit-sha> | \
    --expected-repository-revision <repo-id>=<commit-sha> ...] \
   --approval-record <approval.json> --confirm-real-operation
```

This is the only command that can request real OpenCode execution. It rejects
public/mock configurations, stale plan or baseline hashes, unresolved recovery,
unclean or wrong-branch repositories, repositories not at the operator-supplied
expected revisions, missing or mismatched live-smoke
proof, any role-set or role-permission drift, stall-policy drift, missing preflight, and
missing operator confirmation before launching a process. It exact-matches the
approval record's project, configuration digest, plan digest, run, repository,
first remaining approved step, and complete ordered approval scope before launching a
process. A multi-repository scope requires a complete, exact repository/revision
mapping that matches the approval record; each unique scope repository is checked
clean at its expected revision before launch or resume, including the next step's
repository. A recoverable `REVIEW_REQUIRED` step remains the first approved step
on resume; an accepted prefix remains bound while its approved suffix continues.
It rejects omitted, reordered, extra, or changed scoped entries,
including later-step role, writable-path, evidence-path, and structured-Git
changes, as well as missing, extra, reordered, or changed cluster envelopes.
Legacy records without a scope remain valid only when the recomputed scope
contains exactly one non-cluster step; a legacy or single-step approval never
authorizes a cluster-operation step. The command runs preflight immediately
before launch and records the validated approval record and envelopes in the audit
log.

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

The supervisor supplies semantic routing and rework decisions while the
dispatcher adds exact paths, checks, hashes, permissions, evidence requirements,
session lineage, and response schemas. Straightforward runs do not inherently
require the most expensive supervisor model, but any configured replacement
should first prove strict command conformance and state-sensitive rework behavior
through deterministic fixtures and a bounded smoke scenario.

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
Freshness validation always rechecks plan and source identity, then compares
evidence and review-proof path, size, and hash only for explicitly ACCEPTED
steps. New PENDING-step artifacts and later repository revisions therefore do
not invalidate an autonomous run's historical baseline. A WAIVED decision is
still explicit and hydrates only as `WAIVED`, never as an evidence-backed
acceptance; it makes no historical-evidence provenance claim.
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
