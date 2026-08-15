from __future__ import annotations

import hashlib
import json
import multiprocessing
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from helpers import FixtureProject, create_fixture_project, valid_plan_values

from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.state_store import (
    LeaseConflictError,
    RecoveryRequiredError,
    StaleLeaseRecoveryRequired,
    StateStore,
    StateStoreConflictError,
    StateStoreCorruptionError,
    StateStoreError,
    StateStoreMigrationError,
)
from dispatcher.workflow import (
    BatchRecord,
    BatchStatus,
    CompiledReviewObligation,
    DispatchIntent,
    DispatchRecord,
    DispatchStatus,
    OperatorRequest,
    RepositoryCoordinate,
    RunPolicy,
    RunStatus,
    StepStatus,
    TransitionEvent,
    WorkspaceChild,
    WorkspaceGroup,
    WorkspaceGroupStatus,
    new_run_record,
    transition_dispatch,
    transition_run,
)

_DIGEST = "d" * 64


def _acquire_lease_in_child(state_dir: str, output: multiprocessing.Queue[str]) -> None:
    store = StateStore(state_dir, heartbeat_seconds=30, stale_after_seconds=120)
    try:
        store.acquire_run_lease(
            project_id="fixture-project",
            run_id="fixture-run",
            owner_id="child-owner",
        )
    except LeaseConflictError:
        output.put("conflict")
    else:
        output.put("acquired")


@pytest.fixture
def project(tmp_path: Path) -> FixtureProject:
    return create_fixture_project(tmp_path)


def _event(sequence: int, actor: str = "dispatcher") -> TransitionEvent:
    return TransitionEvent(
        event_id=f"event-{sequence}",
        sequence=sequence,
        actor=actor,  # type: ignore[arg-type]
        reason="fixture state transition",
        correlation_id="fixture-correlation",
        occurred_at=datetime.now(UTC),
    )


def _record(project: FixtureProject):
    return _record_with_steps(project, "prepare-fixture")


def _record_with_steps(
    project: FixtureProject,
    *step_ids: str,
    escalation_role_key: str | None = None,
):
    if not step_ids or step_ids[0] != "prepare-fixture":
        raise ValueError("fixture plans must begin with prepare-fixture")
    values = valid_plan_values(project)
    first = values["steps"][0]
    for ordinal, step_id in enumerate(step_ids[1:], start=2):
        step = json.loads(json.dumps(first))
        step.update(
            {
                "ordinal": ordinal,
                "step_id": step_id,
                "title": step_id.replace("-", " ").title(),
                "produced_outputs": [
                    {
                        "artifact_id": f"{step_id}-output",
                        "producer_step_id": None,
                        "description": f"Output for {step_id}",
                    }
                ],
                "resource_locks": [{"resource_id": f"{step_id}-resource", "mode": "write"}],
                "evidence_requirements": [
                    {
                        "artifact_id": f"{step_id}-evidence",
                        "relative_path": f"{step_id}.md",
                        "media_type": "text/markdown",
                    }
                ],
            }
        )
        values["steps"].append(step)
    if escalation_role_key is not None:
        values["steps"][0]["retry"].update(
            {
                "on_changes_requested": "escalate",
                "escalation_role_key": escalation_role_key,
            }
        )
    plan = NormalizedPlan.model_validate(values)
    return new_run_record(
        run_id="fixture-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-approve-plan"),
        event=_event(1),
    )


def _store(project: FixtureProject) -> StateStore:
    return StateStore(
        project.state,
        heartbeat_seconds=project.config.lease_heartbeat_seconds,
        stale_after_seconds=project.config.lease_stale_after_seconds,
    )


def _dispatch(prompt: str, policy: dict[str, object]) -> DispatchRecord:
    policy_digest = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return DispatchRecord(
        dispatch_id="dispatch-one",
        step_id="prepare-fixture",
        role_key="terra",
        role_kind="executor",
        attempt=1,
        logical_session_key="session-one",
        runtime_session_id=None,
        state=DispatchStatus.PREPARED,
        intent=DispatchIntent(
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            policy_digest=policy_digest,
            expected_result_kind="executor",
            repository=RepositoryCoordinate(repo_id="fixture-repo", base_revision="base-sha"),
            idempotency_key="idempotency-one",
        ),
        result_digest=None,
        forwarding_digest=None,
        last_event=_event(1),
    )


def _request(
    *,
    kind: str,
    allowed_answers: list[str],
    context_ref: str = "fixture-context",
    step_id: str | None = None,
    resume_to: RunStatus = RunStatus.READY,
    reassignment_role_key: str | None = None,
) -> OperatorRequest:
    return OperatorRequest(
        request_id=f"request-{kind}",
        question=f"Resolve {kind}",
        allowed_answers=allowed_answers,
        context_ref=context_ref,
        resume_to=resume_to,
        expires_at=None,
        required_role=None,
        kind=kind,  # type: ignore[arg-type]
        step_id=step_id,
        reassignment_role_key=reassignment_role_key,
    )


def _waiting(record, request: OperatorRequest):
    return transition_run(
        record,
        RunStatus.WAITING_OPERATOR,
        _event(record.sequence + 1),
        operator_request=request,
    )


def _persist_waiting(project: FixtureProject, record, request: OperatorRequest):
    store = _store(project)
    waiting = _waiting(record, request)
    assert store.create_run(waiting) == 1
    return store, waiting


def _answer(store: StateStore, waiting, answer: str):
    updated, generation = store.answer_operator_request(
        run_id=waiting.run_id,
        expected_generation=1,
        request_id=waiting.operator_request.request_id,
        answer=answer,
        actor_id="operator-fixture",
    )
    assert generation == 2
    assert updated.operator_request is None
    assert _operator_decision_count(store) == 1
    return updated


