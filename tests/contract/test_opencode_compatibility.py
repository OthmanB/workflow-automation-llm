from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from dispatcher.sessions import (
    OpenCodeJsonlDecoder,
    OpenCodeProcessError,
    OpenCodeProtocolError,
    _parse_json_output,
    classify_provider_failure,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"


def _compatibility_config() -> dict[str, str]:
    with PYPROJECT_PATH.open("rb") as handle:
        project = tomllib.load(handle)
    return project["tool"]["dispatcher"]["opencode"]


def _fixture_dir() -> Path:
    config = _compatibility_config()
    return PROJECT_ROOT / config["fixture-path"]


def _jsonl_events(path: Path) -> list[dict[str, Any]]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def test_supported_version_matches_fixture_directory() -> None:
    config = _compatibility_config()

    assert config["supported-version"] == "1.18.11"
    assert config["source-tag"] == "v1.18.11"
    assert _fixture_dir().name == config["supported-version"]


@pytest.mark.parametrize(
    "fixture_name",
    [
        "run-new-session.jsonl",
        "run-resumed-session.jsonl",
        "run-forked-session.jsonl",
        "run-tool-events.jsonl",
        "run-error.jsonl",
    ],
)
def test_jsonl_fixtures_are_valid(fixture_name: str) -> None:
    events = _jsonl_events(_fixture_dir() / fixture_name)

    assert events
    assert all(event["sessionID"].startswith("ses_fixture_") for event in events)
    assert all(isinstance(event["timestamp"], int) for event in events)


def test_fixture_set_covers_supported_event_types() -> None:
    event_types = set()
    for path in _fixture_dir().glob("run-*.jsonl"):
        if path.name == "run-malformed.jsonl":
            continue
        event_types.update(event["type"] for event in _jsonl_events(path))

    assert event_types == {
        "error",
        "reasoning",
        "step_finish",
        "step_start",
        "text",
        "tool_use",
    }


def test_structured_session_fixtures_are_valid_json() -> None:
    session_list = json.loads(
        (_fixture_dir() / "session-list.json").read_text(encoding="utf-8")
    )
    session_export = json.loads(
        (_fixture_dir() / "session-export-sanitized.json").read_text(
            encoding="utf-8"
        )
    )
    timeout = json.loads(
        (_fixture_dir() / "run-timeout.json").read_text(encoding="utf-8")
    )
    nonzero_exit = json.loads(
        (_fixture_dir() / "run-nonzero-exit.json").read_text(
            encoding="utf-8"
        )
    )
    import_output = (_fixture_dir() / "session-import-output.txt").read_text(
        encoding="utf-8"
    )

    assert session_list[0]["id"] == "ses_fixture_list"
    assert session_list[0]["directory"] == "/fixture/project"
    assert session_export["info"]["id"] == "ses_fixture_export"
    assert timeout["expected_exit_kind"] == "timeout"
    assert nonzero_exit["exit_code"] == 1
    assert nonzero_exit["stdout_fixture"] == "run-error.jsonl"
    assert import_output == "Imported session: ses_fixture_export\n"


def test_fixtures_contain_only_synthetic_session_ids_and_paths() -> None:
    fixture_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in _fixture_dir().iterdir()
        if path.is_file() and path.name != "README.md"
    )
    session_ids = set(re.findall(r"ses_[A-Za-z0-9_]+", fixture_text))
    absolute_paths = set(re.findall(r'"(/[^"]+)"', fixture_text))

    assert session_ids
    assert all(session_id.startswith("ses_fixture_") for session_id in session_ids)
    assert all(path.startswith("/fixture/") for path in absolute_paths)
    assert "/Users/" not in fixture_text
    for secret_marker in (
        "API_KEY",
        "AUTHORIZATION",
        "BEARER ",
        "PASSWORD",
        "PRIVATE KEY",
    ):
        assert secret_marker not in fixture_text.upper()


def test_current_decoder_supports_opencode_1_18_11_success_stream() -> None:
    stdout = (_fixture_dir() / "run-new-session.jsonl").read_text(
        encoding="utf-8"
    )

    _events, chat, usage, session_id = _parse_json_output(stdout)

    assert session_id == "ses_fixture_new"
    assert chat == "FIXTURE_OK"
    assert usage == {
        "total": 13,
        "input": 8,
        "output": 5,
        "reasoning": 0,
        "cache": {"read": 0, "write": 0},
    }


@pytest.mark.parametrize(
    ("fixture_name", "require_response", "expected_session"),
    [
        ("run-new-session.jsonl", True, "ses_fixture_new"),
        ("run-resumed-session.jsonl", True, "ses_fixture_resume"),
        ("run-forked-session.jsonl", True, "ses_fixture_fork"),
        ("run-tool-events.jsonl", False, "ses_fixture_tool"),
    ],
)
def test_decoder_accepts_every_supported_success_fixture(
    fixture_name: str,
    require_response: bool,
    expected_session: str,
) -> None:
    decoder = OpenCodeJsonlDecoder(max_output_bytes=4096)
    for line_number, line in enumerate(
        (_fixture_dir() / fixture_name).read_text(encoding="utf-8").splitlines(), start=1
    ):
        decoder.consume_line(line, line_number=line_number)

    _raw, _chat, _usage, session_id, _cost = decoder.finish(require_response=require_response)

    assert session_id == expected_session


def test_decoder_rejects_error_and_malformed_fixture_events() -> None:
    error_decoder = OpenCodeJsonlDecoder(max_output_bytes=4096)
    error_decoder.consume_line(
        (_fixture_dir() / "run-error.jsonl").read_text(encoding="utf-8"), line_number=1
    )
    with pytest.raises(OpenCodeProcessError, match="ProviderAuthError"):
        error_decoder.finish(require_response=False)

    malformed_decoder = OpenCodeJsonlDecoder(max_output_bytes=4096)
    malformed_line = (_fixture_dir() / "run-malformed.jsonl").read_text(encoding="utf-8").splitlines()[0]
    with pytest.raises(OpenCodeProtocolError, match="malformed"):
        malformed_decoder.consume_line(malformed_line, line_number=1)


@pytest.mark.parametrize(
    ("name", "message", "expected"),
    [
        ("RateLimitError", "too many requests", "rate_limit"),
        ("ContextOverflowError", "context overflow", "context_overflow"),
        ("ProviderAuthError", "invalid api key", "authentication"),
        ("QuotaExceededError", "quota exhausted", "quota"),
        ("UnknownError", "connection reset", "connection"),
        ("UnknownError", "unclassified failure", "unknown"),
    ],
)
def test_provider_error_classification_is_explicit(name: str, message: str, expected: str) -> None:
    assert classify_provider_failure(name, message) == expected
