# Step 20: macOS Path-Scoped Executor Writes And Structured Git Capability

**Status:** Implemented; see
`markdown/reports/step-20-macos-structured-git-capability-report-2026-08-13.md`

**Date:** 2026-08-12  
**Status:** Proposed implementation plan  
**Prerequisites:** Steps 17-19 local macOS work complete  
**Operating model:** macOS-confined Tier 2 implementation; no Linux dependency

## Purpose

Executors are allowed to implement approved changes, including files that must
ultimately be committed. The security problem is not executor intent to commit;
it is the current implementation of that intent through model-generated
wildcard Bash permissions:

```text
git add *
git commit *
```

This step replaces model-controlled Git staging and commit execution with a
dispatcher-controlled, repository-scoped Git capability. Executors retain
authorized native writes inside a declared per-step path scope, but cannot run
tests, stage files, commit, push, create branches, alter Git configuration, or
invoke arbitrary Bash.

The capability is designed for the accepted macOS Tier 2 boundary:

- `darwin_seatbelt_v1` confines dispatcher-owned acceptance checks in disposable
  repository copies and denies their network access.
- The provider-connected OpenCode parent process is not represented as a general
  OS sandbox. Executor write authority is instead constrained by an approved
  path manifest, OpenCode permission UX, before/after repository inspection,
  dispatcher-owned verification, and dispatcher-owned Git side effects.
- No Linux or bubblewrap dependency is introduced by this step. The existing
  optional `linux_bwrap_v1` backend remains future hardening.

## Current Gap

The current executor contract is `dispatcher.executor_result.v1`. A model
returns a purported resulting Git revision, evidence SHA-256 values and sizes,
and check statuses after it has been allowed to perform a Git commit. The
dispatcher subsequently verifies the result, but Git staging and commit are
already model-triggered external side effects.

Relevant current paths:

- `src/dispatcher/results.py` defines the model-facing executor result and the
  reviewer result.
- `src/dispatcher/sequential.py` renders worker prompts, applies executor
  results, constructs review targets, and persists forwarding.
- `src/dispatcher/execution.py` parses the final worker JSON and invokes
  dispatcher-owned verification.
- `src/dispatcher/repository.py` inspects repository identity, evidence, and
  porcelain changes, but currently uses repository-wide `writable_roots` rather
  than a per-step write manifest.
- `src/dispatcher/permissions.py` maps semantic `commit` authorization to raw
  wildcard Bash Git permissions.
- `src/dispatcher/state_store.py` has no durable record that distinguishes a
  Git commit intent, a staged index, an already-created commit, and an adopted
  post-crash commit.

The result is an incomplete trust boundary: acceptance checks are
dispatcher-owned, while the irreversible Git operation is still model-owned.

## Goals

1. Replace raw executor Git Bash permission with one dispatcher-controlled
   structured commit capability.
2. Make every executor write scope explicit in the immutable normalized plan.
3. Make the model return a proposal, not an authoritative final revision,
   content hash, size, or verification result.
4. Ensure the dispatcher derives evidence metadata, verification records,
   resulting revision, and review target from inspected state.
5. Persist enough intent and post-commit evidence to avoid a duplicate commit
   after a crash, while failing closed for every ambiguous state.
6. Preserve the existing `commit_policy: prohibited` authoritative patch path
   without granting raw Git authority to an executor.
7. Preserve same-repository worktree-barrier semantics and reject overlapping
   concurrent write scopes before child worktrees are provisioned.
8. Bind structured Git authority to real-operation approval evidence, not just
   the OpenCode child permission digest.

## Non-Goals

- This step does not claim that executor processes are generally sandboxed.
- This step does not add MCP access or a method-level MCP capability manifest.
- This step does not add push, remote mutation, checkout, merge, reset, branch
  creation, arbitrary Git configuration, or submodule mutation to the executor
  capability.
- This step does not silently migrate or infer `writable_paths` for persisted
  schema-v2 plans. A plan missing the required field is invalid.
- This step does not automatically recover a partially staged or otherwise
  ambiguous commit. It adopts only a commit that exactly matches durable intent.

## Required Design Decisions

The implementation should use the following decisions. They remain explicit in
this plan so the coding session does not treat them as accidental defaults.

