from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from dispatcher import sessions
from dispatcher.sessions import (
    OpenCodeJsonlDecoder,
    OpenCodeProcessError,
    OpenCodeProcessIdentityError,
    OpenCodeProtocolError,
    OpenCodeSessionError,
    OpenCodeTimeoutError,
    OpenCodeVersionError,
    SessionLifecycleCallbacks,
    _session_exists,
    build_child_environment,
    cancel_process_group,
    list_sessions,
    run_session,
    validate_session_reference,
)


def _write_fake_opencode(tmp_path: Path, run_body: str, *, session_list: list[dict[str, object]] | None = None) -> Path:
    binary = tmp_path / "opencode"
    session_list_json = json.dumps(session_list or [])
    binary.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

if sys.argv[1:] == ["--version"]:
    print("opencode 1.18.11")
elif sys.argv[1:3] == ["session", "list"]:
    print({session_list_json!r})
elif len(sys.argv) > 1 and sys.argv[1] == "run":
{run_body}
else:
    raise SystemExit(64)
""",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return binary


def _success_body(session_id: str = "ses_fixture_new") -> str:
    return f"""    assert "-" not in sys.argv
    prompt = sys.stdin.read()
    assert prompt == "fixture prompt"
    print(json.dumps({{"type": "text", "timestamp": 1, "sessionID": "{session_id}", "part": {{"type": "text", "text": "FIXTURE_OK"}}}}))
    print(json.dumps({{"type": "step_finish", "timestamp": 2, "sessionID": "{session_id}", "part": {{"type": "step-finish", "cost": 0.01, "tokens": {{"total": 3, "input": 2, "output": 1, "reasoning": 0}}}}}}))
"""


def _run_kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "prompt": "fixture prompt",
        "model": "fixture/model",
        "variant": "high",
        "session_id": None,
        "mode": "new",
        "workdir": tmp_path,
        "title": "fixture",
        "auto_approve": False,
        "timeout_seconds": 5,
        "termination_grace_seconds": 1,
        "max_output_bytes": 4096,
        "state_dir": tmp_path / "state",
    }


def test_run_session_streams_validated_output_to_private_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_body = """    policy = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
    assert policy == {"permission": {"*": "deny"}}
    assert "UNRELATED_SECRET" not in os.environ
""" + _success_body()
    binary = _write_fake_opencode(tmp_path, run_body)
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(binary))
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-reach-child")

    kwargs = _run_kwargs(tmp_path)
    kwargs["permission_config"] = {"permission": {"*": "deny"}}
    result = run_session(**kwargs)

    assert result.session_id == "ses_fixture_new"
    assert result.chat_response == "FIXTURE_OK"
    assert result.usage == {"total": 3, "input": 2, "output": 1, "reasoning": 0}
    assert result.cost == 0.01
    assert "FIXTURE_OK" in Path(result.stdout_log_path).read_text(encoding="utf-8")
    assert stat.S_IMODE(Path(result.stdout_log_path).stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "state" / "opencode-events").stat().st_mode) == 0o700


def test_lifecycle_callbacks_run_before_prompt_and_once_for_session_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _write_fake_opencode(tmp_path, _success_body())
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(binary))
    observed: list[tuple[str, object]] = []
    kwargs = _run_kwargs(tmp_path)
    kwargs["lifecycle"] = SessionLifecycleCallbacks(
        lambda process_id, process_create_time: observed.append(
            ("process", (process_id, process_create_time))
        ),
        lambda session_id: observed.append(("session", session_id)),
    )

    result = run_session(**kwargs)

    assert result.session_id == "ses_fixture_new"
    assert [kind for kind, _value in observed] == ["process", "session"]
    process_id, process_create_time = observed[0][1]
    assert isinstance(process_id, int)
    assert isinstance(process_create_time, float)
    assert observed[1][1] == "ses_fixture_new"


def test_process_callback_failure_terminates_the_started_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_body = """    import time
    time.sleep(60)
