#!/usr/bin/env python3
"""Deterministic OpenCode 1.18.11 stand-in for disposable integration tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "fake-state.json"
CALLS_PATH = ROOT / "calls.jsonl"
FAULT_PATH = ROOT / "fault.json"

_DIAGNOSTIC_COMMANDS = (
    "pwd",
    "ls",
    "git status --porcelain=v1",
    "git branch --show-current",
    "git rev-parse HEAD",
    "git diff --no-ext-diff --no-textconv",
)
_NATIVE_OBSERVATION_TOOLS = ("read", "glob", "grep")

_EXPECTED_REVIEWER_BASH = {
    "*": "deny",
    **{command: "allow" for command in _DIAGNOSTIC_COMMANDS},
}

_DENIED_REVIEWER_COMMANDS = (
    "ls /dev/null > marker.txt",
    "pytest -q test_real_output.py",
    "python -m pytest -q test_real_output.py",
    "ruff check",
    "mypy .",
    "git add marker.txt",
    'git commit -m "reviewer mutation"',
    "git branch reviewer-mutation",
    "git push origin HEAD",
)


def main() -> int:
    if sys.argv[1:] == ["--version"]:
        print("opencode 1.18.11")
        return 0
    if sys.argv[1:3] == ["session", "list"]:
        state = _load_state()
        print(json.dumps(state.get("sessions", [])))
        return 0
    if len(sys.argv) < 2 or sys.argv[1] != "run":
        print("unsupported fake OpenCode command", file=sys.stderr)
        return 64
    return _run()


def _run() -> int:
    args = sys.argv[2:]
    model = _option(args, "-m")
    workdir = Path(_option(args, "--dir"))
    requested_session = _optional_option(args, "-s")
    prompt = sys.stdin.read()
    policy = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
    state = _load_state()
    role = _role(model)
    payload = json.loads(prompt) if role != "supervisor" else None
    session_id = _session_id(state, role, requested_session)
    fault = _consume_fault()
    call = {
        "role": role,
        "model": model,
        "argv": args,
        "session_id": session_id,
        "requested_session": requested_session,
        "child_environment": {
            key: os.environ[key]
            for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")
        },
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "policy": policy,
        "fault": fault,
        "head_before": _git_head(workdir),
    }
    _event("step_start", session_id, {"type": "step-start"})
    if fault == "timeout":
        time.sleep(60)
        return 70
    if fault == "malformed_jsonl":
        print("{not-json", flush=True)
        return 0
    if fault == "nonzero":
        print("injected nonzero exit", file=sys.stderr, flush=True)
        return 78

    if isinstance(payload, dict) and isinstance(payload.get("permission_probe"), str):
        action = payload["permission_probe"]
        decision = _permission_decision(policy, action)
        call["permission_probe"] = {"action": action, "decision": decision}
        call["head_after"] = _git_head(workdir)
        _append_call(call)
        _save_state(state)
        if decision != "allow":
            print(f"permission probe {action} was {decision}", file=sys.stderr, flush=True)
            return 77
        _event("text", session_id, {"type": "text", "text": json.dumps(call["permission_probe"])})
        _event(
            "step_finish",
            session_id,
            {
                "type": "step-finish",
                "cost": 0.0,
                "tokens": {"total": 0, "input": 0, "output": 0, "reasoning": 0},
            },
        )
        return 0

    if role == "supervisor":
        response = _supervisor_response(state)
    elif role == "executor":
        assert isinstance(payload, dict)
        response = _executor_response(payload, policy, workdir)
        if fault == "write_nonzero":
            call["head_after"] = _git_head(workdir)
            _append_call(call)
            _save_state(state)
            print("injected exit after repository write", file=sys.stderr, flush=True)
            return 79
    else:
        assert isinstance(payload, dict)
        response = _reviewer_response(payload, policy, workdir)

    call["head_after"] = _git_head(workdir)
    _append_call(call)
    _save_state(state)
    _emit_narrated_response(session_id, response)

    return 0


def _emit_narrated_response(session_id: str, response: dict[str, Any]) -> None:
    _event("text", session_id, {"type": "text", "text": "I will inspect the fixture first."})
    _event("tool_use", session_id, {"type": "tool", "tool": "read"})
    _event(
        "step_finish",
        session_id,
        {
            "type": "step-finish",
            "cost": 0.001,
            "tokens": {"total": 10, "input": 6, "output": 4, "reasoning": 0},
        },
    )
    _event("step_start", session_id, {"type": "step-start"})
    _event("text", session_id, {"type": "text", "text": "The fixture worktree is clean."})
    _event("tool_use", session_id, {"type": "tool", "tool": "status"})
    _event(
        "step_finish",
        session_id,
        {
            "type": "step-finish",
            "cost": 0.001,
            "tokens": {"total": 10, "input": 6, "output": 4, "reasoning": 0},
        },
    )
    _event("step_start", session_id, {"type": "step-start"})
    _event("text", session_id, {"type": "text", "text": json.dumps(response, sort_keys=True)})
    _event(
        "step_finish",
        session_id,
        {
            "type": "step-finish",
            "cost": 0.001,
            "tokens": {"total": 10, "input": 6, "output": 4, "reasoning": 0},
        },
    )


def _supervisor_response(state: dict[str, Any]) -> dict[str, Any]:
    turn = int(state.get("supervisor_turn", 0)) + 1
    state["supervisor_turn"] = turn
    if turn == 1:
        return _dispatch("terra", "new", "Implement the approved fixture change.")
    if turn == 2:
        return _dispatch("reviewer", "new", "Review the exact executor revision.")
    if turn == 3:
        return _dispatch("terra", "resume", "Apply the requested fixture rework.")
    if turn == 4:
        return _dispatch("reviewer", "new", "Review the reworked exact revision.")
    if turn == 5:
        return {
            "protocol_version": 1,
            "action": "request_completion",
            "rationale": "The accepted fixture work is complete.",
        }
    raise RuntimeError(f"unexpected supervisor turn: {turn}")


def _dispatch(target_role: str, session_mode: str, prompt: str) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "action": "dispatch",
        "step_id": "prepare-fixture",
        "target_role": target_role,
        "session_mode": session_mode,
        "prompt": prompt,
        "rationale": "deterministic integration scenario",
    }


def _executor_response(
    payload: dict[str, Any],
    policy: dict[str, Any],
    workdir: Path,
) -> dict[str, Any]:
    permission = policy["permission"]
    if permission.get("edit") != "allow" or permission.get("write") != "allow":
        raise RuntimeError("executor mutation permission was not allowed")
    attempt = int(payload["attempt"])
    value_path = workdir / "src" / "value.txt"
    evidence_path = workdir / "evidence" / "fixture.md"
    value_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    value_path.write_text(f"value={attempt}\n", encoding="utf-8")
    evidence_path.write_text(f"fixture evidence attempt {attempt}\n", encoding="utf-8")
    return {
        "proposal_version": 2,
        "response_contract": "dispatcher.executor_proposal.v2",
        "dispatch_id": payload["dispatch_id"],
        "attempt": attempt,
        "step_id": payload["step_id"],
        "repository": {
            "repo_id": payload["repo_id"],
            "base_revision": payload["base_revision"],
        },
        "evidence": [
            {
                "artifact_id": "fixture-evidence",
                "relative_path": "fixture.md",
                "media_type": "text/markdown",
            }
        ],
        "criterion_self_reports": [
            {
                "check_id": criterion["criterion_id"],
                "status": "not_run",
                "summary": "dispatcher owns this check",
            }
            for criterion in payload["acceptance_criteria"]
        ],
        "summary": f"executor attempt {attempt} completed",
        "outcome": "completed",
    }


def _reviewer_response(
    payload: dict[str, Any],
    policy: dict[str, Any],
    workdir: Path,
) -> dict[str, Any]:
    permission = policy["permission"]
    if permission.get("edit") != "deny" or permission.get("write") != "deny":
        raise RuntimeError("reviewer mutation permission was not denied")
    bash = permission.get("bash")
    if not isinstance(bash, dict):
        raise RuntimeError("reviewer Bash permission map is missing")
    if bash.get("*") != "deny":
        raise RuntimeError("reviewer Bash wildcard permission was not denied")
    if next(iter(bash)) != "*":
        raise RuntimeError("reviewer Bash wildcard denial is not the first rule")
    if bash != _EXPECTED_REVIEWER_BASH:
        raise RuntimeError("reviewer Bash permission is not the exact diagnostic allowlist")
    allowed = {pattern for pattern, decision in bash.items() if decision == "allow"}
    if any(command in allowed for command in _DENIED_REVIEWER_COMMANDS):
        raise RuntimeError("reviewer Bash permission allowed a mutation or test command")
    observation_tools = payload.get("observation_tools")
    if observation_tools != {
        "native": list(_NATIVE_OBSERVATION_TOOLS),
        "diagnostic_commands": list(_DIAGNOSTIC_COMMANDS),
        "mcp": [],
    }:
        raise RuntimeError("reviewer prompt observation tools drifted from permission policy")
    if any(permission.get(tool) != "allow" for tool in _NATIVE_OBSERVATION_TOOLS):
        raise RuntimeError("reviewer native observation permission was not allowed")
    target = payload["review_target"]
    if target["result_revision"] != _git_head(workdir):
        raise RuntimeError("review target does not match current Git revision")
    attempt = int(payload["attempt"])
    verdict = "changes_requested" if attempt == 1 else "accepted"
    remediation = ["set the fixture to its second value"] if attempt == 1 else []
    return {
        "result_version": 1,
        "response_contract": "dispatcher.reviewer_result.v1",
        "dispatch_id": payload["dispatch_id"],
        "attempt": attempt,
        "step_id": payload["step_id"],
        "repo_id": payload["repo_id"],
        "review_target": target,
        "findings": [],
        "verification": [
            {
                "check_id": criterion["criterion_id"],
                "status": "passed",
                "summary": "revision reviewed",
            }
            for criterion in payload["acceptance_criteria"]
        ],
        "required_remediation": remediation,
        "summary": f"review attempt {attempt}: {verdict}",
        "transcript_ref": None,
        "verdict": verdict,
    }


def _session_id(
    state: dict[str, Any],
    role: str,
    requested_session: str | None,
) -> str:
    sessions = state.setdefault("sessions", [])
    if requested_session is not None:
        if not any(item["id"] == requested_session for item in sessions):
            raise RuntimeError(f"unknown resumed fake session: {requested_session}")
        return requested_session
    counters = state.setdefault("counters", {})
    counters[role] = int(counters.get(role, 0)) + 1
    session_id = f"ses_fake_{role}_{counters[role]}"
    sessions.append(
        {
            "id": session_id,
            "title": f"fake {role}",
            "updated": counters[role],
            "directory": "fixture",
        }
    )
    return session_id


def _role(model: str) -> str:
    if model.endswith("/supervisor"):
        return "supervisor"
    if model.endswith("/executor"):
        return "executor"
    if model.endswith("/reviewer"):
        return "reviewer"
    raise RuntimeError(f"unsupported fake model: {model}")


def _permission_decision(policy: dict[str, Any], action: str) -> str:
    value = policy["permission"].get(action, policy["permission"].get("*", "deny"))
    return value if isinstance(value, str) else "deny"


def _option(args: list[str], name: str) -> str:
    value = _optional_option(args, name)
    if value is None:
        raise RuntimeError(f"missing required option: {name}")
    return value


def _optional_option(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    index = args.index(name)
    return args[index + 1]


def _event(event_type: str, session_id: str, part: dict[str, Any]) -> None:
    print(
        json.dumps(
            {"type": event_type, "timestamp": 1, "sessionID": session_id, "part": part}
        ),
        flush=True,
    )


def _git(workdir: Path, *args: str) -> str:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Fixture Executor",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture Executor",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
        }
    )
    result = subprocess.run(
        ["git", *args],
        cwd=workdir,
        env=environment,
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _git_head(workdir: Path) -> str:
    try:
        return _git(workdir, "rev-parse", "HEAD")
    except subprocess.CalledProcessError:
        return ""


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"supervisor_turn": 0, "counters": {}, "sessions": []}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def _append_call(call: dict[str, Any]) -> None:
    with CALLS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(call, sort_keys=True) + "\n")


def _consume_fault() -> str | None:
    if not FAULT_PATH.exists():
        return None
    payload = json.loads(FAULT_PATH.read_text(encoding="utf-8"))
    FAULT_PATH.unlink()
    value = payload.get("next")
    return str(value) if value is not None else None


if __name__ == "__main__":
    raise SystemExit(main())