| Area | Decision |
|---|---|
| Executor boundary | Apply the proposal boundary to every durable `SequentialExecutionCoordinator` executor path, including explicit mock workflow tests. Do not maintain a model-commit compatibility path. |
| Completed required-commit work | A `completed` proposal in a `commit_policy: required` repository must produce at least one approved content change and exactly one dispatcher-created commit. A no-op completed proposal fails closed. |
| Evidence scope | Every required evidence file must also be inside `writable_paths`; evidence paths do not implicitly widen executor authority. |
| Commit identity | Author and committer name/email are explicit YAML configuration, are included in the config digest and approval capability digest, and have no runtime fallback. |
| Interrupted commit | Automatically adopt only a committed HEAD that exactly matches recorded parent, tree, identity, message, path set, and post-commit inspection. Every other interrupted state requires operator `reconcile` or `halt`. |
| Non-committing repositories | `commit_policy: prohibited` retains dispatcher-derived patch-hash semantics and never invokes the Git capability. |

## Security Invariants

The implementation is correct only if all of the following are true:

1. The model receives no allow rule for `git add`, `git commit`, `git push`,
   `git config`, test runners, interpreters, or arbitrary Bash.
2. A plan's prose, model prompt, evidence declaration, or reviewer remediation
   cannot expand `writable_paths`.
3. Every changed path, including both sides of a rename, is inside the exact
   declared write scope and the configured repository-wide `writable_roots`
   upper bound.
4. `.git` and every descendant are always forbidden. A changed path that is a
   symlink, traverses a symlink outside the registered worktree, changes file
   mode, or has unsupported Git status fails closed.
5. The real Git index is clean before the structured capability begins. An
   executor-staged change is rejected rather than adopted.
6. Dispatcher verification runs before a required commit and only in the
   existing disposable, network-denied verification backend.
7. The dispatcher persists Git commit intent before modifying the real index.
8. The dispatcher runs Git only through exact argv lists, with a bounded timeout
   and no shell.
9. A required commit stages only the exact validated, sorted changed paths;
   after commit the assigned repository or assigned child worktree is clean.
10. Reviewer targets bind the dispatcher-derived revision, evidence hashes, and
    authoritative verification records, never model-provided final metadata.
11. A crash never causes the dispatcher to create a second commit for a
    previously committed tree. Ambiguous external state is not retried.
12. No raw model result, raw command output, credential, or environment value is
    copied into public documentation, reports, or logs.

## Immutable Plan Contract

### `writable_paths`

Add `writable_paths` to `StepAuthorization` in `src/dispatcher/plan.py`:

```yaml
authorization:
  authorized_actions: [inspect, modify, commit]
  writable_paths:
    - result.txt
    - evidence/
  requires_operator_approval: true
```

`writable_paths` is a nonempty ordered list whenever `modify` is authorized.
It is an exact per-step authorization, not a suggestion and not a replacement
for repository-level `writable_roots`.

Path grammar and validation rules:

- Each entry is a nonempty POSIX repository-relative path with a maximum length
  consistent with existing relative path contracts.
- Absolute paths, `.` roots, empty segments, `.` segments, `..` segments,
  leading or repeated separators, and NUL/control characters are rejected.
- A final `/` means the entry is a directory root and authorizes descendants;
  without that final `/`, the entry authorizes exactly one file path.
- `.git` and every descendant are rejected unconditionally.
- A file entry and a directory entry with the same normalized path are
  ambiguous and reject. Duplicate entries reject.
- A directory entry cannot overlap any other file or directory entry. A file
  entry cannot be contained by a declared directory entry. This preserves one
  canonical scope for every repository path.
- Every entry must be contained by one configured repository `writable_roots`
  value. The repository configuration remains the broad upper bound; it cannot
  be broadened by a plan.
- A plan action set containing `modify` without `writable_paths` rejects.
- A plan action set containing `commit` requires `modify`, a nonempty path
  scope, and a repository with `commit_policy: required`.
- A `commit_policy: prohibited` step may authorize `modify` but not `commit`.
- Every `evidence_requirements[].relative_path`, resolved beneath exactly one
  configured evidence root, must fall within the declared scope. Evidence is
  not an exception to the manifest.

The `writable_paths` value participates in `NormalizedPlan.plan_digest` through
the existing canonical step serialization. Changing it invalidates plan approval
and real-operation approval.

### Path interpretation

The implementation must compare logical repository-relative POSIX paths, not
string prefixes. For example, `src/` contains `src/value.py` but not
`src-other/value.py`. A file scope `result.txt` does not permit `result.txt.bak`.

For all paths involved in a status record:

- created, modified, deleted, and untracked paths must individually be allowed;
- renames must validate both old and new paths;
- a mode change is rejected, even when the path itself is in scope;
- symlinks are rejected for changed paths and for evidence paths;
- a non-existent target may be created only if each existing parent resolves
  inside the assigned registered worktree;
