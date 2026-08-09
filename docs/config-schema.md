# Project Configuration Schema V1

The runtime schema is `src/dispatcher/config.py:ProjectConfigModel`. Its
machine-readable JSON Schema is generated at `schemas/project-config-v1.json`.
Every key shown below is required unless marked optional. Unknown fields are
rejected at every level. Scalar validation is strict: YAML booleans and strings
are not coerced into another type.

```yaml
schema_version: 1

project:
  project_id: example-project
  name: Example Project
  description: Project-neutral schema-v1 example

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
    expected_remote:
      name: origin
      url: https://github.com/example/application.git
    default_branch: main
    evidence_roots: [evidence]
    writable_roots: [.]
    permission_policy: repository
    allow_shared_writable_roots: false

roles:
  supervisor:
    supervisor-role:
      model: provider/supervisor-model
      variant: high
      display: Supervisor
      permission_policy: supervisor
  executors:
    executor-role:
      model: provider/executor-model
      variant: high
      display: Executor
      permission_policy: executor
  reviewers: {}

profile:
  profiles_file: ./profiles.yaml
  profile_id: balanced

execution:
  mode: mock_only
  protocol_version: 1
  scheduling: sequential
  default_repo_id: application
  timeout_seconds: 1800
  termination_grace_seconds: 15
  max_output_bytes: 1048576
  max_rounds_per_step: 4
  halt_mode: ask_on_ambiguity
  underspec_mode: ask
  response_template: "[response_content_chat]"

permission_policies:
  global_policy: global
  project_policy: project
  role_class_policies:
    supervisor: supervisor-class
    executor: executor-class
    reviewer: reviewer-class
  policies:
    global:
      default: deny
      actions: {}
    project:
      default: deny
      actions: {}
    repository:
      default: deny
      actions: {inspect: allow, modify: allow, verify: allow}
    supervisor-class:
      default: deny
      actions: {inspect: allow}
    executor-class:
      default: deny
      actions: {inspect: allow, modify: allow, verify: allow}
    reviewer-class:
      default: deny
      actions: {inspect: allow, modify: deny, verify: allow}
    supervisor:
      default: deny
      actions: {}
    executor:
      default: deny
      actions: {}
    reviewer:
      default: deny
      actions: {}

evidence:
  hash_algorithm: sha256
  require_content_hashes: true
  immutable: true
  allow_unexpected_writes: false

# Optional. Its absence disables preflight.
preflight:
  enabled: true
  models_smoke_test: true
  smoke_prompt: "Reply with exactly: OK"
  credentials: []
  require_git_remote: true
  disk_space_min_mb: 500
```

## Path and repository rules

- Paths in `sources`, `state`, `profile`, and repository `root` resolve against
  the project configuration file directory, never the process working directory.
- `state.lease_heartbeat_seconds` and `state.lease_stale_after_seconds` are
  required for transactional runtime ownership. The stale threshold must exceed
  the heartbeat interval; replacing a stale lease still requires an explicit
  operator approval reference.
- `plan_files` and `roles_files` are relative to `sources.plans_dir`; `..` and
  absolute paths are rejected.
- Repository evidence and writable roots are relative to their repository root,
  must exist, and cannot escape through a symlink.
- Registered repository roots must be unique. Overlapping writable roots are
  rejected unless every affected repository explicitly allows sharing.
- Every repository must match the exact configured Git remote URL before
  preflight or mock execution begins.
- Every configured role, repository, and explicit global/project/role-class
  layer must refer to an existing named permission policy. The dispatcher
  compiles layers in global, project, repository, role-class, concrete-role,
  then dispatch-authorization order. `actions` maps a semantic action to
  `allow`, `ask`, or `deny`; undeclared dispatch actions compile to denial.

## Profiles document

`profile.profiles_file` must point to another schema-v1 YAML document:

```yaml
schema_version: 1
profiles:
  balanced:
    review_schedule: critical
    multi_review: on_critical_only
default: balanced
```

Valid `review_schedule` values are `on_failure`, `critical`, and `always`.
Valid `multi_review` values are `off`, `on_critical_only`, and
`on_every_review`. Quote `"off"` when using YAML 1.1-compatible parsers.

## Current execution limit

Schema-v1 permits only `execution.mode: mock_only` and
`scheduling: sequential`. This is deliberate: real OpenCode dispatch,
policy enforcement, reviewed execution, and parallelism are unavailable until
their later remediation phases pass.