"""
    binary = _write_fake_opencode(tmp_path, run_body)
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(binary))
    process_ids: list[int] = []

    def reject_start(process_id: int, _process_create_time: float) -> None:
        process_ids.append(process_id)
        raise RuntimeError("durable process transition failed")

    kwargs = _run_kwargs(tmp_path)
    kwargs["lifecycle"] = SessionLifecycleCallbacks(reject_start, lambda _session_id: None)
    with pytest.raises(RuntimeError, match="durable process transition failed"):
        run_session(**kwargs)

    assert len(process_ids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(process_ids[0], 0)


def test_run_session_rejects_structured_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_body = """    sys.stdin.read()
    print(json.dumps({"type": "error", "timestamp": 1, "sessionID": "ses_fixture_error", "error": {"name": "ProviderAuthError", "data": {"message": "token=secret-value"}}}))
"""
    binary = _write_fake_opencode(tmp_path, run_body)
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(binary))

    with pytest.raises(OpenCodeProcessError, match=r"token=\[REDACTED\]") as captured:
        run_session(**_run_kwargs(tmp_path))

    assert captured.value.exit_code == 0
    assert captured.value.stderr_log_path
    assert "secret-value" not in Path(captured.value.stdout_log_path).read_text(encoding="utf-8")


def test_run_session_terminates_the_process_group_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_body = """    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.path.join(os.environ["TMPDIR"], "child-pid"), "w", encoding="utf-8") as handle:
        handle.write(str(child.pid))
    sys.stdin.read()
    import time
    time.sleep(60)
"""
    binary = _write_fake_opencode(tmp_path, run_body)
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(binary))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    kwargs = _run_kwargs(tmp_path)
    kwargs["timeout_seconds"] = 1

    with pytest.raises(OpenCodeTimeoutError):
        run_session(**kwargs)

    child_pid = int((tmp_path / "child-pid").read_text(encoding="utf-8"))
    time.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_run_session_rejects_output_above_the_configured_memory_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_body = """    sys.stdin.read()
    for _ in range(300):
        print(json.dumps({"type": "text", "timestamp": 1, "sessionID": "ses_fixture_large", "part": {"type": "text", "text": "x" * 32}}))