- `.gitignore` is not specially allowed. It is allowed only if it is explicitly
  declared and lies in the configured repository-level writable roots.

## Executor Proposal Contract

### New model-facing contract

Add a new exact contract:

```text
dispatcher.executor_proposal.v2
```

The executor proposal is a distinct object from the existing authoritative
`dispatcher.executor_result.v1`. It records what the executor claims to have
attempted; it cannot assert facts reserved for dispatcher inspection.

Required common fields:

- `proposal_version: 2`;
- `response_contract: "dispatcher.executor_proposal.v2"`;
- `dispatch_id`, `attempt`, and `step_id`;
- a repository base coordinate containing `repo_id` and `base_revision`;
- ordered evidence declarations containing only `artifact_id`, `relative_path`,
  and `media_type`;
- ordered acceptance self-reports, one for every criterion ID;
- a bounded nonempty summary; and
- an optional transcript reference.

Outcome variants:

- `completed`;
- `blocked`, which requires a nonempty list of bounded blockers; and
- `failed`, which requires one stable `failure_code`.

The proposal has no fields for:

- final revision;
- patch hash;
- evidence hash or size;
- model-authoritative verification; or
- commit message, Git argv, branch, remote, identity, or any other Git control.

Acceptance self-reports exist solely to preserve exact criterion coverage in the
model response and to make the prohibition on model test execution observable.
Their only accepted status is `not_run`. The dispatcher must not derive
verification authority from them. A model that returns `passed`, `failed`, an
unknown criterion, duplicate criterion, missing criterion, or extra criterion
is rejected.

For a completed proposal, evidence declarations must match the normalized plan's
evidence requirements exactly and in plan order. Blocked and failed proposals
also retain exact criterion coverage, but do not authorize a commit.

### Dispatcher-generated authoritative result

Keep `dispatcher.executor_result.v1` as the authoritative downstream result
shape for workflow state, forwarding, and reviewer input. It is no longer a
model-facing completion contract. After successful proposal validation,
inspection, verification, and either structured commit or authoritative patch
derivation, the dispatcher constructs it with:

- the dispatch's repository base revision;
- the dispatcher-inspected resulting revision or patch SHA-256;
- dispatcher-calculated evidence hashes and sizes;
- dispatcher-owned verification records projected to the existing result
  verification shape; and
- the executor proposal summary and outcome only where compatible with the
  authoritative result state.

This is not response repair. The executor proposal, structured Git record, and
authoritative executor result are separate durable records with separate
provenance.

## Permission And Approval Boundary

### OpenCode executor permissions

`commit` remains a semantic plan authorization. It means that the dispatcher
may invoke the structured Git capability after the other invariants pass. It
does not mean that the executor receives Bash Git permission.

Update `src/dispatcher/permissions.py` so executor dispatches:

- retain `inspect` and native `edit`/`write` only when authorized;
- continue to remove `verify`, because checks are dispatcher-owned;
- deny every raw Git mutation action, including staging and commit;
- deny test runners and arbitrary Bash;
- no longer receive evidence hash/size diagnostic Bash allowances; and
- cannot gain a model Git allowance through any configured policy layer.

The permission implementation must preserve a visible deny for model Git
patterns rather than merely relying on an undocumented absence of allow rules.
Reviewer and supervisor immutable read-only ceilings remain unchanged.

The project must not add unverified OpenCode path-pattern permissions as though
they were an OS boundary. `writable_paths` is enforced by dispatcher validation,
not by a claim that OpenCode itself enforces file paths.

### Real-operation manifest

Extend `RolePermissionManifest` in `src/dispatcher/operation.py` so a
real-operation approval binds both:

- the exact child-process permission digest for every participating role; and
- the dispatcher structured-Git capability digest for the executable step.

The structured capability digest must canonically include at least:

- capability version;
- repository ID and `commit_policy`;
- step ID and ordered `writable_paths`;
- resolved ordered evidence repository paths;
- base commit authorization;
- deterministic commit-message format;
- configured author/committer identity digest; and
- exact Git safety policy version.

The approval must fail if either the child permission manifest or dispatcher
capability manifest differs from the approved value. This prevents a safe-looking
OpenCode permission digest from approving a changed dispatcher Git capability.

## Explicit Commit Identity Configuration

