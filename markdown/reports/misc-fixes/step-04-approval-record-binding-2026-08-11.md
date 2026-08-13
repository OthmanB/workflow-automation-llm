# Step 4 - Real-Operation Approval Record Binding

**Date:** 2026-08-11  
**Status:** Implemented, verified, uncommitted, for human review.

## Purpose

`dispatcher execute` previously treated a non-empty `--approval-ref` string as
operator approval. That label was not bound to the configuration, plan, durable
run, repository, or executable step. This step replaces that presence-only
input with a private JSON approval record whose fields are exact-matched before
the real-operation coordinator can launch a process.

## Implementation Evidence

- `src/dispatcher/operation.py:48-58` defines the immutable
  `RealOperationApproval` contract. It carries `approval_ref`, `project_id`,
  `config_digest`, `plan_digest`, `run_id`, `repo_id`, `step_id`, and
  `decided_at`.
- `src/dispatcher/operation.py:79-85` adds
  `load_real_operation_approval(path)`. It follows
  `load_live_smoke_proof` exactly: resolve the path, validate JSON with the
  contract model, and wrap `OSError` or `ValueError` in `RealOperationError`.
- `src/dispatcher/operation.py:88-100` factors
  `first_pending_executable_step(plan, record)`, retaining the prior ordered
  pending-or-ready lookup. Both the record producer and execute gate use it.
- `src/dispatcher/operation.py:103-124` adds
  `approve_real_operation(...)`, which rejects a non-first repository and
  records the current config and durable run values with `datetime.now(UTC)`.
- `src/dispatcher/cli.py:90-110` registers `approve-real-operation` and
  changes execute to require `--approval-record`.
- `src/dispatcher/cli.py:324-352` implements the local-only producer handler:
  it loads config, state-backed run, and normalized plan; calls the shared
  constructor; and persists the result with `atomic_write_private_text`.
  `src/dispatcher/cli.py:769-776` dispatches the new subcommand.
- `src/dispatcher/operation.py:167-182` resolves and validates the first
  pending step, then immediately loads and exact-matches the approval record.
  This is after first-step resolution by design, so `step_id` and `repo_id`
  have their current authoritative values; it is before live-smoke,
  permission-policy, and stall-policy checks. The existing fail-fast
  `--confirm-real-operation` check remains at `src/dispatcher/operation.py:143-146`.
- `src/dispatcher/cli.py:294-304` derives the audit event identifier from the
  validated record and writes the complete validated approval object in the
  audit payload. It includes the approval label, project, config digest, plan
  digest, run, repository, step, and decision time rather than a bare CLI
  string.
- `docs/operations.md:31-77` documents the smoke-proof producer, the new
  approval producer, and the exact-matching execute gate. `README.md:50-58`
  updates its command synopsis so it does not advertise the removed execute
  argument.

## CLI Contract

The new producer usage is:

```text
dispatcher approve-real-operation --config <private-v2.yaml> --run-id <run-id> \
  --plan <plan.yaml> --repo-id <repo-id> --approval-ref <decision-ref> \
  --output <approval.json>
```

Its complete argument list is `--config`, `--run-id`, `--plan`, `--repo-id`,
`--approval-ref`, and `--output`; all are required. It performs only local
configuration, normalized-plan, and SQLite-state reads plus the owner-only
artifact write. It does not call OpenCode.

The changed execute usage is:

```text
dispatcher execute --config <private-v2.yaml> --run-id <run-id> \
  --plan <plan.yaml> --repo-id <repo-id> \
  --smoke-proof <proof.json> --smoke-model <provider/model> \
  --permission-digest <sha256> --stall-policy-digest <sha256> \
  --expected-revision <commit-sha> \
  --approval-record <approval.json> --confirm-real-operation
```

Its required arguments are `--config`, `--run-id`, `--plan`, `--repo-id`,
`--smoke-proof`, `--smoke-model`, `--permission-digest`,
`--stall-policy-digest`, `--expected-revision`, and `--approval-record`, plus
the required acknowledgement flag `--confirm-real-operation`. Optional
arguments remain `--max-turns <int>` (default `20`) and `--log-level`
(`DEBUG`, `INFO`, `WARNING`, or `ERROR`).

The documented smoke-proof producer is:

```text
dispatcher smoke-proof --config <private-v2.yaml> --model <provider/model> \
  --output <proof.json>
```

It requires `DISPATCHER_LIVE_OPENCODE=1` and is the only command that produces
a valid live-smoke-proof artifact for the execute gate.

## Public Signatures

```python
class RealOperationApproval(ContractModel):
    approval_ref: Identifier
    project_id: Identifier
    config_digest: Sha256
    plan_digest: Sha256
    run_id: Identifier
    repo_id: Identifier
    step_id: Identifier
    decided_at: datetime

def load_real_operation_approval(path: str | Path) -> RealOperationApproval: ...

def first_pending_executable_step(
    plan: NormalizedPlan,
    record: RunRecord,
) -> PlanStep | None: ...

def approve_real_operation(
    *,
    config: Config,
    record: RunRecord,
    plan: NormalizedPlan,
    repo_id: str,
    approval_ref: str,
) -> RealOperationApproval: ...

def validate_real_operation_prerequisites(
    *,
    config: Config,
    store: StateStore,
    record: RunRecord,
    plan_path: str | Path,
    repo_id: str,
    smoke_proof_path: str | Path,
    smoke_model: str,
    permission_digest: str,
    stall_policy_digest: str,
    expected_revision: str,
    approval_record_path: str | Path,
    confirm: bool,
) -> dict[str, Any]: ...
```

The CLI handler is `def _cmd_approve_real_operation(args: argparse.Namespace) -> int`
at `src/dispatcher/cli.py:324`.