"""
    binary = _write_fake_opencode(tmp_path, run_body)
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(binary))
    kwargs = _run_kwargs(tmp_path)
    kwargs["max_output_bytes"] = 1024

    with pytest.raises(OpenCodeProtocolError, match="output limit"):
        run_session(**kwargs)


def test_session_list_and_registry_validation_use_exact_structured_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    binary = _write_fake_opencode(
        tmp_path,
        _success_body(),
        session_list=[
            {"id": "ses_fixture", "title": "exact", "updated": 1, "directory": str(workdir)},
            {"id": "ses_fixture_extra", "title": "other", "updated": 2, "directory": str(workdir)},
        ],
    )
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(binary))

    assert [item.session_id for item in list_sessions()] == ["ses_fixture", "ses_fixture_extra"]
    assert _session_exists("ses_fixture")
    assert not _session_exists("ses_fixture_e")
    descriptor = validate_session_reference(
        session_id="ses_fixture",
        registry_entry={"session_id": "ses_fixture", "working_directory": str(workdir.resolve())},
        workdir=workdir,
    )

    assert descriptor.title == "exact"
    with pytest.raises(OpenCodeSessionError, match="dispatcher registry"):
        validate_session_reference(
            session_id="ses_fixture",
            registry_entry={"session_id": "ses_fixture_extra", "working_directory": str(workdir)},
            workdir=workdir,
        )


def test_fork_must_return_a_distinct_child_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = _write_fake_opencode(tmp_path, _success_body("ses_fixture_child"))
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(binary))
    kwargs = _run_kwargs(tmp_path)
    kwargs.update({"session_id": "ses_fixture_parent", "mode": "fork"})

    result = run_session(**kwargs)

    assert result.session_id == "ses_fixture_child"
    assert result.parent_session_id == "ses_fixture_parent"


def test_resume_rejects_missing_or_stale_session_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _write_fake_opencode(tmp_path, _success_body("ses_fixture_different"))
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(binary))
    kwargs = _run_kwargs(tmp_path)
    kwargs.update({"session_id": "ses_fixture_parent", "mode": "resume"})
    identified: list[str] = []
    kwargs["lifecycle"] = SessionLifecycleCallbacks(
        lambda _process_id, _process_create_time: None,
        identified.append,
    )

    with pytest.raises(OpenCodeSessionError, match="expected"):
        run_session(**kwargs)
    assert identified == []

    kwargs["session_id"] = None
    with pytest.raises(OpenCodeSessionError, match="require"):
        run_session(**kwargs)


def test_session_validation_rejects_a_missing_or_foreign_registry_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    binary = _write_fake_opencode(tmp_path, _success_body(), session_list=[])
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(binary))

    with pytest.raises(OpenCodeSessionError, match="not found"):
        validate_session_reference(
            session_id="ses_fixture_missing",
            registry_entry={
                "session_id": "ses_fixture_missing",
                "working_directory": str(workdir.resolve()),
            },
            workdir=workdir,
        )


def test_decoder_rejects_malformed_and_missing_completion() -> None:
    decoder = OpenCodeJsonlDecoder(max_output_bytes=4096)

    with pytest.raises(OpenCodeProtocolError, match="malformed"):
        decoder.consume_line("not-json", line_number=1)

    decoder.consume_line(
        json.dumps(
            {
                "type": "text",
                "timestamp": 1,
                "sessionID": "ses_fixture",
                "part": {"type": "text", "text": "only text"},
            }
        ),
        line_number=2,
    )
    with pytest.raises(OpenCodeProtocolError, match="step_finish"):
        decoder.finish(require_response=True)


def test_decoder_uses_only_the_last_text_event_as_chat_response() -> None:
    final_answer = '{"response_contract":"dispatcher.executor_result.v1","outcome":"completed"}'
    stream = "\n".join(
        json.dumps(event)
        for event in [
            {
                "type": "text",
                "timestamp": 1,
                "sessionID": "ses_fixture_narration",
                "part": {"id": "prt_narration_one", "type": "text", "text": "I will inspect the repo."},
            },
            {
                "type": "tool_use",
                "timestamp": 2,
                "sessionID": "ses_fixture_narration",
                "part": {"id": "prt_tool_one", "type": "tool"},
            },
            {
                "type": "text",
                "timestamp": 3,
                "sessionID": "ses_fixture_narration",
                "part": {"id": "prt_narration_two", "type": "text", "text": "The worktree is clean."},
            },
            {
                "type": "step_finish",
                "timestamp": 4,
                "sessionID": "ses_fixture_narration",
                "part": {
                    "type": "step-finish",
                    "cost": 0.01,
                    "tokens": {"total": 4, "input": 2, "output": 2, "reasoning": 0},
                },
            },
            {
                "type": "text",
                "timestamp": 5,
                "sessionID": "ses_fixture_narration",
                "part": {"id": "prt_final", "type": "text", "text": final_answer},
            },
            {
                "type": "step_finish",
                "timestamp": 6,
                "sessionID": "ses_fixture_narration",
                "part": {
                    "type": "step-finish",
                    "cost": 0.02,
                    "tokens": {"total": 8, "input": 4, "output": 4, "reasoning": 0},
                },
            },
        ]
    )
    decoder = OpenCodeJsonlDecoder(max_output_bytes=4096)

    for line_number, line in enumerate(stream.splitlines(), start=1):
        decoder.consume_line(line, line_number=line_number)
    raw, chat_response, _usage, _session_id, _cost = decoder.finish(require_response=True)

    assert chat_response == final_answer
    assert "I will inspect" not in chat_response
    assert [event["part"]["id"] for event in raw if event["type"] == "text"] == [
        "prt_narration_one",
        "prt_narration_two",
        "prt_final",
    ]


def test_cancel_process_group_checks_host_before_signalling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions.socket, "gethostname", lambda: "dispatcher-host")

    with pytest.raises(OpenCodeProcessError, match="another host"):
        cancel_process_group(4242, "other-host", 1, 1.0)


def test_cancel_process_group_interrupts_and_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    alive = {4242}
    monkeypatch.setattr(sessions.socket, "gethostname", lambda: "dispatcher-host")

    class FakeProcess:
        def __init__(self, process_id: int) -> None:
            assert process_id == 4242

        def create_time(self) -> float:
            return 1.0

    def fake_kill(pid: int, signal_number: int) -> None:
        if signal_number == 0:
            if pid not in alive:
                raise ProcessLookupError
            return
        calls.append((pid, signal_number))
        if signal_number == sessions.signal.SIGINT:
            alive.clear()

    monkeypatch.setattr(sessions.os, "kill", fake_kill)
    monkeypatch.setattr(sessions.os, "killpg", fake_kill)
    monkeypatch.setattr(sessions.psutil, "Process", FakeProcess)
    monkeypatch.setattr(sessions.time, "sleep", lambda _seconds: None)

    assert cancel_process_group(4242, "dispatcher-host", 1, 1.0) is True
    assert calls == [(4242, sessions.signal.SIGINT)]


def test_cancel_process_group_refuses_a_reused_pid_without_signalling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        recorded_create_time = psutil.Process(process.pid).create_time()
        signal_calls: list[tuple[int, int]] = []

        class ReusedProcess:
            def __init__(self, process_id: int) -> None:
                assert process_id == process.pid

            def create_time(self) -> float:
                return recorded_create_time + 2

        def record_signal(process_id: int, signal_number: int) -> None:
            signal_calls.append((process_id, signal_number))

        monkeypatch.setattr(sessions, "socket", SimpleNamespace(gethostname=lambda: "dispatcher-host"))
        monkeypatch.setattr(sessions, "os", SimpleNamespace(kill=record_signal, killpg=record_signal))
        monkeypatch.setattr(sessions.psutil, "Process", ReusedProcess)

        with pytest.raises(OpenCodeProcessIdentityError, match="identity does not match"):
            cancel_process_group(process.pid, "dispatcher-host", 1, recorded_create_time)

        assert signal_calls == []
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_cancel_process_group_confirms_sigkill_termination(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions.socket, "gethostname", lambda: "dispatcher-host")
    signals_sent: list[int] = []
    monotonic_calls = {"count": 0}

    def fake_monotonic() -> float:
        monotonic_calls["count"] += 1
        return monotonic_calls["count"] * 0.05

    class FakeProcess:
        def __init__(self, process_id: int) -> None:
            assert process_id == 4242

        def create_time(self) -> float:
            return 1.0

    def survivor_kill(pid: int, signal_number: int) -> None:
        if signal_number == 0:
            return
        signals_sent.append(signal_number)

    monkeypatch.setattr(sessions.os, "kill", survivor_kill)
    monkeypatch.setattr(sessions.os, "killpg", survivor_kill)
    monkeypatch.setattr(sessions.psutil, "Process", FakeProcess)
    monkeypatch.setattr(sessions.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(sessions.time, "monotonic", fake_monotonic)

    assert cancel_process_group(4242, "dispatcher-host", 1, 1.0) is False
    assert signals_sent == [
        sessions.signal.SIGINT,
        sessions.signal.SIGTERM,
        sessions.signal.SIGKILL,
    ]

    signals_sent.clear()
    monotonic_calls["count"] = 0
    exited = {"value": False}

    def exit_after_kill(pid: int, signal_number: int) -> None:
        if signal_number == 0 and exited["value"]:
            raise ProcessLookupError
        if signal_number == 0:
            return
        signals_sent.append(signal_number)
        if signal_number == sessions.signal.SIGKILL:
            exited["value"] = True

    monkeypatch.setattr(sessions.os, "kill", exit_after_kill)
    monkeypatch.setattr(sessions.os, "killpg", exit_after_kill)

    assert cancel_process_group(4242, "dispatcher-host", 1, 1.0) is True
    assert signals_sent == [
        sessions.signal.SIGINT,
        sessions.signal.SIGTERM,
        sessions.signal.SIGKILL,
    ]


def test_run_session_rejects_an_unsupported_binary_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _write_fake_opencode(tmp_path, _success_body())
    contents = binary.read_text(encoding="utf-8").replace("opencode 1.18.11", "opencode 9.9.9")
    binary.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(binary))

    with pytest.raises(OpenCodeVersionError, match="unsupported OpenCode version"):
        run_session(**_run_kwargs(tmp_path))


def test_run_session_passes_required_mcp_environment_and_inline_mcp_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_body = """    policy = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
    assert policy == {
        "permission": {"*": "deny", "fixture_echo": "allow"},
        "mcp": {
            "fixture": {
                "type": "local",
                "command": ["python3", "server.py"],
                "environment": {},
                "enabled": True,
            },
            "docs": {
                "type": "remote",
                "url": "https://example.invalid/mcp",
                "headers": {"Authorization": "Bearer {env:DOCS_MCP_TOKEN}"},
                "enabled": True,
            },
        },
    }
    assert os.environ["FIXTURE_MCP_TOKEN"] == "secret-value"
    assert os.environ["DOCS_MCP_TOKEN"] == "docs-secret"
    assert "UNRELATED_SECRET" not in os.environ
