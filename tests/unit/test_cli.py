from __future__ import annotations

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


def test_baseline_cli_inspect_and_approve_are_read_only_until_approval(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    (project.evidence / "fixture.md").write_text("fixture evidence\n", encoding="utf-8")
    plan_path = tmp_path / "plan.yaml"
    candidate_path = tmp_path / "baseline.json"
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
                str(candidate_path),
            ]
        )
        == 0
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
                "--candidate",
                str(candidate_path),
                "--operator-decision-ref",
                "decision-approve-baseline",
            ]
        )
        == 0
    )
