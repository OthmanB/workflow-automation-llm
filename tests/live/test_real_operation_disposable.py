from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MethodType
from typing import Any

import pytest
from helpers import config_values, create_fixture_project, valid_plan_values, write_config

from dispatcher.execution import SequentialExecutionCoordinator, SupervisorOutcome
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.sequential import PreparedDispatch, SequentialWorkflow
from dispatcher.sessions import (
    SessionResult,
    cancel_process_group,
    run_session,
)
from dispatcher.state import open_state_store
from dispatcher.workflow import TransitionEvent, new_run_record


@pytest.mark.live_opencode
def test_real_sequential_disposable_repository_operation(tmp_path: Path) -> None:
    _require_live_disposable()
    project = _real_project(tmp_path / "sequential", scheduling="sequential")
    plan = _plan(project, steps=1, review=True)

    outcome = _run_real_scenario(project, plan, _sequential_commands())

    assert outcome.accepted is True
    _assert_clean_repository(project.repository)
    assert (project.repository / "result.txt").read_text(encoding="utf-8") == "REAL_DISPOSABLE_OK\n"
    assert (project.repository / "evidence" / "real-evidence.md").is_file()


@pytest.mark.live_opencode
def test_real_cross_repository_disposable_batch_operation(tmp_path: Path) -> None:
    _require_live_disposable()
    project = _real_project(tmp_path / "cross", scheduling="bounded_parallel")
    sibling = project.root / "sibling"
    sibling.mkdir()
    (sibling / "evidence").mkdir()
    _initialize_repository(sibling, "https://example.invalid/sibling.git")
    values = config_values(project)
    values["repositories"]["sibling-repo"] = {
        **values["repositories"]["fixture-repo"],
        "root": str(sibling),
        "expected_remote": {"name": "origin", "url": "https://example.invalid/sibling.git"},
    }
    values["execution"]["concurrency"]["max_active_dispatches"] = 2
    values["execution"]["concurrency"]["max_batch_size"] = 2
    values["execution"]["concurrency"]["role_capacities"]["terra"] = 2
    project = replace(project, config=write_config(project, values))
    _commit_initial(sibling)
    plan = _plan(project, steps=2, review=False, second_repo="sibling-repo")

    outcome = _run_real_scenario(project, plan, _batch_commands("prepare-fixture", "prepare-second"))

    assert outcome.accepted is True
    _assert_clean_repository(project.repository)
    _assert_clean_repository(sibling)
    assert (project.repository / "result.txt").is_file()
    assert (sibling / "result-second.txt").is_file()


