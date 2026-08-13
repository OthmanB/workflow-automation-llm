from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import config_values, create_fixture_project, valid_plan_values, write_config

from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.protocol import DispatchRequest
from dispatcher.scheduler import (
    SchedulingError,
    evaluate_readiness,
    validate_batch,
    validate_workspace_batch,
)
from dispatcher.workflow import (
    StepStatus,
    TransitionEvent,
    WorkspaceChild,
    WorkspaceGroup,
    WorkspaceGroupStatus,
    new_run_record,
    transition_step,
)


def _event(sequence: int) -> TransitionEvent:
    return TransitionEvent(
        event_id=f"event-{sequence}",
        sequence=sequence,
        actor="dispatcher",
        reason="scheduler fixture",
        correlation_id="scheduler-fixture",
        occurred_at=datetime.now(UTC),
    )


def _parallel_config(project_path: Path):
    project = create_fixture_project(project_path)
    values = config_values(project)
    values["execution"].update(
        {
            "scheduling": "bounded_parallel",
            "concurrency": {
                "max_active_dispatches": 2,
                "max_batch_size": 2,
                "role_capacities": {"terra": 2, "reviewer": 1, "reviewer-two": 1},
                "failure_mode": "wait_for_started",
                "same_repository_mode": "serialized",
                "worktree_root": str(project.root / "worktrees"),
                "worktree_branch_prefix": "dispatcher/workspace",
            },
        }
    )
    return project, write_config(project, values)


def _two_step_plan(project) -> NormalizedPlan:
    values = valid_plan_values(project)
    second = json.loads(json.dumps(values["steps"][0]))
    second.update(
        {
            "ordinal": 2,
            "step_id": "second-fixture",
            "title": "Second fixture",
            "repo_id": "second-repository",
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
    return NormalizedPlan.model_validate(values)


def _ready_record(project, plan: NormalizedPlan):
    record = new_run_record(
        run_id="scheduler-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-scheduler"),
        event=_event(1),
    )
    steps = {
        step_id: transition_step(step, StepStatus.READY, _event(index + 2))
        for index, (step_id, step) in enumerate(record.steps.items())
    }
    return record.model_copy(update={"steps": steps, "sequence": 3, "updated_at": _event(3).occurred_at})


def test_readiness_explains_unaccepted_dependency_in_plan_order(tmp_path: Path) -> None:
    project, config = _parallel_config(tmp_path)
    values = valid_plan_values(project)
    second = json.loads(json.dumps(values["steps"][0]))
    second.update(
        {
            "ordinal": 2,
            "step_id": "dependent-fixture",
            "title": "Dependent fixture",
            "depends_on": ["prepare-fixture"],
            "required_inputs": [
                {
                    "artifact_id": "fixture-output",
                    "producer_step_id": "prepare-fixture",
                    "description": "Prepared fixture output",
                }
            ],
            "produced_outputs": [
                {
                    "artifact_id": "dependent-output",
                    "producer_step_id": None,
                    "description": "Dependent output",
                }
            ],
            "resource_locks": [{"resource_id": "dependent-resource", "mode": "write"}],
            "evidence_requirements": [
                {
                    "artifact_id": "dependent-evidence",
                    "relative_path": "dependent.md",
                    "media_type": "text/markdown",
                }
            ],
        }
    )
    values["steps"].append(second)
    plan = NormalizedPlan.model_validate(values)
    record = new_run_record(
        run_id="dependency-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-dependency"),
        event=_event(1),
    )

    readiness = evaluate_readiness(config, record)

    assert [item.step_id for item in readiness] == ["prepare-fixture", "dependent-fixture"]
    assert readiness[1].reasons == (
        "state is PENDING",
        "dependency prepare-fixture is not accepted",
        "input producer prepare-fixture is not accepted",
    )


