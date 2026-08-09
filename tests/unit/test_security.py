from __future__ import annotations

import json
import stat
from pathlib import Path

from dispatcher import audit, state
from dispatcher.sessions import build_child_environment


def test_child_environment_excludes_parent_credentials_and_isolates_opencode_state(tmp_path: Path) -> None:
    environment = build_child_environment(
        {"PATH": "/usr/bin", "UNRELATED_SECRET": "do-not-pass", "OPENCODE_CONFIG": "parent"},
        state_dir=tmp_path / "state",
        permission_config={"permission": {"*": "deny"}},
    )

    assert environment["PATH"] == "/usr/bin"
    assert "UNRELATED_SECRET" not in environment
    assert "OPENCODE_CONFIG" not in environment
    assert environment["OPENCODE_CONFIG_CONTENT"] == '{"permission":{"*":"deny"}}'
    assert Path(environment["HOME"]).is_relative_to(tmp_path / "state")
    assert stat.S_IMODE(Path(environment["HOME"]).stat().st_mode) == 0o700


def test_runtime_files_are_private_and_redact_credentials(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"

    state.save_state(str(state_dir), {"token": "secret-token"})
    state.save_sessions(str(state_dir), {"supervisor": {}})
    transcript = state.save_transcript(
        str(state_dir),
        "response",
        "Authorization: Bearer secret-token\nhttps://user:password@example.test/path",
    )
    second_transcript = state.save_transcript(str(state_dir), "response", "second response")
    audit.write_event(str(state_dir), {"kind": "test", "api_key": "secret-value"})

    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    for path in (state_dir / "state.json", state_dir / "sessions.json", state_dir / "audit.jsonl"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    transcript_content = (tmp_path / transcript).read_text(encoding="utf-8")
    state_content = (state_dir / "state.json").read_text(encoding="utf-8")
    audit_content = json.loads((state_dir / "audit.jsonl").read_text(encoding="utf-8"))
    assert "secret-token" not in transcript_content
    assert "password" not in transcript_content
    assert "secret-token" not in state_content
    assert audit_content["api_key"] == "[REDACTED]"
    assert transcript != second_transcript
