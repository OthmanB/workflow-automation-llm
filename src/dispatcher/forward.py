"""Forwarding template — renders the supervisor inbox message after every slave response."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def render_forwarding_message(
    *,
    template: str,
    role_display: str,
    model_display: str,
    step: str,
    session_id: str,
    chat_response: str,
    evidence: list[str],
    context_pct: float = 0.0,
    tokens_used: int = 0,
    max_chars: int = 4000,
) -> str:
    """Render the forwarding template for the supervisor.

    The supervisor receives a compact summary: the slave's chat response
    (truncated), the list of new evidence files written this turn, and
    context-usage stats so it can decide when to fork or consolidate.
    """
    truncated = chat_response[:max_chars]
    if len(chat_response) > max_chars:
        truncated += f"\n... (truncated, {len(chat_response)} chars total)"

    pct = f"{context_pct:.0f}%" if context_pct > 0 else "?%"
    tk = f"{tokens_used // 1000}k" if tokens_used > 0 else "?k"

    body = template.replace("[response_content_chat]", truncated)
    body = body.replace("[executor_model_name]", model_display)

    # Append evidence list.
    lines = [body.strip(), ""]
    if evidence:
        lines.append("Evidence written this turn:")
        for e in evidence:
            lines.append(f"- {e}")
        lines.append("")

    lines.append(
        f"(session {session_id} · step {step} · {pct} context, {tk} used)"
    )

    return "\n".join(lines)