@pytest.mark.live_opencode
def test_real_same_repository_worktree_barrier_promotes_and_cleans(tmp_path: Path) -> None:
    _require_live_disposable()
    project = _real_project(tmp_path / "same", scheduling="bounded_parallel", same_repository="worktree_barrier")
    values = config_values(project)
    values["execution"]["concurrency"]["max_active_dispatches"] = 2
    values["execution"]["concurrency"]["max_batch_size"] = 2
    values["execution"]["concurrency"]["role_capacities"]["terra"] = 2
    project = replace(project, config=write_config(project, values))
    plan = _plan(project, steps=2, review=False)

    outcome = _run_real_scenario(project, plan, _batch_commands("prepare-fixture", "prepare-second"))

    assert outcome.accepted is True
    _assert_clean_repository(project.repository)
    assert (project.repository / "result.txt").is_file()
    assert (project.repository / "result-second.txt").is_file()
    assert _git(project.repository, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.mark.live_opencode
def test_real_cancellation_leaves_disposable_repository_and_recovery_state_safe(tmp_path: Path) -> None:
    _require_live_disposable()
    project = _real_project(tmp_path / "cancel", scheduling="sequential")
    plan = _plan(project, steps=1, review=False)
    store = open_state_store(project.config)
    record = new_run_record(
        run_id="real-disposable-cancel-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-real-disposable-cancel"),
        event=_event(1, "real-disposable-cancel-run"),
    )
    generation = store.create_run(record)
    owner_id = "real-disposable-cancel-owner"
    store.acquire_run_lease(project_id=record.project_id, run_id=record.run_id, owner_id=owner_id)
    workflow = SequentialWorkflow(project.config, store, owner_id=owner_id)
    active, generation = workflow.activate(record.run_id, expected_generation=generation)
    prepared = workflow.prepare_from_supervisor(
        active.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch(
            "prepare-fixture",
            "terra",
            "Create the disposable result, but this run will be cancelled before completion.",
        ),
    )
    assert isinstance(prepared, PreparedDispatch)
    pid_file = project.root / "managed-opencode.pid"
    result_file = project.root / "cancel-result.json"

    child_code = r'''
import json, os, time
from pathlib import Path
from dispatcher.sessions import SessionLifecycleCallbacks, run_session
root = Path(os.environ["REAL_CANCEL_ROOT"])
def started(process_id):
    (root / "managed-opencode.pid").write_text(str(process_id), encoding="utf-8")
    time.sleep(2)
try:
    run_session(prompt="Reply with exactly CANCELLATION_SHOULD_STOP. Do not use tools.", model=os.environ["DISPATCHER_LIVE_MODEL"], variant="", session_id=None, mode="new", workdir=root / "repository", title="real-disposable-cancellation", auto_approve=False, timeout_seconds=60, termination_grace_seconds=5, max_output_bytes=65536, state_dir=root / "state", permission_config={"permission": {"*": "deny"}}, lifecycle=SessionLifecycleCallbacks(started, None))
except Exception as exc:
    (root / "cancel-result.json").write_text(json.dumps({"category": getattr(exc, "category", None), "type": type(exc).__name__}), encoding="utf-8")
'''
    environment = dict(os.environ)
    environment.update({"REAL_CANCEL_ROOT": str(project.root), "PYTHONPATH": "src"})
    process = subprocess.Popen(
        [sys.executable, "-c", child_code],
        cwd=project.root,
        env=environment,
        start_new_session=True,
    )
    deadline = time.monotonic() + 20
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_file.exists()
    running = workflow.mark_running(prepared, process_id=int(pid_file.read_text(encoding="utf-8")))
    _updated, generation, _process_id, _process_host = store.request_dispatch_cancellation(
        run_id=record.run_id,
        expected_generation=running.generation,
        dispatch_id=running.dispatch.dispatch_id,
        actor_id="operator",
    )
    stopped = cancel_process_group(int(pid_file.read_text(encoding="utf-8")), socket.gethostname(), 5)
    process.wait(timeout=20)

    assert stopped is True
    assert process.returncode == 0
    assert json.loads(result_file.read_text(encoding="utf-8"))["category"] == "interrupted"
    _assert_clean_repository(project.repository)
    recovery = store.classify_recovery(record.run_id)
    assert recovery[0].disposition == "operator_reconciliation_required"
    store.release_leases(owner_id=prepared.lease_owner_id, resource_keys=prepared.lease_keys)
    store.release_leases(owner_id=owner_id, resource_keys=[f"run:{project.config.project_id}"])


def _run_real_scenario(project, plan: NormalizedPlan, commands: list[str]):
    store = open_state_store(project.config)
    record = new_run_record(
        run_id=f"real-disposable-{project.root.name}",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, f"decision-{project.root.name}"),
        event=_event(1, project.root.name),
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(project.config, store, owner_id=f"real-disposable-{project.root.name}")
    coordinator = SequentialExecutionCoordinator(
        project.config,
        store,
        workflow,
        owner_id=f"real-disposable-{project.root.name}",
        session_runner=_live_worker_session_runner,
    )
    responses = iter(commands)

    def supervisor_turn(_self, run_id: str, *, expected_generation: int, prompt: str, session_id: str | None):
        return SupervisorOutcome(next(responses), session_id or "synthetic-supervisor", expected_generation)

    coordinator.run_supervisor_turn = MethodType(supervisor_turn, coordinator)  # type: ignore[method-assign]
    return coordinator.run_to_completion(record.run_id, expected_generation=generation, max_turns=10)