def test_batch_rejects_same_repository_even_with_distinct_declared_resources(tmp_path: Path) -> None:
    project, config = _parallel_config(tmp_path)
    plan = _two_step_plan(project)
    same_repo_values = plan.model_dump(mode="json")
    same_repo_values["steps"][1]["repo_id"] = "fixture-repo"
    same_repo = NormalizedPlan.model_validate(same_repo_values)
    record = _ready_record(project, same_repo)
    children = (
        DispatchRequest(step_id="prepare-fixture", target_role="terra", session_mode="new", prompt="one"),
        DispatchRequest(step_id="second-fixture", target_role="terra", session_mode="new", prompt="two"),
    )

    with pytest.raises(SchedulingError, match="repository:fixture-repo"):
        validate_batch(config, record, children)


def test_batch_accepts_independent_cross_repository_children_in_plan_order(tmp_path: Path) -> None:
    project, config = _parallel_config(tmp_path)
    record = _ready_record(project, _two_step_plan(project))
    children = (
        DispatchRequest(step_id="second-fixture", target_role="terra", session_mode="new", prompt="two"),
        DispatchRequest(step_id="prepare-fixture", target_role="terra", session_mode="new", prompt="one"),
    )

    scheduled = validate_batch(config, record, children)

    assert [child.step_id for child in scheduled] == ["prepare-fixture", "second-fixture"]


def test_batch_rejects_children_that_exceed_a_worker_role_capacity(tmp_path: Path) -> None:
    project, _config = _parallel_config(tmp_path)
    values = config_values(project)
    values["execution"]["concurrency"]["role_capacities"]["terra"] = 1
    config = write_config(project, values)
    record = _ready_record(project, _two_step_plan(project))
    children = (
        DispatchRequest(step_id="prepare-fixture", target_role="terra", session_mode="new", prompt="one"),
        DispatchRequest(step_id="second-fixture", target_role="terra", session_mode="new", prompt="two"),
    )

    with pytest.raises(SchedulingError, match="role terra exceeds configured capacity"):
        validate_batch(config, record, children)


def _barrier_config(project_path: Path):
    project = create_fixture_project(project_path)
    values = config_values(project)
    values["execution"].update(
        {
            "scheduling": "bounded_parallel",
            "concurrency": {
                "max_active_dispatches": 2,
                "max_batch_size": 2,
                "role_capacities": {"terra": 2, "reviewer": 1, "reviewer-two": 1},
                "failure_mode": "wait_for_started",
                "same_repository_mode": "worktree_barrier",
                "worktree_root": str(project.root / "worktrees"),
                "worktree_branch_prefix": "dispatcher/workspace",
            },
        }
    )
    return project, write_config(project, values)


def _barrier_plan(project, first_writable: str, second_writable: str) -> NormalizedPlan:
    values = valid_plan_values(project)
    second = json.loads(json.dumps(values["steps"][0]))
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
            "resource_locks": [{"resource_id": "second-resource", "mode": "write"}],
            "evidence_requirements": [
                {
                    "artifact_id": "second-evidence",
                    "relative_path": "second.md",
                    "media_type": "text/markdown",
                }
            ],
            "authorization": {
                "authorized_actions": ["inspect", "modify", "commit"],
                "writable_paths": [second_writable],
                "requires_operator_approval": False,
            },
        }
    )
    values["steps"][0]["authorization"] = {
        "authorized_actions": ["inspect", "modify", "commit"],
        "writable_paths": [first_writable],
        "requires_operator_approval": False,
    }
    values["steps"].append(second)
    return NormalizedPlan.model_validate(values)


def _barrier_children() -> tuple[DispatchRequest, DispatchRequest]:
    return (
        DispatchRequest(step_id="prepare-fixture", target_role="terra", session_mode="new", prompt="one"),
        DispatchRequest(step_id="second-fixture", target_role="terra", session_mode="new", prompt="two"),
    )


def test_workspace_batch_rejects_overlapping_writable_scopes(tmp_path: Path) -> None:
    project, config = _barrier_config(tmp_path)
    plan = _barrier_plan(project, "work/shared/", "work/shared/inner.txt")
    record = _ready_record(project, plan)

    with pytest.raises(SchedulingError, match="overlaps step"):
        validate_workspace_batch(config, record, _barrier_children())


def test_workspace_batch_rejects_identical_writable_scopes(tmp_path: Path) -> None:
    project, config = _barrier_config(tmp_path)
    plan = _barrier_plan(project, "work/shared/", "work/shared/")
    record = _ready_record(project, plan)

    with pytest.raises(SchedulingError, match="overlaps step"):
        validate_workspace_batch(config, record, _barrier_children())


