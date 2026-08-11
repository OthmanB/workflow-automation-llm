from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from helpers import config_values, create_fixture_project, valid_plan_values, write_config

from dispatcher.execution import SequentialExecutionCoordinator
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.repository import EvidenceManifestEntry, RepositorySnapshot
from dispatcher.sequential import PreparedBatch, SequentialWorkflow
from dispatcher.sessions import OpenCodeProcessError, SessionResult
from dispatcher.state_store import StateStore
from dispatcher.workflow import RunStatus, TransitionEvent, new_run_record


def test_batch_preparation_is_all_or_none_and_failed_children_join_durably(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    sibling = _initialize_sibling_repository(project.root / "sibling")
    config = _parallel_two_repository_config(project, sibling)
    plan = _two_repository_plan(project)
    record = new_run_record(
        run_id="batch-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-batch"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(
        config,
        store,
        owner_id="batch-owner",
        repository_inspector=_repository_snapshot,
    )
    coordinator = SequentialExecutionCoordinator(
        config,
        store,
        workflow,
        owner_id="batch-owner",
        session_runner=_failing_session_runner,
    )
    coordinator.acquire_run(record.run_id)
    try:
        active, generation = workflow.activate(record.run_id, expected_generation=generation)
        invalid = json.dumps(
            {
                "protocol_version": 2,
                "action": "dispatch_batch",
                "children": [
                    {
                        "step_id": "prepare-fixture",
                        "target_role": "terra",
                        "session_mode": "new",
                        "prompt": "first fixture",
                    },
                    {
                        "step_id": "missing-step",
                        "target_role": "terra",
                        "session_mode": "new",
                        "prompt": "invalid fixture",
                    },
                ],
            }
        )
        try:
            workflow.prepare_from_supervisor(
                active.run_id,
                expected_generation=generation,
                supervisor_text=invalid,
            )
        except ValueError as exc:
            assert "unknown step missing-step" in str(exc)
        else:
            raise AssertionError("invalid batch was accepted")
        assert store.load_run(record.run_id)[0].dispatches == {}
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
                            "repo_id": "fixture-repo",
                            "prompt": "first fixture",
                        },
                        {
                            "step_id": "prepare-sibling",
                            "target_role": "terra",
                            "session_mode": "new",
                            "repo_id": "sibling-repo",
                            "prompt": "second fixture",
                        },
                    ],
                }
            ),
        )
        assert isinstance(prepared, PreparedBatch)
        persisted, _generation = store.load_run(record.run_id)
        assert len(persisted.dispatches) == 2
        assert persisted.batches[prepared.batch_id].state.value == "PREPARED"

        outcome = coordinator.execute_batch(prepared)
    finally:
        coordinator.release_run()

    assert outcome.record.state is RunStatus.WAITING_OPERATOR
    assert outcome.record.operator_request is not None
    assert outcome.record.operator_request.kind == "batch_reconciliation"
    assert outcome.record.batches[outcome.batch_id].state.value == "FAILED"
    assert len(outcome.record.batches[outcome.batch_id].failed_dispatch_ids) == 2
    assert {dispatch.state.value for dispatch in outcome.record.dispatches.values()} == {"FAILED"}
    assert store.classify_recovery(record.run_id) == []


def test_successful_batch_forwards_and_acknowledges_every_child(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    sibling = _initialize_sibling_repository(project.root / "sibling")
    config = _parallel_two_repository_config(project, sibling)
    plan = _two_repository_plan(project)
    record = new_run_record(
        run_id="successful-batch-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-successful-batch"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(
        config,
        store,
        owner_id="successful-batch-owner",
        repository_inspector=_repository_snapshot,
    )
    coordinator = SequentialExecutionCoordinator(
        config,
        store,
        workflow,
        owner_id="successful-batch-owner",
        session_runner=_successful_session_runner,
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
                            "prompt": "first fixture",
                        },
                        {
                            "step_id": "prepare-sibling",
                            "target_role": "terra",
                            "session_mode": "new",
                            "prompt": "second fixture",
                        },
                    ],
                }
            ),
        )
        assert isinstance(prepared, PreparedBatch)
        outcome = coordinator.execute_batch(prepared)
        assert outcome.record.state is RunStatus.RUNNING, [
            dispatch.last_event.reason for dispatch in outcome.record.dispatches.values()
        ]
        assert outcome.record.batches[outcome.batch_id].state.value == "JOINED"
        assert len(outcome.forwarded_dispatch_ids) == 2
        acknowledged = outcome.record
        generation = outcome.generation
        for dispatch_id in outcome.forwarded_dispatch_ids:
            acknowledged, generation = workflow.acknowledge_forwarding(
                acknowledged.run_id,
                expected_generation=generation,
                dispatch_id=dispatch_id,
            )
    finally:
        coordinator.release_run()

    assert {dispatch.state.value for dispatch in acknowledged.dispatches.values()} == {"ACKNOWLEDGED"}
    assert {step.state.value for step in acknowledged.steps.values()} == {"ACCEPTED"}


