"""Transactional connection between the sequential workflow and OpenCode adapter."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from typing import Any
from uuid import uuid4

from .config import Config
from .mcp import (
    collect_role_mcp_environment,
    compile_role_mcp_servers,
    inherits_global_mcp_config,
)
from .permissions import (
    compile_effective_policy,
    generate_opencode_config,
    role_scoped_authorized_actions,
    should_auto_approve,
)
from .plan import PlanError, verify_plan_sources
from .repository import RepositoryValidationError
from .results import ResultError, parse_executor_proposal, parse_reviewer_result
from .security import redact_text
from .sequential import (
    CompletionDecision,
    PreparedBatch,
    PreparedDispatch,
    SequentialWorkflow,
    SequentialWorkflowError,
    SupervisorCommandRejectedError,
    WorkerResultValidationError,
    _authoritative_sources,
    _source_roots,
    session_registry_identity,
)
from .sessions import (
    OpenCodeAdapterError,
    SessionLifecycleCallbacks,
    SessionResult,
    run_session,
)
from .state_store import StateStore, StateStoreError
from .verification import AuthoritativeVerification, VerificationRunner
from .workflow import (
    DispatchRecord,
    DispatchStatus,
    RunRecord,
    RunStatus,
    StepStatus,
    TransitionError,
)

logger = logging.getLogger(__name__)

_SUPERVISOR_CORRECTION_REASON_LIMIT = 1_000

SessionRunner = Callable[..., SessionResult]
VerificationRunnerFn = Callable[[Any, Path], tuple[AuthoritativeVerification, ...]]


class ExecutionCoordinatorError(RuntimeError):
    """A session completed without satisfying the transactional workflow boundary."""


class ReviewerResponseValidationError(ExecutionCoordinatorError):
    """A finalized read-only reviewer response did not satisfy its typed contract."""


@dataclass(frozen=True)
class WorkerOutcome:
    """One durable worker result ready to be consumed by the supervisor."""

    record: RunRecord
    generation: int
    dispatch_id: str
    forwarding: str


@dataclass(frozen=True)
class BatchOutcome:
    """One joined batch result with every successful forwarding identified."""

    record: RunRecord
    generation: int
    batch_id: str
    forwarding: str
    forwarded_dispatch_ids: tuple[str, ...]


@dataclass(frozen=True)
class SupervisorOutcome:
    """One strict supervisor command returned by a persisted OpenCode session."""

    response: str
    session_id: str
    generation: int


class SequentialExecutionCoordinator:
    """Execute sessions only through durable sequential workflow transitions."""

    def __init__(
        self,
        config: Config,
        store: StateStore,
        workflow: SequentialWorkflow,
        *,
        owner_id: str,
        session_runner: SessionRunner = run_session,
        verification_runner: VerificationRunnerFn | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.workflow = workflow
        self.owner_id = owner_id
        self._session_runner = session_runner
        self._verification_runner = (
            verification_runner or VerificationRunner.from_config(config).run
        )
        self._run_lease_key = f"run:{config.project_id}"
        self._heartbeat_lock = threading.Lock()

    def acquire_run(self, run_id: str) -> None:
        """Acquire the single-writer run lease before bootstrap or resume."""
        self.store.acquire_run_lease(
            project_id=self.config.project_id,
            run_id=run_id,
            owner_id=self.owner_id,
        )

    def release_run(self) -> None:
        """Release the run lease after a terminal or explicitly stopped command."""
        self.store.release_leases(owner_id=self.owner_id, resource_keys=[self._run_lease_key])

    def heartbeat(self) -> None:
        """Refresh the run lease around every external process boundary."""
        with self._heartbeat_lock:
            self.store.heartbeat_leases(owner_id=self.owner_id, resource_keys=[self._run_lease_key])

    def run_supervisor_turn(
        self,
        run_id: str,
        *,
        expected_generation: int,
        prompt: str,
        session_id: str | None,
    ) -> SupervisorOutcome:
        """Run and persist one read-only supervisor turn."""
        self.heartbeat()
        _record, generation_before_launch = self.store.load_run(run_id)
        if generation_before_launch != expected_generation:
            raise ExecutionCoordinatorError("run changed before supervisor session")
        role_key = self.config.supervisor_key
        role = self.config.role(role_key)
        repo_id = self.config.default_repository_id
        authorized_actions = role_scoped_authorized_actions(("inspect",), "supervisor")
        permission = generate_opencode_config(
            compile_effective_policy(
                self.config,
                repo_id=repo_id,
                role_key=role_key,
                dispatch_authorized_actions=authorized_actions,
            ),
            mcp_servers=compile_role_mcp_servers(self.config, role_key),
        )
        session_mode = "resume" if session_id else "new"
        invocation_id = f"supervisor:{run_id}:{expected_generation}:{uuid4().hex}"
        self.store.begin_opencode_invocation(
            invocation_id=invocation_id,
            run_id=run_id,
            dispatch_id=None,
            role_kind="supervisor",
            role_key=role_key,
            step_id=None,
            session_mode=session_mode,
            requested_session_id=session_id,
        )
        try:
            result = self._session_runner(
                prompt=prompt,
                model=role.model,
                variant=role.variant,
                session_id=session_id,
                mode=session_mode,
                workdir=self.config.repository_root(repo_id),
                title=f"supervisor - {run_id}",
                auto_approve=should_auto_approve(permission["permission"]),
                timeout_seconds=self.config.execution.timeout_seconds,
                termination_grace_seconds=self.config.execution.termination_grace_seconds,
                max_output_bytes=self.config.execution.max_output_bytes,
                state_dir=self.config.state_dir,
                permission_config=permission,
                environment_passthrough=collect_role_mcp_environment(self.config, role_key),
                inherit_opencode_config=inherits_global_mcp_config(self.config),
                snapshot_dirs=[],
            )
        except OpenCodeAdapterError as exc:
            self.store.finish_opencode_invocation(
                invocation_id=invocation_id,
                runtime_session_id=exc.runtime_session_id or session_id,
                usage=_adapter_error_usage(exc),
                failure_category=exc.category,
            )
            raise
        except Exception as exc:
            self.store.finish_opencode_invocation(
                invocation_id=invocation_id,
                runtime_session_id=session_id,
                usage=None,
                failure_category=_worker_failure(exc)[0],
            )
            raise
        _record, generation_before_accounting = self.store.load_run(run_id)
        record, generation = self.store.finish_opencode_invocation(
            invocation_id=invocation_id,
            runtime_session_id=result.session_id,
            usage=_session_usage(result),
        )
        if generation_before_accounting != expected_generation:
            raise ExecutionCoordinatorError("run changed during supervisor session")
        sessions = self.store.sessions_for_run(run_id)
        sessions.setdefault("supervisor", {})[role_key] = {
            "session_id": result.session_id,
            "working_directory": str(self.config.repository_root(repo_id)),
            "status": "active",
        }
        generation = self.store.save_run(
            record,
            expected_generation=generation,
            sessions=sessions,
        )
        self.store.write_transcript(
            run_id=run_id,
            dispatch_id=None,
            content=result.chat_response,
            sequence=record.sequence,
            label="supervisor-response",
        )
        self.heartbeat()
        return SupervisorOutcome(result.chat_response, result.session_id, generation)

    def execute_worker(self, prepared: PreparedDispatch) -> WorkerOutcome:
        """Execute one prepared worker with synchronous lifecycle transactions."""
        current = prepared
        try:
            self._validate_persisted_worker_prompt(current)
        except ExecutionCoordinatorError as exc:
            try:
                self.workflow.recover_interrupted_dispatch(
                    current.run_id,
                    current.dispatch.dispatch_id,
                    recovery_reason=str(exc),
                )
            finally:
                self.store.release_leases(
                    owner_id=current.lease_owner_id,
                    resource_keys=current.lease_keys,
                )
            raise
        invocation_id = f"dispatch:{prepared.run_id}:{prepared.dispatch.dispatch_id}"
        invocation_finalized = False

        def process_started(process_id: int, process_create_time: float) -> None:
            nonlocal current
            self.heartbeat()
            current = self.workflow.mark_running(
                current,
                process_id=process_id,
                process_create_time=process_create_time,
            )

        def session_identified(runtime_session_id: str) -> None:
            nonlocal current
            current = self.workflow.record_session_id(
                current,
                runtime_session_id=runtime_session_id,
            )

        role = self.config.role(prepared.dispatch.role_key)
        repository = self.config.repository(prepared.dispatch.intent.repository.repo_id)
        snapshot_dirs = [
            str(prepared.workdir / evidence_root)
            for evidence_root in repository.evidence_roots
        ]
        worker_state_dir = worker_opencode_state_dir(
            self.config.state_dir,
            run_id=prepared.run_id,
            dispatch=prepared.dispatch,
        )
        self.store.begin_opencode_invocation(
            invocation_id=invocation_id,
            run_id=prepared.run_id,
            dispatch_id=prepared.dispatch.dispatch_id,
            role_kind=prepared.dispatch.role_kind,
            role_key=prepared.dispatch.role_key,
            step_id=prepared.dispatch.step_id,
            session_mode=prepared.session_mode,
            requested_session_id=prepared.session_id,
        )

        def finalize_invocation(
            *,
            runtime_session_id: str | None,
            usage: dict[str, object] | None,
            failure_category: str | None = None,
        ) -> None:
            nonlocal current, invocation_finalized
            record, generation = self.workflow.finish_opencode_invocation(
                invocation_id=invocation_id,
                runtime_session_id=runtime_session_id,
                usage=usage,
                failure_category=failure_category,
            )
            current = _refresh_prepared(current, record, generation)
            invocation_finalized = True

        try:
            result = self._session_runner(
                prompt=prepared.prompt,
                model=role.model,
                variant=role.variant,
                session_id=prepared.session_id,
                mode=prepared.session_mode,
                workdir=prepared.workdir,
                title=(
                    f"{prepared.dispatch.role_kind} - {prepared.dispatch.step_id} - "
                    f"attempt {prepared.dispatch.attempt}"
                ),
                auto_approve=prepared.auto_approve,
                timeout_seconds=self.config.execution.timeout_seconds,
                termination_grace_seconds=self.config.execution.termination_grace_seconds,
                max_output_bytes=self.config.execution.max_output_bytes,
                state_dir=worker_state_dir,
                credential_state_dir=self.config.state_dir,
                permission_config=prepared.permission_config,
                environment_passthrough=collect_role_mcp_environment(
                    self.config, prepared.dispatch.role_key
                ),
                inherit_opencode_config=inherits_global_mcp_config(self.config),
                snapshot_dirs=snapshot_dirs,
                lifecycle=SessionLifecycleCallbacks(process_started, session_identified),
            )
            usage = _session_usage(result)
            finalize_invocation(runtime_session_id=result.session_id, usage=usage)
            if current.dispatch.runtime_session_id != result.session_id:
                raise ExecutionCoordinatorError(
                    "adapter result session does not match the durable running dispatch"
                )
            try:
                payload = _worker_json_object(result.chat_response)
            except ExecutionCoordinatorError as exc:
                if current.dispatch.role_kind == "reviewer":
                    raise ReviewerResponseValidationError(str(exc)) from exc
                raise
            if current.dispatch.role_kind == "executor":
                proposal = parse_executor_proposal(payload)
                proposal_snapshot = self.workflow.record_executor_proposal(current, proposal)
                authoritative_verification: tuple[AuthoritativeVerification, ...] = ()
                if proposal.outcome == "completed":
                    authoritative_verification = self._run_authoritative_verification(current)
                    if any(item.status != "passed" for item in authoritative_verification):
                        record, generation = self.workflow.record_executor_verification_failure(
                            current,
                            proposal,
                            authoritative_verification=authoritative_verification,
                            usage=usage,
                            verified_snapshot=proposal_snapshot,
                        )
                        if record.state is not RunStatus.RUNNING:
                            return WorkerOutcome(
                                record,
                                generation,
                                current.dispatch.dispatch_id,
                                "",
                            )
                        if record.steps[current.dispatch.step_id].state is StepStatus.READY:
                            retry = self.workflow.prepare_pending_verification_retry(
                                record,
                                generation,
                            )
                            if isinstance(retry, PreparedDispatch):
                                return self.execute_worker(retry)
                            if isinstance(retry, tuple):
                                waiting, waiting_generation = retry
                                return WorkerOutcome(
                                    waiting,
                                    waiting_generation,
                                    current.dispatch.dispatch_id,
                                    "",
                                )
                        raise WorkerResultValidationError(
                            "dispatcher-owned verification failed: "
                            + ", ".join(
                                item.check_id
                                for item in authoritative_verification
                                if item.status != "passed"
                            )
                        )
                record, generation, forwarding = self.workflow.materialize_executor_proposal(
                    current,
                    proposal,
                    authoritative_verification=authoritative_verification,
                    usage=usage,
                    verified_snapshot=proposal_snapshot,
                )
            else:
                try:
                    parsed_review = parse_reviewer_result(payload)
                except ResultError as exc:
                    raise ReviewerResponseValidationError(str(exc)) from exc
                authoritative_verification = ()
                if parsed_review.verdict == "accepted":
                    review_snapshot = self.workflow.inspect_reviewer_result(
                        current,
                        parsed_review,
                    )
                    authoritative_verification = self._run_authoritative_verification(current)
                    if any(item.status != "passed" for item in authoritative_verification):
                        record, generation = self.workflow.record_reviewer_verification_failure(
                            current,
                            parsed_review,
                            authoritative_verification=authoritative_verification,
                            usage=usage,
                            verified_snapshot=review_snapshot,
                        )
                        if record.state is not RunStatus.RUNNING:
                            return WorkerOutcome(
                                record,
                                generation,
                                current.dispatch.dispatch_id,
                                "",
                            )
                        if record.steps[current.dispatch.step_id].state is StepStatus.READY:
                            retry = self.workflow.prepare_pending_verification_retry(
                                record,
                                generation,
                            )
                            if isinstance(retry, PreparedDispatch):
                                return self.execute_worker(retry)
                            if isinstance(retry, tuple):
                                waiting, waiting_generation = retry
                                return WorkerOutcome(
                                    waiting,
                                    waiting_generation,
                                    current.dispatch.dispatch_id,
                                    "",
                                )
                        raise WorkerResultValidationError(
                            "fresh post-review dispatcher verification failed: "
                            + ", ".join(
                                item.check_id
                                for item in authoritative_verification
                                if item.status != "passed"
                            )
                        )
                record, generation, forwarding = self.workflow.apply_reviewer_result(
                    current,
                    parsed_review,
                    authoritative_verification=authoritative_verification,
                    usage=usage,
                )
        except ReviewerResponseValidationError as exc:
            if not invocation_finalized:
                raise ExecutionCoordinatorError(
                    "reviewer response validation ran before invocation finalization"
                ) from exc
            stored, stored_generation = self.store.load_run(current.run_id)
            stored_dispatch = stored.dispatches[current.dispatch.dispatch_id]
            if stored_dispatch.state.value in {"PREPARED", "RUNNING"}:
                current = _refresh_prepared(current, stored, stored_generation)
                record, generation, retry_allowed = self.workflow.handle_stall(
                    current,
                    category="result_validation",
                    reason=str(exc),
                )
                if retry_allowed:
                    sleep(self.config.execution.stall_policy.cooldown_seconds)
                    retry = self.workflow.prepare_stall_retry(
                        record,
                        generation,
                        current,
                        category="result_validation",
                    )
                    return self.execute_worker(retry)
            raise
        except OpenCodeAdapterError as exc:
            failure_category, failure_detail = _worker_failure(exc)
            if not invocation_finalized:
                finalize_invocation(
                    runtime_session_id=(
                        exc.runtime_session_id or current.dispatch.runtime_session_id
                    ),
                    usage=_adapter_error_usage(exc),
                    failure_category=failure_category,
                )
            stored, stored_generation = self.store.load_run(current.run_id)
            stored_dispatch = stored.dispatches[current.dispatch.dispatch_id]
            if stored_dispatch.state.value in {"PREPARED", "RUNNING"}:
                current = _refresh_prepared(current, stored, stored_generation)
                if exc.category in {"timeout", "interrupted", "connection", "rate_limit", "context_overflow"}:
                    record, generation, retry_allowed = self.workflow.handle_stall(
                        current,
                        category=exc.category,
                        reason=failure_detail,
                    )
                    if retry_allowed:
                        sleep(self.config.execution.stall_policy.cooldown_seconds)
                        retry = self.workflow.prepare_stall_retry(
                            record,
                            generation,
                            current,
                            category=exc.category,
                        )
                        return self.execute_worker(retry)
                else:
                    self.workflow.fail_dispatch(
                        current,
                        reason=_failure_event_reason(failure_category, failure_detail),
                        failure_category=failure_category,
                        failure_detail=failure_detail,
                    )
            raise
        except Exception as exc:
            failure_category, failure_detail = _worker_failure(exc)
            if not invocation_finalized:
                finalize_invocation(
                    runtime_session_id=current.dispatch.runtime_session_id,
                    usage=None,
                    failure_category=failure_category,
                )
            stored, stored_generation = self.store.load_run(current.run_id)
            stored_dispatch = stored.dispatches[current.dispatch.dispatch_id]
            if stored_dispatch.state.value in {"PREPARED", "RUNNING"}:
                current = _refresh_prepared(current, stored, stored_generation)
                self.workflow.fail_dispatch(
                    current,
                    reason=_failure_event_reason(failure_category, failure_detail),
                    failure_category=failure_category,
                    failure_detail=failure_detail,
                )
            raise
        self.heartbeat()
        return WorkerOutcome(record, generation, current.dispatch.dispatch_id, forwarding)

    def _run_authoritative_verification(
        self,
        prepared: PreparedDispatch,
    ) -> tuple[AuthoritativeVerification, ...]:
        record, _generation = self.store.load_run(prepared.run_id)
        step = next(step for step in record.plan.steps if step.step_id == prepared.dispatch.step_id)
        self.heartbeat()
        results = self._verification_runner(step, Path(prepared.workdir))
        self.heartbeat()
        return results

    def _validate_persisted_worker_prompt(self, prepared: PreparedDispatch) -> None:
        """Reject a legacy or altered durable worker context before any OpenCode launch."""
        try:
            record, _generation = self.store.load_run(prepared.run_id)
            dispatch = record.dispatches[prepared.dispatch.dispatch_id]
            stored = self.store.load_dispatch_payload(prepared.run_id, prepared.dispatch.dispatch_id)
        except (KeyError, StateStoreError) as exc:
            raise ExecutionCoordinatorError(
                f"cannot load persisted worker prompt for dispatch {prepared.dispatch.dispatch_id}"
            ) from exc
        try:
            verify_plan_sources(record.plan, self.config)
        except PlanError as exc:
            raise ExecutionCoordinatorError(
                f"immutable plan source verification failed: {exc}"
            ) from exc
        if hashlib.sha256(stored.prompt.encode("utf-8")).hexdigest() != dispatch.intent.prompt_sha256:
            raise ExecutionCoordinatorError(
                "persisted worker prompt does not match its immutable dispatch intent"
            )
        context = _strict_json_object(
            stored.prompt,
            description=f"persisted worker prompt for dispatch {prepared.dispatch.dispatch_id}",
        )
        expected_sources = _authoritative_sources(record.plan.sources, _source_roots(self.config))
        if context.get("authoritative_sources") != expected_sources:
            raise ExecutionCoordinatorError(
                "persisted worker prompt lacks a valid authoritative-source ledger"
            )
        if stored.prompt != prepared.prompt:
            raise ExecutionCoordinatorError(
                "persisted worker prompt does not match the launchable worker context"
            )

    def execute_batch(self, prepared: PreparedBatch) -> BatchOutcome:
        """Run all atomically prepared children within the configured global bound."""
        outcomes: list[WorkerOutcome] = []
        with ThreadPoolExecutor(
            max_workers=min(
                len(prepared.dispatches),
                self.config.execution.concurrency.max_active_dispatches,
            ),
            thread_name_prefix="dispatcher-worker",
        ) as executor:
            futures = [
                (child, executor.submit(self.execute_worker, child))
                for child in prepared.dispatches
            ]
            for child, future in futures:
                try:
                    outcomes.append(future.result())
                except Exception as exc:
                    # The worker boundary persisted its own failed dispatch state before raising.
                    category, detail = _worker_failure(exc)
                    logger.warning(
                        "batch child dispatch %s failed [%s]: %s",
                        child.dispatch.dispatch_id,
                        category,
                        detail,
                        extra={
                            "dispatcher_context": {
                                "project_id": self.config.project_id,
                                "run_id": child.run_id,
                                "dispatch_id": child.dispatch.dispatch_id,
                                "step_id": child.dispatch.step_id,
                            }
                        },
                    )
                    continue
        _record, current_generation = self.store.load_run(prepared.run_id)
        record, generation, forwarding = self.workflow.finalize_batch(
            prepared.run_id,
            expected_generation=current_generation,
            batch_id=prepared.batch_id,
        )
        return BatchOutcome(
            record=record,
            generation=generation,
            batch_id=prepared.batch_id,
            forwarding=forwarding,
            forwarded_dispatch_ids=tuple(outcome.dispatch_id for outcome in outcomes),
        )

    def _continuation_prompt(
        self,
        bootstrap: str,
        record: RunRecord,
    ) -> tuple[str, list[str]]:
        """Build a deterministic replay envelope from durable pending forwardings."""
        pending = sorted(
            (
                dispatch
                for dispatch in record.dispatches.values()
                if dispatch.state is DispatchStatus.FORWARDED
            ),
            key=lambda dispatch: (dispatch.last_event.sequence, dispatch.dispatch_id),
        )
        if not pending:
            return bootstrap, []
        pending_ids = [dispatch.dispatch_id for dispatch in pending]
        if len(pending_ids) != len(set(pending_ids)):
            raise ExecutionCoordinatorError("durable pending forwardings contain duplicate dispatch IDs")

        forwardings: list[dict[str, object]] = []
        for dispatch in pending:
            try:
                stored = self.store.load_dispatch_payload(record.run_id, dispatch.dispatch_id)
            except StateStoreError as exc:
                raise ExecutionCoordinatorError(
                    f"cannot load durable forwarding payload for dispatch {dispatch.dispatch_id}"
                ) from exc
            raw = stored.forwarding_payload
            if raw is None:
                raise ExecutionCoordinatorError(
                    f"durable forwarding payload is missing for dispatch {dispatch.dispatch_id}"
                )
            if not raw.strip():
                raise ExecutionCoordinatorError(
                    f"durable forwarding payload is empty for dispatch {dispatch.dispatch_id}"
                )
            payload = _strict_json_object(
                raw,
                description=f"durable forwarding payload for dispatch {dispatch.dispatch_id}",
            )
            if payload.get("dispatch_id") != dispatch.dispatch_id:
                raise ExecutionCoordinatorError(
                    f"durable forwarding payload identity does not match dispatch {dispatch.dispatch_id}"
                )
            expected_kind = f"{dispatch.role_kind}_result"
            if payload.get("kind") != expected_kind:
                raise ExecutionCoordinatorError(
                    f"durable forwarding payload kind does not match {dispatch.role_kind} "
                    f"dispatch {dispatch.dispatch_id}"
                )
            forwardings.append({"dispatch_id": dispatch.dispatch_id, "payload": payload})

        return (
            json.dumps(
                {
                    "kind": "orchestration_resume",
                    "bootstrap": bootstrap,
                    "pending_forwardings": forwardings,
                },
                sort_keys=True,
            ),
            pending_ids,
        )

    def _persisted_supervisor_session_id(self, run_id: str) -> str | None:
        """Return the configured supervisor's durable session ID when it is usable."""
        entry = self.store.sessions_for_run(run_id).get("supervisor", {}).get(
            self.config.supervisor_key,
            {},
        )
        if not isinstance(entry, dict):
            return None
        session_id = entry.get("session_id")
        return session_id if isinstance(session_id, str) and session_id else None

    def run_to_completion(
        self,
        run_id: str,
        *,
        expected_generation: int,
        max_turns: int = 20,
    ) -> CompletionDecision:
        """Run one bounded sequential fake/live-compatible orchestration loop."""
        self.acquire_run(run_id)
        try:
            record, generation = self.store.load_run(run_id)
            if generation != expected_generation:
                raise ExecutionCoordinatorError("run generation changed before activation")
            reviewer_retry = self.workflow.prepare_pending_reviewer_result_validation_retry(
                record,
                generation,
            )
            if isinstance(reviewer_retry, tuple):
                waiting, waiting_generation = reviewer_retry
                report_path = self.store.export_run_report(
                    run_id,
                    record_override=waiting,
                    generation_override=waiting_generation,
                )
                return CompletionDecision(
                    accepted=False,
                    obligations=(f"run stopped by policy: {waiting.state.value}",),
                    report_path=report_path,
                )
            bootstrap, _path = self.workflow.render_bootstrap(run_id)
            if isinstance(reviewer_retry, PreparedDispatch):
                worker = self.execute_worker(reviewer_retry)
                record = worker.record
                generation = worker.generation
                if record.state is not RunStatus.RUNNING:
                    return CompletionDecision(
                        accepted=False,
                        obligations=(f"run stopped by policy: {record.state.value}",),
                        report_path=self.store.export_run_report(
                            run_id,
                            record_override=record,
                            generation_override=generation,
                        ),
                    )
                supervisor_prompt = worker.forwarding
                pending_acknowledgements = [worker.dispatch_id]
            else:
                record, generation = self.workflow.activate(
                    run_id,
                    expected_generation=expected_generation,
                )
                supervisor_prompt = ""
                pending_acknowledgements = []
            for dispatch_id in sorted(
                (
                    dispatch.dispatch_id
                    for dispatch in record.dispatches.values()
                    if dispatch.state is DispatchStatus.COMPLETED
                )
            ):
                record, generation, _forwarding = self.workflow.recover_completed_dispatch(
                    run_id,
                    dispatch_id,
                )
            if not pending_acknowledgements:
                verification_retry = self.workflow.prepare_pending_verification_retry(
                    record,
                    generation,
                )
                if isinstance(verification_retry, tuple):
                    waiting, waiting_generation = verification_retry
                    report_path = self.store.export_run_report(
                        run_id,
                        record_override=waiting,
                        generation_override=waiting_generation,
                    )
                    return CompletionDecision(
                        accepted=False,
                        obligations=(f"run stopped by policy: {waiting.state.value}",),
                        report_path=report_path,
                    )
                if isinstance(verification_retry, PreparedDispatch):
                    worker = self.execute_worker(verification_retry)
                    record = worker.record
                    generation = worker.generation
                    if record.state is not RunStatus.RUNNING:
                        return CompletionDecision(
                            accepted=False,
                            obligations=(f"run stopped by policy: {record.state.value}",),
                            report_path=self.store.export_run_report(
                                run_id,
                                record_override=record,
                                generation_override=generation,
                            ),
                        )
                    supervisor_prompt = worker.forwarding
                    pending_acknowledgements = [worker.dispatch_id]
                else:
                    supervisor_prompt, pending_acknowledgements = self._continuation_prompt(
                        bootstrap,
                        record,
                    )
            supervisor_session_id = self._persisted_supervisor_session_id(run_id)
            for _turn in range(max_turns):
                supervisor = self.run_supervisor_turn(
                    run_id,
                    expected_generation=generation,
                    prompt=supervisor_prompt,
                    session_id=supervisor_session_id,
                )
                supervisor_session_id = supervisor.session_id
                generation = supervisor.generation
                if pending_acknowledgements:
                    for dispatch_id in pending_acknowledgements:
                        record, generation = self.workflow.acknowledge_forwarding(
                            run_id,
                            expected_generation=generation,
                            dispatch_id=dispatch_id,
                        )
                    pending_acknowledgements = []
                    record, generation = self.workflow.refresh_readiness(record, generation)
                try:
                    action = self.workflow.prepare_from_supervisor(
                        run_id,
                        expected_generation=generation,
                        supervisor_text=supervisor.response,
                    )
                except SupervisorCommandRejectedError as exc:
                    # This boundary guarantees the command did not persist work or acquire worker leases.
                    supervisor_prompt = _supervisor_correction_envelope(exc)
                    continue
                if isinstance(action, PreparedDispatch):
                    worker = self.execute_worker(action)
                    generation = worker.generation
                    if worker.record.state is not RunStatus.RUNNING:
                        return CompletionDecision(
                            accepted=False,
                            obligations=(f"run stopped by policy: {worker.record.state.value}",),
                            report_path=self.store.export_run_report(
                                run_id,
                                record_override=worker.record,
                                generation_override=worker.generation,
                            ),
                        )
                    supervisor_prompt = worker.forwarding
                    pending_acknowledgements = [worker.dispatch_id]
                    continue
                if isinstance(action, PreparedBatch):
                    batch = self.execute_batch(action)
                    generation = batch.generation
                    if batch.record.state is not RunStatus.RUNNING:
                        return CompletionDecision(
                            accepted=False,
                            obligations=(f"run stopped by batch policy: {batch.record.state.value}",),
                            report_path=self.store.export_run_report(
                                run_id,
                                record_override=batch.record,
                                generation_override=batch.generation,
                            ),
                        )
                    supervisor_prompt = batch.forwarding
                    pending_acknowledgements = list(batch.forwarded_dispatch_ids)
                    continue
                if isinstance(action, CompletionDecision):
                    if not action.accepted:
                        record, generation = self.store.load_run(run_id)
                        if any(
                            step.state is StepStatus.FAILED
                            for step in record.steps.values()
                        ):
                            return action
                        supervisor_prompt = json.dumps(
                            {
                                "kind": "completion_denied",
                                "obligations": list(action.obligations),
                            },
                            sort_keys=True,
                        )
                        continue
                    return action
                raise ExecutionCoordinatorError(
                    f"orchestration stopped in run state {action.state.value}"
                )
            raise ExecutionCoordinatorError(f"sequential run exceeded {max_turns} supervisor turns")
        finally:
            self.release_run()