def test_workspace_batch_accepts_disjoint_writable_scopes(tmp_path: Path) -> None:
    project, config = _barrier_config(tmp_path)
    plan = _barrier_plan(project, "work/alpha/", "work/beta.txt")
    record = _ready_record(project, plan)

    ordered = validate_workspace_batch(config, record, _barrier_children())

    assert [child.step_id for child in ordered] == ["prepare-fixture", "second-fixture"]


def test_workspace_batch_rejects_non_executor_children(tmp_path: Path) -> None:
    project, config = _barrier_config(tmp_path)
    plan = _barrier_plan(project, "work/alpha/", "work/beta/")
    record = _ready_record(project, plan)
    children = (
        DispatchRequest(step_id="prepare-fixture", target_role="terra", session_mode="new", prompt="one"),
        DispatchRequest(step_id="second-fixture", target_role="reviewer", session_mode="new", prompt="two"),
    )

    with pytest.raises(SchedulingError, match="must target an executor role"):
        validate_workspace_batch(config, record, children)


def test_workspace_batch_rejects_duplicate_step_ids(tmp_path: Path) -> None:
    project, config = _barrier_config(tmp_path)
    plan = _barrier_plan(project, "work/alpha/", "work/beta/")
    record = _ready_record(project, plan)
    children = (
        DispatchRequest(step_id="prepare-fixture", target_role="terra", session_mode="new", prompt="one"),
        DispatchRequest(step_id="prepare-fixture", target_role="terra", session_mode="new", prompt="two"),
    )

    with pytest.raises(SchedulingError, match="duplicate step IDs"):
        validate_workspace_batch(config, record, children)


def test_workspace_batch_rejects_resumed_sessions(tmp_path: Path) -> None:
    project, config = _barrier_config(tmp_path)
    plan = _barrier_plan(project, "work/alpha/", "work/beta/")
    record = _ready_record(project, plan)
    children = (
        DispatchRequest(step_id="prepare-fixture", target_role="terra", session_mode="new", prompt="one"),
        DispatchRequest(step_id="second-fixture", target_role="terra", session_mode="resume", prompt="two"),
    )

    with pytest.raises(SchedulingError, match="requires a new session"):
        validate_workspace_batch(config, record, children)


def test_workspace_batch_requires_required_commit_policy(tmp_path: Path) -> None:
    project, config = _barrier_config(tmp_path)
    values = config_values(project)
    values["repositories"]["fixture-repo"]["commit_policy"] = "prohibited"
    config = write_config(project, values)
    plan = _barrier_plan(project, "work/alpha/", "work/beta/")
    record = _ready_record(project, plan)

    with pytest.raises(SchedulingError, match="requires commit_policy required"):
        validate_workspace_batch(config, record, _barrier_children())


def test_workspace_batch_rejects_an_active_workspace_group(tmp_path: Path) -> None:
    project, config = _barrier_config(tmp_path)
    plan = _barrier_plan(project, "work/alpha/", "work/beta/")
    record = _ready_record(project, plan)
    group = WorkspaceGroup(
        workspace_group_id="workspace-existing",
        repo_id="fixture-repo",
        base_revision="base-sha",
        base_branch="main",
        integration_branch="dispatcher/workspace/workspace-existing/integration",
        integration_worktree_path="/tmp/existing/integration",
        worktree_root="/tmp/existing",
        lease_owner_id="workspace-workspace-existing",
        children=(
            WorkspaceChild(
                step_id="prepare-fixture",
                branch="dispatcher/workspace/workspace-existing/prepare-fixture",
                worktree_path="/tmp/existing/prepare-fixture",
                base_revision="base-sha",
            ),
        ),
        state=WorkspaceGroupStatus.ACTIVE,
        last_event=_event(9),
    )
    record = record.model_copy(
        update={
            "workspace_groups": {"workspace-existing": group},
            "sequence": 9,
            "updated_at": _event(9).occurred_at,
        }
    )

    with pytest.raises(SchedulingError, match="already has an active workspace group"):
        validate_workspace_batch(config, record, _barrier_children())
