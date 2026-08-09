"""Deterministic readiness evaluation for sequential and bounded-parallel dispatches."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .plan import PlanStep, ResourceLock
from .protocol import DispatchRequest
from .workflow import ACTIVE_DISPATCH_STATES, DispatchRecord, RunRecord, StepStatus


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