## Verification Evidence

New tests:

- `tests/unit/test_cli.py:161` -
  `test_approve_real_operation_command_writes_exact_bound_record` starts a
  durable run, invokes the new CLI command, and asserts every stored binding
  field and a timezone-aware decision timestamp.
- `tests/unit/test_operation.py:265` -
  `test_real_operation_accepts_matching_approval_record` demonstrates a valid
  approval passes this specific gate: validation reaches the later intentional
  permission-digest failure.
- `tests/unit/test_operation.py:275-301` -
  `test_real_operation_rejects_mismatched_approval_record` parametrically
  rejects independently mismatched project, config digest, plan digest, run,
  repository, and step values.

Updated tests using real-operation prerequisite calls:

- `test_real_operation_rejects_public_mock_mode_before_other_checks`
- `test_real_operation_requires_explicit_confirmation_and_schema_v2`
- `test_real_operation_gates_on_the_expected_revision`
- `test_real_operation_rejects_stale_live_smoke_proof`
- `test_real_operation_accepts_recent_live_smoke_proof_for_freshness_gate`
- `test_real_operation_rejects_naive_live_smoke_proof_timestamp`

Each now supplies an actual written `RealOperationApproval` through
`approval_record_path`; no test bypasses the new API with a bare approval
string. Existing assertions were not weakened.

Commands run:

```text
.venv/bin/python -m pytest tests/unit/test_operation.py tests/unit/test_cli.py -q -m "not live_opencode"
23 passed in 2.18s

.venv/bin/python -m pytest tests/contract/test_documentation.py -q -m "not live_opencode"
4 passed in 0.07s

.venv/bin/ruff check src/dispatcher/operation.py src/dispatcher/cli.py tests/unit/test_operation.py tests/unit/test_cli.py
All checks passed!

.venv/bin/python -m pytest tests -q -m "not live_opencode"
256 passed, 5 deselected in 35.26s
```

The final suite has 256 passing tests, strictly more than the prior step's
248 passing tests.

## Scope And Judgment Calls

No commit, branch creation, or push was made. All changes, including this
report, remain uncommitted. `config/projects/local/`, `config/state/`, and
`state/` were neither read nor modified. No live OpenCode invocation, network
or HTTP request, credential access, or external service call was made.

`RealOperationApproval` uses `Identifier` for the decision label and identity
fields, and `Sha256` for both digests. This deliberately mirrors
`PlanApproval.operator_decision_ref` and the established digest conventions,
rather than accepting arbitrary non-empty strings. `decided_at` remains a
timezone-aware UTC timestamp generated by the producer; no freshness rule was
added because this change binds identity and must not weaken or duplicate the
separate live-smoke recency rule.

The approval comparison is immediately after pending-step resolution because
the approval must bind to that exact step. It remains after existing
configuration, plan, durable-run, recovery, baseline, repository, and expected
revision checks, preserving their current safety order, and before every
subsequent prerequisite. The only scope expansion was updating the README
command synopsis to remove the obsolete execute `--approval-ref` spelling; it
is directly related documentation needed to avoid publishing an invalid CLI
invocation. No other deviations were required.

## Supervisor spot-check (independently verified, not just self-reported)

- `git log --oneline -3` → still at `72a4dea`. No commit created. Confirmed.
- `git status --porcelain -- config/ state/` → empty. Confirmed untouched.
- `git diff --stat HEAD` → 36 files, 1218 insertions/119 deletions total
  across the whole (multi-step) working tree; the file set matches this
  step's described scope plus prior steps' already-verified changes.
- Read `src/dispatcher/operation.py:48-58,79-124,150-182` directly in full
  (not just the reported line ranges) and confirmed, precisely as designed:
  - `RealOperationApproval` carries exactly the six binding fields plus
    `approval_ref`/`decided_at`, typed `Identifier`/`Sha256` mirroring
    `PlanApproval`.
  - `first_pending_executable_step` is a single shared helper, called from
    both `approve_real_operation` (line ~168) and
    `validate_real_operation_prerequisites` (line ~166) — no duplicated
    lookup logic, exactly as required.
  - The six-field exact-match block
    (`project_id`/`config_digest`/`plan_digest`/`run_id`/`repo_id`/
    `step_id`) sits immediately after the `pending_step` resolution check
    and strictly before the smoke-proof block — confirmed by direct
    reading, not just the report's claim.
  - `approve_real_operation` itself also re-validates
    `pending_step.repo_id != repo_id` before minting a record — a sensible
    extra defense not explicitly requested but consistent with the design
    intent.
- Confirmed via grep that `cli.py` registers `approve-real-operation`
  (parser + dispatch + handler `_cmd_approve_real_operation`) and that
  `execute_parser` now requires `--approval-record` (the old
  `--approval-ref` execute argument is gone).
- Collected `tests/unit/test_operation.py` directly: confirmed
  `test_real_operation_rejects_mismatched_approval_record` is parametrized
  over exactly the six bound fields
  (`project_id`/`config_digest`/`plan_digest`/`run_id`/`repo_id`/`step_id`),
  each as an independent case — genuinely exhaustive per-field coverage, not
  a single representative case.
- Re-ran the full non-live suite independently: **256 passed, 5 deselected**
  — matches the model's reported count exactly (248 → 256, +8).

No corrections needed to the model's self-report for this step. This closes
the design gap identified as Blocker B4 in the original review: `execute`'s
approval gate is now a genuine content-addressed binding, not a decorative
string. Deeper review of whether a plain JSON file (vs. e.g. a durable
state-store-backed record) is a sufficiently tamper-resistant storage choice
for this artifact class is deferred to the final Sonnet 5 review, per
standing policy.
