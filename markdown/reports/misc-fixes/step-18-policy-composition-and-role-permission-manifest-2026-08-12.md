# Step 18: Policy Composition And Role Permission Manifest

**Date:** 2026-08-12  
**Status:** Implemented and verified; all changes remain uncommitted

## Outcome

Step 18 removes silent role-class permission inheritance and replaces the
single executor-only real-operation digest with an exact role-keyed permission
manifest.

The launch gate now binds and re-verifies:

- the configured supervisor;
- every configured executor eligible for the first executable step; and
- every reviewer in the compiled review obligation for that step.

No compatibility path for the old singular digest was retained because no
real operation has shipped.

## Canonical Permission Actions

`src/dispatcher/config.py:48` defines the ordered canonical vocabulary:

```text
inspect, modify, verify, commit, push, force_push, create_branch
```

`PermissionPolicy.actions` accepts only those keys. The OpenCode mapping in
`src/dispatcher/permissions.py` is checked against the same ordered tuple at
import time, so schema and compilation cannot silently drift.

`ProjectConfigModel.validate_references` at
`src/dispatcher/config.py:400-411` requires every policy referenced by
`role_class_policies` to declare all seven decisions. Missing actions fail
configuration loading with the role class, policy ID, and missing action.
Global, project, repository, and concrete-role policies may remain sparse
ordered overrides.

The public example and shared test fixtures now explicitly define all seven
role-class decisions. Reviewer `verify` is denied consistently with Step 17's
inspect-only role ceiling.

## Corrected Composition Semantics

`compile_effective_policy` now documents the actual order:

1. global;
2. project;
3. repository;
4. role class;
5. concrete role;
6. dispatch authorization narrowing; and
7. immutable reviewer/supervisor ceilings.

Configured layers are ordered overrides, not universally monotonic tightening.
Dispatch authorization and the hard role ceilings are the final narrowing
boundaries.

## Role Permission Manifest

`src/dispatcher/operation.py:55-70` defines:

- `RolePermissionEntry` — role kind, role-scoped authorized actions, digest;
- `RolePermissionManifest` — version, repository, step, exact role map.

`compile_role_permission_manifest` at `operation.py:128-177`:

1. resolves the first pending/ready step;
2. compiles the current durable review obligation;
3. includes the one supervisor, every configured executor, and required
   reviewer roles;
4. scopes actions through the same production role helper used at dispatch;
5. generates the exact OpenCode permission object per role; and
6. hashes canonical JSON with SHA-256.

Role ordering is deterministic by role kind and role key. Duplicate role keys
fail closed.

No-review manifests omit reviewers but retain the supervisor and all eligible
executors.

## Approval And Execute Binding

`RealOperationApproval` now requires `permission_manifest`.

`approve_real_operation` at `operation.py:217` requires an operator-supplied
role/digest map. It exact-matches both the role set and each digest before
writing approval.

`validate_real_operation_prerequisites` at `operation.py:250` independently:

- recompiles the current manifest;
- exact-matches it against the approval record;
- exact-matches the repeated execute CLI digest arguments; and
- rejects missing, extra, duplicate, malformed, or mismatched roles before
  process launch.

Reviewer digest mismatch and tampered reviewer manifest each have direct unit
tests.

## CLI Workflow

The new local-only producer is:

```sh
dispatcher permission-manifest --config <config> --run-id <run> \
  --plan <plan> --repo-id <repo> --output <manifest.json>
```

It writes an owner-only manifest through `atomic_write_private_text` and does
not invoke OpenCode or a network service.

Approval and execute now accept repeated strict values:

```text
--permission-digest ROLE=SHA256
```

`parse_permission_digest_args` at `operation.py:180-196` rejects malformed
role keys, malformed hashes, and duplicate roles. Approval/execute then reject
missing and extra roles against the compiled manifest.

The standalone producer, approval consumer, and execute gate use the same
manifest compiler and digest parser.

## Schemas And Documentation

Updated:

- `schemas/project-config-v1.json` with the canonical action-key enum;
- `config/projects/example.yaml` with complete role-class policies;
- `README.md` command examples;
- `docs/config-schema.md` composition/completeness rules;
- `docs/operations.md` manifest/approval/execute workflow; and
- `docs/protocol.md` role-set binding semantics.

Generated schema equality remains contract-tested.

## Tests

New or strengthened tests include:

- `test_role_class_permission_policy_requires_every_canonical_action`
- `test_permission_actions_use_one_complete_canonical_vocabulary`
- `test_unknown_permission_action_is_rejected_during_config_loading`
- `test_reviewer_class_silence_on_commit_is_a_config_error`
- `test_permission_rule_mapping_uses_the_canonical_action_order`
- `test_permission_digest_arguments_reject_malformed_and_duplicate_roles`
- `test_permission_manifest_covers_supervisor_all_executors_and_required_reviewers`
- `test_permission_manifest_includes_every_configured_executor`
- `test_permission_manifest_omits_reviewers_when_review_is_not_required`
- `test_approval_rejects_mismatched_reviewer_permission_digest`
- `test_approval_rejects_missing_or_extra_permission_roles`
- `test_execute_gate_rejects_supplied_reviewer_permission_digest_mismatch`
- `test_execute_gate_rejects_tampered_reviewer_permission_manifest`
- the CLI producer/approval round-trip in
  `test_approve_real_operation_command_writes_exact_bound_record`; and
- the full disposable `dispatcher execute` integration using repeated role
  digests.

## Verification

Focused config, permission, operation, CLI, schema, and execute suite:

```text
84 passed in 8.33s
```

Full non-live suite:

```text
433 passed, 10 deselected in 58.30s
```

Live collection:

```text
31 tests collected in 0.17s
```

Post-Step-18 live reviewer security/rework proof with Luna:

```text
test_real_reviewer_mutation_attempts_are_denied_before_execution PASSED
test_real_review_rework_resume_cycle_accepts_after_remediation PASSED
2 passed in 207.42s (0:03:27)
```

Package checks:

```text
pip check: No broken requirements found.
wheel: dispatcher-0.1.0-py3-none-any.whl built successfully
```

Static checks:

```text
ruff: passed
git diff --check: passed
```

`config/projects/example.yaml` loads successfully after the schema migration.
`git status --porcelain -- config/projects/local config/state state` produced
no output.

## Safety And Remaining Work

- No private T2 state, credential, or auth file was inspected or modified.
- No commit, push, amend, or branch was created.
- The only live calls were the two explicitly required disposable reviewer
  scenarios using Luna.
- Step 17 reviewer/supervisor ceilings remain effective after complete
  role-class policy migration.
- Step 19 dispatcher-owned verification and OS/network isolation remain
  required before claiming technical no-network/no-external-service
  enforcement.