""" + _success_body()
    binary = _write_fake_opencode(tmp_path, run_body)
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(binary))
    monkeypatch.setenv("FIXTURE_MCP_TOKEN", "secret-value")
    monkeypatch.setenv("DOCS_MCP_TOKEN", "docs-secret")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-reach-child")

    kwargs = _run_kwargs(tmp_path)
    kwargs["permission_config"] = {
        "permission": {"*": "deny", "fixture_echo": "allow"},
        "mcp": {
            "fixture": {
                "type": "local",
                "command": ["python3", "server.py"],
                "environment": {},
                "enabled": True,
            },
            "docs": {
                "type": "remote",
                "url": "https://example.invalid/mcp",
                "headers": {"Authorization": "Bearer {env:DOCS_MCP_TOKEN}"},
                "enabled": True,
            },
        },
    }
    kwargs["environment_passthrough"] = ["FIXTURE_MCP_TOKEN", "DOCS_MCP_TOKEN"]
    result = run_session(**kwargs)

    assert result.session_id == "ses_fixture_new"


def test_run_session_never_redacts_the_executable_open_code_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_body = """    policy = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
    header = policy["mcp"]["docs"]["headers"]["Authorization"]
    assert header == "Bearer {env:DOCS_MCP_TOKEN}", header
    assert "REDACTED" not in os.environ["OPENCODE_CONFIG_CONTENT"]
