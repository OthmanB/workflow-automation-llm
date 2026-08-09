"""Schema-v1 run, step, and dispatch state machines with completion guards."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Literal, TypeVar

from pydantic import Field, model_validator

from .config import ContractModel, Identifier
from .plan import NormalizedPlan, PlanApproval, PlanError, validate_plan_approval


class TransitionError(ValueError):
    """A requested workflow transition is not valid from the current state."""


class RunStatus(str, Enum):
    NEW = "NEW"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_OPERATOR = "WAITING_OPERATOR"
    HALTED = "HALTED"
    FAILED = "FAILED"
    SUCCEEDED = "SUCCEEDED"
    CANCELLED = "CANCELLED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REVIEWING = "REVIEWING"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    BLOCKED = "BLOCKED"
    ACCEPTED = "ACCEPTED"
    WAIVED = "WAIVED"
    FAILED = "FAILED"


class DispatchStatus(str, Enum):
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    FORWARDED = "FORWARDED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    ABANDONED = "ABANDONED"


RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.NEW: frozenset(
        {RunStatus.READY, RunStatus.WAITING_OPERATOR, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.READY: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.WAITING_OPERATOR,
            RunStatus.HALTED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_OPERATOR,
            RunStatus.HALTED,
            RunStatus.FAILED,
            RunStatus.SUCCEEDED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING_OPERATOR: frozenset(
        {
            RunStatus.READY,
            RunStatus.RUNNING,
            RunStatus.HALTED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.HALTED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}

STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset({StepStatus.READY, StepStatus.WAIVED}),
    StepStatus.READY: frozenset(
        {StepStatus.EXECUTING, StepStatus.BLOCKED, StepStatus.WAIVED, StepStatus.FAILED}
    ),
    StepStatus.EXECUTING: frozenset(
        {StepStatus.EXECUTED, StepStatus.BLOCKED, StepStatus.FAILED}
    ),
    StepStatus.EXECUTED: frozenset({StepStatus.REVIEW_REQUIRED, StepStatus.ACCEPTED}),
    StepStatus.REVIEW_REQUIRED: frozenset(
        {StepStatus.REVIEWING, StepStatus.BLOCKED, StepStatus.FAILED}
    ),
    StepStatus.REVIEWING: frozenset(
        {
            StepStatus.ACCEPTED,
            StepStatus.CHANGES_REQUESTED,
            StepStatus.BLOCKED,
            StepStatus.FAILED,
            StepStatus.REVIEW_REQUIRED,
        }
    ),
    StepStatus.CHANGES_REQUESTED: frozenset(
        {StepStatus.READY, StepStatus.BLOCKED, StepStatus.FAILED}
    ),
    StepStatus.BLOCKED: frozenset({StepStatus.READY, StepStatus.WAIVED, StepStatus.FAILED}),
    StepStatus.ACCEPTED: frozenset(),
    StepStatus.WAIVED: frozenset(),
    StepStatus.FAILED: frozenset(),
}

DISPATCH_TRANSITIONS: dict[DispatchStatus, frozenset[DispatchStatus]] = {
    DispatchStatus.PREPARED: frozenset({DispatchStatus.RUNNING, DispatchStatus.ABANDONED}),
    DispatchStatus.RUNNING: frozenset(
        {DispatchStatus.COMPLETED, DispatchStatus.FAILED, DispatchStatus.ABANDONED}
    ),
    DispatchStatus.COMPLETED: frozenset({DispatchStatus.FORWARDED}),
    DispatchStatus.FAILED: frozenset(),
    DispatchStatus.FORWARDED: frozenset({DispatchStatus.ACKNOWLEDGED, DispatchStatus.ABANDONED}),
    DispatchStatus.ACKNOWLEDGED: frozenset(),
    DispatchStatus.ABANDONED: frozenset(),
}

ACTIVE_DISPATCH_STATES = frozenset(
    {
        DispatchStatus.PREPARED,
        DispatchStatus.RUNNING,
        DispatchStatus.COMPLETED,
        DispatchStatus.FORWARDED,
    }
)


class TransitionEvent(ContractModel):
    """Correlated transition provenance required by every state change."""

    event_id: Identifier
    sequence: Annotated[int, Field(ge=1)]
    actor: Literal["dispatcher", "supervisor", "executor", "reviewer", "operator"]
    reason: Annotated[str, Field(min_length=1, max_length=5000)]
    correlation_id: Identifier
    occurred_at: datetime


class OperatorRequest(ContractModel):
    """Durable operator question required for the non-terminal waiting state."""

    request_id: Identifier
    question: Annotated[str, Field(min_length=1, max_length=10_000)]
    allowed_answers: list[Identifier]
    context_ref: Identifier
    resume_to: RunStatus
    expires_at: datetime | None
    required_role: Identifier | None

    @model_validator(mode="after")
    def nonempty_answers(self) -> "OperatorRequest":
        if not self.allowed_answers:
            raise ValueError("operator request requires at least one allowed answer")
        if len(self.allowed_answers) != len(set(self.allowed_answers)):
            raise ValueError("operator request allowed_answers must not contain duplicates")
        return self


class RepositoryCoordinate(ContractModel):
    """Repository revision captured before a dispatch is prepared."""

    repo_id: Identifier
    base_revision: Annotated[str, Field(min_length=1, max_length=200)]


class DispatchIntent(ContractModel):
    """Durable intent required before a subprocess can be launched in Phase 3."""

    prompt_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    policy_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    expected_result_kind: Literal["executor", "reviewer"]
    repository: RepositoryCoordinate
    idempotency_key: Identifier


class DispatchRecord(ContractModel):
    """One independently recoverable dispatch attempt."""

    dispatch_id: Identifier
    step_id: Identifier
    role_key: Identifier
    role_kind: Literal["executor", "reviewer"]
    attempt: Annotated[int, Field(ge=1, le=100)]
    logical_session_key: Identifier
    runtime_session_id: Identifier | None
    state: DispatchStatus
    intent: DispatchIntent
    result_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None
    forwarding_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None
    last_event: TransitionEvent

    @model_validator(mode="after")
    def validate_state_data(self) -> "DispatchRecord":
        if self.state is DispatchStatus.PREPARED and self.runtime_session_id is not None:
            raise ValueError("prepared dispatch cannot have runtime_session_id")
        if self.state in {
            DispatchStatus.COMPLETED,
            DispatchStatus.FORWARDED,
            DispatchStatus.ACKNOWLEDGED,
        } and self.runtime_session_id is None:
            raise ValueError("completed dispatch requires runtime_session_id")
        if self.state in {
            DispatchStatus.COMPLETED,
            DispatchStatus.FORWARDED,
            DispatchStatus.ACKNOWLEDGED,
        } and self.result_digest is None:
            raise ValueError("completed dispatch requires result_digest")
        if self.state in {DispatchStatus.FORWARDED, DispatchStatus.ACKNOWLEDGED} and (
            self.forwarding_digest is None
        ):
            raise ValueError("forwarded dispatch requires forwarding_digest")
        return self


class StepRecord(ContractModel):
    """Mutable run status for one immutable normalized plan step."""

    step_id: Identifier
    state: StepStatus
    executor_attempts: Annotated[int, Field(ge=0, le=100)]
    reviewer_attempts: Annotated[int, Field(ge=0, le=100)]
    active_dispatch_id: Identifier | None
    accepted_artifact_ids: list[Identifier]
    review_acceptances: Annotated[int, Field(ge=0, le=100)]
    operator_gate_resolved: bool
    waiver_decision_ref: Identifier | None
    result_digests: list[Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]]
    last_event: TransitionEvent

    @model_validator(mode="after")
    def validate_waiver(self) -> "StepRecord":
        if self.state is StepStatus.WAIVED and self.waiver_decision_ref is None:
            raise ValueError("waived step requires waiver_decision_ref")
        if self.state is not StepStatus.WAIVED and self.waiver_decision_ref is not None:
            raise ValueError("non-waived step cannot have waiver_decision_ref")
        if len(self.accepted_artifact_ids) != len(set(self.accepted_artifact_ids)):
            raise ValueError("accepted_artifact_ids must not contain duplicates")
        if len(self.result_digests) != len(set(self.result_digests)):
            raise ValueError("result_digests must not contain duplicates")
        return self


class RunRecord(ContractModel):
    """Schema-v1 run state containing its immutable normalized plan and digest."""

    state_schema_version: Literal[1]
    run_id: Identifier
    project_id: Identifier
    config_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    plan: NormalizedPlan
    plan_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    plan_approval: PlanApproval
    state: RunStatus
    sequence: Annotated[int, Field(ge=0)]
    steps: dict[Identifier, StepRecord]
    dispatches: dict[Identifier, DispatchRecord]
    operator_request: OperatorRequest | None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_run_record(self) -> "RunRecord":
        if self.plan_digest != self.plan.plan_digest:
            raise ValueError("plan_digest does not match normalized plan")
        try:
            validate_plan_approval(self.plan, self.plan_approval)
        except PlanError as exc:
            raise ValueError(str(exc)) from exc
        if set(self.steps) != {step.step_id for step in self.plan.steps}:
            raise ValueError("run step records must exactly match normalized plan steps")
        if self.state is RunStatus.WAITING_OPERATOR and self.operator_request is None:
            raise ValueError("waiting operator run requires operator_request")
        if self.state is not RunStatus.WAITING_OPERATOR and self.operator_request is not None:
            raise ValueError("only waiting operator run may contain operator_request")
        return self


class CompletionObligation(ContractModel):
    """One unmet dispatcher-owned condition that blocks run success."""

    code: Literal[
        "step_not_accepted",
        "dependency_not_accepted",
        "review_incomplete",
        "evidence_missing",
        "operator_gate_unresolved",
        "dispatch_in_flight",
        "operator_request_unresolved",
    ]
    step_id: Identifier | None
    detail: Annotated[str, Field(min_length=1, max_length=5000)]


def new_run_record(
    *,
    run_id: str,
    project_id: str,
    config_digest: str,
    plan: NormalizedPlan,
    plan_approval: PlanApproval,
    event: TransitionEvent,
) -> RunRecord:
    """Create a NEW run with one PENDING record for every normalized plan step."""
    now = datetime.now(UTC)
    steps = {
        step.step_id: StepRecord(
            step_id=step.step_id,
            state=StepStatus.PENDING,
            executor_attempts=0,
            reviewer_attempts=0,
            active_dispatch_id=None,
            accepted_artifact_ids=[],
            review_acceptances=0,
            operator_gate_resolved=not step.authorization.requires_operator_approval,
            waiver_decision_ref=None,
            result_digests=[],
            last_event=event,
        )
        for step in plan.steps
    }
    return RunRecord(
        state_schema_version=1,
        run_id=run_id,
        project_id=project_id,
        config_digest=config_digest,
        plan=plan,
        plan_digest=plan.plan_digest,
        plan_approval=plan_approval,
        state=RunStatus.NEW,
        sequence=event.sequence,
        steps=steps,
        dispatches={},
        operator_request=None,
        created_at=now,
        updated_at=now,
    )


def transition_run(
    record: RunRecord,
    target: RunStatus,
    event: TransitionEvent,
    *,
    operator_request: OperatorRequest | None = None,
) -> RunRecord:
    """Apply a validated run transition and retain a durable operator question."""
    _validate_transition("run", record.state, target, RUN_TRANSITIONS)
    if target is RunStatus.WAITING_OPERATOR and operator_request is None:
        raise TransitionError("WAITING_OPERATOR requires operator_request")
    if target is not RunStatus.WAITING_OPERATOR and operator_request is not None:
        raise TransitionError("operator_request is only valid for WAITING_OPERATOR")
    if target is RunStatus.SUCCEEDED:
        obligations = completion_obligations(record)
        if obligations:
            raise TransitionError(
                "completion denied: " + "; ".join(obligation.detail for obligation in obligations)
            )
    values = record.model_dump()
    values.update(
        {
            "state": target,
            "sequence": event.sequence,
            "operator_request": operator_request,
            "updated_at": event.occurred_at,
        }
    )
    return RunRecord.model_validate(values)


def transition_step(
    record: StepRecord,
    target: StepStatus,
    event: TransitionEvent,
    *,
    active_dispatch_id: str | None = None,
    waiver_decision_ref: str | None = None,
) -> StepRecord:
    """Apply one validated step transition without interpreting free-form chat."""
    _validate_transition("step", record.state, target, STEP_TRANSITIONS)
    if target is StepStatus.WAIVED and waiver_decision_ref is None:
        raise TransitionError("WAIVED requires waiver_decision_ref")
    if target is not StepStatus.WAIVED and waiver_decision_ref is not None:
        raise TransitionError("waiver_decision_ref is only valid for WAIVED")
    values = record.model_dump()
    values.update(
        {
            "state": target,
            "active_dispatch_id": active_dispatch_id,
            "waiver_decision_ref": waiver_decision_ref,
            "last_event": event,
        }
    )
    return StepRecord.model_validate(values)


def transition_dispatch(
    record: DispatchRecord,
    target: DispatchStatus,
    event: TransitionEvent,
    *,
    runtime_session_id: str | None = None,
    result_digest: str | None = None,
    forwarding_digest: str | None = None,
) -> DispatchRecord:
    """Apply a dispatch state transition with its required durable transition data."""
    _validate_transition("dispatch", record.state, target, DISPATCH_TRANSITIONS)
    updates: dict[str, object] = {"state": target, "last_event": event}
    if runtime_session_id is not None:
        updates["runtime_session_id"] = runtime_session_id
    if result_digest is not None:
        updates["result_digest"] = result_digest
    if forwarding_digest is not None:
        updates["forwarding_digest"] = forwarding_digest
    try:
        values = record.model_dump()
        values.update(updates)
        return DispatchRecord.model_validate(values)
    except ValueError as exc:
        raise TransitionError(f"invalid dispatch transition data: {exc}") from exc


def completion_obligations(record: RunRecord) -> list[CompletionObligation]:
    """Return every outstanding condition that prevents successful completion."""
    obligations: list[CompletionObligation] = []
    plan_steps = {step.step_id: step for step in record.plan.steps}
    for step_id, step_record in record.steps.items():
        plan_step = plan_steps[step_id]
        if step_record.state not in {StepStatus.ACCEPTED, StepStatus.WAIVED}:
            obligations.append(
                CompletionObligation(
                    code="step_not_accepted",
                    step_id=step_id,
                    detail=f"step {step_id} is {step_record.state.value}",
                )
            )
            continue
        for dependency_id in plan_step.depends_on:
            dependency = record.steps[dependency_id]
            if dependency.state not in {StepStatus.ACCEPTED, StepStatus.WAIVED}:
                obligations.append(
                    CompletionObligation(
                        code="dependency_not_accepted",
                        step_id=step_id,
                        detail=f"step {step_id} dependency {dependency_id} is not accepted or waived",
                    )
                )
        if step_record.state is StepStatus.ACCEPTED:
            required_artifacts = {
                requirement.artifact_id for requirement in plan_step.evidence_requirements
            }
            missing_artifacts = required_artifacts - set(step_record.accepted_artifact_ids)
            if missing_artifacts:
                obligations.append(
                    CompletionObligation(
                        code="evidence_missing",
                        step_id=step_id,
                        detail=f"step {step_id} is missing evidence {sorted(missing_artifacts)}",
                    )
                )
            if step_record.review_acceptances < plan_step.review.required_acceptances:
                obligations.append(
                    CompletionObligation(
                        code="review_incomplete",
                        step_id=step_id,
                        detail=(
                            f"step {step_id} has {step_record.review_acceptances} review acceptances, "
                            f"requires {plan_step.review.required_acceptances}"
                        ),
                    )
                )
            if not step_record.operator_gate_resolved:
                obligations.append(
                    CompletionObligation(
                        code="operator_gate_unresolved",
                        step_id=step_id,
                        detail=f"step {step_id} requires an unresolved operator gate",
                    )
                )
    for dispatch_id, dispatch in record.dispatches.items():
        if dispatch.state in ACTIVE_DISPATCH_STATES:
            obligations.append(
                CompletionObligation(
                    code="dispatch_in_flight",
                    step_id=dispatch.step_id,
                    detail=f"dispatch {dispatch_id} is {dispatch.state.value}",
                )
            )
    if record.operator_request is not None:
        obligations.append(
            CompletionObligation(
                code="operator_request_unresolved",
                step_id=None,
                detail=f"operator request {record.operator_request.request_id} remains unresolved",
            )
        )
    return obligations


def terminal_exit_code(status: RunStatus) -> int:
    """Return the CLI exit code for terminal states and reject non-terminal ones."""
    codes = {
        RunStatus.SUCCEEDED: 0,
        RunStatus.HALTED: 1,
        RunStatus.CANCELLED: 1,
        RunStatus.FAILED: 2,
    }
    try:
        return codes[status]
    except KeyError as exc:
        raise TransitionError(f"run state {status.value} is not terminal") from exc


State = TypeVar("State", RunStatus, StepStatus, DispatchStatus)


def _validate_transition(
    entity: str,
    current: State,
    target: State,
    transitions: dict[State, frozenset[State]],
) -> None:
    if target not in transitions[current]:
        raise TransitionError(f"invalid {entity} transition: {current.value} -> {target.value}")
