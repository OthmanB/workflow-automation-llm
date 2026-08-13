from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import yaml
from helpers import create_fixture_project, valid_plan_values

from dispatcher import sessions
from dispatcher.config import load_config
from dispatcher.execution import SequentialExecutionCoordinator
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.sequential import SequentialWorkflow
from dispatcher.state import open_state_store
from dispatcher.workflow import RunStatus, TransitionEvent, new_run_record


def test_fake_opencode_executes_narrated_rework_review_and_completion_in_disposable_git(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = create_fixture_project(tmp_path)
    _commit_initial_fixture(project.repository)
    plan_values = valid_plan_values(project)
    step = plan_values["steps"][0]
    step["authorization"] = {
        "authorized_actions": ["inspect", "modify", "verify", "commit"],
        "writable_paths": ["evidence/", "src/value.txt"],
        "requires_operator_approval": False,
    }
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
    config_values = yaml.safe_load(project.config_path.read_text(encoding="utf-8"))
    config_values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    config_values["permission_policies"]["policies"]["executor-class"]["actions"]["commit"] = "allow"
    project.config_path.write_text(yaml.safe_dump(config_values, sort_keys=False), encoding="utf-8")
    project = replace(project, config=load_config(project.config_path))
    plan = NormalizedPlan.model_validate(plan_values)
    event = TransitionEvent(
        event_id="event-integration-start",
        sequence=1,
        actor="dispatcher",
        reason="integration fixture created",
        correlation_id="integration-run",
        occurred_at=datetime.now(UTC),
    )
    record = new_run_record(
        run_id="integration-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-integration-plan"),
        event=event,
    )
    store = open_state_store(project.config)
    generation = store.create_run(record)
    fake_opencode = _install_fake_opencode(tmp_path)
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(fake_opencode))
    workflow = SequentialWorkflow(project.config, store, owner_id="integration-owner")
    coordinator = SequentialExecutionCoordinator(
        project.config,
        store,
        workflow,
        owner_id="integration-owner",
    )

    decision = coordinator.run_to_completion(
        record.run_id,
        expected_generation=generation,
    )

    final_record, final_generation = store.load_run(record.run_id)
    final_step = final_record.steps["prepare-fixture"]
    dispatches = sorted(final_record.dispatches.values(), key=lambda item: item.last_event.sequence)
    executor_dispatches = [item for item in dispatches if item.role_kind == "executor"]
    reviewer_dispatches = [item for item in dispatches if item.role_kind == "reviewer"]
    assert decision.accepted
    assert final_record.state is RunStatus.SUCCEEDED
    assert final_generation > generation
    assert final_step.state.value == "ACCEPTED"
    assert final_step.executor_attempts == 2
    assert final_step.reviewer_attempts == 2
    assert final_step.review_acceptances == 1
    assert all(item.state.value == "ACKNOWLEDGED" for item in dispatches)
    assert len(executor_dispatches) == 2
    assert executor_dispatches[0].runtime_session_id == executor_dispatches[1].runtime_session_id
    assert executor_dispatches[0].logical_session_key == executor_dispatches[1].logical_session_key
    assert len(reviewer_dispatches) == 2
    assert reviewer_dispatches[0].runtime_session_id != reviewer_dispatches[1].runtime_session_id
    assert _git(project.repository, "rev-list", "--count", "HEAD") == "3"
    assert _git(project.repository, "status", "--porcelain") == ""
    assert (project.repository / "src" / "value.txt").read_text(encoding="utf-8") == "value=2\n"
    assert not (project.repository / "reviewer-write.txt").exists()

    calls = [
        json.loads(line)
        for line in (fake_opencode.parent / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    worker_calls = [call for call in calls if call["role"] != "supervisor"]
    executor_calls = [call for call in worker_calls if call["role"] == "executor"]
    reviewer_calls = [call for call in worker_calls if call["role"] == "reviewer"]
    assert len(calls) == 9
    assert executor_calls[0]["requested_session"] is None
    assert executor_calls[1]["requested_session"] == executor_calls[0]["session_id"]
    assert executor_calls[0]["child_environment"] == executor_calls[1]["child_environment"]
    assert executor_calls[0]["child_environment"] != reviewer_calls[0]["child_environment"]
    assert executor_calls[0]["child_environment"]["HOME"] == str(
        Path(project.config.state_dir)
        / "opencode-dispatches"
        / record.run_id
        / "executors"
        / "terra"
        / "opencode-child"
        / "home"
    )
    assert reviewer_calls[0]["child_environment"]["HOME"] == str(
        Path(project.config.state_dir)
        / "opencode-dispatches"
        / record.run_id
        / "reviewers"
        / "reviewer"
        / "opencode-child"
        / "home"
    )
    assert all(call["policy"]["permission"]["edit"] == "allow" for call in executor_calls)
    assert all(call["policy"]["permission"]["write"] == "deny" for call in reviewer_calls)
    assert all(call["head_before"] == call["head_after"] for call in reviewer_calls)

    final_evidence = (project.repository / "evidence" / "fixture.md").read_bytes()
    final_evidence_sha = hashlib.sha256(final_evidence).hexdigest()
    assert decision.report_path is not None
    report = decision.report_path.read_text(encoding="utf-8")
    assert "State: `SUCCEEDED`" in report
    assert final_evidence_sha in report
    assert "changes_requested" in report
    assert "accepted" in report
    with sqlite3.connect(store.database_path) as connection:
        connection.row_factory = sqlite3.Row
        audit = connection.execute(
            "SELECT kind FROM audit_events WHERE run_id = ?", (record.run_id,)
        ).fetchall()
        lease_count = connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
    assert [row["kind"] for row in audit] == ["run_succeeded"]
    assert lease_count == 0


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