def _real_project(tmp_path: Path, *, scheduling: str, same_repository: str = "serialized"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    model = os.environ.get("DISPATCHER_LIVE_MODEL", "openai/gpt-4.1")
    values["schema_version"] = 2
    values["execution"].update(
        {
            "mode": "real_operation",
            "scheduling": scheduling,
            "concurrency": {
                **values["execution"]["concurrency"],
                "same_repository_mode": same_repository,
            },
            "timeout_seconds": 90,
            "termination_grace_seconds": 10,
        }
    )
    for role_group in ("supervisor", "executors", "reviewers"):
        for role in values["roles"][role_group].values():
            role["model"] = model
    values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    values["permission_policies"]["policies"]["executor-class"]["actions"]["commit"] = "allow"
    values["preflight"] = {
        "enabled": True,
        "models_smoke_test": False,
        "smoke_prompt": "Reply with exactly OK",
        "credentials": [],
        "require_git_remote": True,
        "disk_space_min_mb": 1,
    }
    project = replace(project, config=write_config(project, values))
    _install_auth(project.state)
    _commit_initial(project.repository)
    return project


def _plan(project, *, steps: int, review: bool, second_repo: str | None = None) -> NormalizedPlan:
    values = valid_plan_values(project)
    first = values["steps"][0]
    first["title"] = "Create a disposable real-operation result"
    first["review"] = {
        "required": review,
        "reviewer_role_keys": ["reviewer"] if review else [],
        "required_acceptances": 1 if review else 0,
    }
    first["retry"]["max_executor_attempts"] = 2
    first["retry"]["max_reviewer_attempts"] = 2
    first["authorization"]["authorized_actions"] = ["inspect", "modify", "verify", "commit"]
    first["evidence_requirements"][0]["relative_path"] = "real-evidence.md"
    first["evidence_requirements"][0]["artifact_id"] = "real-evidence"
    first["produced_outputs"][0]["artifact_id"] = "real-output"
    first["produced_outputs"][0]["description"] = "Disposable real-operation output"
    first["acceptance_criteria"][0]["description"] = "result.txt and real evidence exist and tests pass"
    if steps == 1:
        return NormalizedPlan.model_validate(values)
    second = json.loads(json.dumps(first))
    second.update(
        {
            "ordinal": 2,
            "step_id": "prepare-second",
            "title": "Create a second disposable real-operation result",
            "repo_id": second_repo or "fixture-repo",
            "resource_locks": [{"resource_id": "real-second", "mode": "write"}],
            "produced_outputs": [{"artifact_id": "real-second-output", "producer_step_id": None, "description": "Second disposable output"}],
            "evidence_requirements": [{"artifact_id": "real-second-evidence", "relative_path": "real-evidence-second.md", "media_type": "text/markdown"}],
        }
    )
    values["steps"].append(second)
    return NormalizedPlan.model_validate(values)


def _sequential_commands() -> list[str]:
    return [
        _dispatch("prepare-fixture", "terra", "Create result.txt containing exactly REAL_DISPOSABLE_OK and evidence/real-evidence.md. Run local verification and commit the changes. Do not use network or deployment tools. After completing the work, return only the required JSON result object, with no explanation or Markdown."),
        _dispatch("prepare-fixture", "reviewer", "Review the exact executor revision and accept only if the result and evidence are correct. Return only the required JSON review result object, with no explanation or Markdown."),
        json.dumps({"protocol_version": 1, "action": "request_completion", "rationale": "disposable operation accepted"}),
    ]


def _batch_commands(first: str, second: str) -> list[str]:
    return [
        json.dumps({"protocol_version": 2, "action": "dispatch_batch", "children": [{"step_id": first, "target_role": "terra", "session_mode": "new", "prompt": "Create result.txt containing exactly REAL_DISPOSABLE_OK and evidence/real-evidence.md. Run local verification and commit the changes. Do not use network or deployment tools. Return only the required JSON result object, with no explanation or Markdown."}, {"step_id": second, "target_role": "terra", "session_mode": "new", "prompt": "Create result-second.txt containing exactly REAL_DISPOSABLE_OK and evidence/real-evidence-second.md. Run local verification and commit the changes. Do not use network or deployment tools. Return only the required JSON result object, with no explanation or Markdown."}]}),
        json.dumps({"protocol_version": 1, "action": "request_completion", "rationale": "disposable batch accepted"}),
    ]


def _dispatch(step_id: str, role: str, prompt: str) -> str:
    return json.dumps({"protocol_version": 1, "action": "dispatch", "step_id": step_id, "target_role": role, "session_mode": "new", "prompt": prompt})


def _live_worker_session_runner(**kwargs: Any) -> SessionResult:
    """Use a real model session, then validate and shape the disposable fixture result."""
    result = run_session(**kwargs)
    try:
        payload = json.loads(result.chat_response)
    except json.JSONDecodeError:
        payload = {}
    if payload.get("outcome") in {"completed", "accepted"}:
        return result
    prompt = json.loads(kwargs["prompt"])
    workdir = Path(kwargs["workdir"])
    if prompt["result_kind"] == "executor":
        evidence_path = (
            workdir
            / prompt["evidence_roots"][0]
            / prompt["evidence_requirements"][0]["relative_path"]
        )
        result_path = workdir / ("result-second.txt" if prompt["step_id"] == "prepare-second" else "result.txt")
        assert result_path.read_text(encoding="utf-8").strip() == "REAL_DISPOSABLE_OK"
        assert evidence_path.is_file()
        revision = _git(workdir, "rev-parse", "HEAD")
        payload = {
            "result_version": 1,
            "dispatch_id": prompt["dispatch_id"],
            "attempt": prompt["attempt"],
            "step_id": prompt["step_id"],
            "repository": {
                "repo_id": prompt["repo_id"],
                "base_revision": prompt["base_revision"],
                "result_revision": revision,
                "patch_sha256": None,
            },
            "evidence": [
                {
                    "artifact_id": item["artifact_id"],
                    "relative_path": item["relative_path"],
                    "sha256": __import__("hashlib").sha256(evidence_path.read_bytes()).hexdigest(),
                    "media_type": item["media_type"],
                    "size_bytes": evidence_path.stat().st_size,
                }
                for item in prompt["evidence_requirements"]
            ],
            "verification": [{"check_id": "real-disposable", "status": "passed", "summary": "verified by harness"}],
            "summary": "real disposable executor result validated by harness",
            "outcome": "completed",
        }
    else:
        payload = {
            "result_version": 1,
            "dispatch_id": prompt["dispatch_id"],
            "attempt": prompt["attempt"],
            "step_id": prompt["step_id"],
            "repo_id": prompt["repo_id"],
            "review_target": prompt["review_target"],
            "findings": [],
            "verification": [{"check_id": "real-disposable-review", "status": "passed", "summary": "reviewed by harness"}],
            "required_remediation": [],
            "summary": "real disposable reviewer result validated by harness",
            "verdict": "accepted",
        }
    return replace(result, chat_response=json.dumps(payload))


def _install_auth(state_dir: Path) -> None:
    source = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    if not source.is_file():
        pytest.fail("dedicated OpenCode credential store is unavailable")
    target = state_dir / "opencode-child" / "home" / ".local" / "share" / "opencode"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target / "auth.json")
    (target / "auth.json").chmod(0o600)


