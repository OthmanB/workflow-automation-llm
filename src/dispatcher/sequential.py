"""Plan-driven sequential workflow facade backed by the SQLite authority."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import socket
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import wraps
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, TypeVar, cast

from .config import Config, ConfigError
from .git_commit import (
    StructuredGitError,
    StructuredGitIntent,
    adopt_structured_git_commit,
    execute_structured_git_commit,
    prepare_structured_git_intent,
)
from .mcp import compile_role_mcp_servers, resolve_role_mcp_tools
from .permissions import (
    READ_ONLY_DIAGNOSTIC_COMMANDS,
    READ_ONLY_NATIVE_TOOLS,
    compile_effective_policy,
    generate_opencode_config,
    role_scoped_authorized_actions,
    should_auto_approve,
)
from .plan import PlanError, PlanSource, PlanStep, validate_plan_approval, verify_plan_sources
from .policy import PolicyError, compile_run_policy
from .protocol import (
    AskOperatorCommand,
    BatchDispatchCommand,
    DispatchCommand,
    HaltCommand,
    ProtocolError,
    RequestCompletionCommand,
    RequestReviewWaiverCommand,
    parse_supervisor_command,
)
from .repository import (
    RepositorySnapshot,
    RepositoryValidationError,
    authoritative_evidence,
    inspect_repository,
    inspect_workspace,
    validate_executor_snapshot,
    validate_pending_executor_changes,
    validate_review_snapshot,
    working_patch_sha256,
)
from .results import (
    EXECUTOR_PROPOSAL_CONTRACT,
    EXECUTOR_PROPOSAL_OUTCOME_OPTIONS,
    REVIEWER_RESPONSE_CONTRACT,
    REVIEWER_VERDICT_OPTIONS,
    ExecutorBlockedResult,
    ExecutorCompletedProposal,
    ExecutorCompletedResult,
    ExecutorFailedResult,
    ExecutorProposal,
    ExecutorResult,
    ResultError,
    ResultExpectation,
    ReviewerAcceptedResult,
    ReviewerChangesRequestedResult,
    ReviewerResult,
    ReviewTarget,
    VerificationResult,
    parse_executor_proposal,
    parse_executor_result,
    parse_reviewer_result,
    validate_executor_proposal_context,
    validate_executor_result_context,
    validate_reviewer_result_context,
)
from .results import RepositoryCoordinate as ResultRepositoryCoordinate
from .scheduler import SchedulingError, resource_keys, validate_batch, validate_workspace_batch
from .schema_export import schema_documents
from .security import redact_text
from .state_store import DispatchPayload, StateStore, StateStoreError
from .verification import AuthoritativeVerification
from .workflow import (
    ACTIVE_DISPATCH_STATES,
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
    WorkspaceChild,
    WorkspaceGroup,
    WorkspaceGroupStatus,
    completion_obligations,
    transition_batch,
    transition_dispatch,
    transition_run,
    transition_step,
)
from .workflow import RepositoryCoordinate as DispatchRepositoryCoordinate
from .workspaces import WorkspaceCoordinator, WorkspaceError


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


class SupervisorCommandRejectedError(SequentialWorkflowError):
    """A supervisor command was rejected before it changed durable workflow state."""


class WorkerResultValidationError(SequentialWorkflowError):
    """A structurally valid worker result violates its active plan context."""


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
        self.workspace_coordinator = WorkspaceCoordinator(config, store)

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
            "observation_tools": _observation_tools_markdown(self.config),
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

    @_serialized_transition
    def finish_opencode_invocation(
        self,
        *,
        invocation_id: str,
        runtime_session_id: str | None,
        usage: Mapping[str, object] | None,
        failure_category: str | None = None,
    ) -> tuple[RunRecord, int]:
        """Account a worker invocation without racing another workflow transition."""
        return self.store.finish_opencode_invocation(
            invocation_id=invocation_id,
            runtime_session_id=runtime_session_id,
            usage=usage,
            failure_category=failure_category,
        )

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
        try:
            command = parse_supervisor_command(supervisor_text)
        except ProtocolError as exc:
            raise SupervisorCommandRejectedError(str(exc)) from exc
        record, generation = self.store.load_run(run_id)
        if generation != expected_generation:
            raise SequentialWorkflowError("run generation changed before supervisor command")
        if isinstance(command, DispatchCommand):
            return self.prepare_dispatch(record, generation, command)
        if isinstance(command, BatchDispatchCommand):
            if self._is_workspace_batch_candidate(record, command):
                return self.prepare_workspace_batch(record, generation, command)
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
        raise SupervisorCommandRejectedError(
            f"unsupported supervisor command: {type(command).__name__}"
        )

    def prepare_dispatch(
        self,
        record: RunRecord,
        generation: int,
        command: DispatchCommand,
        *,
        retry_repository_before: RepositorySnapshot | None = None,
        retry_expected_snapshot: RepositorySnapshot | None = None,
        retry_require_changes: bool | None = None,
        verification_feedback: tuple[AuthoritativeVerification, ...] = (),
    ) -> PreparedDispatch | RunRecord:
        """Commit a fully validated PREPARED dispatch before any worker launch."""
        # Source drift is a fail-closed prerequisite, never a supervisor correction.
        self._verify_dispatch_sources(record)
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
            raise SupervisorCommandRejectedError(
                f"supervisor repository assertion {command.repo_id!r} does not match step repository {step.repo_id!r}"
            )
        role_kind = self._role_kind(command.target_role)
        authorized_actions = role_scoped_authorized_actions(
            step.authorization.authorized_actions,
            role_kind,
        )
        if not record.steps[step.step_id].operator_gate_resolved:
            return self._request_risk_gate(record, generation, step)
        self._ensure_budget_allows_dispatch(
            record,
            step,
            role_key=command.target_role,
            session_mode=command.session_mode,
        )
        self._validate_step_readiness(record, step, role_kind, command.session_mode, command.target_role)
        workspace_group = self._workspace_group_for_step(record, step.step_id)
        workspace_child = _workspace_child(workspace_group, step.step_id) if workspace_group is not None else None
        workdir = Path(workspace_child.worktree_path) if workspace_child is not None else self.config.repository_root(step.repo_id)
        if retry_repository_before is not None or retry_expected_snapshot is not None:
            if (
                retry_repository_before is None
                or retry_expected_snapshot is None
                or workspace_child is not None
                or role_kind != "executor"
                or command.session_mode != "resume"
                or not verification_feedback
            ):
                raise SequentialWorkflowError("verification retry context is incomplete or unsupported")
            observed = self._inspect_repository(step.repo_id, require_clean=False)
            if observed != retry_expected_snapshot:
                raise SequentialWorkflowError(
                    "repository changed after failed verification; operator reconciliation is required"
                )
            validate_pending_executor_changes(
                self.config,
                coordinate=retry_repository_before.dispatch_coordinate(),
                before=retry_repository_before,
                after=observed,
                root=workdir,
                writable_paths=step.authorization.writable_paths,
                require_changes=(
                    self.config.repository(step.repo_id).commit_policy == "required"
                    if retry_require_changes is None
                    else retry_require_changes
                ),
            )
            repository_before = retry_repository_before
        else:
            repository_before = (
                self._inspect_workspace(step.repo_id, workspace_child, require_clean=True)
                if workspace_child is not None
                else self._inspect_repository(step.repo_id, require_clean=True)
            )
        policy = generate_opencode_config(
            compile_effective_policy(
                self.config,
                repo_id=step.repo_id,
                role_key=command.target_role,
                dispatch_authorized_actions=authorized_actions,
            ),
            mcp_servers=compile_role_mcp_servers(self.config, command.target_role),
        )
        evidence_diagnostic_commands: tuple[str, ...] = ()
        policy_rules = policy["permission"]
        step_record = record.steps[step.step_id]
        attempt = step_record.executor_attempts + 1 if role_kind == "executor" else step_record.reviewer_attempts + 1
        dispatch_id = f"dispatch-{uuid.uuid4().hex}"
        event = self._event(record, "dispatcher", f"prepared {role_kind} dispatch", dispatch_id)
        review_target = self._review_target(record, step) if role_kind == "reviewer" else None
        review_authoritative_verification = (
            self.store.load_dispatch_payload(
                record.run_id,
                review_target.executor_dispatch_id,
            ).authoritative_verification
            if review_target is not None
            else None
        )
        worker_prompt = _worker_prompt(
            dispatch_id=dispatch_id,
            attempt=attempt,
            role_kind=role_kind,
            step=step,
            task=command.prompt,
            authoritative_sources=record.plan.sources,
            source_roots=_source_roots(self.config),
            repository=repository_before.dispatch_coordinate(
                base_branch=workspace_group.base_branch if workspace_group is not None else None
            ),
            evidence_roots=self.config.repository(step.repo_id).evidence_roots,
            review_target=review_target,
            review_authoritative_verification=review_authoritative_verification,
            evidence_diagnostic_commands=evidence_diagnostic_commands,
            authorized_actions=authorized_actions,
            mcp_tools=resolve_role_mcp_tools(self.config, command.target_role),
            verification_feedback=verification_feedback,
        )
        dispatch = DispatchRecord(
            dispatch_id=dispatch_id,
            workspace_group_id=workspace_group.workspace_group_id if workspace_group is not None else None,
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
                repository=repository_before.dispatch_coordinate(
                    base_branch=workspace_group.base_branch if workspace_group is not None else None
                ),
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
        lease_keys = _lease_keys(step, include_repository=workspace_group is None)
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
                    "verification_feedback": [
                        item.model_dump(mode="json") for item in verification_feedback
                    ],
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

    @_serialized_transition
    def record_executor_verification_failure(
        self,
        prepared: PreparedDispatch,
        proposal: ExecutorCompletedProposal,
        *,
        authoritative_verification: tuple[AuthoritativeVerification, ...],
        usage: Mapping[str, object] | None,
        verified_snapshot: RepositorySnapshot,
    ) -> tuple[RunRecord, int]:
        """Persist a safe failed check as bounded executor rework feedback."""
        record, generation = self.store.load_run(prepared.run_id)
        if generation != prepared.generation and self.config.execution.scheduling == "sequential":
            raise SequentialWorkflowError("running dispatch generation is stale")
        dispatch = record.dispatches[prepared.dispatch.dispatch_id]
        step = _plan_step(record, dispatch.step_id)
        _validate_executor_proposal(step, proposal, dispatch)
        expected_ids = [criterion.criterion_id for criterion in step.acceptance_criteria]
        if [item.check_id for item in authoritative_verification] != expected_ids:
            raise WorkerResultValidationError(
                "failed authoritative verification does not exactly cover plan criteria"
            )
        if not any(item.status != "passed" for item in authoritative_verification):
            raise WorkerResultValidationError(
                "verification feedback requires at least one failed dispatcher check"
            )
        observed = self._inspect_dispatch_repository(record, dispatch, require_clean=False)
        if observed != verified_snapshot:
            raise WorkerResultValidationError(
                "repository changed between proposal inspection and failed verification persistence"
            )
        validate_pending_executor_changes(
            self.config,
            coordinate=dispatch.intent.repository,
            before=prepared.repository_before,
            after=observed,
            root=prepared.workdir,
            writable_paths=step.authorization.writable_paths,
            require_changes=self.config.repository(step.repo_id).commit_policy == "required",
        )
        usage_error: SequentialWorkflowError | None = None
        try:
            record, generation = self._record_usage(
                record,
                generation,
                dispatch,
                usage,
                persist=False,
            )
        except SequentialWorkflowError as exc:
            usage_error = exc
        failure_detail = _verification_failure_detail(authoritative_verification)
        dispatch_event = self._event(
            record,
            "dispatcher",
            "dispatcher-owned verification requested executor rework",
            dispatch.dispatch_id,
        )
        failed_dispatch = transition_dispatch(
            dispatch,
            DispatchStatus.FAILED,
            dispatch_event,
            failure_category="authoritative_verification",
            failure_detail=failure_detail,
        )
        dispatches = dict(record.dispatches)
        dispatches[dispatch.dispatch_id] = failed_dispatch
        record = record.model_copy(
            update={
                "dispatches": dispatches,
                "sequence": dispatch_event.sequence,
                "updated_at": dispatch_event.occurred_at,
            }
        )
        step_record = record.steps[step.step_id]
        blocked_event = self._event(
            record,
            "dispatcher",
            "authoritative verification failed",
            dispatch.dispatch_id,
        )
        blocked = transition_step(step_record, StepStatus.BLOCKED, blocked_event).model_copy(
            update={"rework_rounds": step_record.rework_rounds + 1}
        )
        may_retry = (
            usage_error is None
            and _supports_automatic_verification_rework(dispatch)
            and step.retry.on_changes_requested == "retry"
            and blocked.rework_rounds < self.config.execution.max_rounds_per_step
            and blocked.executor_attempts < step.retry.max_executor_attempts
        )
        if usage_error is not None:
            updated_step = blocked
        elif may_retry:
            ready_event = self._event(
                record,
                "dispatcher",
                "verification rework is ready",
                dispatch.dispatch_id,
                sequence=blocked_event.sequence + 1,
            )
            updated_step = transition_step(blocked, StepStatus.READY, ready_event)
        elif (
            _supports_automatic_verification_rework(dispatch)
            and step.retry.on_changes_requested == "escalate"
        ):
            updated_step = blocked
        elif _supports_automatic_verification_rework(dispatch):
            failed_event = self._event(
                record,
                "dispatcher",
                "verification rework policy halted the step",
                dispatch.dispatch_id,
                sequence=blocked_event.sequence + 1,
            )
            updated_step = transition_step(blocked, StepStatus.FAILED, failed_event)
        else:
            updated_step = blocked
        record = self._step_replacement_record(record, updated_step)
        if usage_error is not None and _supports_automatic_verification_rework(dispatch):
            record = self._verification_usage_waiting_record(record, dispatch, usage_error)
        elif (
            _supports_automatic_verification_rework(dispatch)
            and step.retry.on_changes_requested == "escalate"
        ):
            record = self._escalation_waiting_record(record, step, dispatch)
        elif dispatch.batch_id is None and not _supports_automatic_verification_rework(dispatch):
            record = self._unsupported_verification_rework_waiting_record(record, dispatch)
        payload = self.store.load_dispatch_payload(record.run_id, dispatch.dispatch_id)
        metadata = dict(payload.session_metadata or {})
        metadata["verification_feedback"] = [
            item.model_dump(mode="json") for item in authoritative_verification
        ]
        generation = self.store.save_run(
            record,
            expected_generation=generation,
            dispatch_payloads={
                dispatch.dispatch_id: DispatchPayload(
                    prompt=payload.prompt,
                    policy=payload.policy,
                    result=proposal.model_dump(mode="json"),
                    authoritative_verification=tuple(
                        item.model_dump(mode="json") for item in authoritative_verification
                    ),
                    forwarding_payload=payload.forwarding_payload,
                    process_id=payload.process_id,
                    session_metadata=metadata,
                    repository_before=payload.repository_before,
                    repository_after=observed.model_dump(mode="json"),
                )
            },
        )
        self.store.release_leases(
            owner_id=prepared.lease_owner_id,
            resource_keys=prepared.lease_keys,
        )
        return record, generation

    def prepare_workspace_batch(
        self,
        record: RunRecord,
        generation: int,
        command: BatchDispatchCommand,
    ) -> PreparedBatch:
        """Prepare one same-repository executor barrier in durable child worktrees."""
        # Validate every command prerequisite before provisioning any worktree or lease.
        self._verify_dispatch_sources(record)
        for child in command.children:
            self._role_kind(child.target_role)
        try:
            children = validate_workspace_batch(self.config, record, tuple(command.children))
        except SchedulingError as exc:
            raise SequentialWorkflowError(
                f"workspace batch is not schedulable: {exc}"
            ) from exc
        repo_id = _plan_step(record, children[0].step_id).repo_id
        try:
            outcome = self.workspace_coordinator.prepare(
                run_id=record.run_id,
                expected_generation=generation,
                repo_id=repo_id,
                step_ids=[child.step_id for child in children],
            )
            return self.prepare_batch(
                outcome.record,
                outcome.generation,
                command,
                workspace_group=outcome.group,
                sources_verified=True,
            )
        except Exception as exc:
            if "outcome" in locals():
                try:
                    self.workspace_coordinator.cleanup(
                        run_id=outcome.record.run_id,
                        expected_generation=outcome.generation,
                        workspace_group_id=outcome.group.workspace_group_id,
                        force=True,
                    )
                except WorkspaceError:
                    pass
                if isinstance(exc, SupervisorCommandRejectedError):
                    raise SequentialWorkflowError(
                        "workspace batch command became invalid after workspace preparation"
                    ) from exc
            if isinstance(exc, SequentialWorkflowError):
                raise
            raise SequentialWorkflowError(f"workspace batch preparation failed: {exc}") from exc

    def prepare_batch(
        self,
        record: RunRecord,
        generation: int,
        command: BatchDispatchCommand,
        *,
        workspace_group: WorkspaceGroup | None = None,
        sources_verified: bool = False,
    ) -> PreparedBatch:
        """Atomically prepare every independently valid child in a protocol-v2 batch."""
        # Source drift is a fail-closed prerequisite, never a supervisor correction.
        if not sources_verified:
            self._verify_dispatch_sources(record)
        if record.state is not RunStatus.RUNNING:
            raise SequentialWorkflowError("only RUNNING runs may prepare a batch")
        if workspace_group is None:
            try:
                children = validate_batch(self.config, record, tuple(command.children))
            except SchedulingError as exc:
                raise SequentialWorkflowError(f"batch is not schedulable: {exc}") from exc
        else:
            children = tuple(sorted(command.children, key=lambda child: _plan_step(record, child.step_id).ordinal))

        batch_id = f"batch-{uuid.uuid4().hex}"
        working = record
        prepared_children: list[PreparedDispatch] = []
        for child in children:
            step = _plan_step(working, child.step_id)
            if child.repo_id is not None and child.repo_id != step.repo_id:
                raise SupervisorCommandRejectedError(
                    f"supervisor repository assertion {child.repo_id!r} does not match step repository {step.repo_id!r}"
                )
            role_kind = self._role_kind(child.target_role)
            authorized_actions = role_scoped_authorized_actions(
                step.authorization.authorized_actions,
                role_kind,
            )
            if not working.steps[step.step_id].operator_gate_resolved:
                raise SequentialWorkflowError(
                    f"batch step {step.step_id} has an unresolved operator gate"
                )
            self._ensure_budget_allows_dispatch(
                working,
                step,
                role_key=child.target_role,
                session_mode=child.session_mode,
            )
            self._validate_step_readiness(working, step, role_kind, child.session_mode, child.target_role)
            workspace_child = _workspace_child(workspace_group, step.step_id) if workspace_group is not None else None
            workdir = Path(workspace_child.worktree_path) if workspace_child is not None else self.config.repository_root(step.repo_id)
            repository_before = (
                self._inspect_workspace(step.repo_id, workspace_child, require_clean=True)
                if workspace_child is not None
                else self._inspect_repository(step.repo_id, require_clean=True)
            )
            policy = generate_opencode_config(
                compile_effective_policy(
                    self.config,
                    repo_id=step.repo_id,
                    role_key=child.target_role,
                    dispatch_authorized_actions=authorized_actions,
                ),
                mcp_servers=compile_role_mcp_servers(self.config, child.target_role),
            )
            evidence_diagnostic_commands: tuple[str, ...] = ()
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
            review_authoritative_verification = (
                self.store.load_dispatch_payload(
                    working.run_id,
                    review_target.executor_dispatch_id,
                ).authoritative_verification
                if review_target is not None
                else None
            )
            worker_prompt = _worker_prompt(
                dispatch_id=dispatch_id,
                attempt=attempt,
                role_kind=role_kind,
                step=step,
                task=child.prompt,
                authoritative_sources=working.plan.sources,
                source_roots=_source_roots(self.config),
                repository=repository_before.dispatch_coordinate(
                    base_branch=workspace_group.base_branch if workspace_group is not None else None
                ),
                evidence_roots=self.config.repository(step.repo_id).evidence_roots,
                review_target=review_target,
                review_authoritative_verification=review_authoritative_verification,
                evidence_diagnostic_commands=evidence_diagnostic_commands,
                authorized_actions=authorized_actions,
                mcp_tools=resolve_role_mcp_tools(self.config, child.target_role),
            )
            dispatch = DispatchRecord(
                dispatch_id=dispatch_id,
                batch_id=batch_id,
                workspace_group_id=workspace_group.workspace_group_id if workspace_group is not None else None,
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
                    repository=repository_before.dispatch_coordinate(
                        base_branch=workspace_group.base_branch if workspace_group is not None else None
                    ),
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
            lease_keys = _lease_keys(step, include_repository=workspace_group is None)
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
        process_create_time: float,
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
            process_host=socket.gethostname(),
            process_started_at=event.occurred_at,
            process_create_time=process_create_time,
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
        pool, session_registry_key = session_registry_identity(dispatch)
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
        authoritative_verification: tuple[AuthoritativeVerification, ...] = (),
        usage: Mapping[str, object] | None = None,
        repository_after: RepositorySnapshot | None = None,
        structured_git_final: Mapping[str, Any] | None = None,
    ) -> tuple[RunRecord, int, str]:
        """Apply a typed executor result and persist the next supervisor message."""
        if prepared.dispatch.role_kind != "executor":
            raise SequentialWorkflowError("executor result does not match a reviewer dispatch")
        record, generation = self.store.load_run(prepared.run_id)
        if generation != prepared.generation and self.config.execution.scheduling == "sequential":
            raise SequentialWorkflowError("running dispatch generation is stale")
        dispatch = record.dispatches[prepared.dispatch.dispatch_id]
        record, generation = self._record_usage(
            record,
            generation,
            dispatch,
            usage,
            persist=structured_git_final is None,
        )
        _validate_executor_result(result, dispatch)
        step = _plan_step(record, dispatch.step_id)
        authoritative_verification = _effective_authoritative_verification(
            self.config,
            result,
            authoritative_verification,
        )
        _validate_result_verification(step, result, authoritative_verification)
        if isinstance(result, ExecutorCompletedResult):
            self._validate_executor_evidence(step, result)
        repository_after = repository_after or self._inspect_dispatch_repository(
            record, dispatch, require_clean=False
        )
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
        completed = transition_dispatch(
            dispatch,
            DispatchStatus.COMPLETED,
            completion_event,
            result_digest=_sha256_json(result.model_dump(mode="json")),
        )
        dispatches = dict(record.dispatches)
        dispatches[dispatch.dispatch_id] = completed
        record = record.model_copy(
            update={
                "dispatches": dispatches,
                "sequence": completion_event.sequence,
                "updated_at": completion_event.occurred_at,
            }
        )
        step_record = record.steps[step.step_id]
        result_event = self._event(record, "dispatcher", f"executor outcome {result.outcome}", dispatch.dispatch_id)
        updated_step, escalation_required = self._executor_step_outcome(
            record, step, step_record, dispatch, result, result_event
        )
        record, generation = self._replace_step(record, generation, updated_step)
        forwarding = _executor_forwarding(
            result,
            record.usage.by_session.get(
                dispatch.runtime_session_id or dispatch.logical_session_key,
                UsageAmount(),
            ),
            authoritative_verification,
        )
        record = self._forwarding_record(
            record,
            completed,
            dispatch_id=dispatch.dispatch_id,
            event_reason="supervisor forwarding persisted",
            forwarding=forwarding,
        )
        if escalation_required:
            record = self._escalation_waiting_record(record, step, dispatch)
        record, generation = self.store.persist_forwarded_dispatch(
            record,
            expected_generation=generation,
            dispatch_id=dispatch.dispatch_id,
            result=result.model_dump(mode="json"),
            authoritative_verification=[
                item.model_dump(mode="json") for item in authoritative_verification
            ],
            repository_after=repository_after.model_dump(mode="json"),
            forwarding_payload=forwarding,
            structured_git_final=structured_git_final,
        )
        self.store.release_leases(owner_id=prepared.lease_owner_id, resource_keys=prepared.lease_keys)
        if escalation_required:
            return record, generation, forwarding
        return self._apply_budget_limit(record, generation, dispatch, forwarding)

    def record_executor_proposal(
        self,
        prepared: PreparedDispatch,
        proposal: ExecutorProposal,
    ) -> RepositorySnapshot:
        """Validate and persist one model proposal before dispatcher side effects."""
        if prepared.dispatch.role_kind != "executor":
            raise SequentialWorkflowError("executor proposal does not match a reviewer dispatch")
        record, _generation = self.store.load_run(prepared.run_id)
        dispatch = record.dispatches[prepared.dispatch.dispatch_id]
        step = _plan_step(record, dispatch.step_id)
        _validate_executor_proposal(step, proposal, dispatch)
        repository_after = self._inspect_dispatch_repository(record, dispatch, require_clean=False)
        if isinstance(proposal, ExecutorCompletedProposal):
            validate_pending_executor_changes(
                self.config,
                coordinate=dispatch.intent.repository,
                before=prepared.repository_before,
                after=repository_after,
                root=prepared.workdir,
                writable_paths=step.authorization.writable_paths,
                require_changes=self.config.repository(step.repo_id).commit_policy == "required",
            )
            authoritative_evidence(
                self.config,
                repo_id=step.repo_id,
                snapshot=repository_after,
                requirements=step.evidence_requirements,
                declarations=proposal.evidence,
            )
        elif repository_after != prepared.repository_before:
            raise SequentialWorkflowError(
                "blocked or failed executor proposal left repository changes"
            )
        self.store.record_executor_proposal(
            run_id=record.run_id,
            dispatch_id=dispatch.dispatch_id,
            proposal=proposal.model_dump(mode="json"),
        )
        return repository_after

    def materialize_executor_proposal(
        self,
        prepared: PreparedDispatch,
        proposal: ExecutorProposal,
        *,
        authoritative_verification: tuple[AuthoritativeVerification, ...],
        usage: Mapping[str, object] | None,
        verified_snapshot: RepositorySnapshot | None = None,
    ) -> tuple[RunRecord, int, str]:
        """Convert a proposal into one dispatcher-authoritative executor result."""
        record, _generation = self.store.load_run(prepared.run_id)
        dispatch = record.dispatches[prepared.dispatch.dispatch_id]
        step = _plan_step(record, dispatch.step_id)
        repository = self.config.repository(step.repo_id)
        canonical_usage: Mapping[str, object] | None = None
        if usage is not None:
            canonical_usage = _usage_amount(usage).model_dump(mode="json")
        elif self.config.model.budget.enabled:
            raise SequentialWorkflowError(
                "measured OpenCode usage is required while budget enforcement is enabled"
            )
        repository_after = self._inspect_dispatch_repository(record, dispatch, require_clean=False)
        if verified_snapshot is not None and repository_after != verified_snapshot:
            raise WorkerResultValidationError(
                "repository changed between proposal inspection and verification materialization"
            )
        verification = [
            VerificationResult(check_id=item.check_id, status=item.status, summary=item.summary)
            for item in authoritative_verification
        ]
        structured_git_final: Mapping[str, Any]

        if isinstance(proposal, ExecutorCompletedProposal):
            if [item.check_id for item in authoritative_verification] != [
                criterion.criterion_id for criterion in step.acceptance_criteria
            ] or any(item.status != "passed" for item in authoritative_verification):
                raise WorkerResultValidationError(
                    "completed executor proposal requires exact passing dispatcher verification"
                )
            artifacts = authoritative_evidence(
                self.config,
                repo_id=step.repo_id,
                snapshot=repository_after,
                requirements=step.evidence_requirements,
                declarations=proposal.evidence,
            )
            checked_payload = {
                "authoritative_verification": [
                    item.model_dump(mode="json") for item in authoritative_verification
                ],
                "evidence": [item.model_dump(mode="json") for item in artifacts],
                "repository_before": prepared.repository_before.model_dump(mode="json"),
                "repository_dirty": repository_after.model_dump(mode="json"),
                "usage": canonical_usage,
            }
            if repository.commit_policy == "required":
                try:
                    intent = prepare_structured_git_intent(
                        self.config,
                        step=step,
                        attempt=dispatch.attempt,
                        worktree=prepared.workdir,
                        coordinate=dispatch.intent.repository,
                        before=prepared.repository_before,
                        dirty=repository_after,
                    )
                    self.store.record_structured_git_checked(
                        run_id=record.run_id,
                        dispatch_id=dispatch.dispatch_id,
                        checked=checked_payload,
                        intent=intent.model_dump(mode="json"),
                    )

                    def record_staged(stage: Any) -> None:
                        self.store.record_structured_git_staged(
                            run_id=record.run_id,
                            dispatch_id=dispatch.dispatch_id,
                            stage=stage.model_dump(mode="json"),
                        )

                    outcome = execute_structured_git_commit(
                        self.config,
                        worktree=prepared.workdir,
                        intent=intent,
                        on_staged=record_staged,
                    )
                except (RepositoryValidationError, StructuredGitError, StateStoreError) as exc:
                    try:
                        self.store.mark_structured_git_reconciliation(
                            run_id=record.run_id,
                            dispatch_id=dispatch.dispatch_id,
                            detail=str(exc),
                        )
                    except StateStoreError:
                        pass
                    raise SequentialWorkflowError(str(exc)) from exc
                repository_after = outcome.repository_after
                artifacts = authoritative_evidence(
                    self.config,
                    repo_id=step.repo_id,
                    snapshot=repository_after,
                    requirements=step.evidence_requirements,
                    declarations=proposal.evidence,
                )
                result_coordinate = ResultRepositoryCoordinate(
                    repo_id=step.repo_id,
                    base_revision=dispatch.intent.repository.base_revision,
                    result_revision=outcome.result_revision,
                    patch_sha256=None,
                )
                structured_git_final = {
                    "state": "COMMITTED",
                    "commit": outcome.commit.model_dump(mode="json"),
                    "result_revision": outcome.result_revision,
                    "repository_after": repository_after.model_dump(mode="json"),
                }
            else:
                validate_pending_executor_changes(
                    self.config,
                    coordinate=dispatch.intent.repository,
                    before=prepared.repository_before,
                    after=repository_after,
                    root=prepared.workdir,
                    writable_paths=step.authorization.writable_paths,
                    require_changes=False,
                )
                patch_sha256 = working_patch_sha256(prepared.workdir)
                self.store.record_structured_git_checked(
                    run_id=record.run_id,
                    dispatch_id=dispatch.dispatch_id,
                    checked=checked_payload,
                    intent=None,
                )
                result_coordinate = ResultRepositoryCoordinate(
                    repo_id=step.repo_id,
                    base_revision=dispatch.intent.repository.base_revision,
                    result_revision=repository_after.revision,
                    patch_sha256=patch_sha256,
                )
                structured_git_final = {
                    "state": "NO_COMMIT_FINALIZED",
                    "commit": None,
                    "result_revision": repository_after.revision,
                    "repository_after": repository_after.model_dump(mode="json"),
                }
            result: ExecutorResult = ExecutorCompletedResult(
                result_version=1,
                response_contract="dispatcher.executor_result.v1",
                dispatch_id=proposal.dispatch_id,
                attempt=proposal.attempt,
                step_id=proposal.step_id,
                repository=result_coordinate,
                evidence=artifacts,
                verification=verification,
                summary=proposal.summary,
                transcript_ref=proposal.transcript_ref,
                outcome="completed",
            )
        else:
            clean_after = self._inspect_dispatch_repository(record, dispatch, require_clean=True)
            if clean_after != prepared.repository_before:
                raise SequentialWorkflowError(
                    "blocked or failed executor proposal left repository changes"
                )
            self.store.record_structured_git_checked(
                run_id=record.run_id,
                dispatch_id=dispatch.dispatch_id,
                checked={
                    "repository_after": clean_after.model_dump(mode="json"),
                    "usage": canonical_usage,
                },
                intent=None,
            )
            skipped = [
                VerificationResult(
                    check_id=item.check_id,
                    status="skipped",
                    summary=item.summary,
                )
                for item in proposal.criterion_self_reports
            ]
            coordinate = ResultRepositoryCoordinate(
                repo_id=step.repo_id,
                base_revision=dispatch.intent.repository.base_revision,
                result_revision=clean_after.revision,
                patch_sha256=None,
            )
            if proposal.outcome == "blocked":
                result = ExecutorBlockedResult(
                    result_version=1,
                    response_contract="dispatcher.executor_result.v1",
                    dispatch_id=proposal.dispatch_id,
                    attempt=proposal.attempt,
                    step_id=proposal.step_id,
                    repository=coordinate,
                    evidence=[],
                    verification=skipped,
                    summary=proposal.summary,
                    transcript_ref=proposal.transcript_ref,
                    outcome="blocked",
                    blockers=proposal.blockers,
                )
            else:
                result = ExecutorFailedResult(
                    result_version=1,
                    response_contract="dispatcher.executor_result.v1",
                    dispatch_id=proposal.dispatch_id,
                    attempt=proposal.attempt,
                    step_id=proposal.step_id,
                    repository=coordinate,
                    evidence=[],
                    verification=skipped,
                    summary=proposal.summary,
                    transcript_ref=proposal.transcript_ref,
                    outcome="failed",
                    failure_code=proposal.failure_code,
                )
            repository_after = clean_after
            structured_git_final = {
                "state": "NO_COMMIT_FINALIZED",
                "commit": None,
                "result_revision": clean_after.revision,
                "repository_after": clean_after.model_dump(mode="json"),
            }
        return self.apply_executor_result(
            prepared,
            result,
            authoritative_verification=authoritative_verification,
            usage=canonical_usage,
            repository_after=repository_after,
            structured_git_final=structured_git_final,
        )

    @_serialized_transition
    def adopt_interrupted_structured_commit(
        self,
        run_id: str,
        dispatch_id: str,
    ) -> tuple[RunRecord, int, str]:
        """Adopt one exact dispatcher-created commit after its final state write was interrupted."""
        record, generation = self.store.load_run(run_id)
        try:
            dispatch = record.dispatches[dispatch_id]
        except KeyError as exc:
            raise SequentialWorkflowError(f"unknown recovery dispatch: {dispatch_id}") from exc
        if dispatch.role_kind != "executor" or dispatch.state is not DispatchStatus.RUNNING:
            raise SequentialWorkflowError(
                "structured commit adoption requires a RUNNING executor dispatch"
            )
        if _dispatch_process_is_active(dispatch):
            raise SequentialWorkflowError(
                "structured commit adoption is unavailable while the recorded worker process is active"
            )
        structured = self.store.load_structured_git_record(run_id, dispatch_id)
        if structured.state != "STAGED" or structured.intent is None or structured.checked is None:
            raise SequentialWorkflowError(
                "structured commit adoption requires durable STAGED intent and checked observations"
            )
        payload = self.store.load_dispatch_payload(run_id, dispatch_id)
        if payload.repository_before is None:
            raise SequentialWorkflowError("structured commit recovery has no durable pre-dispatch snapshot")
        step = _plan_step(record, dispatch.step_id)
        group = self._workspace_group_for_step(
            record,
            dispatch.step_id,
            dispatch.workspace_group_id,
        )
        workdir = (
            Path(_workspace_child(group, dispatch.step_id).worktree_path)
            if group is not None
            else self.config.repository_root(step.repo_id)
        )
        try:
            proposal = parse_executor_proposal(structured.proposal)
            if not isinstance(proposal, ExecutorCompletedProposal):
                raise SequentialWorkflowError(
                    "only a completed executor proposal can have an adoptable commit"
                )
            _validate_executor_proposal(step, proposal, dispatch)
            intent = StructuredGitIntent.model_validate_json(json.dumps(structured.intent))
            before = RepositorySnapshot.model_validate_json(json.dumps(payload.repository_before))
            checked_before = structured.checked.get("repository_before")
            if checked_before != before.model_dump(mode="json"):
                raise SequentialWorkflowError(
                    "structured commit checked snapshot differs from durable dispatch input"
                )
            raw_dirty = structured.checked.get("repository_dirty")
            if not isinstance(raw_dirty, dict):
                raise SequentialWorkflowError(
                    "structured commit recovery has no durable dirty repository observation"
                )
            dirty = RepositorySnapshot.model_validate_json(json.dumps(raw_dirty))
            if dirty.manifest_sha256 != intent.pre_commit_snapshot_sha256:
                raise SequentialWorkflowError(
                    "structured commit dirty snapshot does not match the durable commit intent"
                )
            if dirty.worktree_id != intent.worktree_id or dirty.revision != intent.base_revision:
                raise SequentialWorkflowError(
                    "structured commit dirty snapshot identity does not match the durable commit intent"
                )
            if dirty.git_metadata_sha256 != before.git_metadata_sha256:
                raise SequentialWorkflowError(
                    "structured commit dirty snapshot records Git metadata mutation"
                )
            if dirty.git_refs_sha256 != before.git_refs_sha256:
                raise SequentialWorkflowError(
                    "structured commit dirty snapshot records Git refs mutation"
                )
            raw_verification = structured.checked.get("authoritative_verification")
            if not isinstance(raw_verification, list):
                raise SequentialWorkflowError(
                    "structured commit recovery has no durable authoritative verification"
                )
            authoritative_verification = tuple(
                AuthoritativeVerification.model_validate_json(json.dumps(item))
                for item in raw_verification
            )
            raw_usage = structured.checked.get("usage")
            if raw_usage is None:
                if self.config.model.budget.enabled:
                    raise SequentialWorkflowError(
                        "structured commit recovery has no durable measured usage"
                    )
                recovery_usage: Mapping[str, object] | None = None
            elif isinstance(raw_usage, dict):
                recovery_usage = _usage_amount(raw_usage).model_dump(mode="json")
            else:
                raise SequentialWorkflowError(
                    "structured commit recovery has malformed durable measured usage"
                )
            adoption, repository_after = adopt_structured_git_commit(
                self.config,
                worktree=workdir,
                intent=intent,
            )
            artifacts = authoritative_evidence(
                self.config,
                repo_id=step.repo_id,
                snapshot=repository_after,
                requirements=step.evidence_requirements,
                declarations=proposal.evidence,
            )
            if structured.checked.get("evidence") != [
                artifact.model_dump(mode="json") for artifact in artifacts
            ]:
                raise SequentialWorkflowError(
                    "structured commit evidence differs from the durable checked observation"
                )
            if repository_after.git_metadata_sha256 != dirty.git_metadata_sha256:
                raise SequentialWorkflowError(
                    "adopted commit repository Git metadata differs from the durable dirty observation"
                )
        except (
            ResultError,
            RepositoryValidationError,
            StructuredGitError,
            SequentialWorkflowError,
            ValueError,
        ) as exc:
            try:
                self.store.mark_structured_git_reconciliation(
                    run_id=run_id,
                    dispatch_id=dispatch_id,
                    detail=str(exc),
                )
            except StateStoreError:
                pass
            if isinstance(exc, SequentialWorkflowError):
                raise
            raise SequentialWorkflowError(str(exc)) from exc

        verification = [
            VerificationResult(check_id=item.check_id, status=item.status, summary=item.summary)
            for item in authoritative_verification
        ]
        result = ExecutorCompletedResult(
            result_version=1,
            response_contract="dispatcher.executor_result.v1",
            dispatch_id=proposal.dispatch_id,
            attempt=proposal.attempt,
            step_id=proposal.step_id,
            repository=ResultRepositoryCoordinate(
                repo_id=step.repo_id,
                base_revision=dispatch.intent.repository.base_revision,
                result_revision=adoption.result_revision,
                patch_sha256=None,
            ),
            evidence=artifacts,
            verification=verification,
            summary=proposal.summary,
            transcript_ref=proposal.transcript_ref,
            outcome="completed",
        )
        dispatch_leases = [
            lease
            for lease in self.store.leases_for_run(run_id)
            if lease.owner_id.endswith(f".dispatch.{dispatch_id}")
        ]
        lease_owners = {lease.owner_id for lease in dispatch_leases}
        if len(lease_owners) > 1:
            raise SequentialWorkflowError(
                "structured commit recovery found multiple dispatch lease owners"
            )
        prepared = PreparedDispatch(
            run_id=run_id,
            generation=generation,
            dispatch=dispatch,
            prompt=payload.prompt,
            workdir=workdir,
            permission_config=dict(payload.policy),
            auto_approve=False,
            lease_keys=tuple(lease.resource_key for lease in dispatch_leases),
            lease_owner_id=(
                next(iter(lease_owners))
                if lease_owners
                else _dispatch_lease_owner_id(self.owner_id, dispatch_id)
            ),
            review_target=None,
            session_mode="new",
            session_id=None,
            repository_before=before,
        )
        try:
            return self.apply_executor_result(
                prepared,
                result,
                authoritative_verification=authoritative_verification,
                usage=recovery_usage,
                repository_after=repository_after,
                structured_git_final={
                    "state": "COMMITTED",
                    "commit": adoption.model_dump(mode="json"),
                    "result_revision": adoption.result_revision,
                    "repository_after": repository_after.model_dump(mode="json"),
                },
            )
        except (SequentialWorkflowError, StateStoreError) as exc:
            try:
                self.store.mark_structured_git_reconciliation(
                    run_id=run_id,
                    dispatch_id=dispatch_id,
                    detail=str(exc),
                )
            except StateStoreError:
                pass
            raise

    @_serialized_transition
    def recover_completed_dispatch(
        self,
        run_id: str,
        dispatch_id: str,
    ) -> tuple[RunRecord, int, str]:
        """Reconstruct and persist forwarding for a durable COMPLETED dispatch.

        Recovery uses only the durable result payload, authoritative
        verification, and dispatch/step state. It never reruns Git commands,
        verification checks, or a model. Step-outcome transitions replay only
        when the step's last durable event was not correlated to this dispatch,
        so acceptance and rework accounting apply exactly once.
        """
        record, generation = self.store.load_run(run_id)
        try:
            dispatch = record.dispatches[dispatch_id]
        except KeyError as exc:
            raise SequentialWorkflowError(f"unknown recovery dispatch: {dispatch_id}") from exc
        if dispatch.state is not DispatchStatus.COMPLETED:
            raise SequentialWorkflowError(
                "completed forwarding recovery requires a COMPLETED dispatch"
            )
        payload = self.store.load_dispatch_payload(run_id, dispatch_id)
        if payload.result is None:
            raise SequentialWorkflowError("completed dispatch has no durable result payload")
        if payload.forwarding_payload:
            raise SequentialWorkflowError(
                "completed dispatch already has a durable forwarding payload"
            )
        step = _plan_step(record, dispatch.step_id)
        step_record = record.steps[dispatch.step_id]
        applied = step_record.active_dispatch_id != dispatch_id
        escalation_required = False
        review_id: str | None = None
        review: Mapping[str, Any] | None = None
        if dispatch.role_kind == "executor":
            result: ExecutorResult = parse_executor_result(payload.result)
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
                raise SequentialWorkflowError(
                    f"completed executor dispatch has an invalid durable result: {exc}"
                ) from exc
            verification = tuple(
                AuthoritativeVerification.model_validate_json(json.dumps(item))
                for item in payload.authoritative_verification or ()
            )
            _validate_result_verification(step, result, verification)
            if not applied:
                if step_record.state is not StepStatus.EXECUTING:
                    raise SequentialWorkflowError(
                        "completed executor dispatch has incompatible step state "
                        f"{step_record.state.value}"
                    )
                result_event = self._event(
                    record,
                    "dispatcher",
                    f"executor outcome {result.outcome}",
                    dispatch_id,
                )
                updated_step, escalation_required = self._executor_step_outcome(
                    record, step, step_record, dispatch, result, result_event
                )
                record = self._step_replacement_record(record, updated_step)
            usage = record.usage.by_session.get(
                dispatch.runtime_session_id or dispatch.logical_session_key,
                UsageAmount(),
            )
            forwarding = _executor_forwarding(result, usage, verification)
        else:
            reviewer_result: ReviewerResult = parse_reviewer_result(payload.result)
            expectation = ResultExpectation(
                dispatch_id=dispatch.dispatch_id,
                attempt=dispatch.attempt,
                step_id=dispatch.step_id,
                repo_id=dispatch.intent.repository.repo_id,
                expected_review_target=self._review_target(record, step),
            )
            try:
                validate_reviewer_result_context(reviewer_result, expectation)
            except ResultError as exc:
                raise SequentialWorkflowError(
                    f"completed reviewer dispatch has an invalid durable result: {exc}"
                ) from exc
            verification = tuple(
                AuthoritativeVerification.model_validate_json(json.dumps(item))
                for item in payload.authoritative_verification or ()
            )
            _validate_result_verification(step, reviewer_result, verification)
            if (
                isinstance(reviewer_result, ReviewerAcceptedResult)
                and dispatch.role_key in step_record.accepted_reviewer_role_keys
                and not applied
            ):
                raise SequentialWorkflowError(
                    "completed reviewer dispatch was already accepted; "
                    "duplicate acceptance recovery is forbidden"
                )
            review = payload.result
            review_id = (
                None
                if self.store.review_for_dispatch(run_id, dispatch_id)
                else f"review-{uuid.uuid4().hex}"
            )
            if not applied:
                if step_record.state is not StepStatus.REVIEWING:
                    raise SequentialWorkflowError(
                        "completed reviewer dispatch has incompatible step state "
                        f"{step_record.state.value}"
                    )
                verdict_event = self._event(
                    record,
                    "dispatcher",
                    f"review verdict {reviewer_result.verdict}",
                    dispatch_id,
                )
                updated_step, escalation_required = self._reviewer_step_outcome(
                    record, step, step_record, dispatch, reviewer_result, verdict_event
                )
                record = self._step_replacement_record(record, updated_step)
            usage = record.usage.by_session.get(
                dispatch.runtime_session_id or dispatch.logical_session_key,
                UsageAmount(),
            )
            forwarding = _reviewer_forwarding(reviewer_result, usage, verification)
        record = self._forwarding_record(
            record,
            dispatch,
            dispatch_id=dispatch_id,
            event_reason="supervisor forwarding recovered",
            forwarding=forwarding,
        )
        if escalation_required:
            record = self._escalation_waiting_record(record, step, dispatch)
        record, generation = self.store.persist_forwarded_dispatch(
            record,
            expected_generation=generation,
            dispatch_id=dispatch_id,
            result=payload.result,
            authoritative_verification=list(payload.authoritative_verification or ()),
            repository_after=payload.repository_after,
            forwarding_payload=forwarding,
            review_id=review_id,
            review=review if review_id is not None else None,
        )
        return record, generation, forwarding

    @_serialized_transition
    def restore_forwarded_verification(
        self,
        run_id: str,
        dispatch_id: str,
        *,
        authoritative_verification: tuple[AuthoritativeVerification, ...],
    ) -> tuple[RunRecord, int, str]:
        """Repair only missing authority on a recovered, unacknowledged forwarding."""
        record, generation = self.store.load_run(run_id)
        try:
            dispatch = record.dispatches[dispatch_id]
        except KeyError as exc:
            raise SequentialWorkflowError(f"unknown recovery dispatch: {dispatch_id}") from exc
        if dispatch.state is not DispatchStatus.FORWARDED:
            raise SequentialWorkflowError(
                "verification restoration requires a FORWARDED dispatch"
            )
        payload = self.store.load_dispatch_payload(run_id, dispatch_id)
        if payload.result is None or payload.forwarding_payload is None:
            raise SequentialWorkflowError(
                "forwarded dispatch is missing its durable result or forwarding payload"
            )
        if _sha256_text(payload.forwarding_payload) != dispatch.forwarding_digest:
            raise SequentialWorkflowError(
                "forwarded dispatch payload does not match its durable forwarding digest"
            )
        if payload.authoritative_verification:
            raise SequentialWorkflowError(
                "verification restoration requires a forwarding with no durable authority"
            )
        step = _plan_step(record, dispatch.step_id)
        if dispatch.role_kind == "executor":
            executor_result = parse_executor_result(payload.result)
            expectation = ResultExpectation(
                dispatch_id=dispatch.dispatch_id,
                attempt=dispatch.attempt,
                step_id=dispatch.step_id,
                repo_id=dispatch.intent.repository.repo_id,
                expected_review_target=None,
            )
            try:
                validate_executor_result_context(executor_result, expectation)
            except ResultError as exc:
                raise SequentialWorkflowError(
                    f"forwarded executor dispatch has an invalid durable result: {exc}"
                ) from exc
            if not isinstance(executor_result, ExecutorCompletedResult):
                raise SequentialWorkflowError(
                    "verification restoration requires a completed executor result"
                )
            _validate_result_verification(step, executor_result, authoritative_verification)
            forwarding = _executor_forwarding(
                executor_result,
                record.usage.by_session.get(
                    dispatch.runtime_session_id or dispatch.logical_session_key,
                    UsageAmount(),
                ),
                authoritative_verification,
            )
        else:
            reviewer_result = parse_reviewer_result(payload.result)
            expectation = ResultExpectation(
                dispatch_id=dispatch.dispatch_id,
                attempt=dispatch.attempt,
                step_id=dispatch.step_id,
                repo_id=dispatch.intent.repository.repo_id,
                expected_review_target=self._review_target(record, step),
            )
            try:
                validate_reviewer_result_context(reviewer_result, expectation)
            except ResultError as exc:
                raise SequentialWorkflowError(
                    f"forwarded reviewer dispatch has an invalid durable result: {exc}"
                ) from exc
            if not isinstance(reviewer_result, ReviewerAcceptedResult):
                raise SequentialWorkflowError(
                    "verification restoration requires an accepted reviewer result"
                )
            _validate_result_verification(step, reviewer_result, authoritative_verification)
            forwarding = _reviewer_forwarding(
                reviewer_result,
                record.usage.by_session.get(
                    dispatch.runtime_session_id or dispatch.logical_session_key,
                    UsageAmount(),
                ),
                authoritative_verification,
            )
        event = self._event(
            record,
            "dispatcher",
            "authoritative verification restored for recovered forwarding",
            dispatch_id,
        )
        dispatches = dict(record.dispatches)
        dispatches[dispatch_id] = dispatch.model_copy(
            update={
                "forwarding_digest": _sha256_text(forwarding),
                "last_event": event,
            }
        )
        updated = record.model_copy(
            update={
                "dispatches": dispatches,
                "sequence": event.sequence,
                "updated_at": event.occurred_at,
            }
        )
        replacement = DispatchPayload(
            prompt=payload.prompt,
            policy=payload.policy,
            result=payload.result,
            authoritative_verification=tuple(
                item.model_dump(mode="json") for item in authoritative_verification
            ),
            forwarding_payload=forwarding,
            process_id=payload.process_id,
            session_metadata=payload.session_metadata,
            repository_before=payload.repository_before,
            repository_after=payload.repository_after,
        )
        generation = self.store.save_run(
            updated,
            expected_generation=generation,
            dispatch_payloads={dispatch_id: replacement},
        )
        return updated, generation, forwarding

    @_serialized_transition
    def adopt_failed_reviewer_result(
        self,
        run_id: str,
        dispatch_id: str,
        result: ReviewerResult,
        *,
        runtime_session_id: str,
        authoritative_verification: tuple[AuthoritativeVerification, ...],
        usage: Mapping[str, object] | None,
        actor_id: str,
    ) -> tuple[RunRecord, int]:
        """Adopt a typed result from an immutable failed reviewer response."""
        record, generation = self.store.load_run(run_id)
        try:
            dispatch = record.dispatches[dispatch_id]
        except KeyError as exc:
            raise SequentialWorkflowError(f"unknown recovery dispatch: {dispatch_id}") from exc
        request = record.operator_request
        if (
            record.state is not RunStatus.WAITING_OPERATOR
            or request is None
            or request.kind != "reconciliation"
            or request.context_ref != dispatch_id
            or request.step_id != dispatch.step_id
        ):
            raise SequentialWorkflowError(
                "failed review adoption requires the matching reconciliation request"
            )
        if (
            dispatch.role_kind != "reviewer"
            or dispatch.state is not DispatchStatus.FAILED
            or dispatch.failure_category != "result_validation"
        ):
            raise SequentialWorkflowError(
                "failed review adoption requires a result-validation FAILED reviewer dispatch"
            )
        if dispatch.runtime_session_id != runtime_session_id:
            raise SequentialWorkflowError(
                "failed review response session does not match the durable dispatch session"
            )
        step = _plan_step(record, dispatch.step_id)
        step_record = record.steps[dispatch.step_id]
        if step_record.state is not StepStatus.REVIEW_REQUIRED:
            raise SequentialWorkflowError(
                "failed review adoption requires a REVIEW_REQUIRED step"
            )
        payload = self.store.load_dispatch_payload(run_id, dispatch_id)
        if payload.result is not None or self.store.review_for_dispatch(run_id, dispatch_id):
            raise SequentialWorkflowError("failed reviewer result was already adopted")
        if payload.repository_before is None:
            raise SequentialWorkflowError("failed reviewer dispatch has no durable repository snapshot")
        metadata = payload.session_metadata or {}
        raw_target = metadata.get("review_target")
        if not isinstance(raw_target, dict):
            raise SequentialWorkflowError("failed reviewer dispatch has no durable review target")
        try:
            review_target = ReviewTarget.model_validate_json(json.dumps(raw_target))
            repository_before = RepositorySnapshot.model_validate_json(
                json.dumps(payload.repository_before)
            )
        except ValueError as exc:
            raise SequentialWorkflowError(
                "failed reviewer dispatch recovery metadata is malformed"
            ) from exc
        current_target = self._review_target(record, step)
        if review_target != current_target:
            raise SequentialWorkflowError(
                "failed reviewer dispatch target no longer matches the immutable work product"
            )
        expectation = ResultExpectation(
            dispatch_id=dispatch.dispatch_id,
            attempt=dispatch.attempt,
            step_id=dispatch.step_id,
            repo_id=dispatch.intent.repository.repo_id,
            expected_review_target=review_target,
        )
        try:
            validate_reviewer_result_context(result, expectation)
        except ResultError as exc:
            raise WorkerResultValidationError(str(exc)) from exc
        if (
            isinstance(result, ReviewerAcceptedResult)
            and dispatch.role_key in step_record.accepted_reviewer_role_keys
        ):
            raise WorkerResultValidationError(
                "reviewer role already accepted this immutable artifact"
            )
        _validate_result_verification(step, result, authoritative_verification)
        repository_after = self._inspect_dispatch_repository(record, dispatch, require_clean=False)
        try:
            validate_review_snapshot(
                self.config,
                coordinate=dispatch.intent.repository,
                before=repository_before,
                after=repository_after,
                review_target=result.review_target,
            )
        except RepositoryValidationError as exc:
            raise SequentialWorkflowError(str(exc)) from exc
        record, _unused_generation = self._record_usage(
            record,
            generation,
            dispatch,
            usage,
            persist=False,
        )
        reconcile_event = self._event(
            record,
            "operator",
            f"operator answered request {request.request_id}",
            dispatch_id,
        )
        steps = dict(record.steps)
        steps[dispatch.step_id] = step_record.model_copy(update={"last_event": reconcile_event})
        reconciled = transition_run(
            record.model_copy(
                update={
                    "steps": steps,
                    "sequence": reconcile_event.sequence,
                    "updated_at": reconcile_event.occurred_at,
                }
            ),
            RunStatus.RUNNING,
            reconcile_event,
        )
        reviewing_event = self._event(
            reconciled,
            "dispatcher",
            "adopting typed result from failed reviewer response",
            dispatch_id,
        )
        reviewing_step = transition_step(
            reconciled.steps[dispatch.step_id],
            StepStatus.REVIEWING,
            reviewing_event,
            active_dispatch_id=dispatch_id,
        )
        reviewing = self._step_replacement_record(reconciled, reviewing_step)
        verdict_event = self._event(
            reviewing,
            "dispatcher",
            f"adopted failed review verdict {result.verdict}",
            dispatch_id,
        )
        updated_step, escalation_required = self._reviewer_step_outcome(
            reviewing,
            step,
            reviewing_step,
            dispatch,
            result,
            verdict_event,
        )
        if escalation_required:
            raise SequentialWorkflowError(
                "failed review adoption cannot create a new escalation request"
            )
        updated = self._step_replacement_record(reviewing, updated_step)
        return self.store.persist_adopted_failed_review(
            updated,
            expected_generation=generation,
            dispatch_id=dispatch_id,
            result=result.model_dump(mode="json"),
            authoritative_verification=[
                item.model_dump(mode="json") for item in authoritative_verification
            ],
            repository_after=repository_after.model_dump(mode="json"),
            review_id=f"review-{uuid.uuid4().hex}",
            request_id=request.request_id,
            actor_id=actor_id,
        )

    @_serialized_transition
    def record_adopted_failed_review_usage(
        self,
        run_id: str,
        dispatch_id: str,
        result: ReviewerResult,
        *,
        runtime_session_id: str,
        usage: Mapping[str, object],
    ) -> tuple[RunRecord, int]:
        """Recover measured usage omitted from an already adopted failed review."""
        record, generation = self.store.load_run(run_id)
        try:
            dispatch = record.dispatches[dispatch_id]
        except KeyError as exc:
            raise SequentialWorkflowError(f"unknown recovery dispatch: {dispatch_id}") from exc
        if (
            record.state is not RunStatus.SUCCEEDED
            or dispatch.role_kind != "reviewer"
            or dispatch.state is not DispatchStatus.FAILED
            or dispatch.failure_category != "result_validation"
            or dispatch.runtime_session_id != runtime_session_id
        ):
            raise SequentialWorkflowError(
                "adopted review usage recovery does not match a terminal failed reviewer attempt"
            )
        payload = self.store.load_dispatch_payload(run_id, dispatch_id)
        if payload.result != result.model_dump(mode="json"):
            raise SequentialWorkflowError(
                "adopted review usage recovery result differs from the durable review"
            )
        if not self.store.review_for_dispatch(run_id, dispatch_id):
            raise SequentialWorkflowError("adopted review usage recovery has no durable review")
        if runtime_session_id in record.usage.by_session:
            raise SequentialWorkflowError("adopted review session usage was already recorded")
        updated, _unused_generation = self._record_usage(
            record,
            generation,
            dispatch,
            usage,
            persist=False,
        )
        next_generation = self.store.save_run(updated, expected_generation=generation)
        self.store.export_run_report(
            run_id,
            record_override=updated,
            generation_override=next_generation,
        )
        return updated, next_generation

    def inspect_reviewer_result(
        self,
        prepared: PreparedDispatch,
        result: ReviewerResult,
    ) -> RepositorySnapshot:
        """Validate a reviewer response and immutable target before running fresh checks."""
        if prepared.dispatch.role_kind != "reviewer" or prepared.review_target is None:
            raise SequentialWorkflowError("reviewer result does not match a prepared review dispatch")
        record, generation = self.store.load_run(prepared.run_id)
        if generation != prepared.generation and self.config.execution.scheduling == "sequential":
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
            raise WorkerResultValidationError(str(exc)) from exc
        step = _plan_step(record, dispatch.step_id)
        step_record = record.steps[step.step_id]
        if (
            isinstance(result, ReviewerAcceptedResult)
            and dispatch.role_key in step_record.accepted_reviewer_role_keys
        ):
            raise WorkerResultValidationError(
                "reviewer role already accepted this immutable artifact"
            )
        _validate_model_verification(step, result)
        repository_after = self._inspect_dispatch_repository(record, dispatch, require_clean=False)
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
        return repository_after

    @_serialized_transition
    def record_reviewer_verification_failure(
        self,
        prepared: PreparedDispatch,
        result: ReviewerAcceptedResult,
        *,
        authoritative_verification: tuple[AuthoritativeVerification, ...],
        usage: Mapping[str, object] | None,
        verified_snapshot: RepositorySnapshot,
    ) -> tuple[RunRecord, int]:
        """Persist a failed post-review rerun without recording acceptance."""
        record, generation = self.store.load_run(prepared.run_id)
        if generation != prepared.generation and self.config.execution.scheduling == "sequential":
            raise SequentialWorkflowError("running review generation is stale")
        dispatch = record.dispatches[prepared.dispatch.dispatch_id]
        step = _plan_step(record, dispatch.step_id)
        expected_ids = [criterion.criterion_id for criterion in step.acceptance_criteria]
        if [item.check_id for item in authoritative_verification] != expected_ids:
            raise WorkerResultValidationError(
                "failed acceptance verification does not exactly cover plan criteria"
            )
        if not any(item.status != "passed" for item in authoritative_verification):
            raise WorkerResultValidationError(
                "post-review feedback requires at least one failed dispatcher check"
            )
        observed = self.inspect_reviewer_result(prepared, result)
        if observed != verified_snapshot:
            raise WorkerResultValidationError(
                "repository changed between review inspection and failed verification persistence"
            )
        usage_error: SequentialWorkflowError | None = None
        try:
            record, generation = self._record_usage(
                record,
                generation,
                dispatch,
                usage,
                persist=False,
            )
        except SequentialWorkflowError as exc:
            usage_error = exc
        dispatch_event = self._event(
            record,
            "dispatcher",
            "post-review dispatcher verification requested executor rework",
            dispatch.dispatch_id,
        )
        failed_dispatch = transition_dispatch(
            dispatch,
            DispatchStatus.FAILED,
            dispatch_event,
            failure_category="acceptance_verification",
            failure_detail=_verification_failure_detail(authoritative_verification),
        )
        dispatches = dict(record.dispatches)
        dispatches[dispatch.dispatch_id] = failed_dispatch
        record = record.model_copy(
            update={
                "dispatches": dispatches,
                "sequence": dispatch_event.sequence,
                "updated_at": dispatch_event.occurred_at,
            }
        )
        step_record = record.steps[step.step_id]
        changed_event = self._event(
            record,
            "dispatcher",
            "fresh acceptance verification failed",
            dispatch.dispatch_id,
        )
        changed = transition_step(
            step_record,
            StepStatus.CHANGES_REQUESTED,
            changed_event,
        ).model_copy(
            update={
                "rework_rounds": step_record.rework_rounds + 1,
                "review_acceptances": 0,
                "accepted_reviewer_role_keys": [],
                "accepted_artifact_ids": [],
            }
        )
        may_retry = (
            usage_error is None
            and _supports_automatic_verification_rework(dispatch)
            and step.retry.on_changes_requested == "retry"
            and changed.rework_rounds < self.config.execution.max_rounds_per_step
            and changed.executor_attempts < step.retry.max_executor_attempts
            and changed.reviewer_attempts < step.retry.max_reviewer_attempts
        )
        if usage_error is not None:
            blocked_event = self._event(
                record,
                "dispatcher",
                "post-review verification usage could not be validated",
                dispatch.dispatch_id,
                sequence=changed_event.sequence + 1,
            )
            updated_step = transition_step(changed, StepStatus.BLOCKED, blocked_event)
        elif may_retry:
            ready_event = self._event(
                record,
                "dispatcher",
                "post-review verification rework is ready",
                dispatch.dispatch_id,
                sequence=changed_event.sequence + 1,
            )
            updated_step = transition_step(changed, StepStatus.READY, ready_event)
        elif (
            _supports_automatic_verification_rework(dispatch)
            and step.retry.on_changes_requested == "escalate"
        ):
            blocked_event = self._event(
                record,
                "dispatcher",
                "post-review verification rework requires escalation",
                dispatch.dispatch_id,
                sequence=changed_event.sequence + 1,
            )
            updated_step = transition_step(changed, StepStatus.BLOCKED, blocked_event)
        elif _supports_automatic_verification_rework(dispatch):
            failed_event = self._event(
                record,
                "dispatcher",
                "post-review verification rework policy halted the step",
                dispatch.dispatch_id,
                sequence=changed_event.sequence + 1,
            )
            updated_step = transition_step(changed, StepStatus.FAILED, failed_event)
        else:
            blocked_event = self._event(
                record,
                "dispatcher",
                "post-review verification requires operator reconciliation",
                dispatch.dispatch_id,
                sequence=changed_event.sequence + 1,
            )
            updated_step = transition_step(changed, StepStatus.BLOCKED, blocked_event)
        record = self._step_replacement_record(record, updated_step)
        if usage_error is not None and _supports_automatic_verification_rework(dispatch):
            record = self._verification_usage_waiting_record(record, dispatch, usage_error)
        elif (
            _supports_automatic_verification_rework(dispatch)
            and step.retry.on_changes_requested == "escalate"
        ):
            record = self._escalation_waiting_record(record, step, dispatch)
        elif dispatch.batch_id is None and not _supports_automatic_verification_rework(dispatch):
            record = self._unsupported_verification_rework_waiting_record(record, dispatch)
        payload = self.store.load_dispatch_payload(record.run_id, dispatch.dispatch_id)
        metadata = dict(payload.session_metadata or {})
        metadata["verification_feedback"] = [
            item.model_dump(mode="json") for item in authoritative_verification
        ]
        generation = self.store.save_run(
            record,
            expected_generation=generation,
            dispatch_payloads={
                dispatch.dispatch_id: DispatchPayload(
                    prompt=payload.prompt,
                    policy=payload.policy,
                    result=result.model_dump(mode="json"),
                    authoritative_verification=tuple(
                        item.model_dump(mode="json") for item in authoritative_verification
                    ),
                    forwarding_payload=payload.forwarding_payload,
                    process_id=payload.process_id,
                    session_metadata=metadata,
                    repository_before=payload.repository_before,
                    repository_after=observed.model_dump(mode="json"),
                )
            },
        )
        self.store.release_leases(
            owner_id=prepared.lease_owner_id,
            resource_keys=prepared.lease_keys,
        )
        return record, generation

    @_serialized_transition
    def prepare_pending_verification_retry(
        self,
        record: RunRecord,
        generation: int,
    ) -> PreparedDispatch | tuple[RunRecord, int] | None:
        """Recover one persisted verification rework before another supervisor turn."""
        candidates = [
            dispatch
            for dispatch in record.dispatches.values()
            if dispatch.state is DispatchStatus.FAILED
            and dispatch.failure_category
            in {"authoritative_verification", "acceptance_verification"}
            and record.steps[dispatch.step_id].state is StepStatus.READY
            and _supports_automatic_verification_rework(dispatch)
            and dispatch.attempt
            == (
                record.steps[dispatch.step_id].executor_attempts
                if dispatch.role_kind == "executor"
                else record.steps[dispatch.step_id].reviewer_attempts
            )
        ]
        if not candidates:
            return None
        failed_dispatch = max(
            candidates,
            key=lambda dispatch: (dispatch.last_event.sequence, dispatch.dispatch_id),
        )
        try:
            payload = self.store.load_dispatch_payload(record.run_id, failed_dispatch.dispatch_id)
            verification = _authoritative_verification_from_payload(payload)
            return self._prepare_durable_verification_retry(
                record,
                generation,
                failed_dispatch,
                verification,
                payload,
            )
        except (SequentialWorkflowError, RepositoryValidationError, StateStoreError, ValueError) as exc:
            return self._verification_retry_waiting_record(
                record,
                generation,
                failed_dispatch,
                reason=str(exc),
            )

    @_serialized_transition
    def prepare_pending_reviewer_result_validation_retry(
        self,
        record: RunRecord,
        generation: int,
    ) -> PreparedDispatch | tuple[RunRecord, int] | None:
        """Prepare one safe fresh retry for a durable malformed reviewer response."""
        candidates = [
            dispatch
            for dispatch in record.dispatches.values()
            if dispatch.role_kind == "reviewer"
            and dispatch.state is DispatchStatus.FAILED
            and dispatch.failure_category == "result_validation"
            and record.steps[dispatch.step_id].state is StepStatus.REVIEW_REQUIRED
        ]
        if not candidates:
            return None
        failed_dispatch = max(
            candidates,
            key=lambda dispatch: (dispatch.last_event.sequence, dispatch.dispatch_id),
        )
        try:
            self._verify_reviewer_result_validation_retry(record, failed_dispatch)
            if record.state is RunStatus.WAITING_OPERATOR:
                request = record.operator_request
                if (
                    request is None
                    or request.kind != "reconciliation"
                    or request.context_ref != failed_dispatch.dispatch_id
                    or request.step_id != failed_dispatch.step_id
                ):
                    raise SequentialWorkflowError(
                        "failed reviewer retry is blocked by an unrelated operator request"
                    )
                resume_event = self._event(
                    record,
                    "dispatcher",
                    "durable malformed reviewer response is safe to retry",
                    failed_dispatch.dispatch_id,
                )
                record = transition_run(record, RunStatus.RUNNING, resume_event)
                generation = self.store.save_run(record, expected_generation=generation)
            if record.state is not RunStatus.RUNNING:
                raise SequentialWorkflowError(
                    f"failed reviewer retry requires a RUNNING run, found {record.state.value}"
                )
            retried = self.prepare_dispatch(
                record,
                generation,
                DispatchCommand(
                    protocol_version=1,
                    action="dispatch",
                    step_id=failed_dispatch.step_id,
                    target_role=failed_dispatch.role_key,
                    session_mode="new",
                    prompt=(
                        "Return the required typed reviewer result for the same immutable review target. "
                        "Do not repeat any executor work or accepted reviewer work."
                    ),
                    rationale="dispatcher-owned malformed reviewer response recovery",
                ),
            )
            if not isinstance(retried, PreparedDispatch):
                raise SequentialWorkflowError("reviewer result-validation retry unexpectedly requested an operator gate")
            return retried
        except (SequentialWorkflowError, RepositoryValidationError, StateStoreError, ValueError) as exc:
            return self._reviewer_result_validation_retry_waiting_record(
                record,
                generation,
                failed_dispatch,
                reason=str(exc),
            )

    def _verify_reviewer_result_validation_retry(
        self,
        record: RunRecord,
        failed_dispatch: DispatchRecord,
    ) -> None:
        """Reject retries unless the failed reader still targets the exact same work product."""
        if failed_dispatch.batch_id is not None:
            raise SequentialWorkflowError("batch reviewer result-validation retries require reconciliation")
        step = _plan_step(record, failed_dispatch.step_id)
        step_record = record.steps[failed_dispatch.step_id]
        if failed_dispatch.attempt != step_record.reviewer_attempts:
            raise SequentialWorkflowError("failed reviewer attempt is not the step's current reviewer attempt")
        if step_record.reviewer_attempts >= step.retry.max_reviewer_attempts:
            raise SequentialWorkflowError("step exhausted reviewer attempts")
        if step_record.stalls >= self.config.execution.stall_policy.maximum_retries_per_step:
            raise SequentialWorkflowError("step exhausted bounded reviewer retries")
        obligation = self._review_obligation(record, step)
        if failed_dispatch.role_key not in obligation.reviewer_role_keys:
            raise SequentialWorkflowError("failed reviewer role is no longer obligated for the step")
        if failed_dispatch.role_key in step_record.accepted_reviewer_role_keys:
            raise SequentialWorkflowError("failed reviewer role already accepted the immutable artifact")
        if any(
            dispatch.dispatch_id != failed_dispatch.dispatch_id
            and dispatch.state in ACTIVE_DISPATCH_STATES
            for dispatch in record.dispatches.values()
        ):
            raise SequentialWorkflowError("another dispatch is unresolved during reviewer retry recovery")
        self._verify_dispatch_sources(record)
        payload = self.store.load_dispatch_payload(record.run_id, failed_dispatch.dispatch_id)
        if (
            payload.result is not None
            or payload.forwarding_payload is not None
            or self.store.review_for_dispatch(record.run_id, failed_dispatch.dispatch_id)
        ):
            raise SequentialWorkflowError("failed reviewer response was already durably applied")
        if hashlib.sha256(payload.prompt.encode("utf-8")).hexdigest() != failed_dispatch.intent.prompt_sha256:
            raise SequentialWorkflowError("failed reviewer prompt does not match its immutable dispatch intent")
        if _sha256_json(payload.policy) != failed_dispatch.intent.policy_digest:
            raise SequentialWorkflowError("failed reviewer policy does not match its immutable dispatch intent")
        if payload.repository_before is None:
            raise SequentialWorkflowError("failed reviewer dispatch has no durable repository snapshot")
        metadata = payload.session_metadata or {}
        raw_target = metadata.get("review_target")
        if not isinstance(raw_target, dict):
            raise SequentialWorkflowError("failed reviewer dispatch has no durable review target")
        try:
            review_target = ReviewTarget.model_validate_json(json.dumps(raw_target))
            repository_before = RepositorySnapshot.model_validate_json(
                json.dumps(payload.repository_before)
            )
        except ValueError as exc:
            raise SequentialWorkflowError(
                "failed reviewer retry recovery metadata is malformed"
            ) from exc
        if review_target != self._review_target(record, step):
            raise SequentialWorkflowError(
                "failed reviewer target no longer matches the immutable work product"
            )
        observed = self._inspect_dispatch_repository(record, failed_dispatch, require_clean=True)
        if observed != repository_before:
            raise SequentialWorkflowError(
                "repository snapshot changed after the malformed reviewer response"
            )
        validate_review_snapshot(
            self.config,
            coordinate=failed_dispatch.intent.repository,
            before=repository_before,
            after=observed,
            review_target=review_target,
        )

    def _reviewer_result_validation_retry_waiting_record(
        self,
        record: RunRecord,
        generation: int,
        failed_dispatch: DispatchRecord,
        *,
        reason: str,
    ) -> tuple[RunRecord, int]:
        """Preserve failed reviewer evidence and require an operator when retry checks fail."""
        if record.state is RunStatus.WAITING_OPERATOR:
            return record, generation
        if record.state is not RunStatus.RUNNING:
            raise SequentialWorkflowError(
                f"failed reviewer retry cannot enter recovery from {record.state.value}"
            )
        safe_reason = redact_text(reason)[:5000]
        event = self._event(
            record,
            "dispatcher",
            "malformed reviewer response retry requires operator reconciliation",
            failed_dispatch.dispatch_id,
        )
        request = OperatorRequest(
            request_id=f"request-{uuid.uuid4().hex}",
            question=(
                f"A malformed response from reviewer {failed_dispatch.role_key} cannot be retried safely: "
                f"{safe_reason}. Reconcile the immutable review target and repository snapshot or halt."
            ),
            allowed_answers=["reconcile", "halt"],
            context_ref=failed_dispatch.dispatch_id,
            resume_to=RunStatus.RUNNING,
            expires_at=None,
            required_role=None,
            kind="reconciliation",
            step_id=failed_dispatch.step_id,
        )
        waiting = transition_run(record, RunStatus.WAITING_OPERATOR, event, operator_request=request)
        return waiting, self.store.save_run(waiting, expected_generation=generation)

    def _verification_usage_waiting_record(
        self,
        record: RunRecord,
        dispatch: DispatchRecord,
        error: SequentialWorkflowError,
    ) -> RunRecord:
        """Stop after preserving failed verification when usage cannot be trusted."""
        budget_enforced = self._verification_budget_stop()
        event = self._event(
            record,
            "dispatcher",
            "verification evidence persisted but OpenCode usage could not be validated",
            dispatch.dispatch_id,
        )
        safe_reason = redact_text(str(error))[:5000]
        request = OperatorRequest(
            request_id=f"request-{uuid.uuid4().hex}",
            question=(
                "Dispatcher-owned verification evidence was saved, but OpenCode usage could not be "
                f"validated: {safe_reason}. "
                + (
                    "Budget enforcement forbids further paid work; halt this run."
                    if budget_enforced
                    else "Reconcile the usage record and session state or halt."
                )
            ),
            allowed_answers=["halt"] if budget_enforced else ["reconcile", "halt"],
            context_ref=dispatch.dispatch_id,
            resume_to=RunStatus.HALTED if budget_enforced else RunStatus.RUNNING,
            expires_at=None,
            required_role=None,
            kind="budget" if budget_enforced else "reconciliation",
            step_id=dispatch.step_id,
        )
        return transition_run(record, RunStatus.WAITING_OPERATOR, event, operator_request=request)

    def _verification_budget_stop(self, reason: str | None = None) -> bool:
        """Keep retry-preparation and usage-validation budget stops consistent."""
        if not self.config.model.budget.enabled:
            return False
        if reason is None:
            return True
        normalized = reason.lower()
        return "budget" in normalized or "token limit" in normalized

    def _verification_retry_waiting_record(
        self,
        record: RunRecord,
        generation: int,
        failed_dispatch: DispatchRecord,
        *,
        reason: str,
    ) -> tuple[RunRecord, int]:
        safe_reason = redact_text(reason)[:5000]
        step = record.steps[failed_dispatch.step_id]
        blocked_event = self._event(
            record,
            "dispatcher",
            "verification retry preparation requires operator action",
            failed_dispatch.dispatch_id,
        )
        blocked = transition_step(step, StepStatus.BLOCKED, blocked_event)
        record = self._step_replacement_record(record, blocked)
        request_event = self._event(
            record,
            "dispatcher",
            "verification retry could not be prepared safely",
            failed_dispatch.dispatch_id,
        )
        budget_failure = self._verification_budget_stop(safe_reason)
        request = OperatorRequest(
            request_id=f"request-{uuid.uuid4().hex}",
            question=(
                f"Verification retry preparation failed: {safe_reason}. "
                + ("Halt this run." if budget_failure else "Reconcile repository and session state or halt.")
            ),
            allowed_answers=["halt"] if budget_failure else ["reconcile", "halt"],
            context_ref=failed_dispatch.dispatch_id,
            resume_to=RunStatus.HALTED if budget_failure else RunStatus.RUNNING,
            expires_at=None,
            required_role=None,
            kind="budget" if budget_failure else "reconciliation",
            step_id=failed_dispatch.step_id,
        )
        waiting = transition_run(
            record,
            RunStatus.WAITING_OPERATOR,
            request_event,
            operator_request=request,
        )
        next_generation = self.store.save_run(waiting, expected_generation=generation)
        return waiting, next_generation

    def _unsupported_verification_rework_waiting_record(
        self,
        record: RunRecord,
        failed_dispatch: DispatchRecord,
    ) -> RunRecord:
        """Stop non-batch workspace work after saving authoritative check evidence."""
        event = self._event(
            record,
            "dispatcher",
            "verification rework is unsupported for this workspace dispatch",
            failed_dispatch.dispatch_id,
        )
        request = OperatorRequest(
            request_id=f"request-{uuid.uuid4().hex}",
            question=(
                "Dispatcher-owned verification failed for a workspace dispatch. "
                "Reconcile the workspace and session state or halt."
            ),
            allowed_answers=["reconcile", "halt"],
            context_ref=failed_dispatch.dispatch_id,
            resume_to=RunStatus.RUNNING,
            expires_at=None,
            required_role=None,
            kind="reconciliation",
            step_id=failed_dispatch.step_id,
        )
        return transition_run(record, RunStatus.WAITING_OPERATOR, event, operator_request=request)

    def _prepare_durable_verification_retry(
        self,
        record: RunRecord,
        generation: int,
        failed_dispatch: DispatchRecord,
        verification: tuple[AuthoritativeVerification, ...],
        payload: DispatchPayload,
    ) -> PreparedDispatch:
        if not _supports_automatic_verification_rework(failed_dispatch):
            raise SequentialWorkflowError(
                "automatic verification rework is unsupported for batch or workspace dispatches"
            )
        step = record.steps[failed_dispatch.step_id]
        if step.state is not StepStatus.READY:
            raise SequentialWorkflowError("failed verification is not eligible for automatic retry")
        retry_before: RepositorySnapshot | None = None
        retry_snapshot: RepositorySnapshot | None = None
        if failed_dispatch.failure_category == "authoritative_verification":
            if payload.repository_before is None or payload.repository_after is None:
                raise SequentialWorkflowError(
                    "verification retry is missing durable repository snapshots"
                )
            retry_before = RepositorySnapshot.model_validate_json(
                json.dumps(payload.repository_before)
            )
            retry_snapshot = RepositorySnapshot.model_validate_json(
                json.dumps(payload.repository_after)
            )
            target_role = failed_dispatch.role_key
            prompt = (
                "Correct the same authorized step after dispatcher-owned verification failed. "
                "Use verification_feedback and return a new "
                "dispatcher.executor_proposal.v2 object."
            )
            rationale = "Dispatcher-owned verification requested bounded rework."
        elif failed_dispatch.failure_category == "acceptance_verification":
            if payload.repository_before is None or payload.repository_after is None:
                raise SequentialWorkflowError(
                    "post-review verification retry is missing durable repository snapshots"
                )
            retry_before = RepositorySnapshot.model_validate_json(
                json.dumps(payload.repository_before)
            )
            retry_snapshot = RepositorySnapshot.model_validate_json(
                json.dumps(payload.repository_after)
            )
            metadata = payload.session_metadata or {}
            raw_target = metadata.get("review_target")
            if not isinstance(raw_target, dict):
                raise SequentialWorkflowError("failed review has no durable executor target")
            review_target = ReviewTarget.model_validate_json(json.dumps(raw_target))
            try:
                target_role = record.dispatches[
                    review_target.executor_dispatch_id
                ].role_key
            except KeyError as exc:
                raise SequentialWorkflowError(
                    "failed review executor target disappeared"
                ) from exc
            prompt = (
                "Correct the same authorized step because the fresh post-review dispatcher "
                "verification failed. Use verification_feedback and return a new "
                "dispatcher.executor_proposal.v2 object."
            )
            rationale = "Fresh post-review verification requested bounded rework."
        else:
            raise SequentialWorkflowError("dispatch has no recoverable verification failure")
        prepared = self.prepare_dispatch(
            record,
            generation,
            DispatchCommand(
                protocol_version=1,
                action="dispatch",
                step_id=failed_dispatch.step_id,
                target_role=target_role,
                session_mode="resume",
                prompt=prompt,
                rationale=rationale,
            ),
            retry_repository_before=retry_before,
            retry_expected_snapshot=retry_snapshot,
            retry_require_changes=(
                None
                if failed_dispatch.failure_category == "authoritative_verification"
                else False
            ),
            verification_feedback=verification,
        )
        if not isinstance(prepared, PreparedDispatch):
            raise SequentialWorkflowError("verification retry unexpectedly requested an operator gate")
        return prepared

    @_serialized_transition
    def apply_reviewer_result(
        self,
        prepared: PreparedDispatch,
        result: ReviewerResult,
        *,
        authoritative_verification: tuple[AuthoritativeVerification, ...] = (),
        usage: Mapping[str, object] | None = None,
    ) -> tuple[RunRecord, int, str]:
        """Apply an immutable reviewer verdict to the exact reviewed work product."""
        if prepared.dispatch.role_kind != "reviewer" or prepared.review_target is None:
            raise SequentialWorkflowError("reviewer result does not match a prepared review dispatch")
        record, generation = self.store.load_run(prepared.run_id)
        if generation != prepared.generation and self.config.execution.scheduling == "sequential":
            raise SequentialWorkflowError("running review generation is stale")
        dispatch = record.dispatches[prepared.dispatch.dispatch_id]
        repository_after = self.inspect_reviewer_result(prepared, result)
        record, generation = self._record_usage(record, generation, dispatch, usage)
        step = _plan_step(record, dispatch.step_id)
        authoritative_verification = _effective_authoritative_verification(
            self.config,
            result,
            authoritative_verification,
        )
        _validate_result_verification(step, result, authoritative_verification)
        completion_event = self._event(record, "reviewer", "typed reviewer result received", dispatch.dispatch_id)
        completed = transition_dispatch(
            dispatch,
            DispatchStatus.COMPLETED,
            completion_event,
            result_digest=_sha256_json(result.model_dump(mode="json")),
        )
        dispatches = dict(record.dispatches)
        dispatches[dispatch.dispatch_id] = completed
        record = record.model_copy(
            update={
                "dispatches": dispatches,
                "sequence": completion_event.sequence,
                "updated_at": completion_event.occurred_at,
            }
        )
        step_record = record.steps[step.step_id]
        verdict_event = self._event(record, "dispatcher", f"review verdict {result.verdict}", dispatch.dispatch_id)
        updated_step, escalation_required = self._reviewer_step_outcome(
            record, step, step_record, dispatch, result, verdict_event
        )
        record, generation = self._replace_step(record, generation, updated_step)
        forwarding = _reviewer_forwarding(
            result,
            record.usage.by_session.get(
                dispatch.runtime_session_id or dispatch.logical_session_key,
                UsageAmount(),
            ),
            authoritative_verification,
        )
        record = self._forwarding_record(
            record,
            completed,
            dispatch_id=dispatch.dispatch_id,
            event_reason="review forwarding persisted",
            forwarding=forwarding,
        )
        if escalation_required:
            record = self._escalation_waiting_record(record, step, dispatch)
        record, generation = self.store.persist_forwarded_dispatch(
            record,
            expected_generation=generation,
            dispatch_id=dispatch.dispatch_id,
            result=result.model_dump(mode="json"),
            authoritative_verification=[
                item.model_dump(mode="json") for item in authoritative_verification
            ],
            repository_after=repository_after.model_dump(mode="json"),
            forwarding_payload=forwarding,
            review_id=f"review-{uuid.uuid4().hex}",
            review=result.model_dump(mode="json"),
        )
        self.store.release_leases(owner_id=prepared.lease_owner_id, resource_keys=prepared.lease_keys)
        if escalation_required:
            return record, generation, forwarding
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
        updated, next_generation = self.store.commit_dispatch_transition(
            record,
            expected_generation=generation,
            dispatch_id=dispatch_id,
            target=DispatchStatus.ACKNOWLEDGED,
            event=event,
        )
        dispatch = updated.dispatches[dispatch_id]
        if dispatch.workspace_group_id is None:
            return updated, next_generation
        return self._maybe_integrate_workspace_group(updated, next_generation, dispatch.workspace_group_id)

    def _maybe_integrate_workspace_group(
        self,
        record: RunRecord,
        generation: int,
        workspace_group_id: str,
    ) -> tuple[RunRecord, int]:
        try:
            group = record.workspace_groups[workspace_group_id]
        except KeyError as exc:
            raise SequentialWorkflowError("workspace group disappeared before integration") from exc
        if group.state is not WorkspaceGroupStatus.ACTIVE:
            return record, generation
        group_steps = {child.step_id for child in group.children}
        if any(record.steps[step_id].state is not StepStatus.ACCEPTED for step_id in group_steps):
            return record, generation
        if any(
            dispatch.workspace_group_id == workspace_group_id and dispatch.state in ACTIVE_DISPATCH_STATES
            for dispatch in record.dispatches.values()
        ):
            return record, generation
        try:
            outcome = self.workspace_coordinator.integrate(
                run_id=record.run_id,
                expected_generation=generation,
                workspace_group_id=workspace_group_id,
            )
            return outcome.record, outcome.generation
        except WorkspaceError:
            failed_record, failed_generation = self.store.load_run(record.run_id)
            request_event = self._event(
                failed_record,
                "dispatcher",
                "workspace integration requires operator reconciliation",
                workspace_group_id,
            )
            request = OperatorRequest(
                request_id=f"request-{uuid.uuid4().hex}",
                question=(
                    f"Workspace group {workspace_group_id} could not be integrated. "
                    "Reconcile the temporary branches before continuing."
                ),
                allowed_answers=["reconcile", "halt"],
                context_ref=workspace_group_id,
                resume_to=RunStatus.RUNNING,
                expires_at=None,
                required_role=None,
                kind="workspace_reconciliation",
                step_id=None,
            )
            waiting = transition_run(failed_record, RunStatus.WAITING_OPERATOR, request_event, operator_request=request)
            waiting_generation = self.store.save_run(waiting, expected_generation=failed_generation)
            return waiting, waiting_generation

    @_serialized_transition
    def recover_interrupted_dispatch(
        self,
        run_id: str,
        dispatch_id: str,
        *,
        recovery_reason: str | None = None,
    ) -> tuple[RunRecord, int]:
        """Convert an abandoned active dispatch into an explicit operator reconciliation."""
        record, generation = self.store.load_run(run_id)
        try:
            dispatch = record.dispatches[dispatch_id]
        except KeyError as exc:
            raise SequentialWorkflowError(f"unknown interrupted dispatch {dispatch_id}") from exc
        if dispatch.state not in {DispatchStatus.PREPARED, DispatchStatus.RUNNING}:
            return record, generation
        if dispatch.state is DispatchStatus.RUNNING and _dispatch_process_is_active(dispatch):
            raise SequentialWorkflowError(
                f"interrupted dispatch {dispatch_id} may still have an active process; cancel it before recovery"
            )
        invalid_prompt = recovery_reason is not None
        failure_detail = recovery_reason or "OpenCode invocation did not durably apply a worker result"
        target = DispatchStatus.ABANDONED if dispatch.state is DispatchStatus.PREPARED else DispatchStatus.FAILED
        dispatch_event = self._event(
            record,
            "dispatcher",
            (
                "invalid persisted worker prompt requires operator reconciliation"
                if invalid_prompt
                else "interrupted OpenCode invocation requires operator reconciliation"
            ),
            dispatch_id,
        )
        recovered_dispatch = transition_dispatch(
            dispatch,
            target,
            dispatch_event,
            failure_category="invalid_persisted_prompt" if invalid_prompt else "interrupted",
            failure_detail=failure_detail,
        )
        dispatches = dict(record.dispatches)
        dispatches[dispatch_id] = recovered_dispatch
        record = record.model_copy(
            update={
                "dispatches": dispatches,
                "sequence": dispatch_event.sequence,
                "updated_at": dispatch_event.occurred_at,
            }
        )
        step = record.steps[dispatch.step_id]
        step_event = self._event(
            record,
            "dispatcher",
            (
                "invalid persisted worker prompt requires operator reconciliation"
                if invalid_prompt
                else "interrupted worker requires operator reconciliation"
            ),
            dispatch_id,
        )
        if step.state is StepStatus.EXECUTING:
            updated_step = transition_step(step, StepStatus.BLOCKED, step_event)
        elif step.state is StepStatus.REVIEWING:
            updated_step = transition_step(step, StepStatus.REVIEW_REQUIRED, step_event)
        else:
            raise SequentialWorkflowError(
                f"interrupted dispatch {dispatch_id} has incompatible step state {step.state.value}"
            )
        record = self._step_replacement_record(record, updated_step)
        if dispatch.batch_id is not None:
            generation = self.store.save_run(record, expected_generation=generation)
            return record, generation
        request_event = self._event(
            record,
            "dispatcher",
            (
                "invalid persisted worker prompt requires operator reconciliation"
                if invalid_prompt
                else "interrupted worker requires operator reconciliation"
            ),
            dispatch_id,
        )
        request = OperatorRequest(
            request_id=f"request-{uuid.uuid4().hex}",
            question=(
                "The persisted worker prompt cannot be safely replayed: "
                f"{redact_text(failure_detail)[:5000]}. "
                "It was not launched. Reconcile the dispatch record or halt."
                if invalid_prompt
                else "An interrupted OpenCode worker may have made external changes without a durable result. "
                "Reconcile repository and session state before any retry."
            ),
            allowed_answers=["reconcile", "halt"],
            context_ref=dispatch_id,
            resume_to=RunStatus.RUNNING,
            expires_at=None,
            required_role=None,
            kind="reconciliation",
            step_id=dispatch.step_id,
        )
        waiting = transition_run(record, RunStatus.WAITING_OPERATOR, request_event, operator_request=request)
        generation = self.store.save_run(waiting, expected_generation=generation)
        return waiting, generation

    @_serialized_transition
    def fail_dispatch(
        self,
        prepared: PreparedDispatch,
        *,
        reason: str,
        failure_category: str,
        failure_detail: str,
    ) -> tuple[RunRecord, int]:
        """Record a failed adapter/result boundary without advancing the plan step."""
        safe_reason = redact_text(reason)[:5000]
        safe_detail = redact_text(failure_detail)[:5000]
        try:
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
            event = self._event(record, "dispatcher", safe_reason, dispatch.dispatch_id)
            record, generation = self.store.commit_dispatch_transition(
                record,
                expected_generation=generation,
                dispatch_id=dispatch.dispatch_id,
                target=target,
                event=event,
                failure_category=failure_category,
                failure_detail=safe_detail,
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
            return record, generation
        finally:
            self.store.release_leases(owner_id=prepared.lease_owner_id, resource_keys=prepared.lease_keys)

    @_serialized_transition
    def handle_stall(
        self,
        prepared: PreparedDispatch,
        *,
        category: str,
        reason: str,
    ) -> tuple[RunRecord, int, bool]:
        """Persist one interruption and decide whether a bounded retry is allowed."""
        record, generation = self.store.load_run(prepared.run_id)
        dispatch = record.dispatches[prepared.dispatch.dispatch_id]
        if dispatch.state not in {DispatchStatus.PREPARED, DispatchStatus.RUNNING}:
            raise SequentialWorkflowError(f"cannot record stall from dispatch state {dispatch.state.value}")
        event = self._event(record, "dispatcher", f"worker stall: {category}", dispatch.dispatch_id)
        record, generation = self.store.commit_dispatch_transition(
            record,
            expected_generation=generation,
            dispatch_id=dispatch.dispatch_id,
            target=DispatchStatus.FAILED if dispatch.state is DispatchStatus.RUNNING else DispatchStatus.ABANDONED,
            event=event,
            failure_category=category,
            failure_detail=reason,
        )
        step = record.steps[dispatch.step_id]
        stall_count = step.stalls + 1
        step_event = self._event(record, "dispatcher", "stall recorded", dispatch.dispatch_id)
        if step.state is StepStatus.EXECUTING:
            blocked = transition_step(step, StepStatus.BLOCKED, step_event)
            retry_state = StepStatus.READY
        elif step.state is StepStatus.REVIEWING:
            blocked = transition_step(step, StepStatus.REVIEW_REQUIRED, step_event)
            retry_state = StepStatus.REVIEW_REQUIRED
        else:
            raise SequentialWorkflowError(f"stalled dispatch has incompatible step state {step.state.value}")
        updated_step = blocked.model_copy(
            update={"stalls": stall_count, "last_stall_category": category, "last_stall_reason": reason[:5000]}
        )
        plan_step = next(item for item in record.plan.steps if item.step_id == dispatch.step_id)
        attempt_limit_available = (
            step.executor_attempts < plan_step.retry.max_executor_attempts
            if dispatch.role_kind == "executor"
            else step.reviewer_attempts < plan_step.retry.max_reviewer_attempts
        )
        retry_allowed = (
            stall_count <= self.config.execution.stall_policy.maximum_retries_per_step
            and attempt_limit_available
        )
        if retry_allowed:
            ready_event = self._event(record, "dispatcher", "stall retry is ready", dispatch.dispatch_id)
            if updated_step.state is not retry_state:
                updated_step = transition_step(updated_step, retry_state, ready_event)
            else:
                updated_step = updated_step.model_copy(update={"last_event": ready_event})
            updated_step = updated_step.model_copy(
                update={"stalls": stall_count, "last_stall_category": category, "last_stall_reason": reason[:5000]}
            )
        elif self.config.execution.stall_policy.on_exhausted == "fail":
            failed_event = self._event(
                record,
                "dispatcher",
                "stall retry limit exhausted; step failed",
                dispatch.step_id,
                sequence=updated_step.last_event.sequence + 1,
            )
            updated_step = transition_step(updated_step, StepStatus.FAILED, failed_event)
        record, generation = self._replace_step(record, generation, updated_step)
        if not retry_allowed:
            if self.config.execution.stall_policy.on_exhausted == "ask":
                request_event = self._event(record, "dispatcher", "stall retry limit exhausted", dispatch.step_id)
                request = OperatorRequest(
                    request_id=f"request-{uuid.uuid4().hex}",
                    question=(
                        f"Step {dispatch.step_id} exhausted stall retries after {stall_count} interruptions. "
                        "Retry once more or halt?"
                    ),
                    allowed_answers=["retry", "halt"],
                    context_ref=dispatch.dispatch_id,
                    resume_to=RunStatus.RUNNING,
                    expires_at=None,
                    required_role=None,
                    kind="stall_recovery",
                    step_id=dispatch.step_id,
                )
                record = transition_run(record, RunStatus.WAITING_OPERATOR, request_event, operator_request=request)
                generation = self.store.save_run(record, expected_generation=generation)
            elif self.config.execution.stall_policy.on_exhausted == "halt":
                halt_event = self._event(record, "dispatcher", "stall retry limit exhausted; halted", dispatch.step_id)
                record = transition_run(record, RunStatus.HALTED, halt_event)
                generation = self.store.save_run(record, expected_generation=generation)
        self.store.release_leases(owner_id=prepared.lease_owner_id, resource_keys=prepared.lease_keys)
        return record, generation, retry_allowed

    def prepare_stall_retry(
        self,
        record: RunRecord,
        generation: int,
        prepared: PreparedDispatch,
        *,
        category: str,
    ) -> PreparedDispatch:
        """Create a fresh dispatch with a bounded continuation instruction."""
        command = DispatchCommand(
            protocol_version=1,
            action="dispatch",
            step_id=prepared.dispatch.step_id,
            target_role=prepared.dispatch.role_key,
            session_mode="new",
            prompt=(
                "Continue the current approved step from its last durable result. "
                "Do not repeat completed side effects. Return the required typed result only. "
                f"The previous interruption category was {category}."
            ),
            rationale="dispatcher-owned bounded stall continuation",
        )
        retried = self.prepare_dispatch(record, generation, command)
        if not isinstance(retried, PreparedDispatch):
            raise SequentialWorkflowError("stall retry could not prepare a worker dispatch")
        return retried

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
        updated = self._step_replacement_record(record, updated_step)
        return updated, self.store.save_run(updated, expected_generation=generation)

    def _step_replacement_record(
        self,
        record: RunRecord,
        updated_step: StepRecord,
    ) -> RunRecord:
        """Build the record with one replaced step and the terminal-failure invariant."""
        steps = dict(record.steps)
        steps[updated_step.step_id] = updated_step
        updated = record.model_copy(
            update={
                "steps": steps,
                "sequence": updated_step.last_event.sequence,
                "updated_at": updated_step.last_event.occurred_at,
            }
        )
        if updated_step.state is StepStatus.FAILED:
            if record.state in {RunStatus.RUNNING, RunStatus.WAITING_OPERATOR}:
                failed_event = self._event(
                    record,
                    "dispatcher",
                    f"step {updated_step.step_id} entered FAILED; run failed",
                    updated_step.step_id,
                    sequence=updated_step.last_event.sequence + 1,
                )
                updated = transition_run(updated, RunStatus.FAILED, failed_event)
            elif record.state is not RunStatus.FAILED:
                raise SequentialWorkflowError(
                    f"cannot persist a failed step while run is {record.state.value}"
                )
        return updated

    def _forwarding_record(
        self,
        record: RunRecord,
        completed: DispatchRecord,
        *,
        dispatch_id: str,
        event_reason: str,
        forwarding: str,
    ) -> RunRecord:
        """Transition one completed dispatch to FORWARDED in memory."""
        forward_event = self._event(record, "dispatcher", event_reason, dispatch_id)
        forwarded = transition_dispatch(
            completed,
            DispatchStatus.FORWARDED,
            forward_event,
            forwarding_digest=_sha256_text(forwarding),
        )
        dispatches = dict(record.dispatches)
        dispatches[dispatch_id] = forwarded
        return record.model_copy(
            update={
                "dispatches": dispatches,
                "sequence": forward_event.sequence,
                "updated_at": forward_event.occurred_at,
            }
        )

    def _executor_step_outcome(
        self,
        record: RunRecord,
        step: PlanStep,
        step_record: StepRecord,
        dispatch: DispatchRecord,
        result: ExecutorResult,
        result_event: TransitionEvent,
    ) -> tuple[StepRecord, bool]:
        """Apply one executor outcome to its step and report escalation need."""
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
            return (
                updated_step.model_copy(
                    update={
                        "accepted_artifact_ids": [item.artifact_id for item in result.evidence],
                        # A fresh executor result invalidates prior reviewer votes on older work.
                        "review_acceptances": 0,
                        "accepted_reviewer_role_keys": [],
                    }
                ),
                False,
            )
        if result.outcome == "blocked":
            blocked = transition_step(step_record, StepStatus.BLOCKED, result_event)
            updated_step = self._retry_or_terminal_step(
                record,
                step,
                blocked,
                policy=step.retry.on_blocked,
                correlation_id=dispatch.dispatch_id,
            )
            return updated_step, step.retry.on_blocked == "escalate"
        if step.retry.on_failed == "retry" and step_record.executor_attempts < step.retry.max_executor_attempts:
            blocked = transition_step(step_record, StepStatus.BLOCKED, result_event)
            updated_step = self._retry_or_terminal_step(
                record,
                step,
                blocked,
                policy="retry",
                correlation_id=dispatch.dispatch_id,
            )
            return updated_step, False
        if step.retry.on_failed == "escalate":
            return transition_step(step_record, StepStatus.BLOCKED, result_event), True
        return transition_step(step_record, StepStatus.FAILED, result_event), False

    def _reviewer_step_outcome(
        self,
        record: RunRecord,
        step: PlanStep,
        step_record: StepRecord,
        dispatch: DispatchRecord,
        result: ReviewerResult,
        verdict_event: TransitionEvent,
    ) -> tuple[StepRecord, bool]:
        """Apply one reviewer verdict to its step and report escalation need."""
        if isinstance(result, ReviewerAcceptedResult):
            obligation = self._review_obligation(record, step)
            accepted_roles = [*step_record.accepted_reviewer_role_keys, dispatch.role_key]
            accepted_count = step_record.review_acceptances + 1
            target = (
                StepStatus.ACCEPTED
                if accepted_count >= obligation.required_acceptances
                else StepStatus.REVIEW_REQUIRED
            )
            updated_step = transition_step(step_record, target, verdict_event)
            return (
                updated_step.model_copy(
                    update={
                        "review_acceptances": accepted_count,
                        "accepted_reviewer_role_keys": accepted_roles,
                        "accepted_artifact_ids": (
                            [requirement.artifact_id for requirement in step.evidence_requirements]
                            if target is StepStatus.ACCEPTED
                            else []
                        ),
                    }
                ),
                False,
            )
        if isinstance(result, ReviewerChangesRequestedResult):
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
                return transition_step(changed, StepStatus.REVIEW_REQUIRED, tie_break_event), False
            if (
                step.retry.on_changes_requested == "retry"
                and changed.rework_rounds < self.config.execution.max_rounds_per_step
                and step_record.executor_attempts < step.retry.max_executor_attempts
            ):
                ready_event = self._event(
                    record,
                    "dispatcher",
                    "review rework is ready",
                    dispatch.dispatch_id,
                    sequence=verdict_event.sequence + 1,
                )
                return transition_step(changed, StepStatus.READY, ready_event), False
            if step.retry.on_changes_requested == "escalate":
                blocked_event = self._event(
                    record,
                    "dispatcher",
                    "review rework requires escalation",
                    dispatch.dispatch_id,
                    sequence=verdict_event.sequence + 1,
                )
                return transition_step(changed, StepStatus.BLOCKED, blocked_event), True
            failed_event = self._event(
                record,
                "dispatcher",
                "review rework policy halted the step",
                dispatch.dispatch_id,
                sequence=verdict_event.sequence + 1,
            )
            return transition_step(changed, StepStatus.FAILED, failed_event), False
        blocked = transition_step(step_record, StepStatus.BLOCKED, verdict_event)
        if step.retry.on_blocked == "retry" and step_record.reviewer_attempts < step.retry.max_reviewer_attempts:
            retry_event = self._event(
                record,
                "dispatcher",
                "review retry is ready",
                dispatch.dispatch_id,
                sequence=verdict_event.sequence + 1,
            )
            return transition_step(blocked, StepStatus.REVIEW_REQUIRED, retry_event), False
        if step.retry.on_blocked == "escalate":
            return blocked, True
        failed_event = self._event(
            record,
            "dispatcher",
            "review blocked policy halted the step",
            dispatch.dispatch_id,
            sequence=verdict_event.sequence + 1,
        )
        return transition_step(blocked, StepStatus.FAILED, failed_event), False

    def _escalation_waiting_record(
        self,
        record: RunRecord,
        step: PlanStep,
        dispatch: DispatchRecord,
    ) -> RunRecord:
        """Build the WAITING_OPERATOR escalation request without persisting it."""
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
        return transition_run(record, RunStatus.WAITING_OPERATOR, event, operator_request=request)

    def _wait_for_operator(
        self,
        record: RunRecord,
        generation: int,
        command: AskOperatorCommand,
    ) -> RunRecord:
        if record.policy is None or record.policy.underspec_mode != "ask":
            raise SupervisorCommandRejectedError(
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
            raise SupervisorCommandRejectedError("review waiver requires a step awaiting review")
        if not obligation.waivable:
            raise SupervisorCommandRejectedError("compiled review obligation cannot be waived")
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
            raise SequentialWorkflowError(
                "run cost budget is exhausted; a new dispatch is forbidden"
            )
        if step_usage.cost_usd >= budget.max_step_cost_usd:
            raise SequentialWorkflowError(
                f"step {step.step_id} cost budget is exhausted; a new dispatch is forbidden"
            )
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
        *,
        persist: bool = True,
    ) -> tuple[RunRecord, int]:
        journaled_usage, journal_status = self.store.invocation_usage_for_dispatch(
            record.run_id,
            dispatch.dispatch_id,
        )
        if journal_status == "PENDING":
            raise SequentialWorkflowError(
                "OpenCode invocation usage must be finalized before result application"
            )
        if journal_status == "COMPLETE":
            if journaled_usage is None:
                raise SequentialWorkflowError(
                    "complete OpenCode invocation usage is missing its measured amount"
                )
            if usage is not None and _usage_amount(usage) != journaled_usage:
                raise SequentialWorkflowError(
                    "result usage differs from the finalized OpenCode invocation"
                )
            return record, generation
        if journal_status == "MISSING":
            if usage is not None:
                raise SequentialWorkflowError(
                    "result usage was supplied for an invocation finalized without usage"
                )
            if self.config.model.budget.enabled:
                raise SequentialWorkflowError(
                    "measured OpenCode usage is required while budget enforcement is enabled"
                )
            return record, generation
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
        if not persist:
            return updated, generation
        return updated, self.store.save_run(updated, expected_generation=generation)

    def _apply_budget_limit(
        self,
        record: RunRecord,
        generation: int,
        dispatch: DispatchRecord,
        forwarding: str,
    ) -> tuple[RunRecord, int, str]:
        if record.state is not RunStatus.RUNNING:
            return record, generation, forwarding
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

    def _verify_dispatch_sources(self, record: RunRecord) -> None:
        """Recheck the immutable source ledger immediately before prompt preparation."""
        try:
            verify_plan_sources(record.plan, self.config)
        except PlanError as exc:
            raise SequentialWorkflowError(f"cannot prepare worker dispatch: {exc}") from exc

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

    def _review_obligation(self, record: RunRecord, step: PlanStep) -> CompiledReviewObligation:
        policy = record.policy
        if policy is None:
            try:
                policy = compile_run_policy(self.config, record.plan)
            except PolicyError as exc:
                raise SequentialWorkflowError(
                    f"run policy must be compiled before dispatch: {exc}"
                ) from exc
        try:
            return policy.review_obligations[step.step_id]
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
            raise SupervisorCommandRejectedError(
                f"step {step.step_id} is {step_record.state.value}, not {expected_state.value}"
            )
        if role_kind == "executor":
            if (
                step_record.reassignment_role_key is not None
                and role_key != step_record.reassignment_role_key
            ):
                raise SupervisorCommandRejectedError(
                    f"step {step.step_id} requires reassignment to {step_record.reassignment_role_key}"
                )
            if step_record.executor_attempts >= step.retry.max_executor_attempts:
                raise SequentialWorkflowError(
                    f"step {step.step_id} exhausted executor attempts"
                )
        else:
            obligation = self._review_obligation(record, step)
            if mode != "new":
                raise SupervisorCommandRejectedError(
                    "reviewer sessions must be new for independent review"
                )
            if role_key not in obligation.reviewer_role_keys:
                raise SupervisorCommandRejectedError(
                    f"role {role_key} is not compiled to review step {step.step_id}"
                )
            if role_key in step_record.accepted_reviewer_role_keys:
                raise SupervisorCommandRejectedError(
                    f"role {role_key} already accepted step {step.step_id}"
                )
            if step_record.reviewer_attempts >= step.retry.max_reviewer_attempts:
                raise SequentialWorkflowError(
                    f"step {step.step_id} exhausted reviewer attempts"
                )
        for dependency_id in step.depends_on:
            dependency = record.steps[dependency_id]
            if dependency.state not in {StepStatus.ACCEPTED, StepStatus.WAIVED}:
                raise SupervisorCommandRejectedError(
                    f"step {step.step_id} dependency {dependency_id} is not accepted"
                )
        for artifact in step.required_inputs:
            if artifact.producer_step_id is None:
                continue
            producer = record.steps[artifact.producer_step_id]
            if producer.state not in {StepStatus.ACCEPTED, StepStatus.WAIVED}:
                raise SupervisorCommandRejectedError(
                    f"step {step.step_id} input producer {artifact.producer_step_id} is not accepted"
                )
        if mode in {"resume", "fork"}:
            sessions = self.store.sessions_for_run(record.run_id)
            pool = "executors" if role_kind == "executor" else "reviewers"
            if not sessions.get(pool, {}).get(role_key, {}).get("session_id"):
                raise SequentialWorkflowError(
                    "requested session mode has no dispatcher-owned session"
                )

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

    def _inspect_workspace(self, repo_id: str, child: WorkspaceChild, *, require_clean: bool) -> RepositorySnapshot:
        try:
            return inspect_workspace(
                self.config,
                repo_id,
                root=Path(child.worktree_path),
                expected_branch=child.branch,
                require_clean=require_clean,
            )
        except RepositoryValidationError as exc:
            raise SequentialWorkflowError(str(exc)) from exc

    def _inspect_dispatch_repository(
        self,
        record: RunRecord,
        dispatch: DispatchRecord,
        *,
        require_clean: bool,
    ) -> RepositorySnapshot:
        group = self._workspace_group_for_step(record, dispatch.step_id, dispatch.workspace_group_id)
        if group is None:
            return self._inspect_repository(dispatch.intent.repository.repo_id, require_clean=require_clean)
        return self._inspect_workspace(
            dispatch.intent.repository.repo_id,
            _workspace_child(group, dispatch.step_id),
            require_clean=require_clean,
        )

    def _workspace_group_for_step(
        self,
        record: RunRecord,
        step_id: str,
        workspace_group_id: str | None = None,
    ) -> WorkspaceGroup | None:
        if workspace_group_id is not None:
            try:
                group = record.workspace_groups[workspace_group_id]
            except KeyError as exc:
                raise SequentialWorkflowError("dispatch references an unknown workspace group") from exc
            if step_id not in {child.step_id for child in group.children}:
                raise SequentialWorkflowError("workspace group does not own dispatch step")
            return group
        groups = [
            group
            for group in record.workspace_groups.values()
            if group.state in {WorkspaceGroupStatus.ACTIVE, WorkspaceGroupStatus.INTEGRATING}
            and step_id in {child.step_id for child in group.children}
        ]
        if len(groups) > 1:
            raise SequentialWorkflowError("multiple active workspace groups own one step")
        return groups[0] if groups else None

    def _is_workspace_batch_candidate(self, record: RunRecord, command: BatchDispatchCommand) -> bool:
        if self.config.execution.concurrency.same_repository_mode != "worktree_barrier":
            return False
        if len(command.children) < 2:
            return False
        try:
            repo_ids = {_plan_step(record, child.step_id).repo_id for child in command.children}
        except SequentialWorkflowError:
            return False
        return len(repo_ids) == 1

    def _validate_executor_evidence(self, step: PlanStep, result: ExecutorResult) -> None:
        expected = {item.artifact_id: item for item in step.evidence_requirements}
        actual = {item.artifact_id: item for item in result.evidence}
        if set(actual) != set(expected):
            raise WorkerResultValidationError(
                "executor result evidence does not exactly match step requirements"
            )
        for artifact_id, requirement in expected.items():
            artifact = actual[artifact_id]
            if artifact.relative_path != requirement.relative_path or artifact.media_type != requirement.media_type:
                raise WorkerResultValidationError(
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
        try:
            role_kind = self.config.role_kind(role_key)
        except ConfigError as exc:
            raise SupervisorCommandRejectedError(str(exc)) from exc
        if role_kind not in {"executor", "reviewer"}:
            raise SupervisorCommandRejectedError("supervisor cannot be a dispatch target")
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
    raise SupervisorCommandRejectedError(f"supervisor requested unknown plan step: {step_id}")


def session_registry_identity(
    dispatch: DispatchRecord,
) -> tuple[Literal["executors", "reviewers"], str]:
    """Return the durable session-registry identity for one dispatch."""
    pool: Literal["executors", "reviewers"] = (
        "executors" if dispatch.role_kind == "executor" else "reviewers"
    )
    key = dispatch.logical_session_key if dispatch.batch_id is not None else dispatch.role_key
    return pool, key


def _lease_keys(step: PlanStep, *, include_repository: bool = True) -> tuple[str, ...]:
    keys = set(resource_keys(step.repo_id, tuple(lock.resource_id for lock in step.resource_locks)))
    if not include_repository:
        keys.discard(f"repository:{step.repo_id}")
    return tuple(sorted(keys))


def _workspace_child(group: WorkspaceGroup, step_id: str) -> WorkspaceChild:
    for child in group.children:
        if child.step_id == step_id:
            return child
    raise SequentialWorkflowError(f"workspace group {group.workspace_group_id} does not own step {step_id}")


def _dispatch_lease_owner_id(owner_id: str, dispatch_id: str) -> str:
    return f"{owner_id}.dispatch.{dispatch_id}"


def _dispatch_process_is_active(dispatch: DispatchRecord) -> bool:
    """Fail closed unless the recorded local worker is known to have exited."""
    if (
        dispatch.process_id is None
        or dispatch.process_create_time is None
        or dispatch.process_host != socket.gethostname()
    ):
        return True
    try:
        os.kill(dispatch.process_id, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


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


def _observation_tools_markdown(config: Config) -> str:
    native = ", ".join(f"`{tool}`" for tool in READ_ONLY_NATIVE_TOOLS)
    diagnostics = "\n".join(f"- `{command}`" for command in READ_ONLY_DIAGNOSTIC_COMMANDS)
    supervisor_role = next(iter(config.model.roles.supervisor))
    mcp_tools = resolve_role_mcp_tools(config, supervisor_role)
    mcp_line = (
        "- MCP tools: " + ", ".join(f"`{tool}`" for tool in mcp_tools) + "."
        if mcp_tools
        else "- MCP tools: none."
    )
    return (
        f"- Native content inspection: {native}.\n"
        "- Exact diagnostic shell commands:\n"
        f"{diagnostics}\n"
        f"{mcp_line}"
    )


def _evidence_diagnostic_commands(
    step: PlanStep,
    evidence_roots: list[str],
) -> tuple[str, ...]:
    """Return exact non-mutating hash/size commands for declared evidence paths."""
    commands: list[str] = []
    for root in evidence_roots:
        for requirement in step.evidence_requirements:
            path = shlex.quote(str(Path(root) / requirement.relative_path))
            commands.extend((f"shasum -a 256 {path}", f"wc -c {path}"))
    return tuple(commands)


def _dispatch_example(config: Config, record: RunRecord) -> str:
    step = record.plan.steps[0]
    role = next(iter(config.model.roles.executors))
    return DispatchCommand(
        protocol_version=1,
        action="dispatch",
        step_id=step.step_id,
        target_role=role,
        session_mode="new",
        prompt=(
            "Perform only the authorized work, preserve the plan constraints, and return exactly "
            "one dispatcher.executor_proposal.v2 JSON object."
        ),
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
    authoritative_sources: tuple[PlanSource, ...],
    source_roots: Mapping[str, Path],
    repository: DispatchRepositoryCoordinate,
    evidence_roots: list[str],
    review_target: ReviewTarget | None,
    review_authoritative_verification: tuple[Mapping[str, Any], ...] | None,
    evidence_diagnostic_commands: tuple[str, ...],
    authorized_actions: tuple[str, ...],
    mcp_tools: tuple[str, ...] = (),
    verification_feedback: tuple[AuthoritativeVerification, ...] = (),
) -> str:
    """Render the exact machine context a worker needs to return a typed result."""
    response_schema_name = (
        "executor-proposal-v2.json" if role_kind == "executor" else "reviewer-result-v1.json"
    )
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
            "authoritative_sources": _authoritative_sources(authoritative_sources, source_roots),
            "authoritative_sources_rule": (
                "Read the exact listed path entries as normative. You must not substitute similarly named "
                "files. Do not calculate hashes or treat Bash, tests, or Git output as source authority; "
                "dispatcher source verification is authoritative."
            ),
            **(
                {
                    "role_instruction": (
                        "You are a reviewer. Use native read, glob, and grep to inspect file contents "
                        "and locate files. Use exact diagnostic shell commands only for current "
                        "directory, branch, revision, status, and diff metadata. Do not add shell "
                        "arguments, redirection, chaining, pipes, substitutions, or other shell syntax. "
                        "Do not run tests. Do not create, edit, stage, commit, delete, or otherwise "
                        "modify files or Git state. "
                        "If remediation is required, describe it in required_remediation for the "
                        "executor; do not perform it."
                    ),
                    "observation_tools": {
                        "native": list(READ_ONLY_NATIVE_TOOLS),
                        "diagnostic_commands": list(READ_ONLY_DIAGNOSTIC_COMMANDS),
                        "mcp": list(mcp_tools),
                    },
                }
                if role_kind == "reviewer"
                else {
                    "role_instruction": (
                        "Write only paths authorized by writable_paths. Do not run acceptance tests "
                        "or substitute verification commands. Do not stage, commit, push, modify "
                        "branches, or modify Git configuration. The dispatcher inspects changes, "
                        "executes every structured check, derives evidence metadata, and performs "
                        "any authorized commit after your response."
                    ),
                    "writable_paths": list(step.authorization.writable_paths),
                    "research_tools": {
                        "mcp": list(mcp_tools),
                        "rule": (
                            "Research MCP tools are optional capabilities for discovering library "
                            "documentation, packing code for analysis, and semantic code search. "
                            "They do not expand writable_paths and cannot replace dispatcher checks."
                        ),
                    },
                }
            ),
            "evidence_roots": evidence_roots,
            "evidence_path_rule": (
                "Each evidence relative_path is relative to one listed evidence_root; "
                "do not include the evidence_root directory name in relative_path."
            ),
            "authorized_actions": list(authorized_actions),
            "verification_authority": (
                "The dispatcher executes every acceptance criterion check from structured argv. "
                "Do not invent, alter, or substitute commands. Model verification is a self-report; "
                "state advancement uses dispatcher-owned results."
            ),
            **(
                {
                    "verification_feedback": [
                        item.model_dump(mode="json") for item in verification_feedback
                    ],
                    "verification_feedback_rule": (
                        "These are dispatcher-owned results from the previous proposal. Correct the "
                        "implementation without running or substituting the checks; the dispatcher "
                        "will rerun the complete plan-owned check set."
                    ),
                }
                if verification_feedback
                else {}
            ),
            **(
                {
                    "executor_authoritative_verification": list(
                        review_authoritative_verification or ()
                    )
                }
                if role_kind == "reviewer"
                else {}
            ),
            "response_contract": (
                EXECUTOR_PROPOSAL_CONTRACT
                if role_kind == "executor"
                else REVIEWER_RESPONSE_CONTRACT
            ),
            "response_contract_rule": (
                "MUST return exactly one JSON object with a required response_contract field equal to "
                f"{EXECUTOR_PROPOSAL_CONTRACT}; MUST NOT return prose, Markdown, or code fences."
                if role_kind == "executor"
                else "MUST return exactly one JSON object with a required response_contract field equal to "
                f"{REVIEWER_RESPONSE_CONTRACT}; MUST NOT return prose, Markdown, or code fences."
            ),
            "required_response_fields": (
                [
                    "response_contract",
                    "proposal_version",
                    "dispatch_id",
                    "attempt",
                    "step_id",
                    "repository",
                    "evidence",
                    "criterion_self_reports",
                    "summary",
                    "outcome",
                ]
                if role_kind == "executor"
                else [
                    "response_contract",
                    "result_version",
                    "dispatch_id",
                    "attempt",
                    "step_id",
                    "repo_id",
                    "review_target",
                    "findings",
                    "verification",
                    "required_remediation",
                    "summary",
                    "verdict",
                ]
            ),
            **(
                {"outcome_options": list(EXECUTOR_PROPOSAL_OUTCOME_OPTIONS)}
                if role_kind == "executor"
                else {"verdict_options": list(REVIEWER_VERDICT_OPTIONS)}
            ),
            "response_json_schema": schema_documents()[response_schema_name],
            "required_verification_check_ids": [
                criterion.criterion_id for criterion in step.acceptance_criteria
            ],
            "verification_contract": (
                "Executor criterion_self_reports MUST contain exactly one not_run entry for every "
                "acceptance criterion in plan order; executor checks are never authoritative."
                if role_kind == "executor"
                else "verification MUST contain exactly one entry for every acceptance criterion; "
                "accepted requires every status to be passed."
            ),
            "final_response_check": (
                "Before sending, verify the JSON object contains every item in required_response_fields, "
                "especially response_contract and attempt. Missing, null, empty, or renamed fields are invalid. "
                f"The outcome field MUST be exactly one of: {', '.join(EXECUTOR_PROPOSAL_OUTCOME_OPTIONS)}; "
                "no other word, synonym, or variation is acceptable. Conform to response_json_schema exactly: "
                "no extra fields, no missing required fields, and no values outside any defined enum. "
                "criterion_self_reports MUST provide exact ordered criterion coverage, and every status "
                "must be not_run. Do not claim final revisions, patch hashes, evidence hashes/sizes, or "
                "Git control fields."
                if role_kind == "executor"
                else "Before sending, verify the JSON object contains every item in required_response_fields, "
                "especially response_contract and attempt. Missing, null, empty, or renamed fields are invalid. "
                f"The verdict field MUST be exactly one of: {', '.join(REVIEWER_VERDICT_OPTIONS)}; "
                "no other word, synonym, or variation is acceptable. Conform to response_json_schema exactly: "
                "no extra fields, no missing required fields, and no values outside any defined enum. "
                "verification MUST provide exact one-to-one criterion_id/check_id coverage with no duplicate, "
                "missing, renamed, or extra IDs; accepted requires every verification status to be passed, "
                "accepted requires required_remediation to be an empty list, while non-success verdicts "
                "still require exact coverage."
            ),
            "acceptance_criteria": [
                criterion.model_dump(mode="json") for criterion in step.acceptance_criteria
            ],
            "evidence_requirements": [
                requirement.model_dump(mode="json") for requirement in step.evidence_requirements
            ],
            "review_target": (
                review_target.model_dump(mode="json") if review_target is not None else None
            ),
            "response_template": _worker_response_template(
                role_kind=role_kind,
                contract=(
                    EXECUTOR_PROPOSAL_CONTRACT
                    if role_kind == "executor"
                    else REVIEWER_RESPONSE_CONTRACT
                ),
                dispatch_id=dispatch_id,
                attempt=attempt,
                step_id=step.step_id,
                repo_id=step.repo_id,
                base_revision=repository.base_revision,
                acceptance_criteria=step.acceptance_criteria,
                evidence_requirements=step.evidence_requirements,
                review_target=review_target,
                requires_attention=False,
            ),
            "response_requires_attention_template": _worker_response_template(
                role_kind=role_kind,
                contract=(
                    EXECUTOR_PROPOSAL_CONTRACT
                    if role_kind == "executor"
                    else REVIEWER_RESPONSE_CONTRACT
                ),
                dispatch_id=dispatch_id,
                attempt=attempt,
                step_id=step.step_id,
                repo_id=step.repo_id,
                base_revision=repository.base_revision,
                acceptance_criteria=step.acceptance_criteria,
                evidence_requirements=step.evidence_requirements,
                review_target=review_target,
                requires_attention=True,
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _source_roots(config: Config) -> dict[str, Path]:
    """Return the normalized configured roots for immutable plan sources."""
    return {
        "plans": Path(config.model.sources.plans_dir).resolve(),
        "specifications": Path(config.model.sources.specifications_dir).resolve(),
    }


def _authoritative_sources(
    sources: tuple[PlanSource, ...],
    source_roots: Mapping[str, Path],
) -> list[dict[str, str]]:
    """Render the approved source ledger without consulting mutable source selections."""
    return [
        {
            "source_id": source.source_id,
            "root": source.root,
            "relative_path": source.relative_path,
            "sha256": source.sha256,
            "path": str((source_roots[source.root] / source.relative_path).resolve()),
        }
        for source in sources
    ]


def _worker_response_template(
    *,
    role_kind: Literal["executor", "reviewer"],
    contract: str,
    dispatch_id: str,
    attempt: int,
    step_id: str,
    repo_id: str,
    base_revision: str,
    acceptance_criteria: Iterable[Any],
    evidence_requirements: Iterable[Any],
    review_target: ReviewTarget | None,
    requires_attention: bool,
) -> dict[str, Any]:
    """Give workers a concrete response shape instead of an undocumented schema name."""
    if role_kind == "executor":
        template = {
            "response_contract": contract,
            "proposal_version": 2,
            "dispatch_id": dispatch_id,
            "attempt": attempt,
            "step_id": step_id,
            "repository": {
                "repo_id": repo_id,
                "base_revision": base_revision,
            },
            "evidence": [
                {
                    "artifact_id": requirement.artifact_id,
                    "relative_path": requirement.relative_path,
                    "media_type": requirement.media_type,
                }
                for requirement in evidence_requirements
            ],
            "criterion_self_reports": [
                {
                    "check_id": criterion.criterion_id,
                    "status": "not_run",
                    "summary": "The dispatcher owns this acceptance check.",
                }
                for criterion in acceptance_criteria
            ],
            "summary": "<non-empty summary>",
            "outcome": "completed",
        }
        if requires_attention:
            template.update(
                {
                    "summary": "The worker cannot complete the authorized task.",
                    "outcome": "blocked",
                    "blockers": ["A concrete blocker prevents completion."],
                }
            )
        return template
    template = {
        "response_contract": contract,
        "result_version": 1,
        "dispatch_id": dispatch_id,
        "attempt": attempt,
        "step_id": step_id,
        "repo_id": repo_id,
        "review_target": review_target.model_dump(mode="json") if review_target is not None else {},
        "findings": [],
        "verification": [
            {
                "check_id": criterion.criterion_id,
                "status": "passed",
                "summary": "The acceptance criterion passed.",
            }
            for criterion in acceptance_criteria
        ],
        "required_remediation": [],
        "summary": "<non-empty summary>",
        "verdict": "accepted",
    }
    if requires_attention:
        template.update(
            {
                "findings": [
                    {
                        "finding_id": "required-change",
                        "severity": "blocking",
                        "summary": "The reviewed result requires a concrete correction.",
                    }
                ],
                "verification": [
                    {
                        "check_id": criterion.criterion_id,
                        "status": "failed",
                        "summary": "The reviewed result did not satisfy a required check.",
                    }
                    for criterion in acceptance_criteria
                ],
                "required_remediation": ["Correct the identified blocking finding."],
                "summary": "The reviewed result requires changes before acceptance.",
                "verdict": "changes_requested",
            }
        )
    return template


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
        raise WorkerResultValidationError(str(exc)) from exc
    if result.repository.base_revision != dispatch.intent.repository.base_revision:
        raise WorkerResultValidationError(
            "executor result base revision does not match prepared dispatch"
        )


def _validate_executor_proposal(
    step: PlanStep,
    proposal: ExecutorProposal,
    dispatch: DispatchRecord,
) -> None:
    expectation = ResultExpectation(
        dispatch_id=dispatch.dispatch_id,
        attempt=dispatch.attempt,
        step_id=dispatch.step_id,
        repo_id=dispatch.intent.repository.repo_id,
        expected_review_target=None,
    )
    try:
        validate_executor_proposal_context(proposal, expectation)
    except ResultError as exc:
        raise WorkerResultValidationError(str(exc)) from exc
    if proposal.repository.base_revision != dispatch.intent.repository.base_revision:
        raise WorkerResultValidationError(
            "executor proposal base revision does not match prepared dispatch"
        )
    expected_ids = [criterion.criterion_id for criterion in step.acceptance_criteria]
    actual_ids = [item.check_id for item in proposal.criterion_self_reports]
    if actual_ids != expected_ids:
        raise WorkerResultValidationError(
            "executor proposal criterion self-reports must exactly match plan order"
        )
    expected_evidence = [
        (item.artifact_id, item.relative_path, item.media_type)
        for item in step.evidence_requirements
    ]
    actual_evidence = [
        (item.artifact_id, item.relative_path, item.media_type)
        for item in proposal.evidence
    ]
    if isinstance(proposal, ExecutorCompletedProposal) and actual_evidence != expected_evidence:
        raise WorkerResultValidationError(
            "completed executor proposal evidence must exactly match plan order"
        )


def _validate_result_verification(
    step: PlanStep,
    result: ExecutorResult | ReviewerResult,
    authoritative_verification: tuple[AuthoritativeVerification, ...] = (),
) -> None:
    """Require exact model coverage and dispatcher-owned success verification."""
    _validate_model_verification(step, result)
    expected_ids = [criterion.criterion_id for criterion in step.acceptance_criteria]
    success = isinstance(result, (ExecutorCompletedResult, ReviewerAcceptedResult))
    problems = []
    if success:
        authoritative_ids = [item.check_id for item in authoritative_verification]
        if authoritative_ids != expected_ids:
            problems.append(
                "authoritative verification IDs do not exactly match plan criteria"
            )
        authoritative_by_id = {
            item.check_id: item for item in authoritative_verification
        }
        failed_authoritative = sorted(
            item.check_id
            for item in authoritative_verification
            if item.status != "passed"
        )
        if failed_authoritative:
            problems.append(
                f"dispatcher-owned checks failed={failed_authoritative}"
            )
        disagreements = sorted(
            verification.check_id
            for verification in result.verification
            if verification.check_id in authoritative_by_id
            and verification.status
            != authoritative_by_id[verification.check_id].status
        )
        if disagreements:
            problems.append(
                f"model verification disagrees with dispatcher checks={disagreements}"
            )
    if problems:
        variant = (
            f"executor {result.outcome}"
            if hasattr(result, "outcome")
            else f"reviewer {result.verdict}"
        )
        raise WorkerResultValidationError(
            f"{variant} result verification is invalid for step {step.step_id}: "
            + "; ".join(problems)
        )


def _validate_model_verification(
    step: PlanStep,
    result: ExecutorResult | ReviewerResult,
) -> None:
    """Validate model-declared criterion coverage without granting it authority."""
    expected_ids = [criterion.criterion_id for criterion in step.acceptance_criteria]
    actual_ids = [verification.check_id for verification in result.verification]
    duplicate_ids = sorted(
        check_id for check_id in set(actual_ids) if actual_ids.count(check_id) > 1
    )
    missing_ids = sorted(set(expected_ids) - set(actual_ids))
    unknown_ids = sorted(set(actual_ids) - set(expected_ids))
    success = isinstance(result, (ExecutorCompletedResult, ReviewerAcceptedResult))
    non_passing = sorted(
        f"{verification.check_id}={verification.status}"
        for verification in result.verification
        if success and verification.status != "passed"
    )
    problems = []
    if duplicate_ids:
        problems.append(f"duplicate check IDs={duplicate_ids}")
    if missing_ids:
        problems.append(f"missing criterion IDs={missing_ids}")
    if unknown_ids:
        problems.append(f"unknown check IDs={unknown_ids}")
    if non_passing:
        problems.append(f"non-passing checks={non_passing}")
    if problems:
        variant = (
            f"executor {result.outcome}"
            if hasattr(result, "outcome")
            else f"reviewer {result.verdict}"
        )
        raise WorkerResultValidationError(
            f"{variant} result verification is invalid for step {step.step_id}: "
            + "; ".join(problems)
        )


def _effective_authoritative_verification(
    config: Config,
    result: ExecutorResult | ReviewerResult,
    authoritative_verification: tuple[AuthoritativeVerification, ...],
) -> tuple[AuthoritativeVerification, ...]:
    """Provide deterministic synthetic authority only in explicit mock mode."""
    success = isinstance(result, (ExecutorCompletedResult, ReviewerAcceptedResult))
    if authoritative_verification or not success:
        return authoritative_verification
    if config.execution.mode != "mock_workflow_test":
        return authoritative_verification
    empty_hash = hashlib.sha256(b"").hexdigest()
    return tuple(
        AuthoritativeVerification(
            check_id=item.check_id,
            status="passed" if item.status == "passed" else "failed",
            argv=("dispatcher-mock-verification", item.check_id),
            exit_code=0 if item.status == "passed" else 1,
            timed_out=False,
            output_truncated=False,
            stdout_sha256=empty_hash,
            stderr_sha256=empty_hash,
            transcript_sha256=hashlib.sha256(
                f"mock:{item.check_id}:{item.status}".encode("utf-8")
            ).hexdigest(),
            duration_ms=0,
            backend="mock-workflow-test",
            summary="synthetic dispatcher verification for explicit mock mode",
        )
        for item in result.verification
    )


def _executor_forwarding(
    result: ExecutorResult,
    usage: UsageAmount,
    authoritative_verification: tuple[AuthoritativeVerification, ...],
) -> str:
    return json.dumps(
        {
            "kind": "executor_result",
            "dispatch_id": result.dispatch_id,
            "outcome": result.outcome,
            "summary": result.summary,
            "evidence": [item.model_dump(mode="json") for item in result.evidence],
            "authoritative_verification": [
                item.model_dump(mode="json") for item in authoritative_verification
            ],
            "usage": usage.model_dump(mode="json"),
        },
        sort_keys=True,
    )


def _reviewer_forwarding(
    result: ReviewerResult,
    usage: UsageAmount,
    authoritative_verification: tuple[AuthoritativeVerification, ...],
) -> str:
    return json.dumps(
        {
            "kind": "reviewer_result",
            "dispatch_id": result.dispatch_id,
            "verdict": result.verdict,
            "authoritative_verification": [
                item.model_dump(mode="json") for item in authoritative_verification
            ],
            "summary": result.summary,
            "review_target": result.review_target.model_dump(mode="json"),
            "usage": usage.model_dump(mode="json"),
        },
        sort_keys=True,
    )


def _verification_failure_detail(
    verification: tuple[AuthoritativeVerification, ...],
) -> str:
    failed = [item for item in verification if item.status != "passed"]
    return redact_text(
        "; ".join(
            f"{item.check_id}(exit={item.exit_code}, timeout={item.timed_out}, "
            f"truncated={item.output_truncated}, backend={item.backend}, "
            f"transcript={item.transcript_sha256}, summary={item.summary})"
            for item in failed
        )
    )[:5000]


def _authoritative_verification_from_payload(
    payload: DispatchPayload,
) -> tuple[AuthoritativeVerification, ...]:
    if not payload.authoritative_verification:
        raise SequentialWorkflowError("verification retry has no durable feedback")
    try:
        return tuple(
            AuthoritativeVerification.model_validate_json(json.dumps(item))
            for item in payload.authoritative_verification
        )
    except ValueError as exc:
        raise SequentialWorkflowError("durable verification feedback is malformed") from exc


def _supports_automatic_verification_rework(dispatch: DispatchRecord) -> bool:
    """Restrict same-session dirty-tree rework to isolated sequential dispatches."""
    return dispatch.batch_id is None and dispatch.workspace_group_id is None


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
