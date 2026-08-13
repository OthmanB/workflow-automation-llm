"""Authoritative SQLite state, leases, artifacts, and recovery classification."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from .security import (
    OWNER_FILE_MODE,
    atomic_write_private_text,
    ensure_private_directory,
    redact_text,
    redact_value,
    write_private_text_exclusive,
)
from .workflow import (
    BatchStatus,
    DispatchRecord,
    DispatchStatus,
    RunRecord,
    RunStatus,
    StepStatus,
    TransitionEvent,
    WorkspaceGroup,
    WorkspaceGroupStatus,
    transition_run,
    transition_step,
)
from .workflow import transition_dispatch as transition_dispatch_record

CURRENT_SCHEMA_VERSION = 7
TERMINAL_RUN_STATES = frozenset(
    {RunStatus.HALTED.value, RunStatus.FAILED.value, RunStatus.SUCCEEDED.value, RunStatus.CANCELLED.value}
)
logger = logging.getLogger("dispatcher.state_store")


class StateStoreError(RuntimeError):
    """The authoritative store cannot safely complete a requested operation."""


class StateStoreMigrationError(StateStoreError):
    """The database schema cannot be migrated safely."""


class StateStoreCorruptionError(StateStoreError):
    """SQLite data is corrupt or does not match the runtime state contract."""


class StateStoreConflictError(StateStoreError):
    """A write used a stale generation or duplicate immutable identity."""


class LeaseConflictError(StateStoreError):
    """An active owner already holds one or more required locks."""


class StaleLeaseRecoveryRequired(StateStoreError):
    """A stale lease needs an explicit operator approval before replacement."""


class RecoveryRequiredError(StateStoreError):
    """A requested action would repeat an unresolved external side effect."""


@dataclass(frozen=True)
class Lease:
    """One durable run or repository resource lease."""

    resource_key: str
    owner_id: str
    owner_pid: int
    owner_host: str
    run_id: str
    acquired_at: datetime
    heartbeat_at: datetime


@dataclass(frozen=True)
class DispatchPayload:
    """Private durable payloads required to recover one dispatch attempt."""

    prompt: str
    policy: Mapping[str, Any]
    result: Mapping[str, Any] | None = None
    authoritative_verification: tuple[Mapping[str, Any], ...] | None = None
    forwarding_payload: str | None = None
    process_id: int | None = None
    session_metadata: Mapping[str, Any] | None = None
    repository_before: Mapping[str, Any] | None = None
    repository_after: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RecoveryItem:
    """A deterministic disposition for an unresolved durable dispatch."""

    dispatch_id: str
    state: DispatchStatus
    disposition: Literal[
        "safe_to_retry",
        "structured_commit_adoption_required",
        "operator_reconciliation_required",
        "forwarding_required",
        "acknowledgement_required",
    ]
    detail: str


@dataclass(frozen=True)
class WorkspaceRecoveryItem:
    """A durable cleanup or reconciliation disposition for one workspace group."""

    workspace_group_id: str
    state: WorkspaceGroupStatus
    disposition: Literal["cleanup_required", "operator_reconciliation_required"]
    detail: str


@dataclass(frozen=True)
class StructuredGitRecord:
    """Durable proposal and dispatcher Git lifecycle for one executor attempt."""

    run_id: str
    dispatch_id: str
    state: Literal[
        "PROPOSAL_RECEIVED",
        "CHECKED",
        "COMMIT_INTENT_PERSISTED",
        "STAGED",
        "COMMITTED",
        "NO_COMMIT_FINALIZED",
        "RECONCILIATION_REQUIRED",
    ]
    proposal: Mapping[str, Any]
    proposal_digest: str
    checked: Mapping[str, Any] | None
    intent: Mapping[str, Any] | None
    stage: Mapping[str, Any] | None
    commit: Mapping[str, Any] | None
    result_revision: str | None
    repository_after: Mapping[str, Any] | None
    created_at: datetime
    updated_at: datetime


class StateStore:
    """SQLite authority for one configured dispatcher state directory.

    The database is intentionally private to the local owner. WAL and FULL
    synchronous mode require a local filesystem with atomic rename, fsync, and
    POSIX-style advisory locking semantics; network filesystems are unsupported.
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        heartbeat_seconds: int,
        stale_after_seconds: int,
    ) -> None:
        if heartbeat_seconds < 1:
            raise ValueError("heartbeat_seconds must be positive")
        if stale_after_seconds <= heartbeat_seconds:
            raise ValueError("stale_after_seconds must exceed heartbeat_seconds")
        self.state_dir = ensure_private_directory(state_dir)
        self.database_path = self.state_dir / "dispatcher.sqlite3"
        self.heartbeat_seconds = heartbeat_seconds
        self.stale_after_seconds = stale_after_seconds
        self._connection: sqlite3.Connection | None = None
        self._connection_lock = threading.RLock()
        self._initialized = False

    def __enter__(self) -> StateStore:
        self.initialize()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def initialize(self) -> None:
        """Open, harden, and migrate the authoritative database."""
        connection = self._connect()
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            self._migrate(connection)
            self._secure_database_files()
            self._initialized = True
        except sqlite3.DatabaseError as exc:
            self.close()
            raise StateStoreCorruptionError(
                f"state database is unreadable at {self.database_path}; restore from a verified backup: {exc}"
            ) from exc

    def close(self) -> None:
        """Close the SQLite handle after checkpointing WAL data."""
        if self._connection is not None:
            try:
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:
                pass
            self._connection.close()
            self._connection = None
        self._initialized = False
        self._secure_database_files()

    def create_run(
        self,
        record: RunRecord,
        *,
        sessions: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    ) -> int:
        """Persist the initial immutable run and all child rows atomically."""
        connection = self._ready_connection()
        with self._transaction(connection):
            active = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE project_id = ? AND state NOT IN ('HALTED', 'FAILED', 'SUCCEEDED', 'CANCELLED')
                """,
                (record.project_id,),
            ).fetchone()
            if active is not None:
                raise StateStoreConflictError(
                    f"project {record.project_id} already has active run {active['run_id']}; "
                    "archive or terminate it before starting a new run"
                )
            existing = connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (record.run_id,)
            ).fetchone()
            if existing is not None:
                raise StateStoreConflictError(f"run already exists: {record.run_id}")
            self._write_snapshot(connection, record, generation=1, sessions=sessions or {})
        self._secure_database_files()
        return 1

    def resume_run(self, *, project_id: str, run_id: str) -> tuple[RunRecord, int]:
        """Return one resumable non-terminal run without creating any session or state."""
        record, generation = self.load_run(run_id)
        if record.project_id != project_id:
            raise StateStoreError("run does not belong to the selected project")
        if record.state.value in TERMINAL_RUN_STATES:
            raise StateStoreError("terminal runs require archive or explicit new-run handling")
        active = self.active_run(project_id)
        if active is None or active[0].run_id != run_id:
            raise StateStoreCorruptionError(
                "selected resumable run is not the project's sole active run; operator reconciliation is required"
            )
        return record, generation

    def save_run(
        self,
        record: RunRecord,
        *,
        expected_generation: int,
        sessions: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
        dispatch_payloads: Mapping[str, DispatchPayload] | None = None,
        structured_git_updates: Mapping[str, Mapping[str, Any]] | None = None,
        fault_hook: Callable[[], None] | None = None,
    ) -> int:
        """Atomically commit a complete run generation and optional dispatch payloads."""
        current_sessions = self.sessions_for_run(record.run_id) if sessions is None else sessions
        connection = self._ready_connection()
        with self._transaction(connection):
            row = connection.execute(
                "SELECT generation FROM runs WHERE run_id = ?", (record.run_id,)
            ).fetchone()
            if row is None:
                raise StateStoreConflictError(f"run does not exist: {record.run_id}")
            if row["generation"] != expected_generation:
                raise StateStoreConflictError(
                    f"run {record.run_id} generation conflict: expected {expected_generation}, "
                    f"found {row['generation']}"
                )
            generation = expected_generation + 1
            self._write_snapshot(connection, record, generation=generation, sessions=current_sessions)
            for dispatch_id, payload in (dispatch_payloads or {}).items():
                self._write_dispatch_payload(connection, record.run_id, dispatch_id, payload)
            for dispatch_id, update in (structured_git_updates or {}).items():
                self._write_structured_git_final(
                    connection,
                    run_id=record.run_id,
                    dispatch_id=dispatch_id,
                    update=update,
                )
            if fault_hook is not None:
                fault_hook()
        self._secure_database_files()
        logger.info(
            "run generation committed",
            extra={
                "dispatcher_context": {
                    "project_id": record.project_id,
                    "run_id": record.run_id,
                    "dispatch_id": None,
                    "step_id": None,
                }
            },
        )
        return generation

    def load_run(self, run_id: str) -> tuple[RunRecord, int]:
        """Load one authoritative run record and its generation."""
        with self._connection_lock:
            row = self._ready_connection().execute(
                "SELECT record_json, generation FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise StateStoreError(f"run not found: {run_id}")
            try:
                return RunRecord.model_validate_json(row["record_json"]), int(row["generation"])
            except ValueError as exc:
                raise StateStoreCorruptionError(
                    f"stored run {run_id} does not match the current workflow schema; recover from backup"
                ) from exc

    def latest_run(self, project_id: str) -> tuple[RunRecord, int] | None:
        """Load the most recently updated run for one project."""
        row = self._ready_connection().execute(
            "SELECT run_id FROM runs WHERE project_id = ? ORDER BY updated_at DESC, run_id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return None if row is None else self.load_run(str(row["run_id"]))

    def active_run(self, project_id: str) -> tuple[RunRecord, int] | None:
        """Return the only non-terminal run for a project or fail closed on ambiguity."""
        placeholders = ",".join("?" for _ in TERMINAL_RUN_STATES)
        rows = self._ready_connection().execute(
            f"SELECT run_id FROM runs WHERE project_id = ? AND state NOT IN ({placeholders}) "
            "ORDER BY updated_at DESC, run_id DESC",
            (project_id, *sorted(TERMINAL_RUN_STATES)),
        ).fetchall()
        if len(rows) > 1:
            raise StateStoreCorruptionError(
                f"project {project_id} has multiple active runs; operator reconciliation is required"
            )
        return None if not rows else self.load_run(str(rows[0]["run_id"]))

    def sessions_for_run(self, run_id: str) -> dict[str, dict[str, dict[str, Any]]]:
        """Load the dispatcher-owned session registry for one run."""
        sessions: dict[str, dict[str, dict[str, Any]]] = {}
        with self._connection_lock:
            rows = self._ready_connection().execute(
                "SELECT pool, role_key, session_json FROM sessions WHERE run_id = ? ORDER BY pool, role_key",
                (run_id,),
            ).fetchall()
            for row in rows:
                sessions.setdefault(str(row["pool"]), {})[str(row["role_key"])] = json.loads(
                    row["session_json"]
                )
        return sessions

    def request_dispatch_cancellation(
        self,
        *,
        run_id: str,
        expected_generation: int,
        dispatch_id: str,
        actor_id: str,
    ) -> tuple[RunRecord, int, int, str, float]:
        """Record cancellation intent before a separate process may be signalled."""
        record, generation = self.load_run(run_id)
        if generation != expected_generation:
            raise StateStoreConflictError("run changed before cancellation request")
        dispatch = record.dispatches.get(dispatch_id)
        if dispatch is None:
            raise StateStoreError(f"dispatch not found: {dispatch_id}")
        if dispatch.state is not DispatchStatus.RUNNING or dispatch.process_id is None:
            raise StateStoreError("cancellation requires a running dispatch with a recorded process")
        if dispatch.cancel_requested:
            raise StateStoreError("dispatch cancellation was already requested")
        if dispatch.process_host is None:
            raise StateStoreCorruptionError("running dispatch has no recorded process host")
        if dispatch.process_create_time is None:
            raise StateStoreCorruptionError("running dispatch has no recorded OS process creation time")
        event = TransitionEvent(
            event_id=f"event-{uuid.uuid4().hex}",
            sequence=record.sequence + 1,
            actor="operator",
            reason="operator requested dispatch cancellation before signalling process",
            correlation_id=dispatch_id,
            occurred_at=datetime.now(UTC),
        )
        updated_dispatch = dispatch.model_copy(
            update={"cancel_requested": True, "cancel_requested_at": event.occurred_at, "last_event": event}
        )
        dispatches = dict(record.dispatches)
        dispatches[dispatch_id] = updated_dispatch
        updated = record.model_copy(
            update={"dispatches": dispatches, "sequence": event.sequence, "updated_at": event.occurred_at}
        )
        next_generation = self.save_run(updated, expected_generation=generation)
        self.append_audit_event(
            run_id=run_id,
            event_id=f"audit-{event.event_id}",
            sequence=event.sequence,
            kind="dispatch_cancellation_requested",
            correlation_id=dispatch_id,
            causation_id=None,
            payload={"actor_id": actor_id, "process_host": dispatch.process_host},
        )
        return (
            updated,
            next_generation,
            dispatch.process_id,
            dispatch.process_host,
            dispatch.process_create_time,
        )

    def leases_for_run(self, run_id: str) -> tuple[Lease, ...]:
        """Return every durable lease currently associated with one run."""
        with self._connection_lock:
            rows = self._ready_connection().execute(
                "SELECT * FROM leases WHERE run_id = ? ORDER BY resource_key",
                (run_id,),
            ).fetchall()
        return tuple(self._lease_from_row(row) for row in rows)

    def acquire_run_lease(
        self,
        *,
        project_id: str,
        run_id: str,
        owner_id: str,
        recovery_approved_by: str | None = None,
    ) -> list[Lease]:
        """Acquire the single-writer lease for one project run."""
        return self.acquire_resource_leases(
            run_id=run_id,
            owner_id=owner_id,
            resource_keys=[f"run:{project_id}"],
            recovery_approved_by=recovery_approved_by,
        )

    def acquire_resource_leases(
        self,
        *,
        run_id: str,
        owner_id: str,
        resource_keys: Iterable[str],
        recovery_approved_by: str | None = None,
    ) -> list[Lease]:
        """Atomically acquire normalized repository and resource locks for one owner."""
        keys = tuple(sorted(set(resource_keys)))
        if not keys:
            raise ValueError("at least one resource key is required")
        now = _utc_now()
        connection = self._ready_connection()
        leases: list[Lease] = []
        with self._transaction(connection):
            for key in keys:
                row = connection.execute(
                    "SELECT * FROM leases WHERE resource_key = ?", (key,)
                ).fetchone()
                if row is not None and row["owner_id"] != owner_id:
                    if not self._lease_is_stale(str(row["heartbeat_at"])):
                        raise LeaseConflictError(
                            f"resource {key} is held by {row['owner_id']} on {row['owner_host']}"
                        )
                    if not recovery_approved_by:
                        raise StaleLeaseRecoveryRequired(
                            f"resource {key} has a stale lease from {row['owner_id']}; "
                            "explicit operator approval is required"
                        )
                connection.execute(
                    """
                    INSERT INTO leases(
                        resource_key, owner_id, owner_pid, owner_host, run_id,
                        acquired_at, heartbeat_at, recovery_approved_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(resource_key) DO UPDATE SET
                        owner_id = excluded.owner_id,
                        owner_pid = excluded.owner_pid,
                        owner_host = excluded.owner_host,
                        run_id = excluded.run_id,
                        acquired_at = excluded.acquired_at,
                        heartbeat_at = excluded.heartbeat_at,
                        recovery_approved_by = excluded.recovery_approved_by
                    """,
                    (
                        key,
                        owner_id,
                        os.getpid(),
                        socket.gethostname(),
                        run_id,
                        now,
                        now,
                        recovery_approved_by,
                    ),
                )
                leases.append(self._lease_from_row(connection.execute(
                    "SELECT * FROM leases WHERE resource_key = ?", (key,)
                ).fetchone()))
        self._secure_database_files()
        return leases

    def heartbeat_leases(self, *, owner_id: str, resource_keys: Iterable[str]) -> None:
        """Refresh leases only when the caller still owns every requested resource."""
        keys = tuple(sorted(set(resource_keys)))
        if not keys:
            raise ValueError("at least one resource key is required")
        connection = self._ready_connection()
        with self._transaction(connection):
            for key in keys:
                cursor = connection.execute(
                    "UPDATE leases SET heartbeat_at = ? WHERE resource_key = ? AND owner_id = ?",
                    (_utc_now(), key, owner_id),
                )
                if cursor.rowcount != 1:
                    raise LeaseConflictError(f"cannot heartbeat unowned resource: {key}")

    def release_leases(self, *, owner_id: str, resource_keys: Iterable[str]) -> None:
        """Release only leases still owned by the caller."""
        keys = tuple(sorted(set(resource_keys)))
        if not keys:
            return
        connection = self._ready_connection()
        with self._transaction(connection):
            for key in keys:
                connection.execute(
                    "DELETE FROM leases WHERE resource_key = ? AND owner_id = ?", (key, owner_id)
                )

    def prepare_dispatch(
        self,
        record: RunRecord,
        *,
        expected_generation: int,
        dispatch: DispatchRecord,
        prompt: str,
        policy: Mapping[str, Any],
        repository_before: Mapping[str, Any],
        session_metadata: Mapping[str, Any] | None = None,
        sessions: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    ) -> int:
        """Commit a PREPARED dispatch and exact private inputs before launching a process."""
        if dispatch.state is not DispatchStatus.PREPARED:
            raise StateStoreError("only PREPARED dispatches may be committed before launch")
        if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != dispatch.intent.prompt_sha256:
            raise StateStoreError("dispatch prompt does not match durable prompt_sha256")
        if _sha256_json(policy) != dispatch.intent.policy_digest:
            raise StateStoreError("dispatch policy does not match durable policy_digest")
        if record.dispatches.get(dispatch.dispatch_id) != dispatch:
            raise StateStoreError("run record does not contain the exact prepared dispatch")
        if repository_before.get("repo_id") != dispatch.intent.repository.repo_id:
            raise StateStoreError("prepared repository snapshot does not match dispatch repository")
        return self.save_run(
            record,
            expected_generation=expected_generation,
            sessions=sessions,
            dispatch_payloads={
                dispatch.dispatch_id: DispatchPayload(
                    prompt=prompt,
                    policy=policy,
                    repository_before=repository_before,
                    session_metadata=session_metadata,
                )
            },
        )

    def prepare_dispatch_batch(
        self,
        record: RunRecord,
        *,
        expected_generation: int,
        dispatch_payloads: Mapping[str, DispatchPayload],
    ) -> int:
        """Atomically persist every prepared child and exact private batch inputs."""
        if not dispatch_payloads:
            raise StateStoreError("prepared batch requires at least one dispatch payload")
        for dispatch_id, payload in dispatch_payloads.items():
            try:
                dispatch = record.dispatches[dispatch_id]
            except KeyError as exc:
                raise StateStoreError(f"batch payload references unknown dispatch: {dispatch_id}") from exc
            if dispatch.state is not DispatchStatus.PREPARED:
                raise StateStoreError("batch payload requires PREPARED dispatches")
            if hashlib.sha256(payload.prompt.encode("utf-8")).hexdigest() != dispatch.intent.prompt_sha256:
                raise StateStoreError("batch dispatch prompt does not match durable prompt_sha256")
            if _sha256_json(payload.policy) != dispatch.intent.policy_digest:
                raise StateStoreError("batch dispatch policy does not match durable policy_digest")
            if payload.repository_before is None or (
                payload.repository_before.get("repo_id") != dispatch.intent.repository.repo_id
            ):
                raise StateStoreError("batch prepared repository snapshot does not match dispatch repository")
        return self.save_run(
            record,
            expected_generation=expected_generation,
            dispatch_payloads=dispatch_payloads,
        )

    def save_workspace_group(
        self,
        record: RunRecord,
        *,
        expected_generation: int,
        group: WorkspaceGroup,
    ) -> tuple[RunRecord, int]:
        """Persist one temporary-worktree lifecycle update with the run snapshot."""
        groups = dict(record.workspace_groups)
        groups[group.workspace_group_id] = group
        updated = record.model_copy(
            update={
                "workspace_groups": groups,
                "sequence": group.last_event.sequence,
                "updated_at": group.last_event.occurred_at,
            }
        )
        generation = self.save_run(updated, expected_generation=expected_generation)
        return updated, generation

    def update_dispatch_payload(
        self,
        *,
        run_id: str,
        dispatch_id: str,
        process_id: int | None = None,
        session_metadata: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        authoritative_verification: list[Mapping[str, Any]] | None = None,
        forwarding_payload: str | None = None,
    ) -> None:
        """Persist external process, result, or forwarding data before the next side effect."""
        connection = self._ready_connection()
        with self._transaction(connection):
            existing = connection.execute(
                "SELECT dispatch_id FROM dispatches WHERE run_id = ? AND dispatch_id = ?",
                (run_id, dispatch_id),
            ).fetchone()
            if existing is None:
                raise StateStoreError(f"dispatch not found: {dispatch_id}")
            connection.execute(
                """
                UPDATE dispatch_payloads
                SET process_id = COALESCE(?, process_id),
                    session_metadata_json = COALESCE(?, session_metadata_json),
                    result_json = COALESCE(?, result_json),
                    authoritative_verification_json = COALESCE(?, authoritative_verification_json),
                    forwarding_payload = COALESCE(?, forwarding_payload)
                WHERE run_id = ? AND dispatch_id = ?
                """,
                (
                    process_id,
                    _json_text(session_metadata) if session_metadata is not None else None,
                    _json_text(result) if result is not None else None,
                    _json_text(authoritative_verification)
                    if authoritative_verification is not None
                    else None,
                    redact_text(forwarding_payload) if forwarding_payload is not None else None,
                    run_id,
                    dispatch_id,
                ),
            )

    def commit_dispatch_transition(
        self,
        record: RunRecord,
        *,
        expected_generation: int,
        dispatch_id: str,
        target: DispatchStatus,
        event: TransitionEvent,
        runtime_session_id: str | None = None,
        result_digest: str | None = None,
        forwarding_digest: str | None = None,
        result: Mapping[str, Any] | None = None,
        authoritative_verification: list[Mapping[str, Any]] | None = None,
        forwarding_payload: str | None = None,
        process_id: int | None = None,
        process_host: str | None = None,
        process_started_at: datetime | None = None,
        process_create_time: float | None = None,
        failure_category: str | None = None,
        failure_detail: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
        repository_after: Mapping[str, Any] | None = None,
        sessions: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
        structured_git_final: Mapping[str, Any] | None = None,
    ) -> tuple[RunRecord, int]:
        """Atomically transition a dispatch and its durable external payloads."""
        try:
            current = record.dispatches[dispatch_id]
        except KeyError as exc:
            raise StateStoreError(f"run record does not contain dispatch: {dispatch_id}") from exc
        if target is DispatchStatus.COMPLETED and result is None:
            raise StateStoreError("COMPLETED dispatch transition requires the exact result payload")
        if target is DispatchStatus.COMPLETED and repository_after is None:
            raise StateStoreError("COMPLETED dispatch transition requires the inspected repository snapshot")
        if target is DispatchStatus.RUNNING and (
            process_id is None or process_id < 1 or process_create_time is None
        ):
            raise StateStoreError(
                "RUNNING dispatch transition requires a positive process_id and OS process creation time"
            )
        if target is DispatchStatus.FORWARDED and forwarding_payload is None:
            raise StateStoreError("FORWARDED dispatch transition requires the complete forwarding payload")
        transitioned = transition_dispatch_record(
            current,
            target,
            event,
            runtime_session_id=runtime_session_id,
            result_digest=result_digest,
            forwarding_digest=forwarding_digest,
            process_host=process_host,
            process_started_at=process_started_at,
            process_create_time=process_create_time,
            process_id=process_id,
            failure_category=failure_category,
            failure_detail=failure_detail,
        )
        dispatches = dict(record.dispatches)
        dispatches[dispatch_id] = transitioned
        updated = record.model_copy(
            update={
                "dispatches": dispatches,
                "sequence": event.sequence,
                "updated_at": event.occurred_at,
            }
        )
        payload = self.load_dispatch_payload(record.run_id, dispatch_id)
        replacement = DispatchPayload(
            prompt=payload.prompt,
            policy=payload.policy,
            result=result if result is not None else payload.result,
            authoritative_verification=(
                tuple(authoritative_verification)
                if authoritative_verification is not None
                else payload.authoritative_verification
            ),
            forwarding_payload=(
                forwarding_payload if forwarding_payload is not None else payload.forwarding_payload
            ),
            process_id=process_id if process_id is not None else payload.process_id,
            session_metadata=(
                session_metadata if session_metadata is not None else payload.session_metadata
            ),
            repository_before=payload.repository_before,
            repository_after=(
                repository_after if repository_after is not None else payload.repository_after
            ),
        )
        generation = self.save_run(
            updated,
            expected_generation=expected_generation,
            sessions=sessions,
            dispatch_payloads={dispatch_id: replacement},
            structured_git_updates=(
                {dispatch_id: structured_git_final}
                if structured_git_final is not None
                else None
            ),
        )
        logger.info(
            "dispatch transitioned",
            extra={
                "dispatcher_context": {
                    "project_id": updated.project_id,
                    "run_id": updated.run_id,
                    "dispatch_id": dispatch_id,
                    "step_id": transitioned.step_id,
                }
            },
        )
        return updated, generation

    def persist_forwarded_dispatch(
        self,
        record: RunRecord,
        *,
        expected_generation: int,
        dispatch_id: str,
        result: Mapping[str, Any],
        authoritative_verification: list[Mapping[str, Any]] | None,
        repository_after: Mapping[str, Any] | None,
        forwarding_payload: str,
        review_id: str | None = None,
        review: Mapping[str, Any] | None = None,
        structured_git_final: Mapping[str, Any] | None = None,
        sessions: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
        fault_hook: Callable[[], None] | None = None,
    ) -> tuple[RunRecord, int]:
        """Atomically persist a completed worker result with its forwarding.

        The dispatch record must already be transitioned to FORWARDED in
        memory. Result payload, authoritative verification, repository
        observation, forwarding payload, optional review row, and optional
        structured Git final state commit in one transaction so a crash can
        never strand a durable result without its forwarding.
        """
        try:
            dispatch = record.dispatches[dispatch_id]
        except KeyError as exc:
            raise StateStoreError(f"run record does not contain dispatch: {dispatch_id}") from exc
        if dispatch.state is not DispatchStatus.FORWARDED:
            raise StateStoreError("forwarded persistence requires a FORWARDED dispatch record")
        if dispatch.result_digest is None:
            raise StateStoreError("forwarded persistence requires a durable result digest")
        if dispatch.forwarding_digest is None:
            raise StateStoreError("forwarded persistence requires a durable forwarding digest")
        forwarding_digest = hashlib.sha256(forwarding_payload.encode("utf-8")).hexdigest()
        if dispatch.forwarding_digest != forwarding_digest:
            raise StateStoreError("forwarding payload does not match the durable forwarding digest")
        if _sha256_json(result) != dispatch.result_digest:
            raise StateStoreError("result payload does not match the durable result digest")
        if review is not None and review_id is None:
            raise StateStoreError("review persistence requires a review_id")
        connection = self._ready_connection()
        with self._transaction(connection):
            row = connection.execute(
                "SELECT generation, state FROM dispatches WHERE run_id = ? AND dispatch_id = ?",
                (record.run_id, dispatch_id),
            ).fetchone()
            if row is None:
                raise StateStoreConflictError(f"dispatch does not exist: {dispatch_id}")
            if row["state"] not in {"RUNNING", "COMPLETED"}:
                raise StateStoreConflictError(
                    f"dispatch cannot be forwarded from durable state {row['state']}"
                )
            generation_row = connection.execute(
                "SELECT generation FROM runs WHERE run_id = ?", (record.run_id,)
            ).fetchone()
            if generation_row is None or generation_row["generation"] != expected_generation:
                raise StateStoreConflictError(
                    f"run {record.run_id} generation conflict: expected {expected_generation}, "
                    f"found {generation_row['generation'] if generation_row else None}"
                )
            existing = self.load_dispatch_payload(record.run_id, dispatch_id)
            replacement = DispatchPayload(
                prompt=existing.prompt,
                policy=existing.policy,
                result=result,
                authoritative_verification=(
                    tuple(authoritative_verification)
                    if authoritative_verification is not None
                    else existing.authoritative_verification
                ),
                forwarding_payload=forwarding_payload,
                process_id=existing.process_id,
                session_metadata=existing.session_metadata,
                repository_before=existing.repository_before,
                repository_after=(
                    repository_after
                    if repository_after is not None
                    else existing.repository_after
                ),
            )
            generation = expected_generation + 1
            self._write_snapshot(
                connection,
                record,
                generation=generation,
                sessions=self.sessions_for_run(record.run_id) if sessions is None else sessions,
            )
            self._write_dispatch_payload(
                connection,
                record.run_id,
                dispatch_id,
                replacement,
            )
            if review_id is not None and review is not None:
                connection.execute(
                    """
                    INSERT INTO reviews(review_id, run_id, dispatch_id, review_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (review_id, record.run_id, dispatch_id, _json_text(review), _utc_now()),
                )
            if structured_git_final is not None:
                self._write_structured_git_final(
                    connection,
                    run_id=record.run_id,
                    dispatch_id=dispatch_id,
                    update=structured_git_final,
                )
            if fault_hook is not None:
                fault_hook()
        self._secure_database_files()
        logger.info(
            "dispatch forwarded",
            extra={
                "dispatcher_context": {
                    "project_id": record.project_id,
                    "run_id": record.run_id,
                    "dispatch_id": dispatch_id,
                    "step_id": dispatch.step_id,
                }
            },
        )
        return record, generation

    def persist_adopted_failed_review(
        self,
        record: RunRecord,
        *,
        expected_generation: int,
        dispatch_id: str,
        result: Mapping[str, Any],
        authoritative_verification: list[Mapping[str, Any]],
        repository_after: Mapping[str, Any],
        review_id: str,
        request_id: str,
        actor_id: str,
    ) -> tuple[RunRecord, int]:
        """Atomically retain a failed attempt while adopting its validated review."""
        try:
            dispatch = record.dispatches[dispatch_id]
        except KeyError as exc:
            raise StateStoreError(f"run record does not contain dispatch: {dispatch_id}") from exc
        if dispatch.state is not DispatchStatus.FAILED:
            raise StateStoreError("failed review adoption requires a FAILED dispatch record")
        connection = self._ready_connection()
        with self._transaction(connection):
            row = connection.execute(
                "SELECT generation, state FROM dispatches WHERE run_id = ? AND dispatch_id = ?",
                (record.run_id, dispatch_id),
            ).fetchone()
            if row is None or row["state"] != "FAILED":
                raise StateStoreConflictError("durable dispatch is not the failed review attempt")
            generation_row = connection.execute(
                "SELECT generation, state FROM runs WHERE run_id = ?", (record.run_id,)
            ).fetchone()
            if generation_row is None or generation_row["generation"] != expected_generation:
                raise StateStoreConflictError(
                    f"run {record.run_id} generation conflict: expected {expected_generation}, "
                    f"found {generation_row['generation'] if generation_row else None}"
                )
            if generation_row["state"] != "WAITING_OPERATOR":
                raise StateStoreConflictError(
                    "durable run is not waiting on the failed review reconciliation"
                )
            if connection.execute(
                "SELECT 1 FROM reviews WHERE run_id = ? AND dispatch_id = ?",
                (record.run_id, dispatch_id),
            ).fetchone() is not None:
                raise StateStoreConflictError("failed dispatch review was already adopted")
            existing = self.load_dispatch_payload(record.run_id, dispatch_id)
            replacement = DispatchPayload(
                prompt=existing.prompt,
                policy=existing.policy,
                result=result,
                authoritative_verification=tuple(authoritative_verification),
                forwarding_payload=existing.forwarding_payload,
                process_id=existing.process_id,
                session_metadata=existing.session_metadata,
                repository_before=existing.repository_before,
                repository_after=repository_after,
            )
            generation = expected_generation + 1
            self._write_snapshot(
                connection,
                record,
                generation=generation,
                sessions=self.sessions_for_run(record.run_id),
            )
            self._write_dispatch_payload(connection, record.run_id, dispatch_id, replacement)
            connection.execute(
                """
                INSERT INTO reviews(review_id, run_id, dispatch_id, review_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (review_id, record.run_id, dispatch_id, _json_text(result), _utc_now()),
            )
            connection.execute(
                """
                INSERT INTO operator_decisions(decision_id, run_id, request_id, actor_id, answer, created_at)
                VALUES (?, ?, ?, ?, 'reconcile', ?)
                """,
                (
                    f"decision-{uuid.uuid4().hex}",
                    record.run_id,
                    request_id,
                    actor_id,
                    _utc_now(),
                ),
            )
        self._secure_database_files()
        return record, generation

    def review_for_dispatch(self, run_id: str, dispatch_id: str) -> bool:
        """Return whether a durable review row already exists for a dispatch."""
        row = self._ready_connection().execute(
            "SELECT 1 FROM reviews WHERE run_id = ? AND dispatch_id = ?",
            (run_id, dispatch_id),
        ).fetchone()
        return row is not None

    def bind_dispatch_session(
        self,
        record: RunRecord,
        *,
        expected_generation: int,
        dispatch_id: str,
        runtime_session_id: str,
        event: TransitionEvent,
        pool: str,
        role_key: str,
        session_entry: Mapping[str, Any],
    ) -> tuple[RunRecord, int]:
        """Atomically bind the first validated session ID to a running dispatch."""
        try:
            current = record.dispatches[dispatch_id]
        except KeyError as exc:
            raise StateStoreError(f"run record does not contain dispatch: {dispatch_id}") from exc
        if current.state is not DispatchStatus.RUNNING:
            raise StateStoreError("session identity can only bind to a RUNNING dispatch")
        if current.runtime_session_id is not None:
            raise StateStoreConflictError("running dispatch already has a runtime session ID")
        dispatches = dict(record.dispatches)
        dispatches[dispatch_id] = current.model_copy(
            update={"runtime_session_id": runtime_session_id, "last_event": event}
        )
        updated = record.model_copy(
            update={
                "dispatches": dispatches,
                "sequence": event.sequence,
                "updated_at": event.occurred_at,
            }
        )
        sessions = self.sessions_for_run(record.run_id)
        sessions.setdefault(pool, {})[role_key] = dict(session_entry)
        payload = self.load_dispatch_payload(record.run_id, dispatch_id)
        metadata = dict(payload.session_metadata or {})
        metadata["runtime_session_id"] = runtime_session_id
        generation = self.save_run(
            updated,
            expected_generation=expected_generation,
            sessions=sessions,
            dispatch_payloads={
                dispatch_id: DispatchPayload(
                    prompt=payload.prompt,
                    policy=payload.policy,
                    result=payload.result,
                    authoritative_verification=payload.authoritative_verification,
                    forwarding_payload=payload.forwarding_payload,
                    process_id=payload.process_id,
                    session_metadata=metadata,
                    repository_before=payload.repository_before,
                    repository_after=payload.repository_after,
                )
            },
        )
        return updated, generation

    def load_dispatch_payload(self, run_id: str, dispatch_id: str) -> DispatchPayload:
        """Return the exact private inputs and outputs associated with one dispatch."""
        with self._connection_lock:
            row = self._ready_connection().execute(
                """
                SELECT prompt, policy_json, result_json, authoritative_verification_json,
                       forwarding_payload, process_id, session_metadata_json,
                       repository_before_json, repository_after_json
                FROM dispatch_payloads WHERE run_id = ? AND dispatch_id = ?
                """,
                (run_id, dispatch_id),
            ).fetchone()
            if row is None:
                raise StateStoreError(f"dispatch payload not found: {dispatch_id}")
            return DispatchPayload(
                prompt=str(row["prompt"]),
                policy=json.loads(row["policy_json"]),
                result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
                authoritative_verification=(
                    tuple(json.loads(row["authoritative_verification_json"]))
                    if row["authoritative_verification_json"] is not None
                    else None
                ),
                forwarding_payload=(
                    str(row["forwarding_payload"]) if row["forwarding_payload"] is not None else None
                ),
                process_id=int(row["process_id"]) if row["process_id"] is not None else None,
                session_metadata=(
                    json.loads(row["session_metadata_json"])
                    if row["session_metadata_json"] is not None
                    else None
                ),
                repository_before=(
                    json.loads(row["repository_before_json"])
                    if row["repository_before_json"] is not None
                    else None
                ),
                repository_after=(
                    json.loads(row["repository_after_json"])
                    if row["repository_after_json"] is not None
                    else None
                ),
            )

    def record_executor_proposal(
        self,
        *,
        run_id: str,
        dispatch_id: str,
        proposal: Mapping[str, Any],
    ) -> StructuredGitRecord:
        """Persist one immutable executor proposal before dispatcher side effects."""
        proposal_json = _json_text(proposal)
        proposal_digest = hashlib.sha256(proposal_json.encode("utf-8")).hexdigest()
        now = _utc_now()
        connection = self._ready_connection()
        with self._transaction(connection):
            dispatch = connection.execute(
                "SELECT state FROM dispatches WHERE run_id = ? AND dispatch_id = ?",
                (run_id, dispatch_id),
            ).fetchone()
            if dispatch is None:
                raise StateStoreError(f"executor proposal references unknown dispatch: {dispatch_id}")
            existing = connection.execute(
                "SELECT proposal_digest FROM structured_git_commits WHERE run_id = ? AND dispatch_id = ?",
                (run_id, dispatch_id),
            ).fetchone()
            if existing is not None:
                if existing["proposal_digest"] != proposal_digest:
                    raise StateStoreConflictError("executor proposal differs from the durable proposal")
            else:
                connection.execute(
                    """
                    INSERT INTO structured_git_commits(
                        run_id, dispatch_id, state, proposal_json, proposal_digest,
                        created_at, updated_at
                    ) VALUES (?, ?, 'PROPOSAL_RECEIVED', ?, ?, ?, ?)
                    """,
                    (run_id, dispatch_id, proposal_json, proposal_digest, now, now),
                )
        return self.load_structured_git_record(run_id, dispatch_id)

    def record_structured_git_checked(
        self,
        *,
        run_id: str,
        dispatch_id: str,
        checked: Mapping[str, Any],
        intent: Mapping[str, Any] | None,
    ) -> StructuredGitRecord:
        """Persist checks and optional commit intent before changing the real index."""
        target = "COMMIT_INTENT_PERSISTED" if intent is not None else "CHECKED"
        self._transition_structured_git_record(
            run_id=run_id,
            dispatch_id=dispatch_id,
            allowed_states=("PROPOSAL_RECEIVED",),
            target=target,
            checked_json=_json_text(checked),
            intent_json=_json_text(intent) if intent is not None else None,
        )
        return self.load_structured_git_record(run_id, dispatch_id)

    def record_structured_git_staged(
        self,
        *,
        run_id: str,
        dispatch_id: str,
        stage: Mapping[str, Any],
    ) -> StructuredGitRecord:
        """Persist the exact staged-tree observation before running Git commit."""
        self._transition_structured_git_record(
            run_id=run_id,
            dispatch_id=dispatch_id,
            allowed_states=("COMMIT_INTENT_PERSISTED",),
            target="STAGED",
            stage_json=_json_text(stage),
        )
        return self.load_structured_git_record(run_id, dispatch_id)

    def mark_structured_git_reconciliation(
        self,
        *,
        run_id: str,
        dispatch_id: str,
        detail: str,
    ) -> StructuredGitRecord:
        """Fail closed after an ambiguous checked, staged, or committed side effect."""
        self._transition_structured_git_record(
            run_id=run_id,
            dispatch_id=dispatch_id,
            allowed_states=(
                "PROPOSAL_RECEIVED",
                "CHECKED",
                "COMMIT_INTENT_PERSISTED",
                "STAGED",
            ),
            target="RECONCILIATION_REQUIRED",
            failure_detail=redact_text(detail)[:5000],
        )
        return self.load_structured_git_record(run_id, dispatch_id)

    def load_structured_git_record(
        self,
        run_id: str,
        dispatch_id: str,
    ) -> StructuredGitRecord:
        """Load one exact proposal/commit lifecycle record."""
        row = self._ready_connection().execute(
            "SELECT * FROM structured_git_commits WHERE run_id = ? AND dispatch_id = ?",
            (run_id, dispatch_id),
        ).fetchone()
        if row is None:
            raise StateStoreError(f"structured Git record not found: {dispatch_id}")
        return StructuredGitRecord(
            run_id=str(row["run_id"]),
            dispatch_id=str(row["dispatch_id"]),
            state=str(row["state"]),  # type: ignore[arg-type]
            proposal=json.loads(row["proposal_json"]),
            proposal_digest=str(row["proposal_digest"]),
            checked=json.loads(row["checked_json"]) if row["checked_json"] else None,
            intent=json.loads(row["intent_json"]) if row["intent_json"] else None,
            stage=json.loads(row["stage_json"]) if row["stage_json"] else None,
            commit=json.loads(row["commit_json"]) if row["commit_json"] else None,
            result_revision=str(row["result_revision"]) if row["result_revision"] else None,
            repository_after=(
                json.loads(row["repository_after_json"])
                if row["repository_after_json"]
                else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def _transition_structured_git_record(
        self,
        *,
        run_id: str,
        dispatch_id: str,
        allowed_states: tuple[str, ...],
        target: str,
        checked_json: str | None = None,
        intent_json: str | None = None,
        stage_json: str | None = None,
        commit_json: str | None = None,
        result_revision: str | None = None,
        repository_after_json: str | None = None,
        failure_detail: str | None = None,
    ) -> None:
        connection = self._ready_connection()
        with self._transaction(connection):
            row = connection.execute(
                "SELECT state FROM structured_git_commits WHERE run_id = ? AND dispatch_id = ?",
                (run_id, dispatch_id),
            ).fetchone()
            if row is None:
                raise StateStoreError(f"structured Git record not found: {dispatch_id}")
            if row["state"] not in allowed_states:
                raise StateStoreConflictError(
                    f"structured Git record cannot move from {row['state']} to {target}"
                )
            connection.execute(
                """
                UPDATE structured_git_commits
                SET state = ?, checked_json = COALESCE(?, checked_json),
                    intent_json = COALESCE(?, intent_json),
                    stage_json = COALESCE(?, stage_json),
                    commit_json = COALESCE(?, commit_json),
                    result_revision = COALESCE(?, result_revision),
                    repository_after_json = COALESCE(?, repository_after_json),
                    failure_detail = COALESCE(?, failure_detail), updated_at = ?
                WHERE run_id = ? AND dispatch_id = ?
                """,
                (
                    target,
                    checked_json,
                    intent_json,
                    stage_json,
                    commit_json,
                    result_revision,
                    repository_after_json,
                    failure_detail,
                    _utc_now(),
                    run_id,
                    dispatch_id,
                ),
            )

    def classify_recovery(self, run_id: str) -> list[RecoveryItem]:
        """Classify unresolved dispatches without launching or retrying any work."""
        record, _generation = self.load_run(run_id)
        items: list[RecoveryItem] = []
        for dispatch in sorted(record.dispatches.values(), key=lambda value: value.dispatch_id):
            if dispatch.state is DispatchStatus.PREPARED:
                items.append(
                    RecoveryItem(
                        dispatch.dispatch_id,
                        dispatch.state,
                        "operator_reconciliation_required",
                        "a crash may have occurred after process creation but before RUNNING was committed",
                    )
                )
            elif dispatch.state is DispatchStatus.RUNNING:
                commit_row = self._ready_connection().execute(
                    "SELECT state FROM structured_git_commits WHERE run_id = ? AND dispatch_id = ?",
                    (run_id, dispatch.dispatch_id),
                ).fetchone()
                detail = "external side effects may have completed; automatic retry is forbidden"
                disposition: Literal[
                    "structured_commit_adoption_required",
                    "operator_reconciliation_required",
                ] = "operator_reconciliation_required"
                if commit_row is not None and commit_row["state"] == "STAGED":
                    disposition = "structured_commit_adoption_required"
                    detail = (
                        "durable staged intent requires exact committed HEAD adoption; "
                        "any mismatch requires operator reconciliation"
                    )
                elif commit_row is not None and commit_row["state"] in {
                    "CHECKED",
                    "COMMIT_INTENT_PERSISTED",
                    "RECONCILIATION_REQUIRED",
                }:
                    detail = (
                        f"structured Git state {commit_row['state']} may have external side effects; "
                        "exact commit adoption or operator reconciliation is required"
                    )
                items.append(
                    RecoveryItem(
                        dispatch.dispatch_id,
                        dispatch.state,
                        disposition,
                        detail,
                    )
                )
            elif dispatch.state is DispatchStatus.COMPLETED:
                items.append(
                    RecoveryItem(
                        dispatch.dispatch_id,
                        dispatch.state,
                        "forwarding_required",
                        "the result is durable but supervisor forwarding is not acknowledged",
                    )
                )
            elif dispatch.state is DispatchStatus.FORWARDED:
                items.append(
                    RecoveryItem(
                        dispatch.dispatch_id,
                        dispatch.state,
                        "acknowledgement_required",
                        "the forwarding payload is durable but acknowledgement is missing",
                    )
                )
        return items

    def classify_workspace_recovery(self, run_id: str) -> list[WorkspaceRecoveryItem]:
        """Classify incomplete temporary-worktree lifecycles without Git side effects."""
        record, _generation = self.load_run(run_id)
        items: list[WorkspaceRecoveryItem] = []
        for group in sorted(record.workspace_groups.values(), key=lambda value: value.workspace_group_id):
            if group.state is WorkspaceGroupStatus.CLEANED:
                continue
            if group.state is WorkspaceGroupStatus.CLEANUP_PENDING:
                items.append(
                    WorkspaceRecoveryItem(
                        group.workspace_group_id,
                        group.state,
                        "cleanup_required",
                        "workspace cleanup intent is durable; inspect and remove only owned temporary Git state",
                    )
                )
            else:
                items.append(
                    WorkspaceRecoveryItem(
                        group.workspace_group_id,
                        group.state,
                        "operator_reconciliation_required",
                        "temporary workspace branches may contain unintegrated work; automatic retry is forbidden",
                    )
                )
        return items

    def answer_operator_request(
        self,
        *,
        run_id: str,
        expected_generation: int,
        request_id: str,
        answer: str,
        actor_id: str,
    ) -> tuple[RunRecord, int]:
        """Persist an allowed answer and its state transition in one transaction."""
        record, generation = self.load_run(run_id)
        if generation != expected_generation:
            raise StateStoreConflictError(
                f"run {run_id} generation conflict: expected {expected_generation}, found {generation}"
            )
        request = record.operator_request
        if record.state is not RunStatus.WAITING_OPERATOR or request is None:
            raise StateStoreError("run is not waiting for an operator answer")
        if request.request_id != request_id:
            raise StateStoreError("operator request ID does not match the active waiting request")
        if answer not in request.allowed_answers:
            raise StateStoreError("operator answer is not one of the allowed answers")
        if request.expires_at is not None and request.expires_at <= datetime.now(UTC):
            raise StateStoreError("operator request has expired")
        if request.required_role is not None and actor_id != request.required_role:
            raise StateStoreError("operator actor does not satisfy the required role")
        event = TransitionEvent(
            event_id=f"event-{uuid.uuid4().hex}",
            sequence=record.sequence + 1,
            actor="operator",
            reason=f"operator answered request {request_id}",
            correlation_id=request.context_ref,
            occurred_at=datetime.now(UTC),
        )
        decision_id = f"decision-{uuid.uuid4().hex}"
        updated_record = record
        if request.kind == "risk_gate":
            if request.step_id is None or request.step_id not in record.steps:
                raise StateStoreCorruptionError("risk gate request does not reference a known step")
            if answer == "approve":
                step = record.steps[request.step_id]
                if step.operator_gate_resolved:
                    raise StateStoreError("risk gate was already resolved")
                steps = dict(record.steps)
                steps[request.step_id] = step.model_copy(
                    update={"operator_gate_resolved": True, "last_event": event}
                )
                updated_record = record.model_copy(
                    update={"steps": steps, "sequence": event.sequence, "updated_at": event.occurred_at}
                )
                target = RunStatus.RUNNING
            elif answer == "deny":
                target = RunStatus.HALTED
            else:
                raise StateStoreCorruptionError(f"invalid risk gate answer {answer!r}")
        elif request.kind == "escalation":
            if request.step_id is None or request.step_id not in record.steps:
                raise StateStoreCorruptionError("escalation request does not reference a known step")
            if answer == "reassign":
                step = record.steps[request.step_id]
                if step.state is not StepStatus.BLOCKED:
                    raise StateStoreError("escalation reassignment requires a blocked step")
                plan_step = next(step for step in record.plan.steps if step.step_id == request.step_id)
                if plan_step.retry.escalation_role_key != request.reassignment_role_key:
                    raise StateStoreCorruptionError(
                        "escalation request reassignment role does not match the normalized plan"
                    )
                steps = dict(record.steps)
                reassigned = transition_step(step, StepStatus.READY, event)
                steps[request.step_id] = reassigned.model_copy(
                    update={"reassignment_role_key": request.reassignment_role_key}
                )
                updated_record = record.model_copy(
                    update={"steps": steps, "sequence": event.sequence, "updated_at": event.occurred_at}
                )
                target = RunStatus.RUNNING
            elif answer == "halt":
                target = RunStatus.HALTED
            else:
                raise StateStoreCorruptionError(f"invalid escalation answer {answer!r}")
        elif request.kind == "review_waiver":
            if request.step_id is None or request.step_id not in record.steps or record.policy is None:
                raise StateStoreCorruptionError("review waiver request does not reference a compiled policy step")
            obligation = record.policy.review_obligations[request.step_id]
            step = record.steps[request.step_id]
            if not obligation.waivable or step.state is not StepStatus.REVIEW_REQUIRED:
                raise StateStoreError("review waiver is not available for the current step state")
            if answer == "waive":
                waived_review = transition_step(
                    step,
                    StepStatus.ACCEPTED,
                    event,
                    review_waiver_decision_ref=decision_id,
                )
                steps = dict(record.steps)
                steps[request.step_id] = waived_review
                updated_record = record.model_copy(
                    update={"steps": steps, "sequence": event.sequence, "updated_at": event.occurred_at}
                )
                target = RunStatus.RUNNING
            elif answer == "halt":
                target = RunStatus.HALTED
            else:
                raise StateStoreCorruptionError(f"invalid review waiver answer {answer!r}")
        elif request.kind == "stall_recovery":
            if request.step_id is None or request.step_id not in record.steps:
                raise StateStoreCorruptionError("stall recovery request does not reference a known step")
            if answer == "retry":
                step = record.steps[request.step_id]
                if step.state is StepStatus.BLOCKED:
                    steps = dict(record.steps)
                    steps[request.step_id] = transition_step(step, StepStatus.READY, event)
                    updated_record = record.model_copy(
                        update={"steps": steps, "sequence": event.sequence, "updated_at": event.occurred_at}
                    )
                elif step.state is StepStatus.REVIEW_REQUIRED:
                    steps = dict(record.steps)
                    steps[request.step_id] = step.model_copy(update={"last_event": event})
                    updated_record = record.model_copy(
                        update={"steps": steps, "sequence": event.sequence, "updated_at": event.occurred_at}
                    )
                else:
                    raise StateStoreError("stall retry requires a blocked or review-required step")
                target = RunStatus.RUNNING
            elif answer == "halt":
                target = RunStatus.HALTED
            else:
                raise StateStoreCorruptionError(f"invalid stall recovery answer {answer!r}")
        elif request.kind == "underspecification":
            if answer == "answer":
                target = RunStatus.RUNNING
            elif answer == "halt":
                target = RunStatus.HALTED
            else:
                raise StateStoreCorruptionError(f"invalid underspecification answer {answer!r}")
        elif request.kind == "budget":
            if answer != "halt":
                raise StateStoreCorruptionError(f"invalid budget answer {answer!r}")
            target = RunStatus.HALTED
        elif request.kind == "reconciliation":
            if answer == "halt":
                target = RunStatus.HALTED
            elif answer == "reconcile":
                if request.step_id is None or request.step_id not in record.steps:
                    raise StateStoreCorruptionError(
                        "reconciliation request does not reference a known step"
                    )
                try:
                    dispatch = record.dispatches[request.context_ref]
                except KeyError as exc:
                    raise StateStoreCorruptionError(
                        "reconciliation request does not reference a known dispatch"
                    ) from exc
                if dispatch.step_id != request.step_id:
                    raise StateStoreCorruptionError(
                        "reconciliation dispatch does not belong to the requested step"
                    )
                if dispatch.state not in {DispatchStatus.FAILED, DispatchStatus.ABANDONED}:
                    raise StateStoreError("reconciliation requires a failed or abandoned dispatch")
                step = record.steps[request.step_id]
                steps = dict(record.steps)
                if step.state is StepStatus.BLOCKED:
                    steps[request.step_id] = transition_step(step, StepStatus.READY, event)
                elif step.state is StepStatus.REVIEW_REQUIRED:
                    steps[request.step_id] = step.model_copy(update={"last_event": event})
                else:
                    raise StateStoreError(
                        "reconciliation requires a blocked or review-required step"
                    )
                updated_record = record.model_copy(
                    update={"steps": steps, "sequence": event.sequence, "updated_at": event.occurred_at}
                )
                target = RunStatus.RUNNING
            else:
                raise StateStoreCorruptionError(f"invalid reconciliation answer {answer!r}")
        elif request.kind == "batch_reconciliation":
            if answer == "halt":
                target = RunStatus.HALTED
            elif answer == "reconcile":
                try:
                    batch = record.batches[request.context_ref]
                except KeyError as exc:
                    raise StateStoreCorruptionError(
                        "batch reconciliation request does not reference a known batch"
                    ) from exc
                if batch.state is not BatchStatus.FAILED:
                    raise StateStoreError("batch reconciliation requires a failed batch")
                if not batch.failed_dispatch_ids:
                    raise StateStoreCorruptionError(
                        "failed batch reconciliation requires failed dispatch IDs"
                    )
                affected_step_ids: set[str] = set()
                for dispatch_id in batch.failed_dispatch_ids:
                    try:
                        dispatch = record.dispatches[dispatch_id]
                    except KeyError as exc:
                        raise StateStoreCorruptionError(
                            f"batch reconciliation references unknown dispatch {dispatch_id}"
                        ) from exc
                    if dispatch.batch_id != batch.batch_id:
                        raise StateStoreCorruptionError(
                            f"dispatch {dispatch_id} does not belong to batch {batch.batch_id}"
                        )
                    if dispatch.state not in {DispatchStatus.FAILED, DispatchStatus.ABANDONED}:
                        raise StateStoreError(
                            f"batch reconciliation dispatch {dispatch_id} is not failed or abandoned"
                        )
                    if dispatch.step_id not in record.steps:
                        raise StateStoreCorruptionError(
                            f"batch reconciliation dispatch {dispatch_id} references an unknown step"
                        )
                    affected_step_ids.add(dispatch.step_id)
                steps = dict(record.steps)
                for step_id in sorted(affected_step_ids):
                    step = record.steps[step_id]
                    if step.state is StepStatus.BLOCKED:
                        steps[step_id] = transition_step(step, StepStatus.READY, event)
                    elif step.state is StepStatus.REVIEW_REQUIRED:
                        steps[step_id] = step.model_copy(update={"last_event": event})
                    else:
                        raise StateStoreError(
                            f"batch reconciliation step {step_id} has incompatible state {step.state.value}"
                        )
                updated_record = record.model_copy(
                    update={"steps": steps, "sequence": event.sequence, "updated_at": event.occurred_at}
                )
                target = RunStatus.RUNNING
            else:
                raise StateStoreCorruptionError(f"invalid batch reconciliation answer {answer!r}")
        elif request.kind == "workspace_reconciliation":
            if answer == "halt":
                target = RunStatus.HALTED
            elif answer == "reconcile":
                try:
                    group = record.workspace_groups[request.context_ref]
                except KeyError as exc:
                    raise StateStoreCorruptionError(
                        "workspace reconciliation request does not reference a known workspace group"
                    ) from exc
                if group.state is not WorkspaceGroupStatus.CLEANED:
                    raise StateStoreError(
                        "workspace reconciliation requires durable cleanup to reach CLEANED"
                    )
                target = RunStatus.RUNNING
            else:
                raise StateStoreCorruptionError(f"invalid workspace reconciliation answer {answer!r}")
        else:
            raise StateStoreCorruptionError(
                f"no answer handling for operator request kind {request.kind!r}"
            )
        updated = transition_run(updated_record, target, event)
        sessions = self.sessions_for_run(run_id)
        connection = self._ready_connection()
        with self._transaction(connection):
            row = connection.execute(
                "SELECT generation FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row["generation"] != expected_generation:
                raise StateStoreConflictError(f"run {run_id} changed before the answer was committed")
            self._write_snapshot(connection, updated, generation=expected_generation + 1, sessions=sessions)
            connection.execute(
                """
                INSERT INTO operator_decisions(decision_id, run_id, request_id, actor_id, answer, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    run_id,
                    request_id,
                    actor_id,
                    redact_text(answer),
                    _utc_now(),
                ),
            )
        return updated, expected_generation + 1

    def write_transcript(
        self,
        *,
        run_id: str,
        dispatch_id: str | None,
        content: str,
        sequence: int,
        label: str,
    ) -> Path:
        """Write a collision-free, hashed transcript derived from authoritative state."""
        transcripts = ensure_private_directory(self.state_dir / "transcripts" / run_id)
        safe_label = "".join(character if character.isalnum() or character in "._-" else "_" for character in label)
        path = transcripts / f"{sequence:08d}-{safe_label}-{uuid.uuid4().hex}.md"
        content = redact_text(content)
        write_private_text_exclusive(path, content)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        connection = self._ready_connection()
        with self._transaction(connection):
            connection.execute(
                """
                INSERT INTO artifacts(artifact_id, run_id, dispatch_id, kind, path, sha256, created_at)
                VALUES (?, ?, ?, 'transcript', ?, ?, ?)
                """,
                (
                    f"artifact-{uuid.uuid4().hex}",
                    run_id,
                    dispatch_id,
                    str(path.relative_to(self.state_dir)),
                    digest,
                    _utc_now(),
                ),
            )
        return path

    def append_audit_event(
        self,
        *,
        run_id: str,
        event_id: str,
        sequence: int,
        kind: str,
        correlation_id: str,
        causation_id: str | None,
        payload: Mapping[str, Any],
    ) -> None:
        """Commit one correlated audit event to the authority before derived export."""
        connection = self._ready_connection()
        with self._transaction(connection):
            connection.execute(
                """
                INSERT INTO audit_events(
                    event_id, run_id, sequence, kind, correlation_id, causation_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    sequence,
                    kind,
                    correlation_id,
                    causation_id,
                    _json_text(payload),
                    _utc_now(),
                ),
            )

    def complete_run(
        self,
        record: RunRecord,
        *,
        expected_generation: int,
        event_id: str,
        sequence: int,
        correlation_id: str,
        report_path: str,
    ) -> int:
        """Commit terminal run state and its audit event in one transaction."""
        if record.state is not RunStatus.SUCCEEDED:
            raise StateStoreError("complete_run requires a SUCCEEDED run record")
        sessions = self.sessions_for_run(record.run_id)
        connection = self._ready_connection()
        with self._transaction(connection):
            row = connection.execute(
                "SELECT generation FROM runs WHERE run_id = ?", (record.run_id,)
            ).fetchone()
            if row is None or row["generation"] != expected_generation:
                raise StateStoreConflictError(
                    f"run {record.run_id} changed before terminal commit"
                )
            generation = expected_generation + 1
            self._write_snapshot(
                connection,
                record,
                generation=generation,
                sessions=sessions,
            )
            connection.execute(
                """
                INSERT INTO audit_events(
                    event_id, run_id, sequence, kind, correlation_id,
                    causation_id, payload_json, created_at
                ) VALUES (?, ?, ?, 'run_succeeded', ?, NULL, ?, ?)
                """,
                (
                    event_id,
                    record.run_id,
                    sequence,
                    correlation_id,
                    _json_text({"report_path": report_path}),
                    _utc_now(),
                ),
            )
        return generation

    def record_review(
        self,
        *,
        run_id: str,
        dispatch_id: str,
        review_id: str,
        review: Mapping[str, Any],
    ) -> None:
        """Store an immutable review payload correlated to its dispatch attempt."""
        connection = self._ready_connection()
        with self._transaction(connection):
            exists = connection.execute(
                "SELECT 1 FROM dispatches WHERE run_id = ? AND dispatch_id = ?",
                (run_id, dispatch_id),
            ).fetchone()
            if exists is None:
                raise StateStoreError(f"review references unknown dispatch: {dispatch_id}")
            connection.execute(
                """
                INSERT INTO reviews(review_id, run_id, dispatch_id, review_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (review_id, run_id, dispatch_id, _json_text(review), _utc_now()),
            )

    def record_tool_version(self, *, run_id: str, tool_name: str, version: str) -> None:
        """Persist an exact external tool version used by an authoritative run."""
        if not tool_name or not version:
            raise ValueError("tool_name and version must be non-empty")
        connection = self._ready_connection()
        with self._transaction(connection):
            connection.execute(
                """
                INSERT INTO tool_versions(run_id, tool_name, version, recorded_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id, tool_name) DO UPDATE SET
                    version = excluded.version,
                    recorded_at = excluded.recorded_at
                """,
                (run_id, tool_name, version, _utc_now()),
            )

    def save_baseline(
        self,
        *,
        project_id: str,
        plan_digest: str,
        source_digest: str,
        candidate: Mapping[str, Any],
        operator_decision_ref: str,
    ) -> None:
        """Append one immutable operator-approved historical baseline before any new run."""
        candidate_json = _json_text(candidate)
        candidate_digest = hashlib.sha256(candidate_json.encode("utf-8")).hexdigest()
        connection = self._ready_connection()
        with self._transaction(connection):
            connection.execute(
                """
                INSERT INTO baseline_approvals(
                    project_id, plan_digest, source_digest, approval_json,
                    approval_digest, operator_decision_ref, approved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    plan_digest,
                    source_digest,
                    candidate_json,
                    candidate_digest,
                    operator_decision_ref,
                    _utc_now(),
                ),
            )

    def load_baseline(self, *, project_id: str, plan_digest: str) -> dict[str, Any] | None:
        """Load one approved baseline or return ``None`` when no approval exists."""
        row = self._ready_connection().execute(
            """
            SELECT approval_json, approval_digest, source_digest, operator_decision_ref, approved_at
            FROM baseline_approvals WHERE project_id = ? AND plan_digest = ?
            ORDER BY approved_at DESC, approval_digest DESC LIMIT 1
            """,
            (project_id, plan_digest),
        ).fetchone()
        if row is None:
            return None
        candidate = json.loads(row["approval_json"])
        actual_digest = hashlib.sha256(row["approval_json"].encode("utf-8")).hexdigest()
        if actual_digest != row["approval_digest"]:
            raise StateStoreCorruptionError("approved baseline digest does not match stored approval")
        return {
            "candidate": candidate,
            "source_digest": row["source_digest"],
            "operator_decision_ref": row["operator_decision_ref"],
            "approved_at": row["approved_at"],
        }

    def export_audit_jsonl(self, run_id: str) -> Path:
        """Write a deterministic, derived JSONL audit export from authoritative rows."""
        rows = self._ready_connection().execute(
            """
            SELECT event_id, sequence, kind, correlation_id, causation_id, payload_json, created_at
            FROM audit_events WHERE run_id = ? ORDER BY sequence, created_at, event_id
            """,
            (run_id,),
        ).fetchall()
        lines = []
        for row in rows:
            lines.append(
                json.dumps(
                    {
                        "event_id": row["event_id"],
                        "sequence": row["sequence"],
                        "kind": row["kind"],
                        "correlation_id": row["correlation_id"],
                        "causation_id": row["causation_id"],
                        "payload": redact_value(json.loads(row["payload_json"])),
                        "created_at": row["created_at"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        directory = ensure_private_directory(self.state_dir / "audit")
        path = directory / f"run-{run_id}.jsonl"
        atomic_write_private_text(path, "\n".join(lines) + ("\n" if lines else ""))
        return path

    def export_run_report(
        self,
        run_id: str,
        *,
        record_override: RunRecord | None = None,
        generation_override: int | None = None,
    ) -> Path:
        """Render a deterministic Markdown summary from the authoritative rows."""
        stored_record, stored_generation = self.load_run(run_id)
        record = record_override or stored_record
        generation = generation_override or stored_generation
        if record.run_id != run_id:
            raise StateStoreError("report record does not match requested run_id")
        lines = [
            f"# Run {record.run_id}",
            "",
            f"- Project: `{record.project_id}`",
            f"- State: `{record.state.value}`",
            f"- Generation: `{generation}`",
            f"- Plan digest: `{record.plan_digest}`",
            "",
            "## Steps",
            "",
            "| Step | State | Executor attempts | Reviewer attempts |",
            "|---|---|---:|---:|",
        ]
        for step in sorted(record.steps.values(), key=lambda value: value.step_id):
            lines.append(
                f"| `{step.step_id}` | `{step.state.value}` | {step.executor_attempts} | "
                f"{step.reviewer_attempts} |"
            )
        lines.extend(
            [
                "",
                "## Measured Usage",
                "",
                "| Scope | Cost USD | Tokens | Input | Output | Reasoning |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for scope, usage in [("run", record.usage.run), *sorted(record.usage.by_step.items())]:
            lines.append(
                f"| `{scope}` | {usage.cost_usd:.6f} | {usage.tokens_total} | {usage.tokens_input} | "
                f"{usage.tokens_output} | {usage.tokens_reasoning} |"
            )
        lines.extend(
            [
                "",
                "## Dispatches",
                "",
                "| Dispatch | State | Attempt | Session | Result revision |",
                "|---|---|---:|---|---|",
            ]
        )
        for dispatch in sorted(record.dispatches.values(), key=lambda value: value.dispatch_id):
            payload = self._ready_connection().execute(
                "SELECT result_json FROM dispatch_payloads WHERE run_id = ? AND dispatch_id = ?",
                (record.run_id, dispatch.dispatch_id),
            ).fetchone()
            revision = ""
            if payload is not None and payload["result_json"] is not None:
                result = json.loads(payload["result_json"])
                repository = result.get("repository") if isinstance(result, dict) else None
                if isinstance(repository, dict):
                    revision = str(repository.get("result_revision") or repository.get("patch_sha256") or "")
            lines.append(
                f"| `{dispatch.dispatch_id}` | `{dispatch.state.value}` | {dispatch.attempt} | "
                f"`{dispatch.runtime_session_id or ''}` | `{revision}` |"
            )
        lines.extend(["", "## Batches", "", "| Batch | State | Children | Failed children |", "|---|---|---|---|"])
        for batch in sorted(record.batches.values(), key=lambda value: value.batch_id):
            lines.append(
                f"| `{batch.batch_id}` | `{batch.state.value}` | "
                f"`{', '.join(batch.dispatch_ids)}` | `{', '.join(batch.failed_dispatch_ids)}` |"
            )
        lines.extend(
            [
                "",
                "## Workspace Groups",
                "",
                "| Group | Repository | State | Base revision | Children |",
                "|---|---|---|---|---|",
            ]
        )
        for group in sorted(record.workspace_groups.values(), key=lambda value: value.workspace_group_id):
            lines.append(
                f"| `{group.workspace_group_id}` | `{group.repo_id}` | `{group.state.value}` | "
                f"`{group.base_revision}` | `{', '.join(child.step_id for child in group.children)}` |"
            )
        lines.extend(
            [
                "",
                "## Repository Coordinates",
                "",
                "| Dispatch | Repository | Base branch | Working branch | Base SHA | Worktree ID | Expected remote |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for dispatch in sorted(record.dispatches.values(), key=lambda value: value.dispatch_id):
            coordinate = dispatch.intent.repository
            lines.append(
                f"| `{dispatch.dispatch_id}` | `{coordinate.repo_id}` | `{coordinate.base_branch or ''}` | "
                f"`{coordinate.working_branch or ''}` | `{coordinate.base_revision}` | "
                f"`{coordinate.worktree_id or ''}` | "
                f"`{coordinate.remote_name or ''} {coordinate.remote_url or ''}` |"
            )
        artifacts = self._ready_connection().execute(
            "SELECT kind, path, sha256 FROM artifacts WHERE run_id = ? ORDER BY path, artifact_id",
            (record.run_id,),
        ).fetchall()
        lines.extend(["", "## Artifacts", "", "| Kind | Path | SHA-256 |", "|---|---|---|"])
        for artifact in artifacts:
            lines.append(f"| `{artifact['kind']}` | `{artifact['path']}` | `{artifact['sha256']}` |")
        audit_rows = self._ready_connection().execute(
            """
            SELECT sequence, kind, correlation_id, causation_id, created_at
            FROM audit_events WHERE run_id = ? ORDER BY sequence, created_at, event_id
            """,
            (record.run_id,),
        ).fetchall()
        lines.extend(
            [
                "",
                "## Audit Events",
                "",
                "| Sequence | Kind | Correlation | Causation | Recorded at |",
                "|---:|---|---|---|---|",
            ]
        )
        for audit in audit_rows:
            lines.append(
                f"| {audit['sequence']} | `{audit['kind']}` | `{audit['correlation_id']}` | "
                f"`{audit['causation_id'] or ''}` | `{audit['created_at']}` |"
            )
        lines.extend(["", "## Typed Evidence", "", "| Dispatch | Artifact | Path | SHA-256 |", "|---|---|---|---|"])
        payload_rows = self._ready_connection().execute(
            "SELECT dispatch_id, result_json FROM dispatch_payloads WHERE run_id = ? ORDER BY dispatch_id",
            (record.run_id,),
        ).fetchall()
        for payload_row in payload_rows:
            if payload_row["result_json"] is None:
                continue
            result = json.loads(payload_row["result_json"])
            for evidence in result.get("evidence", []) if isinstance(result, dict) else []:
                lines.append(
                    f"| `{payload_row['dispatch_id']}` | `{evidence['artifact_id']}` | "
                    f"`{evidence['relative_path']}` | `{evidence['sha256']}` |"
                )
        lines.extend(
            [
                "",
                "## Inspected Evidence Manifests",
                "",
                "| Dispatch | Manifest SHA-256 |",
                "|---|---|",
                "",
                "| Dispatch | Path | Type | Size | SHA-256 |",
                "|---|---|---|---:|---|",
            ]
        )
        manifest_rows = self._ready_connection().execute(
            "SELECT dispatch_id, repository_after_json FROM dispatch_payloads WHERE run_id = ? ORDER BY dispatch_id",
            (record.run_id,),
        ).fetchall()
        for manifest_row in manifest_rows:
            if manifest_row["repository_after_json"] is None:
                continue
            snapshot = json.loads(manifest_row["repository_after_json"])
            if isinstance(snapshot, dict):
                lines.append(
                    f"| `{manifest_row['dispatch_id']}` | `{snapshot.get('manifest_sha256', '')}` |"
                )
            for entry in snapshot.get("evidence", []) if isinstance(snapshot, dict) else []:
                lines.append(
                    f"| `{manifest_row['dispatch_id']}` | `{entry['relative_path']}` | "
                    f"`{entry['file_type']}` | {entry['size_bytes']} | `{entry['sha256']}` |"
                )
        lines.extend(["", "## Reviews", "", "| Review | Dispatch | Verdict | Target revision |", "|---|---|---|---|"])
        reviews = self._ready_connection().execute(
            "SELECT review_id, dispatch_id, review_json FROM reviews WHERE run_id = ? ORDER BY created_at, review_id",
            (record.run_id,),
        ).fetchall()
        for review_row in reviews:
            review = json.loads(review_row["review_json"])
            target = review.get("review_target", {})
            lines.append(
                f"| `{review_row['review_id']}` | `{review_row['dispatch_id']}` | "
                f"`{review.get('verdict', '')}` | `{target.get('result_revision') or target.get('patch_sha256') or ''}` |"
            )
        reports = ensure_private_directory(self.state_dir / "reports")
        path = reports / f"run-{run_id}.md"
        atomic_write_private_text(path, "\n".join(lines) + "\n")
        return path

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            try:
                self._connection = sqlite3.connect(
                    self.database_path,
                    isolation_level=None,
                    check_same_thread=False,
                )
                self._connection.row_factory = sqlite3.Row
            except sqlite3.DatabaseError as exc:
                raise StateStoreCorruptionError(
                    f"cannot open state database at {self.database_path}; restore from a verified backup"
                ) from exc
        return self._connection

    def _ready_connection(self) -> sqlite3.Connection:
        if not self._initialized:
            self.initialize()
        return self._connect()

    @contextmanager
    def _transaction(self, connection: sqlite3.Connection) -> Iterator[None]:
        with self._connection_lock:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield
                connection.execute("COMMIT")
            except sqlite3.DatabaseError as exc:
                connection.execute("ROLLBACK")
                raise StateStoreError(f"authoritative state transaction failed: {exc}") from exc
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _migrate(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        row = connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        version = int(row["version"] or 0)
        if version > CURRENT_SCHEMA_VERSION:
            raise StateStoreMigrationError(
                f"state database schema {version} is newer than supported {CURRENT_SCHEMA_VERSION}; "
                "upgrade dispatcher before opening this state directory"
            )
        if version == 0:
            self._apply_v1(connection)
            version = 1
        if version == 1:
            self._apply_v2(connection)
            version = 2
        if version == 2:
            self._apply_v3(connection)
            version = 3
        if version == 3:
            self._apply_v4(connection)
            version = 4
        if version == 4:
            self._apply_v5(connection)
            version = 5
        if version == 5:
            self._apply_v6(connection)
            version = 6
        if version == 6:
            self._apply_v7(connection)

    def _apply_v1(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            f"""
            BEGIN IMMEDIATE;
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                config_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK(generation >= 1),
                record_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX runs_one_active_project ON runs(project_id)
            WHERE state NOT IN ('HALTED', 'FAILED', 'SUCCEEDED', 'CANCELLED');
            CREATE TABLE normalized_plans (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
                plan_digest TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                source_digest TEXT NOT NULL,
                generation INTEGER NOT NULL
            );
            CREATE TABLE steps (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                step_id TEXT NOT NULL,
                state TEXT NOT NULL,
                generation INTEGER NOT NULL,
                record_json TEXT NOT NULL,
                PRIMARY KEY(run_id, step_id)
            );
            CREATE TABLE dispatches (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                dispatch_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                state TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                prompt_sha256 TEXT NOT NULL,
                policy_digest TEXT NOT NULL,
                repository_id TEXT NOT NULL,
                repository_revision TEXT NOT NULL,
                runtime_session_id TEXT,
                process_id INTEGER,
                session_metadata_json TEXT,
                result_json TEXT,
                forwarding_payload TEXT,
                generation INTEGER NOT NULL,
                record_json TEXT NOT NULL,
                PRIMARY KEY(run_id, dispatch_id),
                UNIQUE(run_id, idempotency_key)
            );
            CREATE TABLE dispatch_payloads (
                run_id TEXT NOT NULL,
                dispatch_id TEXT NOT NULL,
                prompt TEXT NOT NULL,
                policy_json TEXT NOT NULL,
                result_json TEXT,
                forwarding_payload TEXT,
                process_id INTEGER,
                session_metadata_json TEXT,
                PRIMARY KEY(run_id, dispatch_id),
                FOREIGN KEY(run_id, dispatch_id)
                    REFERENCES dispatches(run_id, dispatch_id) ON DELETE CASCADE
            );
            CREATE TABLE sessions (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                pool TEXT NOT NULL,
                role_key TEXT NOT NULL,
                session_json TEXT NOT NULL,
                generation INTEGER NOT NULL,
                PRIMARY KEY(run_id, pool, role_key)
            );
            CREATE TABLE reviews (
                review_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                dispatch_id TEXT NOT NULL,
                review_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE artifacts (
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                dispatch_id TEXT,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, path)
            );
            CREATE TABLE operator_decisions (
                decision_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                request_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                answer TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, request_id)
            );
            CREATE TABLE leases (
                resource_key TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                owner_pid INTEGER NOT NULL,
                owner_host TEXT NOT NULL,
                run_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                recovery_approved_by TEXT
            );
            CREATE TABLE audit_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                kind TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                causation_id TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, sequence, event_id)
            );
            CREATE TABLE tool_versions (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                tool_name TEXT NOT NULL,
                version TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY(run_id, tool_name)
            );
            INSERT INTO schema_migrations(version, applied_at) VALUES (1, '{_utc_now()}');
            COMMIT;
            """
        )

    def _apply_v2(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            f"""
            BEGIN IMMEDIATE;
            CREATE TABLE baselines (
                project_id TEXT NOT NULL,
                plan_digest TEXT NOT NULL,
                source_digest TEXT NOT NULL,
                candidate_json TEXT NOT NULL,
                candidate_digest TEXT NOT NULL,
                operator_decision_ref TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                PRIMARY KEY(project_id, plan_digest)
            );
            INSERT INTO schema_migrations(version, applied_at) VALUES (2, '{_utc_now()}');
            COMMIT;
            """
        )

    def _apply_v3(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            f"""
            BEGIN IMMEDIATE;
            ALTER TABLE dispatch_payloads ADD COLUMN repository_before_json TEXT;
            ALTER TABLE dispatch_payloads ADD COLUMN repository_after_json TEXT;
            INSERT INTO schema_migrations(version, applied_at) VALUES (3, '{_utc_now()}');
            COMMIT;
            """
        )

    def _apply_v4(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            f"""
            BEGIN IMMEDIATE;
            CREATE TABLE workspace_groups (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                workspace_group_id TEXT NOT NULL,
                repo_id TEXT NOT NULL,
                state TEXT NOT NULL,
                base_revision TEXT NOT NULL,
                integration_branch TEXT NOT NULL,
                lease_owner_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                record_json TEXT NOT NULL,
                PRIMARY KEY(run_id, workspace_group_id)
            );
            CREATE INDEX workspace_groups_recovery ON workspace_groups(run_id, state);
            INSERT INTO schema_migrations(version, applied_at) VALUES (4, '{_utc_now()}');
            COMMIT;
            """
        )

    def _apply_v5(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            f"""
            BEGIN IMMEDIATE;
            CREATE TABLE baseline_approvals (
                project_id TEXT NOT NULL,
                plan_digest TEXT NOT NULL,
                source_digest TEXT NOT NULL,
                approval_json TEXT NOT NULL,
                approval_digest TEXT NOT NULL,
                operator_decision_ref TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                PRIMARY KEY(project_id, plan_digest, approval_digest)
            );
            CREATE INDEX baseline_approvals_latest ON baseline_approvals(project_id, plan_digest, approved_at);
            INSERT INTO schema_migrations(version, applied_at) VALUES (5, '{_utc_now()}');
            COMMIT;
            """
        )

    def _apply_v6(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            f"""
            BEGIN IMMEDIATE;
            ALTER TABLE dispatch_payloads ADD COLUMN authoritative_verification_json TEXT;
            INSERT INTO schema_migrations(version, applied_at) VALUES (6, '{_utc_now()}');
            COMMIT;
            """
        )

    def _apply_v7(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            f"""
            BEGIN IMMEDIATE;
            CREATE TABLE structured_git_commits (
                run_id TEXT NOT NULL,
                dispatch_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'PROPOSAL_RECEIVED', 'CHECKED', 'COMMIT_INTENT_PERSISTED',
                    'STAGED', 'COMMITTED', 'NO_COMMIT_FINALIZED',
                    'RECONCILIATION_REQUIRED'
                )),
                proposal_json TEXT NOT NULL,
                proposal_digest TEXT NOT NULL,
                checked_json TEXT,
                intent_json TEXT,
                stage_json TEXT,
                commit_json TEXT,
                result_revision TEXT,
                repository_after_json TEXT,
                failure_detail TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(run_id, dispatch_id),
                FOREIGN KEY(run_id, dispatch_id)
                    REFERENCES dispatches(run_id, dispatch_id) ON DELETE CASCADE
            );
            CREATE INDEX structured_git_recovery
                ON structured_git_commits(run_id, state);
            INSERT INTO schema_migrations(version, applied_at) VALUES (7, '{_utc_now()}');
            COMMIT;
            """
        )

    def _write_snapshot(
        self,
        connection: sqlite3.Connection,
        record: RunRecord,
        *,
        generation: int,
        sessions: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> None:
        record_json = record.model_dump_json()
        connection.execute(
            """
            INSERT INTO runs(run_id, project_id, config_digest, state, generation, record_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                project_id = excluded.project_id,
                config_digest = excluded.config_digest,
                state = excluded.state,
                generation = excluded.generation,
                record_json = excluded.record_json,
                updated_at = excluded.updated_at
            """,
            (
                record.run_id,
                record.project_id,
                record.config_digest,
                record.state.value,
                generation,
                record_json,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO normalized_plans(run_id, plan_digest, plan_json, source_digest, generation)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                plan_digest = excluded.plan_digest,
                plan_json = excluded.plan_json,
                source_digest = excluded.source_digest,
                generation = excluded.generation
            """,
            (
                record.run_id,
                record.plan_digest,
                record.plan.model_dump_json(),
                record.plan.source_digest,
                generation,
            ),
        )
        for step in record.steps.values():
            connection.execute(
                """
                INSERT INTO steps(run_id, step_id, state, generation, record_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id, step_id) DO UPDATE SET
                    state = excluded.state,
                    generation = excluded.generation,
                    record_json = excluded.record_json
                """,
                (record.run_id, step.step_id, step.state.value, generation, step.model_dump_json()),
            )
        for dispatch in record.dispatches.values():
            connection.execute(
                """
                INSERT INTO dispatches(
                    run_id, dispatch_id, step_id, attempt, state, idempotency_key,
                    prompt_sha256, policy_digest, repository_id, repository_revision,
                    runtime_session_id, generation, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, dispatch_id) DO UPDATE SET
                    state = excluded.state,
                    runtime_session_id = excluded.runtime_session_id,
                    generation = excluded.generation,
                    record_json = excluded.record_json
                """,
                (
                    record.run_id,
                    dispatch.dispatch_id,
                    dispatch.step_id,
                    dispatch.attempt,
                    dispatch.state.value,
                    dispatch.intent.idempotency_key,
                    dispatch.intent.prompt_sha256,
                    dispatch.intent.policy_digest,
                    dispatch.intent.repository.repo_id,
                    dispatch.intent.repository.base_revision,
                    dispatch.runtime_session_id,
                    generation,
                    dispatch.model_dump_json(),
                ),
            )
        for group in record.workspace_groups.values():
            connection.execute(
                """
                INSERT INTO workspace_groups(
                    run_id, workspace_group_id, repo_id, state, base_revision,
                    integration_branch, lease_owner_id, generation, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, workspace_group_id) DO UPDATE SET
                    state = excluded.state,
                    base_revision = excluded.base_revision,
                    integration_branch = excluded.integration_branch,
                    lease_owner_id = excluded.lease_owner_id,
                    generation = excluded.generation,
                    record_json = excluded.record_json
                """,
                (
                    record.run_id,
                    group.workspace_group_id,
                    group.repo_id,
                    group.state.value,
                    group.base_revision,
                    group.integration_branch,
                    group.lease_owner_id,
                    generation,
                    group.model_dump_json(),
                ),
            )
        connection.execute("DELETE FROM sessions WHERE run_id = ?", (record.run_id,))
        for pool, role_entries in sessions.items():
            for role_key, session in role_entries.items():
                connection.execute(
                    """
                    INSERT INTO sessions(run_id, pool, role_key, session_json, generation)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (record.run_id, pool, role_key, _json_text(session), generation),
                )

    def _write_dispatch_payload(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        dispatch_id: str,
        payload: DispatchPayload,
    ) -> None:
        cursor = connection.execute(
            """
            INSERT INTO dispatch_payloads(
                run_id, dispatch_id, prompt, policy_json, result_json,
                authoritative_verification_json, forwarding_payload, process_id, session_metadata_json,
                repository_before_json, repository_after_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, dispatch_id) DO UPDATE SET
                prompt = excluded.prompt,
                policy_json = excluded.policy_json,
                result_json = excluded.result_json,
                authoritative_verification_json = excluded.authoritative_verification_json,
                forwarding_payload = excluded.forwarding_payload,
                process_id = excluded.process_id,
                session_metadata_json = excluded.session_metadata_json,
                repository_before_json = excluded.repository_before_json,
                repository_after_json = excluded.repository_after_json
            """,
            (
                run_id,
                dispatch_id,
                redact_text(payload.prompt),
                _json_text(payload.policy),
                _json_text(payload.result) if payload.result is not None else None,
                _json_text(payload.authoritative_verification)
                if payload.authoritative_verification is not None
                else None,
                redact_text(payload.forwarding_payload) if payload.forwarding_payload is not None else None,
                payload.process_id,
                _json_text(payload.session_metadata) if payload.session_metadata is not None else None,
                _json_text(payload.repository_before) if payload.repository_before is not None else None,
                _json_text(payload.repository_after) if payload.repository_after is not None else None,
            ),
        )
        if cursor.rowcount != 1:
            raise StateStoreError(f"dispatch payload references unknown dispatch: {dispatch_id}")

    def _write_structured_git_final(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        dispatch_id: str,
        update: Mapping[str, Any],
    ) -> None:
        target = update.get("state")
        allowed = {
            "COMMITTED": {"STAGED"},
            "NO_COMMIT_FINALIZED": {"CHECKED"},
        }
        if target not in allowed:
            raise StateStoreError("structured Git final update has an invalid target state")
        row = connection.execute(
            "SELECT state FROM structured_git_commits WHERE run_id = ? AND dispatch_id = ?",
            (run_id, dispatch_id),
        ).fetchone()
        if row is None or row["state"] not in allowed[target]:
            raise StateStoreConflictError(
                f"structured Git final update cannot move from {row['state'] if row else 'missing'} to {target}"
            )
        connection.execute(
            """
            UPDATE structured_git_commits
            SET state = ?, commit_json = ?, result_revision = ?,
                repository_after_json = ?, updated_at = ?
            WHERE run_id = ? AND dispatch_id = ?
            """,
            (
                target,
                _json_text(update.get("commit")) if update.get("commit") is not None else None,
                update.get("result_revision"),
                _json_text(update.get("repository_after")),
                _utc_now(),
                run_id,
                dispatch_id,
            ),
        )

    def _lease_is_stale(self, heartbeat_at: str) -> bool:
        try:
            heartbeat = datetime.fromisoformat(heartbeat_at)
        except ValueError as exc:
            raise StateStoreCorruptionError("lease heartbeat is not a valid timestamp") from exc
        return heartbeat <= datetime.now(UTC) - timedelta(seconds=self.stale_after_seconds)

    @staticmethod
    def _lease_from_row(row: sqlite3.Row | None) -> Lease:
        if row is None:
            raise StateStoreCorruptionError("lease disappeared during acquisition")
        return Lease(
            resource_key=str(row["resource_key"]),
            owner_id=str(row["owner_id"]),
            owner_pid=int(row["owner_pid"]),
            owner_host=str(row["owner_host"]),
            run_id=str(row["run_id"]),
            acquired_at=datetime.fromisoformat(str(row["acquired_at"])),
            heartbeat_at=datetime.fromisoformat(str(row["heartbeat_at"])),
        )

    def _secure_database_files(self) -> None:
        for path in (
            self.database_path,
            self.database_path.with_name(f"{self.database_path.name}-wal"),
            self.database_path.with_name(f"{self.database_path.name}-shm"),
        ):
            if path.exists():
                path.chmod(OWNER_FILE_MODE)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _json_text(value: object) -> str:
    return json.dumps(redact_value(value or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
