# Step 18: Policy Composition and Role Permission Manifest

**Date:** 2026-08-12  
**Status:** Implemented and verified; see Step 18 evidence report  
**Source:**
`markdown/reports/misc-fixes/step-16-permission-boundary-security-analysis-2026-08-12.md`

## Goal

Eliminate silent permission inheritance caused by incomplete role-class
policies and bind real-operation approval to the exact compiled permissions
of every role that may participate in the first executable step.

## Design Decisions

### Canonical semantic actions

Define one canonical ordered action vocabulary shared by config validation and
permission compilation:

- `inspect`
- `modify`
- `verify`
- `commit`
- `push`
- `force_push`
- `create_branch`

Place it where `config.py` and `permissions.py` can import it without a
circular dependency. `_ACTION_RULES` remains the tool-pattern mapping but may
not be the only source known to schema validation.

### Complete role-class policies

Every policy referenced by `role_class_policies` must explicitly declare a
decision for every canonical semantic action. Omission is a configuration
error, not inheritance.

Other layer types may remain sparse because they express ordered overrides.
Documentation must accurately state that layer composition is ordered
override, followed by dispatch authorization narrowing and non-overridable
role ceilings. Remove the inaccurate blanket claim that all layering can only
tighten.

### Role permission manifest

Replace the singular executor-only real-operation permission digest with an
exact role-keyed manifest.

The manifest must cover:

- the configured supervisor role;
- every configured executor role that the supervisor could target for the
  first executable step;
- every reviewer role in the compiled durable review obligation for that
  step.

Each entry contains role key, role kind, scoped authorized actions, and digest
of the exact generated OpenCode permission JSON.

Ordering is deterministic by role kind and role key. Duplicate or missing
roles are invalid.

### Approval binding

`RealOperationApproval` must embed the full role permission digest map. The
approval producer accepts an operator-supplied role-keyed digest set, verifies
exact equality against freshly compiled policies, then writes the bound
approval record.

`dispatcher execute` recomputes the manifest and requires exact role-set and
digest equality with the approval record. No role may be silently added,
removed, or changed after approval.

Because no real operation has shipped, remove the singular
`--permission-digest` interface rather than adding compatibility fallbacks.
Use a repeated, duplicate-rejecting form such as:

```text
--permission-digest supervisor=<sha256>
--permission-digest terra=<sha256>
--permission-digest reviewer=<sha256>
```

The approval command and execute command must share one strict parser/helper.

## Scope

Likely files:

- a new small canonical action module, or an existing dependency-neutral
  contract module
- `src/dispatcher/config.py`
- `src/dispatcher/permissions.py`
- `src/dispatcher/operation.py`
- `src/dispatcher/cli.py`
- generated config/operation schemas where applicable
- config, permission, operation, CLI, and documentation tests
- `docs/config-schema.md`
- `docs/operations.md`
- `docs/protocol.md`

## Required Tests

- Role-class policy missing any canonical action fails config loading with a
  precise path/action error.
- Repository commit allow plus reviewer-class omission reproducer now fails
  config validation.
- Complete executor/reviewer/supervisor class policies load successfully.
- Hard Step 17 role ceilings remain effective after complete policy migration.
- Manifest includes exact supervisor, all eligible executors, and compiled
  reviewer obligation roles.
- No-review step omits reviewer entries but still includes supervisor and all
  eligible executors.
- Repeated CLI digest parsing rejects duplicates, malformed SHA values,
  unknown roles, missing roles, and extra roles.
- Approval record binds the exact manifest.
- Changed policy/config/role set invalidates approval and execute.
- Reviewer digest mismatch fails before process launch.
- Existing disposable execute end-to-end test uses the full manifest.
- Full non-live suite remains green with a higher count than Step 17.

## Live Proof

No new live scenario is required. After implementation, rerun the Step 17
adversarial reviewer scenario and the complete intended-matrix disposable
suite to ensure the compiled policies used live match the approved manifest.

## Evidence

Write:

`markdown/reports/misc-fixes/step-18-policy-composition-and-role-permission-manifest-2026-08-12.md`

Use GPT-5.6 Sol in a fresh session. Leave all changes uncommitted.
