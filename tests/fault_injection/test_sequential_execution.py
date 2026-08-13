from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import (
    FixtureProject,
    config_values,
    create_fixture_project,
    valid_plan_values,
    write_config,
)

from dispatcher import sessions
from dispatcher.execution import SequentialExecutionCoordinator
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.sequential import PreparedDispatch, SequentialWorkflow
from dispatcher.sessions import OpenCodeProcessError, OpenCodeProtocolError, OpenCodeTimeoutError
from dispatcher.state import open_state_store
from dispatcher.state_store import StateStore
from dispatcher.workflow import RunRecord, RunStatus, TransitionEvent, new_run_record


@dataclass(frozen=True)
class FaultFixture:
    project: FixtureProject
    store: StateStore
    workflow: SequentialWorkflow
    coordinator: SequentialExecutionCoordinator
    record: RunRecord
    generation: int
    fake_opencode: Path


@pytest.mark.parametrize(
    ("fault", "error_type", "expected_category"),
    [
        ("nonzero", OpenCodeProcessError, "unknown"),
        ("malformed_jsonl", OpenCodeProtocolError, "protocol"),
        ("timeout", OpenCodeTimeoutError, "timeout"),
    ],
)
def test_worker_process_failures_never_advance_the_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    error_type: type[Exception],
    expected_category: str,
) -> None:
    fixture = _create_fault_fixture(tmp_path, monkeypatch, timeout_seconds=1)
    prepared = _prepare_executor(fixture)
    _inject_fault(fixture.fake_opencode, fault)

    with pytest.raises(error_type):
        fixture.coordinator.execute_worker(prepared)

    failed, _generation = fixture.store.load_run(fixture.record.run_id)
    dispatch = failed.dispatches[prepared.dispatch.dispatch_id]
    assert failed.state is RunStatus.WAITING_OPERATOR
    assert failed.operator_request is not None
    assert failed.operator_request.context_ref == dispatch.dispatch_id
    assert failed.steps["prepare-fixture"].state.value == "BLOCKED"
    assert dispatch.state.value == "FAILED"
    assert dispatch.failure_category == expected_category
    assert dispatch.failure_detail
    assert _git(fixture.project.repository, "rev-list", "--count", "HEAD") == "1"
    fixture.coordinator.release_run()


def test_repository_write_followed_by_nonzero_exit_requires_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _create_fault_fixture(tmp_path, monkeypatch)
    prepared = _prepare_executor(fixture)
    _inject_fault(fixture.fake_opencode, "write_nonzero")

    with pytest.raises(OpenCodeProcessError):
        fixture.coordinator.execute_worker(prepared)

    failed, _generation = fixture.store.load_run(fixture.record.run_id)
    assert failed.state is RunStatus.WAITING_OPERATOR
    assert failed.steps["prepare-fixture"].state.value == "BLOCKED"
    assert _git(fixture.project.repository, "rev-list", "--count", "HEAD") == "1"
    assert _git(fixture.project.repository, "status", "--porcelain") != ""
    fixture.coordinator.release_run()


def test_timeout_uses_one_bounded_cooldown_retry_then_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _create_fault_fixture(
        tmp_path,
        monkeypatch,
        timeout_seconds=1,
        max_executor_attempts=2,
        stall_retries=1,
        cooldown_seconds=3,
    )
    prepared = _prepare_executor(fixture)
    _inject_fault(fixture.fake_opencode, "timeout")
    sleeps: list[int] = []
    monkeypatch.setattr("dispatcher.execution.sleep", lambda seconds: sleeps.append(seconds))

    outcome = fixture.coordinator.execute_worker(prepared)

    assert outcome.record.steps["prepare-fixture"].state.value == "ACCEPTED"
    assert outcome.record.steps["prepare-fixture"].stalls == 1
    assert sleeps == [3]
    first_attempt = outcome.record.dispatches[prepared.dispatch.dispatch_id]
    assert first_attempt.state.value == "FAILED"
    assert first_attempt.failure_category == "timeout"
    assert first_attempt.failure_detail
    assert len(
        [
            dispatch
            for dispatch in outcome.record.dispatches.values()
            if dispatch.failure_category is not None
        ]
    ) == 1
    fixture.coordinator.release_run()


def test_report_failure_does_not_commit_success_or_terminal_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _create_fault_fixture(tmp_path, monkeypatch, review_required=True)

    def fail_report(*_args, **_kwargs):
        raise OSError("injected report failure")

    monkeypatch.setattr(fixture.store, "export_run_report", fail_report)
    with pytest.raises(OSError, match="injected report failure"):
        fixture.coordinator.run_to_completion(
            fixture.record.run_id,
            expected_generation=fixture.generation,
        )

    record, _generation = fixture.store.load_run(fixture.record.run_id)
    assert record.state is RunStatus.RUNNING
    assert record.steps["prepare-fixture"].state.value == "ACCEPTED"
    with sqlite3.connect(fixture.store.database_path) as connection:
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE run_id = ?",
            (fixture.record.run_id,),
        ).fetchone()[0]
    assert audit_count == 0


