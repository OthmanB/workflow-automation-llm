# Project Configuration Schema V1

`src/dispatcher/config.py:ProjectConfigModel` is the runtime authority. The
generated schema at `schemas/project-config-v1.json` is the machine-readable
contract. Every field below is required unless marked optional; unknown fields
duplicate YAML keys, and type coercion fail closed.

```yaml
schema_version: 2
project: {project_id: example-project, name: Example Project, description: Public example}
sources:
  specifications_dir: ./specifications
  plans_dir: ./plans
  plan_files: [execution-plan.md]
  roles_files: [execution-roles.md]
state:
  directory: ./state/example-project
  lease_heartbeat_seconds: 30
  lease_stale_after_seconds: 120
repositories:
  application:
    root: ../application
    expected_remote: {name: origin, url: https://github.com/example/application.git}
    default_branch: main
    evidence_roots: [evidence]
    writable_roots: [.]
    external_roots: []
    commit_policy: required
    permission_policy: repository
    allow_shared_writable_roots: false
roles:
  supervisor:
    supervisor-role: {model: provider/supervisor, variant: standard, display: Supervisor, permission_policy: supervisor}
  executors:
    executor-role: {model: provider/executor, variant: standard, display: Executor, permission_policy: executor}
  reviewers:
    reviewer-role: {model: provider/reviewer, variant: standard, display: Reviewer, permission_policy: reviewer}
profile: {profiles_file: ./profiles.yaml, profile_id: balanced}
execution:
  mode: mock_workflow_test
  protocol_version: 1
  verification_backend: darwin_seatbelt_v1
  structured_git:
    capability_version: 1
    author_name: Dispatcher Executor
    author_email: dispatcher-author@example.invalid
    committer_name: Dispatcher Committer
    committer_email: dispatcher-committer@example.invalid
    timeout_seconds: 30
    max_output_bytes: 65536
  scheduling: sequential
  concurrency:
    max_active_dispatches: 1
    max_batch_size: 1
    role_capacities: {executor-role: 1, reviewer-role: 1}
    failure_mode: wait_for_started
  default_repo_id: application
  timeout_seconds: 1800
  termination_grace_seconds: 15
  max_output_bytes: 1048576
  max_rounds_per_step: 4
  stall_policy:
    maximum_retries_per_step: 2
    cooldown_seconds: 180
    on_exhausted: ask
  halt_mode: ask_on_ambiguity
  underspec_mode: ask
  response_template: "[response_content_chat]"
review_policy:
  mandatory_review: false
  critical_risk_tags: [critical]
  allow_operator_waiver: false
budget:
  enabled: false
  max_run_cost_usd: 10.0
  max_step_cost_usd: 5.0
  max_context_tokens: 100000
  on_limit: halt
observability:
  log_format: json
  log_level: INFO
  retention:
    mode: archive
    archive_directory: ./state/archive
    max_transcripts_per_run: 100
    max_reports: 100
    max_audit_exports: 100
    max_support_bundles: 50
    max_archived_artifacts: 1000
permission_policies:
  global_policy: global
  project_policy: project
  role_class_policies: {supervisor: supervisor-class, executor: executor-class, reviewer: reviewer-class}
  policies:
    global: {default: deny, actions: {}}
    project: {default: deny, actions: {}}
    repository: {default: deny, actions: {inspect: allow, modify: allow, verify: allow}}
    supervisor-class: {default: deny, actions: {inspect: allow, modify: deny, verify: deny, commit: deny, push: deny, force_push: deny, create_branch: deny}}
    executor-class: {default: deny, actions: {inspect: allow, modify: allow, verify: allow, commit: deny, push: deny, force_push: deny, create_branch: deny}}
    reviewer-class: {default: deny, actions: {inspect: allow, modify: deny, verify: deny, commit: deny, push: deny, force_push: deny, create_branch: deny}}
    supervisor: {default: deny, actions: {}}
    executor: {default: deny, actions: {}}
    reviewer: {default: deny, actions: {}}
evidence:
  hash_algorithm: sha256
  require_content_hashes: true
  immutable: true
  allow_unexpected_writes: false
# Optional: absence disables preflight.
preflight:
  enabled: true
  models_smoke_test: false
  smoke_prompt: "Reply with exactly: OK"
  credentials: []
  require_git_remote: true
  disk_space_min_mb: 500
```