Add a strict YAML-backed dispatcher Git identity in `src/dispatcher/config.py`.
It must contain nonempty author/committer name and email values, with control
characters rejected. It has no environment or code fallback.

The identity is used only by the dispatcher structured Git capability. It must
be included in the project config digest, generated config schema, public
example configuration, fixture configuration, and real-operation capability
digest.

The implementation must not alter repository-local Git configuration to set the
identity. It passes a bounded sanitized Git environment and explicit command
configuration for the single commit invocation.

## Structured Git Capability

### Placement and API

Add a focused module, for example `src/dispatcher/git_commit.py`, rather than
embedding Git side effects in the workflow state machine. It should accept only
dispatcher-owned typed inputs:

- assigned worktree path from `PreparedDispatch.workdir`;
- immutable pre-dispatch `RepositoryCoordinate` and snapshot;
- validated proposal and plan step;
- exact normalized changed path list;
- authoritative verification records;
- derived evidence manifest;
- deterministic message;
- explicit configured identity; and
- durable commit intent ID.

It returns only typed dispatcher observations: stage transcript, commit
transcript, final revision, and inspected post-commit snapshot. It must have no
interface for an arbitrary Git subcommand, arbitrary options, arbitrary
environment, arbitrary pathspec, or model-provided commit message.

### Preconditions

Before creating commit intent, the dispatcher must:

1. Reload the authoritative run and confirm dispatch identity, attempt,
   repository coordinate, worktree ID, expected branch, remote, and active
   repository lease.
2. Parse and validate exactly one executor proposal object, including duplicate
   JSON-key rejection before Pydantic validation.
3. Confirm the proposal identity and base revision match the active dispatch.
4. Inspect the assigned repository or child worktree with `require_clean=False`.
5. Confirm that HEAD is still the dispatch base revision, the real index is
   clean, and no other actor moved the branch.
6. Validate every observed change against `writable_paths`, configured
   `writable_roots`, evidence requirements, external-root snapshot, path type,
   mode, and symlink rules.
7. Reject an empty change set for a required-commit completed proposal.
8. Run every dispatcher-owned acceptance check against a disposable copy of that
   exact dirty worktree through `darwin_seatbelt_v1`.
9. Require every check to pass. A check failure does not invoke Git and leaves
   the original worktree for operator reconciliation.
10. Derive evidence hashes, sizes, and the relevant repository manifest from
    dispatcher inspection, not proposal fields.

For `commit_policy: prohibited`, perform all non-Git validation and verification
steps, derive the authoritative working patch SHA-256, persist the authoritative
v1 result, and do not create a structured commit record or run a Git mutation.

### Git hardening

The capability performs Git work only through argv execution with `shell=False`,
`stdin=DEVNULL`, bounded stdout/stderr, an explicit timeout, and a sanitized
environment. It uses the assigned `PreparedDispatch.workdir`, never the
configured repository root when a worktree barrier supplied a child worktree.

Before staging, inspect and reject local repository configuration that could
cause unexpected process execution or configuration indirection. At minimum the
implementation must reject active local includes and external command settings
for Git filters, hooks, and filesystem monitors. It must execute with system and
global Git configuration disabled, hooks disabled, signing disabled, terminal
prompting disabled, and a fixed noninteractive pager/editor environment.

The allowed capability operation sequence is fixed:

1. Build the expected tree in a dispatcher-owned temporary Git index using only
   the exact sorted allowed path list.
2. Persist the immutable commit intent before changing the real index.
3. Run `git add -A -- <exact sorted paths>` against the assigned worktree. The
   `-A` is required to stage declared deletions while the pathspec remains exact.
4. Inspect the real index and require its tree ID to equal the persisted expected
   tree. Require every staged path to equal the validated path set.
5. Persist the `STAGED` observation, including bounded argv/transcript hashes.
6. Run a single commit with disabled hooks and signing, fixed identity, and a
   dispatcher-generated message.
7. Inspect the committed revision, parent, tree, author/committer identity,
   subject, clean status, evidence manifest, branch, worktree ID, and remote.
8. Persist the commit observation and authoritative result before any forwarding
   is generated.

The deterministic message is generated only from trusted dispatcher values:

```text
dispatcher: <step_id> attempt <n>
```

The implementation must pass it as `git commit -m <message>`. It must not use
`git commit -- <message>`, which treats the message as a pathspec rather than a
commit message.

The capability must not construct or execute commands for push, fetch, pull,
checkout, switch, merge, rebase, reset, clean, tag, branch creation, worktree
creation, `git config`, or submodule operations. Existing dispatcher-owned
worktree provisioning and deterministic integration remain separate, audited
operations.

