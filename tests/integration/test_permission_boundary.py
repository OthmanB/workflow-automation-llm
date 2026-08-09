from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from helpers import create_fixture_project

from dispatcher import sessions
from dispatcher.permissions import (
    compile_effective_policy,
    generate_opencode_config,
    should_auto_approve,
)
from dispatcher.sessions import OpenCodeProcessError, SessionResult, run_session


def test_executor_write_allow_reaches_the_isolated_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls_path = _install_fake_opencode(tmp_path, monkeypatch)
    policy = generate_opencode_config({"*": "deny", "write": "allow"})

    result = _run_probe(tmp_path, policy, model="fixture/executor", auto_approve=should_auto_approve(policy["permission"]))

    assert json.loads(result.chat_response) == {"action": "write", "decision": "allow"}
    call = json.loads(calls_path.read_text(encoding="utf-8").strip())
    assert call["permission_probe"] == {"action": "write", "decision": "allow"}
    assert "--auto" in call["argv"]


def test_compiled_project_repository_role_and_dispatch_policy_reaches_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_fixture_project(tmp_path)
    calls_path = _install_fake_opencode(tmp_path, monkeypatch)
    policy = generate_opencode_config(
        compile_effective_policy(
            project.config,
            repo_id="fixture-repo",
            role_key="terra",
            dispatch_authorized_actions=["inspect", "modify"],
        )
    )

    result = _run_probe(
        tmp_path,
        policy,
        model="fixture/executor",
        auto_approve=should_auto_approve(policy["permission"]),
    )

    assert json.loads(result.chat_response) == {"action": "write", "decision": "allow"}
    call = json.loads(calls_path.read_text(encoding="utf-8").strip())
    assert call["policy"] == policy


@pytest.mark.parametrize(
    ("model", "decision", "expects_auto"),
    [("fixture/reviewer", "deny", True), ("fixture/executor", "ask", False)],
)
def test_denied_and_ask_writes_are_not_silently_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    decision: str,
    expects_auto: bool,
) -> None:
    calls_path = _install_fake_opencode(tmp_path, monkeypatch)
    policy = generate_opencode_config({"*": "deny", "write": decision})

    with pytest.raises(OpenCodeProcessError, match=f"permission probe write was {decision}"):
        _run_probe(tmp_path, policy, model=model, auto_approve=should_auto_approve(policy["permission"]))

    call = json.loads(calls_path.read_text(encoding="utf-8").strip())
    assert call["permission_probe"] == {"action": "write", "decision": decision}
    assert ("--auto" in call["argv"]) is expects_auto


def _run_probe(
    tmp_path: Path,
    policy: dict[str, object],
    *,
    model: str,
    auto_approve: bool,
) -> SessionResult:
    return run_session(
        prompt=json.dumps({"permission_probe": "write"}),
        model=model,
        variant="high",
        session_id=None,
        mode="new",
        workdir=tmp_path,
        title="permission-probe",
        auto_approve=auto_approve,
        timeout_seconds=5,
        termination_grace_seconds=1,
        max_output_bytes=4096,
        state_dir=tmp_path / "state",
        permission_config=policy,
    )


def _install_fake_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = Path(__file__).parents[1] / "fixtures" / "opencode" / "fake_cli.py"
    fake = tmp_path / "opencode"
    shutil.copy2(source, fake)
    fake.chmod(0o700)
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(fake))
    return tmp_path / "calls.jsonl"
