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
    StaleLeaseRecoveryRequired,
    StateStore,
    StateStoreConflictError,
    StateStoreCorruptionError,
    StateStoreError,
    StateStoreMigrationError,
)
from dispatcher.workflow import (
    DispatchIntent,
    DispatchRecord,
    DispatchStatus,
    OperatorRequest,
    RepositoryCoordinate,
    RunStatus,
    TransitionEvent,
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
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
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


def test_phase_five_manifest_columns_migrate_existing_phase_four_database(
    project: FixtureProject,
) -> None:
    store = _store(project)
    store.initialize()
    store.close()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("ALTER TABLE dispatch_payloads DROP COLUMN repository_before_json")
        connection.execute("ALTER TABLE dispatch_payloads DROP COLUMN repository_after_json")
        connection.execute("DELETE FROM schema_migrations WHERE version = 3")

    migrated = _store(project)
    migrated.initialize()
    with sqlite3.connect(migrated.database_path) as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'baselines'"
        ).fetchone()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(dispatch_payloads)").fetchall()}

    assert version == 3
    assert table == ("baselines",)
    assert columns >= {"repository_before_json", "repository_after_json"}


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
    assert store.classify_recovery(record.run_id)[0].disposition == (
        "operator_reconciliation_required"
    )

    running = transition_dispatch(
        dispatch,
        DispatchStatus.RUNNING,
        _event(2),
        runtime_session_id="ses-fixture",
    )
    running_record = prepared_record.model_copy(update={"dispatches": {running.dispatch_id: running}})
    generation = store.save_run(running_record, expected_generation=generation)
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
    assert store.classify_recovery(record.run_id)[0].disposition == "forwarding_required"

    forwarded = transition_dispatch(
        completed,
        DispatchStatus.FORWARDED,
        _event(4),
        forwarding_digest=_DIGEST,
    )
    forwarded_record = completed_record.model_copy(update={"dispatches": {forwarded.dispatch_id: forwarded}})
    store.save_run(forwarded_record, expected_generation=generation)
    assert store.classify_recovery(record.run_id)[0].disposition == "acknowledgement_required"


def test_operator_answer_and_transcripts_are_durable_and_collision_free(project: FixtureProject) -> None:
    store = _store(project)
    record = transition_run(_record(project), RunStatus.READY, _event(2))
    request = OperatorRequest(
        request_id="request-one",
        question="Choose a safe option",
        allowed_answers=["approve", "halt"],
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
        answer="approve",
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
    assert answered.state is RunStatus.READY
    assert first != second
    assert "secret-token" not in first.read_text(encoding="utf-8")
    assert store.export_run_report(waiting.run_id).is_file()
    with pytest.raises(StateStoreError, match="not waiting"):
        store.answer_operator_request(
            run_id=waiting.run_id,
            expected_generation=generation,
            request_id=request.request_id,
            answer="approve",
            actor_id="operator-fixture",
        )


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
