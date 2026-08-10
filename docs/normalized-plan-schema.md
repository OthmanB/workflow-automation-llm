# Normalized Plan Schema V1

The dispatcher executes an immutable `NormalizedPlan`, not a Markdown table.
The machine-readable schema is `schemas/normalized-plan-v1.json`; the runtime
model is `src/dispatcher/plan.py:NormalizedPlan`.

## Required top-level fields

```yaml
schema_version: 1
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

A `PlanApproval` is required before a schema-v1 run record can be created. It
contains the plan digest, source digest, an operator decision reference, and an
approval timestamp. Any semantic plan or source identity/hash change invalidates
that approval and requires a new operator decision.

## Required step fields

Every step explicitly declares:

- `ordinal`, `step_id`, `title`, and `repo_id`;
- `depends_on` and `required_inputs`;
- `produced_outputs` and `resource_locks`;
- `risk_tags`;
- `authorization` with nonempty `authorized_actions` and operator-gate status;
- nonempty `acceptance_criteria`;
- nonempty `evidence_requirements`;
- a typed `review` obligation; and
- an explicit `retry` policy.

Empty lists are valid only when a field is structurally optional for the step,
such as `depends_on` on an initial step. Authorization, acceptance, and evidence
cannot be omitted or inferred from prose.

## Graph and cross-project validation

Normalization rejects duplicate IDs, noncontiguous ordinals, self or unknown
dependencies, dependencies ordered after their dependents, duplicate output
artifacts, unordered competing write locks, invalid review counts, and invalid
retry escalation references.

`validate_plan_for_config()` additionally rejects unknown repository IDs,
reviewer keys that are not configured reviewers, authorization actions that
exceed the selected repository permission policy, and concurrency/retry
contracts that cannot be satisfied by configured roles.

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
