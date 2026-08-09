"""Internal routing derived from strict schema-v1 supervisor commands."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .protocol import (
    AskOperatorCommand,
    DispatchCommand,
    HaltCommand,
    RequestCompletionCommand,
    SupervisorCommand,
)


class RouteError(ValueError):
    """A valid protocol command cannot be routed under project configuration."""


@dataclass(frozen=True)
class Route:
    """Dispatcher-internal route derived from an already validated command."""

    kind: str  # executor | reviewer | completion | halt | ask
    target: str = ""
    mode: str = "new"
    step: str = ""
    prompt_body: str = ""
    rationale: str = ""


def route_from_command(command: SupervisorCommand, config: Config) -> Route:
    """Resolve target role class from configuration, never command prose."""
    if isinstance(command, DispatchCommand):
        role_kind = config.role_kind(command.target_role)
        if role_kind == "supervisor":
            raise RouteError("dispatch target_role cannot be the supervisor")
        return Route(
            kind=role_kind,
            target=command.target_role,
            mode=command.session_mode,
            step=command.step_id,
            prompt_body=command.prompt,
            rationale=command.rationale or "",
        )
    if isinstance(command, AskOperatorCommand):
        return Route(
            kind="ask",
            step=command.step_id or "",
            prompt_body=command.question,
            rationale=command.rationale or "",
        )
    if isinstance(command, HaltCommand):
        return Route(kind="halt", prompt_body=command.reason)
    if isinstance(command, RequestCompletionCommand):
        return Route(kind="completion", rationale=command.rationale or "")
    raise RouteError(f"unsupported command type: {type(command).__name__}")