def _operator_decision_count(store: StateStore) -> int:
    with sqlite3.connect(store.database_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM operator_decisions").fetchone()[0])


def _step_in_state(record, step_id: str, state: StepStatus):
    steps = dict(record.steps)
    steps[step_id] = steps[step_id].model_copy(update={"state": state, "last_event": _event(record.sequence)})
    return record.model_copy(update={"steps": steps})


def _dispatch_in_state(
    *,
    dispatch_id: str,
    step_id: str,
    state: DispatchStatus,
    batch_id: str | None = None,
) -> DispatchRecord:
    dispatch = _dispatch("fixture prompt", {"permission": {"*": "deny"}})
    updates: dict[str, object] = {
        "dispatch_id": dispatch_id,
        "step_id": step_id,
        "state": state,
        "batch_id": batch_id,
        "intent": dispatch.intent.model_copy(update={"idempotency_key": f"idempotency-{dispatch_id}"}),
    }
    if state in {DispatchStatus.COMPLETED, DispatchStatus.FORWARDED, DispatchStatus.ACKNOWLEDGED}:
        updates.update({"runtime_session_id": f"session-{dispatch_id}", "result_digest": _DIGEST})
    if state in {DispatchStatus.FORWARDED, DispatchStatus.ACKNOWLEDGED}:
        updates["forwarding_digest"] = _DIGEST
    return dispatch.model_copy(update=updates)


def _batch(
    dispatch_ids: tuple[str, ...],
    failed_dispatch_ids: tuple[str, ...],
    *,
    state: BatchStatus = BatchStatus.FAILED,
) -> BatchRecord:
    return BatchRecord(
        batch_id="batch-one",
        dispatch_ids=dispatch_ids,
        state=state,
        failure_mode="wait_for_started",
        failed_dispatch_ids=failed_dispatch_ids,
        last_event=_event(1),
    )


def _waivable_policy(record) -> RunPolicy:
    obligations = {
        step_id: CompiledReviewObligation(
            step_id=step_id,
            required=True,
            reviewer_role_keys=("reviewer",),
            required_acceptances=1,
            independence="fresh_session",
            waivable=True,
            source_policy_digest=_DIGEST,
        )
        for step_id in record.steps
    }
    return RunPolicy(
        profile_id="fixture-profile",
        profile_digest=_DIGEST,
        review_obligations=obligations,
        underspec_mode="ask",
        policy_digest=_DIGEST,
    )


def _workspace_group(project: FixtureProject, *, state: WorkspaceGroupStatus) -> WorkspaceGroup:
    root = project.root / "workspaces" / "workspace-one"
    return WorkspaceGroup(
        workspace_group_id="workspace-one",
        repo_id="fixture-repo",
        base_revision="base-sha",
        base_branch="main",
        integration_branch="dispatcher/workspace/workspace-one/integration",
        integration_worktree_path=str(root / "integration"),
        worktree_root=str(root),
        lease_owner_id="workspace-workspace-one",
        children=(
            WorkspaceChild(
                step_id="prepare-fixture",
                branch="dispatcher/workspace/workspace-one/prepare-fixture",
                worktree_path=str(root / "prepare-fixture"),
                base_revision="base-sha",
            ),
        ),
        state=state,
        last_event=_event(1),
    )


def test_snapshot_commits_every_generation_atomically(project: FixtureProject) -> None:
    store = _store(project)
    record = _record(project)
    sessions = {"executors": {"terra": {"session_id": "ses-fixture"}}}

    assert store.create_run(record, sessions=sessions) == 1
    updated = transition_run(record, RunStatus.READY, _event(2))
    assert store.save_run(updated, expected_generation=1, sessions=sessions) == 2

    loaded, generation = store.load_run(record.run_id)
    assert loaded == updated
    assert generation == 2
    with sqlite3.connect(store.database_path) as connection:
        run_generation = connection.execute(
            "SELECT generation FROM runs WHERE run_id = ?", (record.run_id,)
        ).fetchone()[0]
        plan_generation = connection.execute(
            "SELECT generation FROM normalized_plans WHERE run_id = ?", (record.run_id,)
        ).fetchone()[0]
        step_generations = {
            row[0]
            for row in connection.execute("SELECT generation FROM steps WHERE run_id = ?", (record.run_id,))
        }
        session_generations = {
            row[0]
            for row in connection.execute("SELECT generation FROM sessions WHERE run_id = ?", (record.run_id,))
        }

    assert {run_generation, plan_generation, *step_generations, *session_generations} == {2}


def test_cancellation_intent_is_persisted_before_process_signal(project: FixtureProject) -> None:
    store = _store(project)
    record = _record(project)
    dispatch = _dispatch("perform fixture task", {"permission": {"*": "deny"}})
    prepared_record = record.model_copy(update={"dispatches": {dispatch.dispatch_id: dispatch}})
    store.create_run(prepared_record)
    generation = store.prepare_dispatch(
        prepared_record,
        expected_generation=1,
        dispatch=dispatch,
        prompt="perform fixture task",
        policy={"permission": {"*": "deny"}},
        repository_before={"repo_id": "fixture-repo"},
    )
    running = transition_dispatch(
        dispatch,
        DispatchStatus.RUNNING,
        _event(2),
        runtime_session_id="session-one",
        process_id=4242,
        process_host="fixture-host",
        process_started_at=datetime.now(UTC),
        process_create_time=1234.5,
    )
    running_record = prepared_record.model_copy(update={"dispatches": {dispatch.dispatch_id: running}})
    generation = store.save_run(running_record, expected_generation=generation)

    cancelled, next_generation, process_id, process_host, process_create_time = (
        store.request_dispatch_cancellation(
            run_id=record.run_id,
            expected_generation=generation,
            dispatch_id=dispatch.dispatch_id,
            actor_id="operator",
        )
    )

    assert next_generation == generation + 1
    assert process_id == 4242
    assert process_host == "fixture-host"
    assert process_create_time == 1234.5
    assert cancelled.dispatches[dispatch.dispatch_id].cancel_requested is True
    assert store.load_run(record.run_id)[0].dispatches[dispatch.dispatch_id].process_create_time == 1234.5
    with sqlite3.connect(store.database_path) as connection:
        kind = connection.execute(
            "SELECT kind FROM audit_events WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
            (record.run_id,),
        ).fetchone()[0]
    assert kind == "dispatch_cancellation_requested"


def test_faulted_snapshot_rolls_back_all_tables(project: FixtureProject) -> None:
    store = _store(project)
    record = _record(project)
    store.create_run(record)
    updated = transition_run(record, RunStatus.READY, _event(2))

    with pytest.raises(RuntimeError, match="injected crash"):
        store.save_run(
            updated,
            expected_generation=1,
            fault_hook=lambda: (_ for _ in ()).throw(RuntimeError("injected crash")),
        )

    loaded, generation = store.load_run(record.run_id)
    assert loaded == record
    assert generation == 1


def test_start_and_resume_require_explicit_nonterminal_state(project: FixtureProject) -> None:
    store = _store(project)
    record = _record(project)
    store.create_run(record)

    resumed, generation = store.resume_run(project_id=record.project_id, run_id=record.run_id)

    assert resumed == record
    assert generation == 1
    with pytest.raises(StateStoreConflictError, match="already has active run"):
        store.create_run(record.model_copy(update={"run_id": "second-run"}))

    terminal = transition_run(record, RunStatus.READY, _event(2))
    terminal = transition_run(terminal, RunStatus.CANCELLED, _event(3))
    store.save_run(terminal, expected_generation=1)
    with pytest.raises(StateStoreError, match="terminal runs require"):
        store.resume_run(project_id=record.project_id, run_id=record.run_id)


def test_corruption_and_newer_schema_fail_with_recovery_guidance(
    project: FixtureProject,
    tmp_path: Path,
) -> None:
    store = _store(project)
    store.initialize()
    store.close()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (99, 'now')")

    with pytest.raises(StateStoreMigrationError, match="upgrade dispatcher"):
        _store(project).initialize()

    corrupt_parent = tmp_path / "corrupt"
    corrupt_parent.mkdir()
    corrupt_project = create_fixture_project(corrupt_parent)
    database = corrupt_project.state / "dispatcher.sqlite3"
    corrupt_project.state.mkdir(parents=True, exist_ok=True)
    database.write_text("not a sqlite database", encoding="utf-8")
    with pytest.raises(StateStoreCorruptionError, match="restore from a verified backup"):
        _store(corrupt_project).initialize()


def test_workspace_group_table_migrates_existing_phase_three_database(
    project: FixtureProject,
) -> None:
    store = _store(project)
    store.initialize()
    store.close()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("ALTER TABLE dispatch_payloads DROP COLUMN repository_before_json")
        connection.execute("ALTER TABLE dispatch_payloads DROP COLUMN repository_after_json")
        connection.execute("ALTER TABLE dispatch_payloads DROP COLUMN authoritative_verification_json")
        connection.execute("DROP TABLE workspace_groups")
        connection.execute("DROP TABLE baseline_approvals")
        connection.execute("DROP TABLE structured_git_commits")
        connection.execute("DROP TABLE opencode_invocations")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 3")

    migrated = _store(project)
    migrated.initialize()
    with sqlite3.connect(migrated.database_path) as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'baselines'"
        ).fetchone()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(dispatch_payloads)").fetchall()}
        workspace_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'workspace_groups'"
        ).fetchone()
        approval_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'baseline_approvals'"
        ).fetchone()

    assert version == 8
    assert table == ("baselines",)
    assert columns >= {
        "repository_before_json",
        "repository_after_json",
        "authoritative_verification_json",
    }
    assert workspace_table == ("workspace_groups",)
    assert approval_table == ("baseline_approvals",)


