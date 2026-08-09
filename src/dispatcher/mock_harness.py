"""Mock harness — scripted responder that plays executor/reviewer for testing.

Used when the dispatcher is invoked with --mock so routing, state, resume,
and permission compilation can be exercised without spending tokens.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .sessions import SessionResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Built-in scenario catalogue
# ---------------------------------------------------------------------------

# A canned response for a successful executor run.
_MOCK_EXECUTOR_OK = {
    "session_id": "mock-executor-1",
    "output": [
        '{"session_id":"mock-executor-1","text":"Mock executor: work completed successfully.\\n\\ngate result: COMPLETE\\n\\nEvidence written: mock-evidence/handoff.md"}',
        '{"session_id":"mock-executor-1","usage":{"input_tokens":150,"output_tokens":80,"total_tokens":230}}',
    ],
    "exit_code": 0,
}

# A canned response for a reviewer pass verdict.
_MOCK_REVIEWER_PASS = {
    "session_id": "mock-reviewer-1",
    "output": [
        '{"session_id":"mock-reviewer-1","text":"Mock reviewer: PASS\\n\\nAll checks passed."}',
        '{"session_id":"mock-reviewer-1","usage":{"input_tokens":300,"output_tokens":60,"total_tokens":360}}',
    ],
    "exit_code": 0,
}

# A canned response for "model not found" (smoke test failure scenario).
_MOCK_MODEL_NOT_FOUND = {
    "session_id": "",
    "output": ['{"text":"Error: model not found"}'],
    "exit_code": 1,
}

# A canned supervisor response — a dispatch envelope.
_MOCK_SUPERVISOR_DISPATCH = (
    "{\"protocol_version\":1,\"action\":\"dispatch\","
    "\"step_id\":\"mock-step\",\"target_role\":\"terra\","
    "\"session_mode\":\"new\",\"prompt\":\"Perform the mock task and return evidence.\"}"
)

# A canned supervisor "done" response.
_MOCK_SUPERVISOR_DONE = (
    "{\"protocol_version\":1,\"action\":\"request_completion\","
    "\"rationale\":\"mock task finished\"}"
)

# Canned bootstrapped supervisor initial response (dispatch to terra).
_MOCK_SUPERVISOR_BOOTSTRAP = _MOCK_SUPERVISOR_DISPATCH


# ---------------------------------------------------------------------------
# Scenario logic
# ---------------------------------------------------------------------------

class MockSession:
    """A stateful mock that replays canned responses based on turn count."""

    def __init__(self, scenario: str = "simple") -> None:
        self.scenario = scenario
        self.supervisor_turns = 0

    def supervisor_turn(self, _prompt: str) -> SessionResult:
        """Simulate one supervisor turn."""
        self.supervisor_turns += 1

        if self.scenario == "model_error":
            # Inject a model-not-found error on the first turn.
            if self.supervisor_turns == 1:
                return self._result_from_canned(_MOCK_MODEL_NOT_FOUND)

        if self.supervisor_turns == 1:
            body = _MOCK_SUPERVISOR_BOOTSTRAP
        elif self.supervisor_turns >= 2:
            # After one executor round, respond "done".
            body = _MOCK_SUPERVISOR_DONE
        else:
            body = _MOCK_SUPERVISOR_DONE

        return self._result_from_text(body, "mock-supervisor")

    def executor_turn(self, _prompt: str) -> SessionResult:
        return self._result_from_canned(_MOCK_EXECUTOR_OK)

    def reviewer_turn(self, _prompt: str) -> SessionResult:
        return self._result_from_canned(_MOCK_REVIEWER_PASS)

    # -- helpers ----------------------------------------------------------

    def _result_from_canned(self, canned: dict[str, Any]) -> SessionResult:
        return SessionResult(
            session_id=canned["session_id"],
            exit_code=canned["exit_code"],
            chat_response=_extract_chat(canned.get("output", [])),
            evidence_written=["mock-evidence/handoff.md"],
            usage=_extract_usage(canned.get("output", [])),
            elapsed_s=0.1,
            opencode_version="mock",
        )

    def _result_from_text(
        self, text: str, session_id: str
    ) -> SessionResult:
        return SessionResult(
            session_id=session_id,
            exit_code=0,
            chat_response=text,
            evidence_written=[],
            usage={},
            elapsed_s=0.1,
        )


# ---------------------------------------------------------------------------
# Simulated opencode run (plugs into sessions.py)
# ---------------------------------------------------------------------------

class MockRunner:
    """Drop-in replacement for sessions.run_session in mock mode."""

    def __init__(self, scenario: str = "simple") -> None:
        self.mock = MockSession(scenario)
        self._call_count = 0

    def __call__(
        self,
        *,
        prompt: str = "",
        model: str = "",
        variant: str = "",
        session_id: str | None = None,
        workdir: str | Path | None = None,
        title: str = "",
        auto_approve: bool = False,
        timeout_seconds: int = 60,
        termination_grace_seconds: int = 5,
        max_output_bytes: int = 65536,
        state_dir: str | Path | None = None,
        mode: str = "new",
        snapshot_dirs: list[str] | None = None,
    ) -> SessionResult:
        self._call_count += 1
        logger.debug("mock call #%d  model=%s  session=%s  title=%s",
                     self._call_count, model, session_id or "<new>", title)

        if title.startswith("smoke-test "):
            return SessionResult(
                session_id=f"mock-smoke-{self._call_count}",
                exit_code=0,
                chat_response="OK",
                evidence_written=[],
                usage={},
                elapsed_s=0.1,
                opencode_version="mock",
            )

        # Route based on title/model convention.
        title_low = title.lower()
        if "supervisor" in title_low or "boot" in title_low or "resume" in title_low:
            return self.mock.supervisor_turn(prompt)
        if "review" in title_low:
            return self.mock.reviewer_turn(prompt)
        return self.mock.executor_turn(prompt)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_chat(output: list[str]) -> str:
    import json
    texts = []
    for line in output:
        try:
            ev = json.loads(line)
            t = ev.get("text") or ev.get("content") or ""
            if t.strip():
                texts.append(t.strip())
        except json.JSONDecodeError:
            pass
    return "\n".join(texts)


def _extract_usage(output: list[str]) -> dict[str, Any]:
    import json
    for line in reversed(output):
        try:
            ev = json.loads(line)
            u = ev.get("usage") or ev.get("tokenUsage")
            if u:
                return u
        except json.JSONDecodeError:
            pass
    return {}