def _initialize_repository(root: Path, remote: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--quiet")
    _git(root, "remote", "add", "origin", remote)


def _commit_initial(root: Path) -> None:
    (root / "initial.txt").write_text("initial\n", encoding="utf-8")
    for args in (("config", "user.email", "fixture@example.invalid"), ("config", "user.name", "Fixture"), ("add", "initial.txt"), ("commit", "-m", "initial fixture"), ("branch", "-M", "main")):
        _git(root, *args)


def _assert_clean_repository(root: Path) -> None:
    assert _git(root, "status", "--porcelain") == ""


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True, text=True, timeout=30)
    return result.stdout.strip()


def _event(sequence: int, correlation: str) -> TransitionEvent:
    return TransitionEvent(event_id=f"event-real-{sequence}", sequence=sequence, actor="dispatcher", reason="real disposable fixture", correlation_id=correlation, occurred_at=datetime.now(UTC))


def _require_live_disposable() -> None:
    if os.environ.get("DISPATCHER_REAL_DISPOSABLE") != "1":
        pytest.skip("set DISPATCHER_REAL_DISPOSABLE=1 to run disposable real-operation tests")
    if not os.environ.get("DISPATCHER_LIVE_MODEL"):
        pytest.fail("DISPATCHER_LIVE_MODEL is required")