def test_opencode_invocation_usage_is_incremental_and_idempotent(
    project: FixtureProject,
) -> None:
    store = _store(project)
    record = _record(project)
    store.create_run(record)

    store.begin_opencode_invocation(
        invocation_id="supervisor-first",
        run_id=record.run_id,
        dispatch_id=None,
        role_kind="supervisor",
        role_key="supervisor",
        step_id=None,
        session_mode="new",
        requested_session_id=None,
    )
    first, first_generation = store.finish_opencode_invocation(
        invocation_id="supervisor-first",
        runtime_session_id="session-supervisor",
        usage={
            "cost_usd": 0.1,
            "tokens_total": 10,
            "tokens_input": 6,
            "tokens_output": 4,
            "tokens_reasoning": 0,
        },
    )
    repeated, repeated_generation = store.finish_opencode_invocation(
        invocation_id="supervisor-first",
        runtime_session_id="session-supervisor",
        usage={
            "cost_usd": 0.1,
            "tokens_total": 10,
            "tokens_input": 6,
            "tokens_output": 4,
            "tokens_reasoning": 0,
        },
    )

    store.begin_opencode_invocation(
        invocation_id="supervisor-resumed",
        run_id=record.run_id,
        dispatch_id=None,
        role_kind="supervisor",
        role_key="supervisor",
        step_id=None,
        session_mode="resume",
        requested_session_id="session-supervisor",
    )
    resumed, resumed_generation = store.finish_opencode_invocation(
        invocation_id="supervisor-resumed",
        runtime_session_id="session-supervisor",
        usage={
            "cost_usd": 0.2,
            "tokens_total": 20,
            "tokens_input": 12,
            "tokens_output": 7,
            "tokens_reasoning": 1,
        },
    )

    assert first_generation == 2
    assert repeated_generation == first_generation
    assert repeated == first
    assert resumed_generation == 3
    assert resumed.usage.run.cost_usd == pytest.approx(0.3)
    assert resumed.usage.run.tokens_total == 30
    assert resumed.usage.by_role["supervisor"].tokens_total == 30
    assert resumed.usage.by_session["session-supervisor"].tokens_total == 30
    assert len(store.opencode_invocations_for_run(record.run_id)) == 2


def test_opencode_invocation_missing_usage_is_reported_as_unknown(
    project: FixtureProject,
) -> None:
    store = _store(project)
    record = _record(project)
    store.create_run(record)
    store.begin_opencode_invocation(
        invocation_id="supervisor-missing",
        run_id=record.run_id,
        dispatch_id=None,
        role_kind="supervisor",
        role_key="supervisor",
        step_id=None,
        session_mode="new",
        requested_session_id=None,
    )
    with pytest.raises(RecoveryRequiredError, match="automatic relaunch is forbidden"):
        store.begin_opencode_invocation(
            invocation_id="supervisor-missing",
            run_id=record.run_id,
            dispatch_id=None,
            role_kind="supervisor",
            role_key="supervisor",
            step_id=None,
            session_mode="new",
            requested_session_id=None,
        )
    store.finish_opencode_invocation(
        invocation_id="supervisor-missing",
        runtime_session_id="session-supervisor",
        usage=None,
        failure_category="protocol",
    )

    report = store.export_run_report(record.run_id).read_text(encoding="utf-8")

    assert "Missing usage records: `1`" in report
    assert "`MISSING` | unknown | unknown" in report


def test_recovery_finalizes_prepared_opencode_invocations_without_usage(
    project: FixtureProject,
) -> None:
    store = _store(project)
    record = _record(project)
    store.create_run(record)
    store.begin_opencode_invocation(
        invocation_id="supervisor-interrupted",
        run_id=record.run_id,
        dispatch_id=None,
        role_kind="supervisor",
        role_key="supervisor",
        step_id=None,
        session_mode="new",
        requested_session_id=None,
    )

    assert [item["invocation_id"] for item in store.prepared_opencode_invocations_for_run(record.run_id)] == [
        "supervisor-interrupted"
    ]
    assert store.finalize_prepared_opencode_invocations(record.run_id) == ("supervisor-interrupted",)
    assert store.finalize_prepared_opencode_invocations(record.run_id) == ()

    invocation = store.opencode_invocations_for_run(record.run_id)[0]
    assert invocation["lifecycle"] == "FAILED"
    assert invocation["usage_status"] == "MISSING"
    assert invocation["failure_category"] == "interrupted"


def test_session_inspection_report_uses_state_directory_for_supervisor_root(
    project: FixtureProject,
) -> None:
    store = _store(project)
    record = _record(project)
    generation = store.create_run(record)
    persisted, generation = store.load_run(record.run_id)
    generation = store.save_run(
        persisted,
        expected_generation=generation,
        sessions={
            "supervisor": {
                "supervisor": {
                    "session_id": "ses-supervisor-inspection",
                    "role_key": "supervisor",
                    "working_directory": str(project.repository),
                    "status": "active",
                }
            }
        },
    )

    report = store.export_run_report(
        record.run_id,
        generation_override=generation,
    ).read_text(encoding="utf-8")

    assert (
        f"| `supervisor:supervisor` | `ses-supervisor-inspection` | "
        f"`{project.repository}` | `.` | `opencode-events` |"
    ) in report


