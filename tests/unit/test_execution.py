from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
from helpers import create_fixture_project, valid_plan_values

from dispatcher.execution import (
    ExecutionCoordinatorError,
    SequentialExecutionCoordinator,
    SupervisorOutcome,
    _worker_failure,
    _worker_json_object,
    worker_opencode_state_dir,
)
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.protocol import ProtocolError
from dispatcher.repository import (
    EvidenceManifestEntry,
    RepositorySnapshot,
    RepositoryValidationError,
)
from dispatcher.results import ResultError
from dispatcher.sequential import (
    PreparedDispatch,
    SequentialWorkflow,
    SequentialWorkflowError,
    WorkerResultValidationError,
)
from dispatcher.sessions import OpenCodeAdapterError, SessionResult
from dispatcher.state_store import DispatchPayload, StateStore
from dispatcher.workflow import (
    DispatchIntent,
    DispatchRecord,
    DispatchStatus,
    RepositoryCoordinate,
    RunRecord,
    RunStatus,
    TransitionEvent,
    new_run_record,
)

_DIGEST = "a" * 64


@pytest.mark.parametrize(
    "response",
    [
        'Review complete.\n\n{"verdict":"accepted"}',
        'Review complete.\n\n```json\n{"verdict":"accepted"}\n```',
    ],
)
def test_worker_response_extracts_one_final_json_object(response: str) -> None:
    assert _worker_json_object(response) == {"verdict": "accepted"}


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ('{"first":1}\n{"second":2}', "another JSON object"),
        ('{"value":1} trailing', "exactly one final JSON object"),
        ('Review\n```json\n{"value":1}\n``` trailing', "malformed or non-final"),
        ('Review\n```\n{"value":1}\n```', "at most one final JSON Markdown fence"),
        ('Review\n```json\n{"value":1}\n```\n```', "at most one final JSON Markdown fence"),
        ('Review\n{"value":NaN}', "exactly one final JSON object"),
        ('Review\n{"value":1,"value":2}', "exactly one final JSON object"),
    ],
)
def test_worker_response_rejects_ambiguous_or_malformed_json(
    response: str,
    message: str,
) -> None:
    with pytest.raises(ExecutionCoordinatorError, match=message):
        _worker_json_object(response)


def _dispatch(
    dispatch_id: str,
    *,
    step_id: str = "prepare-fixture",
    role_key: str = "terra",
    role_kind: Literal["executor", "reviewer"] = "executor",
    attempt: int = 1,
    batch_id: str | None = None,
) -> DispatchRecord:
    return DispatchRecord(
        dispatch_id=dispatch_id,
        batch_id=batch_id,
        step_id=step_id,
        role_key=role_key,
        role_kind=role_kind,
        attempt=attempt,
        logical_session_key=f"{role_kind}-{role_key}-{step_id}",
        runtime_session_id=None,
        state=DispatchStatus.PREPARED,
        intent=DispatchIntent(
            prompt_sha256=_DIGEST,
            policy_digest=_DIGEST,
            expected_result_kind=role_kind,
            repository=RepositoryCoordinate(repo_id="fixture-repo", base_revision="base-sha"),
            idempotency_key=f"idempotency-{dispatch_id}",
        ),
        result_digest=None,
        forwarding_digest=None,
        last_event=TransitionEvent(
            event_id=f"event-{dispatch_id}",
            sequence=attempt,
            actor="dispatcher",
            reason="fixture dispatch",
            correlation_id=dispatch_id,
            occurred_at=datetime.now(UTC),
        ),
    )


def test_worker_opencode_state_dir_reuses_home_for_sequential_rework(tmp_path: Path) -> None:
    initial = _dispatch("dispatch-initial", attempt=1)
    rework = _dispatch("dispatch-rework", attempt=2)

    initial_path = worker_opencode_state_dir(tmp_path / "state", run_id="run-one", dispatch=initial)
    rework_path = worker_opencode_state_dir(tmp_path / "state", run_id="run-one", dispatch=rework)

    assert initial_path == rework_path
    assert initial_path == tmp_path / "state" / "opencode-dispatches" / "run-one" / "executors" / "terra"


