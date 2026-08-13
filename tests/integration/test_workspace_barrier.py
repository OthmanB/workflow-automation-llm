from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from helpers import config_values, create_fixture_project, valid_plan_values, write_config

from dispatcher.cli import main
from dispatcher.execution import SequentialExecutionCoordinator
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.sequential import PreparedBatch, SequentialWorkflow
from dispatcher.sessions import SessionResult
from dispatcher.state_store import StateStore
from dispatcher.workflow import (
    OperatorRequest,
    RunStatus,
    TransitionEvent,
    WorkspaceGroupStatus,
    new_run_record,
    transition_run,
    transition_workspace_group,
)
from dispatcher.workspaces import WorkspaceCoordinator, WorktreeManager


def test_same_repository_barrier_promotes_once_and_cleans_every_temporary_branch(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    _commit_initial(project.repository)
    config = _workspace_config(project)
    plan = _two_step_plan(project)
    record = new_run_record(
        run_id="workspace-barrier-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-workspace-barrier"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(config, store, owner_id="workspace-barrier-owner")
    coordinator = SequentialExecutionCoordinator(
        config,
        store,
        workflow,
        owner_id="workspace-barrier-owner",
        session_runner=_workspace_executor_runner,
    )
    coordinator.acquire_run(record.run_id)
    try:
        active, generation = workflow.activate(record.run_id, expected_generation=generation)
        prepared = workflow.prepare_from_supervisor(
            active.run_id,
            expected_generation=generation,
            supervisor_text=json.dumps(
                {
                    "protocol_version": 2,
                    "action": "dispatch_batch",
                    "children": [
                        {
                            "step_id": "prepare-fixture",
                            "target_role": "terra",
                            "session_mode": "new",
                            "prompt": "write first independent artifact",
                        },
                        {
                            "step_id": "prepare-second",
                            "target_role": "terra",
                            "session_mode": "new",
                            "prompt": "write second independent artifact",
                        },
                    ],
                }
            ),
        )
        assert isinstance(prepared, PreparedBatch)
        assert {item.workdir for item in prepared.dispatches} != {project.repository}
        assert all(item.dispatch.workspace_group_id is not None for item in prepared.dispatches)

        barrier = coordinator.execute_batch(prepared)
        assert barrier.record.workspace_groups[next(iter(barrier.record.workspace_groups))].state is WorkspaceGroupStatus.ACTIVE
        assert _git(project.repository, "log", "--format=%s", "-1") == "initial fixture"

        acknowledged = barrier.record
        generation = barrier.generation
        for dispatch_id in barrier.forwarded_dispatch_ids:
            acknowledged, generation = workflow.acknowledge_forwarding(
                acknowledged.run_id,
                expected_generation=generation,
                dispatch_id=dispatch_id,
            )
    finally:
        coordinator.release_run()

    group = next(iter(acknowledged.workspace_groups.values()))
    assert group.state is WorkspaceGroupStatus.CLEANED
    assert group.integration_revision == _git(project.repository, "rev-parse", "HEAD")
    assert (project.repository / "work-prepare-fixture.txt").is_file()
    assert (project.repository / "work-prepare-second.txt").is_file()
    assert (project.repository / "evidence" / "fixture.md").is_file()
    assert (project.repository / "evidence" / "second.md").is_file()
    assert _git(project.repository, "worktree", "list", "--porcelain").count("worktree ") == 1
    for child in group.children:
        assert _git(project.repository, "branch", "--list", child.branch) == ""
        assert not Path(child.worktree_path).exists()
    assert _git(project.repository, "branch", "--list", group.integration_branch) == ""


def test_workspace_reviewer_dispatch_uses_the_executor_child_worktree(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    _commit_initial(project.repository)
    config = _workspace_config(project)
    plan = _two_step_plan(project, review_required=True)
    record = new_run_record(
        run_id="workspace-review-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-workspace-review"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(config, store, owner_id="workspace-review-owner")
    coordinator = SequentialExecutionCoordinator(
        config,
        store,
        workflow,
        owner_id="workspace-review-owner",
        session_runner=_workspace_executor_runner,
    )
    coordinator.acquire_run(record.run_id)
    try:
        active, generation = workflow.activate(record.run_id, expected_generation=generation)
        prepared = workflow.prepare_from_supervisor(
            active.run_id,
            expected_generation=generation,
            supervisor_text=_workspace_batch_command(),
        )
        assert isinstance(prepared, PreparedBatch)
        barrier = coordinator.execute_batch(prepared)
        acknowledged = barrier.record
        generation = barrier.generation
        for dispatch_id in barrier.forwarded_dispatch_ids:
            acknowledged, generation = workflow.acknowledge_forwarding(
                acknowledged.run_id,
                expected_generation=generation,
                dispatch_id=dispatch_id,
            )
        reviewer = workflow.prepare_from_supervisor(
            acknowledged.run_id,
            expected_generation=generation,
            supervisor_text=json.dumps(
                {
                    "protocol_version": 1,
                    "action": "dispatch",
                    "step_id": "prepare-fixture",
                    "target_role": "reviewer",
                    "session_mode": "new",
                    "prompt": "review the immutable child result",
                }
            ),
        )
        assert reviewer.dispatch.workspace_group_id is not None
        group = acknowledged.workspace_groups[reviewer.dispatch.workspace_group_id]
        child = next(item for item in group.children if item.step_id == "prepare-fixture")
        assert reviewer.workdir == Path(child.worktree_path)
        failed, generation = workflow.fail_dispatch(
            reviewer,
            reason="test cleanup",
            failure_category="workflow_validation",
            failure_detail="test cleanup",
        )
        cleanup = workflow.workspace_coordinator.cleanup(
            run_id=failed.run_id,
            expected_generation=generation,
            workspace_group_id=group.workspace_group_id,
            force=True,
        )
        assert cleanup.group.state is WorkspaceGroupStatus.CLEANED
    finally:
        coordinator.release_run()


def test_workspace_reconciliation_cli_cleans_non_force_before_recording_answer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project, config, store, waiting, generation, group = _workspace_reconciliation_fixture(tmp_path)
    calls: list[tuple[WorkspaceGroupStatus, bool]] = []
    original_cleanup = WorktreeManager.cleanup

    def tracked_cleanup(self, pending, *, event, force=False):
        calls.append((pending.state, force))
        return original_cleanup(self, pending, event=event, force=force)

    monkeypatch.setattr(WorktreeManager, "cleanup", tracked_cleanup)
    store.close()

    result = main(
        [
            "answer",
            "--config",
            str(project.config_path),
            "--run-id",
            waiting.run_id,
            "--request-id",
            waiting.operator_request.request_id,
            "--answer",
            "reconcile",
            "--actor-id",
            "operator",
        ]
    )

    assert result == 0
    assert calls == [(WorkspaceGroupStatus.CLEANUP_PENDING, False)]
    persisted = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    updated, updated_generation = persisted.load_run(waiting.run_id)
    cleaned = updated.workspace_groups[group.workspace_group_id]
    assert updated.state is RunStatus.RUNNING
    assert updated.operator_request is None
    assert cleaned.state is WorkspaceGroupStatus.CLEANED
    assert updated.sequence == cleaned.last_event.sequence + 1
    assert updated_generation == generation + 3
    assert _operator_decision_count(persisted) == 1
    for child in group.children:
        assert not Path(child.worktree_path).exists()
        assert _git(project.repository, "branch", "--list", child.branch) == ""


def test_workspace_reconciliation_cli_cleanup_failure_keeps_request_and_unsafe_git_state(
    tmp_path: Path,
    capsys,
) -> None:
    project, config, store, waiting, generation, group = _workspace_reconciliation_fixture(tmp_path)
    child = group.children[0]
    child_path = Path(child.worktree_path)
    unsafe_file = child_path / "unmerged.txt"
    unsafe_file.write_text("must survive failed cleanup\n", encoding="utf-8")
    _git(child_path, "add", "unmerged.txt")
    _git(child_path, "commit", "-m", "unmerged workspace work")
    unsafe_revision = _git(child_path, "rev-parse", "HEAD")
    store.close()

    result = main(
        [
            "answer",
            "--config",
            str(project.config_path),
            "--run-id",
            waiting.run_id,
            "--request-id",
            waiting.operator_request.request_id,
            "--answer",
            "reconcile",
            "--actor-id",
            "operator",
        ]
    )

    assert result == 2
    assert "workspace branch is not merged" in capsys.readouterr().err
    persisted = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    updated, updated_generation = persisted.load_run(waiting.run_id)
    assert updated.state is RunStatus.WAITING_OPERATOR
    assert updated.operator_request == waiting.operator_request
    assert updated.workspace_groups[group.workspace_group_id].state is WorkspaceGroupStatus.FAILED
    assert updated_generation == generation + 2
    assert _operator_decision_count(persisted) == 0
    assert child_path.is_dir()
    assert unsafe_file.read_text(encoding="utf-8") == "must survive failed cleanup\n"
    assert _git(project.repository, "rev-parse", child.branch) == unsafe_revision


def test_workspace_reconciliation_cli_halt_skips_cleanup_and_preserves_group(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project, config, store, waiting, generation, group = _workspace_reconciliation_fixture(tmp_path)

    def forbidden_cleanup(*_args, **_kwargs):
        raise AssertionError("halt must not invoke workspace cleanup")

    monkeypatch.setattr(WorkspaceCoordinator, "cleanup", forbidden_cleanup)
    store.close()

    result = main(
        [
            "answer",
            "--config",
            str(project.config_path),
            "--run-id",
            waiting.run_id,
            "--request-id",
            waiting.operator_request.request_id,
            "--answer",
            "halt",
            "--actor-id",
            "operator",
        ]
    )

    assert result == 0
    persisted = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    updated, updated_generation = persisted.load_run(waiting.run_id)
    assert updated.state is RunStatus.HALTED
    assert updated.workspace_groups[group.workspace_group_id] == group
    assert updated_generation == generation + 1
    assert _operator_decision_count(persisted) == 1
    assert all(Path(child.worktree_path).is_dir() for child in group.children)


def _workspace_reconciliation_fixture(tmp_path: Path):
    project = create_fixture_project(tmp_path)
    _commit_initial(project.repository)
    config = _workspace_config(project)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = new_run_record(
        run_id="workspace-reconciliation-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-workspace-reconciliation"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    outcome = WorkspaceCoordinator(config, store).prepare(
        run_id=record.run_id,
        expected_generation=generation,
        repo_id="fixture-repo",
        step_ids=("prepare-fixture",),
    )
    failed = transition_workspace_group(
        outcome.group,
        WorkspaceGroupStatus.FAILED,
        _event(outcome.record.sequence + 1),
    )
    failed_record, generation = store.save_workspace_group(
        outcome.record,
        expected_generation=outcome.generation,
        group=failed,
    )
    request = OperatorRequest(
        request_id="request-workspace-reconciliation",
        question="Reconcile the failed workspace group",
        allowed_answers=["reconcile", "halt"],
        context_ref=failed.workspace_group_id,
        resume_to=RunStatus.RUNNING,
        expires_at=None,
        required_role=None,
        kind="workspace_reconciliation",
    )
    waiting = transition_run(
        failed_record,
        RunStatus.WAITING_OPERATOR,
        _event(failed_record.sequence + 1),
        operator_request=request,
    )
    generation = store.save_run(waiting, expected_generation=generation)
    return project, config, store, waiting, generation, failed


def _operator_decision_count(store: StateStore) -> int:
    with sqlite3.connect(store.database_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM operator_decisions").fetchone()[0])


def _workspace_config(project):
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
                "worktree_root": str(project.root / "workspaces"),
                "worktree_branch_prefix": "dispatcher/workspace",
            },
        }
    )
    values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    values["permission_policies"]["policies"]["executor-class"]["actions"]["commit"] = "allow"
    return write_config(project, values)


def _two_step_plan(project, *, review_required: bool = False) -> NormalizedPlan:
    values = valid_plan_values(project)
    values["steps"][0]["authorization"] = {
        "authorized_actions": ["inspect", "modify", "verify", "commit"],
        "writable_paths": ["evidence/fixture.md", "work-prepare-fixture.txt"],
        "requires_operator_approval": False,
    }
    if review_required:
        values["steps"][0]["review"] = {
            "required": True,
            "reviewer_role_keys": ["reviewer"],
            "required_acceptances": 1,
        }
        values["steps"][0]["retry"]["max_reviewer_attempts"] = 1
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
            "authorization": {
                "authorized_actions": ["inspect", "modify", "verify", "commit"],
                "writable_paths": ["evidence/second.md", "work-prepare-second.txt"],
                "requires_operator_approval": False,
            },
        }
    )
    values["steps"].append(second)
    return NormalizedPlan.model_validate(values)


def _workspace_batch_command() -> str:
    return json.dumps(
        {
            "protocol_version": 2,
            "action": "dispatch_batch",
            "children": [
                {
                    "step_id": "prepare-fixture",
                    "target_role": "terra",
                    "session_mode": "new",
                    "prompt": "write first independent artifact",
                },
                {
                    "step_id": "prepare-second",
                    "target_role": "terra",
                    "session_mode": "new",
                    "prompt": "write second independent artifact",
                },
            ],
        }
    )


def _workspace_executor_runner(**kwargs: Any) -> SessionResult:
    lifecycle = kwargs["lifecycle"]
    prompt = json.loads(kwargs["prompt"])
    workdir = Path(kwargs["workdir"])
    step_id = prompt["step_id"]
    evidence_name = "fixture.md" if step_id == "prepare-fixture" else "second.md"
    artifact_id = "fixture-evidence" if step_id == "prepare-fixture" else "second-evidence"
    lifecycle.on_process_started(2000, 2000.0)
    session_id = f"session-{prompt['dispatch_id']}"
    lifecycle.on_session_identified(session_id)
    (workdir / f"work-{step_id}.txt").write_text(f"{step_id}\n", encoding="utf-8")
    evidence = workdir / "evidence" / evidence_name
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text(f"evidence {step_id}\n", encoding="utf-8")
    return SessionResult(
        session_id=session_id,
        exit_code=0,
        evidence_written=[],
        chat_response=json.dumps(
            {
                "proposal_version": 2,
                "response_contract": "dispatcher.executor_proposal.v2",
                "dispatch_id": prompt["dispatch_id"],
                "attempt": prompt["attempt"],
                "step_id": step_id,
                "repository": {
                    "repo_id": prompt["repo_id"],
                    "base_revision": prompt["base_revision"],
                },
                "evidence": [
                    {
                        "artifact_id": artifact_id,
                        "relative_path": evidence_name,
                        "media_type": "text/markdown",
                    }
                ],
                "criterion_self_reports": [
                    {
                        "check_id": criterion["criterion_id"],
                        "status": "not_run",
                        "summary": "dispatcher owns this check",
                    }
                    for criterion in prompt["acceptance_criteria"]
                ],
                "summary": f"completed {step_id}",
                "outcome": "completed",
            }
        ),
        usage={"total": 0, "input": 0, "output": 0, "reasoning": 0},
        cost=0.0,
    )


def _commit_initial(root: Path) -> None:
    (root / "initial.txt").write_text("initial\n", encoding="utf-8")
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
        event_id=f"event-workspace-barrier-{sequence}",
        sequence=sequence,
        actor="dispatcher",
        reason="workspace barrier fixture",
        correlation_id="workspace-barrier-run",
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