### Postconditions

For `commit_policy: required`:

- HEAD is a new commit whose sole parent is the dispatch base revision.
- The committed tree equals the persisted candidate tree.
- The commit subject and configured identity equal the persisted intent.
- The assigned worktree has no staged, unstaged, untracked, mode, or rename
  residue.
- The post-commit evidence manifest is derived and matches the evidence used to
  construct the authoritative result.
- The dispatcher-generated revision and evidence are the only values forwarded
  to reviewers and supervisors.

For `commit_policy: prohibited`:

- HEAD remains the base revision.
- The dispatcher-derived working patch SHA-256 identifies the exact accepted
  dirty worktree.
- Review and forwarding retain existing immutable patch semantics.

## Repository Inspection Changes

Extend `src/dispatcher/repository.py` rather than relying only on the current
coarse `RepositoryChange` categories.

Required additions:

- expose whether a path is staged, unstaged, untracked, renamed, copied,
  deleted, type-changed, or mode-changed;
- preserve both paths of a rename or copy record;
- distinguish a clean index from a clean worktree;
- produce a canonical, sorted validated path set for the structured capability;
- inspect the temporary-index tree and real-index tree without shell expansion;
- provide one reusable exact-scope validator used by both ordinary and worktree
  dispatches; and
- reject evidence symlinks and changed-path symlinks before hashes, tests, or
  Git staging are accepted.

The existing repository-wide `writable_roots` validation remains an outer guard.
It must not be removed merely because `writable_paths` is more restrictive.

## Durable State And Recovery

SQLite cannot atomically commit a Git repository. The durable record therefore
must bridge the external Git side effect with fail-closed state transitions.

### Migration

Increase `CURRENT_SCHEMA_VERSION` from 6 to 7 and add a dedicated
`structured_git_commits` table keyed by `(run_id, dispatch_id)`. Keep this
record outside `RunRecord` unless a compact immutable summary is needed for
status rendering. This avoids adding a fake optional default to the authoritative
workflow contract merely to represent a database side effect.

At minimum persist:

- commit record ID and capability version;
- run ID, dispatch ID, step ID, repository ID, and worktree ID;
- proposal JSON and canonical proposal digest;
- state: `PROPOSAL_RECEIVED`, `CHECKED`, `COMMIT_INTENT_PERSISTED`, `STAGED`,
  `COMMITTED`, or `RECONCILIATION_REQUIRED`;
- base revision and pre-commit repository snapshot digest;
- canonical expected changed paths and candidate tree ID;
- deterministic message and identity/capability digests;
- exact Git argv arrays or canonical hashes of them;
- bounded/redacted stage and commit transcript hashes, exit statuses, and
  timestamps;
- resulting revision and post-commit snapshot JSON/digest; and
- immutable creation/update timestamps.

The state store must expose typed methods to persist proposal receipt, commit
intent, staging observation, committed observation, and safe recovery lookup.
It must not expose a generic SQL or arbitrary-side-effect API to workflow code.

### Required ordering

For a completed required-commit proposal:

1. Persist proposal receipt after strict parsing and before using it for a state
   transition.
2. Validate writes and execute dispatcher-owned checks.
3. Persist `CHECKED` data containing authoritative checks, evidence inspection,
   candidate tree, and expected exact path set.
4. Persist `COMMIT_INTENT_PERSISTED` before touching the real index.
5. Stage only the exact paths and persist `STAGED` observation.
6. Create the Git commit.
7. Inspect post-commit state and atomically persist `COMMITTED`, the
   dispatcher-generated v1 executor result, authoritative verification, and the
   post-commit repository snapshot.
8. Only then transition to the existing durable forwarding path.

No forwarding is constructed from a proposal or partial commit record.

### Recovery rules

`classify_recovery()` and the recovery command must understand structured Git
records. They must never classify an interrupted structured commit as
automatically safe to retry.

| Durable record / repository observation | Recovery disposition |
|---|---|
| No commit intent and dispatch was `PREPARED` or `RUNNING` | Existing operator reconciliation behavior. |
| Proposal or checks persisted, HEAD is base, index/worktree is dirty | Operator reconciliation; no automatic staging or commit. |
| Commit intent persisted, HEAD is base, index state is clean or staged | Operator reconciliation; no automatic continuation or reset. |
| HEAD is one exact matching commit but final state write was interrupted | Adopt the commit only after exact parent, tree, identity, subject, path-set, and post-inspection verification; then atomically build the authoritative result. |
| HEAD diverged, tree differs, parent differs, extra changes exist, identity/subject differs, or evidence differs | Mark reconciliation required and forbid automatic retry. |
| Durable record is `COMMITTED` but forwarding is absent | Reuse the current forwarding-required path; do not invoke Git. |

