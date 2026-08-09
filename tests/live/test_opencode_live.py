from __future__ import annotations

import os
from pathlib import Path

import pytest

from dispatcher.sessions import run_session


@pytest.mark.live_opencode
def test_read_only_pinned_opencode_smoke(tmp_path: Path) -> None:
    if os.environ.get("DISPATCHER_LIVE_OPENCODE") != "1":
        pytest.skip("set DISPATCHER_LIVE_OPENCODE=1 to run the live OpenCode smoke suite")
    model = os.environ.get("DISPATCHER_LIVE_MODEL")
    if not model:
        pytest.fail("DISPATCHER_LIVE_MODEL is required when live smoke is enabled")

    result = run_session(
        prompt="Reply with exactly LIVE_SMOKE_OK. Do not use tools or inspect files.",
        model=model,
        variant="",
        session_id=None,
        mode="new",
        workdir=tmp_path,
        title="dispatcher-read-only-live-smoke",
        auto_approve=False,
        timeout_seconds=30,
        termination_grace_seconds=5,
        max_output_bytes=65_536,
        state_dir=Path(tmp_path) / "state",
        permission_config={"permission": {"*": "deny", "read": "allow", "glob": "allow", "grep": "allow"}},
    )

    assert "LIVE_SMOKE_OK" in result.chat_response
    assert result.evidence_written == []
