from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from helpers import FixtureProject, create_fixture_project, valid_plan_values

from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.results import parse_executor_result, parse_reviewer_result
from dispatcher.sequential import (
    CompletionDecision,
    PreparedDispatch,
    SequentialWorkflow,
    SequentialWorkflowError,
)
from dispatcher.state_store import StateStore
from dispatcher.workflow import (
    RunRecord,
    StepStatus,
    TransitionEvent,
    new_run_record,
    transition_step,
)

_EVIDENCE_SHA = "a" * 64


@pytest.fixture
def project(tmp_path: Path) -> FixtureProject:
    return create_fixture_project(tmp_path)


def _event(sequence: int) -> TransitionEvent:
    return TransitionEvent(
        event_id=f"event-{sequence}",
        sequence=sequence,
        actor="dispatcher",
        reason="fixture workflow transition",
        correlation_id="fixture-correlation",
        occurred_at=datetime.now(UTC),
    )


def _store(project: FixtureProject) -> StateStore:
    return StateStore(
        project.state,
        heartbeat_seconds=project.config.lease_heartbeat_seconds,
        stale_after_seconds=project.config.lease_stale_after_seconds,
    )


def _record(project: FixtureProject, *, review_required: bool = False) -> RunRecord:
    values = valid_plan_values(project)
    if review_required:
        values["steps"][0]["review"] = {
            "required": True,
            "reviewer_role_keys": ["reviewer"],
            "required_acceptances": 1,
        }
        values["steps"][0]["retry"]["max_reviewer_attempts"] = 1
        values["steps"][0]["retry"]["max_executor_attempts"] = 2
        values["steps"][0]["retry"]["on_changes_requested"] = "retry"
    plan = NormalizedPlan.model_validate(values)
    record = new_run_record(
        run_id="fixture-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-approve-plan"),
        event=_event(1),
    )
    pending = record.steps["prepare-fixture"]
    ready = transition_step(pending, StepStatus.READY, _event(2))
    return record.model_copy(
        update={
            "steps": {"prepare-fixture": ready},
            "sequence": ready.last_event.sequence,
            "updated_at": ready.last_event.occurred_at,
        }
    )


def _workflow(project: FixtureProject, store: StateStore) -> SequentialWorkflow:
    return SequentialWorkflow(
        project.config,
        store,
        owner_id="fixture-owner",
        revision_resolver=lambda _workdir: "base-sha",
    )


def _dispatch_command(role: str = "terra", mode: str = "new") -> str:
    return json.dumps(
        {
            "protocol_version": 1,
            "action": "dispatch",
            "step_id": "prepare-fixture",
            "target_role": role,
            "session_mode": mode,
            "prompt": "Perform the approved fixture work.",
            "rationale": "fixture",
        }
    )


def _executor_result(prepared: PreparedDispatch, outcome: str = "completed") -> dict[str, Any]:
    result: dict[str, Any] = {
        "result_version": 1,
        "dispatch_id": prepared.dispatch.dispatch_id,
        "attempt": prepared.dispatch.attempt,
        "step_id": "prepare-fixture",
        "repository": {
            "repo_id": "fixture-repo",
            "base_revision": "base-sha",
            "result_revision": "base-sha",
            "patch_sha256": None,
        },
        "evidence": [
            {
                "artifact_id": "fixture-evidence",
                "relative_path": "fixture.md",
                "sha256": _EVIDENCE_SHA,
                "media_type": "text/markdown",
                "size_bytes": 10,
            }
        ],
        "verification": [{"check_id": "fixture-check", "status": "passed", "summary": "passed"}],
        "summary": "fixture executor result",
        "outcome": outcome,
    }
    if outcome == "failed":
        result["failure_code"] = "fixture-failure"
    if outcome == "blocked":
        result["blockers"] = ["fixture blocker"]
    return result


def _activate_ready_run(project: FixtureProject, *, review_required: bool = False) -> tuple[StateStore, SequentialWorkflow, RunRecord, int]:
    store = _store(project)
    record = _record(project, review_required=review_required)
    generation = store.create_run(record)
    workflow = _workflow(project, store)
    active, generation = workflow.activate(record.run_id, expected_generation=generation)
    return store, workflow, active, generation


def _prepare_executor(project: FixtureProject, *, review_required: bool = False) -> tuple[StateStore, SequentialWorkflow, PreparedDispatch]:
    _store_value, workflow, record, generation = _activate_ready_run(project, review_required=review_required)
    prepared = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(),
    )
    assert isinstance(prepared, PreparedDispatch)
    running = workflow.mark_running(prepared, process_id=1234)
    identified = workflow.record_session_id(running, runtime_session_id="ses-executor")
    return _store_value, workflow, identified


def test_bootstrap_is_self_contained_and_persisted(project: FixtureProject) -> None:
    store, workflow, record, _generation = _activate_ready_run(project)

    bootstrap, path = workflow.render_bootstrap(record.run_id)

    assert record.plan_digest in bootstrap
    assert "specification.md" in bootstrap
    assert "plan.md" in bootstrap
    assert "prepare-fixture" in bootstrap
    assert path.is_file()