def _supervisor_correction_envelope(error: SupervisorCommandRejectedError) -> str:
    """Return the bounded deterministic repair prompt for a side-effect-free command rejection."""
    reason = redact_text(str(error)).strip()[:_SUPERVISOR_CORRECTION_REASON_LIMIT]
    if not reason:
        reason = type(error).__name__
    return json.dumps(
        {
            "instruction": (
                "Inspect current durable state and select one valid next action. "
                "Reply with exactly one supervisor command JSON object."
            ),
            "kind": "supervisor_command_rejected",
            "previous_command_status": "rejected",
            "reason": reason,
        },
        sort_keys=True,
    )


def _refresh_prepared(
    current: PreparedDispatch,
    stored: RunRecord,
    generation: int,
) -> PreparedDispatch:
    """Refresh a prepared dispatch from the authoritative record after adapter failure."""
    stored_dispatch = stored.dispatches[current.dispatch.dispatch_id]
    return PreparedDispatch(
        run_id=current.run_id,
        generation=generation,
        dispatch=stored_dispatch,
        prompt=current.prompt,
        workdir=current.workdir,
        permission_config=current.permission_config,
        auto_approve=current.auto_approve,
        lease_keys=current.lease_keys,
        lease_owner_id=current.lease_owner_id,
        review_target=current.review_target,
        session_mode=current.session_mode,
        session_id=current.session_id,
        repository_before=current.repository_before,
    )