The implementation must never call `git reset`, `git clean`, or any destructive
Git command as automatic recovery. Operator reconciliation may require a human

### Existing state compatibility

Persisted normalized plans from before this step lack required
`writable_paths`. They are intentionally not compatible with a resumed Step 20
dispatcher. Loading such a run must fail clearly before execution rather than
infer a path scope or silently expand it to repository-wide authority. The
operations and migration documentation must explain that a new approved plan and
run are required.

## Workflow Wiring

### `SequentialExecutionCoordinator`

In `src/dispatcher/execution.py`, executor processing becomes:

1. Strictly parse one proposal JSON object.
2. Persist proposal receipt through the workflow/state-store boundary.
3. For `completed`, invoke dispatcher-owned verification against the assigned
   worktree and then invoke the structured Git capability only for
   `commit_policy: required`.
4. Build a dispatcher-generated authoritative v1 executor result.
5. Pass that authoritative result to the existing state-transition, review, and
   forwarding code.
6. For `blocked` and `failed`, verify that the assigned worktree remains equal
   to the pre-dispatch state. Any residue is a repository-validation failure and
   requires reconciliation; neither outcome invokes Git.

The explicit mock workflow mode may use a deterministic injected mock verifier
for tests, but it must derive its authority from the plan/check fixture and not
from proposal self-report values. There is no mock-only model-commit exception.

### `SequentialWorkflow`

In `src/dispatcher/sequential.py`:

- replace executor proposal parsing, context validation, and template generation
  without changing the reviewer result contract;
- add a workflow transition that converts validated proposal plus dispatcher
  observations into the authoritative v1 executor result;
- retain the current review-target construction, but source its result revision,
  patch hash, evidence hashes, and authoritative verification solely from the
  dispatcher-created result and durable commit record;
- include the authoritative repository coordinate in executor forwarding, in
  addition to evidence and verification records; and
- preserve at-least-once supervisor forwarding semantics after the authoritative
  result is durable.

The worker prompt must explicitly say that an executor:

- may write only the supplied `writable_paths` values;
- must not run acceptance tests or substitutes;
- must not stage, commit, push, modify branches, modify Git configuration, or
  invoke Git mutation commands;
- must return only `dispatcher.executor_proposal.v2` JSON; and
- must mark all acceptance self-reports as `not_run`.

Remove the previous instructions to calculate evidence hashes/sizes or confirm a
clean post-commit status. The dispatcher owns those facts.

## Worktree Barrier Interaction

For a same-repository `worktree_barrier` batch:

- every structured commit runs in the exact child worktree stored on its
  `PreparedDispatch`, never in the source repository root;
- child commits are created before their review dispatches and remain available
  on their child branch through rework/review;
- current dispatcher-owned integration remains a separate Git side effect and
  must retain its existing durable integration/cleanup lifecycle;
- scheduler admission must additionally reject child steps whose declared
  `writable_paths` scopes overlap after resolving evidence paths, even when their
  resource locks differ; and
- recovery must preserve temporary worktrees and branches when child commits are
  unresolved. It must not force cleanup solely because a structured commit
  record exists.

This closes a current gap where resource locks alone may permit two independent
child branches to edit the same path and later conflict during deterministic
integration.

## Files Expected To Change

### Runtime

- `src/dispatcher/config.py`
- `src/dispatcher/plan.py`
- `src/dispatcher/results.py`
- `src/dispatcher/schema_export.py`
- `src/dispatcher/permissions.py`
- `src/dispatcher/repository.py`
- `src/dispatcher/git_commit.py` (new focused capability)
- `src/dispatcher/state_store.py`
- `src/dispatcher/workflow.py`
- `src/dispatcher/sequential.py`
- `src/dispatcher/execution.py`
- `src/dispatcher/operation.py`
- `src/dispatcher/workspaces.py`
- scheduler module(s) responsible for batch/worktree admission

### Fixtures and tests

