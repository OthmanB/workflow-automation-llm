from __future__ import annotations

import json
from pathlib import Path

from helpers import create_fixture_project

from dispatcher.cli import main


def test_mock_run_never_invokes_real_opencode_and_refuses_premature_completion(
    tmp_path: Path,
) -> None:
    project = create_fixture_project(tmp_path, include_preflight=False)

    result = main(["run", "--config", str(project.config_path), "--mock", "--skip-smoke"])

    assert result == 1
    saved_state = json.loads((project.state / "state.json").read_text(encoding="utf-8"))
    assert saved_state["current_step"] == "mock-step"
    audit_events = [
        json.loads(line)
        for line in (project.state / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["kind"] for event in audit_events] == [
        "preflight",
        "dispatch",
        "response",
        "halt",
    ]
