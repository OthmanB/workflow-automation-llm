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


class BatchStatus(str, Enum):
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    JOINED = "JOINED"
    FAILED = "FAILED"


class WorkspaceGroupStatus(str, Enum):
    PREPARED = "PREPARED"
    ACTIVE = "ACTIVE"
    INTEGRATING = "INTEGRATING"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    CLEANED = "CLEANED"
    FAILED = "FAILED"

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
        {StepStatus.READY, StepStatus.REVIEW_REQUIRED, StepStatus.BLOCKED, StepStatus.FAILED}
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

BATCH_TRANSITIONS: dict[BatchStatus, frozenset[BatchStatus]] = {
    BatchStatus.PREPARED: frozenset({BatchStatus.RUNNING, BatchStatus.FAILED}),
    BatchStatus.RUNNING: frozenset({BatchStatus.JOINED, BatchStatus.FAILED}),
    BatchStatus.JOINED: frozenset(),
    BatchStatus.FAILED: frozenset(),
}

WORKSPACE_GROUP_TRANSITIONS: dict[WorkspaceGroupStatus, frozenset[WorkspaceGroupStatus]] = {
    WorkspaceGroupStatus.PREPARED: frozenset({WorkspaceGroupStatus.ACTIVE, WorkspaceGroupStatus.FAILED}),
    WorkspaceGroupStatus.ACTIVE: frozenset(
        {WorkspaceGroupStatus.INTEGRATING, WorkspaceGroupStatus.CLEANUP_PENDING, WorkspaceGroupStatus.FAILED}
    ),
    WorkspaceGroupStatus.INTEGRATING: frozenset({WorkspaceGroupStatus.CLEANUP_PENDING, WorkspaceGroupStatus.FAILED}),
    WorkspaceGroupStatus.CLEANUP_PENDING: frozenset({WorkspaceGroupStatus.CLEANED, WorkspaceGroupStatus.FAILED}),
    WorkspaceGroupStatus.CLEANED: frozenset(),
    WorkspaceGroupStatus.FAILED: frozenset({WorkspaceGroupStatus.CLEANUP_PENDING}),
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
    kind: Literal[
        "underspecification",
        "risk_gate",
        "reconciliation",
        "budget",
        "escalation",
        "review_waiver",
        "batch_reconciliation",
        "workspace_reconciliation",
        "stall_recovery",
    ] = "underspecification"
    step_id: Identifier | None = None
    reassignment_role_key: Identifier | None = None

    @model_validator(mode="after")
    def nonempty_answers(self) -> "OperatorRequest":
        if not self.allowed_answers:
            raise ValueError("operator request requires at least one allowed answer")
        if len(self.allowed_answers) != len(set(self.allowed_answers)):
            raise ValueError("operator request allowed_answers must not contain duplicates")
        if (self.kind == "escalation") != (self.reassignment_role_key is not None):
            raise ValueError("only escalation requests may define reassignment_role_key")
        return self


class RepositoryCoordinate(ContractModel):
    """Repository revision captured before a dispatch is prepared."""

    repo_id: Identifier
    base_revision: Annotated[str, Field(min_length=1, max_length=200)]
    base_branch: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,300}$")] | None = None
    working_branch: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,300}$")] | None = None
    worktree_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    remote_name: Identifier | None = None
    remote_url: Annotated[str, Field(min_length=1)] | None = None


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
    batch_id: Identifier | None = None
    workspace_group_id: Identifier | None = None
    process_id: Annotated[int, Field(ge=1)] | None = None
    process_host: Identifier | None = None
    process_started_at: datetime | None = None
    cancel_requested: bool = False
    cancel_requested_at: datetime | None = None
    failure_category: Identifier | None = None
    failure_detail: str | None = None
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


class BatchRecord(ContractModel):
    """Durable aggregate state for one all-or-none prepared dispatch batch."""

    batch_id: Identifier
    dispatch_ids: tuple[Identifier, ...]
    state: BatchStatus
    failure_mode: Literal["wait_for_started"]
    failed_dispatch_ids: tuple[Identifier, ...] = ()
    last_event: TransitionEvent

    @model_validator(mode="after")
    def validate_children(self) -> "BatchRecord":
        if not self.dispatch_ids:
            raise ValueError("batch requires at least one dispatch")
        if len(self.dispatch_ids) != len(set(self.dispatch_ids)):
            raise ValueError("batch dispatch_ids must not contain duplicates")
        if not set(self.failed_dispatch_ids).issubset(self.dispatch_ids):
            raise ValueError("batch failed_dispatch_ids must reference batch dispatch_ids")
        return self


