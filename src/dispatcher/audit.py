"""Audit log: append-only JSONL trail of every dispatch, response, and decision."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .security import open_private_text, redact_value

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_event(state_dir: str, event: dict[str, Any]) -> None:
    """Append one audit event to <state_dir>/audit.jsonl."""
    path = Path(state_dir) / "audit.jsonl"
    event.setdefault("timestamp", _utc_now())
    with open_private_text(path) as fh:
        fh.write(json.dumps(redact_value(event), ensure_ascii=False) + "\n")
    logger.debug("audit: %s", event.get("kind", "?"))


# ---------------------------------------------------------------------------
# Convenience helpers for common event kinds
# ---------------------------------------------------------------------------

def dispatch_sent(
    state_dir: str,
    role: str,
    target: str,
    step: str,
    mode: str,
    session_id: str,
    prompt_hash: str,
) -> None:
    write_event(state_dir, {
        "kind": "dispatch",
        "role": role,
        "target": target,
        "step": step,
        "mode": mode,
        "session_id": session_id,
        "prompt_hash": prompt_hash,
    })


def response_received(
    state_dir: str,
    role: str,
    target: str,
    step: str,
    session_id: str,
    evidence: list[str],
    usage: dict[str, Any],
    exit_code: int,
) -> None:
    write_event(state_dir, {
        "kind": "response",
        "role": role,
        "target": target,
        "step": step,
        "session_id": session_id,
        "evidence": evidence,
        "usage": usage,
        "exit_code": exit_code,
    })


def preflight_result(
    state_dir: str,
    passed: bool,
    checks: dict[str, Any],
) -> None:
    write_event(state_dir, {
        "kind": "preflight",
        "passed": passed,
        "checks": checks,
    })


def operator_decision(
    state_dir: str,
    question: str,
    answer: str,
) -> None:
    write_event(state_dir, {
        "kind": "operator_decision",
        "question": question[:500],
        "answer": answer[:500],
    })


def halt(state_dir: str, reason: str) -> None:
    write_event(state_dir, {
        "kind": "halt",
        "reason": reason,
    })