def test_structured_git_lifecycle_is_durable_and_rejects_duplicate_side_effects(
    project: FixtureProject,
) -> None:
    store = _store(project)
    record = _record(project)
    prompt = "perform fixture task"
    policy: dict[str, object] = {"permission": {"*": "deny"}}
    dispatch = _dispatch(prompt, policy)
    prepared_record = record.model_copy(update={"dispatches": {dispatch.dispatch_id: dispatch}})
    store.create_run(prepared_record)
    store.prepare_dispatch(
        prepared_record,
        expected_generation=1,
        dispatch=dispatch,
        prompt=prompt,
        policy=policy,
        repository_before={"repo_id": "fixture-repo"},
    )
    proposal = {
        "proposal_version": 2,
        "response_contract": "dispatcher.executor_proposal.v2",
        "dispatch_id": dispatch.dispatch_id,
    }

    received = store.record_executor_proposal(
        run_id=record.run_id,
        dispatch_id=dispatch.dispatch_id,
        proposal=proposal,
    )
    checked = store.record_structured_git_checked(
        run_id=record.run_id,
        dispatch_id=dispatch.dispatch_id,
        checked={"verification": []},
        intent={"candidate_tree": "a" * 40},
    )
    staged = store.record_structured_git_staged(
        run_id=record.run_id,
        dispatch_id=dispatch.dispatch_id,
        stage={"transcript_sha256": "b" * 64},
    )

    assert received.state == "PROPOSAL_RECEIVED"
    assert checked.state == "COMMIT_INTENT_PERSISTED"
    assert staged.state == "STAGED"
    store.close()
    restored = _store(project).load_structured_git_record(record.run_id, dispatch.dispatch_id)
    assert restored.proposal == proposal
    assert restored.intent == {"candidate_tree": "a" * 40}
    with pytest.raises(StateStoreConflictError, match="cannot move from STAGED"):
        _store(project).record_structured_git_staged(
            run_id=record.run_id,
            dispatch_id=dispatch.dispatch_id,
            stage={"transcript_sha256": "c" * 64},
        )


def test_leases_are_single_writer_atomic_and_require_approved_stale_recovery(
    project: FixtureProject,
) -> None:
    store = _store(project)
    record = _record(project)
    store.create_run(record)

    store.acquire_resource_leases(
        run_id=record.run_id,
        owner_id="owner-one",
        resource_keys=["repo:fixture-repo", "run:fixture-project"],
    )
    with pytest.raises(LeaseConflictError, match="held by owner-one"):
        store.acquire_resource_leases(
            run_id=record.run_id,
            owner_id="owner-two",
            resource_keys=["repo:fixture-repo"],
        )
    with pytest.raises(LeaseConflictError):
        store.acquire_resource_leases(
            run_id=record.run_id,
            owner_id="owner-two",
            resource_keys=["repo:free", "repo:fixture-repo"],
        )
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT owner_id FROM leases WHERE resource_key = 'repo:free'"
        ).fetchone() is None
        connection.execute(
            "UPDATE leases SET heartbeat_at = ? WHERE resource_key = 'repo:fixture-repo'",
            ((datetime.now(UTC) - timedelta(seconds=121)).isoformat(),),
        )

    with pytest.raises(StaleLeaseRecoveryRequired, match="explicit operator approval"):
        store.acquire_resource_leases(
            run_id=record.run_id,
            owner_id="owner-two",
            resource_keys=["repo:fixture-repo"],
        )
    recovered = store.acquire_resource_leases(
        run_id=record.run_id,
        owner_id="owner-two",
        resource_keys=["repo:fixture-repo"],
        recovery_approved_by="decision-stale-lock",
    )

    assert recovered[0].owner_id == "owner-two"


def test_second_process_cannot_acquire_the_active_run_lease(project: FixtureProject) -> None:
    store = _store(project)
    record = _record(project)
    store.create_run(record)
    store.acquire_run_lease(
        project_id=record.project_id,
        run_id=record.run_id,
        owner_id="parent-owner",
    )
    context = multiprocessing.get_context("spawn")
    output: multiprocessing.Queue[str] = context.Queue()
    process = context.Process(target=_acquire_lease_in_child, args=(str(project.state), output))

    process.start()
    process.join(timeout=10)

    assert process.exitcode == 0
    assert output.get(timeout=2) == "conflict"


def test_prepared_running_completed_and_forwarded_recovery_is_deterministic(
    project: FixtureProject,
) -> None:
    store = _store(project)
    record = _record(project)
    prompt = "perform fixture task"
    policy: dict[str, object] = {"permission": {"*": "deny"}}
    dispatch = _dispatch(prompt, policy)
    prepared_record = record.model_copy(update={"dispatches": {dispatch.dispatch_id: dispatch}})
    store.create_run(prepared_record)
    generation = store.prepare_dispatch(
        prepared_record,
        expected_generation=1,
        dispatch=dispatch,
        prompt=prompt,
        policy=policy,
        repository_before={"repo_id": "fixture-repo"},
    )
    store.close()
    store = _store(project)
    assert store.classify_recovery(record.run_id)[0].disposition == (
        "operator_reconciliation_required"
    )

    running = transition_dispatch(
        dispatch,
        DispatchStatus.RUNNING,
        _event(2),
        runtime_session_id="ses-fixture",
        process_create_time=1234.5,
    )
    running_record = prepared_record.model_copy(update={"dispatches": {running.dispatch_id: running}})
    generation = store.save_run(running_record, expected_generation=generation)
    store.close()
    store = _store(project)
    item = store.classify_recovery(record.run_id)[0]
    assert item.disposition == "operator_reconciliation_required"
    assert "automatic retry is forbidden" in item.detail

    completed = transition_dispatch(
        running,
        DispatchStatus.COMPLETED,
        _event(3),
        result_digest=_DIGEST,
    )
    completed_record = running_record.model_copy(update={"dispatches": {completed.dispatch_id: completed}})
    generation = store.save_run(completed_record, expected_generation=generation)
    store.close()
    store = _store(project)
    assert store.classify_recovery(record.run_id)[0].disposition == "forwarding_required"

    forwarded = transition_dispatch(
        completed,
        DispatchStatus.FORWARDED,
        _event(4),
        forwarding_digest=_DIGEST,
    )
    forwarded_record = completed_record.model_copy(update={"dispatches": {forwarded.dispatch_id: forwarded}})
    store.save_run(forwarded_record, expected_generation=generation)
    store.close()
    store = _store(project)
    assert store.classify_recovery(record.run_id)[0].disposition == "acknowledgement_required"


def test_operator_answer_and_transcripts_are_durable_and_collision_free(project: FixtureProject) -> None:
    store = _store(project)
    record = transition_run(_record(project), RunStatus.READY, _event(2))
    request = OperatorRequest(
        request_id="request-one",
        question="Choose a safe option",
        allowed_answers=["answer", "halt"],
        context_ref="context-one",
        resume_to=RunStatus.READY,
        expires_at=None,
        required_role=None,
    )
    waiting = transition_run(record, RunStatus.WAITING_OPERATOR, _event(3), operator_request=request)
    store.create_run(waiting)
    store.close()
    store = _store(project)

    answered, generation = store.answer_operator_request(
        run_id=waiting.run_id,
        expected_generation=1,
        request_id=request.request_id,
        answer="answer",
        actor_id="operator-fixture",
    )
    first = store.write_transcript(
        run_id=waiting.run_id,
        dispatch_id=None,
        content="Authorization: Bearer secret-token",
        sequence=answered.sequence,
        label="operator answer",
    )
    second = store.write_transcript(
        run_id=waiting.run_id,
        dispatch_id=None,
        content="second transcript",
        sequence=answered.sequence,
        label="operator answer",
    )

    assert generation == 2
    assert answered.state is RunStatus.RUNNING
    assert first != second
    assert "secret-token" not in first.read_text(encoding="utf-8")
    assert store.export_run_report(waiting.run_id).is_file()
    with pytest.raises(StateStoreError, match="not waiting"):
        store.answer_operator_request(
            run_id=waiting.run_id,
            expected_generation=generation,
            request_id=request.request_id,
            answer="answer",
            actor_id="operator-fixture",
        )