""" + _success_body()
    binary = _write_fake_opencode(tmp_path, run_body)
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(binary))

    kwargs = _run_kwargs(tmp_path)
    kwargs["permission_config"] = {
        "permission": {"*": "deny"},
        "mcp": {
            "docs": {
                "type": "remote",
                "url": "https://example.invalid/mcp",
                "headers": {"Authorization": "Bearer {env:DOCS_MCP_TOKEN}"},
                "enabled": True,
            }
        },
    }
    result = run_session(**kwargs)

    assert result.session_id == "ses_fixture_new"


def test_run_session_rejects_a_missing_required_mcp_variable_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _write_fake_opencode(tmp_path, _success_body())
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(binary))
    monkeypatch.delenv("FIXTURE_MCP_TOKEN", raising=False)

    kwargs = _run_kwargs(tmp_path)
    kwargs["environment_passthrough"] = ["FIXTURE_MCP_TOKEN"]

    with pytest.raises(OpenCodeSessionError, match="required MCP environment variable missing"):
        run_session(**kwargs)


def test_child_environment_inherits_operator_opencode_config_directory(
    tmp_path: Path,
) -> None:
    operator_home = tmp_path / "operator"
    config_dir = operator_home / ".config" / "opencode"
    config_dir.mkdir(parents=True)

    environment = build_child_environment(
        {
            "HOME": str(operator_home),
            "PATH": "/usr/bin",
            "MCP_API_KEY": "operator-secret",
            "OPENCODE_CONFIG_CONTENT": "parent-inline-config",
        },
        state_dir=tmp_path / "state",
        permission_config={"permission": {"*": "deny"}},
        inherit_opencode_config=True,
    )

    assert environment["OPENCODE_CONFIG_DIR"] == str(config_dir)
    assert environment["HOME"] != str(operator_home)
    assert environment["MCP_API_KEY"] == "operator-secret"
    assert environment["OPENCODE_CONFIG_CONTENT"] != "parent-inline-config"


def test_explicit_mcp_mode_does_not_inherit_operator_config_directory(tmp_path: Path) -> None:
    operator_home = tmp_path / "operator"
    (operator_home / ".config" / "opencode").mkdir(parents=True)

    environment = build_child_environment(
        {"HOME": str(operator_home), "PATH": "/usr/bin"},
        state_dir=tmp_path / "state",
        permission_config={"permission": {"*": "deny"}, "mcp": {}},
    )

    assert "OPENCODE_CONFIG_DIR" not in environment
