from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import FixtureProject, create_fixture_project, valid_plan_values

from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.state import load_run_record, save_run_record
from dispatcher.workflow import (
    ACTIVE_DISPATCH_STATES,
    DISPATCH_TRANSITIONS,
    RUN_TRANSITIONS,
    STEP_TRANSITIONS,
    DispatchIntent,
    DispatchRecord,
    DispatchStatus,
    OperatorRequest,
    RepositoryCoordinate,
    RunStatus,
    StepStatus,
    TransitionError,
    TransitionEvent,
    completion_obligations,
    new_run_record,
    terminal_exit_code,
    transition_dispatch,
    transition_run,
    transition_step,
)

_DIGEST = "c" * 64


@pytest.fixture
def project(tmp_path: Path) -> FixtureProject:
    return create_fixture_project(tmp_path)


def _event(sequence: int, reason: str = "fixture transition") -> TransitionEvent:
    return TransitionEvent(
        event_id=f"event-{sequence}",
        sequence=sequence,
        actor="dispatcher",
        reason=reason,
        correlation_id="fixture-correlation",
        occurred_at=datetime.now(UTC),
    )


def _record(project: FixtureProject):
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    return new_run_record(
        run_id="fixture-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-approve-plan"),
        event=_event(1),
    )


def _dispatch_record() -> DispatchRecord:
    return DispatchRecord(
        dispatch_id="dispatch-one",
        step_id="prepare-fixture",
        role_key="terra",
        role_kind="executor",
        attempt=1,
        logical_session_key="session-one",
        runtime_session_id=None,
        state=DispatchStatus.PREPARED,
        intent=DispatchIntent(
            prompt_sha256=_DIGEST,
            policy_digest=_DIGEST,
            expected_result_kind="executor",
            repository=RepositoryCoordinate(repo_id="fixture-repo", base_revision="base-sha"),
            idempotency_key="idempotency-one",
        ),
        result_digest=None,
        forwarding_digest=None,
        last_event=_event(1),
    )


def test_transition_tables_cover_declared_states() -> None:
    assert set(RUN_TRANSITIONS) == set(RunStatus)
    assert set(STEP_TRANSITIONS) == set(StepStatus)
    assert set(DISPATCH_TRANSITIONS) == set(DispatchStatus)
    assert DispatchStatus.PREPARED in ACTIVE_DISPATCH_STATES


def test_run_waiting_operator_is_non_terminal_and_requires_durable_request(
    project: FixtureProject,
) -> None:
    record = transition_run(_record(project), RunStatus.READY, _event(2))
    request = OperatorRequest(
        request_id="request-one",
        question="Choose the fixture option",
        allowed_answers=["approve", "halt"],
        context_ref="context-one",
        resume_to=RunStatus.READY,
        expires_at=None,
        required_role=None,
    )

    waiting = transition_run(record, RunStatus.WAITING_OPERATOR, _event(3), operator_request=request)
    resumed = transition_run(waiting, RunStatus.READY, _event(4))

    assert waiting.operator_request == request
    assert resumed.operator_request is None
    with pytest.raises(TransitionError, match="WAITING_OPERATOR requires operator_request"):
        transition_run(record, RunStatus.WAITING_OPERATOR, _event(5))


def test_step_and_dispatch_allowed_and_rejected_transitions(project: FixtureProject) -> None:
    record = _record(project)
    step = record.steps["prepare-fixture"]
    ready = transition_step(step, StepStatus.READY, _event(2))
    executing = transition_step(ready, StepStatus.EXECUTING, _event(3), active_dispatch_id="dispatch-one")
    executed = transition_step(executing, StepStatus.EXECUTED, _event(4))

    assert executed.state is StepStatus.EXECUTED
    with pytest.raises(TransitionError, match="invalid step transition"):
        transition_step(step, StepStatus.ACCEPTED, _event(5))

    dispatch = _dispatch_record()
    running = transition_dispatch(
        dispatch,
        DispatchStatus.RUNNING,
        _event(2),
    )
    assert running.runtime_session_id is None
    identified = running.model_copy(update={"runtime_session_id": "runtime-session-one"})
    completed = transition_dispatch(
        identified,
        DispatchStatus.COMPLETED,
        _event(3),
        result_digest=_DIGEST,
    )
    forwarded = transition_dispatch(
        completed,
        DispatchStatus.FORWARDED,
        _event(4),
        forwarding_digest=_DIGEST,
    )

    assert forwarded.state is DispatchStatus.FORWARDED
    with pytest.raises(TransitionError, match="runtime_session_id"):
        transition_dispatch(running, DispatchStatus.COMPLETED, _event(5), result_digest=_DIGEST)
    with pytest.raises(TransitionError, match="invalid dispatch transition"):
        transition_dispatch(dispatch, DispatchStatus.COMPLETED, _event(6), result_digest=_DIGEST)


def test_completion_guard_returns_structured_unmet_obligations(project: FixtureProject) -> None:
    record = transition_run(_record(project), RunStatus.READY, _event(2))
    running = transition_run(record, RunStatus.RUNNING, _event(3))

    obligations = completion_obligations(running)

    assert obligations[0].code == "step_not_accepted"
    with pytest.raises(TransitionError, match="completion denied"):
        transition_run(running, RunStatus.SUCCEEDED, _event(4))


def test_completion_guard_requires_evidence_and_no_active_dispatch(project: FixtureProject) -> None:
    record = transition_run(_record(project), RunStatus.READY, _event(2))
    running = transition_run(record, RunStatus.RUNNING, _event(3))
    accepted_step = running.steps["prepare-fixture"].model_copy(
        update={"state": StepStatus.ACCEPTED}
    )
    with_accepted_step = running.model_copy(
        update={"steps": {"prepare-fixture": accepted_step}}
    )

    codes = {obligation.code for obligation in completion_obligations(with_accepted_step)}

    assert codes == {"evidence_missing"}
    completed_step = accepted_step.model_copy(update={"accepted_artifact_ids": ["fixture-evidence"]})
    active = with_accepted_step.model_copy(
        update={
            "steps": {"prepare-fixture": completed_step},
            "dispatches": {"dispatch-one": _dispatch_record()},
        }
    )
    assert {obligation.code for obligation in completion_obligations(active)} == {"dispatch_in_flight"}


def test_run_record_persists_normalized_plan_and_digest(project: FixtureProject) -> None:
    record = _record(project)

    save_run_record(project.state, record)
    restored = load_run_record(project.state)

    assert restored == record
    assert restored is not None
    assert restored.plan_digest == record.plan.plan_digest


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RunStatus.SUCCEEDED, 0),
        (RunStatus.HALTED, 1),
        (RunStatus.CANCELLED, 1),
        (RunStatus.FAILED, 2),
    ],
)
def test_terminal_exit_codes(status: RunStatus, expected: int) -> None:
    assert terminal_exit_code(status) == expected


def test_nonterminal_exit_code_is_rejected() -> None:
    with pytest.raises(TransitionError, match="not terminal"):
        terminal_exit_code(RunStatus.RUNNING)