The schema-version-2 contract accepts optional `mcp` and per-role `mcp_tools`
fields. Their absence selects inherited OpenCode MCP configuration and the
default research catalog. See the MCP Servers and Role Tools section below for
explicit override and disable forms.

When `models_smoke_test` is enabled, the configured prompt must cause every
tested model to return the exact marker `OK` after surrounding whitespace is
trimmed. The preflight check does not accept a substring match.

## Key Rules

- Config-relative paths resolve from the project YAML, not the process working
  directory. This includes observability archive paths.
- Exactly one supervisor and at least one executor are required. Worker role
  capacities must exactly match configured executor and reviewer keys.
- `scheduling: sequential` requires `max_active_dispatches: 1`.
  `bounded_parallel` is explicit and only permits independent,
  resource-unconflicted cross-repository children. Same-repository work remains
  serialized.
- `same_repository_mode: worktree_barrier` declares the owner-only root and
  branch prefix used for temporary same-repository worktrees. This manager
  supports only `commit_policy: required`. Independent same-repository executor
  children run behind one barrier, reviewers route to the same child worktree,
  accepted child branches merge in deterministic order, and temporary branches
  and worktrees are removed after promotion.
- Review profiles declare `review_schedule`, `multi_review`, reviewer role
  keys, and required acceptance count. Mandatory plan or project review cannot
  be weakened by profile choice or an operator waiver.
- Enabled budgets require measured worker usage. Limits halt or create a durable
  operator decision according to `budget.on_limit`; missing required usage fails
  closed.
- `execution.stall_policy` is separate from `budget`: it controls bounded retries
  after timeouts, interruptions, temporary connection/rate-limit failures, or
  incomplete provider output. Provider quota, billing, authentication, and
  unknown errors do not retry automatically.
- The canonical semantic action vocabulary is `inspect`, `modify`, `verify`,
  `commit`, `push`, `force_push`, and `create_branch`. Every role-class policy
  must explicitly decide all seven; omission is a configuration error. Other
  policy layers may be sparse.
- Configured permission layers use ordered override precedence: global,
  project, repository, role-class, then concrete-role. Dispatch authorization
  subsequently denies undeclared actions.
  `ask` prevents `--auto`. Before compilation, executors retain ordered step
  actions, reviewers are narrowed to `inspect` only when the step authorizes
  it, supervisors are inspect-only, and executor `verify` is removed because
  structured checks are dispatcher-owned. After compilation, reviewer and
  supervisor `edit`/`write` are forced to `deny` and their complete Bash map is
  replaced by a hardcoded `"*": deny` fallback plus exact allows for `pwd`,
  `ls`, `git status --porcelain=v1`, `git branch --show-current`, `git rev-parse
  HEAD`, and `git diff --no-ext-diff --no-textconv`; configuration cannot
  override that ceiling. No diagnostic allow pattern has wildcard arguments.
- Observability retention only touches derived artifacts. It never deletes
  SQLite state, active-run artifacts, or unresolved dispatch data.
- Schema v2 defines `mock_workflow_test` and `real_operation`. The public example
  and ordinary development configurations must use `mock_workflow_test`.
  `real_operation` is accepted only by the separately guarded `dispatcher
  execute` command.
- `execution.verification_backend` is explicit. `darwin_seatbelt_v1` is the
  supported local macOS backend for dispatcher-owned checks; it denies network
  and write access outside the disposable verification workspace.
  `linux_bwrap_v1` is an optional future Linux backend with an unshared network
  namespace. `direct_test_v1` is restricted to `mock_workflow_test` fixture
  configurations; it preserves dispatcher-owned check execution semantics but
  is rejected for `real_operation`.