def test_executor_dispatch_transitions_only_its_ready_step_and_completion_is_guarded(
    project: FixtureProject,
) -> None:
    store, workflow, prepared = _prepare_executor(project)

    record, generation, forwarding = workflow.apply_executor_result(
        prepared,
        parse_executor_result(_executor_result(prepared)),
    )

    assert record.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert "executor_result" in forwarding
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=prepared.dispatch.dispatch_id,
    )
    decision = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text='{"protocol_version":1,"action":"request_completion"}',
    )

    assert isinstance(decision, CompletionDecision)
    assert decision.accepted
    assert decision.report_path is not None
    assert store.load_run(record.run_id)[0].state.value == "SUCCEEDED"


def test_unknown_step_and_missing_session_fail_before_dispatch_persistence(project: FixtureProject) -> None:
    store, workflow, record, generation = _activate_ready_run(project)
    unknown = json.loads(_dispatch_command())
    unknown["step_id"] = "missing-step"

    with pytest.raises(SequentialWorkflowError, match="unknown plan step"):
        workflow.prepare_from_supervisor(
            record.run_id,
            expected_generation=generation,
            supervisor_text=json.dumps(unknown),
        )
    resume = json.loads(_dispatch_command(mode="resume"))
    with pytest.raises(SequentialWorkflowError, match="no dispatcher-owned session"):
        workflow.prepare_from_supervisor(
            record.run_id,
            expected_generation=generation,
            supervisor_text=json.dumps(resume),
        )

    assert store.load_run(record.run_id)[0].dispatches == {}


def test_supervisor_completion_request_cannot_bypass_unmet_obligations(project: FixtureProject) -> None:
    store, workflow, record, generation = _activate_ready_run(project)

    decision = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text='{"protocol_version":1,"action":"request_completion","rationale":"premature"}',
    )

    assert isinstance(decision, CompletionDecision)
    assert not decision.accepted
    assert any("step_not_accepted" in obligation for obligation in decision.obligations)
    assert store.load_run(record.run_id)[0].state.value == "RUNNING"


def test_failed_executor_result_never_advances_the_step(project: FixtureProject) -> None:
    _store_value, workflow, prepared = _prepare_executor(project)

    record, _generation, _forwarding = workflow.apply_executor_result(
        prepared,
        parse_executor_result(_executor_result(prepared, "failed")),
    )

    assert record.steps["prepare-fixture"].state is StepStatus.FAILED


def test_reviewer_acceptance_is_fresh_policy_bound_and_revision_bound(project: FixtureProject) -> None:
    _store_value, workflow, executor = _prepare_executor(project, review_required=True)
    record, generation, _forwarding = workflow.apply_executor_result(
        executor,
        parse_executor_result(_executor_result(executor)),
    )
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=executor.dispatch.dispatch_id,
    )
    prepared = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(role="reviewer"),
    )
    assert isinstance(prepared, PreparedDispatch)
    assert prepared.review_target is not None
    assert prepared.permission_config["permission"]["edit"] == "deny"
    running = workflow.mark_running(prepared, process_id=1235)
    reviewer = workflow.record_session_id(running, runtime_session_id="ses-reviewer")
    review_result = parse_reviewer_result(
        {
            "result_version": 1,
            "dispatch_id": reviewer.dispatch.dispatch_id,
            "attempt": reviewer.dispatch.attempt,
            "step_id": "prepare-fixture",
            "repo_id": "fixture-repo",
            "review_target": reviewer.review_target.model_dump(mode="json"),
            "findings": [],
            "verification": [{"check_id": "review-check", "status": "passed", "summary": "passed"}],
            "required_remediation": [],
            "summary": "fixture review",
            "verdict": "accepted",
        }
    )

    record, _generation, _forwarding = workflow.apply_reviewer_result(reviewer, review_result)

    assert record.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert record.steps["prepare-fixture"].review_acceptances == 1


def test_reviewer_changes_requested_returns_a_deterministic_rework_state(project: FixtureProject) -> None:
    _store_value, workflow, executor = _prepare_executor(project, review_required=True)
    record, generation, _forwarding = workflow.apply_executor_result(
        executor,
        parse_executor_result(_executor_result(executor)),
    )
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=executor.dispatch.dispatch_id,
    )
    prepared = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(role="reviewer"),
    )
    assert isinstance(prepared, PreparedDispatch)
    running = workflow.mark_running(prepared, process_id=1235)
    reviewer = workflow.record_session_id(running, runtime_session_id="ses-reviewer")
    review_result = parse_reviewer_result(
        {
            "result_version": 1,
            "dispatch_id": reviewer.dispatch.dispatch_id,
            "attempt": reviewer.dispatch.attempt,
            "step_id": "prepare-fixture",
            "repo_id": "fixture-repo",
            "review_target": reviewer.review_target.model_dump(mode="json"),
            "findings": [],
            "verification": [{"check_id": "review-check", "status": "passed", "summary": "passed"}],
            "required_remediation": ["repair fixture"],
            "summary": "fixture review",
            "verdict": "changes_requested",
        }
    )

    record, _generation, _forwarding = workflow.apply_reviewer_result(reviewer, review_result)

    assert record.steps["prepare-fixture"].state is StepStatus.READY
