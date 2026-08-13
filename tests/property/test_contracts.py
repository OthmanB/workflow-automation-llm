from __future__ import annotations

import copy
import json
import string
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import FixtureProject, config_values, valid_plan_values
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from dispatcher.config import ProjectConfigModel
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.protocol import ProtocolError, parse_supervisor_command
from dispatcher.results import parse_executor_result
from dispatcher.sequential import SequentialWorkflowError, _validate_result_verification
from dispatcher.verification import AuthoritativeVerification
from dispatcher.workflow import (
    STEP_TRANSITIONS,
    StepStatus,
    TransitionEvent,
    new_run_record,
    transition_step,
)


@pytest.fixture
def project(tmp_path: Path) -> FixtureProject:
    from helpers import create_fixture_project

    return create_fixture_project(tmp_path)


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=16))
def test_project_config_rejects_arbitrary_unknown_top_level_keys(project: FixtureProject, key: str) -> None:
    values = copy.deepcopy(config_values(project))
    if key in values:
        return
    values[key] = "unexpected"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProjectConfigModel.model_validate(values)


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=24))
def test_normalized_plan_rejects_unknown_dependency_references(project: FixtureProject, dependency: str) -> None:
    if dependency == "prepare-fixture":
        return
    values = copy.deepcopy(valid_plan_values(project))
    values["steps"][0]["depends_on"] = [dependency]

    with pytest.raises(ValidationError, match="unknown step"):
        NormalizedPlan.model_validate(values)


@settings(max_examples=100)
@given(
    st.recursive(
        st.none() | st.booleans() | st.integers() | st.text(max_size=100),
        lambda children: st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=12), children, max_size=5),
        max_leaves=20,
    )
)
def test_protocol_parser_never_routes_arbitrary_json(payload: object) -> None:
    try:
        command = parse_supervisor_command(json.dumps(payload))
    except ProtocolError:
        return

    assert command.action in {
        "dispatch",
        "dispatch_batch",
        "ask_operator",
        "request_review_waiver",
        "halt",
        "request_completion",
    }


@settings(max_examples=80, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    source=st.sampled_from([StepStatus.PENDING, StepStatus.READY, StepStatus.BLOCKED, StepStatus.FAILED]),
    target=st.sampled_from([StepStatus.PENDING, StepStatus.READY, StepStatus.BLOCKED, StepStatus.FAILED]),
)
def test_step_transition_table_rejects_every_invalid_generated_edge(
    project: FixtureProject,
    source: StepStatus,
    target: StepStatus,
) -> None:
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = new_run_record(
        run_id="property-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-property"),
        event=TransitionEvent(
            event_id="event-property",
            sequence=1,
            actor="dispatcher",
            reason="property fixture",
            correlation_id="property-run",
            occurred_at=datetime.now(UTC),
        ),
    )
    step = record.steps["prepare-fixture"].model_copy(update={"state": source})
    event = TransitionEvent(
        event_id="event-property-transition",
        sequence=2,
        actor="dispatcher",
        reason="property transition",
        correlation_id="prepare-fixture",
        occurred_at=datetime.now(UTC),
    )

    if target in STEP_TRANSITIONS[source]:
        assert transition_step(step, target, event).state is target
    else:
        with pytest.raises(ValueError, match="invalid step transition"):
            transition_step(step, target, event)


@settings(max_examples=80, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    verification=st.lists(
        st.tuples(
            st.sampled_from(["fixture-check", "unknown-check"]),
            st.sampled_from(["passed", "failed", "skipped"]),
        ),
        max_size=4,
    )
)
def test_completed_result_context_requires_exact_unique_all_passed_verification(
    project: FixtureProject,
    verification: list[tuple[str, str]],
) -> None:
    step = NormalizedPlan.model_validate(valid_plan_values(project)).steps[0]
    result = parse_executor_result(_executor_payload(verification, outcome="completed"))
    valid = verification == [("fixture-check", "passed")]
    authoritative = (
        AuthoritativeVerification(
            check_id="fixture-check",
            status="passed",
            argv=("property-check",),
            exit_code=0,
            timed_out=False,
            output_truncated=False,
            stdout_sha256="0" * 64,
            stderr_sha256="0" * 64,
            transcript_sha256="0" * 64,
            duration_ms=0,
            backend="property-test",
            summary="property verification passed",
        ),
    )

    if valid:
        _validate_result_verification(step, result, authoritative)
    else:
        with pytest.raises(SequentialWorkflowError):
            _validate_result_verification(step, result)


@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(status=st.sampled_from(["passed", "failed", "skipped"]))
def test_blocked_result_context_allows_any_status_only_with_exact_coverage(
    project: FixtureProject,
    status: str,
) -> None:
    step = NormalizedPlan.model_validate(valid_plan_values(project)).steps[0]
    result = parse_executor_result(
        _executor_payload([("fixture-check", status)], outcome="blocked")
    )

    _validate_result_verification(step, result)


def _executor_payload(
    verification: list[tuple[str, str]],
    *,
    outcome: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "result_version": 1,
        "response_contract": "dispatcher.executor_result.v1",
        "dispatch_id": "dispatch-property",
        "attempt": 1,
        "step_id": "prepare-fixture",
        "repository": {
            "repo_id": "fixture-repo",
            "base_revision": "base-sha",
            "result_revision": "result-sha",
            "patch_sha256": None,
        },
        "evidence": [],
        "verification": [
            {"check_id": check_id, "status": status, "summary": "property check"}
            for check_id, status in verification
        ],
        "summary": "property result",
        "outcome": outcome,
    }
    if outcome == "blocked":
        payload["blockers"] = ["property blocker"]
    return payload