def test_authoritative_run_report_includes_audit_event_summary(project: FixtureProject) -> None:
    store = _store(project)
    record = _record(project)
    store.create_run(record)
    store.append_audit_event(
        run_id=record.run_id,
        event_id="audit-summary-event",
        sequence=2,
        kind="operator_decision",
        correlation_id="fixture-correlation",
        causation_id=None,
        payload={"token": "must-not-appear-in-summary"},
    )

    report = store.export_run_report(record.run_id).read_text(encoding="utf-8")

    assert "## Audit Events" in report
    assert "operator_decision" in report
    assert "fixture-correlation" in report
    assert "must-not-appear-in-summary" not in report


@pytest.mark.parametrize(
    ("expires_at", "required_role", "actor_id", "message"),
    [
        (datetime.now(UTC) - timedelta(seconds=1), None, "operator", "has expired"),
        (None, "release-manager", "operator", "required role"),
    ],
)
def test_operator_answer_rejects_expired_or_unauthorized_requests(
    project: FixtureProject,
    expires_at: datetime | None,
    required_role: str | None,
    actor_id: str,
    message: str,
) -> None:
    store = _store(project)
    record = transition_run(_record(project), RunStatus.READY, _event(2))
    request = OperatorRequest(
        request_id="request-guarded",
        question="Approve guarded action",
        allowed_answers=["approve"],
        context_ref="guarded",
        resume_to=RunStatus.READY,
        expires_at=expires_at,
        required_role=required_role,
        kind="risk_gate",
        step_id="prepare-fixture",
    )
    waiting = transition_run(record, RunStatus.WAITING_OPERATOR, _event(3), operator_request=request)
    store.create_run(waiting)

    with pytest.raises(StateStoreError, match=message):
        store.answer_operator_request(
            run_id=waiting.run_id,
            expected_generation=1,
            request_id=request.request_id,
            answer="approve",
            actor_id=actor_id,
        )


def test_risk_gate_approve_resolves_step_gate_and_resumes_running(project: FixtureProject) -> None:
    record = _record(project)
    step = record.steps["prepare-fixture"].model_copy(update={"operator_gate_resolved": False})
    record = record.model_copy(update={"steps": {"prepare-fixture": step}})
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="risk_gate",
            allowed_answers=["approve", "deny"],
            step_id="prepare-fixture",
        ),
    )

    updated = _answer(store, waiting, "approve")

    assert updated.state is RunStatus.RUNNING
    assert updated.steps["prepare-fixture"].operator_gate_resolved is True


def test_risk_gate_deny_halts_the_run(project: FixtureProject) -> None:
    record = _record(project)
    step = record.steps["prepare-fixture"].model_copy(update={"operator_gate_resolved": False})
    record = record.model_copy(update={"steps": {"prepare-fixture": step}})
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="risk_gate",
            allowed_answers=["approve", "deny"],
            step_id="prepare-fixture",
        ),
    )

    updated = _answer(store, waiting, "deny")

    assert updated.state is RunStatus.HALTED
    assert updated.steps["prepare-fixture"].operator_gate_resolved is False


def test_escalation_reassign_requires_blocked_step_and_resumes_ready(project: FixtureProject) -> None:
    record = _step_in_state(
        _record_with_steps(project, "prepare-fixture", escalation_role_key="terra"),
        "prepare-fixture",
        StepStatus.BLOCKED,
    )
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="escalation",
            allowed_answers=["reassign", "halt"],
            step_id="prepare-fixture",
            reassignment_role_key="terra",
        ),
    )

    updated = _answer(store, waiting, "reassign")

    assert updated.state is RunStatus.RUNNING
    assert updated.steps["prepare-fixture"].state is StepStatus.READY
    assert updated.steps["prepare-fixture"].reassignment_role_key == "terra"


def test_escalation_halt_preserves_blocked_step_and_halts(project: FixtureProject) -> None:
    record = _step_in_state(
        _record_with_steps(project, "prepare-fixture", escalation_role_key="terra"),
        "prepare-fixture",
        StepStatus.BLOCKED,
    )
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="escalation",
            allowed_answers=["reassign", "halt"],
            step_id="prepare-fixture",
            reassignment_role_key="terra",
        ),
    )

    updated = _answer(store, waiting, "halt")

    assert updated.state is RunStatus.HALTED
    assert updated.steps["prepare-fixture"].state is StepStatus.BLOCKED


def test_review_waiver_waive_accepts_step_and_records_decision_reference(
    project: FixtureProject,
) -> None:
    record = _record(project)
    record = record.model_copy(update={"policy": _waivable_policy(record)})
    record = _step_in_state(record, "prepare-fixture", StepStatus.REVIEW_REQUIRED)
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="review_waiver",
            allowed_answers=["waive", "halt"],
            step_id="prepare-fixture",
        ),
    )

    updated = _answer(store, waiting, "waive")

    assert updated.state is RunStatus.RUNNING
    assert updated.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert updated.steps["prepare-fixture"].review_waiver_decision_ref is not None


def test_review_waiver_halt_preserves_review_required_step(project: FixtureProject) -> None:
    record = _record(project)
    record = record.model_copy(update={"policy": _waivable_policy(record)})
    record = _step_in_state(record, "prepare-fixture", StepStatus.REVIEW_REQUIRED)
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="review_waiver",
            allowed_answers=["waive", "halt"],
            step_id="prepare-fixture",
        ),
    )

    updated = _answer(store, waiting, "halt")

    assert updated.state is RunStatus.HALTED
    assert updated.steps["prepare-fixture"].state is StepStatus.REVIEW_REQUIRED


def test_stall_recovery_retry_moves_blocked_step_to_ready(project: FixtureProject) -> None:
    record = _step_in_state(_record(project), "prepare-fixture", StepStatus.BLOCKED)
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="stall_recovery",
            allowed_answers=["retry", "halt"],
            step_id="prepare-fixture",
        ),
    )

    updated = _answer(store, waiting, "retry")

    assert updated.state is RunStatus.RUNNING
    assert updated.steps["prepare-fixture"].state is StepStatus.READY


def test_stall_recovery_retry_preserves_review_required_and_refreshes_event(
    project: FixtureProject,
) -> None:
    record = _step_in_state(_record(project), "prepare-fixture", StepStatus.REVIEW_REQUIRED)
    previous_event = record.steps["prepare-fixture"].last_event
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="stall_recovery",
            allowed_answers=["retry", "halt"],
            step_id="prepare-fixture",
        ),
    )

    updated = _answer(store, waiting, "retry")

    assert updated.state is RunStatus.RUNNING
    assert updated.steps["prepare-fixture"].state is StepStatus.REVIEW_REQUIRED
    assert updated.steps["prepare-fixture"].last_event != previous_event