def worker_opencode_state_dir(
    state_dir: str | Path,
    *,
    run_id: str,
    dispatch: DispatchRecord,
) -> Path:
    """Return the private OpenCode state root owned by one durable session identity."""
    pool, session_registry_key = session_registry_identity(dispatch)
    return Path(state_dir) / "opencode-dispatches" / run_id / pool / session_registry_key


def _strict_json_object(
    text: str,
    *,
    description: str = "worker response",
) -> dict[str, Any]:
    stripped = text.strip()
    try:
        value = json.loads(
            stripped,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExecutionCoordinatorError(
            f"{description} is not one strict JSON object: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ExecutionCoordinatorError(f"{description} must be a JSON object")
    return value


def _worker_json_object(
    text: str,
    *,
    description: str = "worker response",
) -> dict[str, Any]:
    """Extract one final JSON object from a live worker's final text event."""
    stripped = text.strip()
    if not stripped:
        raise ExecutionCoordinatorError(f"{description} does not contain a final JSON object")
    if stripped.endswith("```"):
        if stripped.count("```") != 2:
            raise ExecutionCoordinatorError(
                f"{description} must contain at most one final JSON Markdown fence"
            )
        opening = stripped.rfind("```json", 0, -3)
        if opening < 0:
            raise ExecutionCoordinatorError(
                f"{description} must contain at most one final JSON Markdown fence"
            )
        fenced = stripped[opening + len("```json") : -3].strip()
        return _strict_json_object(fenced, description=description)
    if "```" in stripped:
        raise ExecutionCoordinatorError(
            f"{description} has a malformed or non-final JSON Markdown fence"
        )

    decoder = json.JSONDecoder(
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_finite_constant,
    )
    decoded: list[tuple[int, int, Any]] = []
    for start, character in enumerate(stripped):
        if character != "{":
            continue
        try:
            value, length = decoder.raw_decode(stripped[start:])
        except (json.JSONDecodeError, ValueError):
            continue
        decoded.append((start, start + length, value))

    final = [item for item in decoded if not stripped[item[1] :].strip()]
    if len(final) != 1:
        raise ExecutionCoordinatorError(
            f"{description} does not contain exactly one final JSON object"
        )
    start, end, value = final[0]
    if not isinstance(value, dict):
        raise ExecutionCoordinatorError(f"{description} final JSON value must be an object")
    if any(
        other_start < start or other_end > end
        for other_start, other_end, _other_value in decoded
        if (other_start, other_end) != (start, end)
    ):
        raise ExecutionCoordinatorError(
            f"{description} contains prose with another JSON object"
        )
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _session_usage(result: SessionResult) -> dict[str, object] | None:
    if result.cost is None:
        return None
    tokens = result.usage
    usage = {
        "cost_usd": result.cost,
        "tokens_total": tokens.get("total"),
        "tokens_input": tokens.get("input"),
        "tokens_output": tokens.get("output"),
        "tokens_reasoning": tokens.get("reasoning"),
    }
    return usage if _complete_usage(usage) else None


def _adapter_error_usage(error: OpenCodeAdapterError) -> dict[str, object] | None:
    if error.cost is None or error.usage is None:
        return None
    usage = {
        "cost_usd": error.cost,
        "tokens_total": error.usage.get("total"),
        "tokens_input": error.usage.get("input"),
        "tokens_output": error.usage.get("output"),
        "tokens_reasoning": error.usage.get("reasoning"),
    }
    return usage if _complete_usage(usage) else None


def _complete_usage(usage: dict[str, object]) -> bool:
    def complete_token(key: str) -> bool:
        value = usage[key]
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    return all(
        complete_token(key)
        for key in ("tokens_total", "tokens_input", "tokens_output", "tokens_reasoning")
    )


def _worker_failure(exc: BaseException) -> tuple[str, str]:
    """Return one stable category and bounded redacted worker-boundary detail."""
    chain = _exception_chain(exc)
    category: str
    if isinstance(exc, OpenCodeAdapterError):
        category = exc.category
    elif any(isinstance(item, (ExecutionCoordinatorError, ResultError, WorkerResultValidationError)) for item in chain):
        category = "result_validation"
    elif any(isinstance(item, RepositoryValidationError) for item in chain):
        category = "repository_validation"
    elif any(
        isinstance(item, (SequentialWorkflowError, StateStoreError, TransitionError))
        for item in chain
    ):
        category = "workflow_validation"
    else:
        category = "internal"
    detail = redact_text(str(exc))[:5000]
    if not detail:
        detail = type(exc).__name__
    return category, detail


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _failure_event_reason(category: str, detail: str) -> str:
    prefix = f"worker execution failed [{category}]: "
    return (prefix + detail)[:5000]
