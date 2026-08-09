"""Transactional connection between the sequential workflow and OpenCode adapter."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .permissions import compile_effective_policy, generate_opencode_config, should_auto_approve
from .results import parse_executor_result, parse_reviewer_result
from .sequential import CompletionDecision, PreparedBatch, PreparedDispatch, SequentialWorkflow
from .sessions import (
    SessionLifecycleCallbacks,
    SessionResult,
    run_session,
)
from .state_store import StateStore
from .workflow import RunRecord, RunStatus

SessionRunner = Callable[..., SessionResult]


class ExecutionCoordinatorError(RuntimeError):
    """A session completed without satisfying the transactional workflow boundary."""


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
    ) -> None:
        self.config = config
        self.store = store
        self.workflow = workflow
        self.owner_id = owner_id
        self._session_runner = session_runner
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
        role_key = next(iter(self.config.model.roles.supervisor))
        role = self.config.role(role_key)
        repo_id = self.config.default_repository_id
        permission = generate_opencode_config(
            compile_effective_policy(
                self.config,
                repo_id=repo_id,
                role_key=role_key,
                dispatch_authorized_actions=["inspect"],
            )
        )
        result = self._session_runner(
            prompt=prompt,
            model=role.model,
            variant=role.variant,
            session_id=session_id,
            mode="resume" if session_id else "new",
            workdir=self.config.repository_root(repo_id),
            title=f"supervisor - {run_id}",
            auto_approve=should_auto_approve(permission["permission"]),
            timeout_seconds=self.config.execution.timeout_seconds,
            termination_grace_seconds=self.config.execution.termination_grace_seconds,
            max_output_bytes=self.config.execution.max_output_bytes,
            state_dir=self.config.state_dir,
            permission_config=permission,
            snapshot_dirs=[],
        )
        record, generation = self.store.load_run(run_id)
        if generation != expected_generation:
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

        def process_started(process_id: int) -> None:
            nonlocal current
            self.heartbeat()
            current = self.workflow.mark_running(current, process_id=process_id)

        def session_identified(runtime_session_id: str) -> None:
            nonlocal current
            current = self.workflow.record_session_id(
                current,
                runtime_session_id=runtime_session_id,
            )

        role = self.config.role(prepared.dispatch.role_key)
        repository = self.config.repository(prepared.dispatch.intent.repository.repo_id)
        snapshot_dirs = [
            str(Path(repository.root) / evidence_root)
            for evidence_root in repository.evidence_roots
        ]
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
                state_dir=self.config.state_dir,
                permission_config=prepared.permission_config,
                snapshot_dirs=snapshot_dirs,
                lifecycle=SessionLifecycleCallbacks(process_started, session_identified),
            )
            if current.dispatch.runtime_session_id != result.session_id:
                raise ExecutionCoordinatorError(
                    "adapter result session does not match the durable running dispatch"
                )
            payload = _strict_json_object(result.chat_response)
            usage = _session_usage(result)
            if current.dispatch.role_kind == "executor":
                record, generation, forwarding = self.workflow.apply_executor_result(
                    current,
                    parse_executor_result(payload),
                    usage=usage,
                )
            else:
                record, generation, forwarding = self.workflow.apply_reviewer_result(
                    current,
                    parse_reviewer_result(payload),
                    usage=usage,
                )
        except Exception as exc:
            stored, stored_generation = self.store.load_run(current.run_id)
            stored_dispatch = stored.dispatches[current.dispatch.dispatch_id]
            if stored_dispatch.state.value in {"PREPARED", "RUNNING"}:
                current = PreparedDispatch(
                    run_id=current.run_id,
                    generation=stored_generation,
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
                self.workflow.fail_dispatch(
                    current,
                    reason=f"worker execution failed: {type(exc).__name__}",
                )
            raise
        self.heartbeat()
        return WorkerOutcome(record, generation, current.dispatch.dispatch_id, forwarding)

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
            futures = [executor.submit(self.execute_worker, child) for child in prepared.dispatches]
            for future in futures:
                try:
                    outcomes.append(future.result())
                except Exception:
                    # The worker boundary persisted its own failed dispatch state before raising.
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
            bootstrap, _path = self.workflow.render_bootstrap(run_id)
            record, generation = self.workflow.activate(
                run_id,
                expected_generation=expected_generation,
            )
            supervisor_session_id: str | None = None
            supervisor_prompt = bootstrap
            pending_acknowledgements: list[str] = []
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
                action = self.workflow.prepare_from_supervisor(
                    run_id,
                    expected_generation=generation,
                    supervisor_text=supervisor.response,
                )
                if isinstance(action, PreparedDispatch):
                    worker = self.execute_worker(action)
                    generation = worker.generation
                    if worker.record.state is not RunStatus.RUNNING:
                        return CompletionDecision(
                            accepted=False,
                            obligations=(f"run stopped by policy: {worker.record.state.value}",),
                            report_path=None,
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
                            report_path=None,
                        )
                    supervisor_prompt = batch.forwarding
                    pending_acknowledgements = list(batch.forwarded_dispatch_ids)
                    continue
                if isinstance(action, CompletionDecision):
                    if not action.accepted:
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


def _strict_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    try:
        value = json.loads(stripped, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExecutionCoordinatorError(f"worker response is not one strict JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ExecutionCoordinatorError("worker response must be a JSON object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _session_usage(result: SessionResult) -> dict[str, object] | None:
    if result.cost is None:
        return None
    tokens = result.usage
    return {
        "cost_usd": result.cost,
        "tokens_total": tokens.get("total"),
        "tokens_input": tokens.get("input"),
        "tokens_output": tokens.get("output"),
        "tokens_reasoning": tokens.get("reasoning"),
    }
