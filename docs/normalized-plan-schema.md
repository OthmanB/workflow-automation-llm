# Normalized Plan Schema V2

The dispatcher executes an immutable `NormalizedPlan`, not a Markdown table.
The machine-readable schema is `schemas/normalized-plan-v2.json`; the runtime
model is `src/dispatcher/plan.py:NormalizedPlan`.

## Required top-level fields

```yaml
schema_version: 2
plan_id: example-plan
sources:
  - source_id: plan-source
    root: plans
    relative_path: execution-plan.md
    sha256: <64 lowercase hexadecimal characters>
    media_type: text/markdown
steps: []
```

`plan_digest` is calculated from `schema_version`, `plan_id`, and normalized
steps. It intentionally excludes source identities and hashes, so two import
adapters producing identical semantic steps have the same plan digest.
`source_digest` separately binds the approved source identities and content
hashes.

A `PlanApproval` is required before a schema-v2 run record can be created. It
contains the plan digest, source digest, an operator decision reference, and an
approval timestamp. Any semantic plan or source identity/hash change invalidates
that approval and requires a new operator decision.

## Required step fields

Every step explicitly declares:

- `ordinal`, `step_id`, `title`, and `repo_id`;
- `depends_on` and `required_inputs`;
- `produced_outputs` and `resource_locks`;
- `risk_tags`;
- `authorization` with nonempty `authorized_actions`, required `writable_paths`,
  and operator-gate status;
- nonempty `acceptance_criteria`, each with one dispatcher-owned structured
  `check` (`argv`, repository working directory, timeout/output bounds,
  expected exit codes, and `network_policy: deny`);
- nonempty `evidence_requirements`;
- a typed `review` obligation; and
- an explicit `retry` policy.

A step may additionally declare `cluster_operation` only as a static reference
to a repository-owned operation manifest:

```yaml
cluster_operation:
  target_name: integration-deploy
  operation_manifest_path: deploy/operations/sample.yaml
  requires_cluster_approval: true
  preauthorized_actions: [kubectl_server_dry_run, helm_upgrade_install]
  requires_automatic_rollback: true
```

The path is a normalized repository-relative YAML file, and the approval marker
and automatic rollback marker must be literal boolean `true`. `preauthorized_actions`
is a nonempty, ordered, duplicate-free subset of
`kubectl_server_dry_run`, `helm_upgrade_install`, `port_forward`, and
`tls_dc8_no_client_certificate_rejection`. It is the
action order the executor is authorized to create, not a later manifest hint.
`PlanStep` is the executable normalized plan node, so this reference cannot
appear on plan sources, artifacts, or other non-step metadata. Existing schema-v2
plans without the optional field are unchanged.

Empty lists are valid only when a field is structurally optional for the step,
such as `depends_on` on an initial step. Authorization, acceptance, and evidence
cannot be omitted or inferred from prose.

Verification commands are argv arrays and are never interpreted by a shell.
The dispatcher executes them in an isolated disposable copy of the inspected
repository. Model-supplied verification is a self-report; only the durable
dispatcher check record advances workflow state.

`writable_paths` is an exact repository-relative file or directory scope for
executor changes. Absolute paths, `..`, `.git`, repository-root scope,
duplicates, and overlapping entries are invalid. Every evidence location must
resolve inside exactly one writable scope. A modifying step on a repository
whose `commit_policy` is `required` must authorize `commit`, but that semantic
authorization never grants the model raw Git commands: the dispatcher stages
the exact inspected path set and creates the commit itself.

## Graph and cross-project validation

Normalization rejects duplicate IDs, noncontiguous ordinals, self or unknown
dependencies, dependencies ordered after their dependents, duplicate output
artifacts, unordered competing write locks, invalid review counts, and invalid
retry escalation references.

The YAML sidecar loader rejects duplicate mapping keys before normalization.
`validate_plan_for_config()` additionally rejects unknown repository IDs,
reviewer keys that are not configured reviewers, authorization actions that
exceed the selected repository permission policy, and concurrency/retry
contracts that cannot be satisfied by configured roles. When a step has
`cluster_operation`, this public library validation admits only its static
reference: target/preflight binding, allowed repository, normalized path,
operation-manifest root, declared action tuple, and automatic rollback intent. It
deliberately does not require the manifest or files that the executor will create
and dispatcher structured Git will commit. The separate post-commit
`validate_cluster_operations_for_plan()` API must validate the complete manifest,
declared files, symlink containment, exact action order, and automatic rollback
against the exact committed revision before any future cluster snapshot, approval,
or mutation; there is no fallback that skips it.

## Import adapters

`load_normalized_plan()` imports an explicit YAML sidecar. It verifies all
listed source hashes and does not infer missing fields from Markdown.

`import_tier2_markdown()` is a reference adapter for the Tier 2 step table. It
compares table step IDs, titles, repositories, and exact Markdown hash against
the same complete YAML sidecar. Tier 2-specific parsing is isolated in
`src/dispatcher/importers.py`; it is not part of the dispatcher core.

Historical work is intentionally not treated as accepted execution baseline
state without a baseline inspection and explicit operator approval for the
exact plan digest. Private-reference migration remains separately authorized
Phase 8 work and is not performed by the public example.