- `tests/helpers.py`
- `tests/fixtures/opencode/fake_cli.py`
- `tests/unit/test_plan.py`
- `tests/unit/test_results.py`
- `tests/unit/test_permissions.py`
- `tests/unit/test_repository.py`
- new focused `tests/unit/test_git_commit.py`
- `tests/unit/test_sequential.py`
- `tests/unit/test_execution.py`
- `tests/unit/test_operation.py`
- `tests/fault_injection/test_state_store.py`
- `tests/fault_injection/test_sequential_execution.py`
- `tests/integration/test_sequential_git_e2e.py`
- `tests/integration/test_batch_execution.py`
- `tests/integration/test_workspace_barrier.py`
- `tests/integration/test_execute_command_disposable.py`
- `tests/live/test_real_operation_disposable.py`
- contract/schema/documentation tests as needed

### Published contracts and documentation

- `schemas/normalized-plan-v2.json`
- `schemas/project-config-v1.json`
- `schemas/executor-proposal-v2.json` (new)
- `schemas/executor-result-v1.json`
- `schemas/workflow-state-v1.json` if the public state model changes
- `schemas/README.md`
- `config/projects/example.yaml`
- `docs/normalized-plan-schema.md`
- `docs/config-schema.md`
- `docs/protocol.md`
- `docs/workflow-state-schema.md`
- `docs/operations.md`
- `docs/migration.md`
- `README.md` where it summarizes execution guarantees

No private project, state, authentication, credential, or local Tier 2 file is
required for this implementation or its non-live test coverage.

## Test Matrix

### Plan and configuration validation

- missing `writable_paths` rejects;
- absolute path, parent traversal, empty segment, duplicate, ambiguous
  file/directory entry, overlap, `.git`, and root-wide path reject;
- valid file and directory paths accept;
- paths outside configured repository `writable_roots` reject;
- `modify` without a scope rejects;
- `commit` without `modify`, a scope, or `commit_policy: required` rejects;
- evidence paths outside `writable_paths` reject;
- a plan/config/identity mutation changes the corresponding digest and
  invalidates approval;
- commit identity is required, type-checked, and rejects control characters;
- no compatibility acceptance exists for schema-v2 plans missing the new field.

### Proposal parsing and prompt contract

- exact proposal contract literal and version reject every alternative;
- one-object parsing rejects prose, Markdown, duplicate keys, non-finite JSON,
  missing fields, null fields, blank fields, wrong enums, and extra fields;
- wrong dispatch, attempt, step, repository, or base revision rejects;
- completed proposal requires exact ordered evidence coverage;
- all outcomes require exact ordered criterion self-report coverage;
- any self-report status other than `not_run` rejects;
- proposal fields for revision, patch hash, evidence hash/size, commit message,
  or arbitrary Git data reject as extra fields;
- blocked/failed proposals cannot cause a commit;
- executor prompt exposes only the proposal schema, exact path scope, and no
  test/Git mutation instructions;
- reviewer prompt and reviewer result schema remain v1 and continue to receive
  dispatcher-owned verification records.

### Permission and approval tests

- an executor with `commit` authorization has no raw Git allow rule;
- `git add`, `git commit`, `git push`, `git config`, test runner, interpreter,
  redirection, chaining, and arbitrary Bash all resolve to deny before execution;
- native executor write remains allowed only when `modify` is authorized;
- reviewer/supervisor read-only ceilings remain unchanged;
- capability digest changes for path scope, evidence resolution, identity,
  message format, capability version, or safety policy changes;
- real-operation approval rejects missing, stale, mismatched, or extra capability
  digest material.

### Repository/path validation tests

- exact file scope allows that file and no sibling;
- directory scope allows descendants and no prefix collisions;
- created, modified, deleted, untracked, and both rename paths are validated;
- staged model changes reject before capability invocation;
- mode changes, copies, unsupported status output, symlinks, `.git` paths,
  parent escapes, external-root changes, and changed evidence symlinks reject;
- a symlink parent that resolves outside the assigned worktree rejects;
- only the worktree associated with the prepared dispatch is inspected and
  changed in worktree-barrier mode.

### Structured Git capability tests

- every subprocess call has an exact expected argv and `shell=False`;
- no capability API can construct a push, branch, merge, checkout, reset,
  config, clean, submodule, or arbitrary Git command;
- system/global configuration, hooks, signing, pager/editor, prompting, and
  process-executing local Git configuration are disabled or rejected;
- temporary-index candidate tree equals the real staged tree;
- staged path list is exact, sorted, and includes declared deletion paths only;
- deterministic message and configured identity are used;
- pre-commit index dirtiness rejects;
- post-commit parent/tree/identity/subject and clean snapshot validate;
- revision, evidence hashes, evidence sizes, and manifest originate only from
  dispatcher inspection;