class WorkspaceChild(ContractModel):
    """One temporary branch and linked worktree owned by a workspace group."""

    step_id: Identifier
    branch: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,300}$")]
    worktree_path: Annotated[str, Field(min_length=1)]
    base_revision: Annotated[str, Field(min_length=1, max_length=200)]
    head_revision: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class WorkspaceGroup(ContractModel):
    """Durable ownership record for temporary same-repository worktrees."""

    workspace_group_id: Identifier
    repo_id: Identifier
    base_revision: Annotated[str, Field(min_length=1, max_length=200)]
    base_branch: Identifier
    integration_branch: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,300}$")]
    integration_worktree_path: Annotated[str, Field(min_length=1)]
    integration_revision: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    worktree_root: Annotated[str, Field(min_length=1)]
    lease_owner_id: Identifier
    children: tuple[WorkspaceChild, ...]
    state: WorkspaceGroupStatus
    last_event: TransitionEvent

    @model_validator(mode="after")
    def validate_children(self) -> "WorkspaceGroup":
        if not self.children:
            raise ValueError("workspace group requires at least one child")
        step_ids = [child.step_id for child in self.children]
        branches = [child.branch for child in self.children]
        paths = [child.worktree_path for child in self.children]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("workspace group children must have unique step IDs")
        if len(branches) != len(set(branches)):
            raise ValueError("workspace group children must have unique branches")
        if len(paths) != len(set(paths)):
            raise ValueError("workspace group children must have unique paths")
        if self.integration_worktree_path in paths:
            raise ValueError("workspace integration path must differ from child paths")
        return self


class CompiledReviewObligation(ContractModel):
    """Concrete pre-dispatch review policy for one normalized plan step."""

    step_id: Identifier
    required: bool
    reviewer_role_keys: tuple[Identifier, ...]
    required_acceptances: Annotated[int, Field(ge=0, le=20)]
    independence: Literal["fresh_session"]
    waivable: bool
    source_policy_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

    @model_validator(mode="after")
    def validate_requirement(self) -> "CompiledReviewObligation":
        if self.required != bool(self.required_acceptances):
            raise ValueError("review obligation required must match required_acceptances")
        if self.required_acceptances > len(self.reviewer_role_keys):
            raise ValueError("review obligation acceptances cannot exceed reviewers")
        if len(self.reviewer_role_keys) != len(set(self.reviewer_role_keys)):
            raise ValueError("review obligation reviewers must be unique")
        return self


class RunPolicy(ContractModel):
    """Immutable Phase 6 policy compiled before a run can launch work."""

    profile_id: Identifier
    profile_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    review_obligations: dict[Identifier, CompiledReviewObligation]
    underspec_mode: Literal["ask", "auto"]
    policy_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class UsageAmount(ContractModel):
    """Normalized measured usage for one run, step, role, or session."""

    cost_usd: Annotated[float, Field(ge=0)] = 0.0
    tokens_total: Annotated[int, Field(ge=0)] = 0
    tokens_input: Annotated[int, Field(ge=0)] = 0
    tokens_output: Annotated[int, Field(ge=0)] = 0
    tokens_reasoning: Annotated[int, Field(ge=0)] = 0


class UsageLedger(ContractModel):
    """Cumulative measured usage with independently auditable dimensions."""

    run: UsageAmount = Field(default_factory=UsageAmount)
    by_step: dict[Identifier, UsageAmount] = Field(default_factory=dict)
    by_role: dict[Identifier, UsageAmount] = Field(default_factory=dict)
    by_session: dict[Identifier, UsageAmount] = Field(default_factory=dict)