def test_stall_recovery_halt_preserves_step_and_halts(project: FixtureProject) -> None:
    record = _step_in_state(_record(project), "prepare-fixture", StepStatus.BLOCKED)
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="stall_recovery",
            allowed_answers=["retry", "halt"],
            step_id="prepare-fixture",
        ),
    )

    updated = _answer(store, waiting, "halt")

    assert updated.state is RunStatus.HALTED
    assert updated.steps["prepare-fixture"].state is StepStatus.BLOCKED


def test_underspecification_answer_acknowledges_and_resumes_running(project: FixtureProject) -> None:
    store, waiting = _persist_waiting(
        project,
        _record(project),
        _request(kind="underspecification", allowed_answers=["answer", "halt"]),
    )

    updated = _answer(store, waiting, "answer")

    assert updated.state is RunStatus.RUNNING


def test_underspecification_halt_halts_the_run(project: FixtureProject) -> None:
    store, waiting = _persist_waiting(
        project,
        _record(project),
        _request(kind="underspecification", allowed_answers=["answer", "halt"]),
    )

    updated = _answer(store, waiting, "halt")

    assert updated.state is RunStatus.HALTED


def test_budget_halt_explicitly_halts_despite_nonhalting_resume_target(project: FixtureProject) -> None:
    store, waiting = _persist_waiting(
        project,
        _record(project),
        _request(
            kind="budget",
            allowed_answers=["halt"],
            step_id="prepare-fixture",
            resume_to=RunStatus.RUNNING,
        ),
    )

    updated = _answer(store, waiting, "halt")

    assert updated.state is RunStatus.HALTED


def test_reconciliation_reconcile_moves_blocked_step_to_ready(project: FixtureProject) -> None:
    dispatch = _dispatch_in_state(
        dispatch_id="dispatch-failed",
        step_id="prepare-fixture",
        state=DispatchStatus.FAILED,
    )
    record = _step_in_state(_record(project), "prepare-fixture", StepStatus.BLOCKED)
    record = record.model_copy(update={"dispatches": {dispatch.dispatch_id: dispatch}})
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=dispatch.dispatch_id,
            step_id="prepare-fixture",
        ),
    )

    updated = _answer(store, waiting, "reconcile")

    assert updated.state is RunStatus.RUNNING
    assert updated.steps["prepare-fixture"].state is StepStatus.READY
    assert updated.dispatches[dispatch.dispatch_id].state is DispatchStatus.FAILED


def test_reconciliation_reconcile_preserves_review_required_and_refreshes_event(
    project: FixtureProject,
) -> None:
    dispatch = _dispatch_in_state(
        dispatch_id="dispatch-review-failed",
        step_id="prepare-fixture",
        state=DispatchStatus.ABANDONED,
    )
    record = _step_in_state(_record(project), "prepare-fixture", StepStatus.REVIEW_REQUIRED)
    previous_event = record.steps["prepare-fixture"].last_event
    record = record.model_copy(update={"dispatches": {dispatch.dispatch_id: dispatch}})
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=dispatch.dispatch_id,
            step_id="prepare-fixture",
        ),
    )

    updated = _answer(store, waiting, "reconcile")

    assert updated.state is RunStatus.RUNNING
    assert updated.steps["prepare-fixture"].state is StepStatus.REVIEW_REQUIRED
    assert updated.steps["prepare-fixture"].last_event != previous_event


def test_reconciliation_halt_preserves_failed_step_and_halts(project: FixtureProject) -> None:
    dispatch = _dispatch_in_state(
        dispatch_id="dispatch-failed",
        step_id="prepare-fixture",
        state=DispatchStatus.FAILED,
    )
    record = _step_in_state(_record(project), "prepare-fixture", StepStatus.BLOCKED)
    record = record.model_copy(update={"dispatches": {dispatch.dispatch_id: dispatch}})
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=dispatch.dispatch_id,
            step_id="prepare-fixture",
        ),
    )

    updated = _answer(store, waiting, "halt")

    assert updated.state is RunStatus.HALTED
    assert updated.steps["prepare-fixture"].state is StepStatus.BLOCKED


def test_reconciliation_rejects_unknown_dispatch_without_decision(project: FixtureProject) -> None:
    record = _step_in_state(_record(project), "prepare-fixture", StepStatus.BLOCKED)
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref="dispatch-missing",
            step_id="prepare-fixture",
        ),
    )

    with pytest.raises(StateStoreCorruptionError, match="known dispatch"):
        _answer(store, waiting, "reconcile")

    assert _operator_decision_count(store) == 0
    assert store.load_run(waiting.run_id)[0].state is RunStatus.WAITING_OPERATOR


def test_reconciliation_rejects_dispatch_for_another_step_without_decision(
    project: FixtureProject,
) -> None:
    dispatch = _dispatch_in_state(
        dispatch_id="dispatch-mismatched",
        step_id="other-step",
        state=DispatchStatus.FAILED,
    )
    record = _step_in_state(_record(project), "prepare-fixture", StepStatus.BLOCKED)
    record = record.model_copy(update={"dispatches": {dispatch.dispatch_id: dispatch}})
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=dispatch.dispatch_id,
            step_id="prepare-fixture",
        ),
    )

    with pytest.raises(StateStoreCorruptionError, match="does not belong"):
        _answer(store, waiting, "reconcile")

    assert _operator_decision_count(store) == 0


def test_reconciliation_rejects_nonterminal_dispatch_without_decision(project: FixtureProject) -> None:
    dispatch = _dispatch_in_state(
        dispatch_id="dispatch-prepared",
        step_id="prepare-fixture",
        state=DispatchStatus.PREPARED,
    )
    record = _step_in_state(_record(project), "prepare-fixture", StepStatus.BLOCKED)
    record = record.model_copy(update={"dispatches": {dispatch.dispatch_id: dispatch}})
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=dispatch.dispatch_id,
            step_id="prepare-fixture",
        ),
    )

    with pytest.raises(StateStoreError, match="failed or abandoned"):
        _answer(store, waiting, "reconcile")

    assert _operator_decision_count(store) == 0