def test_worker_opencode_state_dir_preserves_run_role_and_batch_step_isolation(tmp_path: Path) -> None:
    executor = _dispatch("dispatch-executor")
    reviewer = _dispatch("dispatch-reviewer", role_key="reviewer", role_kind="reviewer")
    batch_first = _dispatch("dispatch-batch-first", batch_id="batch-one")
    batch_second = _dispatch("dispatch-batch-second", step_id="prepare-second", batch_id="batch-one")

    executor_path = worker_opencode_state_dir(tmp_path / "state", run_id="run-one", dispatch=executor)
    reviewer_path = worker_opencode_state_dir(tmp_path / "state", run_id="run-one", dispatch=reviewer)
    other_run_path = worker_opencode_state_dir(tmp_path / "state", run_id="run-two", dispatch=executor)
    batch_first_path = worker_opencode_state_dir(tmp_path / "state", run_id="run-one", dispatch=batch_first)
    batch_second_path = worker_opencode_state_dir(tmp_path / "state", run_id="run-one", dispatch=batch_second)

    assert executor_path != reviewer_path
    assert executor_path != other_run_path
    assert batch_first_path != batch_second_path


def test_continuation_without_pending_forwardings_preserves_bootstrap(tmp_path: Path) -> None:
    coordinator, _store, _workflow, record, _generation = _continuation_fixture(
        tmp_path,
        [],
    )

    prompt, pending = coordinator._continuation_prompt("exact bootstrap", record)

    assert prompt == "exact bootstrap"
    assert pending == []


def test_continuation_replays_one_authoritative_sanitized_executor_forwarding(
    tmp_path: Path,
) -> None:
    raw = json.dumps(
        {
            "kind": "executor_result",
            "dispatch_id": "dispatch-executor",
            "summary": "Bearer private-value",
        }
    )
    coordinator, store, _workflow, record, _generation = _continuation_fixture(
        tmp_path,
        [("dispatch-executor", "executor", DispatchStatus.FORWARDED, 20, raw)],
    )

    prompt, pending = coordinator._continuation_prompt("bootstrap", record)
    envelope = json.loads(prompt)
    stored = store.load_dispatch_payload(record.run_id, "dispatch-executor")

    assert pending == ["dispatch-executor"]
    assert envelope == {
        "kind": "orchestration_resume",
        "bootstrap": "bootstrap",
        "pending_forwardings": [
            {
                "dispatch_id": "dispatch-executor",
                "payload": {
                    "kind": "executor_result",
                    "dispatch_id": "dispatch-executor",
                    "summary": "Bearer [REDACTED]",
                },
            }
        ],
    }
    assert stored.forwarding_payload is not None
    assert "private-value" not in prompt
    assert "private-value" not in stored.forwarding_payload


def test_continuation_accepts_reviewer_forwarding_kind(tmp_path: Path) -> None:
    raw = json.dumps(
        {
            "kind": "reviewer_result",
            "dispatch_id": "dispatch-reviewer",
            "verdict": "accepted",
        }
    )
    coordinator, _store, _workflow, record, _generation = _continuation_fixture(
        tmp_path,
        [("dispatch-reviewer", "reviewer", DispatchStatus.FORWARDED, 20, raw)],
    )

    prompt, pending = coordinator._continuation_prompt("bootstrap", record)

    assert pending == ["dispatch-reviewer"]
    assert json.loads(prompt)["pending_forwardings"][0]["payload"]["kind"] == "reviewer_result"


def test_continuation_order_is_stable_across_dispatch_insertion_orders(
    tmp_path: Path,
) -> None:
    values = {
        dispatch_id: json.dumps(
            {"kind": "executor_result", "dispatch_id": dispatch_id}
        )
        for dispatch_id in ("dispatch-a", "dispatch-b", "dispatch-c")
    }
    first_entries = [
        ("dispatch-b", "executor", DispatchStatus.FORWARDED, 20, values["dispatch-b"]),
        ("dispatch-c", "executor", DispatchStatus.FORWARDED, 10, values["dispatch-c"]),
        ("dispatch-a", "executor", DispatchStatus.FORWARDED, 20, values["dispatch-a"]),
    ]
    first, _store, _workflow, first_record, _generation = _continuation_fixture(
        tmp_path / "first",
        first_entries,
    )
    second, _store, _workflow, second_record, _generation = _continuation_fixture(
        tmp_path / "second",
        list(reversed(first_entries)),
    )

    first_prompt, first_pending = first._continuation_prompt("bootstrap", first_record)
    second_prompt, second_pending = second._continuation_prompt("bootstrap", second_record)

    expected = ["dispatch-c", "dispatch-a", "dispatch-b"]
    assert first_pending == expected
    assert second_pending == expected
    assert first_prompt == second_prompt
    forwarded_ids = [
        item["dispatch_id"]
        for item in json.loads(first_prompt)["pending_forwardings"]
    ]
    assert forwarded_ids == expected
    assert len(forwarded_ids) == len(set(forwarded_ids))


