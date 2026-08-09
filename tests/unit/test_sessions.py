from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from dispatcher import sessions
from dispatcher.sessions import (
    OpenCodeJsonlDecoder,
    OpenCodeProcessError,
    OpenCodeProtocolError,
    OpenCodeSessionError,
    OpenCodeTimeoutError,
    OpenCodeVersionError,
    SessionLifecycleCallbacks,
    _session_exists,
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
        lambda process_id: observed.append(("process", process_id)),
        lambda session_id: observed.append(("session", session_id)),
    )

    result = run_session(**kwargs)

    assert result.session_id == "ses_fixture_new"
    assert [kind for kind, _value in observed] == ["process", "session"]
    assert isinstance(observed[0][1], int)
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

    def reject_start(process_id: int) -> None:
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
        lambda _process_id: None,
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