def test_batch_reconciliation_requeues_unique_failed_steps_and_preserves_accepted_sibling(
    project: FixtureProject,
) -> None:
    record = _record_with_steps(
        project,
        "prepare-fixture",
        "prepare-second",
        "accepted-sibling",
    )
    record = _step_in_state(record, "prepare-fixture", StepStatus.BLOCKED)
    record = _step_in_state(record, "prepare-second", StepStatus.BLOCKED)
    record = _step_in_state(record, "accepted-sibling", StepStatus.ACCEPTED)
    failed_one = _dispatch_in_state(
        dispatch_id="dispatch-failed-one",
        step_id="prepare-fixture",
        state=DispatchStatus.FAILED,
        batch_id="batch-one",
    )
    failed_two = _dispatch_in_state(
        dispatch_id="dispatch-failed-two",
        step_id="prepare-second",
        state=DispatchStatus.ABANDONED,
        batch_id="batch-one",
    )
    sibling = _dispatch_in_state(
        dispatch_id="dispatch-sibling",
        step_id="accepted-sibling",
        state=DispatchStatus.FORWARDED,
        batch_id="batch-one",
    )
    dispatches = {item.dispatch_id: item for item in (failed_one, failed_two, sibling)}
    batch = _batch(tuple(dispatches), (failed_one.dispatch_id, failed_two.dispatch_id))
    record = record.model_copy(update={"dispatches": dispatches, "batches": {batch.batch_id: batch}})
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="batch_reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=batch.batch_id,
        ),
    )

    updated = _answer(store, waiting, "reconcile")

    assert updated.state is RunStatus.RUNNING
    assert updated.steps["prepare-fixture"].state is StepStatus.READY
    assert updated.steps["prepare-second"].state is StepStatus.READY
    assert updated.steps["accepted-sibling"].state is StepStatus.ACCEPTED
    assert updated.dispatches[sibling.dispatch_id].state is DispatchStatus.FORWARDED
    assert updated.batches[batch.batch_id] == batch


def test_batch_reconciliation_deduplicates_two_failed_dispatches_for_one_step(
    project: FixtureProject,
) -> None:
    record = _step_in_state(_record(project), "prepare-fixture", StepStatus.BLOCKED)
    dispatches = {
        dispatch_id: _dispatch_in_state(
            dispatch_id=dispatch_id,
            step_id="prepare-fixture",
            state=DispatchStatus.FAILED,
            batch_id="batch-one",
        )
        for dispatch_id in ("dispatch-failed-one", "dispatch-failed-two")
    }
    batch = _batch(tuple(dispatches), tuple(dispatches))
    record = record.model_copy(update={"dispatches": dispatches, "batches": {batch.batch_id: batch}})
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="batch_reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=batch.batch_id,
        ),
    )

    updated = _answer(store, waiting, "reconcile")

    assert updated.steps["prepare-fixture"].state is StepStatus.READY
    assert updated.steps["prepare-fixture"].last_event.sequence == updated.sequence


def test_batch_reconciliation_preserves_review_required_failed_child(
    project: FixtureProject,
) -> None:
    record = _step_in_state(_record(project), "prepare-fixture", StepStatus.REVIEW_REQUIRED)
    previous_event = record.steps["prepare-fixture"].last_event
    dispatch = _dispatch_in_state(
        dispatch_id="dispatch-review-failed",
        step_id="prepare-fixture",
        state=DispatchStatus.FAILED,
        batch_id="batch-one",
    )
    batch = _batch((dispatch.dispatch_id,), (dispatch.dispatch_id,))
    record = record.model_copy(
        update={"dispatches": {dispatch.dispatch_id: dispatch}, "batches": {batch.batch_id: batch}}
    )
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="batch_reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=batch.batch_id,
        ),
    )

    updated = _answer(store, waiting, "reconcile")

    assert updated.state is RunStatus.RUNNING
    assert updated.steps["prepare-fixture"].state is StepStatus.REVIEW_REQUIRED
    assert updated.steps["prepare-fixture"].last_event != previous_event


def test_batch_reconciliation_halt_preserves_children_and_failed_batch(
    project: FixtureProject,
) -> None:
    record = _step_in_state(_record(project), "prepare-fixture", StepStatus.BLOCKED)
    dispatch = _dispatch_in_state(
        dispatch_id="dispatch-failed",
        step_id="prepare-fixture",
        state=DispatchStatus.FAILED,
        batch_id="batch-one",
    )
    batch = _batch((dispatch.dispatch_id,), (dispatch.dispatch_id,))
    record = record.model_copy(
        update={"dispatches": {dispatch.dispatch_id: dispatch}, "batches": {batch.batch_id: batch}}
    )
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="batch_reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=batch.batch_id,
        ),
    )

    updated = _answer(store, waiting, "halt")

    assert updated.state is RunStatus.HALTED
    assert updated.steps["prepare-fixture"].state is StepStatus.BLOCKED
    assert updated.batches[batch.batch_id] == batch


def test_batch_reconciliation_rejects_unknown_batch_without_decision(project: FixtureProject) -> None:
    store, waiting = _persist_waiting(
        project,
        _record(project),
        _request(
            kind="batch_reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref="batch-missing",
        ),
    )

    with pytest.raises(StateStoreCorruptionError, match="known batch"):
        _answer(store, waiting, "reconcile")

    assert _operator_decision_count(store) == 0


def test_batch_reconciliation_rejects_nonfailed_batch_without_decision(
    project: FixtureProject,
) -> None:
    dispatch = _dispatch_in_state(
        dispatch_id="dispatch-failed",
        step_id="prepare-fixture",
        state=DispatchStatus.FAILED,
        batch_id="batch-one",
    )
    batch = _batch(
        (dispatch.dispatch_id,),
        (dispatch.dispatch_id,),
        state=BatchStatus.PREPARED,
    )
    record = _record(project).model_copy(
        update={"dispatches": {dispatch.dispatch_id: dispatch}, "batches": {batch.batch_id: batch}}
    )
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="batch_reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=batch.batch_id,
        ),
    )

    with pytest.raises(StateStoreError, match="failed batch"):
        _answer(store, waiting, "reconcile")

    assert _operator_decision_count(store) == 0


def test_batch_reconciliation_rejects_empty_failed_list_without_decision(
    project: FixtureProject,
) -> None:
    dispatch = _dispatch_in_state(
        dispatch_id="dispatch-failed",
        step_id="prepare-fixture",
        state=DispatchStatus.FAILED,
        batch_id="batch-one",
    )
    batch = _batch((dispatch.dispatch_id,), ())
    record = _record(project).model_copy(
        update={"dispatches": {dispatch.dispatch_id: dispatch}, "batches": {batch.batch_id: batch}}
    )
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="batch_reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=batch.batch_id,
        ),
    )

    with pytest.raises(StateStoreCorruptionError, match="failed dispatch IDs"):
        _answer(store, waiting, "reconcile")

    assert _operator_decision_count(store) == 0


def test_batch_reconciliation_rejects_foreign_dispatch_without_decision(
    project: FixtureProject,
) -> None:
    dispatch = _dispatch_in_state(
        dispatch_id="dispatch-foreign",
        step_id="prepare-fixture",
        state=DispatchStatus.FAILED,
        batch_id="batch-other",
    )
    batch = _batch((dispatch.dispatch_id,), (dispatch.dispatch_id,))
    record = _record(project).model_copy(
        update={"dispatches": {dispatch.dispatch_id: dispatch}, "batches": {batch.batch_id: batch}}
    )
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="batch_reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=batch.batch_id,
        ),
    )

    with pytest.raises(StateStoreCorruptionError, match="does not belong"):
        _answer(store, waiting, "reconcile")

    assert _operator_decision_count(store) == 0


