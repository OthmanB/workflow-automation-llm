"""Deterministic readiness evaluation for sequential and bounded-parallel dispatches."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .plan import PlanStep, ResourceLock
from .protocol import DispatchRequest
from .workflow import (
    ACTIVE_DISPATCH_STATES,
    DispatchRecord,
    RunRecord,
    StepStatus,
    WorkspaceGroupStatus,
)


class SchedulingError(ValueError):
    """A requested dispatch set is not ready under durable run constraints."""


@dataclass(frozen=True)
class StepReadiness:
    """One step's deterministic eligibility and all reasons preventing dispatch."""

    step_id: str
    ready: bool
    reasons: tuple[str, ...]


def resource_keys(repo_id: str, resource_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Return globally exclusive repository and declared-resource lock keys."""
    return tuple(sorted({f"repository:{repo_id}", *(f"resource:{resource_id}" for resource_id in resource_ids)}))


def evaluate_readiness(config: Config, record: RunRecord) -> tuple[StepReadiness, ...]:
    """Explain each normalized step's readiness in immutable plan order."""
    plan_steps = {step.step_id: step for step in record.plan.steps}
    active_keys = _active_resource_keys(record, plan_steps)
    decisions = []
    for step in record.plan.steps:
        current = record.steps[step.step_id]
        reasons: list[str] = []
        if current.state is not StepStatus.READY:
            reasons.append(f"state is {current.state.value}")
        if not current.operator_gate_resolved:
            reasons.append("operator gate is unresolved")
        for dependency_id in step.depends_on:
            if record.steps[dependency_id].state not in {StepStatus.ACCEPTED, StepStatus.WAIVED}:
                reasons.append(f"dependency {dependency_id} is not accepted")
        for artifact in step.required_inputs:
            if artifact.producer_step_id is not None and record.steps[artifact.producer_step_id].state not in {
                StepStatus.ACCEPTED,
                StepStatus.WAIVED,
            }:
                reasons.append(f"input producer {artifact.producer_step_id} is not accepted")
        if set(_step_resource_keys(step.repo_id, step.resource_locks)) & active_keys:
            reasons.append("resource or repository lock is held")
        decisions.append(StepReadiness(step.step_id, not reasons, tuple(reasons)))
    return tuple(decisions)


def validate_batch(
    config: Config,
    record: RunRecord,
    children: tuple[DispatchRequest, ...],
) -> tuple[DispatchRequest, ...]:
    """Validate a complete batch before any child can be persisted or launched."""
    if len(children) > config.execution.concurrency.max_batch_size:
        raise SchedulingError("batch exceeds execution.concurrency.max_batch_size")
    if config.execution.scheduling != "bounded_parallel":
        raise SchedulingError("batch dispatch requires execution.scheduling bounded_parallel")

    plan_steps = {step.step_id: step for step in record.plan.steps}
    readiness = {item.step_id: item for item in evaluate_readiness(config, record)}
    active_dispatches = [
        dispatch for dispatch in record.dispatches.values() if dispatch.state in ACTIVE_DISPATCH_STATES
    ]
    role_counts = _active_role_counts(active_dispatches)
    active_keys = _active_resource_keys(record, plan_steps)
    selected_keys: set[str] = set()
    errors: list[str] = []
    ordered = tuple(sorted(children, key=lambda child: plan_steps[child.step_id].ordinal if child.step_id in plan_steps else 0))

    if len({child.step_id for child in ordered}) != len(ordered):
        errors.append("batch contains duplicate step IDs")
    if len(active_dispatches) + len(ordered) > config.execution.concurrency.max_active_dispatches:
        errors.append("batch exceeds execution.concurrency.max_active_dispatches")

    for child in ordered:
        step = plan_steps.get(child.step_id)
        if step is None:
            errors.append(f"unknown step {child.step_id}")
            continue
        try:
            role_kind = config.role_kind(child.target_role)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if role_kind not in {"executor", "reviewer"}:
            errors.append(f"role {child.target_role} cannot be dispatched")
            continue
        expected_state = StepStatus.READY if role_kind == "executor" else StepStatus.REVIEW_REQUIRED
        if record.steps[step.step_id].state is not expected_state:
            detail = readiness[step.step_id].reasons or (f"state requires {expected_state.value}",)
            errors.append(f"step {step.step_id} is not ready: {'; '.join(detail)}")
        if child.session_mode != "new":
            errors.append(f"batch child {child.step_id} requires a new session")
        capacity = config.execution.concurrency.role_capacities[child.target_role]
        role_counts[child.target_role] = role_counts.get(child.target_role, 0) + 1
        if role_counts[child.target_role] > capacity:
            errors.append(f"role {child.target_role} exceeds configured capacity")
        keys = set(_step_resource_keys(step.repo_id, step.resource_locks))
        conflict = keys & (active_keys | selected_keys)
        if conflict:
            errors.append(f"step {step.step_id} conflicts on {', '.join(sorted(conflict))}")
        selected_keys.update(keys)

    if errors:
        raise SchedulingError("; ".join(errors))
    return ordered


def validate_workspace_batch(
    config: Config,
    record: RunRecord,
    children: tuple[DispatchRequest, ...],
) -> tuple[DispatchRequest, ...]:
    """Validate independent same-repository executor children for one worktree barrier."""
    if config.execution.scheduling != "bounded_parallel":
        raise SchedulingError("workspace batch requires execution.scheduling bounded_parallel")
    if config.execution.concurrency.same_repository_mode != "worktree_barrier":
        raise SchedulingError("workspace batch requires same_repository_mode worktree_barrier")
    if len(children) < 2:
        raise SchedulingError("workspace batch requires at least two children")
    if len(children) > config.execution.concurrency.max_batch_size:
        raise SchedulingError("workspace batch exceeds execution.concurrency.max_batch_size")
    plan_steps = {step.step_id: step for step in record.plan.steps}
    ordered = tuple(sorted(children, key=lambda child: plan_steps[child.step_id].ordinal if child.step_id in plan_steps else 0))
    errors: list[str] = []
    if len({child.step_id for child in ordered}) != len(ordered):
        errors.append("workspace batch contains duplicate step IDs")
    steps = [plan_steps[child.step_id] for child in ordered if child.step_id in plan_steps]
    if len(steps) != len(ordered):
        errors.extend(f"unknown step {child.step_id}" for child in ordered if child.step_id not in plan_steps)
    repo_ids = {step.repo_id for step in steps}
    if len(repo_ids) != 1:
        errors.append("workspace batch children must target one repository")
    elif config.repository(next(iter(repo_ids))).commit_policy != "required":
        errors.append("workspace batch requires commit_policy required")
    active_groups = [
        group
        for group in record.workspace_groups.values()
        if group.state in {WorkspaceGroupStatus.PREPARED, WorkspaceGroupStatus.ACTIVE, WorkspaceGroupStatus.INTEGRATING}
    ]
    if repo_ids and any(group.repo_id in repo_ids for group in active_groups):
        errors.append("repository already has an active workspace group")
    active_dispatches = [
        dispatch for dispatch in record.dispatches.values() if dispatch.state in ACTIVE_DISPATCH_STATES
    ]
    if len(active_dispatches) + len(ordered) > config.execution.concurrency.max_active_dispatches:
        errors.append("workspace batch exceeds execution.concurrency.max_active_dispatches")
    role_counts = _active_role_counts(active_dispatches)
    selected_resources: set[str] = set()
    for child in ordered:
        step = plan_steps.get(child.step_id)
        if step is None:
            continue
        if config.role_kind(child.target_role) != "executor":
            errors.append(f"workspace child {child.step_id} must target an executor role")
        if record.steps[step.step_id].state is not StepStatus.READY:
            errors.append(f"workspace step {step.step_id} is not READY")
        if not record.steps[step.step_id].operator_gate_resolved:
            errors.append(f"workspace step {step.step_id} has an unresolved operator gate")
        if any(record.steps[dependency_id].state not in {StepStatus.ACCEPTED, StepStatus.WAIVED} for dependency_id in step.depends_on):
            errors.append(f"workspace step {step.step_id} has an unaccepted dependency")
        if any(
            artifact.producer_step_id is not None
            and record.steps[artifact.producer_step_id].state not in {StepStatus.ACCEPTED, StepStatus.WAIVED}
            for artifact in step.required_inputs
        ):
            errors.append(f"workspace step {step.step_id} has an unaccepted input producer")
        if child.session_mode != "new":
            errors.append(f"workspace child {child.step_id} requires a new session")
        role_counts[child.target_role] = role_counts.get(child.target_role, 0) + 1
        if role_counts[child.target_role] > config.execution.concurrency.role_capacities[child.target_role]:
            errors.append(f"role {child.target_role} exceeds configured capacity")
        resources = {f"resource:{lock.resource_id}" for lock in step.resource_locks}
        conflict = resources & selected_resources
        if conflict:
            errors.append(f"workspace step {step.step_id} conflicts on {', '.join(sorted(conflict))}")
        selected_resources.update(resources)
    if errors:
        raise SchedulingError("; ".join(errors))
    return ordered


def _active_role_counts(dispatches: list[DispatchRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for dispatch in dispatches:
        counts[dispatch.role_key] = counts.get(dispatch.role_key, 0) + 1
    return counts


def _active_resource_keys(record: RunRecord, plan_steps: dict[str, PlanStep]) -> set[str]:
    keys: set[str] = set()
    for dispatch in record.dispatches.values():
        if dispatch.state not in ACTIVE_DISPATCH_STATES:
            continue
        step = plan_steps[dispatch.step_id]
        keys.update(_step_resource_keys(step.repo_id, step.resource_locks))
    return keys


def _step_resource_keys(repo_id: str, locks: tuple[ResourceLock, ...]) -> tuple[str, ...]:
    resource_ids = tuple(lock.resource_id for lock in locks)
    return resource_keys(repo_id, resource_ids)