def test_continuation_excludes_acknowledged_dispatches(tmp_path: Path) -> None:
    forwarded = json.dumps(
        {"kind": "executor_result", "dispatch_id": "dispatch-forwarded"}
    )
    acknowledged = json.dumps(
        {"kind": "executor_result", "dispatch_id": "dispatch-acknowledged"}
    )
    coordinator, _store, _workflow, record, _generation = _continuation_fixture(
        tmp_path,
        [
            ("dispatch-acknowledged", "executor", DispatchStatus.ACKNOWLEDGED, 5, acknowledged),
            ("dispatch-forwarded", "executor", DispatchStatus.FORWARDED, 10, forwarded),
        ],
    )

    prompt, pending = coordinator._continuation_prompt("bootstrap", record)

    assert pending == ["dispatch-forwarded"]
    assert "dispatch-acknowledged" not in prompt


@pytest.mark.parametrize(
    "state",
    [
        DispatchStatus.PREPARED,
        DispatchStatus.RUNNING,
        DispatchStatus.COMPLETED,
        DispatchStatus.ACKNOWLEDGED,
        DispatchStatus.FAILED,
        DispatchStatus.ABANDONED,
    ],
)
def test_continuation_ignores_every_non_forwarded_dispatch_state(
    tmp_path: Path,
    state: DispatchStatus,
) -> None:
    coordinator, _store, _workflow, record, _generation = _continuation_fixture(
        tmp_path,
        [("dispatch-excluded", "executor", state, 10, "not JSON")],
    )

    prompt, pending = coordinator._continuation_prompt("bootstrap", record)

    assert prompt == "bootstrap"
    assert pending == []


@pytest.mark.parametrize(
    ("forwarding", "message"),
    [
        (None, "is missing"),
        ("  \n", "is empty"),
        ("{", "not one strict JSON object"),
        (
            '```json\n{"kind":"executor_result","dispatch_id":"dispatch-corrupt"}\n```',
            "not one strict JSON object",
        ),
        (
            'result: {"kind":"executor_result","dispatch_id":"dispatch-corrupt"}',
            "not one strict JSON object",
        ),
        (
            '{"kind":"executor_result","dispatch_id":"dispatch-corrupt",'
            '"dispatch_id":"dispatch-corrupt"}',
            "duplicate JSON key",
        ),
        (
            '{"kind":"executor_result","dispatch_id":"dispatch-corrupt",'
            '"usage":NaN}',
            "non-finite JSON number",
        ),
        (
            '{"kind":"executor_result","dispatch_id":"dispatch-other"}',
            "identity does not match",
        ),
        (
            '{"kind":"reviewer_result","dispatch_id":"dispatch-corrupt"}',
            "kind does not match executor",
        ),
    ],
    ids=[
        "missing",
        "empty",
        "malformed",
        "markdown",
        "surrounding-prose",
        "duplicate-key",
        "non-finite-number",
        "wrong-identity",
        "wrong-kind",
    ],
)
def test_continuation_rejects_corrupt_stored_forwarding(
    tmp_path: Path,
    forwarding: str | None,
    message: str,
) -> None:
    coordinator, _store, _workflow, record, _generation = _continuation_fixture(
        tmp_path,
        [("dispatch-corrupt", "executor", DispatchStatus.FORWARDED, 10, forwarding)],
    )

    with pytest.raises(ExecutionCoordinatorError, match=message):
        coordinator._continuation_prompt("bootstrap", record)


def test_invalid_pending_payload_prevents_all_acknowledgements_and_supervisor_delivery(
    tmp_path: Path,
) -> None:
    valid = json.dumps(
        {"kind": "executor_result", "dispatch_id": "dispatch-valid"}
    )
    coordinator, store, _workflow, record, generation = _continuation_fixture(
        tmp_path,
        [
            ("dispatch-valid", "executor", DispatchStatus.FORWARDED, 10, valid),
            ("dispatch-invalid", "executor", DispatchStatus.FORWARDED, 20, "{"),
        ],
    )
    supervisor_called = False

    def supervisor_turn(**_kwargs: Any) -> SupervisorOutcome:
        nonlocal supervisor_called
        supervisor_called = True
        raise AssertionError("corrupt forwarding reached the supervisor")

    coordinator.run_supervisor_turn = supervisor_turn  # type: ignore[method-assign]

    with pytest.raises(ExecutionCoordinatorError, match="not one strict JSON object"):
        coordinator.run_to_completion(record.run_id, expected_generation=generation)

    persisted, _generation = store.load_run(record.run_id)
    assert supervisor_called is False
    assert {
        dispatch.state for dispatch in persisted.dispatches.values()
    } == {DispatchStatus.FORWARDED}


