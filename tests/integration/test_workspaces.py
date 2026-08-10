from __future__ import annotations

import json
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


def test_workspace_merge_conflict_preserves_source_and_allows_reconciled_cleanup(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    _commit_fixture_repository(project.repository)
    config = _worktree_config(project)
    plan = _two_step_plan(project)
    record = new_run_record(
        run_id="workspace-conflict-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-workspace-conflict"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    coordinator = WorkspaceCoordinator(config, store)
    prepared = coordinator.prepare(
        run_id=record.run_id,
        expected_generation=generation,
        repo_id="fixture-repo",
        step_ids=["prepare-fixture", "prepare-second"],
    )
    for index, child in enumerate(prepared.group.children, start=1):
        child_root = Path(child.worktree_path)
        _git(child_root, "config", "user.email", "fixture@example.invalid")
        _git(child_root, "config", "user.name", "Fixture")
        (child_root / "initial.txt").write_text(f"child {index} change\n", encoding="utf-8")
        _git(child_root, "add", "initial.txt")
        _git(child_root, "commit", "-m", f"child {index} conflict")

    with pytest.raises(WorkspaceError, match="workspace integration failed"):
        coordinator.integrate(
            run_id=prepared.record.run_id,
            expected_generation=prepared.generation,
            workspace_group_id=prepared.group.workspace_group_id,
        )

    failed, generation = store.load_run(record.run_id)
    group = failed.workspace_groups[prepared.group.workspace_group_id]
    assert group.state is WorkspaceGroupStatus.FAILED
    assert _git(project.repository, "show", "HEAD:initial.txt") == "fixture"
    cleaned = coordinator.cleanup(
        run_id=failed.run_id,
        expected_generation=generation,
        workspace_group_id=group.workspace_group_id,
        force=True,
    )
    assert cleaned.group.state is WorkspaceGroupStatus.CLEANED
    assert _git(project.repository, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_workspace_recovery_classifies_active_and_pending_cleanup_after_restart(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    _commit_fixture_repository(project.repository)
    config = _worktree_config(project)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = new_run_record(
        run_id="workspace-recovery-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-workspace-recovery"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    coordinator = WorkspaceCoordinator(config, store)
    active = coordinator.prepare(
        run_id=record.run_id,
        expected_generation=generation,
        repo_id="fixture-repo",
        step_ids=["prepare-fixture"],
    )
    store.close()
    restarted = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )

    active_items = restarted.classify_workspace_recovery(record.run_id)

    assert active_items[0].workspace_group_id == active.group.workspace_group_id
    assert active_items[0].disposition == "operator_reconciliation_required"
    reopened, generation = restarted.load_run(record.run_id)
    group = reopened.workspace_groups[active.group.workspace_group_id]
    pending = WorkspaceCoordinator(config, restarted).manager.begin_cleanup(group, event=_event(reopened.sequence + 1))
    reopened, generation = restarted.save_workspace_group(
        reopened,
        expected_generation=generation,
        group=pending,
    )
    restarted.close()
    restarted = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )

    pending_items = restarted.classify_workspace_recovery(record.run_id)

    assert pending_items[0].disposition == "cleanup_required"
    cleaned = WorkspaceCoordinator(config, restarted).cleanup(
        run_id=reopened.run_id,
        expected_generation=generation,
        workspace_group_id=pending.workspace_group_id,
        force=True,
    )
    assert cleaned.group.state is WorkspaceGroupStatus.CLEANED
    assert _git(project.repository, "worktree", "list", "--porcelain").count("worktree ") == 1

def _worktree_config(project):
    values = config_values(project)
    values["execution"]["concurrency"]["same_repository_mode"] = "worktree_barrier"
    return write_config(project, values)


def _two_step_plan(project) -> NormalizedPlan:
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
    return NormalizedPlan.model_validate(values)


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
