from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from helpers import FixtureProject, create_fixture_project, valid_plan_values

from dispatcher.plan import (
    NormalizedPlan,
    PlanError,
    approve_plan,
    load_normalized_plan,
    validate_plan_approval,
    validate_plan_for_config,
)


@pytest.fixture
def project(tmp_path: Path) -> FixtureProject:
    return create_fixture_project(tmp_path)


def test_generic_plan_accepts_non_t_step_ids_and_arbitrary_role_keys(
    project: FixtureProject,
) -> None:
    plan = NormalizedPlan.model_validate(valid_plan_values(project))

    validate_plan_for_config(plan, project.config)

    assert plan.steps[0].step_id == "prepare-fixture"
    assert len(plan.plan_digest) == 64
    assert isinstance(plan.steps, tuple)
    assert isinstance(plan.steps[0].depends_on, tuple)
    with pytest.raises(AttributeError):
        plan.steps.append(plan.steps[0])


def test_yaml_sidecar_verifies_source_hash_and_cross_references(project: FixtureProject) -> None:
    sidecar = project.root / "fixture-plan.yaml"
    sidecar.write_text(yaml.safe_dump(valid_plan_values(project), sort_keys=False), encoding="utf-8")

    plan = load_normalized_plan(sidecar, project.config)

    assert plan.plan_id == "fixture-plan"
    assert plan.source_digest != plan.plan_digest


def test_semantically_identical_plans_share_digest_despite_source_identity(
    project: FixtureProject,
) -> None:
    first = NormalizedPlan.model_validate(valid_plan_values(project))
    second_values = valid_plan_values(project)
    second_values["sources"][0]["source_id"] = "different-source"
    second_values["sources"][0]["media_type"] = "application/yaml"
    second = NormalizedPlan.model_validate(second_values)

    assert first.plan_digest == second.plan_digest
    assert first.source_digest != second.source_digest


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda values: values["steps"][0].pop("authorization"),
            "authorization\n  Field required",
        ),
        (
            lambda values: values["steps"][0].update({"evidence_requirements": []}),
            "evidence_requirements must not be empty",
        ),
        (
            lambda values: values["steps"][0].update({"acceptance_criteria": []}),
            "acceptance_criteria must not be empty",
        ),
    ],
)
def test_missing_actionable_fields_fail_normalization(
    project: FixtureProject,
    mutate: Any,
    message: str,
) -> None:
    values = valid_plan_values(project)
    mutate(values)

    with pytest.raises(ValueError, match=message):
        NormalizedPlan.model_validate(values)


def test_unknown_repository_and_unauthorized_action_fail_cross_validation(
    project: FixtureProject,
) -> None:
    unknown_repository = NormalizedPlan.model_validate(valid_plan_values(project))
    unknown_values = unknown_repository.model_dump()
    unknown_values["steps"][0]["repo_id"] = "missing-repo"
    with pytest.raises(PlanError, match="unknown repository id"):
        validate_plan_for_config(NormalizedPlan.model_validate(unknown_values), project.config)

    unauthorized_values = valid_plan_values(project)
    unauthorized_values["steps"][0]["authorization"]["authorized_actions"] = ["delete"]
    with pytest.raises(PlanError, match="authorization exceeds repository policy"):
        validate_plan_for_config(NormalizedPlan.model_validate(unauthorized_values), project.config)


def test_dependency_and_resource_conflicts_are_rejected(project: FixtureProject) -> None:
    values = valid_plan_values(project)
    second = values["steps"][0].copy()
    second.update(
        {
            "ordinal": 2,
            "step_id": "second-fixture",
            "title": "Second fixture",
            "produced_outputs": [
                {
                    "artifact_id": "second-output",
                    "producer_step_id": None,
                    "description": "Second output",
                }
            ],
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

    with pytest.raises(ValueError, match="unordered write lock conflict"):
        NormalizedPlan.model_validate(values)


def test_non_contiguous_and_cyclic_dependencies_are_rejected(project: FixtureProject) -> None:
    values = valid_plan_values(project)
    values["steps"][0]["ordinal"] = 2

    with pytest.raises(ValueError, match="contiguous ordinals"):
        NormalizedPlan.model_validate(values)


def test_source_hash_mismatch_fails_sidecar_load(project: FixtureProject) -> None:
    values = valid_plan_values(project)
    values["sources"][0]["sha256"] = "0" * 64
    sidecar = project.root / "invalid-plan.yaml"
    sidecar.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")

    with pytest.raises(PlanError, match="plan source hash mismatch"):
        load_normalized_plan(sidecar, project.config)


def test_plan_approval_is_invalidated_by_source_digest_change(project: FixtureProject) -> None:
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    approval = approve_plan(plan, "decision-approve-plan")
    changed_values = valid_plan_values(project)
    changed_values["sources"][0]["source_id"] = "changed-source-identity"
    changed_plan = NormalizedPlan.model_validate(changed_values)

    with pytest.raises(PlanError, match="source digest"):
        validate_plan_approval(changed_plan, approval)