def test_run_to_completion_returns_immediately_after_terminal_worker_failure(
    tmp_path: Path,
) -> None:
    project = create_fixture_project(tmp_path)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = new_run_record(
        run_id="terminal-worker-failure",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-terminal-worker-failure"),
        event=TransitionEvent(
            event_id="event-terminal-worker-failure",
            sequence=1,
            actor="dispatcher",
            reason="terminal worker failure fixture",
            correlation_id="terminal-worker-failure",
            occurred_at=datetime.now(UTC),
        ),
    )
    store = StateStore(
        project.state,
        heartbeat_seconds=project.config.lease_heartbeat_seconds,
        stale_after_seconds=project.config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(
        project.config,
        store,
        owner_id="terminal-worker-failure-owner",
        repository_inspector=lambda _config, _repo_id, require_clean: _repository_snapshot(),
    )
    coordinator = SequentialExecutionCoordinator(
        project.config,
        store,
        workflow,
        owner_id="terminal-worker-failure-owner",
        session_runner=_result_runner(outcome="failed"),
    )
    prompts: list[str] = []

    def supervisor_turn(_run_id: str, **kwargs: Any) -> SupervisorOutcome:
        prompts.append(kwargs["prompt"])
        return SupervisorOutcome(
            json.dumps(
                {
                    "protocol_version": 1,
                    "action": "dispatch",
                    "step_id": "prepare-fixture",
                    "target_role": "terra",
                    "session_mode": "new",
                    "prompt": "Perform the approved fixture work.",
                }
            ),
            "supervisor-terminal-failure",
            kwargs["expected_generation"],
        )

    coordinator.run_supervisor_turn = supervisor_turn  # type: ignore[method-assign]

    decision = coordinator.run_to_completion(
        record.run_id,
        expected_generation=generation,
        max_turns=1,
    )
    persisted, _generation = store.load_run(record.run_id)

    assert decision.accepted is False
    assert decision.obligations == ("run stopped by policy: FAILED",)
    assert persisted.state is RunStatus.FAILED
    assert len(prompts) == 1
    assert all("completion_denied" not in prompt for prompt in prompts)


def test_successful_supervisor_receipt_acknowledges_then_refreshes_and_processes_command(
    tmp_path: Path,
) -> None:
    raw = json.dumps(
        {"kind": "executor_result", "dispatch_id": "dispatch-timing"}
    )
    coordinator, store, workflow, record, generation = _continuation_fixture(
        tmp_path,
        [("dispatch-timing", "executor", DispatchStatus.FORWARDED, 10, raw)],
    )
    events: list[str] = []
    original_acknowledge = workflow.acknowledge_forwarding
    original_refresh = workflow.refresh_readiness
    original_prepare = workflow.prepare_from_supervisor

    def acknowledge(run_id: str, *, expected_generation: int, dispatch_id: str):
        events.append("acknowledge")
        return original_acknowledge(
            run_id,
            expected_generation=expected_generation,
            dispatch_id=dispatch_id,
        )

    def refresh(current: RunRecord, current_generation: int):
        events.append("refresh")
        return original_refresh(current, current_generation)

    def prepare(run_id: str, *, expected_generation: int, supervisor_text: str):
        events.append("prepare")
        return original_prepare(
            run_id,
            expected_generation=expected_generation,
            supervisor_text=supervisor_text,
        )

    def supervisor_turn(_run_id: str, **kwargs: Any) -> SupervisorOutcome:
        events.append("supervisor")
        current, _current_generation = store.load_run(record.run_id)
        assert current.dispatches["dispatch-timing"].state is DispatchStatus.FORWARDED
        assert json.loads(kwargs["prompt"])["kind"] == "orchestration_resume"
        return SupervisorOutcome(
            json.dumps(
                {"protocol_version": 1, "action": "halt", "reason": "timing proof"}
            ),
            "supervisor-session",
            kwargs["expected_generation"],
        )

    workflow.acknowledge_forwarding = acknowledge  # type: ignore[method-assign]
    workflow.refresh_readiness = refresh  # type: ignore[method-assign]
    workflow.prepare_from_supervisor = prepare  # type: ignore[method-assign]
    coordinator.run_supervisor_turn = supervisor_turn  # type: ignore[method-assign]

    with pytest.raises(ExecutionCoordinatorError, match="HALTED"):
        coordinator.run_to_completion(record.run_id, expected_generation=generation)

    persisted, _generation = store.load_run(record.run_id)
    assert events[-4:] == ["supervisor", "acknowledge", "refresh", "prepare"]
    assert persisted.dispatches["dispatch-timing"].state is DispatchStatus.ACKNOWLEDGED
    assert persisted.state is RunStatus.HALTED


def test_supervisor_failure_leaves_forwarding_for_at_least_once_replay(
    tmp_path: Path,
) -> None:
    raw = json.dumps(
        {"kind": "executor_result", "dispatch_id": "dispatch-replay"}
    )
    coordinator, store, _workflow, record, generation = _continuation_fixture(
        tmp_path,
        [("dispatch-replay", "executor", DispatchStatus.FORWARDED, 10, raw)],
    )
    prompts: list[str] = []

    def failed_supervisor(_run_id: str, **kwargs: Any) -> SupervisorOutcome:
        prompts.append(kwargs["prompt"])
        raise RuntimeError("supervisor transport failed")

    coordinator.run_supervisor_turn = failed_supervisor  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="transport failed"):
        coordinator.run_to_completion(record.run_id, expected_generation=generation)

    persisted, generation = store.load_run(record.run_id)
    assert persisted.dispatches["dispatch-replay"].state is DispatchStatus.FORWARDED

    def successful_supervisor(_run_id: str, **kwargs: Any) -> SupervisorOutcome:
        prompts.append(kwargs["prompt"])
        return SupervisorOutcome(
            json.dumps(
                {"protocol_version": 1, "action": "halt", "reason": "replay received"}
            ),
            "supervisor-session",
            kwargs["expected_generation"],
        )

    coordinator.run_supervisor_turn = successful_supervisor  # type: ignore[method-assign]
    with pytest.raises(ExecutionCoordinatorError, match="HALTED"):
        coordinator.run_to_completion(record.run_id, expected_generation=generation)

    persisted, _generation = store.load_run(record.run_id)
    assert prompts[0] == prompts[1]
    assert persisted.dispatches["dispatch-replay"].state is DispatchStatus.ACKNOWLEDGED
    prompt, pending = coordinator._continuation_prompt("bootstrap", persisted)
    assert prompt == "bootstrap"
    assert pending == []


