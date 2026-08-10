from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from helpers import create_fixture_project, valid_plan_values

from dispatcher.cli import main
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.workflow import TransitionEvent, new_run_record


def test_real_run_is_blocked_before_config_loading(tmp_path) -> None:
    missing_config = tmp_path / "does-not-exist.yaml"

    result = main(["run", "--config", str(missing_config)])

    assert result == 2


def test_start_and_resume_use_explicit_sqlite_run_identity(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    event = TransitionEvent(
        event_id="event-start",
        sequence=1,
        actor="dispatcher",
        reason="fixture start",
        correlation_id="fixture-correlation",
        occurred_at=datetime.now(UTC),
    )
    record = new_run_record(
        run_id="fixture-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-approve-plan"),
        event=event,
    )
    record_path = tmp_path / "run-record.json"
    record_path.write_text(record.model_dump_json(), encoding="utf-8")

    assert main(["start", "--config", str(project.config_path), "--run-record", str(record_path)]) == 0
    assert main(["resume", "--config", str(project.config_path), "--run-id", record.run_id]) == 0
    assert main(["start", "--config", str(project.config_path), "--run-record", str(record_path)]) == 2


def test_status_json_and_support_export_are_derived_from_authoritative_state(tmp_path: Path, capsys) -> None:
    project = create_fixture_project(tmp_path)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = new_run_record(
        run_id="status-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-status"),
        event=TransitionEvent(
            event_id="event-status",
            sequence=1,
            actor="dispatcher",
            reason="fixture status",
            correlation_id="status-run",
            occurred_at=datetime.now(UTC),
        ),
    )
    record_path = tmp_path / "status-run.json"
    record_path.write_text(record.model_dump_json(), encoding="utf-8")
    assert main(["start", "--config", str(project.config_path), "--run-record", str(record_path)]) == 0
    capsys.readouterr()

    assert main(["status", "--config", str(project.config_path), "--run-id", record.run_id, "--format", "json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["run"]["run_id"] == record.run_id
    assert status["blocked_steps"][0]["reasons"] == ["state is PENDING"]

    assert main(["support", "--config", str(project.config_path), "--run-id", record.run_id]) == 0
    assert "support: exported" in capsys.readouterr().out
    assert main(["prune", "--config", str(project.config_path)]) == 2


def test_baseline_cli_inspect_and_approve_are_read_only_until_approval(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    (project.evidence / "fixture.md").write_text("fixture evidence\n", encoding="utf-8")
    plan_path = tmp_path / "plan.yaml"
    observation_path = tmp_path / "baseline-observation.json"
    decisions_path = tmp_path / "baseline-decisions.json"
    plan_path.write_text(yaml.safe_dump(valid_plan_values(project), sort_keys=False), encoding="utf-8")

    assert (
        main(
            [
                "baseline",
                "inspect",
                "--config",
                str(project.config_path),
                "--plan",
                str(plan_path),
                "--output",
                str(observation_path),
            ]
        )
        == 0
    )
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    decisions_path.write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "step_id": step["step_id"],
                        "state": "ACCEPTED",
                        "reason": "fixture evidence is present",
                        "operator_decision_ref": "decision-step-accept",
                    }
                    for step in observation["steps"]
                ]
            }
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "baseline",
                "approve",
                "--config",
                str(project.config_path),
                "--plan",
                str(plan_path),
                "--observation",
                str(observation_path),
                "--decisions",
                str(decisions_path),
                "--approval-decision-ref",
                "decision-approve-baseline",
            ]
        )
        == 0
    )
    baseline_plan = NormalizedPlan.model_validate(valid_plan_values(project))
    baseline_record = new_run_record(
        run_id="baseline-start-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=baseline_plan,
        plan_approval=approve_plan(baseline_plan, "decision-baseline-start"),
        event=TransitionEvent(
            event_id="event-baseline-start",
            sequence=1,
            actor="dispatcher",
            reason="baseline start fixture",
            correlation_id="baseline-start-run",
            occurred_at=datetime.now(UTC),
        ),
    )
    record_path = tmp_path / "baseline-start-run.json"
    record_path.write_text(baseline_record.model_dump_json(), encoding="utf-8")

    assert (
        main(
            [
                "start",
                "--config",
                str(project.config_path),
                "--run-record",
                str(record_path),
                "--use-approved-baseline",
            ]
        )
        == 0
    )
