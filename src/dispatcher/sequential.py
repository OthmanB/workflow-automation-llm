"""Plan-driven sequential workflow facade backed by the SQLite authority."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import wraps
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, TypeVar, cast

from .config import Config
from .permissions import compile_effective_policy, generate_opencode_config, should_auto_approve
from .plan import PlanError, PlanStep, validate_plan_approval, verify_plan_sources
from .policy import PolicyError, compile_run_policy
from .protocol import (
    AskOperatorCommand,
    BatchDispatchCommand,
    DispatchCommand,
    HaltCommand,
    RequestCompletionCommand,
    RequestReviewWaiverCommand,
    parse_supervisor_command,
)
from .repository import (
    RepositorySnapshot,
    RepositoryValidationError,
    inspect_repository,
    validate_executor_snapshot,
    validate_review_snapshot,
)
from .results import (
    ExecutorCompletedResult,
    ExecutorResult,
    ResultError,
    ResultExpectation,
    ReviewerAcceptedResult,
    ReviewerChangesRequestedResult,
    ReviewerResult,
    ReviewTarget,
    validate_executor_result_context,
    validate_reviewer_result_context,
)
from .scheduler import SchedulingError, resource_keys, validate_batch
from .state_store import DispatchPayload, StateStore
from .workflow import (
    BatchRecord,
    BatchStatus,
    CompiledReviewObligation,
    DispatchIntent,
    DispatchRecord,
    DispatchStatus,
    OperatorRequest,
    RunRecord,
    RunStatus,
    StepRecord,
    StepStatus,
    TransitionEvent,
    UsageAmount,
    UsageLedger,
    completion_obligations,
    transition_batch,
    transition_run,
    transition_step,
)
from .workflow import RepositoryCoordinate as DispatchRepositoryCoordinate


class RepositoryInspector(Protocol):
    """Inspectable repository boundary used by production and deterministic tests."""

    def __call__(
        self,
        config: Config,
        repo_id: str,
        *,
        require_clean: bool,
    ) -> RepositorySnapshot: ...


class SequentialWorkflowError(ValueError):
    """A supervisor request or worker result violates dispatcher-owned invariants."""


T = TypeVar("T")


def _serialized_transition(method: Callable[..., T]) -> Callable[..., T]:
    """Serialize run mutations while worker processes continue concurrently."""

    @wraps(method)
    def wrapped(*args: Any, **kwargs: Any) -> T:
        workflow = cast("SequentialWorkflow", args[0])
        with workflow._transition_lock:
            return method(*args, **kwargs)

    return wrapped


@dataclass(frozen=True)
class PreparedDispatch:
    """The only launchable dispatch state returned by the sequential facade."""

    run_id: str
    generation: int
    dispatch: DispatchRecord
    prompt: str
    workdir: Path
    permission_config: dict[str, Any]
    auto_approve: bool
    lease_keys: tuple[str, ...]
    lease_owner_id: str
    review_target: ReviewTarget | None
    session_mode: Literal["new", "resume", "fork"]
    session_id: str | None
    repository_before: RepositorySnapshot


@dataclass(frozen=True)
class CompletionDecision:
    """The dispatcher-owned disposition of a supervisor completion request."""

    accepted: bool
    obligations: tuple[str, ...]
    report_path: Path | None


@dataclass(frozen=True)
class PreparedBatch:
    """An atomically prepared group of independently launchable dispatches."""

    run_id: str
    generation: int
    batch_id: str
    dispatches: tuple[PreparedDispatch, ...]


class SequentialWorkflow:
    """Validate one sequential supervisor/executor/reviewer workflow at a time.

    This class deliberately accepts typed protocol and result objects at its
    boundary. It never treats worker chat or supervisor prose as authorization,
    acceptance, or state transition data.
    """

    def __init__(
        self,
        config: Config,
        store: StateStore,
        *,
        owner_id: str,
        repository_inspector: RepositoryInspector = inspect_repository,
    ) -> None:
        self.config = config
        self.store = store
        self.owner_id = owner_id
        self._repository_inspector = repository_inspector
        self._transition_lock = threading.RLock()

    def render_bootstrap(self, run_id: str) -> tuple[str, Path]:
        """Render and persist a self-contained bootstrap from one approved run."""
        record, _generation = self.store.load_run(run_id)
        self._validate_bootstrap_record(record)
        template = _bootstrap_template()
        values = {
            "project_id": record.project_id,
            "project_name": self.config.project_name,
            "repositories": _repositories_markdown(self.config),
            "specifications": _specifications_markdown(self.config),
            "plans": _plan_sources_markdown(record),
            "plan_digest": record.plan_digest,
            "source_digest": record.plan.source_digest,
            "plan_approval": record.plan_approval.operator_decision_ref,
            "roles": _roles_markdown(self.config),
            "profile": self.config.profile_id,
            "baseline": _baseline_markdown(record),
            "dispatch_example": _dispatch_example(self.config, record),
            "completion_example": _completion_example(),
        }
        rendered = template.format(**values)
        path = self.store.write_transcript(
            run_id=run_id,
            dispatch_id=None,
            content=rendered,
            sequence=record.sequence,
            label="supervisor-bootstrap",
        )
        return rendered, path

    def activate(self, run_id: str, *, expected_generation: int) -> tuple[RunRecord, int]:
        """Move a NEW run through READY into RUNNING before a supervisor turn."""
        record, generation = self.store.load_run(run_id)
        if generation != expected_generation:
            raise SequentialWorkflowError("run generation changed before activation")
        record, generation = self._compile_run_policy(record, generation)
        record, generation = self.refresh_readiness(record, generation)
        if record.state is RunStatus.NEW:
            ready_event = self._event(record, "dispatcher", "run approved and ready", "run-activation")
            record = transition_run(record, RunStatus.READY, ready_event)
            generation = self.store.save_run(record, expected_generation=generation)
        if record.state is RunStatus.READY:
            running_event = self._event(record, "dispatcher", "supervisor workflow started", "run-activation")
            record = transition_run(record, RunStatus.RUNNING, running_event)
            generation = self.store.save_run(record, expected_generation=generation)
        if record.state is not RunStatus.RUNNING:
            raise SequentialWorkflowError(f"run is not dispatchable from state {record.state.value}")
        return record, generation

    def refresh_readiness(
        self,
        record: RunRecord,
        generation: int,
    ) -> tuple[RunRecord, int]:
        """Mark only dependency-satisfied pending steps ready in normalized order."""
        steps = dict(record.steps)
        changed = False
        sequence = record.sequence
        updated_at = record.updated_at
        for plan_step in record.plan.steps:
            step = steps[plan_step.step_id]
            if step.state is not StepStatus.PENDING:
                continue
            if not step.operator_gate_resolved:
                continue
            if any(
                steps[dependency_id].state not in {StepStatus.ACCEPTED, StepStatus.WAIVED}
                for dependency_id in plan_step.depends_on
            ):
                continue
            if any(
                artifact.producer_step_id is not None
                and steps[artifact.producer_step_id].state not in {StepStatus.ACCEPTED, StepStatus.WAIVED}
                for artifact in plan_step.required_inputs
            ):
                continue
            sequence += 1
            event = self._event(
                record,
                "dispatcher",
                "normalized dependencies are satisfied",
                plan_step.step_id,
                sequence=sequence,
            )
            steps[plan_step.step_id] = transition_step(step, StepStatus.READY, event)
            updated_at = event.occurred_at
            changed = True
        if not changed:
            return record, generation
        updated = record.model_copy(
            update={"steps": steps, "sequence": sequence, "updated_at": updated_at}
        )
        return updated, self.store.save_run(updated, expected_generation=generation)

    def prepare_from_supervisor(
        self,
        run_id: str,
        *,
        expected_generation: int,
        supervisor_text: str,
    ) -> PreparedDispatch | PreparedBatch | CompletionDecision | RunRecord:
        """Parse and apply one strict supervisor command without launching a process."""
        command = parse_supervisor_command(supervisor_text)
        record, generation = self.store.load_run(run_id)
        if generation != expected_generation:
            raise SequentialWorkflowError("run generation changed before supervisor command")
        if isinstance(command, DispatchCommand):
            return self.prepare_dispatch(record, generation, command)
        if isinstance(command, BatchDispatchCommand):
            return self.prepare_batch(record, generation, command)
        if isinstance(command, RequestCompletionCommand):
            return self.evaluate_completion(record, generation)
        if isinstance(command, AskOperatorCommand):
            return self._wait_for_operator(record, generation, command)
        if isinstance(command, RequestReviewWaiverCommand):
            return self._request_review_waiver(record, generation, command)
        if isinstance(command, HaltCommand):
            event = self._event(record, "supervisor", command.reason, "supervisor-halt")
            halted = transition_run(record, RunStatus.HALTED, event)
            self.store.save_run(halted, expected_generation=generation)
            return halted
        raise SequentialWorkflowError(f"unsupported supervisor command: {type(command).__name__}")

    def prepare_dispatch(
        self,
        record: RunRecord,
        generation: int,
        command: DispatchCommand,
    ) -> PreparedDispatch | RunRecord:
        """Commit a fully validated PREPARED dispatch before any worker launch."""
        if record.state is not RunStatus.RUNNING:
            raise SequentialWorkflowError("only RUNNING runs may prepare a dispatch")
        if self.config.execution.scheduling == "sequential" and any(
            dispatch.state
            in {
                DispatchStatus.PREPARED,
                DispatchStatus.RUNNING,
                DispatchStatus.COMPLETED,
                DispatchStatus.FORWARDED,
            }
            for dispatch in record.dispatches.values()
        ):
            raise SequentialWorkflowError("sequential workflow already has an unresolved dispatch")
        if self.config.execution.scheduling == "bounded_parallel":
            try:
                validate_batch(self.config, record, (command,))
            except SchedulingError as exc:
                raise SequentialWorkflowError(f"dispatch is not schedulable: {exc}") from exc
        step = _plan_step(record, command.step_id)
        if command.repo_id is not None and command.repo_id != step.repo_id:
            raise SequentialWorkflowError(
                f"supervisor repository assertion {command.repo_id!r} does not match step repository {step.repo_id!r}"
            )
        role_kind = self._role_kind(command.target_role)
        if not record.steps[step.step_id].operator_gate_resolved:
            return self._request_risk_gate(record, generation, step)
        self._ensure_budget_allows_dispatch(
            record,
            step,
            role_key=command.target_role,
            session_mode=command.session_mode,
        )
        self._validate_step_readiness(record, step, role_kind, command.session_mode, command.target_role)
        workdir = self.config.repository_root(step.repo_id)
        repository_before = self._inspect_repository(step.repo_id, require_clean=True)
        policy = generate_opencode_config(
            compile_effective_policy(
                self.config,
                repo_id=step.repo_id,
                role_key=command.target_role,
                dispatch_authorized_actions=step.authorization.authorized_actions,
            )
        )
        policy_rules = policy["permission"]
        step_record = record.steps[step.step_id]
        attempt = step_record.executor_attempts + 1 if role_kind == "executor" else step_record.reviewer_attempts + 1
        dispatch_id = f"dispatch-{uuid.uuid4().hex}"
        event = self._event(record, "dispatcher", f"prepared {role_kind} dispatch", dispatch_id)
        review_target = self._review_target(record, step) if role_kind == "reviewer" else None
        worker_prompt = _worker_prompt(
            dispatch_id=dispatch_id,
            attempt=attempt,
            role_kind=role_kind,
            step=step,
            task=command.prompt,
            repository=repository_before.dispatch_coordinate(),
            review_target=review_target,
        )
        dispatch = DispatchRecord(
            dispatch_id=dispatch_id,
            step_id=step.step_id,
            role_key=command.target_role,
            role_kind=role_kind,
            attempt=attempt,
            logical_session_key=f"{role_kind}-{command.target_role}-{step.step_id}",
            runtime_session_id=None,
            state=DispatchStatus.PREPARED,
            intent=DispatchIntent(
                prompt_sha256=_sha256_text(worker_prompt),
                policy_digest=_sha256_json(policy),
                expected_result_kind=role_kind,
                repository=repository_before.dispatch_coordinate(),
                idempotency_key=f"idempotency-{uuid.uuid4().hex}",
            ),
            result_digest=None,
            forwarding_digest=None,
            last_event=event,
        )
        target_state = StepStatus.EXECUTING if role_kind == "executor" else StepStatus.REVIEWING
        transitioned_step = transition_step(
            step_record,
            target_state,
            event,
            active_dispatch_id=dispatch_id,
        )
        transitioned_step = transitioned_step.model_copy(
            update={
                "executor_attempts": attempt if role_kind == "executor" else step_record.executor_attempts,
                "reviewer_attempts": attempt if role_kind == "reviewer" else step_record.reviewer_attempts,
                "reassignment_role_key": None if role_kind == "executor" else step_record.reassignment_role_key,
            }
        )
        steps = dict(record.steps)
        steps[step.step_id] = transitioned_step
        dispatches = dict(record.dispatches)
        dispatches[dispatch_id] = dispatch
        updated = record.model_copy(
            update={"steps": steps, "dispatches": dispatches, "sequence": event.sequence, "updated_at": event.occurred_at}
        )
        lease_keys = _lease_keys(step)
        lease_owner_id = _dispatch_lease_owner_id(self.owner_id, dispatch_id)
        session_id = self._owned_session_id(record.run_id, role_kind, command.target_role, command.session_mode)
        self.store.acquire_resource_leases(
            run_id=record.run_id,
            owner_id=lease_owner_id,
            resource_keys=lease_keys,
        )
        try:
            next_generation = self.store.prepare_dispatch(
                updated,
                expected_generation=generation,
                dispatch=dispatch,
                prompt=worker_prompt,
                policy=policy,
                repository_before=repository_before.model_dump(mode="json"),
                session_metadata={
                    "session_mode": command.session_mode,
                    "parent_session_id": session_id,
                    "review_target": (
                        review_target.model_dump(mode="json") if review_target is not None else None
                    ),
                },
            )
        except Exception:
            self.store.release_leases(owner_id=lease_owner_id, resource_keys=lease_keys)
            raise
        return PreparedDispatch(
            run_id=record.run_id,
            generation=next_generation,
            dispatch=dispatch,
            prompt=worker_prompt,
            workdir=workdir,
            permission_config=policy,
            auto_approve=should_auto_approve(policy_rules),
            lease_keys=lease_keys,
            lease_owner_id=lease_owner_id,
            review_target=review_target,
            session_mode=command.session_mode,
            session_id=session_id,
            repository_before=repository_before,
        )

    def prepare_batch(
        self,
        record: RunRecord,
        generation: int,
        command: BatchDispatchCommand,
    ) -> PreparedBatch:
        """Atomically prepare every independently valid child in a protocol-v2 batch."""
        if record.state is not RunStatus.RUNNING:
            raise SequentialWorkflowError("only RUNNING runs may prepare a batch")
        try:
            children = validate_batch(self.config, record, tuple(command.children))
        except SchedulingError as exc:
            raise SequentialWorkflowError(f"batch is not schedulable: {exc}") from exc

        batch_id = f"batch-{uuid.uuid4().hex}"
        working = record
        prepared_children: list[PreparedDispatch] = []
        for child in children:
            step = _plan_step(working, child.step_id)
            if child.repo_id is not None and child.repo_id != step.repo_id:
                raise SequentialWorkflowError(
                    f"supervisor repository assertion {child.repo_id!r} does not match step repository {step.repo_id!r}"
                )
            role_kind = self._role_kind(child.target_role)
            if not working.steps[step.step_id].operator_gate_resolved:
                raise SequentialWorkflowError(f"batch step {step.step_id} has an unresolved operator gate")
            self._ensure_budget_allows_dispatch(
                working,
                step,
                role_key=child.target_role,
                session_mode=child.session_mode,
            )
            self._validate_step_readiness(working, step, role_kind, child.session_mode, child.target_role)
            workdir = self.config.repository_root(step.repo_id)
            repository_before = self._inspect_repository(step.repo_id, require_clean=True)
            policy = generate_opencode_config(
                compile_effective_policy(
                    self.config,
                    repo_id=step.repo_id,
                    role_key=child.target_role,
                    dispatch_authorized_actions=step.authorization.authorized_actions,
                )
            )
            step_record = working.steps[step.step_id]
            attempt = (
                step_record.executor_attempts + 1
                if role_kind == "executor"
                else step_record.reviewer_attempts + 1
            )
            dispatch_id = f"dispatch-{uuid.uuid4().hex}"
            event = self._event(
                working,
                "dispatcher",
                f"prepared {role_kind} batch dispatch",
                dispatch_id,
            )
            review_target = self._review_target(working, step) if role_kind == "reviewer" else None
            worker_prompt = _worker_prompt(
                dispatch_id=dispatch_id,
                attempt=attempt,
                role_kind=role_kind,
                step=step,
                task=child.prompt,
                repository=repository_before.dispatch_coordinate(),
                review_target=review_target,
            )
            dispatch = DispatchRecord(
                dispatch_id=dispatch_id,
                batch_id=batch_id,
                step_id=step.step_id,
                role_key=child.target_role,
                role_kind=role_kind,
                attempt=attempt,
                logical_session_key=f"{role_kind}-{child.target_role}-{step.step_id}",
                runtime_session_id=None,
                state=DispatchStatus.PREPARED,
                intent=DispatchIntent(
                    prompt_sha256=_sha256_text(worker_prompt),
                    policy_digest=_sha256_json(policy),
                    expected_result_kind=role_kind,
                    repository=repository_before.dispatch_coordinate(),
                    idempotency_key=f"idempotency-{uuid.uuid4().hex}",
                ),
                result_digest=None,
                forwarding_digest=None,
                last_event=event,
            )
            target_state = StepStatus.EXECUTING if role_kind == "executor" else StepStatus.REVIEWING
            transitioned_step = transition_step(
                step_record,
                target_state,
                event,
                active_dispatch_id=dispatch_id,
            ).model_copy(
                update={
                    "executor_attempts": attempt if role_kind == "executor" else step_record.executor_attempts,
                    "reviewer_attempts": attempt if role_kind == "reviewer" else step_record.reviewer_attempts,
                    "reassignment_role_key": (
                        None if role_kind == "executor" else step_record.reassignment_role_key
                    ),
                }
            )
            steps = dict(working.steps)
            steps[step.step_id] = transitioned_step
            dispatches = dict(working.dispatches)
            dispatches[dispatch_id] = dispatch
            working = working.model_copy(
                update={
                    "steps": steps,
                    "dispatches": dispatches,
                    "sequence": event.sequence,
                    "updated_at": event.occurred_at,
                }
            )
            lease_keys = _lease_keys(step)
            prepared_children.append(
                PreparedDispatch(
                    run_id=record.run_id,
                    generation=generation,
                    dispatch=dispatch,
                    prompt=worker_prompt,
                    workdir=workdir,
                    permission_config=policy,
                    auto_approve=should_auto_approve(policy["permission"]),
                    lease_keys=lease_keys,
                    lease_owner_id=_dispatch_lease_owner_id(self.owner_id, dispatch_id),
                    review_target=review_target,
                    session_mode=child.session_mode,
                    session_id=self._owned_session_id(
                        record.run_id,
                        role_kind,
                        child.target_role,
                        child.session_mode,
                    ),
                    repository_before=repository_before,
                )
            )

        batch_event = self._event(working, "dispatcher", "prepared dispatch batch", batch_id)
        batch = BatchRecord(
            batch_id=batch_id,
            dispatch_ids=tuple(item.dispatch.dispatch_id for item in prepared_children),
            state=BatchStatus.PREPARED,
            failure_mode=self.config.execution.concurrency.failure_mode,
            last_event=batch_event,
        )
        batches = dict(working.batches)
        batches[batch_id] = batch
        working = working.model_copy(
            update={"batches": batches, "sequence": batch_event.sequence, "updated_at": batch_event.occurred_at}
        )
        acquired: list[PreparedDispatch] = []
        try:
            for prepared in prepared_children:
                self.store.acquire_resource_leases(
                    run_id=record.run_id,
                    owner_id=prepared.lease_owner_id,
                    resource_keys=prepared.lease_keys,
                )
                acquired.append(prepared)
            next_generation = self.store.prepare_dispatch_batch(
                working,
                expected_generation=generation,
                dispatch_payloads={
                    prepared.dispatch.dispatch_id: DispatchPayload(
                        prompt=prepared.prompt,
                        policy=prepared.permission_config,
                        repository_before=prepared.repository_before.model_dump(mode="json"),
                        session_metadata={
                            "session_mode": prepared.session_mode,
                            "parent_session_id": prepared.session_id,
                            "review_target": (
                                prepared.review_target.model_dump(mode="json")
                                if prepared.review_target is not None
                                else None
                            ),
                        },
                    )
                    for prepared in prepared_children
                },
            )
        except Exception:
            for prepared in acquired:
                self.store.release_leases(
                    owner_id=prepared.lease_owner_id,
                    resource_keys=prepared.lease_keys,
                )
            raise
        return PreparedBatch(
            run_id=record.run_id,
            generation=next_generation,
            batch_id=batch_id,
            dispatches=tuple(replace(item, generation=next_generation) for item in prepared_children),
        )

    @_serialized_transition
    def mark_running(
        self,
        prepared: PreparedDispatch,
        *,
        process_id: int,
    ) -> PreparedDispatch:
        """Durably record a worker launch before accepting any worker result."""
        record, generation = self.store.load_run(prepared.run_id)
        if generation != prepared.generation and self.config.execution.scheduling == "sequential":
            raise SequentialWorkflowError("prepared dispatch generation is stale")
        dispatch = record.dispatches[prepared.dispatch.dispatch_id]
        if dispatch.batch_id is not None:
            batch = record.batches[dispatch.batch_id]
            if batch.state is BatchStatus.PREPARED:
                batch_event = self._event(record, "dispatcher", "batch worker process launched", batch.batch_id)
                batches = dict(record.batches)
                batches[batch.batch_id] = transition_batch(
                    batch,
                    BatchStatus.RUNNING,
                    batch_event,
                )
                record = record.model_copy(
                    update={
                        "batches": batches,
                        "sequence": batch_event.sequence,
                        "updated_at": batch_event.occurred_at,
                    }
                )
        event = self._event(record, "dispatcher", "worker process launched", prepared.dispatch.dispatch_id)
        updated, next_generation = self.store.commit_dispatch_transition(
            record,
            expected_generation=generation,
            dispatch_id=prepared.dispatch.dispatch_id,
            target=DispatchStatus.RUNNING,
            event=event,
            process_id=process_id,
        )
        return PreparedDispatch(
            run_id=prepared.run_id,
            generation=next_generation,
            dispatch=updated.dispatches[prepared.dispatch.dispatch_id],
            prompt=prepared.prompt,
            workdir=prepared.workdir,
            permission_config=prepared.permission_config,
            auto_approve=prepared.auto_approve,
            lease_keys=prepared.lease_keys,
            lease_owner_id=prepared.lease_owner_id,
            review_target=prepared.review_target,
            session_mode=prepared.session_mode,
            session_id=prepared.session_id,
            repository_before=prepared.repository_before,
        )

    @_serialized_transition
    def record_session_id(
        self,
        running: PreparedDispatch,
        *,
        runtime_session_id: str,
    ) -> PreparedDispatch:
        """Bind the first validated OpenCode session ID to the running attempt."""
        record, generation = self.store.load_run(running.run_id)
        if generation != running.generation and self.config.execution.scheduling == "sequential":
            raise SequentialWorkflowError("running dispatch generation is stale")
        dispatch = record.dispatches[running.dispatch.dispatch_id]
        if dispatch.state is not DispatchStatus.RUNNING:
            raise SequentialWorkflowError("session ID arrived for a dispatch that is not RUNNING")
        event = self._event(record, "dispatcher", "OpenCode session identified", dispatch.dispatch_id)
        pool = "executors" if dispatch.role_kind == "executor" else "reviewers"
        session_registry_key = dispatch.logical_session_key if dispatch.batch_id is not None else dispatch.role_key
        updated, next_generation = self.store.bind_dispatch_session(
            record,
            expected_generation=generation,
            dispatch_id=dispatch.dispatch_id,
            runtime_session_id=runtime_session_id,
            event=event,
            pool=pool,
            role_key=session_registry_key,
            session_entry={
                "session_id": runtime_session_id,
                "logical_session_key": dispatch.logical_session_key,
                "role_key": dispatch.role_key,
                "working_directory": str(running.workdir),
                "status": "active",
            },
        )
        return PreparedDispatch(
            run_id=running.run_id,
            generation=next_generation,
            dispatch=updated.dispatches[dispatch.dispatch_id],
            prompt=running.prompt,
            workdir=running.workdir,
            permission_config=running.permission_config,
            auto_approve=running.auto_approve,
            lease_keys=running.lease_keys,
            lease_owner_id=running.lease_owner_id,
            review_target=running.review_target,
            session_mode=running.session_mode,
            session_id=running.session_id,
            repository_before=running.repository_before,
        )

    @_serialized_transition
    def apply_executor_result(
        self,
        prepared: PreparedDispatch,
        result: ExecutorResult,
        *,
        usage: Mapping[str, object] | None = None,
    ) -> tuple[RunRecord, int, str]:
        """Apply a typed executor result and persist the next supervisor message."""
        if prepared.dispatch.role_kind != "executor":
            raise SequentialWorkflowError("executor result does not match a reviewer dispatch")
        record, generation = self.store.load_run(prepared.run_id)
        if generation != prepared.generation and self.config.execution.scheduling == "sequential":
            raise SequentialWorkflowError("running dispatch generation is stale")
        dispatch = record.dispatches[prepared.dispatch.dispatch_id]
        record, generation = self._record_usage(record, generation, dispatch, usage)
        _validate_executor_result(result, dispatch)
        step = _plan_step(record, dispatch.step_id)
        self._validate_executor_evidence(step, result)
        repository_after = self._inspect_repository(dispatch.intent.repository.repo_id, require_clean=False)
        try:
            validate_executor_snapshot(
                self.config,
                coordinate=dispatch.intent.repository,
                before=prepared.repository_before,
                after=repository_after,
                result=result,
            )
        except RepositoryValidationError as exc:
            raise SequentialWorkflowError(str(exc)) from exc
        completion_event = self._event(record, "executor", "typed executor result received", dispatch.dispatch_id)
        record, generation = self.store.commit_dispatch_transition(
            record,
            expected_generation=generation,
            dispatch_id=dispatch.dispatch_id,
            target=DispatchStatus.COMPLETED,
            event=completion_event,
            result_digest=_sha256_json(result.model_dump(mode="json")),
            result=result.model_dump(mode="json"),
            repository_after=repository_after.model_dump(mode="json"),
        )
        step_record = record.steps[step.step_id]
        result_event = self._event(record, "dispatcher", f"executor outcome {result.outcome}", dispatch.dispatch_id)
        escalation_required = False
        if isinstance(result, ExecutorCompletedResult):
            executed = transition_step(step_record, StepStatus.EXECUTED, result_event)
            obligation = self._review_obligation(record, step)
            target = StepStatus.REVIEW_REQUIRED if obligation.required else StepStatus.ACCEPTED
            final_event = self._event(
                record,
                "dispatcher",
                f"step moved to {target.value}",
                dispatch.dispatch_id,
                sequence=result_event.sequence + 1,
            )
            updated_step = transition_step(executed, target, final_event)
            updated_step = updated_step.model_copy(
                update={
                    "accepted_artifact_ids": [item.artifact_id for item in result.evidence],
                    # A fresh executor result invalidates prior reviewer votes on older work.
                    "review_acceptances": 0,
                    "accepted_reviewer_role_keys": [],
                }
            )
        elif result.outcome == "blocked":
            blocked = transition_step(step_record, StepStatus.BLOCKED, result_event)
            updated_step = self._retry_or_terminal_step(
                record,
                step,
                blocked,
                policy=step.retry.on_blocked,
                correlation_id=dispatch.dispatch_id,
            )
            escalation_required = step.retry.on_blocked == "escalate"
        else:
            if step.retry.on_failed == "retry" and step_record.executor_attempts < step.retry.max_executor_attempts:
                blocked = transition_step(step_record, StepStatus.BLOCKED, result_event)
                updated_step = self._retry_or_terminal_step(
                    record,
                    step,
                    blocked,
                    policy="retry",
                    correlation_id=dispatch.dispatch_id,
                )
            elif step.retry.on_failed == "escalate":
                updated_step = transition_step(step_record, StepStatus.BLOCKED, result_event)
                escalation_required = True
            else:
                updated_step = transition_step(step_record, StepStatus.FAILED, result_event)
        record, generation = self._replace_step(record, generation, updated_step)
        forwarding = _executor_forwarding(
            result,
            record.usage.by_session.get(
                dispatch.runtime_session_id or dispatch.logical_session_key,
                UsageAmount(),
            ),
        )
        forward_event = self._event(record, "dispatcher", "supervisor forwarding persisted", dispatch.dispatch_id)
        record, generation = self.store.commit_dispatch_transition(
            record,
            expected_generation=generation,
            dispatch_id=dispatch.dispatch_id,
            target=DispatchStatus.FORWARDED,
            event=forward_event,
            forwarding_digest=_sha256_text(forwarding),
            forwarding_payload=forwarding,
        )
        self.store.release_leases(owner_id=prepared.lease_owner_id, resource_keys=prepared.lease_keys)
        if escalation_required:
            return self._request_escalation(record, generation, step, dispatch, forwarding)
        return self._apply_budget_limit(record, generation, dispatch, forwarding)

    @_serialized_transition
    def apply_reviewer_result(
        self,
        prepared: PreparedDispatch,
        result: ReviewerResult,
        *,
        usage: Mapping[str, object] | None = None,
    ) -> tuple[RunRecord, int, str]:
        """Apply an immutable reviewer verdict to the exact reviewed work product."""
        if prepared.dispatch.role_kind != "reviewer" or prepared.review_target is None:
            raise SequentialWorkflowError("reviewer result does not match a prepared review dispatch")
        record, generation = self.store.load_run(prepared.run_id)
        if generation != prepared.generation and self.config.execution.scheduling == "sequential":
            raise SequentialWorkflowError("running review generation is stale")
        dispatch = record.dispatches[prepared.dispatch.dispatch_id]
        record, generation = self._record_usage(record, generation, dispatch, usage)
        expectation = ResultExpectation(
            dispatch_id=dispatch.dispatch_id,
            attempt=dispatch.attempt,
            step_id=dispatch.step_id,
            repo_id=dispatch.intent.repository.repo_id,
            expected_review_target=prepared.review_target,
        )
        try:
            validate_reviewer_result_context(result, expectation)
        except ResultError as exc:
            raise SequentialWorkflowError(str(exc)) from exc
        repository_after = self._inspect_repository(dispatch.intent.repository.repo_id, require_clean=False)
        try:
            validate_review_snapshot(
                self.config,
                coordinate=dispatch.intent.repository,
                before=prepared.repository_before,
                after=repository_after,
                review_target=result.review_target,
            )
        except RepositoryValidationError as exc:
            raise SequentialWorkflowError(str(exc)) from exc
        completion_event = self._event(record, "reviewer", "typed reviewer result received", dispatch.dispatch_id)
        record, generation = self.store.commit_dispatch_transition(
            record,
            expected_generation=generation,
            dispatch_id=dispatch.dispatch_id,
            target=DispatchStatus.COMPLETED,
            event=completion_event,
            result_digest=_sha256_json(result.model_dump(mode="json")),
            result=result.model_dump(mode="json"),
            repository_after=repository_after.model_dump(mode="json"),
        )
        self.store.record_review(
            run_id=record.run_id,
            dispatch_id=dispatch.dispatch_id,
            review_id=f"review-{uuid.uuid4().hex}",
            review=result.model_dump(mode="json"),
        )
        step = _plan_step(record, dispatch.step_id)
        step_record = record.steps[step.step_id]
        verdict_event = self._event(record, "dispatcher", f"review verdict {result.verdict}", dispatch.dispatch_id)
        escalation_required = False
        if isinstance(result, ReviewerAcceptedResult):
            obligation = self._review_obligation(record, step)
            if dispatch.role_key in step_record.accepted_reviewer_role_keys:
                raise SequentialWorkflowError("reviewer role already accepted this immutable artifact")
            accepted_roles = [*step_record.accepted_reviewer_role_keys, dispatch.role_key]
            accepted_count = step_record.review_acceptances + 1
            target = (
                StepStatus.ACCEPTED
                if accepted_count >= obligation.required_acceptances
                else StepStatus.REVIEW_REQUIRED
            )
            updated_step = transition_step(step_record, target, verdict_event)
            updated_step = updated_step.model_copy(
                update={
                    "review_acceptances": accepted_count,
                    "accepted_reviewer_role_keys": accepted_roles,
                    "accepted_artifact_ids": (
                        [requirement.artifact_id for requirement in step.evidence_requirements]
                        if target is StepStatus.ACCEPTED
                        else []
                    ),
                }
            )
        elif isinstance(result, ReviewerChangesRequestedResult):
            changed = transition_step(step_record, StepStatus.CHANGES_REQUESTED, verdict_event)
            changed = changed.model_copy(update={"rework_rounds": step_record.rework_rounds + 1})
            obligation = self._review_obligation(record, step)
            remaining_reviewer_roles = set(obligation.reviewer_role_keys) - {
                *step_record.accepted_reviewer_role_keys,
                dispatch.role_key,
            }
            if remaining_reviewer_roles and step_record.review_acceptances:
                tie_break_event = self._event(
                    record,
                    "dispatcher",
                    "conflicting review requires a fresh tie-break on the immutable artifact",
                    dispatch.dispatch_id,
                    sequence=verdict_event.sequence + 1,
                )
                updated_step = transition_step(changed, StepStatus.REVIEW_REQUIRED, tie_break_event)
            elif (
                step.retry.on_changes_requested == "retry"
                and changed.rework_rounds < self.config.execution.max_rounds_per_step
                and (
                step_record.executor_attempts < step.retry.max_executor_attempts
                )
            ):
                ready_event = self._event(
                    record,
                    "dispatcher",
                    "review rework is ready",
                    dispatch.dispatch_id,
                    sequence=verdict_event.sequence + 1,
                )
                updated_step = transition_step(changed, StepStatus.READY, ready_event)
            elif step.retry.on_changes_requested == "escalate":
                blocked_event = self._event(
                    record,
                    "dispatcher",
                    "review rework requires escalation",
                    dispatch.dispatch_id,
                    sequence=verdict_event.sequence + 1,
                )
                updated_step = transition_step(changed, StepStatus.BLOCKED, blocked_event)
                escalation_required = True
            else:
                failed_event = self._event(
                    record,
                    "dispatcher",
                    "review rework policy halted the step",
                    dispatch.dispatch_id,
                    sequence=verdict_event.sequence + 1,
                )
                updated_step = transition_step(changed, StepStatus.FAILED, failed_event)
        else:
            blocked = transition_step(step_record, StepStatus.BLOCKED, verdict_event)
            if step.retry.on_blocked == "retry" and step_record.reviewer_attempts < step.retry.max_reviewer_attempts:
                retry_event = self._event(
                    record,
                    "dispatcher",
                    "review retry is ready",
                    dispatch.dispatch_id,
                    sequence=verdict_event.sequence + 1,
                )
                updated_step = transition_step(blocked, StepStatus.REVIEW_REQUIRED, retry_event)
            elif step.retry.on_blocked == "escalate":
                updated_step = blocked
                escalation_required = True
            else:
                failed_event = self._event(
                    record,
                    "dispatcher",
                    "review blocked policy halted the step",
                    dispatch.dispatch_id,
                    sequence=verdict_event.sequence + 1,
                )
                updated_step = transition_step(blocked, StepStatus.FAILED, failed_event)
        record, generation = self._replace_step(record, generation, updated_step)
        forwarding = _reviewer_forwarding(
            result,
            record.usage.by_session.get(
                dispatch.runtime_session_id or dispatch.logical_session_key,
                UsageAmount(),
            ),
        )
        forward_event = self._event(record, "dispatcher", "review forwarding persisted", dispatch.dispatch_id)
        record, generation = self.store.commit_dispatch_transition(
            record,
            expected_generation=generation,
            dispatch_id=dispatch.dispatch_id,
            target=DispatchStatus.FORWARDED,
            event=forward_event,
            forwarding_digest=_sha256_text(forwarding),
            forwarding_payload=forwarding,
        )
        self.store.release_leases(owner_id=prepared.lease_owner_id, resource_keys=prepared.lease_keys)
        if escalation_required:
            return self._request_escalation(record, generation, step, dispatch, forwarding)
        return self._apply_budget_limit(record, generation, dispatch, forwarding)

    def acknowledge_forwarding(
        self,
        run_id: str,
        *,
        expected_generation: int,
        dispatch_id: str,
    ) -> tuple[RunRecord, int]:
        """Record supervisor acknowledgement separately from the forwarding write."""
        record, generation = self.store.load_run(run_id)
        if generation != expected_generation:
            raise SequentialWorkflowError("forwarding acknowledgement generation is stale")
        event = self._event(record, "supervisor", "supervisor forwarding acknowledged", dispatch_id)
        return self.store.commit_dispatch_transition(
            record,
            expected_generation=generation,
            dispatch_id=dispatch_id,
            target=DispatchStatus.ACKNOWLEDGED,
            event=event,
        )

    @_serialized_transition
    def fail_dispatch(
        self,
        prepared: PreparedDispatch,
        *,
        reason: str,
    ) -> tuple[RunRecord, int]:
        """Record a failed adapter/result boundary without advancing the plan step."""
        record, generation = self.store.load_run(prepared.run_id)
        if generation != prepared.generation and self.config.execution.scheduling == "sequential":
            raise SequentialWorkflowError("failed dispatch generation is stale")
        dispatch = record.dispatches[prepared.dispatch.dispatch_id]
        if dispatch.state is DispatchStatus.PREPARED:
            target = DispatchStatus.ABANDONED
        elif dispatch.state is DispatchStatus.RUNNING:
            target = DispatchStatus.FAILED
        else:
            raise SequentialWorkflowError(
                f"cannot fail dispatch from state {dispatch.state.value}"
            )
        event = self._event(record, "dispatcher", reason, dispatch.dispatch_id)
        record, generation = self.store.commit_dispatch_transition(
            record,
            expected_generation=generation,
            dispatch_id=dispatch.dispatch_id,
            target=target,
            event=event,
        )
        step = record.steps[dispatch.step_id]
        step_event = self._event(
            record,
            "dispatcher",
            "worker boundary failed",
            dispatch.dispatch_id,
        )
        if step.state is StepStatus.EXECUTING:
            updated_step = transition_step(step, StepStatus.BLOCKED, step_event)
        elif step.state is StepStatus.REVIEWING:
            updated_step = transition_step(step, StepStatus.REVIEW_REQUIRED, step_event)
        else:
            raise SequentialWorkflowError(
                f"failed dispatch has incompatible step state {step.state.value}"
            )
        record, generation = self._replace_step(record, generation, updated_step)
        if dispatch.state is DispatchStatus.RUNNING and dispatch.batch_id is None:
            request_event = self._event(
                record,
                "dispatcher",
                "uncertain external side effect requires operator reconciliation",
                dispatch.dispatch_id,
            )
            request = OperatorRequest(
                request_id=f"request-{uuid.uuid4().hex}",
                question=(
                    "The worker process started but did not produce an applicable result. "
                    "Reconcile repository and session state before any retry."
                ),
                allowed_answers=["reconcile", "halt"],
                context_ref=dispatch.dispatch_id,
                resume_to=RunStatus.RUNNING,
                expires_at=None,
                required_role=None,
                kind="reconciliation",
                step_id=dispatch.step_id,
            )
            record = transition_run(
                record,
                RunStatus.WAITING_OPERATOR,
                request_event,
                operator_request=request,
            )
            generation = self.store.save_run(record, expected_generation=generation)
        self.store.release_leases(owner_id=prepared.lease_owner_id, resource_keys=prepared.lease_keys)
        return record, generation

    @_serialized_transition
    def finalize_batch(
        self,
        run_id: str,
        *,
        expected_generation: int,
        batch_id: str,
    ) -> tuple[RunRecord, int, str]:
        """Join completed child attempts and expose every child disposition to the supervisor."""
        record, generation = self.store.load_run(run_id)
        if generation != expected_generation and self.config.execution.scheduling == "sequential":
            raise SequentialWorkflowError("batch generation is stale")
        try:
            batch = record.batches[batch_id]
        except KeyError as exc:
            raise SequentialWorkflowError(f"unknown batch {batch_id}") from exc
        if batch.state not in {BatchStatus.PREPARED, BatchStatus.RUNNING}:
            raise SequentialWorkflowError(f"batch {batch_id} is already joined")
        child_dispatches = [record.dispatches[dispatch_id] for dispatch_id in batch.dispatch_ids]
        unresolved = [
            dispatch.dispatch_id
            for dispatch in child_dispatches
            if dispatch.state in {
                DispatchStatus.PREPARED,
                DispatchStatus.RUNNING,
                DispatchStatus.COMPLETED,
            }
        ]
        if unresolved:
            raise SequentialWorkflowError(
                f"batch {batch_id} cannot join with unresolved dispatches {sorted(unresolved)}"
            )
        failed = tuple(
            dispatch.dispatch_id
            for dispatch in child_dispatches
            if dispatch.state in {DispatchStatus.FAILED, DispatchStatus.ABANDONED}
        )
        event = self._event(
            record,
            "dispatcher",
            "batch joined with failures" if failed else "batch joined successfully",
            batch_id,
        )
        batches = dict(record.batches)
        joined = transition_batch(batch, BatchStatus.FAILED if failed else BatchStatus.JOINED, event)
        batches[batch_id] = joined.model_copy(update={"failed_dispatch_ids": failed})
        record = record.model_copy(
            update={"batches": batches, "sequence": event.sequence, "updated_at": event.occurred_at}
        )
        generation = self.store.save_run(record, expected_generation=generation)
        if failed and record.state is RunStatus.RUNNING:
            request_event = self._event(record, "dispatcher", "batch requires operator reconciliation", batch_id)
            request = OperatorRequest(
                request_id=f"request-{uuid.uuid4().hex}",
                question=(
                    f"Batch {batch_id} has failed dispatches {', '.join(failed)}. "
                    "Reconcile every child before continuing."
                ),
                allowed_answers=["reconcile", "halt"],
                context_ref=batch_id,
                resume_to=RunStatus.RUNNING,
                expires_at=None,
                required_role=None,
                kind="batch_reconciliation",
                step_id=None,
            )
            record = transition_run(record, RunStatus.WAITING_OPERATOR, request_event, operator_request=request)
            generation = self.store.save_run(record, expected_generation=generation)
        forwarding = json.dumps(
            {
                "kind": "batch_result",
                "batch_id": batch_id,
                "children": [
                    {
                        "dispatch_id": dispatch.dispatch_id,
                        "step_id": dispatch.step_id,
                        "state": dispatch.state.value,
                    }
                    for dispatch in child_dispatches
                ],
                "failed_dispatch_ids": list(failed),
            },
            sort_keys=True,
        )
        return record, generation, forwarding

    def evaluate_completion(self, record: RunRecord, generation: int) -> CompletionDecision:
        """Evaluate dispatcher-owned completion obligations, never supervisor prose."""
        obligations = completion_obligations(record)
        if obligations:
            return CompletionDecision(
                accepted=False,
                obligations=tuple(f"{item.code}: {item.detail}" for item in obligations),
                report_path=None,
            )
        event = self._event(record, "dispatcher", "completion guard passed", "completion")
        succeeded = transition_run(record, RunStatus.SUCCEEDED, event)
        report_path = self.store.export_run_report(
            record.run_id,
            record_override=succeeded,
            generation_override=generation + 1,
        )
        self.store.complete_run(
            succeeded,
            expected_generation=generation,
            event_id=event.event_id,
            sequence=event.sequence,
            correlation_id=event.correlation_id,
            report_path=str(report_path.relative_to(self.store.state_dir)),
        )
        return CompletionDecision(accepted=True, obligations=(), report_path=report_path)

    def _replace_step(
        self,
        record: RunRecord,
        generation: int,
        updated_step: StepRecord,
    ) -> tuple[RunRecord, int]:
        steps = dict(record.steps)
        steps[updated_step.step_id] = updated_step
        updated = record.model_copy(
            update={
                "steps": steps,
                "sequence": updated_step.last_event.sequence,
                "updated_at": updated_step.last_event.occurred_at,
            }
        )
        return updated, self.store.save_run(updated, expected_generation=generation)

    def _wait_for_operator(
        self,
        record: RunRecord,
        generation: int,
        command: AskOperatorCommand,
    ) -> RunRecord:
        if record.policy is None or record.policy.underspec_mode != "ask":
            raise SequentialWorkflowError(
                "underspecification requests are denied when execution.underspec_mode is auto"
            )
        event = self._event(record, "supervisor", "operator input requested", "operator-request")
        request = OperatorRequest(
            request_id=f"request-{uuid.uuid4().hex}",
            question=command.question,
            allowed_answers=["answer", "halt"],
            context_ref=command.step_id or "run",
            resume_to=RunStatus.RUNNING,
            expires_at=None,
            required_role=None,
            kind="underspecification",
            step_id=command.step_id,
        )
        waiting = transition_run(record, RunStatus.WAITING_OPERATOR, event, operator_request=request)
        self.store.save_run(waiting, expected_generation=generation)
        return waiting

    def _request_review_waiver(
        self,
        record: RunRecord,
        generation: int,
        command: RequestReviewWaiverCommand,
    ) -> RunRecord:
        step = _plan_step(record, command.step_id)
        obligation = self._review_obligation(record, step)
        if record.steps[step.step_id].state is not StepStatus.REVIEW_REQUIRED:
            raise SequentialWorkflowError("review waiver requires a step awaiting review")
        if not obligation.waivable:
            raise SequentialWorkflowError("compiled review obligation cannot be waived")
        event = self._event(record, "supervisor", command.rationale, step.step_id)
        request = OperatorRequest(
            request_id=f"request-{uuid.uuid4().hex}",
            question=f"Waive the non-mandatory review obligation for step {step.step_id}?",
            allowed_answers=["waive", "halt"],
            context_ref=step.step_id,
            resume_to=RunStatus.RUNNING,
            expires_at=None,
            required_role=None,
            kind="review_waiver",
            step_id=step.step_id,
        )
        waiting = transition_run(record, RunStatus.WAITING_OPERATOR, event, operator_request=request)
        self.store.save_run(waiting, expected_generation=generation)
        return waiting

    def _request_risk_gate(self, record: RunRecord, generation: int, step: PlanStep) -> RunRecord:
        event = self._event(record, "dispatcher", "risk gate requires operator decision", step.step_id)
        request = OperatorRequest(
            request_id=f"request-{uuid.uuid4().hex}",
            question=f"Approve dispatch of risk-gated step {step.step_id}?",
            allowed_answers=["approve", "deny"],
            context_ref=step.step_id,
            resume_to=RunStatus.RUNNING,
            expires_at=None,
            required_role=None,
            kind="risk_gate",
            step_id=step.step_id,
        )
        waiting = transition_run(record, RunStatus.WAITING_OPERATOR, event, operator_request=request)
        self.store.save_run(waiting, expected_generation=generation)
        return waiting

    def _request_escalation(
        self,
        record: RunRecord,
        generation: int,
        step: PlanStep,
        dispatch: DispatchRecord,
        forwarding: str,
    ) -> tuple[RunRecord, int, str]:
        if step.retry.escalation_role_key is None:
            raise SequentialWorkflowError("escalation policy has no configured executor reassignment role")
        event = self._event(record, "dispatcher", "review escalation requires operator reassignment", dispatch.dispatch_id)
        request = OperatorRequest(
            request_id=f"request-{uuid.uuid4().hex}",
            question=(
                f"Step {step.step_id} requires escalation. Reassign executor "
                f"{step.retry.escalation_role_key} for a new attempt or halt?"
            ),
            allowed_answers=["reassign", "halt"],
            context_ref=dispatch.dispatch_id,
            resume_to=RunStatus.RUNNING,
            expires_at=None,
            required_role=None,
            kind="escalation",
            step_id=step.step_id,
            reassignment_role_key=step.retry.escalation_role_key,
        )
        waiting = transition_run(record, RunStatus.WAITING_OPERATOR, event, operator_request=request)
        return waiting, self.store.save_run(waiting, expected_generation=generation), forwarding

    def _ensure_budget_allows_dispatch(
        self,
        record: RunRecord,
        step: PlanStep,
        *,
        role_key: str,
        session_mode: Literal["new", "resume", "fork"],
    ) -> None:
        if not self.config.model.budget.enabled:
            return
        budget = self.config.model.budget
        step_usage = record.usage.by_step.get(step.step_id, UsageAmount())
        if record.usage.run.cost_usd >= budget.max_run_cost_usd:
            raise SequentialWorkflowError("run cost budget is exhausted; a new dispatch is forbidden")
        if step_usage.cost_usd >= budget.max_step_cost_usd:
            raise SequentialWorkflowError(f"step {step.step_id} cost budget is exhausted; a new dispatch is forbidden")
        if session_mode == "resume":
            pool = "executors" if self._role_kind(role_key) == "executor" else "reviewers"
            session = self.store.sessions_for_run(record.run_id).get(pool, {}).get(role_key, {})
            session_id = session.get("session_id")
            if isinstance(session_id, str):
                usage = record.usage.by_session.get(session_id, UsageAmount())
                if usage.tokens_total >= budget.max_context_tokens:
                    raise SequentialWorkflowError(
                        f"session context token limit is exhausted for {session_id}; a resumed dispatch is forbidden"
                    )

    def _record_usage(
        self,
        record: RunRecord,
        generation: int,
        dispatch: DispatchRecord,
        usage: Mapping[str, object] | None,
    ) -> tuple[RunRecord, int]:
        if usage is None:
            if self.config.model.budget.enabled:
                raise SequentialWorkflowError("measured OpenCode usage is required while budget enforcement is enabled")
            return record, generation
        amount = _usage_amount(usage)
        ledger = record.usage
        updated_ledger = UsageLedger(
            run=_add_usage(ledger.run, amount),
            by_step=_updated_usage_bucket(ledger.by_step, dispatch.step_id, amount),
            by_role=_updated_usage_bucket(ledger.by_role, dispatch.role_key, amount),
            by_session=_updated_usage_bucket(
                ledger.by_session,
                dispatch.runtime_session_id or dispatch.logical_session_key,
                amount,
            ),
        )
        updated = record.model_copy(update={"usage": updated_ledger})
        return updated, self.store.save_run(updated, expected_generation=generation)

    def _apply_budget_limit(
        self,
        record: RunRecord,
        generation: int,
        dispatch: DispatchRecord,
        forwarding: str,
    ) -> tuple[RunRecord, int, str]:
        if not self.config.model.budget.enabled:
            return record, generation, forwarding
        budget = self.config.model.budget
        step_usage = record.usage.by_step[dispatch.step_id]
        over_context = record.usage.by_session[dispatch.runtime_session_id or dispatch.logical_session_key]
        detail = None
        if record.usage.run.cost_usd > budget.max_run_cost_usd:
            detail = "run cost budget exceeded"
        elif step_usage.cost_usd > budget.max_step_cost_usd:
            detail = f"step {dispatch.step_id} cost budget exceeded"
        elif over_context.tokens_total > budget.max_context_tokens:
            detail = f"session context token limit exceeded for {dispatch.logical_session_key}"
        if detail is None:
            return record, generation, forwarding
        event = self._event(record, "dispatcher", detail, dispatch.dispatch_id)
        if budget.on_limit == "halt":
            halted = transition_run(record, RunStatus.HALTED, event)
            return halted, self.store.save_run(halted, expected_generation=generation), forwarding
        request = OperatorRequest(
            request_id=f"request-{uuid.uuid4().hex}",
            question=f"{detail}; configured action is {budget.on_limit}. Halt this run?",
            allowed_answers=["halt"],
            context_ref=dispatch.dispatch_id,
            resume_to=RunStatus.HALTED,
            expires_at=None,
            required_role=None,
            kind="budget",
            step_id=dispatch.step_id,
        )
        waiting = transition_run(record, RunStatus.WAITING_OPERATOR, event, operator_request=request)
        return waiting, self.store.save_run(waiting, expected_generation=generation), forwarding

    def _validate_bootstrap_record(self, record: RunRecord) -> None:
        try:
            validate_plan_approval(record.plan, record.plan_approval)
            verify_plan_sources(record.plan, self.config)
        except PlanError as exc:
            raise SequentialWorkflowError(f"cannot render bootstrap: {exc}") from exc

    def _compile_run_policy(self, record: RunRecord, generation: int) -> tuple[RunRecord, int]:
        """Persist exactly one selected-profile policy before activating any run."""
        try:
            compiled = compile_run_policy(self.config, record.plan)
        except PolicyError as exc:
            raise SequentialWorkflowError(f"cannot compile run policy: {exc}") from exc
        if record.policy is not None:
            if record.policy != compiled:
                raise SequentialWorkflowError("stored run policy no longer matches selected configuration")
            return record, generation
        updated = record.model_copy(update={"policy": compiled})
        return updated, self.store.save_run(updated, expected_generation=generation)

    @staticmethod
    def _review_obligation(record: RunRecord, step: PlanStep) -> CompiledReviewObligation:
        if record.policy is None:
            raise SequentialWorkflowError("run policy must be compiled before dispatch")
        try:
            return record.policy.review_obligations[step.step_id]
        except KeyError as exc:
            raise SequentialWorkflowError(f"run policy has no obligation for step {step.step_id}") from exc

    def _validate_step_readiness(
        self,
        record: RunRecord,
        step: PlanStep,
        role_kind: str,
        mode: str,
        role_key: str,
    ) -> None:
        step_record = record.steps[step.step_id]
        expected_state = StepStatus.READY if role_kind == "executor" else StepStatus.REVIEW_REQUIRED
        if step_record.state is not expected_state:
            raise SequentialWorkflowError(
                f"step {step.step_id} is {step_record.state.value}, not {expected_state.value}"
            )
        if role_kind == "executor":
            if (
                step_record.reassignment_role_key is not None
                and role_key != step_record.reassignment_role_key
            ):
                raise SequentialWorkflowError(
                    f"step {step.step_id} requires reassignment to {step_record.reassignment_role_key}"
                )
            if step_record.executor_attempts >= step.retry.max_executor_attempts:
                raise SequentialWorkflowError(f"step {step.step_id} exhausted executor attempts")
        else:
            obligation = self._review_obligation(record, step)
            if mode != "new":
                raise SequentialWorkflowError("reviewer sessions must be new for independent review")
            if role_key not in obligation.reviewer_role_keys:
                raise SequentialWorkflowError(f"role {role_key} is not compiled to review step {step.step_id}")
            if role_key in step_record.accepted_reviewer_role_keys:
                raise SequentialWorkflowError(f"role {role_key} already accepted step {step.step_id}")
            if step_record.reviewer_attempts >= step.retry.max_reviewer_attempts:
                raise SequentialWorkflowError(f"step {step.step_id} exhausted reviewer attempts")
        for dependency_id in step.depends_on:
            dependency = record.steps[dependency_id]
            if dependency.state not in {StepStatus.ACCEPTED, StepStatus.WAIVED}:
                raise SequentialWorkflowError(
                    f"step {step.step_id} dependency {dependency_id} is not accepted"
                )
        for artifact in step.required_inputs:
            if artifact.producer_step_id is None:
                continue
            producer = record.steps[artifact.producer_step_id]
            if producer.state not in {StepStatus.ACCEPTED, StepStatus.WAIVED}:
                raise SequentialWorkflowError(
                    f"step {step.step_id} input producer {artifact.producer_step_id} is not accepted"
                )
        if mode in {"resume", "fork"}:
            sessions = self.store.sessions_for_run(record.run_id)
            pool = "executors" if role_kind == "executor" else "reviewers"
            if not sessions.get(pool, {}).get(role_key, {}).get("session_id"):
                raise SequentialWorkflowError("requested session mode has no dispatcher-owned session")

    def _review_target(self, record: RunRecord, step: PlanStep) -> ReviewTarget:
        candidates = [
            dispatch
            for dispatch in record.dispatches.values()
            if dispatch.step_id == step.step_id and dispatch.role_kind == "executor"
        ]
        if not candidates:
            raise SequentialWorkflowError("review dispatch requires a completed executor dispatch")
        executor_dispatch = max(candidates, key=lambda value: value.attempt)
        if executor_dispatch.state not in {DispatchStatus.COMPLETED, DispatchStatus.FORWARDED, DispatchStatus.ACKNOWLEDGED}:
            raise SequentialWorkflowError("review dispatch requires durable executor completion")
        payload = self.store.load_dispatch_payload(record.run_id, executor_dispatch.dispatch_id)
        if payload.result is None:
            raise SequentialWorkflowError("completed executor dispatch has no durable result payload")
        try:
            from .results import parse_executor_result

            result = parse_executor_result(payload.result)
        except ResultError as exc:
            raise SequentialWorkflowError("durable executor result is invalid") from exc
        return ReviewTarget(
            executor_dispatch_id=executor_dispatch.dispatch_id,
            executor_attempt=executor_dispatch.attempt,
            result_revision=result.repository.result_revision,
            patch_sha256=result.repository.patch_sha256,
            artifact_hashes=[artifact.sha256 for artifact in result.evidence],
        )

    def _inspect_repository(self, repo_id: str, *, require_clean: bool) -> RepositorySnapshot:
        try:
            return self._repository_inspector(self.config, repo_id, require_clean=require_clean)
        except RepositoryValidationError as exc:
            raise SequentialWorkflowError(str(exc)) from exc

    def _validate_executor_evidence(self, step: PlanStep, result: ExecutorResult) -> None:
        expected = {item.artifact_id: item for item in step.evidence_requirements}
        actual = {item.artifact_id: item for item in result.evidence}
        if set(actual) != set(expected):
            raise SequentialWorkflowError("executor result evidence does not exactly match step requirements")
        for artifact_id, requirement in expected.items():
            artifact = actual[artifact_id]
            if artifact.relative_path != requirement.relative_path or artifact.media_type != requirement.media_type:
                raise SequentialWorkflowError(
                    f"evidence artifact {artifact_id} does not match its required path or media type"
                )

    def _retry_or_terminal_step(
        self,
        record: RunRecord,
        step: PlanStep,
        blocked: StepRecord,
        *,
        policy: str,
        correlation_id: str,
    ) -> StepRecord:
        if policy == "retry" and blocked.executor_attempts < step.retry.max_executor_attempts:
            event = self._event(
                record,
                "dispatcher",
                "step retry is ready",
                correlation_id,
                sequence=blocked.last_event.sequence + 1,
            )
            return transition_step(blocked, StepStatus.READY, event)
        if policy == "escalate":
            return blocked
        event = self._event(
            record,
            "dispatcher",
            "step retry policy halted the step",
            correlation_id,
            sequence=blocked.last_event.sequence + 1,
        )
        return transition_step(blocked, StepStatus.FAILED, event)

    def _role_kind(self, role_key: str) -> Literal["executor", "reviewer"]:
        role_kind = self.config.role_kind(role_key)
        if role_kind not in {"executor", "reviewer"}:
            raise SequentialWorkflowError("supervisor cannot be a dispatch target")
        return cast(Literal["executor", "reviewer"], role_kind)

    def _owned_session_id(
        self,
        run_id: str,
        role_kind: Literal["executor", "reviewer"],
        role_key: str,
        mode: Literal["new", "resume", "fork"],
    ) -> str | None:
        if mode == "new":
            return None
        pool = "executors" if role_kind == "executor" else "reviewers"
        session_id = self.store.sessions_for_run(run_id).get(pool, {}).get(role_key, {}).get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise SequentialWorkflowError("requested session mode has no dispatcher-owned session")
        return session_id

    @staticmethod
    def _event(
        record: RunRecord,
        actor: str,
        reason: str,
        correlation_id: str,
        *,
        sequence: int | None = None,
    ) -> TransitionEvent:
        return TransitionEvent(
            event_id=f"event-{uuid.uuid4().hex}",
            sequence=record.sequence + 1 if sequence is None else sequence,
            actor=actor,  # type: ignore[arg-type]
            reason=reason,
            correlation_id=correlation_id,
            occurred_at=datetime.now(UTC),
        )


def _plan_step(record: RunRecord, step_id: str) -> PlanStep:
    for step in record.plan.steps:
        if step.step_id == step_id:
            return step
    raise SequentialWorkflowError(f"supervisor requested unknown plan step: {step_id}")


def _lease_keys(step: PlanStep) -> tuple[str, ...]:
    return resource_keys(step.repo_id, tuple(lock.resource_id for lock in step.resource_locks))


def _dispatch_lease_owner_id(owner_id: str, dispatch_id: str) -> str:
    return f"{owner_id}.dispatch.{dispatch_id}"


def _bootstrap_template() -> str:
    return resources.files("dispatcher").joinpath("templates/bootstrap_supervisor.md").read_text(encoding="utf-8")


def _repositories_markdown(config: Config) -> str:
    return "\n".join(
        f"- `{repo_id}`: root `{repository.root}`, branch `{repository.default_branch}`, "
        f"evidence `{', '.join(repository.evidence_roots)}`"
        for repo_id, repository in config.model.repositories.items()
    )


def _specifications_markdown(config: Config) -> str:
    root = Path(config.model.sources.specifications_dir)
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise SequentialWorkflowError("configured specifications directory contains no files")
    return "\n".join(
        f"- `{path.relative_to(root)}` sha256 `{hashlib.sha256(path.read_bytes()).hexdigest()}`"
        for path in files
    )


def _plan_sources_markdown(record: RunRecord) -> str:
    sources = [source for source in record.plan.sources if source.root == "plans"]
    if not sources:
        raise SequentialWorkflowError("approved normalized plan has no plan sources")
    return "\n".join(f"- `{source.relative_path}` sha256 `{source.sha256}`" for source in sources)


def _roles_markdown(config: Config) -> str:
    roles = "\n".join(
        f"- `{role_key}` ({config.role_kind(role_key)}): `{role.model}` / `{role.variant}`"
        for role_key, role in config.model.all_roles().items()
    )
    plans_root = Path(config.model.sources.plans_dir)
    source_files = "\n".join(
        f"- role source `{relative_path}` sha256 "
        f"`{hashlib.sha256((plans_root / relative_path).read_bytes()).hexdigest()}`"
        for relative_path in config.model.sources.roles_files
    )
    return f"{roles}\n{source_files}"


def _baseline_markdown(record: RunRecord) -> str:
    return "\n".join(
        f"- `{step_id}`: `{step.state.value}`" for step_id, step in sorted(record.steps.items())
    )


def _dispatch_example(config: Config, record: RunRecord) -> str:
    step = record.plan.steps[0]
    role = next(iter(config.model.roles.executors))
    return DispatchCommand(
        protocol_version=1,
        action="dispatch",
        step_id=step.step_id,
        target_role=role,
        session_mode="new",
        prompt="Perform only the authorized work and return one schema-v1 executor result JSON object.",
        rationale="Example only; dispatcher validates every request.",
    ).model_dump_json(
        exclude_none=True,
    )


def _completion_example() -> str:
    return RequestCompletionCommand(
        protocol_version=1,
        action="request_completion",
        rationale="All obligations are met.",
    ).model_dump_json(
        exclude_none=True,
    )


def _worker_prompt(
    *,
    dispatch_id: str,
    attempt: int,
    role_kind: Literal["executor", "reviewer"],
    step: PlanStep,
    task: str,
    repository: DispatchRepositoryCoordinate,
    review_target: ReviewTarget | None,
) -> str:
    """Render the exact machine context a worker needs to return a typed result."""
    return json.dumps(
        {
            "context_version": 1,
            "result_kind": role_kind,
            "dispatch_id": dispatch_id,
            "attempt": attempt,
            "step_id": step.step_id,
            "repo_id": step.repo_id,
            "base_revision": repository.base_revision,
            "base_branch": repository.base_branch,
            "working_branch": repository.working_branch,
            "worktree_id": repository.worktree_id,
            "remote_name": repository.remote_name,
            "remote_url": repository.remote_url,
            "task": task,
            "authorized_actions": list(step.authorization.authorized_actions),
            "acceptance_criteria": [
                criterion.model_dump(mode="json") for criterion in step.acceptance_criteria
            ],
            "evidence_requirements": [
                requirement.model_dump(mode="json") for requirement in step.evidence_requirements
            ],
            "review_target": (
                review_target.model_dump(mode="json") if review_target is not None else None
            ),
            "response_contract": (
                "Return exactly one schema-v1 executor result JSON object."
                if role_kind == "executor"
                else "Return exactly one schema-v1 reviewer result JSON object."
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_executor_result(result: ExecutorResult, dispatch: DispatchRecord) -> None:
    expectation = ResultExpectation(
        dispatch_id=dispatch.dispatch_id,
        attempt=dispatch.attempt,
        step_id=dispatch.step_id,
        repo_id=dispatch.intent.repository.repo_id,
        expected_review_target=None,
    )
    try:
        validate_executor_result_context(result, expectation)
    except ResultError as exc:
        raise SequentialWorkflowError(str(exc)) from exc
    if result.repository.base_revision != dispatch.intent.repository.base_revision:
        raise SequentialWorkflowError("executor result base revision does not match prepared dispatch")


def _executor_forwarding(result: ExecutorResult, usage: UsageAmount) -> str:
    return json.dumps(
        {
            "kind": "executor_result",
            "dispatch_id": result.dispatch_id,
            "outcome": result.outcome,
            "summary": result.summary,
            "evidence": [item.model_dump(mode="json") for item in result.evidence],
            "usage": usage.model_dump(mode="json"),
        },
        sort_keys=True,
    )


def _reviewer_forwarding(result: ReviewerResult, usage: UsageAmount) -> str:
    return json.dumps(
        {
            "kind": "reviewer_result",
            "dispatch_id": result.dispatch_id,
            "verdict": result.verdict,
            "summary": result.summary,
            "review_target": result.review_target.model_dump(mode="json"),
            "usage": usage.model_dump(mode="json"),
        },
        sort_keys=True,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Mapping[str, Any]) -> str:
    return _sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _usage_amount(usage: Mapping[str, object]) -> UsageAmount:
    cost = usage.get("cost_usd")
    if not isinstance(cost, (int, float)) or cost < 0:
        raise SequentialWorkflowError("measured usage cost_usd must be a non-negative number")
    values: dict[str, int] = {}
    for key in ("tokens_total", "tokens_input", "tokens_output", "tokens_reasoning"):
        value = usage.get(key)
        if not isinstance(value, int) or value < 0:
            raise SequentialWorkflowError(f"measured usage {key} must be a non-negative integer")
        values[key] = value
    return UsageAmount(cost_usd=float(cost), **values)


def _add_usage(left: UsageAmount, right: UsageAmount) -> UsageAmount:
    return UsageAmount(
        cost_usd=left.cost_usd + right.cost_usd,
        tokens_total=left.tokens_total + right.tokens_total,
        tokens_input=left.tokens_input + right.tokens_input,
        tokens_output=left.tokens_output + right.tokens_output,
        tokens_reasoning=left.tokens_reasoning + right.tokens_reasoning,
    )


def _updated_usage_bucket(
    current: Mapping[str, UsageAmount],
    key: str,
    amount: UsageAmount,
) -> dict[str, UsageAmount]:
    updated = dict(current)
    updated[key] = _add_usage(updated.get(key, UsageAmount()), amount)
    return updated