def test_successful_supervisor_receipt_acknowledges_all_pending_forwardings(
    tmp_path: Path,
) -> None:
    first = json.dumps(
        {"kind": "executor_result", "dispatch_id": "dispatch-first"}
    )
    second = json.dumps(
        {"kind": "reviewer_result", "dispatch_id": "dispatch-second"}
    )
    coordinator, store, _workflow, record, generation = _continuation_fixture(
        tmp_path,
        [
            ("dispatch-second", "reviewer", DispatchStatus.FORWARDED, 20, second),
            ("dispatch-first", "executor", DispatchStatus.FORWARDED, 10, first),
        ],
    )
    delivered_ids: list[str] = []

    def supervisor_turn(_run_id: str, **kwargs: Any) -> SupervisorOutcome:
        delivered_ids.extend(
            item["dispatch_id"]
            for item in json.loads(kwargs["prompt"])["pending_forwardings"]
        )
        return SupervisorOutcome(
            json.dumps(
                {"protocol_version": 1, "action": "halt", "reason": "all received"}
            ),
            "supervisor-session",
            kwargs["expected_generation"],
        )

    coordinator.run_supervisor_turn = supervisor_turn  # type: ignore[method-assign]

    with pytest.raises(ExecutionCoordinatorError, match="HALTED"):
        coordinator.run_to_completion(record.run_id, expected_generation=generation)

    persisted, _generation = store.load_run(record.run_id)
    assert delivered_ids == ["dispatch-first", "dispatch-second"]
    assert {
        dispatch.state for dispatch in persisted.dispatches.values()
    } == {DispatchStatus.ACKNOWLEDGED}


def test_invalid_supervisor_command_after_receipt_keeps_delivery_acknowledgement(
    tmp_path: Path,
) -> None:
    raw = json.dumps(
        {"kind": "executor_result", "dispatch_id": "dispatch-invalid-command"}
    )
    coordinator, store, _workflow, record, generation = _continuation_fixture(
        tmp_path,
        [
            (
                "dispatch-invalid-command",
                "executor",
                DispatchStatus.FORWARDED,
                10,
                raw,
            )
        ],
    )

    def supervisor_turn(_run_id: str, **kwargs: Any) -> SupervisorOutcome:
        return SupervisorOutcome(
            "not a command",
            "supervisor-session",
            kwargs["expected_generation"],
        )

    coordinator.run_supervisor_turn = supervisor_turn  # type: ignore[method-assign]

    with pytest.raises(ProtocolError, match="invalid supervisor command JSON"):
        coordinator.run_to_completion(record.run_id, expected_generation=generation)

    persisted, _generation = store.load_run(record.run_id)
    assert (
        persisted.dispatches["dispatch-invalid-command"].state
        is DispatchStatus.ACKNOWLEDGED
    )


