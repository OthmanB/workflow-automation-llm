"""Derived legacy state artifacts and the SQLite authority factory."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .security import (
    atomic_write_private_text,
    ensure_private_directory,
    redact_text,
    redact_value,
    write_private_text_exclusive,
)

if TYPE_CHECKING:
    from .config import Config
    from .state_store import StateStore
    from .workflow import RunRecord


def open_state_store(config: "Config") -> "StateStore":
    """Create the configured SQLite authority for explicit Phase 3 operations."""
    from .state_store import StateStore

    return StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default (empty) structures
# ---------------------------------------------------------------------------

def _empty_state() -> dict[str, Any]:
    return {
        "project": "",
        "plan": "",
        "current_step": "",
        "steps": {},          # step_id -> {status, round, ...}
        "last_decision_hash": "",
        "last_response_hash": "",
    }


def _empty_sessions() -> dict[str, Any]:
    return {
        "supervisor": {},
        "executors": {},
        "reviewers": {},
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_state(state_dir: str) -> dict[str, Any]:
    """Load state.json, returning empty defaults if the file does not exist."""
    p = Path(state_dir) / "state.json"
    if not p.exists():
        return _empty_state()
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state_dir: str, data: dict[str, Any]) -> None:
    """Atomically write state.json (write to temp, rename)."""
    d = ensure_private_directory(state_dir)
    atomic_write_private_text(
        d / "state.json", json.dumps(redact_value(data), indent=2, ensure_ascii=False)
    )


def load_sessions(state_dir: str) -> dict[str, Any]:
    """Load sessions.json, returning empty defaults if absent."""
    p = Path(state_dir) / "sessions.json"
    if not p.exists():
        return _empty_sessions()
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save_sessions(state_dir: str, data: dict[str, Any]) -> None:
    """Atomically write sessions.json."""
    d = ensure_private_directory(state_dir)
    atomic_write_private_text(
        d / "sessions.json", json.dumps(redact_value(data), indent=2, ensure_ascii=False)
    )


def session_registry_get(
    sessions: dict[str, Any],
    pool: str,
    key: str,
) -> dict[str, Any]:
    """Return the stored session info for a role, or an empty dict."""
    return sessions.get(pool, {}).get(key, {})


def session_registry_set(
    sessions: dict[str, Any],
    pool: str,
    key: str,
    *,
    session_id: str = "",
    model: str = "",
    variant: str = "",
    context_pct: float = 0.0,
    tokens_used: int = 0,
    working_directory: str = "",
    opencode_version: str = "",
    parent_session_id: str = "",
) -> None:
    """Upsert a role into the session registry."""
    sessions.setdefault(pool, {})[key] = {
        "session_id": session_id,
        "model": model,
        "variant": variant,
        "context_pct": context_pct,
        "tokens_used": tokens_used,
        "working_directory": working_directory,
        "opencode_version": opencode_version,
        "parent_session_id": parent_session_id,
        "status": "active",
    }


def save_transcript(
    state_dir: str,
    label: str,
    content: str,
) -> str:
    """Save a transcript entry as a timestamped .md file.

    Returns the path (relative to the state dir).
    """
    import time
    ts = int(time.time())
    safe_label = label.replace("/", "_").replace(" ", "_")
    fname = f"{safe_label}_{ts}_{uuid.uuid4().hex}.md"
    d = ensure_private_directory(Path(state_dir) / "transcripts")
    path = d / fname
    write_private_text_exclusive(path, redact_text(content))
    return str(path.relative_to(Path(state_dir).parent))


def save_run_record(state_dir: str, record: "RunRecord") -> None:
    """Write a derived compatibility checkpoint; it is never authoritative."""
    d = ensure_private_directory(state_dir)
    atomic_write_private_text(
        d / "run-record.json",
        json.dumps(redact_value(record.model_dump(mode="json")), indent=2, ensure_ascii=False),
    )


def load_run_record(state_dir: str) -> "RunRecord | None":
    """Load a derived compatibility checkpoint when the SQLite authority is absent."""
    from .workflow import RunRecord

    path = Path(state_dir) / "run-record.json"
    if not path.exists():
        return None
    try:
        return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise ValueError(f"invalid schema-v1 run record at {path}: {exc}") from exc
