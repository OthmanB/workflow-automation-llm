"""Derived status, support exports, retention, and redacted structured logging."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .scheduler import evaluate_readiness
from .security import atomic_write_private_text, ensure_private_directory, redact_text, redact_value
from .state_store import StateStore
from .workflow import ACTIVE_DISPATCH_STATES, RunStatus

_CONTEXT_FIELDS = ("project_id", "run_id", "dispatch_id", "step_id")


class JsonFormatter(logging.Formatter):
    """Render dispatcher logs as redacted JSON with explicit correlation fields."""

    def format(self, record: logging.LogRecord) -> str:
        context = getattr(record, "dispatcher_context", {})
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "function": record.funcName,
            "message": record.getMessage(),
            **{field: context.get(field) for field in _CONTEXT_FIELDS},
        }
        return json.dumps(redact_value(payload), ensure_ascii=False, sort_keys=True)


def configure_logging(level: str) -> None:
    """Configure the dispatcher logger without changing unrelated application loggers."""
    logger = logging.getLogger("dispatcher")
    logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level))
    logger.propagate = False


def log_event(logger: logging.Logger, message: str, **context: str | None) -> None:
    """Emit one correlated structured event using only caller-supplied safe IDs."""
    logger.info(message, extra={"dispatcher_context": context})


def status_snapshot(config: Config, store: StateStore, run_id: str | None = None) -> dict[str, Any]:
    """Build a redacted, derived operator status view from authoritative state."""
    loaded = store.load_run(run_id) if run_id is not None else store.latest_run(config.project_id)
    if loaded is None:
        return {"project_id": config.project_id, "run": None}
    record, generation = loaded
    readiness = evaluate_readiness(config, record)
    request = record.operator_request
    snapshot = {
        "project_id": record.project_id,
        "run": {
            "run_id": record.run_id,
            "state": record.state.value,
            "generation": generation,
            "terminal_outcome": record.state.value if record.state in _terminal_states() else None,
        },
        "ready_steps": [item.step_id for item in readiness if item.ready],
        "blocked_steps": [
            {"step_id": item.step_id, "reasons": list(item.reasons)}
            for item in readiness
            if not item.ready
        ],
        "active_dispatches": [
            {
                "dispatch_id": dispatch.dispatch_id,
                "batch_id": dispatch.batch_id,
                "step_id": dispatch.step_id,
                "role_key": dispatch.role_key,
                "state": dispatch.state.value,
                "session_id": dispatch.runtime_session_id,
            }
            for dispatch in record.dispatches.values()
            if dispatch.state in ACTIVE_DISPATCH_STATES
        ],
        "batches": [
            {
                "batch_id": batch.batch_id,
                "state": batch.state.value,
                "dispatch_ids": list(batch.dispatch_ids),
                "failed_dispatch_ids": list(batch.failed_dispatch_ids),
            }
            for batch in record.batches.values()
        ],
        "waiting_operator": (
            {
                "request_id": request.request_id,
                "kind": request.kind,
                "step_id": request.step_id,
                "allowed_answers": request.allowed_answers,
                "expires_at": request.expires_at,
                "required_role": request.required_role,
                "question": redact_text(request.question),
            }
            if request is not None
            else None
        ),
        "usage": record.usage.model_dump(mode="json"),
        "leases": [
            {
                "resource_key": lease.resource_key,
                "owner_id": lease.owner_id,
                "run_id": lease.run_id,
                "acquired_at": lease.acquired_at.isoformat(),
                "heartbeat_at": lease.heartbeat_at.isoformat(),
            }
            for lease in store.leases_for_run(record.run_id)
        ],
    }
    return redact_value(snapshot)


def export_support_bundle(config: Config, store: StateStore, run_id: str) -> Path:
    """Export redacted derived support artifacts without copying authoritative state."""
    record, generation = store.load_run(run_id)
    directory = ensure_private_directory(Path(config.state_dir) / "support" / f"{run_id}-{generation}")
    status = status_snapshot(config, store, run_id)
    report = store.export_run_report(run_id)
    audit = store.export_audit_jsonl(run_id)
    atomic_write_private_text(directory / "status.json", json.dumps(status, indent=2, sort_keys=True) + "\n")
    atomic_write_private_text(directory / "report.md", redact_text(report.read_text(encoding="utf-8")))
    atomic_write_private_text(directory / "audit.jsonl", redact_text(audit.read_text(encoding="utf-8")))
    atomic_write_private_text(
        directory / "manifest.json",
        json.dumps(
            {
                "run_id": record.run_id,
                "generation": generation,
                "plan_digest": record.plan_digest,
                "artifacts": ["status.json", "report.md", "audit.jsonl"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return directory


@dataclass(frozen=True)
class RetentionAction:
    """One derived artifact moved or removed by explicit retention maintenance."""

    action: str
    path: str


def prune_derived_artifacts(config: Config, store: StateStore) -> tuple[RetentionAction, ...]:
    """Apply configured retention without deleting SQLite or active-run artifacts."""
    policy = config.observability.retention
    active = store.active_run(config.project_id)
    active_run_id = active[0].run_id if active is not None else None
    state_dir = Path(config.state_dir)
    archive = ensure_private_directory(policy.archive_directory)
    actions: list[RetentionAction] = []
    transcripts = state_dir / "transcripts"
    if transcripts.is_dir():
        for run_directory in sorted(path for path in transcripts.iterdir() if path.is_dir()):
            if run_directory.name == active_run_id:
                continue
            actions.extend(
                _prune_files(
                    run_directory,
                    policy.max_transcripts_per_run,
                    archive / "transcripts" / run_directory.name,
                    policy.mode,
                )
            )
    for source, limit, category in (
        (state_dir / "reports", policy.max_reports, "reports"),
        (state_dir / "audit", policy.max_audit_exports, "audit"),
        (state_dir / "support", policy.max_support_bundles, "support"),
    ):
        actions.extend(_prune_files(source, limit, archive / category, policy.mode, active_run_id))
    actions.extend(_prune_files(archive, policy.max_archived_artifacts, archive, "delete"))
    return tuple(actions)


def _prune_files(
    source: Path,
    limit: int,
    archive: Path,
    mode: str,
    active_run_id: str | None = None,
) -> list[RetentionAction]:
    if not source.is_dir():
        return []
    candidates = [path for path in source.iterdir() if path.is_file() or path.is_dir()]
    if active_run_id is not None:
        candidates = [path for path in candidates if active_run_id not in path.name]
    ordered = sorted(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    actions: list[RetentionAction] = []
    for path in ordered[limit:]:
        if mode == "archive" and source != archive:
            destination = ensure_private_directory(archive) / path.name
            if destination.exists():
                destination = archive / f"{path.stem}-{path.stat().st_mtime_ns}{path.suffix}"
            shutil.move(str(path), destination)
            actions.append(RetentionAction("archived", str(destination)))
        else:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            actions.append(RetentionAction("deleted", str(path)))
    return actions


def _terminal_states() -> frozenset[RunStatus]:
    return frozenset({RunStatus.HALTED, RunStatus.FAILED, RunStatus.SUCCEEDED, RunStatus.CANCELLED})
