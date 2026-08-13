from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import create_fixture_project, valid_plan_values

from dispatcher.baseline import (
    BaselineDecision,
    approve_baseline,
    create_run_from_baseline,
    inspect_baseline,
)
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.repository import EvidenceManifestEntry, RepositorySnapshot
from dispatcher.results import ExecutorCompletedResult
from dispatcher.sequential import PreparedDispatch, SequentialWorkflow, SequentialWorkflowError
from dispatcher.state_store import StateStore
from dispatcher.workflow import StepStatus, TransitionEvent


def test_baseline_backed_run_dispatches_only_the_first_dependency_ready_pending_step(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    historical_evidence = project.evidence / "historical.md"
    historical_evidence.write_text("historical accepted evidence\n", encoding="utf-8")
    plan = _adoption_plan(project)
    store = StateStore(
        project.state,
        heartbeat_seconds=project.config.lease_heartbeat_seconds,
        stale_after_seconds=project.config.lease_stale_after_seconds,
    )
    observation = inspect_baseline(plan, project.config)
    approval = approve_baseline(
        observation,
        decisions=(
            _decision("historical-accepted", "ACCEPTED"),
            _decision("historical-waived", "WAIVED"),
            _decision("pending-final", "PENDING"),
        ),
        plan=plan,
        config=project.config,
        store=store,
        approval_decision_ref="decision-adopt-sanitized-fixture",
    )
    record = create_run_from_baseline(
        run_id="baseline-adoption-run",
        plan=plan,
        config=project.config,
        plan_approval=approve_plan(plan, "decision-plan-adoption"),
        baseline_approval=approval,
        event=_event(1),
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(
        project.config,
        store,
        owner_id="baseline-adoption-owner",
        repository_inspector=_repository_snapshot,
    )

    assert record.steps["historical-accepted"].state is StepStatus.ACCEPTED
    assert record.steps["historical-waived"].state is StepStatus.WAIVED
    assert record.steps["pending-final"].state is StepStatus.PENDING

    active, generation = workflow.activate(record.run_id, expected_generation=generation)
    assert active.steps["pending-final"].state is StepStatus.READY
    with pytest.raises(SequentialWorkflowError, match="not READY"):
        workflow.prepare_from_supervisor(
            active.run_id,
            expected_generation=generation,
            supervisor_text=_dispatch_command("historical-accepted"),
        )

    prepared = workflow.prepare_from_supervisor(
        active.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command("pending-final"),
    )
    assert isinstance(prepared, PreparedDispatch)
    running = workflow.record_session_id(
        workflow.mark_running(prepared, process_id=3001, process_create_time=3001.0),
        runtime_session_id="session-baseline-pending",
    )
    completed = ExecutorCompletedResult.model_validate(
        {
            "result_version": 1,
            "response_contract": "dispatcher.executor_result.v1",
            "dispatch_id": running.dispatch.dispatch_id,
            "attempt": running.dispatch.attempt,
            "step_id": "pending-final",
            "repository": {
                "repo_id": "fixture-repo",
                "base_revision": "base-sha",
                "result_revision": "base-sha",
                "patch_sha256": None,
            },
            "evidence": [
                {
                    "artifact_id": "pending-evidence",
                    "relative_path": "pending.md",
                    "sha256": "b" * 64,
                    "media_type": "text/markdown",
                    "size_bytes": 10,
                }
            ],
            "verification": [{"check_id": "pending-check", "status": "passed", "summary": "passed"}],
            "summary": "completed sanitized pending step",
            "outcome": "completed",
        }
    )
    completed_record, generation, _forwarding = workflow.apply_executor_result(running, completed)
    acknowledged, generation = workflow.acknowledge_forwarding(
        completed_record.run_id,
        expected_generation=generation,
        dispatch_id=running.dispatch.dispatch_id,
    )
    decision = workflow.evaluate_completion(acknowledged, generation)

    assert acknowledged.steps["pending-final"].state is StepStatus.ACCEPTED
    assert decision.accepted is True


def _adoption_plan(project) -> NormalizedPlan:
    values = valid_plan_values(project)
    first = values["steps"][0]
    first.update(
        {
            "step_id": "historical-accepted",
            "title": "Historical accepted",
            "produced_outputs": [
                {
                    "artifact_id": "accepted-output",
                    "producer_step_id": None,
                    "description": "Historical accepted output",
                }
            ],
            "evidence_requirements": [
                {
                    "artifact_id": "historical-evidence",
                    "relative_path": "historical.md",
                    "media_type": "text/markdown",
                }
            ],
            "resource_locks": [{"resource_id": "historical-resource", "mode": "write"}],
        }
    )
    waived = json.loads(json.dumps(first))
    waived.update(
        {
            "ordinal": 2,
            "step_id": "historical-waived",
            "title": "Historical waived",
            "produced_outputs": [
                {
                    "artifact_id": "waived-output",
                    "producer_step_id": None,
                    "description": "Historical waived output",
                }
            ],
            "evidence_requirements": [
                {
                    "artifact_id": "waived-evidence",
                    "relative_path": "waived.md",
                    "media_type": "text/markdown",
                }
            ],
            "resource_locks": [{"resource_id": "waived-resource", "mode": "write"}],
        }
    )
    pending = json.loads(json.dumps(first))
    pending.update(
        {
            "ordinal": 3,
            "step_id": "pending-final",
            "title": "Pending final",
            "depends_on": ["historical-accepted", "historical-waived"],
            "required_inputs": [
                {
                    "artifact_id": "accepted-output",
                    "producer_step_id": "historical-accepted",
                    "description": "Accepted historical output",
                },
                {
                    "artifact_id": "waived-output",
                    "producer_step_id": "historical-waived",
                    "description": "Waived historical output",
                },
            ],
            "produced_outputs": [
                {
                    "artifact_id": "pending-output",
                    "producer_step_id": None,
                    "description": "Pending output",
                }
            ],
            "evidence_requirements": [
                {
                    "artifact_id": "pending-evidence",
                    "relative_path": "pending.md",
                    "media_type": "text/markdown",
                }
            ],
            "acceptance_criteria": [
                {
                    "criterion_id": "pending-check",
                    "description": "Verify the pending output.",
                    "check": {
                        "argv": ["python", "-c", "print('pending')"],
                        "working_directory": "repository",
                        "timeout_seconds": 30,
                        "max_output_bytes": 65536,
                        "expected_exit_codes": [0],
                        "network_policy": "deny",
                    },
                }
            ],
            "resource_locks": [{"resource_id": "pending-resource", "mode": "write"}],
        }
    )
    values["steps"] = [first, waived, pending]
    return NormalizedPlan.model_validate(values)


def _decision(step_id: str, state: str) -> BaselineDecision:
    return BaselineDecision(
        step_id=step_id,
        state=state,  # type: ignore[arg-type]
        reason=f"sanitized adoption fixture decision: {state}",
        operator_decision_ref=f"decision-{step_id}-{state.lower()}",
    )


def _dispatch_command(step_id: str) -> str:
    return json.dumps(
        {
            "protocol_version": 1,
            "action": "dispatch",
            "step_id": step_id,
            "target_role": "terra",
            "session_mode": "new",
            "prompt": "complete the sanitized pending step",
        }
    )


def _repository_snapshot(_config, repo_id: str, *, require_clean: bool) -> RepositorySnapshot:
    evidence = (
        EvidenceManifestEntry(
            root="evidence",
            relative_path="evidence/historical.md",
            file_type="file",
            size_bytes=10,
            mode=0o644,
            mtime_ns=1,
            sha256="a" * 64,
        ),
        EvidenceManifestEntry(
            root="evidence",
            relative_path="evidence/pending.md",
            file_type="file",
            size_bytes=10,
            mode=0o644,
            mtime_ns=1,
            sha256="b" * 64,
        ),
    )
    return RepositorySnapshot(
        repo_id=repo_id,
        branch="main",
        revision="base-sha",
        worktree_id="c" * 64,
        remote_name="origin",
        remote_url="https://example.invalid/fixture.git",
        clean=True,
        evidence=evidence,
        external=(),
        changes=(),
        ignored=(),
        dirty_patch_sha256="a" * 64,
        git_metadata_sha256="e" * 64,
        git_refs_sha256="f" * 64,
        manifest_sha256="d" * 64,
    )


def _event(sequence: int) -> TransitionEvent:
    return TransitionEvent(
        event_id=f"event-baseline-adoption-{sequence}",
        sequence=sequence,
        actor="dispatcher",
        reason="sanitized baseline adoption fixture",
        correlation_id="baseline-adoption-run",
        occurred_at=datetime.now(UTC),
    )
