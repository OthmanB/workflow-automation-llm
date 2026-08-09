"""Plan-driven sequential workflow facade backed by the SQLite authority."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, cast

from .config import Config
from .permissions import compile_effective_policy, generate_opencode_config, should_auto_approve
from .plan import PlanError, PlanStep, validate_plan_approval, verify_plan_sources
from .protocol import (
    AskOperatorCommand,
    DispatchCommand,
    HaltCommand,
    RequestCompletionCommand,
    parse_supervisor_command,
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
from .state_store import StateStore
from .workflow import (
    DispatchIntent,
    DispatchRecord,
    DispatchStatus,
    OperatorRequest,
    RunRecord,
    RunStatus,
    StepRecord,
    StepStatus,
    TransitionEvent,
    completion_obligations,
    transition_run,
    transition_step,
)
from .workflow import RepositoryCoordinate as DispatchRepositoryCoordinate

RepositoryRevisionResolver = Callable[[Path], str]


class SequentialWorkflowError(ValueError):
    """A supervisor request or worker result violates dispatcher-owned invariants."""


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
    review_target: ReviewTarget | None
    session_mode: Literal["new", "resume", "fork"]
    session_id: str | None


@dataclass(frozen=True)
class CompletionDecision:
    """The dispatcher-owned disposition of a supervisor completion request."""

    accepted: bool
    obligations: tuple[str, ...]
    report_path: Path | None


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
        revision_resolver: RepositoryRevisionResolver | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.owner_id = owner_id
        self._revision_resolver = revision_resolver or _git_head_revision

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
    ) -> PreparedDispatch | CompletionDecision | RunRecord:
        """Parse and apply one strict supervisor command without launching a process."""
        command = parse_supervisor_command(supervisor_text)
        record, generation = self.store.load_run(run_id)
        if generation != expected_generation:
            raise SequentialWorkflowError("run generation changed before supervisor command")
        if isinstance(command, DispatchCommand):
            return self.prepare_dispatch(record, generation, command)
        if isinstance(command, RequestCompletionCommand):
            return self.evaluate_completion(record, generation)
        if isinstance(command, AskOperatorCommand):
            return self._wait_for_operator(record, generation, command)
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
    ) -> PreparedDispatch:
        """Commit a fully validated PREPARED dispatch before any worker launch."""
        if record.state is not RunStatus.RUNNING:
            raise SequentialWorkflowError("only RUNNING runs may prepare a dispatch")
        if any(
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
        step = _plan_step(record, command.step_id)
        role_kind = self._role_kind(command.target_role)
        self._validate_step_readiness(record, step, role_kind, command.session_mode, command.target_role)
        workdir = self.config.repository_root(step.repo_id)
        policy = generate_opencode_config(
            compile_effective_policy(
                self.config,
                repo_id=step.repo_id,
                role_key=command.target_role,
                dispatch_authorized_actions=step.authorization.authorized_actions,
            )
        )
        policy_rules = policy["permission"]
        base_revision = self._revision_resolver(workdir)
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
            base_revision=base_revision,
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
                repository=DispatchRepositoryCoordinate(repo_id=step.repo_id, base_revision=base_revision),
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
        session_id = self._owned_session_id(record.run_id, role_kind, command.target_role, command.session_mode)
        self.store.acquire_resource_leases(
            run_id=record.run_id,
            owner_id=self.owner_id,
            resource_keys=lease_keys,
        )
        try:
            next_generation = self.store.prepare_dispatch(
                updated,
                expected_generation=generation,
                dispatch=dispatch,
                prompt=worker_prompt,
                policy=policy,
                session_metadata={
                    "session_mode": command.session_mode,
                    "parent_session_id": session_id,
                    "review_target": (
                        review_target.model_dump(mode="json") if review_target is not None else None
                    ),
                },
            )
        except Exception:
            self.store.release_leases(owner_id=self.owner_id, resource_keys=lease_keys)
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
            review_target=review_target,
            session_mode=command.session_mode,
            session_id=session_id,
        )

    def mark_running(
        self,
        prepared: PreparedDispatch,
        *,
        process_id: int,
    ) -> PreparedDispatch:
        """Durably record a worker launch before accepting any worker result."""
        record, generation = self.store.load_run(prepared.run_id)
        if generation != prepared.generation:
            raise SequentialWorkflowError("prepared dispatch generation is stale")
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
            review_target=prepared.review_target,
            session_mode=prepared.session_mode,
            session_id=prepared.session_id,
        )

    def record_session_id(
        self,
        running: PreparedDispatch,
        *,
        runtime_session_id: str,
    ) -> PreparedDispatch:
        """Bind the first validated OpenCode session ID to the running attempt."""
        record, generation = self.store.load_run(running.run_id)
        if generation != running.generation:
            raise SequentialWorkflowError("running dispatch generation is stale")
        dispatch = record.dispatches[running.dispatch.dispatch_id]
        if dispatch.state is not DispatchStatus.RUNNING:
            raise SequentialWorkflowError("session ID arrived for a dispatch that is not RUNNING")
        event = self._event(record, "dispatcher", "OpenCode session identified", dispatch.dispatch_id)
        pool = "executors" if dispatch.role_kind == "executor" else "reviewers"
        updated, next_generation = self.store.bind_dispatch_session(
            record,
            expected_generation=generation,
            dispatch_id=dispatch.dispatch_id,
            runtime_session_id=runtime_session_id,
            event=event,
            pool=pool,
            role_key=dispatch.role_key,
            session_entry={
                "session_id": runtime_session_id,
                "logical_session_key": dispatch.logical_session_key,
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
            review_target=running.review_target,
            session_mode=running.session_mode,
            session_id=running.session_id,
        )

    def apply_executor_result(
        self,
        prepared: PreparedDispatch,
        result: ExecutorResult,
    ) -> tuple[RunRecord, int, str]:
        """Apply a typed executor result and persist the next supervisor message."""
        if prepared.dispatch.role_kind != "executor":
            raise SequentialWorkflowError("executor result does not match a reviewer dispatch")
        record, generation = self.store.load_run(prepared.run_id)
        if generation != prepared.generation:
            raise SequentialWorkflowError("running dispatch generation is stale")
        dispatch = record.dispatches[prepared.dispatch.dispatch_id]
        _validate_executor_result(result, dispatch)
        step = _plan_step(record, dispatch.step_id)
        self._validate_executor_evidence(step, result)
        completion_event = self._event(record, "executor", "typed executor result received", dispatch.dispatch_id)
        record, generation = self.store.commit_dispatch_transition(
            record,
            expected_generation=generation,
            dispatch_id=dispatch.dispatch_id,
            target=DispatchStatus.COMPLETED,
            event=completion_event,
            result_digest=_sha256_json(result.model_dump(mode="json")),
            result=result.model_dump(mode="json"),
        )
        step_record = record.steps[step.step_id]
        result_event = self._event(record, "dispatcher", f"executor outcome {result.outcome}", dispatch.dispatch_id)
        if isinstance(result, ExecutorCompletedResult):
            executed = transition_step(step_record, StepStatus.EXECUTED, result_event)
            target = StepStatus.REVIEW_REQUIRED if step.review.required else StepStatus.ACCEPTED
            final_event = self._event(
                record,
                "dispatcher",
                f"step moved to {target.value}",
                dispatch.dispatch_id,
                sequence=result_event.sequence + 1,
            )
            updated_step = transition_step(executed, target, final_event)
            if target is StepStatus.ACCEPTED:
                updated_step = updated_step.model_copy(
                    update={"accepted_artifact_ids": [item.artifact_id for item in result.evidence]}
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
            else:
                updated_step = transition_step(step_record, StepStatus.FAILED, result_event)
        record, generation = self._replace_step(record, generation, updated_step)
        forwarding = _executor_forwarding(result)
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
        self.store.release_leases(owner_id=self.owner_id, resource_keys=prepared.lease_keys)
        return record, generation, forwarding

    def apply_reviewer_result(
        self,
        prepared: PreparedDispatch,
        result: ReviewerResult,
    ) -> tuple[RunRecord, int, str]:
        """Apply an immutable reviewer verdict to the exact reviewed work product."""
        if prepared.dispatch.role_kind != "reviewer" or prepared.review_target is None:
            raise SequentialWorkflowError("reviewer result does not match a prepared review dispatch")
        record, generation = self.store.load_run(prepared.run_id)
        if generation != prepared.generation:
            raise SequentialWorkflowError("running review generation is stale")
        dispatch = record.dispatches[prepared.dispatch.dispatch_id]
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
        if result.review_target.result_revision is not None:
            current_revision = self._revision_resolver(self.config.repository_root(result.repo_id))
            if current_revision != result.review_target.result_revision:
                raise SequentialWorkflowError("review target revision no longer matches the active repository")
        completion_event = self._event(record, "reviewer", "typed reviewer result received", dispatch.dispatch_id)
        record, generation = self.store.commit_dispatch_transition(
            record,
            expected_generation=generation,
            dispatch_id=dispatch.dispatch_id,
            target=DispatchStatus.COMPLETED,
            event=completion_event,
            result_digest=_sha256_json(result.model_dump(mode="json")),
            result=result.model_dump(mode="json"),
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
        if isinstance(result, ReviewerAcceptedResult):
            updated_step = transition_step(step_record, StepStatus.ACCEPTED, verdict_event)
            updated_step = updated_step.model_copy(
                update={
                    "review_acceptances": step_record.review_acceptances + 1,
                    "accepted_artifact_ids": [requirement.artifact_id for requirement in step.evidence_requirements],
                }
            )
        elif isinstance(result, ReviewerChangesRequestedResult):
            changed = transition_step(step_record, StepStatus.CHANGES_REQUESTED, verdict_event)
            if step.retry.on_changes_requested == "retry" and (
                step_record.executor_attempts < step.retry.max_executor_attempts
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
            updated_step = transition_step(step_record, StepStatus.BLOCKED, verdict_event)
        record, generation = self._replace_step(record, generation, updated_step)
        forwarding = _reviewer_forwarding(result)
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
        self.store.release_leases(owner_id=self.owner_id, resource_keys=prepared.lease_keys)
        return record, generation, forwarding

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

    def fail_dispatch(
        self,
        prepared: PreparedDispatch,
        *,
        reason: str,
    ) -> tuple[RunRecord, int]:
        """Record a failed adapter/result boundary without advancing the plan step."""
        record, generation = self.store.load_run(prepared.run_id)
        if generation != prepared.generation:
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
        if dispatch.state is DispatchStatus.RUNNING:
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
            )
            record = transition_run(
                record,
                RunStatus.WAITING_OPERATOR,
                request_event,
                operator_request=request,
            )
            generation = self.store.save_run(record, expected_generation=generation)
        self.store.release_leases(owner_id=self.owner_id, resource_keys=prepared.lease_keys)
        return record, generation

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
        event = self._event(record, "supervisor", "operator input requested", "operator-request")
        request = OperatorRequest(
            request_id=f"request-{uuid.uuid4().hex}",
            question=command.question,
            allowed_answers=["answer", "halt"],
            context_ref=command.step_id or "run",
            resume_to=RunStatus.RUNNING,
            expires_at=None,
            required_role=None,
        )
        waiting = transition_run(record, RunStatus.WAITING_OPERATOR, event, operator_request=request)
        self.store.save_run(waiting, expected_generation=generation)
        return waiting

    def _validate_bootstrap_record(self, record: RunRecord) -> None:
        try:
            validate_plan_approval(record.plan, record.plan_approval)
            verify_plan_sources(record.plan, self.config)
        except PlanError as exc:
            raise SequentialWorkflowError(f"cannot render bootstrap: {exc}") from exc

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
            if step_record.executor_attempts >= step.retry.max_executor_attempts:
                raise SequentialWorkflowError(f"step {step.step_id} exhausted executor attempts")
        else:
            if mode != "new":
                raise SequentialWorkflowError("reviewer sessions must be new for independent review")
            if role_key not in step.review.reviewer_role_keys:
                raise SequentialWorkflowError(f"role {role_key} is not configured to review step {step.step_id}")
            if step_record.reviewer_attempts >= step.retry.max_reviewer_attempts:
                raise SequentialWorkflowError(f"step {step.step_id} exhausted reviewer attempts")
        for dependency_id in step.depends_on:
            dependency = record.steps[dependency_id]
            if dependency.state not in {StepStatus.ACCEPTED, StepStatus.WAIVED}:
                raise SequentialWorkflowError(
                    f"step {step.step_id} dependency {dependency_id} is not accepted"
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
    return tuple(sorted({f"repository:{step.repo_id}", *(f"resource:{lock.resource_id}" for lock in step.resource_locks)}))


def _git_head_revision(workdir: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workdir,
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise SequentialWorkflowError(f"could not resolve repository revision at {workdir}: {exc}") from exc
    revision = result.stdout.strip()
    if not revision:
        raise SequentialWorkflowError(f"repository revision is empty at {workdir}")
    return revision


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
    base_revision: str,
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
            "base_revision": base_revision,
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


def _executor_forwarding(result: ExecutorResult) -> str:
    return json.dumps(
        {
            "kind": "executor_result",
            "dispatch_id": result.dispatch_id,
            "outcome": result.outcome,
            "summary": result.summary,
            "evidence": [item.model_dump(mode="json") for item in result.evidence],
        },
        sort_keys=True,
    )


def _reviewer_forwarding(result: ReviewerResult) -> str:
    return json.dumps(
        {
            "kind": "reviewer_result",
            "dispatch_id": result.dispatch_id,
            "verdict": result.verdict,
            "summary": result.summary,
            "review_target": result.review_target.model_dump(mode="json"),
        },
        sort_keys=True,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Mapping[str, Any]) -> str:
    return _sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))