@pytest.mark.parametrize(
    ("exception", "expected_category"),
    [
        (OpenCodeAdapterError("provider failure", category="authentication"), "authentication"),
        (ExecutionCoordinatorError("strict JSON failed"), "result_validation"),
        (WorkerResultValidationError("verification failed"), "result_validation"),
        (SequentialWorkflowError("workflow failed"), "workflow_validation"),
        (RuntimeError("unexpected failure"), "internal"),
    ],
)
def test_worker_failure_maps_known_boundaries_to_stable_categories(
    exception: BaseException,
    expected_category: str,
) -> None:
    category, detail = _worker_failure(exception)

    assert category == expected_category
    assert detail == str(exception)


def test_worker_failure_recognizes_wrapped_repository_validation() -> None:
    repository_error = RepositoryValidationError("repository revision changed")
    workflow_error = SequentialWorkflowError("repository revision changed")
    workflow_error.__cause__ = repository_error

    category, detail = _worker_failure(workflow_error)

    assert category == "repository_validation"
    assert detail == "repository revision changed"


def test_worker_failure_redacts_before_bounding_detail() -> None:
    category, detail = _worker_failure(
        RuntimeError("token=top-secret " + "x" * 6000)
    )

    assert category == "internal"
    assert detail.startswith("token=[REDACTED]")
    assert "top-secret" not in detail
    assert len(detail) == 5000


def test_worker_failure_uses_exception_class_for_empty_detail() -> None:
    category, detail = _worker_failure(RuntimeError())

    assert category == "internal"
    assert detail == "RuntimeError"


def test_verification_mismatch_through_worker_boundary_persists_reconciliation(
    tmp_path: Path,
) -> None:
    coordinator, store, prepared = _prepared_coordinator(
        tmp_path,
        session_runner=_result_runner(
            verification=[
                {"check_id": "fixture-check", "status": "skipped", "summary": "not run"}
            ]
        ),
    )

    try:
        with pytest.raises(ResultError, match="not_run"):
            coordinator.execute_worker(prepared)
    finally:
        coordinator.release_run()

    record, _generation = store.load_run(prepared.run_id)
    dispatch = record.dispatches[prepared.dispatch.dispatch_id]
    assert dispatch.state.value == "FAILED"
    assert dispatch.failure_category == "result_validation"
    assert "criterion_self_reports.0.status" in dispatch.failure_detail
    assert "not_run" in dispatch.failure_detail
    assert "not_run" in dispatch.last_event.reason
    assert record.state is RunStatus.WAITING_OPERATOR
    assert record.operator_request is not None
    assert record.operator_request.kind == "reconciliation"


def test_repository_validation_failure_persists_actionable_detail(tmp_path: Path) -> None:
    coordinator, store, prepared = _prepared_coordinator(
        tmp_path,
        session_runner=_result_runner(result_revision="reported-sha"),
    )

    try:
        with pytest.raises(ResultError, match="result_revision.*Extra inputs"):
            coordinator.execute_worker(prepared)
    finally:
        coordinator.release_run()

    record, _generation = store.load_run(prepared.run_id)
    dispatch = record.dispatches[prepared.dispatch.dispatch_id]
    assert dispatch.failure_category == "result_validation"
    assert "result_revision" in dispatch.failure_detail
    assert "Extra inputs are not permitted" in dispatch.failure_detail
    assert dispatch.failure_detail in dispatch.last_event.reason


