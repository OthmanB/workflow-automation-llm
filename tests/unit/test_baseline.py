from __future__ import annotations

from pathlib import Path

import pytest
from helpers import FixtureProject, create_fixture_project, valid_plan_values

from dispatcher.baseline import (
    BaselineError,
    approve_baseline,
    inspect_baseline,
    validate_approved_baseline,
)
from dispatcher.plan import NormalizedPlan
from dispatcher.state_store import StateStore


@pytest.fixture
def project(tmp_path: Path) -> FixtureProject:
    return create_fixture_project(tmp_path)


def _store(project: FixtureProject) -> StateStore:
    return StateStore(
        project.state,
        heartbeat_seconds=project.config.lease_heartbeat_seconds,
        stale_after_seconds=project.config.lease_stale_after_seconds,
    )


def test_baseline_inspection_never_infers_acceptance_and_approval_tracks_evidence(
    project: FixtureProject,
) -> None:
    evidence = project.evidence / "fixture.md"
    evidence.write_text("historical fixture evidence\n", encoding="utf-8")
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    store = _store(project)

    candidate = inspect_baseline(plan, project.config)

    assert candidate.steps[0].step_id == "prepare-fixture"
    assert candidate.steps[0].proposed_state == "PENDING"
    assert candidate.steps[0].evidence[0].relative_path == "evidence/fixture.md"
    approve_baseline(
        candidate,
        plan=plan,
        config=project.config,
        store=store,
        operator_decision_ref="decision-approve-baseline",
    )
    assert validate_approved_baseline(plan=plan, config=project.config, store=store) == candidate

    evidence.write_text("changed historical fixture evidence\n", encoding="utf-8")
    with pytest.raises(BaselineError, match="historical evidence changed"):
        validate_approved_baseline(plan=plan, config=project.config, store=store)