- Git staging/commit timeout or nonzero exit retains durable intent and requires
  reconciliation without automatic reset;
- no-op completed required-commit proposal rejects without creating a commit.

### State and crash recovery tests

- schema-v6 database migrates to v7 without losing existing durable rows;
- proposal receipt is durable before verification/commit work;
- crash after proposal and before checks creates no commit;
- crash after checks and before commit creates no commit and is not automatically
  retried;
- crash after staging creates no automatic reset/continuation;
- crash after commit before final SQLite transition adopts exactly one matching
  commit and produces one authoritative result;
- mismatched parent/tree/identity/message/path set/evidence rejects adoption and
  creates reconciliation;
- crash after durable committed result but before forwarding enters the existing
  forwarding-required path without invoking Git;
- a plan persisted before `writable_paths` produces a clear non-resumable
  validation error rather than receiving an inferred scope.

### End-to-end and disposable proof

- fake sequential execution writes files, returns proposals, passes
  dispatcher-owned verification, receives dispatcher-created commits, and reaches
  review/acceptance with a clean repository;
- rework produces one additional dispatcher-created commit and replaces the
  immutable review target correctly;
- cross-repository bounded batches create independent dispatcher commits;
- same-repository worktree barrier commits in child worktrees, rejects
  overlapping scopes, integrates accepted branches deterministically, and cleans
  temporary worktrees only after the established lifecycle;
- non-committing repository flow produces dispatcher-derived patch identity and
  never invokes the structured capability;
- real macOS disposable executor writes the allowed output and evidence but does
  not run tests or Git commands;
- Seatbelt verification passes against the disposable copy, the dispatcher
  creates the commit, and the reviewer accepts immutable dispatcher-generated
  evidence;
- controlled policy probes prove executor Git/test commands are denied before
  execution; live tool-event evidence must show no successful forbidden command;
- changed path outside the manifest fails before Git staging;
- sequential, review/rework, cancellation, reconciliation, cross-repository
  batch, worktree barrier, and halt scenarios all pass from a committed revision
  using the intended heterogeneous role model matrix.

## Implementation Order

1. Add plan path scope and explicit commit identity schema/config validation.
2. Add proposal models/parser/schema export while retaining v1 authoritative
   executor result parsing for dispatcher-created data.
3. Remove executor raw Git/evidence Bash authority and update real-operation
   manifest composition.
4. Implement reusable exact path/change inspection and dedicated structured Git
   capability unit tests.
5. Add schema-v7 durable commit records and recovery classification before
   wiring any real Git side effect through the coordinator.
6. Rewire executor prompts, result flow, authoritative v1 construction,
   forwarding, and review-target lookup.
7. Add worktree-overlap admission and verify child-worktree capability routing.
8. Convert fake, integration, fault-injection, and live disposable fixtures from
   model commits to write-only proposals.
9. Regenerate schemas and update normative documentation, example configuration,
   migration guidance, and contract tests.
10. Run focused tests during each increment, then the full non-live suite,
    formatting/lint/type checks, package checks, wheel build, and macOS
    disposable proof.
11. Commit the audited implementation before running the full intended model
    matrix and requesting a fresh independent final review.

## Verification Commands

The coding session should run bounded commands with explicit timeouts. The final
verification set includes at least:

```sh
.venv/bin/python -m pytest tests -q -m "not live_opencode"
.venv/bin/ruff check src tests
.venv/bin/python -m mypy src
git diff --check
.venv/bin/python -m pip check
.venv/bin/python -m pip wheel . --no-deps -w <temporary-wheel-directory>
```

Then run the targeted real disposable suite from a committed revision with the
intended role-model environment. Do not treat a Luna-only fallback proof as the
final heterogeneous matrix proof.

## Completion Evidence

Write:

```text
markdown/reports/dispatcher-structured-git-capability-report-2026-08-12.md
```

The report must distinguish, for every demonstrated executor attempt:

1. model proposal contract/digest;
2. dispatcher path inspection and evidence manifest;
3. dispatcher-owned verification records and Seatbelt backend;
4. durable structured commit intent, staged tree, commit argv/transcript hashes,
   identity digest, and resulting revision;
5. dispatcher-generated authoritative executor result;
6. reviewer target and acceptance; and
7. any reconciliation disposition.

No narrative claim substitutes for machine output, exact durable IDs, revisions,
or hashes. The report must also state test environments, unverified limits, and
the remaining fact that OpenCode permission UX is not an OS filesystem sandbox.
