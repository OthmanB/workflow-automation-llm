from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import config_values, create_fixture_project, valid_plan_values, write_config

from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.state_store import StateStore
from dispatcher.workflow import TransitionEvent, WorkspaceGroupStatus, new_run_record
from dispatcher.workspaces import WorkspaceCoordinator, WorkspaceError, WorktreeManager


def test_workspace_group_is_durable_and_cleans_its_temporary_branch_and_worktree(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    _commit_fixture_repository(project.repository)
    config = _worktree_config(project)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = new_run_record(
        run_id="workspace-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-workspace"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    coordinator = WorkspaceCoordinator(config, store)
    outcome = coordinator.prepare(
        run_id=record.run_id,
        expected_generation=generation,
        repo_id="fixture-repo",
        step_ids=["prepare-fixture"],
    )
    reopened, reopened_generation = store.load_run(record.run_id)

    assert reopened_generation == outcome.generation
    assert reopened.workspace_groups[outcome.group.workspace_group_id].state is WorkspaceGroupStatus.ACTIVE
    assert store.leases_for_run(record.run_id)[0].resource_key == "repository:fixture-repo"
    inspected = coordinator.manager.inspect(outcome.group)

    assert inspected.state is WorkspaceGroupStatus.ACTIVE
    assert inspected.children[0].head_revision == outcome.group.base_revision
    assert Path(inspected.children[0].worktree_path).is_dir()
    assert _git(project.repository, "branch", "--list", inspected.children[0].branch).endswith(
        inspected.children[0].branch
    )

    cleanup = coordinator.cleanup(
        run_id=record.run_id,
        expected_generation=outcome.generation,
        workspace_group_id=outcome.group.workspace_group_id,
    )
    cleaned = cleanup.group
    record = cleanup.record

    assert cleaned.state is WorkspaceGroupStatus.CLEANED
    assert not Path(cleaned.children[0].worktree_path).exists()
    assert _git(project.repository, "branch", "--list", cleaned.children[0].branch) == ""
    assert _git(project.repository, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert record.workspace_groups[cleaned.workspace_group_id].state is WorkspaceGroupStatus.CLEANED
    assert store.leases_for_run(record.run_id) == ()


def test_workspace_groups_reject_serialized_and_patch_only_repository_modes(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    _commit_fixture_repository(project.repository)
    serialized = write_config(project, config_values(project))
    manager = WorktreeManager(serialized)

    with pytest.raises(WorkspaceError, match="worktree_barrier"):
        manager.plan_group(
            workspace_group_id="workspace-group-serial",
            repo_id="fixture-repo",
            step_ids=["prepare-fixture"],
            event=_event(1),
        )

    values = config_values(project)
    values["execution"]["concurrency"]["same_repository_mode"] = "worktree_barrier"
    values["repositories"]["fixture-repo"]["commit_policy"] = "prohibited"
    patch_only = write_config(project, values)

    with pytest.raises(WorkspaceError, match="commit_policy required"):
        WorktreeManager(patch_only).plan_group(
            workspace_group_id="workspace-group-patch",
            repo_id="fixture-repo",
            step_ids=["prepare-fixture"],
            event=_event(1),
        )


def _worktree_config(project):
    values = config_values(project)
    values["execution"]["concurrency"]["same_repository_mode"] = "worktree_barrier"
    return write_config(project, values)


def _commit_fixture_repository(root: Path) -> None:
    (root / "initial.txt").write_text("fixture\n", encoding="utf-8")
    for args in (
        ("config", "user.email", "fixture@example.invalid"),
        ("config", "user.name", "Fixture"),
        ("add", "initial.txt"),
        ("commit", "-m", "initial fixture"),
        ("branch", "-M", "main"),
    ):
        _git(root, *args)


def _event(sequence: int) -> TransitionEvent:
    return TransitionEvent(
        event_id=f"event-workspace-{sequence}",
        sequence=sequence,
        actor="dispatcher",
        reason="workspace fixture",
        correlation_id="workspace-group-one",
        occurred_at=datetime.now(UTC),
    )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()
