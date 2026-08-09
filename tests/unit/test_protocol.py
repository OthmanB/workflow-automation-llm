from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import create_fixture_project

from dispatcher.dispatch import RouteError, route_from_command
from dispatcher.protocol import (
    BatchDispatchCommand,
    DispatchCommand,
    ProtocolError,
    diagnostic_command_hint,
    parse_supervisor_command,
)


def _dispatch_command(**overrides: object) -> dict[str, object]:
    command: dict[str, object] = {
        "protocol_version": 1,
        "action": "dispatch",
        "step_id": "prepare-fixture",
        "target_role": "terra",
        "session_mode": "new",
        "prompt": "Perform the fixture task.",
    }
    command.update(overrides)
    return command


def test_strict_json_command_parses_and_routes_by_configured_role(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)

    command = parse_supervisor_command(json.dumps(_dispatch_command()))
    route = route_from_command(command, project.config)

    assert isinstance(command, DispatchCommand)
    assert route.kind == "executor"
    assert route.target == "terra"
    assert route.prompt_body == "Perform the fixture task."


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Executor: Terra\nPerform work",
        '{"protocol_version":1,"action":"dispatch","action":"halt"}',
        '{"protocol_version":1,"action":"dispatch","step_id":"prepare-fixture","target_role":"terra","session_mode":"new","prompt":"x","session_id":"ses_fixture"}',
        '{"protocol_version":1,"action":"batch"}',
        '{"protocol_version":1,"action":"dispatch","step_id":"prepare-fixture","target_role":"terra","session_mode":"new","prompt":"x"} trailing prose',
        '{"protocol_version":1,"action":"dispatch","step_id":"prepare-fixture","target_role":"terra","session_mode":"new","prompt":"x"} {"action":"halt"}',
        '{"protocol_version":1,"action":"dispatch","step_id":"prepare-fixture","target_role":"terra","session_mode":"new","prompt":"x" // comment\n}',
    ],
)
def test_malformed_or_unsupported_command_never_parses(text: str) -> None:
    with pytest.raises(ProtocolError):
        parse_supervisor_command(text)


def test_dispatch_cannot_target_configured_supervisor(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    command = parse_supervisor_command(json.dumps(_dispatch_command(target_role="supervisor")))

    with pytest.raises(RouteError, match="cannot be the supervisor"):
        route_from_command(command, project.config)


def test_protocol_v2_batch_requires_unique_independently_valid_children() -> None:
    command = parse_supervisor_command(
        json.dumps(
            {
                "protocol_version": 2,
                "action": "dispatch_batch",
                "children": [
                    {
                        "step_id": "prepare-fixture",
                        "target_role": "terra",
                        "session_mode": "new",
                        "prompt": "first",
                    },
                    {
                        "step_id": "second-fixture",
                        "target_role": "terra",
                        "session_mode": "new",
                        "prompt": "second",
                    },
                ],
            }
        )
    )

    assert isinstance(command, BatchDispatchCommand)
    with pytest.raises(ProtocolError, match="must not target the same step"):
        parse_supervisor_command(
            json.dumps(
                {
                    "protocol_version": 2,
                    "action": "dispatch_batch",
                    "children": [
                        {
                            "step_id": "prepare-fixture",
                            "target_role": "terra",
                            "session_mode": "new",
                            "prompt": "first",
                        },
                        {
                            "step_id": "prepare-fixture",
                            "target_role": "terra",
                            "session_mode": "new",
                            "prompt": "duplicate",
                        },
                    ],
                }
            )
        )


@pytest.mark.parametrize("text", ["{", "[]", "null", "not json", "{}"])
def test_protocol_fuzz_smoke_rejects_invalid_json_shapes(text: str) -> None:
    with pytest.raises(ProtocolError):
        parse_supervisor_command(text)


def test_diagnostic_hint_does_not_turn_prose_into_a_route() -> None:
    hint = diagnostic_command_hint("Executor: use terra")

    assert "was not executed" in hint
    assert "Executor: use terra" in hint
