"""Strict schema-v1 supervisor command protocol."""

from __future__ import annotations

import json
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, ValidationError

from .config import ContractModel, Identifier


class ProtocolError(ValueError):
    """Supervisor output does not satisfy the executable command protocol."""


class DispatchCommand(ContractModel):
    """Request a single executor or reviewer dispatch by logical role key."""

    protocol_version: Literal[1]
    action: Literal["dispatch"]
    step_id: Identifier
    target_role: Identifier
    session_mode: Literal["new", "resume", "fork"]
    prompt: Annotated[str, Field(min_length=1, max_length=50_000)]
    rationale: Annotated[str, Field(min_length=1, max_length=5000)] | None = None


class AskOperatorCommand(ContractModel):
    """Request durable operator input without launching a session."""

    protocol_version: Literal[1]
    action: Literal["ask_operator"]
    step_id: Identifier | None = None
    question: Annotated[str, Field(min_length=1, max_length=10_000)]
    rationale: Annotated[str, Field(min_length=1, max_length=5000)] | None = None


class HaltCommand(ContractModel):
    """Request an explicit halted state with a durable reason."""

    protocol_version: Literal[1]
    action: Literal["halt"]
    reason: Annotated[str, Field(min_length=1, max_length=10_000)]


class RequestCompletionCommand(ContractModel):
    """Ask the dispatcher to evaluate, rather than assume, completion."""

    protocol_version: Literal[1]
    action: Literal["request_completion"]
    rationale: Annotated[str, Field(min_length=1, max_length=5000)] | None = None


SupervisorCommand: TypeAlias = Annotated[
    DispatchCommand | AskOperatorCommand | HaltCommand | RequestCompletionCommand,
    Field(discriminator="action"),
]
_SUPERVISOR_COMMAND_ADAPTER: TypeAdapter[SupervisorCommand] = TypeAdapter(SupervisorCommand)


def parse_supervisor_command(text: str) -> SupervisorCommand:
    """Parse exactly one JSON command, rejecting prose and duplicate object keys."""
    stripped = text.lstrip()
    if not stripped:
        raise ProtocolError("supervisor response must start with one JSON command object")
    try:
        decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_object_keys)
        payload, end = decoder.raw_decode(stripped)
    except (json.JSONDecodeError, DuplicateKeyError) as exc:
        raise ProtocolError(f"invalid supervisor command JSON: {exc}") from exc
    if stripped[end:].strip():
        raise ProtocolError("supervisor command must not contain trailing prose or a second object")
    if not isinstance(payload, dict):
        raise ProtocolError("supervisor command must be a JSON object")
    try:
        return _SUPERVISOR_COMMAND_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise ProtocolError(_format_validation_error(exc)) from exc


def diagnostic_command_hint(text: str) -> str:
    """Return a non-executable repair hint without routing natural-language text."""
    prefix = text.strip().splitlines()[0] if text.strip() else "<empty response>"
    return (
        "Supervisor response was not executed. Reply with exactly one schema-v1 JSON object; "
        f"received prefix: {prefix[:160]!r}"
    )


class DuplicateKeyError(ValueError):
    """JSON object contained a duplicate key and is therefore ambiguous."""


def _reject_duplicate_object_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _format_validation_error(exc: ValidationError) -> str:
    errors = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        errors.append(f"{location}: {error['msg']}")
    return "invalid supervisor command: " + "; ".join(errors)
