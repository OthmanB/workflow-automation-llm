# Dispatcher T2.2a Real-Operation Readiness Review

**Review date:** 2026-08-11
**Scope:** Worker result contract enforcement, cancellation/stall safety and
process lifecycle, the `dispatcher execute` real-operation command and its
pre-launch gates, input integrity (duplicate-key handling), worktree/baseline/
cleanup and crash recovery, and current test/release evidence.
**Reviewed at:** commit `72a4dea` (branch `main`, 7 commits ahead of
`origin/main`) plus the current uncommitted worktree (untracked
`src/dispatcher/yaml_io.py`, untracked
`tests/fixtures/opencode/1.18.11/run-duplicate-key.jsonl`, and modifications to
`config.py`, `importers.py`, `plan.py`, `preflight.py`, `sessions.py`, five
`docs/*.md` files, and related tests).
**Review method:** Independent read-only review. No files modified, no commits,
no branches, no `dispatcher execute`, no OpenCode invocation, no credentials,
no config/projects/local or private T2 state inspected. Findings are backed by
direct file:line citations verified against source, and by running the
non-live automated test suite (`.venv/bin/python -m pytest tests -q -m "not
live_opencode"` → **237 passed, 0 failed, 5 deselected**). Reports and
checklists in `markdown/reports/` and `markdown/plans/` were treated as
narrative claims only and cross-checked against code and tests, not trusted as
proof.

**Question under review:** Is it safe to proceed to the first real T2.2a
operation — one fresh baseline-backed T2 run, T2.2a only, no T2.2b, fixture
mode only, no live HTTP/cluster/deployment/Docker/push/PR/external service use?

---

## Verdict

**NO-GO**

The underlying execution engine (worker-result contract enforcement, YAML/JSON
input-integrity, workspace barriers, baseline drift detection) is well-built
and well-tested. But the actual control surface for the operation in
question — `dispatcher execute`, the guarded real-operation command — has
**never been exercised end-to-end**, has **at least three gates that are
non-functional or cosmetic**, and the project's own evidentiary record for
"real model compliance" is **stale relative to the current strict code**.
Additionally, the explicit, plan-acknowledged risk of PID reuse during
cancellation is **unmitigated in code**, not just untested.

---

## Mandatory Blockers

### B1 — Cancellation does not verify process identity beyond PID+hostname; recorded start time is never checked (PID-reuse exposure)

- **Severity:** Blocker
- **File/line:** `src/dispatcher/sessions.py:794-828` (`cancel_process_group`);
  start time captured but discarded at `src/dispatcher/sequential.py:750`,
  `src/dispatcher/state_store.py:336,376` (`request_dispatch_cancellation`
  return tuple omits `process_started_at`)
- **Reason:** `cancel_process_group(process_id, expected_host,
  grace_seconds)` has no start-time parameter. It only checks `expected_host
  == socket.gethostname()` (line 796) and `os.kill(pid, 0)` liveness (lines
  798/811/821). No `psutil`, `create_time`, or `/proc` check exists anywhere
  in the repository (confirmed via full grep across `src/`). If the recorded
  PID is recycled by the OS between dispatch start and a later `dispatcher
  cancel`, the wrong process is signalled and `True` (success) is reported.
  The project's own plan explicitly names this exact mitigation ("Store
  host/start identity and signal only a verified managed process group",
  `markdown/plans/dispatcher-real-operation-readiness-plan-2026-08-11.md`)
  but it was never implemented. Zero test coverage exists for PID-reuse
  anywhere in `tests/`.
- **Smallest safe remediation:** capture OS process creation time at spawn
  (e.g. via `/proc/<pid>/stat` on Linux or a portable equivalent) and thread
  it through `request_dispatch_cancellation` → `cancel_process_group`; before
  sending any signal, re-read the current process's creation time for that
  PID and abort with a fail-closed error if it does not match the recorded
  value.