def test_non_retryable_adapter_failure_persists_provider_category_and_redacted_detail(
    tmp_path: Path,
) -> None:
    def adapter_failure(**kwargs: Any) -> SessionResult:
        lifecycle = kwargs["lifecycle"]
        lifecycle.on_process_started(1000, 1000.0)
        lifecycle.on_session_identified("session-adapter-failure")
        raise OpenCodeAdapterError(
            "provider rejected token=top-secret",
            category="authentication",
        )

    coordinator, store, prepared = _prepared_coordinator(
        tmp_path,
        session_runner=adapter_failure,
    )

    try:
        with pytest.raises(OpenCodeAdapterError, match="provider rejected"):
            coordinator.execute_worker(prepared)
    finally:
        coordinator.release_run()

    record, _generation = store.load_run(prepared.run_id)
    dispatch = record.dispatches[prepared.dispatch.dispatch_id]
    assert dispatch.failure_category == "authentication"
    assert dispatch.failure_detail == "provider rejected token=[REDACTED]"
    assert "top-secret" not in dispatch.failure_detail
    assert "top-secret" not in dispatch.last_event.reason
    assert "provider rejected token=[REDACTED]" in dispatch.last_event.reason


def test_worker_boundary_persists_failure_detail_bounded_to_5000_characters(
    tmp_path: Path,
) -> None:
    def long_failure(**kwargs: Any) -> SessionResult:
        lifecycle = kwargs["lifecycle"]
        lifecycle.on_process_started(1000, 1000.0)
        lifecycle.on_session_identified("session-long-failure")
        raise RuntimeError("x" * 6000)

    coordinator, store, prepared = _prepared_coordinator(
        tmp_path,
        session_runner=long_failure,
    )

    try:
        with pytest.raises(RuntimeError):
            coordinator.execute_worker(prepared)
    finally:
        coordinator.release_run()

    record, _generation = store.load_run(prepared.run_id)
    dispatch = record.dispatches[prepared.dispatch.dispatch_id]
    assert dispatch.failure_category == "internal"
    assert len(dispatch.failure_detail) == 5000
    assert len(dispatch.last_event.reason) == 5000


def _continuation_fixture(
    tmp_path: Path,
    entries: list[
        tuple[
            str,
            Literal["executor", "reviewer"],
            DispatchStatus,
            int,
            str | None,
        ]
    ],
) -> tuple[
    SequentialExecutionCoordinator,
    StateStore,
    SequentialWorkflow,
    RunRecord,
    int,
]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = create_fixture_project(tmp_path)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = new_run_record(
        run_id=f"continuation-{tmp_path.name}",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, f"decision-continuation-{tmp_path.name}"),
        event=TransitionEvent(
            event_id=f"event-continuation-{tmp_path.name}",
            sequence=1,
            actor="dispatcher",
            reason="continuation fixture",
            correlation_id=f"continuation-{tmp_path.name}",
            occurred_at=datetime.now(UTC),
        ),
    )
    store = StateStore(
        project.state,
        heartbeat_seconds=project.config.lease_heartbeat_seconds,
        stale_after_seconds=project.config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(
        project.config,
        store,
        owner_id=f"continuation-owner-{tmp_path.name}",
        repository_inspector=lambda _config, _repo_id, require_clean: _repository_snapshot(),
    )
    record, generation = workflow.activate(record.run_id, expected_generation=generation)
    if entries:
        dispatches: dict[str, DispatchRecord] = {}
        payloads: dict[str, DispatchPayload] = {}
        for dispatch_id, role_kind, state, sequence, forwarding in entries:
            event = TransitionEvent(
                event_id=f"event-{dispatch_id}-{sequence}",
                sequence=sequence,
                actor="dispatcher",
                reason="forwarding fixture",
                correlation_id=dispatch_id,
                occurred_at=datetime.now(UTC),
            )
            values = _dispatch(
                dispatch_id,
                role_key="terra" if role_kind == "executor" else "reviewer",
                role_kind=role_kind,
            ).model_dump()
            values.update(
                {
                    "state": state,
                    "runtime_session_id": (
                        f"session-{dispatch_id}"
                        if state
                        in {
                            DispatchStatus.COMPLETED,
                            DispatchStatus.FORWARDED,
                            DispatchStatus.ACKNOWLEDGED,
                        }
                        else None
                    ),
                    "result_digest": (
                        _DIGEST
                        if state
                        in {
                            DispatchStatus.COMPLETED,
                            DispatchStatus.FORWARDED,
                            DispatchStatus.ACKNOWLEDGED,
                        }
                        else None
                    ),
                    "forwarding_digest": (
                        (
                            hashlib.sha256(forwarding.encode("utf-8")).hexdigest()
                            if forwarding is not None
                            else _DIGEST
                        )
                        if state
                        in {DispatchStatus.FORWARDED, DispatchStatus.ACKNOWLEDGED}
                        else None
                    ),
                    "last_event": event,
                }
            )
            dispatches[dispatch_id] = DispatchRecord.model_validate(values)
            payloads[dispatch_id] = DispatchPayload(
                prompt=f"private prompt for {dispatch_id}",
                policy={"private": True},
                forwarding_payload=forwarding,
            )
        latest = max(
            (dispatch.last_event for dispatch in dispatches.values()),
            key=lambda event: event.sequence,
        )
        record = RunRecord.model_validate(
            record.model_copy(
                update={
                    "dispatches": dispatches,
                    "sequence": latest.sequence,
                    "updated_at": latest.occurred_at,
                }
            ).model_dump()
        )
        generation = store.save_run(
            record,
            expected_generation=generation,
            dispatch_payloads=payloads,
        )
    coordinator = SequentialExecutionCoordinator(
        project.config,
        store,
        workflow,
        owner_id=f"continuation-owner-{tmp_path.name}",
        session_runner=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("continuation fixture unexpectedly invoked a worker")
        ),
    )
    return coordinator, store, workflow, record, generation


