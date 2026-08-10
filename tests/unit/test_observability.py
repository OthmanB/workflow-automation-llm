from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from helpers import (
    FixtureProject,
    config_values,
    create_fixture_project,
    valid_plan_values,
    write_config,
)

from dispatcher.observability import (
    JsonFormatter,
    export_support_bundle,
    prune_derived_artifacts,
    status_snapshot,
)
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.state_store import StateStore
from dispatcher.workflow import (
    OperatorRequest,
    RunStatus,
    TransitionEvent,
    new_run_record,
    transition_run,
)


def _event(sequence: int) -> TransitionEvent:
    return TransitionEvent(
        event_id=f"event-observability-{sequence}",
        sequence=sequence,
        actor="dispatcher",
        reason="observability fixture",
        correlation_id="observability-run",
        occurred_at=datetime.now(UTC),
    )


def _record(project: FixtureProject):
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    return new_run_record(
        run_id="observability-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-observability"),
        event=_event(1),
    )


def _store(project: FixtureProject) -> StateStore:
    return StateStore(
        project.state,
        heartbeat_seconds=project.config.lease_heartbeat_seconds,
        stale_after_seconds=project.config.lease_stale_after_seconds,
    )


def test_status_snapshot_contains_correlation_and_scheduler_reasons(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    store = _store(project)
    record = _record(project)
    store.create_run(record)
    store.acquire_run_lease(project_id=record.project_id, run_id=record.run_id, owner_id="owner-observability")

    snapshot = status_snapshot(project.config, store, record.run_id)

    assert snapshot["run"] == {
        "run_id": record.run_id,
        "state": RunStatus.NEW.value,
        "generation": 1,
        "terminal_outcome": None,
    }
    assert snapshot["ready_steps"] == []
    assert snapshot["blocked_steps"] == [{"step_id": "prepare-fixture", "reasons": ["state is PENDING"]}]
    assert snapshot["leases"][0]["run_id"] == record.run_id


def test_support_bundle_redacts_operator_question_and_omits_authoritative_database(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    store = _store(project)
    record = transition_run(_record(project), RunStatus.READY, _event(2))
    request = OperatorRequest(
        request_id="request-observability",
        question="Approve credential=super-secret-value",
        allowed_answers=["approve"],
        context_ref="fixture",
        resume_to=RunStatus.READY,
        expires_at=None,
        required_role=None,
    )
    waiting = transition_run(record, RunStatus.WAITING_OPERATOR, _event(3), operator_request=request)
    store.create_run(waiting)

    bundle = export_support_bundle(project.config, store, waiting.run_id)

    assert {path.name for path in bundle.iterdir()} == {"audit.jsonl", "manifest.json", "report.md", "status.json"}
    combined = "\n".join(path.read_text(encoding="utf-8") for path in bundle.iterdir())
    assert "super-secret-value" not in combined
    assert "[REDACTED]" in combined
    assert not (bundle / "dispatcher.sqlite3").exists()


def test_retention_archives_only_derived_files_and_preserves_run_state(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    store = _store(project)
    record = _record(project)
    store.create_run(record)
    reports = project.state / "reports"
    reports.mkdir(parents=True)
    old = reports / "old.md"
    new = reports / "new.md"
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    values = config_values(project)
    values["observability"]["retention"]["max_reports"] = 1
    config = write_config(project, values)

    actions = prune_derived_artifacts(config, store)

    assert len(actions) == 1
    assert actions[0].action == "archived"
    assert new.exists()
    assert not old.exists()
    assert (Path(config.observability.retention.archive_directory) / "reports" / "old.md").exists()
    assert store.load_run(record.run_id)[0] == record


def test_json_formatter_redacts_messages_and_includes_context() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="dispatcher.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="token=top-secret",
        args=(),
        exc_info=None,
    )
    record.dispatcher_context = {"project_id": "fixture", "run_id": "run-one"}  # type: ignore[attr-defined]

    payload = json.loads(formatter.format(record))

    assert payload["project_id"] == "fixture"
    assert payload["run_id"] == "run-one"
    assert payload["dispatch_id"] is None
    assert "top-secret" not in payload["message"]
