from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import FixtureProject, create_fixture_project, valid_plan_values

from dispatcher.baseline import (
    BaselineDecision,
    BaselineError,
    approve_baseline,
    hydrate_run_from_baseline,
    inspect_baseline,
    validate_approved_baseline,
)
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.state_store import StateStore
from dispatcher.workflow import RunStatus, StepStatus, TransitionEvent, new_run_record


@pytest.fixture
def project(tmp_path: Path) -> FixtureProject:
    return create_fixture_project(tmp_path)


def _store(project: FixtureProject) -> StateStore:
    return StateStore(
        project.state,
        heartbeat_seconds=project.config.lease_heartbeat_seconds,
        stale_after_seconds=project.config.lease_stale_after_seconds,
    )


def _decision(step_id: str, state: str, *, reviewers: tuple[str, ...] = ()) -> BaselineDecision:
    return BaselineDecision(
        step_id=step_id,
        state=state,  # type: ignore[arg-type]
        reason=f"explicit historical disposition: {state}",
        operator_decision_ref=f"decision-{step_id}-{state.lower()}",
        accepted_reviewer_role_keys=reviewers,
    )


def test_baseline_observation_never_infers_acceptance_and_tampering_invalidates_approval(
    project: FixtureProject,
) -> None:
    evidence = project.evidence / "fixture.md"
    evidence.write_text("historical fixture evidence\n", encoding="utf-8")
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    store = _store(project)

    observation = inspect_baseline(plan, project.config)

    assert observation.steps[0].evidence[0].artifact_id == "fixture-evidence"
    assert "operator must explicitly decide" in observation.steps[0].gaps[0]
    approval = approve_baseline(
        observation,
        decisions=(_decision("prepare-fixture", "ACCEPTED"),),
        plan=plan,
        config=project.config,
        store=store,
        approval_decision_ref="decision-approve-baseline",
    )
    assert validate_approved_baseline(plan=plan, config=project.config, store=store) == approval

    evidence.write_text("changed historical fixture evidence\n", encoding="utf-8")
    with pytest.raises(BaselineError, match="historical evidence changed"):
        validate_approved_baseline(plan=plan, config=project.config, store=store)
    renewed = approve_baseline(
        inspect_baseline(plan, project.config),
        decisions=(_decision("prepare-fixture", "ACCEPTED"),),
        plan=plan,
        config=project.config,
        store=store,
        approval_decision_ref="decision-renewed-baseline",
    )
    assert renewed.approval_digest != approval.approval_digest
    assert validate_approved_baseline(plan=plan, config=project.config, store=store) == renewed


def test_accepted_baseline_requires_review_proof_and_hydrates_new_run(project: FixtureProject) -> None:
    values = valid_plan_values(project)
    values["steps"][0]["review"] = {
        "required": True,
        "reviewer_role_keys": ["reviewer"],
        "required_acceptances": 1,
    }
    values["steps"][0]["retry"]["max_reviewer_attempts"] = 1
    plan = NormalizedPlan.model_validate(values)
    (project.evidence / "fixture.md").write_text("historical fixture evidence\n", encoding="utf-8")
    store = _store(project)
    observation = inspect_baseline(plan, project.config)

    with pytest.raises(BaselineError, match="lacks required review proof"):
        approve_baseline(
            observation,
            decisions=(_decision("prepare-fixture", "ACCEPTED", reviewers=("reviewer",)),),
            plan=plan,
            config=project.config,
            store=store,
            approval_decision_ref="decision-missing-review",
        )

    review_proof = project.evidence / "prepare-fixture-kimi-review.md"
    review_proof.write_text("historical review proof\n", encoding="utf-8")
    observation = inspect_baseline(plan, project.config)
    approval = approve_baseline(
        observation,
        decisions=(_decision("prepare-fixture", "ACCEPTED", reviewers=("reviewer",)),),
        plan=plan,
        config=project.config,
        store=store,
        approval_decision_ref="decision-reviewed-acceptance",
    )
    record = new_run_record(
        run_id="baseline-hydration-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-plan"),
        event=TransitionEvent(
            event_id="event-baseline-hydration",
            sequence=1,
            actor="dispatcher",
            reason="baseline hydration fixture",
            correlation_id="baseline-hydration-run",
            occurred_at=datetime.now(UTC),
        ),
    )

    hydrated = hydrate_run_from_baseline(record, approval)

    step = hydrated.steps["prepare-fixture"]
    assert hydrated.state is RunStatus.NEW
    assert step.state is StepStatus.ACCEPTED
    assert step.accepted_artifact_ids == ["fixture-evidence"]
    assert step.accepted_reviewer_role_keys == ["reviewer"]


def test_every_historical_step_requires_explicit_pending_or_waived_decision(project: FixtureProject) -> None:
    values = valid_plan_values(project)
    second = json.loads(json.dumps(values["steps"][0]))
    second.update(
        {
            "ordinal": 2,
            "step_id": "prepare-second",
            "title": "Prepare second",
            "produced_outputs": [
                {
                    "artifact_id": "second-output",
                    "producer_step_id": None,
                    "description": "Second output",
                }
            ],
            "resource_locks": [{"resource_id": "second-resource", "mode": "write"}],
            "evidence_requirements": [
                {
                    "artifact_id": "second-evidence",
                    "relative_path": "second.md",
                    "media_type": "text/markdown",
                }
            ],
        }
    )
    values["steps"].append(second)
    plan = NormalizedPlan.model_validate(values)
    store = _store(project)
    observation = inspect_baseline(plan, project.config)

    with pytest.raises(ValueError, match="exactly one decision"):
        approve_baseline(
            observation,
            decisions=(_decision("prepare-fixture", "PENDING"),),
            plan=plan,
            config=project.config,
            store=store,
            approval_decision_ref="decision-incomplete-baseline",
        )

    approval = approve_baseline(
        observation,
        decisions=(
            _decision("prepare-fixture", "PENDING"),
            _decision("prepare-second", "WAIVED"),
        ),
        plan=plan,
        config=project.config,
        store=store,
        approval_decision_ref="decision-complete-baseline",
    )
    record = new_run_record(
        run_id="baseline-mixed-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-mixed-plan"),
        event=TransitionEvent(
            event_id="event-baseline-mixed",
            sequence=1,
            actor="dispatcher",
            reason="baseline mixed fixture",
            correlation_id="baseline-mixed-run",
            occurred_at=datetime.now(UTC),
        ),
    )

    hydrated = hydrate_run_from_baseline(record, approval)

    assert hydrated.steps["prepare-fixture"].state is StepStatus.PENDING
    assert hydrated.steps["prepare-second"].state is StepStatus.WAIVED