- `execution.structured_git` is explicit and has no runtime identity fallback.
  Its bounded dispatcher-owned capability calculates a candidate tree with an
  isolated temporary index, stages only the validated sorted path set in the
  real index, disables system/global config, hooks, signing, prompting, editors,
  and pagers, and creates the deterministic message `dispatcher: <step_id>
  attempt <n>`. Dangerous local Git configuration is rejected.

## Permission Examples

Permission policies are semantic and deny by default. A role/repository policy
can allow inspection while requiring an operator-mediated `ask` for mutation:

```yaml
policies:
  safe-repository:
    default: deny
    actions: {inspect: allow, modify: ask, verify: allow}
```

The dispatcher compiles this to the supported OpenCode permission payload for
the exact repository, role, and dispatch authorization. `ask` disables auto
approval. A `deny` remains denied even if another applicable rule permits
`--auto`; the fake-child integration suite verifies allow, ask, and deny.
Reviewer and supervisor hard ceilings are then applied after these configurable
layers. These OpenCode permissions are UX controls rather than OS filesystem or
network isolation. The finite exact diagnostic allowlist does not make arbitrary
Bash safe and does not establish a no-network guarantee for executors.

## Evidence, Review, and Parallelism

Executor proposals declare only evidence IDs, relative paths, and media types.
The dispatcher derives hashes and sizes from repository inspection. Reviews
bind to the immutable dispatcher-generated executor result and artifact hashes.
Bounded batches require independent ready steps and fresh sessions;
`worktree_barrier` additionally requires non-overlapping step writable scopes.

## Profiles Document

`profile.profiles_file` points to a separate schema-v1 document:

```yaml
schema_version: 1
profiles:
  balanced:
    review_schedule: critical
    multi_review: on_critical_only
    reviewer_role_keys: [reviewer-role, reviewer-two]
    required_acceptances: 2
default: balanced
```

`multi_review: off` requires exactly one acceptance. Other multi-review modes
require at least two and cannot name duplicate reviewer roles.

## MCP Servers and Role Tools

MCP has two schema-v2 modes. When the top-level `mcp` section is omitted,
dispatcher workers load the operator's normal OpenCode configuration directory.
An omitted or empty role `mcp_tools` list then receives the default Context7,
Repomix, and Semble research catalog. A nonempty role list narrows the inherited
catalog.

When a top-level `mcp` section is present, it takes precedence over the global
OpenCode MCP definitions. `environment_passthrough` names the parent variables
copied into the minimal child environment and `servers` defines the only managed
servers available to roles. An explicit empty registry disables MCP. Local
servers require a nonempty argv `command`; remote servers require an HTTP(S)
`url`. Both carry `enabled`.

```yaml
mcp:
  environment_passthrough: [CONTEXT7_TOKEN]
  servers:
    context7:
      type: remote
      enabled: true
      url: https://mcp.context7.com/mcp
      headers:
        Authorization: "Bearer {env:CONTEXT7_TOKEN}"
    repomix:
      type: local
      enabled: true
      command: [repomix-mcp]
      environment: {}
```

`environment_passthrough` names are copied from the parent process into the
isolated child environment (a missing name fails before OpenCode launch) and
are never written into prompts or persisted configuration. Server header and
environment values may use OpenCode `{env:NAME}` placeholders; only names
listed in `environment_passthrough` are resolvable in the child.

Every role tool must appear in the dispatcher's explicit tool catalog, which
currently contains the Context7 resolve/query methods, the Repomix pack/read/
search methods (excluding `repomix_generate_skill`), and the Semble
search/related methods. Validation rejects duplicate role tools, unknown
tools, tools whose catalog server is missing or disabled, empty local
commands, invalid remote URLs, and duplicate passthrough names. A role's
`mcp_tools` list compiles into exact OpenCode permission allow entries and
only the role's selected server definitions are emitted; unlisted methods keep
the global deny (roles with MCP tools require a deny-default permission
policy).

In inherited mode, worker OpenCode data/session directories remain isolated,
but `OPENCODE_CONFIG_DIR` points at the operator's global OpenCode configuration
and the parent process environment is inherited so existing MCP commands and
credential references work. The dispatcher inline permission map is merged last
and still denies unlisted methods.