def test_batch_reconciliation_rejects_unknown_failed_dispatch_without_decision(
    project: FixtureProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(
        kind="batch_reconciliation",
        allowed_answers=["reconcile", "halt"],
        context_ref="batch-one",
    )
    store, waiting = _persist_waiting(project, _record(project), request)
    corrupt_batch = BatchRecord.model_construct(
        batch_id="batch-one",
        dispatch_ids=("dispatch-missing",),
        state=BatchStatus.FAILED,
        failure_mode="wait_for_started",
        failed_dispatch_ids=("dispatch-missing",),
        last_event=_event(1),
    )
    corrupt_record = waiting.model_copy(update={"batches": {corrupt_batch.batch_id: corrupt_batch}})
    monkeypatch.setattr(store, "load_run", lambda _run_id: (corrupt_record, 1))

    with pytest.raises(StateStoreCorruptionError, match="unknown dispatch"):
        _answer(store, waiting, "reconcile")

    assert _operator_decision_count(store) == 0


def test_batch_reconciliation_rejects_nonterminal_failed_dispatch_without_decision(
    project: FixtureProject,
) -> None:
    record = _step_in_state(_record(project), "prepare-fixture", StepStatus.BLOCKED)
    dispatch = _dispatch_in_state(
        dispatch_id="dispatch-prepared",
        step_id="prepare-fixture",
        state=DispatchStatus.PREPARED,
        batch_id="batch-one",
    )
    batch = _batch((dispatch.dispatch_id,), (dispatch.dispatch_id,))
    record = record.model_copy(
        update={"dispatches": {dispatch.dispatch_id: dispatch}, "batches": {batch.batch_id: batch}}
    )
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="batch_reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=batch.batch_id,
        ),
    )

    with pytest.raises(StateStoreError, match="not failed or abandoned"):
        _answer(store, waiting, "reconcile")

    assert _operator_decision_count(store) == 0


def test_batch_reconciliation_rejects_incompatible_step_state_without_decision(
    project: FixtureProject,
) -> None:
    record = _step_in_state(_record(project), "prepare-fixture", StepStatus.READY)
    dispatch = _dispatch_in_state(
        dispatch_id="dispatch-failed",
        step_id="prepare-fixture",
        state=DispatchStatus.FAILED,
        batch_id="batch-one",
    )
    batch = _batch((dispatch.dispatch_id,), (dispatch.dispatch_id,))
    record = record.model_copy(
        update={"dispatches": {dispatch.dispatch_id: dispatch}, "batches": {batch.batch_id: batch}}
    )
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="batch_reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=batch.batch_id,
        ),
    )

    with pytest.raises(StateStoreError, match="incompatible state READY"):
        _answer(store, waiting, "reconcile")

    assert _operator_decision_count(store) == 0


def test_workspace_reconciliation_direct_answer_rejects_group_before_cleaned(
    project: FixtureProject,
) -> None:
    group = _workspace_group(project, state=WorkspaceGroupStatus.FAILED)
    record = _record(project).model_copy(update={"workspace_groups": {group.workspace_group_id: group}})
    store, waiting = _persist_waiting(
        project,
        record,
        _request(
            kind="workspace_reconciliation",
            allowed_answers=["reconcile", "halt"],
            context_ref=group.workspace_group_id,
        ),
    )

    with pytest.raises(StateStoreError, match="reach CLEANED"):
        _answer(store, waiting, "reconcile")

    assert _operator_decision_count(store) == 0
    loaded, generation = store.load_run(waiting.run_id)
    assert generation == 1
    assert loaded.state is RunStatus.WAITING_OPERATOR
    assert loaded.operator_request == waiting.operator_request


def test_operator_answer_rejects_stale_generation_without_decision(project: FixtureProject) -> None:
    store, waiting = _persist_waiting(
        project,
        _record(project),
        _request(kind="underspecification", allowed_answers=["answer", "halt"]),
    )

    with pytest.raises(StateStoreConflictError, match="generation conflict"):
        store.answer_operator_request(
            run_id=waiting.run_id,
            expected_generation=0,
            request_id=waiting.operator_request.request_id,
            answer="answer",
            actor_id="operator-fixture",
        )

    assert _operator_decision_count(store) == 0


def test_operator_answer_rejects_wrong_request_id_without_decision(project: FixtureProject) -> None:
    store, waiting = _persist_waiting(
        project,
        _record(project),
        _request(kind="underspecification", allowed_answers=["answer", "halt"]),
    )

    with pytest.raises(StateStoreError, match="request ID"):
        store.answer_operator_request(
            run_id=waiting.run_id,
            expected_generation=1,
            request_id="request-other",
            answer="answer",
            actor_id="operator-fixture",
        )

    assert _operator_decision_count(store) == 0


def test_unhandled_operator_request_kind_fails_loudly_without_decision(
    project: FixtureProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(kind="underspecification", allowed_answers=["answer", "halt"])
    store, waiting = _persist_waiting(project, _record(project), request)
    corrupt_request = request.model_copy(
        update={"kind": "future_request", "allowed_answers": ["continue"]}
    )
    corrupt_record = waiting.model_copy(update={"operator_request": corrupt_request})
    monkeypatch.setattr(store, "load_run", lambda _run_id: (corrupt_record, 1))

    with pytest.raises(StateStoreCorruptionError, match="no answer handling.*future_request"):
        store.answer_operator_request(
            run_id=waiting.run_id,
            expected_generation=1,
            request_id=request.request_id,
            answer="continue",
            actor_id="operator-fixture",
        )

    assert _operator_decision_count(store) == 0


def test_running_dispatch_never_allows_automatic_retry(project: FixtureProject) -> None:
    store = _store(project)
    record = _record(project)
    prompt = "perform fixture task"
    policy: dict[str, object] = {"permission": {"*": "deny"}}
    dispatch = _dispatch(prompt, policy)
    running = transition_dispatch(
        dispatch,
        DispatchStatus.RUNNING,
        _event(2),
        runtime_session_id="ses-fixture",
        process_create_time=1234.5,
    )
    record = record.model_copy(update={"dispatches": {running.dispatch_id: running}})
    store.create_run(record)

    item = store.classify_recovery(record.run_id)[0]
    assert item.disposition == "operator_reconciliation_required"


def test_correlated_audit_export_and_tool_versions_are_derived_from_sqlite(
    project: FixtureProject,
) -> None:
    store = _store(project)
    record = _record(project)
    store.create_run(record)
    store.record_tool_version(run_id=record.run_id, tool_name="opencode", version="1.18.11")
    store.append_audit_event(
        run_id=record.run_id,
        event_id="event-audit-one",
        sequence=1,
        kind="run_created",
        correlation_id="fixture-correlation",
        causation_id=None,
        payload={"config_digest": record.config_digest},
    )

    export = store.export_audit_jsonl(record.run_id)

    assert json.loads(export.read_text(encoding="utf-8"))["kind"] == "run_created"
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT version FROM tool_versions WHERE run_id = ? AND tool_name = 'opencode'",
            (record.run_id,),
        ).fetchone()[0] == "1.18.11"