class StepRecord(ContractModel):
    """Mutable run status for one immutable normalized plan step."""

    step_id: Identifier
    state: StepStatus
    executor_attempts: Annotated[int, Field(ge=0, le=100)]
    reviewer_attempts: Annotated[int, Field(ge=0, le=100)]
    active_dispatch_id: Identifier | None
    accepted_artifact_ids: list[Identifier]
    review_acceptances: Annotated[int, Field(ge=0, le=100)]
    accepted_reviewer_role_keys: list[Identifier] = Field(default_factory=list)
    rework_rounds: Annotated[int, Field(ge=0, le=100)] = 0
    reassignment_role_key: Identifier | None = None
    review_waiver_decision_ref: Identifier | None = None
    stalls: Annotated[int, Field(ge=0, le=100)] = 0
    last_stall_category: Identifier | None = None
    last_stall_reason: str | None = None
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
        if len(self.accepted_reviewer_role_keys) != len(set(self.accepted_reviewer_role_keys)):
            raise ValueError("accepted_reviewer_role_keys must not contain duplicates")
        if self.reassignment_role_key is not None and self.state is not StepStatus.READY:
            raise ValueError("reassignment_role_key is only valid for ready steps")
        if self.review_waiver_decision_ref is not None and self.state is not StepStatus.ACCEPTED:
            raise ValueError("review_waiver_decision_ref is only valid for accepted steps")
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
    batches: dict[Identifier, BatchRecord] = Field(default_factory=dict)
    workspace_groups: dict[Identifier, WorkspaceGroup] = Field(default_factory=dict)
    operator_request: OperatorRequest | None
    policy: RunPolicy | None = None
    usage: UsageLedger = Field(default_factory=UsageLedger)
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
        if self.policy is not None:
            if set(self.policy.review_obligations) != set(self.steps):
                raise ValueError("run policy review obligations must exactly match run steps")
            for step_id, obligation in self.policy.review_obligations.items():
                if obligation.step_id != step_id:
                    raise ValueError("run policy review obligation step_id must match its mapping key")
        for batch_id, batch in self.batches.items():
            if batch.batch_id != batch_id:
                raise ValueError("batch ID must match its mapping key")
            if not set(batch.dispatch_ids).issubset(self.dispatches):
                raise ValueError("batch dispatch_ids must reference run dispatches")
        for group_id, group in self.workspace_groups.items():
            if group.workspace_group_id != group_id:
                raise ValueError("workspace group ID must match its mapping key")
            if not set(child.step_id for child in group.children).issubset(self.steps):
                raise ValueError("workspace group children must reference run steps")
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
            accepted_reviewer_role_keys=[],
            rework_rounds=0,
            reassignment_role_key=None,
            review_waiver_decision_ref=None,
            stalls=0,
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
        batches={},
        workspace_groups={},
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
    review_waiver_decision_ref: str | None = None,
) -> StepRecord:
    """Apply one validated step transition without interpreting free-form chat."""
    review_waiver_acceptance = (
        record.state is StepStatus.REVIEW_REQUIRED
        and target is StepStatus.ACCEPTED
        and review_waiver_decision_ref is not None
    )
    if not review_waiver_acceptance:
        _validate_transition("step", record.state, target, STEP_TRANSITIONS)
    if target is StepStatus.WAIVED and waiver_decision_ref is None:
        raise TransitionError("WAIVED requires waiver_decision_ref")
    if target is not StepStatus.WAIVED and waiver_decision_ref is not None:
        raise TransitionError("waiver_decision_ref is only valid for WAIVED")
    if target is not StepStatus.ACCEPTED and review_waiver_decision_ref is not None:
        raise TransitionError("review_waiver_decision_ref is only valid for ACCEPTED")
    values = record.model_dump()
    values.update(
        {
            "state": target,
            "active_dispatch_id": active_dispatch_id,
            "waiver_decision_ref": waiver_decision_ref,
            "review_waiver_decision_ref": review_waiver_decision_ref,
            "reassignment_role_key": None,
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
    failure_category: str | None = None,
    failure_detail: str | None = None,
    process_host: str | None = None,
    process_started_at: datetime | None = None,
    process_id: int | None = None,
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
    if failure_category is not None:
        updates["failure_category"] = failure_category
    if failure_detail is not None:
        updates["failure_detail"] = failure_detail[:5000]
    if process_host is not None:
        updates["process_host"] = process_host
    if process_started_at is not None:
        updates["process_started_at"] = process_started_at
    if process_id is not None:
        updates["process_id"] = process_id
    try:
        values = record.model_dump()
        values.update(updates)
        return DispatchRecord.model_validate(values)
    except ValueError as exc:
        raise TransitionError(f"invalid dispatch transition data: {exc}") from exc


def transition_batch(record: BatchRecord, target: BatchStatus, event: TransitionEvent) -> BatchRecord:
    """Apply one durable batch aggregate transition."""
    _validate_transition("batch", record.state, target, BATCH_TRANSITIONS)
    return record.model_copy(update={"state": target, "last_event": event})


def transition_workspace_group(
    record: WorkspaceGroup,
    target: WorkspaceGroupStatus,
    event: TransitionEvent,
) -> WorkspaceGroup:
    """Apply one durable temporary-worktree lifecycle transition."""
    _validate_transition("workspace group", record.state, target, WORKSPACE_GROUP_TRANSITIONS)
    return record.model_copy(update={"state": target, "last_event": event})


def completion_obligations(record: RunRecord) -> list[CompletionObligation]:
    """Return every outstanding condition that prevents successful completion."""
    obligations: list[CompletionObligation] = []
    plan_steps = {step.step_id: step for step in record.plan.steps}
    for step_id, step_record in record.steps.items():
        plan_step = plan_steps[step_id]
        required_acceptances = plan_step.review.required_acceptances
        if record.policy is not None:
            required_acceptances = record.policy.review_obligations[step_id].required_acceptances
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
            if (
                step_record.review_waiver_decision_ref is None
                and step_record.review_acceptances < required_acceptances
            ):
                obligations.append(
                    CompletionObligation(
                        code="review_incomplete",
                        step_id=step_id,
                        detail=(
                            f"step {step_id} has {step_record.review_acceptances} review acceptances, "
                            f"requires {required_acceptances}"
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


State = TypeVar("State", RunStatus, StepStatus, DispatchStatus, BatchStatus, WorkspaceGroupStatus)


def _validate_transition(
    entity: str,
    current: State,
    target: State,
    transitions: dict[State, frozenset[State]],
) -> None:
    if target not in transitions[current]:
        raise TransitionError(f"invalid {entity} transition: {current.value} -> {target.value}")
