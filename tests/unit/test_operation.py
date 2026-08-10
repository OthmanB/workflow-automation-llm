from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import config_values, create_fixture_project, valid_plan_values, write_config

from dispatcher.operation import RealOperationError, validate_real_operation_prerequisites
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.state_store import StateStore
from dispatcher.workflow import TransitionEvent, new_run_record


def _record(project, plan: NormalizedPlan):
    return new_run_record(
        run_id="real-operation-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-real-operation-plan"),
        event=TransitionEvent(
            event_id="event-real-operation-run",
            sequence=1,
            actor="operator",
            reason="real operation fixture",
            correlation_id="real-operation-run",
            occurred_at=datetime.now(UTC),
        ),
    )


def test_real_operation_rejects_public_mock_mode_before_other_checks(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = _record(project, plan)
    store = StateStore(project.state, heartbeat_seconds=30, stale_after_seconds=120)

    with pytest.raises(RealOperationError, match="real_operation"):
        validate_real_operation_prerequisites(
            config=project.config,
            store=store,
            record=record,
            plan_path=project.plans / "plan.md",
            repo_id="fixture-repo",
            smoke_proof_path=project.root / "smoke.json",
            smoke_model="fixture/executor",
            permission_digest="0" * 64,
            stall_policy_digest="0" * 64,
            approval_ref="decision-real-operation",
            confirm=True,
        )


def test_real_operation_requires_explicit_confirmation_and_schema_v2(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["execution"]["mode"] = "real_operation"
    config = write_config(project, values)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = _record(project, plan).model_copy(update={"config_digest": config.config_digest})
    store = StateStore(config.state_dir, heartbeat_seconds=30, stale_after_seconds=120)

    with pytest.raises(RealOperationError, match="confirm-real-operation"):
        validate_real_operation_prerequisites(
            config=config,
            store=store,
            record=record,
            plan_path=project.plans / "plan.md",
            repo_id="fixture-repo",
            smoke_proof_path=project.root / "smoke.json",
            smoke_model="fixture/executor",
            permission_digest="0" * 64,
            stall_policy_digest="0" * 64,
            approval_ref="decision-real-operation",
            confirm=False,
        )