def _prepared_coordinator(
    tmp_path: Path,
    *,
    session_runner,
) -> tuple[SequentialExecutionCoordinator, StateStore, PreparedDispatch]:
    project = create_fixture_project(tmp_path)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = new_run_record(
        run_id="execution-failure-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-execution-failure"),
        event=TransitionEvent(
            event_id="event-execution-failure",
            sequence=1,
            actor="dispatcher",
            reason="execution failure fixture",
            correlation_id="execution-failure-run",
            occurred_at=datetime.now(UTC),
        ),
    )
    store = StateStore(
        project.state,
        heartbeat_seconds=project.config.lease_heartbeat_seconds,
        stale_after_seconds=project.config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(
        project.config,
        store,
        owner_id="execution-failure-owner",
        repository_inspector=lambda _config, _repo_id, require_clean: _repository_snapshot(),
    )
    coordinator = SequentialExecutionCoordinator(
        project.config,
        store,
        workflow,
        owner_id="execution-failure-owner",
        session_runner=session_runner,
    )
    coordinator.acquire_run(record.run_id)
    active, generation = workflow.activate(record.run_id, expected_generation=generation)
    prepared = workflow.prepare_from_supervisor(
        active.run_id,
        expected_generation=generation,
        supervisor_text=json.dumps(
            {
                "protocol_version": 1,
                "action": "dispatch",
                "step_id": "prepare-fixture",
                "target_role": "terra",
                "session_mode": "new",
                "prompt": "Perform the fixture work.",
            }
        ),
    )
    assert isinstance(prepared, PreparedDispatch)
    return coordinator, store, prepared


def _result_runner(
    *,
    verification: list[dict[str, str]] | None = None,
    result_revision: str = "base-sha",
    outcome: Literal["completed", "failed"] = "completed",
):
    def run(**kwargs: Any) -> SessionResult:
        prompt = json.loads(kwargs["prompt"])
        lifecycle = kwargs["lifecycle"]
        session_id = f"session-{prompt['dispatch_id']}"
        lifecycle.on_process_started(1000, 1000.0)
        lifecycle.on_session_identified(session_id)
        self_reports = verification
        if self_reports is None:
            self_reports = [
                {
                    "check_id": criterion["criterion_id"],
                    "status": "not_run",
                    "summary": "dispatcher owns this check",
                }
                for criterion in prompt["acceptance_criteria"]
            ]
        extra = {"result_revision": result_revision} if result_revision != "base-sha" else {}
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
                    "step_id": prompt["step_id"],
                    "repository": {
                        "repo_id": prompt["repo_id"],
                        "base_revision": prompt["base_revision"],
                    },
                    "evidence": [
                        {
                            "artifact_id": "fixture-evidence",
                            "relative_path": "fixture.md",
                            "media_type": "text/markdown",
                        }
                    ],
                    "criterion_self_reports": self_reports,
                    "summary": "execution fixture result",
                    "outcome": outcome,
                    **extra,
                    **(
                        {"failure_code": "terminal-fixture-failure"}
                        if outcome == "failed"
                        else {}
                    ),
                }
            ),
        )

    return run


def _repository_snapshot() -> RepositorySnapshot:
    return RepositorySnapshot(
        repo_id="fixture-repo",
        branch="main",
        revision="base-sha",
        worktree_id="b" * 64,
        remote_name="origin",
        remote_url="https://example.invalid/fixture.git",
        clean=True,
        evidence=(
            EvidenceManifestEntry(
                root="evidence",
                relative_path="evidence/fixture.md",
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
        ignored=(),
        dirty_patch_sha256="a" * 64,
        git_metadata_sha256="d" * 64,
        git_refs_sha256="e" * 64,
    )