def _create_fault_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    timeout_seconds: int = 5,
    review_required: bool = False,
    max_executor_attempts: int | None = None,
    stall_retries: int | None = None,
    cooldown_seconds: int | None = None,
) -> FaultFixture:
    project = create_fixture_project(tmp_path)
    if timeout_seconds != project.config.execution.timeout_seconds:
        values = config_values(project)
        values["execution"]["timeout_seconds"] = timeout_seconds
        project = replace(project, config=write_config(project, values))
    if max_executor_attempts is not None or stall_retries is not None or cooldown_seconds is not None:
        values = config_values(project)
        if stall_retries is not None:
            values["execution"]["stall_policy"]["maximum_retries_per_step"] = stall_retries
        if cooldown_seconds is not None:
            values["execution"]["stall_policy"]["cooldown_seconds"] = cooldown_seconds
        project = replace(project, config=write_config(project, values))
    _commit_initial_fixture(project.repository)
    plan_values = valid_plan_values(project)
    step = plan_values["steps"][0]
    step["authorization"] = {
        "authorized_actions": ["inspect", "modify", "verify", "commit"],
        "writable_paths": ["evidence/", "src/value.txt"],
        "requires_operator_approval": False,
    }
    values = config_values(project)
    values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    values["permission_policies"]["policies"]["executor-class"]["actions"]["commit"] = "allow"
    project = replace(project, config=write_config(project, values))
    if max_executor_attempts is not None:
        step["retry"]["max_executor_attempts"] = max_executor_attempts
    if review_required:
        step["review"] = {
            "required": True,
            "reviewer_role_keys": ["reviewer"],
            "required_acceptances": 1,
        }
        step["retry"] = {
            "max_executor_attempts": 2,
            "max_reviewer_attempts": 2,
            "on_failed": "halt",
            "on_blocked": "halt",
            "on_changes_requested": "retry",
            "escalation_role_key": None,
        }
    plan = NormalizedPlan.model_validate(plan_values)
    event = TransitionEvent(
        event_id="event-fault-start",
        sequence=1,
        actor="dispatcher",
        reason="fault fixture created",
        correlation_id="fault-run",
        occurred_at=datetime.now(UTC),
    )
    record = new_run_record(
        run_id="fault-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-fault-plan"),
        event=event,
    )
    store = open_state_store(project.config)
    generation = store.create_run(record)
    fake_opencode = _install_fake_opencode(tmp_path)
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(fake_opencode))
    workflow = SequentialWorkflow(project.config, store, owner_id="fault-owner")
    coordinator = SequentialExecutionCoordinator(
        project.config,
        store,
        workflow,
        owner_id="fault-owner",
    )
    return FaultFixture(
        project,
        store,
        workflow,
        coordinator,
        record,
        generation,
        fake_opencode,
    )


def _prepare_executor(fixture: FaultFixture) -> PreparedDispatch:
    fixture.coordinator.acquire_run(fixture.record.run_id)
    record, generation = fixture.workflow.activate(
        fixture.record.run_id,
        expected_generation=fixture.generation,
    )
    command = json.dumps(
        {
            "protocol_version": 1,
            "action": "dispatch",
            "step_id": "prepare-fixture",
            "target_role": "terra",
            "session_mode": "new",
            "prompt": "Perform the approved fixture work.",
            "rationale": "fault fixture",
        }
    )
    prepared = fixture.workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=command,
    )
    assert isinstance(prepared, PreparedDispatch)
    return prepared


def _inject_fault(fake_opencode: Path, fault: str) -> None:
    (fake_opencode.parent / "fault.json").write_text(
        json.dumps({"next": fault}),
        encoding="utf-8",
    )


def _install_fake_opencode(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1] / "fixtures" / "opencode" / "fake_cli.py"
    target_dir = tmp_path / "fake-opencode"
    target_dir.mkdir()
    target = target_dir / "opencode"
    shutil.copy2(source, target)
    target.chmod(0o700)
    return target


def _commit_initial_fixture(repository: Path) -> None:
    value_path = repository / "src" / "value.txt"
    evidence_path = repository / "evidence" / "fixture.md"
    value_path.parent.mkdir(parents=True, exist_ok=True)
    value_path.write_text("value=0\n", encoding="utf-8")
    evidence_path.write_text("initial fixture evidence\n", encoding="utf-8")
    _git(repository, "config", "user.name", "Fixture Initializer")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "branch", "-M", "main")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial fixture")


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()