def _parallel_two_repository_config(project, sibling: Path):
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
    values["permission_policies"]["policies"]["sibling-repository"] = {
        "default": "deny",
        "actions": {"inspect": "allow"},
    }
    values["repositories"]["sibling-repo"] = {
        "root": str(sibling),
        "expected_remote": {"name": "origin", "url": "https://example.invalid/sibling.git"},
        "default_branch": "main",
        "evidence_roots": ["evidence"],
        "writable_roots": ["."],
        "external_roots": [],
        "commit_policy": "required",
        "permission_policy": "sibling-repository",
        "allow_shared_writable_roots": False,
    }
    return write_config(project, values)


def _two_repository_plan(project) -> NormalizedPlan:
    values = valid_plan_values(project)
    second = json.loads(json.dumps(values["steps"][0]))
    second.update(
        {
            "ordinal": 2,
            "step_id": "prepare-sibling",
            "title": "Prepare sibling",
            "repo_id": "sibling-repo",
            "produced_outputs": [
                {
                    "artifact_id": "sibling-output",
                    "producer_step_id": None,
                    "description": "Sibling output",
                }
            ],
            "resource_locks": [{"resource_id": "sibling-resource", "mode": "write"}],
            "evidence_requirements": [
                {
                    "artifact_id": "sibling-evidence",
                    "relative_path": "sibling.md",
                    "media_type": "text/markdown",
                }
            ],
        }
    )
    values["steps"].append(second)
    return NormalizedPlan.model_validate(values)


def _repository_snapshot(_config, repo_id: str, *, require_clean: bool) -> RepositorySnapshot:
    filename = "fixture.md" if repo_id == "fixture-repo" else "sibling.md"
    return RepositorySnapshot(
        repo_id=repo_id,
        branch="main",
        revision="base-sha",
        worktree_id="b" * 64,
        remote_name="origin",
        remote_url="https://example.invalid/fixture.git",
        clean=True,
        evidence=(
            EvidenceManifestEntry(
                root="evidence",
                relative_path=f"evidence/{filename}",
                file_type="file",
                size_bytes=10,
                mode=0o644,
                mtime_ns=1,
                sha256="a" * 64,
            ),
        ),
        external=(),
        changes=(),
        manifest_sha256="c" * 64,
    )


def _failing_session_runner(**kwargs: Any):
    lifecycle = kwargs["lifecycle"]
    lifecycle.on_process_started(1000)
    lifecycle.on_session_identified(f"session-{json.loads(kwargs['prompt'])['dispatch_id']}")
    raise OpenCodeProcessError("deterministic batch child failure")


def _successful_session_runner(**kwargs: Any) -> SessionResult:
    lifecycle = kwargs["lifecycle"]
    prompt = json.loads(kwargs["prompt"])
    session_id = f"session-{prompt['dispatch_id']}"
    lifecycle.on_process_started(1000)
    lifecycle.on_session_identified(session_id)
    sibling = prompt["step_id"] == "prepare-sibling"
    return SessionResult(
        session_id=session_id,
        exit_code=0,
        evidence_written=[],
        chat_response=json.dumps(
            {
                "result_version": 1,
                "response_contract": "dispatcher.executor_result.v1",
                "dispatch_id": prompt["dispatch_id"],
                "attempt": prompt["attempt"],
                "step_id": prompt["step_id"],
                "repository": {
                    "repo_id": prompt["repo_id"],
                    "base_revision": "base-sha",
                    "result_revision": "base-sha",
                    "patch_sha256": None,
                },
                "evidence": [
                    {
                        "artifact_id": "sibling-evidence" if sibling else "fixture-evidence",
                        "relative_path": "sibling.md" if sibling else "fixture.md",
                        "sha256": "a" * 64,
                        "media_type": "text/markdown",
                        "size_bytes": 10,
                    }
                ],
                "verification": [
                    {"check_id": "fixture-check", "status": "passed", "summary": "passed"}
                ],
                "summary": "successful batch child",
                "outcome": "completed",
            }
        ),
    )


def _initialize_sibling_repository(path: Path) -> Path:
    (path / "evidence").mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(path)], check=True, capture_output=True, text=True, timeout=10)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/sibling.git"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return path


def _event(sequence: int) -> TransitionEvent:
    return TransitionEvent(
        event_id=f"event-{sequence}",
        sequence=sequence,
        actor="dispatcher",
        reason="batch fixture",
        correlation_id="batch-fixture",
        occurred_at=datetime.now(UTC),
    )