- **Exact proof required after remediation:** a unit test that mocks
  `os.kill(pid,0)` to succeed against an "impostor" process (same PID,
  different/no matching start time) and asserts `cancel_process_group`
  raises/refuses rather than signalling; plus the existing fault-injection
  cancellation test extended to assert the start-time comparison actually
  occurred.

### B2 — `dispatcher execute` has never been exercised end-to-end, positively or negatively

- **Severity:** Blocker
- **File/line:** `src/dispatcher/cli.py:224-282` (`_cmd_execute`),
  `src/dispatcher/operation.py:56-133`
  (`validate_real_operation_prerequisites`); test coverage only
  `tests/unit/test_operation.py` (77 lines, 2 tests, both call
  `validate_real_operation_prerequisites` directly, bypassing the CLI)
- **Reason:** Grep across `tests/` for `_cmd_execute` or an `execute`
  subcommand invocation returns **zero matches**. Only 2 of ~14 pre-launch
  gates have any test at all (schema/mode, confirm-flag) — baseline,
  recovery, repo clean/branch, smoke-proof, permission digest, stall digest,
  and approval-ref binding are completely untested. The one real-process
  test suite (`tests/live/test_real_operation_disposable.py`) constructs
  `SequentialWorkflow`/`SequentialExecutionCoordinator` directly and never
  imports `cli.py` or `operation.py` — it proves the engine works, not that
  the guarded command's gate-wiring, ordering, audit-append, or failure
  handling work. The project's own docs/reports admit this plainly
  (`docs/operations.md:48`: "This command has not been run by this project
  yet"; `markdown/reports/dispatcher-real-operation-mode-report-2026-08-11.md:5,25`).
- **Smallest safe remediation:** add a disposable (non-T2, fixture-repo) test
  that invokes `main(["execute", ...])` (or `_cmd_execute` with a real
  `argparse.Namespace`) through the actual CLI entry point, first proving
  each gate rejects independently (14 negative cases), then proving one
  fully-valid invocation launches and completes.
- **Exact proof required after remediation:** a passing test log showing
  `dispatcher execute` invoked via its real CLI path, all 14 gates
  independently proven to reject when violated, and one successful disposable
  run completing through the CLI (not the bare coordinator).

### B3 — No producer exists for the required `LiveSmokeProof` artifact, and its timestamp is never checked for recency

- **Severity:** Blocker
- **File/line:** `src/dispatcher/operation.py:26-38` (`LiveSmokeProof`
  model), `:47-53` (`load_live_smoke_proof`), `:104-110` (consumption in the
  gate)
- **Reason:** `proof_version`, `session_id_present`, `workdir_clean` etc.
  occur *only* in `operation.py` in the entire repository (confirmed by grep
  across src/tests/docs/markdown) — nothing in the codebase serializes this
  file. `tests/live/test_opencode_live.py` runs the real smoke prompt but
  never writes a `LiveSmokeProof` JSON. An operator must therefore
  hand-author the exact safety attestation (`passed`, `workdir_clean`,
  `evidence_written: []`, etc.) that this gate exists to independently
  verify — this defeats the purpose of the control. Separately,
  `completed_at: datetime` exists on the model but is never compared against
  any max-age window, so a stale (e.g. day/week-old) proof passes as long as
  digests match, contradicting the plan's own requirement that smoke "passed
  recently."
- **Smallest safe remediation:** add a `dispatcher preflight
  --emit-smoke-proof <path>` mode that runs the real smoke call and writes
  the sanitized proof itself (no hand-authoring possible); add a recency
  check (e.g. reject `completed_at` older than N minutes) in
  `validate_real_operation_prerequisites`.
- **Exact proof required after remediation:** a test showing the proof file
  is machine-generated from an actual smoke run (not hand-constructed in a
  test fixture) and that an artificially aged `completed_at` is rejected.

### B4 — `--approval-ref` gate is presence-only; not bound to project/config/plan/run/step as required

- **Severity:** Blocker
- **File/line:** `src/dispatcher/operation.py:75-76`
- **Reason:** `if not approval_ref: raise RealOperationError(...)` — any
  non-empty string satisfies this. It is never cross-checked against
  `record.plan_approval.operator_decision_ref`, the baseline's approval, or
  any dedicated real-operation-approval record (no such record type exists in
  the codebase). The plan's own required test criteria state approval must
  be "bound to the exact project, config, plan, run, and first step" — this
  is currently a cosmetic string field, not a safety control.
- **Smallest safe remediation:** require `approval_ref` to resolve to a
  stored, content-addressed approval record whose digest covers project id,
  config digest, plan digest, run id, and the target step id; reject any
  reference that does not match exactly.
- **Exact proof required after remediation:** a test proving an approval_ref
  that doesn't match the current plan/config/run/step digests is rejected,
  and one that does match is accepted.

### B5 — No "expected/pinned revision" gate, despite the plan's own required criterion

- **Severity:** Blocker
- **File/line:** `src/dispatcher/operation.py:94`
  (`inspect_repository(..., require_clean=True)`),
  `src/dispatcher/repository.py:148-168` (branch + clean checks only)
- **Reason:** The plan states execution requires every repository to be
  "clean, on its expected branch, **and at the expected revision**"
  (`markdown/plans/dispatcher-real-operation-readiness-plan-2026-08-11.md`).
  Grep for `expected_revision|pinned_revision|expected_commit` across
  `src/dispatcher/*.py` returns nothing — only branch and dirty-state are
  enforced. Revision is only *recorded* into the audit dict
  (`operation.py:128`), never compared to an expected value.
- **Smallest safe remediation:** add a config or run-record field for the
  expected/pinned revision per repository and an exact-match check in
  `validate_real_operation_prerequisites`, alongside the existing
  branch/clean checks.
- **Exact proof required after remediation:** a test proving execution is
  rejected when HEAD has moved to an unexpected (even if clean,
  correctly-branched) commit.

### B6 — Disposable-test "4 passed" evidence predates removal of a response-reshaping harness; no live re-run exists under the current strict, non-repairing contract

- **Severity:** Blocker
- **File/line:** `tests/live/test_real_operation_disposable.py` (current,
  strict); commit history `8dbbb7e` ("test: prove disposable real
  operation") vs `72a4dea` ("feat: enforce exact worker response contracts")
- **Reason:** Until commit `72a4dea` (same day, later), the disposable test
  harness contained a `_live_worker_session_runner` that discarded the real
  model's response and fabricated a compliant JSON payload whenever the
  model's actual output wasn't `completed`/`accepted` — i.e. exactly the
  "reshape and repair" behavior this review was asked to check for. The
  prior committed report stated in plain text that the real model's "final
  chat response was a short natural-language success response rather than
  the required schema-v1 result object" and the harness "shaped a typed
  result." Commit `72a4dea` deleted this harness and added the exact
  `Literal["dispatcher.executor_result.v1"/"dispatcher.reviewer_result.v1"]`
  fields, but the report's "4 passed" verification section was **not**
  regenerated — it still reflects the pre-fix (repairing) run. No committed
  evidence shows these four scenarios have been rerun against a real model
  since the strict contract was enforced.
- **Smallest safe remediation:** rerun all four
  `tests/live/test_real_operation_disposable.py` scenarios against the
  target real model with `DISPATCHER_REAL_DISPOSABLE=1`, under the current
  strict contract, and publish a freshly dated verification artifact.
- **Exact proof required after remediation:** a dated, current test run log
  (not a narrative report) showing all four disposable scenarios pass with
  the model returning `response_contract` directly, no reshaping code
  present in the harness (verifiable by diffing the harness against
  `72a4dea`'s state).

---

## Non-Blocking Findings

### N1 — `cancel_process_group` returns `True` unconditionally after final SIGKILL without re-verifying death

`src/dispatcher/sessions.py:824-828`. Does not block a single,
operator-observed T2.2a step (a human is watching; false-positive "stopped"
status is a monitoring/accuracy issue, not a corruption risk for one bounded
step) but should be fixed before broader/unattended real operation. Recommend
adding a post-kill `os.kill(pid,0)` check and returning an explicit
indeterminate status if the process survives.

### N2 — Asymmetric grace-period escalation (SIGINT→SIGTERM waits full `grace_seconds`; SIGTERM→SIGKILL capped at ≤0.1s)

`src/dispatcher/sessions.py:808-819`. Cosmetic timing inconsistency versus
`_terminate_process_group`'s symmetric ladder (lines 766-791); doesn't
threaten a single supervised step since a real OpenCode process responds to
SIGINT promptly per the live cancellation test.

### N3 — `--decisions` JSON file (baseline approve) uses unprotected `json.loads`, unlike every sibling external input

`src/dispatcher/cli.py:574`. Governs review-waiver integrity and is
asymmetrically unprotected relative to config/plan/ownership-map YAML (all
hardened in this same change set). Does not block T2.2a directly because
baseline decisions for T2.1a–T2.1f are described as already approved prior to
this step in the plan's "Fresh run required" section, and
`validate_approved_baseline`'s content-hash check (`baseline.py:219-228`)
still catches post-approval tampering of evidence. Should be fixed before any
further baseline (re-)approvals.

### N4 — `sessions.py:485` (`list_sessions`) JSON parsing has no duplicate-key protection

Read-only, sourced from the pinned trusted OpenCode binary's own stdout (not
adversarial free text); low risk, inconsistent-for-consistency's-sake only.

### N5 — Tier-2 Markdown table parser (`_TIER2_STEP_ROW`, `importers.py:16,95,118-124`) uses heuristic backtick/title extraction

Bounded by a mandatory, hash-verified cross-check against the authoritative
YAML sidecar (`importers.py:67-91`) — a mis-parsed or ambiguous row cannot
silently take effect; it either matches the sidecar or fails closed with
`PlanError`. Not a blocker.

### N6 — No CLI command performs physical crash-recovery reconciliation; `dispatcher recover` only classifies/reports

`src/dispatcher/cli.py:371-397`. For a single scoped T2.2a run with no prior
crash, this gate correctly returns "no unresolved recovery work" and never
needs to be exercised. Relevant only if a prior run left dangling state —
should be closed before broader/unattended operation.

### N7 — Working tree has uncommitted changes (input-integrity hardening: `yaml_io.py` + 5 modified modules + doc/test updates)

Not a functional defect — verified these changes are exactly the duplicate-key
hardening reviewed above, all non-live tests pass (237/237) including the new
tests. But it means the version to be used for T2.2a is not yet a single
committed, tagged, auditable baseline. Should be committed before use.

### N8 — Minor test-coverage gaps (not code gaps)

No property/fuzz test on `parse_executor_result`/`parse_reviewer_result`; no
dedicated test for `execution.py`'s `_strict_json_object`/duplicate-key path
by name (mechanism identical to the tested `protocol.py`/`sessions.py`
copies, verified empirically); no test for
`_validate_evidence`/`_validate_executor_evidence` wrong-hash/wrong-size
rejection specifically (code is correct by inspection, cross-checked directly
at `repository.py:311-334`).

---

## Verified Strengths

- **Worker result contract is exact, not fuzzy**: `response_contract` is a
  Pydantic `Literal["dispatcher.executor_result.v1"]` /
  `Literal["dispatcher.reviewer_result.v1"]` (`results.py:58,132`) under
  `strict=True, extra="forbid"` (`config.py` `ContractModel`). Confirmed
  directly.
- **No reshaping/repair in the current parsing path**: `execution.py:403-420`
  uses plain `json.loads` (rejects trailing prose, code fences, concatenated
  objects — verified empirically) with an `object_pairs_hook` rejecting
  duplicate keys before any Pydantic validation runs.
- **Duplicate-key protection independently wired at three layers** — worker
  results (`execution.py:414-420`), supervisor commands
  (`protocol.py:132-138`), OpenCode JSONL events (`sessions.py:454-461`) —
  and for all YAML inputs via one shared `yaml_io.load_unique_yaml` (config,
  plan, ownership map — confirmed no bypassing `yaml.safe_load`/`yaml.load`
  call remains in `src/dispatcher`).
- **Identity/revision/evidence integrity checked by independent
  recomputation, not trust**: dispatch identity (`results.py:247-264`),
  revision (`repository.py:209-224`), and evidence SHA-256/size
  (`repository.py:311-334`) are all recomputed from actual bytes/state and
  compared with exact equality.
- **Cancellation intent is durably persisted strictly before any signal** —
  `state_store.py:329-376` commits to SQLite + appends an audit event before
  returning `process_id`/`process_host` to the caller; proven by
  `tests/fault_injection/test_state_store.py:146-188`.
- **Process-group isolation is correct**: `start_new_session=True` at spawn
  (`sessions.py:624-631`) and `os.killpg` used consistently; verified by a
  test that reaps a grandchild process via group-kill.
- **Provider failure classification fails closed**: `unknown`, `quota`,
  `authentication`, `permission`, `protocol` are all excluded from the
  retryable allow-list (`execution.py:230`) and route to `fail_dispatch` +
  re-raise (`execution.py:245-250`) — confirmed directly, matches docs.
- **Stall-policy exhaustion fails closed**: `sequential.py:1303-1339`
  transitions to `WAITING_OPERATOR` or `HALTED`, never silently continues.
- **Same-repository barrier is a genuine atomic mechanism**: SQLite lease row
  with `ON CONFLICT` semantics (`state_store.py:403-463`); stale-lease
  recovery requires an explicit `recovery_approved_by` and never auto-steals
  (`state_store.py:428-457`), confirmed by fault-injection test.
- **Baseline approval is exhaustive and explicit**: `validate_coverage`
  (`baseline.py:110-116`) requires exactly one decision per observed step, no
  default/auto-accept; `validate_approved_baseline` (`baseline.py:209-229`)
  recomputes a fresh observation digest and rejects any post-approval
  evidence drift — confirmed by a tampering test that mutates evidence and
  observes rejection.
- **Full non-live automated test suite passes**: 237 passed, 0 failed, 5
  deselected (`live_opencode`-marked) — verified directly during this review.

---

## Required Evidence Before T2.2a

1. Implement OS-level process-creation-time capture and verification in the
   cancellation path (closes B1); add an impostor-PID test.
2. Exercise `dispatcher execute` through its actual CLI entry point
   end-to-end in a disposable, non-T2 scenario — all ~14 gates independently
   proven to reject, plus one successful full run (closes B2).
3. Add a real producer for the `LiveSmokeProof` artifact and a recency check
   on `completed_at` (closes B3).
4. Bind `--approval-ref` to a verifiable, content-addressed approval record
   covering project/config/plan/run/step (closes B4).
5. Add an explicit expected/pinned-revision gate per repository (closes B5).
6. Rerun all four `tests/live/test_real_operation_disposable.py` scenarios
   against a real model under the current strict contract and publish a
   freshly dated pass record — do not reuse the existing report (closes B6).
7. Commit the currently uncommitted working-tree changes so T2.2a runs
   against a single, tagged, reviewed commit rather than a dirty worktree
   (N7).
8. Fix `cancel_process_group` to verify post-SIGKILL death rather than
   assuming it (N1), and harden `--decisions` JSON parsing against duplicate
   keys (N3), before proceeding — low cost, directly related to safety
   claims made elsewhere in this same codebase.
9. Confirm (via operator attestation, not via this reviewer opening private
   state/config files) that the plan's own required "Final Enablement
   Decision" has been explicitly recorded, naming the exact config,
   repositories/branches, run/step, and stop conditions.
10. Only then run the fresh baseline-backed, T2.2a-only, fixture-mode,
    no-T2.2b `dispatcher execute` invocation, under direct operator
    observation, with the operator ready to invoke (now-verified)
    cancellation if needed.
