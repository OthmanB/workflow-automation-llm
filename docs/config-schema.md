# Project Configuration Schema V1

`src/dispatcher/config.py:ProjectConfigModel` is the runtime authority. The
generated schema at `schemas/project-config-v1.json` is the machine-readable
contract. Every field below is required unless marked optional; unknown fields
and type coercion fail closed.

```yaml
schema_version: 1
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
  mode: mock_only
  protocol_version: 1
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
    supervisor-class: {default: deny, actions: {inspect: allow}}
    executor-class: {default: deny, actions: {inspect: allow, modify: allow, verify: allow}}
    reviewer-class: {default: deny, actions: {inspect: allow, modify: deny, verify: allow}}
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

## Key Rules

- Config-relative paths resolve from the project YAML, not the process working
  directory. This includes observability archive paths.
- Exactly one supervisor and at least one executor are required. Worker role
  capacities must exactly match configured executor and reviewer keys.
- `scheduling: sequential` requires `max_active_dispatches: 1`.
  `bounded_parallel` is explicit and only permits independent,
  resource-unconflicted cross-repository children. Same-repository work remains
  serialized.
- Review profiles declare `review_schedule`, `multi_review`, reviewer role
  keys, and required acceptance count. Mandatory plan or project review cannot
  be weakened by profile choice or an operator waiver.
- Enabled budgets require measured worker usage. Limits halt or create a durable
  operator decision according to `budget.on_limit`; missing required usage fails
  closed.
- Permission layers compile in global, project, repository, role-class,
  concrete-role, then dispatch-authorization order. Undeclared actions deny.
  `ask` prevents `--auto`.
- Observability retention only touches derived artifacts. It never deletes
  SQLite state, active-run artifacts, or unresolved dispatch data.

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

## Evidence, Review, and Parallelism

Executor evidence must exactly satisfy declared IDs, relative paths, media
types, hashes, and sizes. Reviews bind to the immutable executor result and its
artifact hashes. Bounded batches require independent ready steps and fresh
sessions; a repository lock serializes same-repository work until a durable
worktree/merge lifecycle is designed.

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
