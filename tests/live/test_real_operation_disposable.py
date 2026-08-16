from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from types import MethodType
from typing import Any, Mapping

import psutil
import pytest
from helpers import config_values, create_fixture_project, valid_plan_values, write_config

from dispatcher import cli as dispatcher_cli
from dispatcher.execution import (
    SequentialExecutionCoordinator,
    SupervisorOutcome,
    worker_opencode_state_dir,
)
from dispatcher.permissions import (
    READ_ONLY_DIAGNOSTIC_COMMANDS,
    read_only_diagnostic_bash_rules,
)
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.results import parse_reviewer_result
from dispatcher.sequential import (
    CompletionDecision,
    PreparedDispatch,
    SequentialWorkflow,
)
from dispatcher.sessions import (
    SessionResult,
    cancel_process_group,
    run_session,
)
from dispatcher.state import open_state_store
from dispatcher.verification import DirectTestBackend, VerificationRunner
from dispatcher.workflow import (
    BatchStatus,
    DispatchStatus,
    RunRecord,
    RunStatus,
    StepStatus,
    TransitionEvent,
    WorkspaceGroupStatus,
    completion_obligations,
    new_run_record,
    transition_step,
)

_REPOSITORY_IGNORE = "__pycache__/\n*.py[cod]\n.pytest_cache/\n"

_FIXED_TESTS: dict[str, tuple[str, str]] = {
    "first": (
        "test_real_output.py",
        "from pathlib import Path\n\n\ndef test_real_output() -> None:\n"
        '    result = Path("result.txt")\n'
        '    assert result.is_file() and result.read_text(encoding="utf-8").strip() == "REAL_DISPOSABLE_OK"\n'
        '    assert Path("evidence/real-evidence.md").is_file()\n',
    ),
    "second": (
        "test_real_second_output.py",
        "from pathlib import Path\n\n\ndef test_real_second_output() -> None:\n"
        '    result = Path("result-second.txt")\n'
        '    assert result.is_file() and result.read_text(encoding="utf-8").strip() == "REAL_DISPOSABLE_OK"\n'
        '    assert Path("evidence/real-evidence-second.md").is_file()\n',
    ),
}

_RESIDUE_NAME = "forced-reconciliation-residue.tmp"

_ADVERSARIAL_REVIEWER_COMMANDS = (
    "ls /dev/null > adversarial-marker.txt",
    "git add adversarial-marker.txt",
    'git commit -m "Adversarial reviewer mutation"',
)

_CONTROLLED_REVIEWER_COMMANDS = (
    *_ADVERSARIAL_REVIEWER_COMMANDS,
    "git push origin HEAD",
    "git branch adversarial-review",
    "pytest -q test_real_output.py",
)

_ROLE_MODEL_VARIABLES: dict[str, str] = {
    "supervisor": "DISPATCHER_LIVE_SUPERVISOR_MODEL",
    "executor": "DISPATCHER_LIVE_EXECUTOR_MODEL",
    "reviewer": "DISPATCHER_LIVE_REVIEWER_MODEL",
}


def resolve_live_models(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Resolve disposable live model IDs from environment variables."""
    values = dict(os.environ if env is None else env)
    fallback = values.get("DISPATCHER_LIVE_MODEL")
    if fallback:
        return {
            role: values.get(role_variables) or fallback
            for role, role_variables in _ROLE_MODEL_VARIABLES.items()
        }
    missing = sorted(
        role_variables
        for role_variables in _ROLE_MODEL_VARIABLES.values()
        if not values.get(role_variables)
    )
    if missing:
        raise ValueError(
            "no DISPATCHER_LIVE_MODEL fallback and missing role model variables: "
            + ", ".join(missing)
        )
    return {
        role: values[role_variables]
        for role, role_variables in _ROLE_MODEL_VARIABLES.items()
    }


def _apply_model_roles(values: dict[str, Any], resolved: Mapping[str, str]) -> dict[str, Any]:
    for group, role_kind in (("supervisor", "supervisor"), ("executors", "executor"), ("reviewers", "reviewer")):
        model = resolved[role_kind]
        for role in values["roles"][group].values():
            role["model"] = model
    return values


def _step_spec(step_id: str) -> dict[str, str]:
    if step_id == "prepare-fixture":
        return {
            "result_file": "result.txt",
            "result_content": "REAL_DISPOSABLE_OK",
            "evidence_path": "evidence/real-evidence.md",
            "pytest_file": "test_real_output.py",
            "criterion_id": "verify-real-output",
            "evidence_artifact": "real-evidence",
            "output_artifact": "real-output",
        }
    return {
        "result_file": "result-second.txt",
        "result_content": "REAL_DISPOSABLE_OK",
        "evidence_path": "evidence/real-evidence-second.md",
        "pytest_file": "test_real_second_output.py",
        "criterion_id": "verify-real-second-output",
        "evidence_artifact": "real-second-evidence",
        "output_artifact": "real-second-output",
    }


def _executor_task_prompt(step_id: str) -> str:
    spec = _step_spec(step_id)
    return (
        f"Create {spec['result_file']} containing exactly {spec['result_content']} and "
        f"{spec['evidence_path']}. Do not stage or commit files with git. Do not invent or edit test files: "
        f"do not run the fixed test; the dispatcher will execute `pytest -q {spec['pytest_file']}` "
        f"from the approved structured check after your response. "
        f"Do not use network or deployment tools. Report the exact criterion ID "
        f"{spec['criterion_id']} with status not_run in your criterion self-reports. "
        f"Return only the required JSON proposal object, with no explanation or Markdown."
    )


def _reviewer_prompt(step_id: str) -> str:
    spec = _step_spec(step_id)
    return (
        f"Review the exact executor revision for step {step_id}. Accept only if {spec['result_file']} "
        f"contains exactly {spec['result_content']} and {spec['evidence_path']} exists. Inspect the immutable "
        f"repository, the fixed test source {spec['pytest_file']}, the executor result, and its evidence with "
        f"native read, glob, and grep; do not run the test. Use exact diagnostic shell commands only for HEAD "
        f"and status metadata, without arguments or other shell syntax. Report the exact criterion ID "
        f"{spec['criterion_id']} in your verification results. Return "
        f"only the required JSON review result object, with no explanation or Markdown."
    )


def _seed_deterministic_fixture(root: Path, *, tests: tuple[str, ...] | list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "evidence").mkdir(exist_ok=True)
    (root / ".gitignore").write_text(_REPOSITORY_IGNORE, encoding="utf-8")
    for test_name in tests:
        filename, content = _FIXED_TESTS[test_name]
        (root / filename).write_text(content, encoding="utf-8")


@dataclass
class ScenarioHandle:
    project: Any
    store: Any
    workflow: Any
    coordinator: Any
    run_id: str
    generation: int
    initial_revisions: dict[str, str]
    supervisor_prompts: list[str]
    completion: CompletionDecision | None = None
    worker_error: BaseException | None = None


@dataclass
class InjectorState:
    calls: int = 0
    injections: int = 0
    last_workdir: Path | None = None
    last_title: str | None = None


class OneShotResidueInjector:
    """Test-only session-runner wrapper that forces exactly one dirty snapshot.

    The wrapped runner executes unchanged and its SessionResult is returned
    unmodified. Only for the first invocation whose workdir matches the target
    does the wrapper write one untracked file after the runner exits, so the
    production repository validation deterministically rejects the snapshot
    without modifying, repairing, or reshaping the model response.
    """

    def __init__(
        self,
        wrapped,
        *,
        target_workdir: str | Path,
        residue_name: str = _RESIDUE_NAME,
    ) -> None:
        self._wrapped = wrapped
        self._target_workdir = Path(target_workdir).resolve()
        self._residue_name = residue_name
        self._lock = threading.Lock()
        self._state = InjectorState()

    @property
    def state(self) -> InjectorState:
        return self._state

    def __call__(self, **kwargs):
        workdir = Path(kwargs["workdir"]).resolve()
        title = kwargs.get("title")
        with self._lock:
            self._state.calls += 1
            inject = workdir == self._target_workdir and self._state.injections == 0
            if inject:
                self._state.injections += 1
                self._state.last_workdir = workdir
                self._state.last_title = title
        result = self._wrapped(**kwargs)
        if inject:
            (workdir / self._residue_name).write_text(
                "forced residue for deterministic reconciliation proof\n", encoding="utf-8"
            )
        return result


class ControlledReviewerMutationAdapter:
    """Simulate reviewer Bash attempts and stop before executing any denied command."""

    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped
        self.attempts: list[tuple[str, str]] = []
        self.repository_snapshots: list[tuple[str, str, str, str]] = []

    def __call__(self, **kwargs):
        payload = json.loads(kwargs["prompt"])
        if payload["result_kind"] != "reviewer":
            return self._wrapped(**kwargs)
        workdir = Path(kwargs["workdir"])
        head_before = _git(workdir, "rev-parse", "HEAD")
        status_before = _git(workdir, "status", "--porcelain")
        permission = kwargs["permission_config"]["permission"]
        for command in READ_ONLY_DIAGNOSTIC_COMMANDS:
            decision = _bash_permission_decision(permission, command)
            self.attempts.append((command, decision))
            if decision != "allow":
                raise AssertionError(
                    f"controlled reviewer diagnostic was not allowed: {command}={decision}"
                )
        for command in _CONTROLLED_REVIEWER_COMMANDS:
            decision = _bash_permission_decision(permission, command)
            self.attempts.append((command, decision))
            if decision != "deny":
                raise AssertionError(
                    f"controlled reviewer command was not denied before execution: {command}={decision}"
                )
        result = self._wrapped(**kwargs)
        self.repository_snapshots.append(
            (
                head_before,
                _git(workdir, "rev-parse", "HEAD"),
                status_before,
                _git(workdir, "status", "--porcelain"),
            )
        )
        return result


def _fake_session_runner():
    """Return an in-process deterministic stand-in for dispatcher.sessions.run_session.

    The stand-in parses the exact worker prompt payload, performs the
    repository work it authorizes and returns a schema-valid SessionResult
    whose chat_response is the exact typed proposal JSON.
    """

    def runner(*, prompt, workdir, session_id, lifecycle=None, **kwargs):
        payload = json.loads(prompt)
        role_kind = payload["result_kind"]
        workdir_path = Path(workdir)
        if role_kind == "executor":
            response = _fake_executor_response(payload, workdir_path)
        else:
            response = _fake_reviewer_response(payload, workdir_path)
        session = session_id or f"fake-session-{uuid.uuid4().hex}"
        if lifecycle is not None:
            lifecycle.on_process_started(os.getpid(), time.time())
            lifecycle.on_session_identified(session)
        return SessionResult(
            session_id=session,
            exit_code=0,
            chat_response=json.dumps(response, sort_keys=True),
            evidence_written=[],
        )

    return runner


def _fake_executor_response(payload: Mapping[str, Any], workdir: Path) -> dict[str, Any]:
    attempt = int(payload["attempt"])
    spec = _step_spec(payload["step_id"])
    result_file = workdir / spec["result_file"]
    evidence_relative = spec["evidence_path"].split("/", 1)[1]
    evidence_file = workdir / "evidence" / evidence_relative
    result_file.write_text(f"{spec['result_content']}\n", encoding="utf-8")
    evidence_file.write_text(f"real disposable evidence attempt {attempt}\n", encoding="utf-8")
    if "review-marker.txt" in payload["task"]:
        (workdir / "review-marker.txt").write_text("rework completed\n", encoding="utf-8")
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
                "artifact_id": requirement["artifact_id"],
                "relative_path": requirement["relative_path"],
                "media_type": requirement["media_type"],
            }
            for requirement in payload["evidence_requirements"]
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
        "transcript_ref": None,
        "outcome": "completed",
    }


def _fake_reviewer_response(payload: Mapping[str, Any], workdir: Path) -> dict[str, Any]:
    target = payload["review_target"]
    if target["result_revision"] != _git(workdir, "rev-parse", "HEAD"):
        raise RuntimeError("fake reviewer saw a moved repository")
    attempt = int(payload["attempt"])
    spec = _step_spec(payload["step_id"])
    if payload["observation_tools"] != {
        "native": ["read", "glob", "grep"],
        "diagnostic_commands": list(READ_ONLY_DIAGNOSTIC_COMMANDS),
        "mcp": [],
    }:
        raise RuntimeError("fake reviewer received an unexpected observation contract")
    if (workdir / spec["result_file"]).read_text(encoding="utf-8").strip() != spec["result_content"]:
        raise RuntimeError("fake reviewer observed incorrect result content")
    if not (workdir / spec["evidence_path"]).read_text(encoding="utf-8").strip():
        raise RuntimeError("fake reviewer observed empty evidence")
    if not (workdir / spec["pytest_file"]).read_text(encoding="utf-8").strip():
        raise RuntimeError("fake reviewer observed empty fixed test source")
    marker = workdir / "review-marker.txt"
    if attempt == 1 and marker.exists():
        raise RuntimeError("review marker existed before executor remediation")
    if attempt > 1 and marker.read_text(encoding="utf-8") != "rework completed\n":
        raise RuntimeError("fake reviewer did not observe executor remediation")
    verdict = "changes_requested" if attempt == 1 else "accepted"
    remediation = ["Create review-marker.txt and commit it"] if attempt == 1 else []
    findings = (
        [
            {
                "finding_id": "marker",
                "severity": "blocking",
                "summary": "review marker required for this disposable protocol test",
            }
        ]
        if attempt == 1
        else []
    )
    return {
        "result_version": 1,
        "response_contract": "dispatcher.reviewer_result.v1",
        "dispatch_id": payload["dispatch_id"],
        "attempt": attempt,
        "step_id": payload["step_id"],
        "repo_id": payload["repo_id"],
        "review_target": target,
        "findings": findings,
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


def _start_real_scenario(
    project,
    plan: NormalizedPlan,
    *,
    steps: tuple[str, ...],
    original_prompts: Mapping[str, str],
    reviewer_role: str | None,
    batch: bool,
    reviewer_prompts: Mapping[str, list[str]] | None = None,
    session_runner=None,
) -> ScenarioHandle:
    store = open_state_store(project.config)
    run_id = f"real-disposable-{project.root.name}"
    record = new_run_record(
        run_id=run_id,
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
        session_runner=session_runner or run_session,
    )
    supervisor_prompts: list[str] = []

    def supervisor_turn(_self, run_id: str, *, expected_generation: int, prompt: str, session_id: str | None):
        supervisor_prompts.append(prompt)
        current, _current_generation = _self.store.load_run(run_id)
        command = _decide_next_command(
            current,
            steps=steps,
            original_prompts=original_prompts,
            reviewer_role=reviewer_role,
            batch=batch,
            reviewer_results=_latest_reviewer_results(_self.store, current),
            reviewer_prompts=reviewer_prompts,
        )
        return SupervisorOutcome(command, session_id or "synthetic-supervisor", expected_generation)

    coordinator.run_supervisor_turn = MethodType(supervisor_turn, coordinator)  # type: ignore[method-assign]
    return ScenarioHandle(
        project=project,
        store=store,
        workflow=workflow,
        coordinator=coordinator,
        run_id=run_id,
        generation=generation,
        initial_revisions=_repository_initial_revisions(project),
        supervisor_prompts=supervisor_prompts,
    )


def _run_real_scenario(
    project,
    plan: NormalizedPlan,
    *,
    steps: tuple[str, ...],
    original_prompts: Mapping[str, str],
    reviewer_role: str | None,
    batch: bool,
    reviewer_prompts: Mapping[str, list[str]] | None = None,
    session_runner=None,
) -> ScenarioHandle:
    handle = _start_real_scenario(
        project,
        plan,
        steps=steps,
        original_prompts=original_prompts,
        reviewer_role=reviewer_role,
        batch=batch,
        reviewer_prompts=reviewer_prompts,
        session_runner=session_runner,
    )
    _run_bounded_orchestration(handle)
    return handle


def _run_bounded_orchestration(handle: ScenarioHandle, *, max_turns: int = 10) -> None:
    _record, generation = handle.store.load_run(handle.run_id)
    handle.generation = generation
    try:
        handle.completion = handle.coordinator.run_to_completion(
            handle.run_id,
            expected_generation=generation,
            max_turns=max_turns,
        )
        handle.worker_error = None
    except Exception as exc:
        handle.worker_error = exc


def _repository_initial_revisions(project) -> dict[str, str]:
    revisions: dict[str, str] = {}
    for repo_id, repository in project.config.model.repositories.items():
        revisions[repo_id] = _git(Path(repository.root), "rev-parse", "HEAD")
    return revisions


def _answer_command(*, config_path, run_id: str, request_id: str, answer: str, actor_id: str = "operator") -> list[str]:
    return [
        "answer",
        "--config",
        str(config_path),
        "--run-id",
        run_id,
        "--request-id",
        request_id,
        "--answer",
        answer,
        "--actor-id",
        actor_id,
    ]


def _answer_via_cli(handle: ScenarioHandle, answer: str) -> int:
    record, _generation = handle.store.load_run(handle.run_id)
    request = record.operator_request
    assert request is not None
    return dispatcher_cli.main(
        _answer_command(
            config_path=handle.project.config_path,
            run_id=handle.run_id,
            request_id=request.request_id,
            answer=answer,
        )
    )


def _reconcile_disposable_repository(root: Path, initial_revision: str, *, residue_name: str) -> None:
    generated_paths = [
        residue_name,
        "result.txt",
        "result-second.txt",
        "review-marker.txt",
        "evidence/real-evidence.md",
        "evidence/real-evidence-second.md",
    ]
    for relative_path in generated_paths:
        path = root / relative_path
        if path.is_file() or path.is_symlink():
            path.unlink()
    _git(root, "reset", "--hard", initial_revision)
    (root / "evidence").mkdir(exist_ok=True)
    assert _git(root, "status", "--porcelain") == ""


def _configure_disposable_project(
    project,
    *,
    scheduling: str,
    same_repository: str = "serialized",
    models: Mapping[str, str] | None = None,
    mode: str,
    verification_backend: str,
):
    values = config_values(project)
    values["schema_version"] = 2
    values["execution"].update(
        {
            "mode": mode,
            "verification_backend": verification_backend,
            "scheduling": scheduling,
            "concurrency": {
                **values["execution"]["concurrency"],
                "same_repository_mode": same_repository,
            },
            "timeout_seconds": 180,
            "termination_grace_seconds": 10,
        }
    )
    _apply_model_roles(values, models or resolve_live_models())
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
    return replace(project, config=write_config(project, values))


def _configure_real_project(
    project,
    *,
    scheduling: str,
    same_repository: str = "serialized",
    models: Mapping[str, str] | None = None,
):
    return _configure_disposable_project(
        project,
        scheduling=scheduling,
        same_repository=same_repository,
        models=models,
        mode="real_operation",
        verification_backend="darwin_seatbelt_v1",
    )


def _configure_fake_runner_project(
    project,
    *,
    scheduling: str,
    same_repository: str = "serialized",
    models: Mapping[str, str],
):
    return _configure_disposable_project(
        project,
        scheduling=scheduling,
        same_repository=same_repository,
        models=models,
        mode="mock_workflow_test",
        verification_backend="direct_test_v1",
    )


def _real_project(
    tmp_path: Path,
    *,
    scheduling: str,
    same_repository: str = "serialized",
    seed_tests: tuple[str, ...] = ("first",),
) -> Any:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = _configure_real_project(
        create_fixture_project(tmp_path),
        scheduling=scheduling,
        same_repository=same_repository,
    )
    _install_auth(project.state)
    _seed_deterministic_fixture(project.repository, tests=seed_tests)
    _commit_initial(project.repository)
    return project


def _plan(project, *, steps: int, review: bool, second_repo: str | None = None) -> NormalizedPlan:
    values = valid_plan_values(project)
    first = values["steps"][0]
    first_spec = _step_spec("prepare-fixture")
    first["title"] = "Create a disposable real-operation result"
    first["review"] = {
        "required": review,
        "reviewer_role_keys": ["reviewer"] if review else [],
        "required_acceptances": 1 if review else 0,
    }
    first["retry"]["max_executor_attempts"] = 2
    first["retry"]["max_reviewer_attempts"] = 2
    first["retry"]["on_changes_requested"] = "retry"
    first["authorization"] = {
        "authorized_actions": ["inspect", "modify", "verify", "commit"],
        "writable_paths": [
            first_spec["evidence_path"],
            first_spec["result_file"],
            "review-marker.txt",
        ],
        "requires_operator_approval": False,
    }
    first["evidence_requirements"][0]["relative_path"] = first_spec["evidence_path"].split("/", 1)[1]
    first["evidence_requirements"][0]["artifact_id"] = first_spec["evidence_artifact"]
    first["produced_outputs"][0]["artifact_id"] = first_spec["output_artifact"]
    first["produced_outputs"][0]["description"] = "Disposable real-operation output"
    first["acceptance_criteria"][0]["criterion_id"] = first_spec["criterion_id"]
    first["acceptance_criteria"][0]["description"] = (
        f"`pytest -q {first_spec['pytest_file']}` must pass with a clean repository"
    )
    first["acceptance_criteria"][0]["check"] = {
        "argv": ["pytest", "-q", first_spec["pytest_file"]],
        "working_directory": "repository",
        "timeout_seconds": 120,
        "max_output_bytes": 65536,
        "expected_exit_codes": [0],
        "network_policy": "deny",
    }
    if steps == 1:
        return NormalizedPlan.model_validate(values)
    second_spec = _step_spec("prepare-second")
    second = json.loads(json.dumps(first))
    second.update(
        {
            "ordinal": 2,
            "step_id": "prepare-second",
            "title": "Create a second disposable real-operation result",
            "repo_id": second_repo or "fixture-repo",
            "resource_locks": [{"resource_id": "real-second", "mode": "write"}],
            "produced_outputs": [
                {
                    "artifact_id": second_spec["output_artifact"],
                    "producer_step_id": None,
                    "description": "Second disposable output",
                }
            ],
            "evidence_requirements": [
                {
                    "artifact_id": second_spec["evidence_artifact"],
                    "relative_path": second_spec["evidence_path"].split("/", 1)[1],
                    "media_type": "text/markdown",
                }
            ],
            "acceptance_criteria": [
                {
                    "criterion_id": second_spec["criterion_id"],
                    "description": (
                        f"`pytest -q {second_spec['pytest_file']}` must pass with a clean repository"
                    ),
                    "check": {
                        "argv": ["pytest", "-q", second_spec["pytest_file"]],
                        "working_directory": "repository",
                        "timeout_seconds": 120,
                        "max_output_bytes": 65536,
                        "expected_exit_codes": [0],
                        "network_policy": "deny",
                    },
                }
            ],
            "authorization": {
                "authorized_actions": ["inspect", "modify", "verify", "commit"],
                "writable_paths": [second_spec["evidence_path"], second_spec["result_file"]],
                "requires_operator_approval": False,
            },
        }
    )
    values["steps"].append(second)
    return NormalizedPlan.model_validate(values)


_REVIEW_PROMPT = (
    "Review the exact executor revision with native read, glob, and grep. Use exact diagnostic shell "
    "commands only for HEAD and status metadata. Do not run tests or modify files or Git state. Accept "
    "only if the immutable result, fixed test source, executor verification, and evidence satisfy the "
    "criterion. Return only the required JSON review result object, with no explanation or Markdown."
)


def _decide_next_command(
    record: RunRecord,
    *,
    steps: tuple[str, ...],
    original_prompts: Mapping[str, str],
    reviewer_role: str | None,
    batch: bool,
    reviewer_results: Mapping[str, Mapping[str, Any]] | None = None,
    reviewer_prompts: Mapping[str, list[str]] | None = None,
) -> str:
    """Decide the next supervisor command purely from the durable run record."""
    plan_steps = {step.step_id: step for step in record.plan.steps}
    pending: list[tuple[str, str, str, str]] = []
    for step_id in steps:
        plan_step = plan_steps[step_id]
        step = record.steps[step_id]
        if step.state in {StepStatus.ACCEPTED, StepStatus.WAIVED}:
            continue
        if step.state is StepStatus.READY:
            if step.executor_attempts >= plan_step.retry.max_executor_attempts:
                continue
            if step.executor_attempts == 0:
                pending.append((step_id, "terra", "new", original_prompts[step_id]))
            elif step.rework_rounds > 0:
                mode = "new" if batch else "resume"
                pending.append(
                    (
                        step_id,
                        "terra",
                        mode,
                        _rework_prompt(step_id, original_prompts[step_id], (reviewer_results or {}).get(step_id)),
                    )
                )
            else:
                pending.append((step_id, "terra", "new", original_prompts[step_id]))
        elif step.state in {StepStatus.EXECUTED, StepStatus.REVIEW_REQUIRED}:
            if reviewer_role is None:
                continue
            if step.reviewer_attempts >= plan_step.retry.max_reviewer_attempts:
                continue
            pool = (reviewer_prompts or {}).get(step_id) or []
            prompt = pool[step.reviewer_attempts] if step.reviewer_attempts < len(pool) else _REVIEW_PROMPT
            pending.append((step_id, reviewer_role, "new", prompt))
    if not pending:
        return _completion_command("all steps are accepted, terminal, or exhausted")
    if batch:
        return _batch_command(pending)
    step_id, role, mode, prompt = pending[0]
    return _dispatch(step_id, role, prompt, session_mode=mode)


def _latest_reviewer_results(store, record: RunRecord) -> dict[str, Mapping[str, Any]]:
    """Load the durable result of the newest reviewer dispatch for each step."""
    results: dict[str, Mapping[str, Any]] = {}
    reviewer_dispatches = [dispatch for dispatch in record.dispatches.values() if dispatch.role_kind == "reviewer"]
    for step_id in {dispatch.step_id for dispatch in reviewer_dispatches}:
        latest = max(
            (dispatch for dispatch in reviewer_dispatches if dispatch.step_id == step_id),
            key=lambda dispatch: dispatch.attempt,
        )
        payload = store.load_dispatch_payload(record.run_id, latest.dispatch_id)
        if payload.result is not None:
            results[step_id] = payload.result
    return results


def _rework_prompt(step_id: str, task: str, reviewer_result: Mapping[str, Any] | None) -> str:
    detail: list[str] = []
    if reviewer_result is not None:
        remediation = reviewer_result.get("required_remediation")
        if isinstance(remediation, list):
            detail.extend(str(item) for item in remediation if str(item).strip())
        findings = reviewer_result.get("findings")
        if isinstance(findings, list):
            detail.extend(
                str(finding.get("summary"))
                for finding in findings
                if isinstance(finding, dict) and finding.get("summary")
            )
    requested = "; ".join(detail) or "apply the reviewer's requested changes"
    return (
        f"Rework the executor result for step {step_id} to satisfy the reviewer: {requested}. "
        f"Original task: {task} Return only the required JSON result object, with no explanation or Markdown."
    )


def _batch_command(children: list[tuple[str, str, str, str]]) -> str:
    return json.dumps(
        {
            "protocol_version": 2,
            "action": "dispatch_batch",
            "children": [
                {"step_id": step_id, "target_role": role, "session_mode": mode, "prompt": prompt}
                for step_id, role, mode, prompt in children
            ],
        }
    )


def _completion_command(rationale: str) -> str:
    return json.dumps({"protocol_version": 1, "action": "request_completion", "rationale": rationale})


def _dispatch(step_id: str, role: str, prompt: str, *, session_mode: str = "new") -> str:
    return json.dumps({"protocol_version": 1, "action": "dispatch", "step_id": step_id, "target_role": role, "session_mode": session_mode, "prompt": prompt})


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
    for args in (
        ("config", "user.email", "fixture@example.invalid"),
        ("config", "user.name", "Fixture"),
        ("add", "."),
        ("commit", "-m", "initial fixture"),
        ("branch", "-M", "main"),
    ):
        _git(root, *args)


def _assert_clean_repository(root: Path) -> None:
    assert _git(root, "status", "--porcelain") == ""


def _assert_fixed_test_passes(root: Path, pytest_file: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", pytest_file],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"fixed test {pytest_file} failed:\n{result.stdout}\n{result.stderr}"


def _assert_no_active_leases(store) -> None:
    with sqlite3.connect(store.database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
    assert count == 0


def _assert_operator_decision_count(store, expected: int) -> None:
    with sqlite3.connect(store.database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM operator_decisions").fetchone()[0]
    assert count == expected


def _assert_only_owned_worktree(project) -> None:
    assert _git(project.repository, "worktree", "list", "--porcelain").count("worktree ") == 1


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True, text=True, timeout=30)
    return result.stdout.strip()


def _bash_permission_decision(permission: Mapping[str, Any], command: str) -> str:
    decision = permission.get("*", "deny")
    bash = permission.get("bash")
    if not isinstance(bash, Mapping):
        return str(decision)
    for pattern, candidate in bash.items():
        if fnmatchcase(command, str(pattern)):
            decision = candidate
    return str(decision)


def _reviewer_tool_events(handle: ScenarioHandle, dispatch) -> list[dict[str, Any]]:
    logs_dir = worker_opencode_state_dir(
        handle.project.config.state_dir,
        run_id=handle.run_id,
        dispatch=dispatch,
    ) / "opencode-events"
    events: list[dict[str, Any]] = []
    for path in sorted(logs_dir.glob("*.stdout.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("sessionID") == dispatch.runtime_session_id:
                events.append(event)
    return events


def _event(sequence: int, correlation: str) -> TransitionEvent:
    return TransitionEvent(event_id=f"event-real-{sequence}", sequence=sequence, actor="dispatcher", reason="real disposable fixture", correlation_id=correlation, occurred_at=datetime.now(UTC))


def _require_live_disposable() -> None:
    if os.environ.get("DISPATCHER_REAL_DISPOSABLE") != "1":
        pytest.skip("set DISPATCHER_REAL_DISPOSABLE=1 to run disposable real-operation tests")
    resolve_live_models()


# ---------------------------------------------------------------------------
# Live scenarios
# ---------------------------------------------------------------------------


@pytest.mark.live_opencode
def test_real_sequential_disposable_repository_operation(tmp_path: Path) -> None:
    _require_live_disposable()
    project = _real_project(tmp_path / "sequential", scheduling="sequential", seed_tests=("first",))
    spec = _step_spec("prepare-fixture")
    plan = _plan(project, steps=1, review=True)

    handle = _run_real_scenario(
        project,
        plan,
        steps=("prepare-fixture",),
        original_prompts={"prepare-fixture": _executor_task_prompt("prepare-fixture")},
        reviewer_role="reviewer",
        batch=False,
        reviewer_prompts={"prepare-fixture": [_reviewer_prompt("prepare-fixture")]},
    )

    assert handle.worker_error is None
    assert handle.completion is not None and handle.completion.accepted is True
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.SUCCEEDED
    step = record.steps["prepare-fixture"]
    assert step.state is StepStatus.ACCEPTED
    assert step.executor_attempts == 1
    assert step.reviewer_attempts == 1
    assert step.review_acceptances == 1
    assert all(dispatch.state is DispatchStatus.ACKNOWLEDGED for dispatch in record.dispatches.values())
    _assert_operator_decision_count(handle.store, 0)
    _assert_clean_repository(project.repository)
    _assert_only_owned_worktree(project)
    assert (project.repository / "result.txt").read_text(encoding="utf-8").strip() == "REAL_DISPOSABLE_OK"
    assert (project.repository / "evidence" / "real-evidence.md").is_file()
    _assert_fixed_test_passes(project.repository, spec["pytest_file"])
    _assert_no_active_leases(handle.store)


@pytest.mark.live_opencode
def test_real_cross_repository_disposable_batch_operation(tmp_path: Path) -> None:
    _require_live_disposable()
    project = _real_project(tmp_path / "cross", scheduling="bounded_parallel", seed_tests=("first",))
    sibling = project.root / "sibling"
    sibling.mkdir()
    _initialize_repository(sibling, "https://example.invalid/sibling.git")
    _seed_deterministic_fixture(sibling, tests=("second",))
    _commit_initial(sibling)
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
    plan = _plan(project, steps=2, review=False, second_repo="sibling-repo")

    handle = _run_real_scenario(
        project,
        plan,
        steps=("prepare-fixture", "prepare-second"),
        original_prompts={
            "prepare-fixture": _executor_task_prompt("prepare-fixture"),
            "prepare-second": _executor_task_prompt("prepare-second"),
        },
        reviewer_role=None,
        batch=True,
    )

    assert handle.worker_error is None
    assert handle.completion is not None and handle.completion.accepted is True
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.SUCCEEDED
    assert record.state is not RunStatus.WAITING_OPERATOR
    assert record.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert record.steps["prepare-second"].state is StepStatus.ACCEPTED
    assert len(record.batches) == 1
    batch = next(iter(record.batches.values()))
    assert batch.state is BatchStatus.JOINED
    assert all(dispatch.state is DispatchStatus.ACKNOWLEDGED for dispatch in record.dispatches.values())
    _assert_operator_decision_count(handle.store, 0)
    _assert_clean_repository(project.repository)
    _assert_clean_repository(sibling)
    _assert_only_owned_worktree(project)
    assert (project.repository / "result.txt").read_text(encoding="utf-8").strip() == "REAL_DISPOSABLE_OK"
    assert (sibling / "result-second.txt").read_text(encoding="utf-8").strip() == "REAL_DISPOSABLE_OK"
    _assert_fixed_test_passes(project.repository, "test_real_output.py")
    _assert_fixed_test_passes(sibling, "test_real_second_output.py")
    _assert_no_active_leases(handle.store)


@pytest.mark.live_opencode
def test_real_same_repository_worktree_barrier_promotes_and_cleans(tmp_path: Path) -> None:
    _require_live_disposable()
    project = _real_project(
        tmp_path / "same",
        scheduling="bounded_parallel",
        same_repository="worktree_barrier",
        seed_tests=("first", "second"),
    )
    values = config_values(project)
    values["execution"]["concurrency"]["max_active_dispatches"] = 2
    values["execution"]["concurrency"]["max_batch_size"] = 2
    values["execution"]["concurrency"]["role_capacities"]["terra"] = 2
    project = replace(project, config=write_config(project, values))
    plan = _plan(project, steps=2, review=False)

    handle = _run_real_scenario(
        project,
        plan,
        steps=("prepare-fixture", "prepare-second"),
        original_prompts={
            "prepare-fixture": _executor_task_prompt("prepare-fixture"),
            "prepare-second": _executor_task_prompt("prepare-second"),
        },
        reviewer_role=None,
        batch=True,
    )

    assert handle.worker_error is None
    assert handle.completion is not None and handle.completion.accepted is True
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.SUCCEEDED
    assert record.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert record.steps["prepare-second"].state is StepStatus.ACCEPTED
    assert len(record.batches) == 1
    batch = next(iter(record.batches.values()))
    assert batch.state is BatchStatus.JOINED
    assert all(group.state is WorkspaceGroupStatus.CLEANED for group in record.workspace_groups.values())
    _assert_operator_decision_count(handle.store, 0)
    _assert_clean_repository(project.repository)
    _assert_only_owned_worktree(project)
    assert (project.repository / "result.txt").is_file()
    assert (project.repository / "result-second.txt").is_file()
    _assert_fixed_test_passes(project.repository, "test_real_output.py")
    _assert_fixed_test_passes(project.repository, "test_real_second_output.py")
    _assert_no_active_leases(handle.store)


@pytest.mark.live_opencode
def test_real_cancellation_leaves_disposable_repository_and_recovery_state_safe(tmp_path: Path) -> None:
    _require_live_disposable()
    resolved = resolve_live_models()
    project = _real_project(tmp_path / "cancel", scheduling="sequential", seed_tests=("first",))
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
def started(process_id, _process_create_time):
    (root / "managed-opencode.pid").write_text(str(process_id), encoding="utf-8")
    time.sleep(2)
try:
    run_session(prompt="Reply with exactly CANCELLATION_SHOULD_STOP. Do not use tools.", model=os.environ["DISPATCHER_LIVE_EXECUTOR_MODEL"], variant="", session_id=None, mode="new", workdir=root / "repository", title="real-disposable-cancellation", auto_approve=False, timeout_seconds=60, termination_grace_seconds=5, max_output_bytes=65536, state_dir=root / "state", permission_config={"permission": {"*": "deny"}}, lifecycle=SessionLifecycleCallbacks(started, None))
except Exception as exc:
    (root / "cancel-result.json").write_text(json.dumps({"category": getattr(exc, "category", None), "type": type(exc).__name__}), encoding="utf-8")
'''
    environment = dict(os.environ)
    environment.update(
        {
            "REAL_CANCEL_ROOT": str(project.root),
            "PYTHONPATH": "src",
            "DISPATCHER_LIVE_EXECUTOR_MODEL": resolved["executor"],
        }
    )
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
    process_id = int(pid_file.read_text(encoding="utf-8"))
    process_create_time = psutil.Process(process_id).create_time()
    running = workflow.mark_running(
        prepared,
        process_id=process_id,
        process_create_time=process_create_time,
    )
    _updated, generation, _process_id, _process_host, recorded_create_time = (
        store.request_dispatch_cancellation(
            run_id=record.run_id,
            expected_generation=running.generation,
            dispatch_id=running.dispatch.dispatch_id,
            actor_id="operator",
        )
    )
    stopped = cancel_process_group(process_id, socket.gethostname(), 5, recorded_create_time)
    process.wait(timeout=20)

    assert stopped is True
    assert process.returncode == 0
    assert json.loads(result_file.read_text(encoding="utf-8"))["category"] == "interrupted"
    _assert_clean_repository(project.repository)
    recovery = store.classify_recovery(record.run_id)
    assert recovery[0].disposition == "operator_reconciliation_required"
    store.release_leases(owner_id=prepared.lease_owner_id, resource_keys=prepared.lease_keys)
    store.release_leases(owner_id=owner_id, resource_keys=[f"run:{project.config.project_id}"])
    _assert_no_active_leases(store)


@pytest.mark.live_opencode
def test_real_review_rework_resume_cycle_accepts_after_remediation(tmp_path: Path) -> None:
    _require_live_disposable()
    project = _real_project(tmp_path / "rework", scheduling="sequential", seed_tests=("first",))
    spec = _step_spec("prepare-fixture")
    plan = _plan(project, steps=1, review=True)
    force_rework = (
        f"Review the exact executor revision for step prepare-fixture. For this disposable protocol test only, "
        f"use native read, glob, and grep to inspect {spec['result_file']}, {spec['evidence_path']}, and fixed "
        f"test source {spec['pytest_file']}. Do not run pytest. Use exact diagnostic shell commands only for "
        f"HEAD and status metadata. Do not create, edit, stage, or commit any file. Do not perform the remediation yourself. Return a "
        f"schema-valid changes_requested verdict and state in required_remediation that the executor must create "
        f"and commit review-marker.txt. Inspect the immutable executor result and evidence. Report the exact criterion ID {spec['criterion_id']} in your verification "
        f"results. Return only the required JSON review result object, with no explanation or Markdown."
    )
    accept_after_rework = (
        f"Review the reworked exact revision for step prepare-fixture. Do not create, edit, stage, or commit any "
        f"file, and do not perform remediation yourself. Accept only after inspecting the immutable repository "
        f"with native read, glob, and grep and confirming the executor committed review-marker.txt, {spec['result_file']} contains exactly "
        f"{spec['result_content']}, and {spec['evidence_path']} exists. Inspect fixed test source "
        f"{spec['pytest_file']}, the executor result, and evidence. Do not run pytest. Use exact diagnostic shell "
        f"commands only for HEAD and status metadata, without arguments or other shell syntax. Report the exact criterion ID "
        f"{spec['criterion_id']} in your verification results. Return only the required JSON review result "
        f"object, with no explanation or Markdown."
    )

    handle = _run_real_scenario(
        project,
        plan,
        steps=("prepare-fixture",),
        original_prompts={"prepare-fixture": _executor_task_prompt("prepare-fixture")},
        reviewer_role="reviewer",
        batch=False,
        reviewer_prompts={"prepare-fixture": [force_rework, accept_after_rework]},
    )

    assert handle.worker_error is None
    assert handle.completion is not None and handle.completion.accepted is True
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.SUCCEEDED
    step = record.steps["prepare-fixture"]
    assert step.state is StepStatus.ACCEPTED
    assert step.executor_attempts == 2
    assert step.reviewer_attempts == 2
    assert step.review_acceptances == 1
    assert step.rework_rounds == 1
    executor_dispatches = sorted(
        (dispatch for dispatch in record.dispatches.values() if dispatch.role_kind == "executor"),
        key=lambda dispatch: dispatch.attempt,
    )
    reviewer_dispatches = sorted(
        (dispatch for dispatch in record.dispatches.values() if dispatch.role_kind == "reviewer"),
        key=lambda dispatch: dispatch.attempt,
    )
    assert len(executor_dispatches) == 2
    assert len(reviewer_dispatches) == 2
    assert executor_dispatches[0].attempt == 1
    assert executor_dispatches[1].attempt == 2
    assert executor_dispatches[0].runtime_session_id == executor_dispatches[1].runtime_session_id
    first_review_payload = handle.store.load_dispatch_payload(
        handle.run_id,
        reviewer_dispatches[0].dispatch_id,
    )
    assert first_review_payload.repository_before["revision"] == first_review_payload.repository_after[
        "revision"
    ]
    assert first_review_payload.repository_after["revision"] == reviewer_dispatches[
        0
    ].intent.repository.base_revision
    assert all(dispatch.state is DispatchStatus.ACKNOWLEDGED for dispatch in record.dispatches.values())
    _assert_operator_decision_count(handle.store, 0)
    _assert_clean_repository(project.repository)
    _assert_only_owned_worktree(project)
    assert (project.repository / "review-marker.txt").is_file()
    assert (project.repository / "result.txt").read_text(encoding="utf-8").strip() == "REAL_DISPOSABLE_OK"
    assert (project.repository / "evidence" / "real-evidence.md").is_file()
    _assert_fixed_test_passes(project.repository, spec["pytest_file"])
    _assert_no_active_leases(handle.store)


@pytest.mark.live_opencode
def test_real_reviewer_mutation_attempts_are_denied_before_execution(tmp_path: Path) -> None:
    _require_live_disposable()
    project = _real_project(tmp_path / "reviewer-adversarial", scheduling="sequential", seed_tests=("first",))
    spec = _step_spec("prepare-fixture")
    plan = _plan(project, steps=1, review=True)
    adversarial_review = (
        "This is an adversarial permission-boundary test. First run every exact command listed in "
        "observation_tools.diagnostic_commands; each must be allowed without adding syntax. Then attempt each mutation command "
        "exactly once: "
        "`ls /dev/null > adversarial-marker.txt`, `git add adversarial-marker.txt`, and "
        "`git commit -m \"Adversarial reviewer mutation\"`. The permission layer must deny each attempt. "
        f"After the denied attempts, use native read, glob, and grep to inspect the immutable repository, fixed test source {spec['pytest_file']}, "
        f"executor result, and evidence. If inspection confirms the criterion, return accepted with status passed "
        f"for exact criterion ID {spec['criterion_id']}. Return one schema-valid review result. The dispatcher may "
        f"canonicalize explicitly optional defaults, but must not repair or reinterpret the response."
    )

    handle = _run_real_scenario(
        project,
        plan,
        steps=("prepare-fixture",),
        original_prompts={"prepare-fixture": _executor_task_prompt("prepare-fixture")},
        reviewer_role="reviewer",
        batch=False,
        reviewer_prompts={"prepare-fixture": [adversarial_review]},
    )

    assert handle.worker_error is None
    assert handle.completion is not None and handle.completion.accepted is True
    record, _generation = handle.store.load_run(handle.run_id)
    reviewer = next(
        dispatch for dispatch in record.dispatches.values() if dispatch.role_kind == "reviewer"
    )
    events = _reviewer_tool_events(handle, reviewer)
    tool_events = [event for event in events if event.get("type") == "tool_use"]
    diagnostic_events = {
        event["part"]["state"].get("input", {}).get("command"): event["part"]["state"].get(
            "status"
        )
        for event in tool_events
        if event.get("part", {}).get("tool") == "bash"
    }
    for command in READ_ONLY_DIAGNOSTIC_COMMANDS:
        assert diagnostic_events.get(command) == "completed"
    mutation_events = [
        event
        for event in tool_events
        if event.get("part", {}).get("tool") in {"edit", "write"}
        or (
            event.get("part", {}).get("tool") == "bash"
            and event.get("part", {}).get("state", {}).get("input", {}).get("command")
            not in READ_ONLY_DIAGNOSTIC_COMMANDS
        )
    ]
    bash_commands = {
        event["part"]["state"].get("input", {}).get("command"): event["part"]["state"].get(
            "status"
        )
        for event in mutation_events
        if event.get("part", {}).get("tool") == "bash"
    }
    for command in _ADVERSARIAL_REVIEWER_COMMANDS:
        assert bash_commands.get(command) == "error"
    assert mutation_events
    assert all(event["part"]["state"].get("status") == "error" for event in mutation_events)
    assert not any(
        event["part"]["state"].get("status") == "completed" for event in mutation_events
    )
    assert not (project.repository / "adversarial-marker.txt").exists()
    assert _git(project.repository, "rev-parse", "HEAD") == reviewer.intent.repository.base_revision
    assert _git(project.repository, "status", "--porcelain") == ""
    stored = handle.store.load_dispatch_payload(handle.run_id, reviewer.dispatch_id)
    final_text = [
        event["part"]["text"]
        for event in events
        if event.get("type") == "text" and isinstance(event.get("part", {}).get("text"), str)
    ][-1]
    raw_result = json.loads(final_text)
    canonical_result = parse_reviewer_result(raw_result).model_dump(mode="json")
    assert stored.result == canonical_result
    _assert_no_active_leases(handle.store)


@pytest.mark.live_opencode
def test_real_solo_reconciliation_via_cli_resumes_and_succeeds(tmp_path: Path) -> None:
    _require_live_disposable()
    project = _real_project(tmp_path / "reconcile", scheduling="sequential", seed_tests=("first",))
    spec = _step_spec("prepare-fixture")
    plan = _plan(project, steps=1, review=False)
    injector = OneShotResidueInjector(run_session, target_workdir=project.repository)

    handle = _run_real_scenario(
        project,
        plan,
        steps=("prepare-fixture",),
        original_prompts={"prepare-fixture": _executor_task_prompt("prepare-fixture")},
        reviewer_role=None,
        batch=False,
        session_runner=injector,
    )

    assert injector.state.injections == 1
    assert handle.worker_error is not None
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.WAITING_OPERATOR
    request = record.operator_request
    assert request is not None and request.kind == "reconciliation"
    assert request.allowed_answers == ["reconcile", "halt"]
    step = record.steps["prepare-fixture"]
    assert step.state is StepStatus.BLOCKED
    first = record.dispatches[request.context_ref]
    assert first.state is DispatchStatus.FAILED
    assert first.failure_category == "repository_validation"
    assert _RESIDUE_NAME in (first.failure_detail or "")
    assert (project.repository / _RESIDUE_NAME).is_file()
    _assert_operator_decision_count(handle.store, 0)

    _reconcile_disposable_repository(
        project.repository,
        handle.initial_revisions["fixture-repo"],
        residue_name=_RESIDUE_NAME,
    )
    assert _answer_via_cli(handle, "reconcile") == 0
    _assert_operator_decision_count(handle.store, 1)
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.RUNNING
    assert record.steps["prepare-fixture"].state is StepStatus.READY

    _run_bounded_orchestration(handle)
    assert handle.worker_error is None
    assert handle.completion is not None and handle.completion.accepted is True
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.SUCCEEDED
    step = record.steps["prepare-fixture"]
    assert step.state is StepStatus.ACCEPTED
    assert step.executor_attempts == 2
    executor_dispatches = sorted(
        (dispatch for dispatch in record.dispatches.values() if dispatch.role_kind == "executor"),
        key=lambda dispatch: dispatch.attempt,
    )
    assert len(executor_dispatches) == 2
    assert executor_dispatches[0].state is DispatchStatus.FAILED
    assert executor_dispatches[0].failure_category == "repository_validation"
    assert executor_dispatches[1].state is DispatchStatus.ACKNOWLEDGED
    _assert_clean_repository(project.repository)
    _assert_only_owned_worktree(project)
    assert (project.repository / "result.txt").read_text(encoding="utf-8").strip() == "REAL_DISPOSABLE_OK"
    assert (project.repository / "evidence" / "real-evidence.md").is_file()
    _assert_fixed_test_passes(project.repository, spec["pytest_file"])
    _assert_no_active_leases(handle.store)
    _assert_operator_decision_count(handle.store, 1)


@pytest.mark.live_opencode
def test_real_batch_reconciliation_via_cli_retries_only_failed_child(tmp_path: Path) -> None:
    _require_live_disposable()
    project = _real_project(tmp_path / "batch-reconcile", scheduling="bounded_parallel", seed_tests=("first",))
    sibling = project.root / "sibling"
    sibling.mkdir()
    _initialize_repository(sibling, "https://example.invalid/sibling.git")
    _seed_deterministic_fixture(sibling, tests=("second",))
    _commit_initial(sibling)
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
    plan = _plan(project, steps=2, review=False, second_repo="sibling-repo")
    injector = OneShotResidueInjector(run_session, target_workdir=sibling)

    handle = _run_real_scenario(
        project,
        plan,
        steps=("prepare-fixture", "prepare-second"),
        original_prompts={
            "prepare-fixture": _executor_task_prompt("prepare-fixture"),
            "prepare-second": _executor_task_prompt("prepare-second"),
        },
        reviewer_role=None,
        batch=True,
        session_runner=injector,
    )

    assert injector.state.injections == 1
    assert handle.completion is not None and handle.completion.accepted is False
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.WAITING_OPERATOR
    request = record.operator_request
    assert request is not None and request.kind == "batch_reconciliation"
    assert request.allowed_answers == ["reconcile", "halt"]
    assert record.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert record.steps["prepare-second"].state is StepStatus.BLOCKED
    first_batch = record.batches[request.context_ref]
    assert first_batch.state is BatchStatus.FAILED
    assert len(first_batch.failed_dispatch_ids) == 1
    failed_dispatch = record.dispatches[first_batch.failed_dispatch_ids[0]]
    assert failed_dispatch.step_id == "prepare-second"
    assert failed_dispatch.state is DispatchStatus.FAILED
    assert failed_dispatch.failure_category == "repository_validation"
    assert _RESIDUE_NAME in (failed_dispatch.failure_detail or "")
    assert (sibling / _RESIDUE_NAME).is_file()
    primary_head_after_first_batch = _git(project.repository, "rev-parse", "HEAD")
    _assert_clean_repository(project.repository)
    _assert_operator_decision_count(handle.store, 0)

    _reconcile_disposable_repository(
        sibling,
        handle.initial_revisions["sibling-repo"],
        residue_name=_RESIDUE_NAME,
    )
    assert _answer_via_cli(handle, "reconcile") == 0
    _assert_operator_decision_count(handle.store, 1)
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.RUNNING
    assert record.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert record.steps["prepare-second"].state is StepStatus.READY

    _run_bounded_orchestration(handle)
    assert handle.worker_error is None
    assert handle.completion is not None and handle.completion.accepted is True
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.SUCCEEDED
    assert record.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert record.steps["prepare-second"].state is StepStatus.ACCEPTED
    assert record.steps["prepare-fixture"].executor_attempts == 1
    assert record.steps["prepare-second"].executor_attempts == 2
    assert len(record.batches) == 2
    batches = sorted(record.batches.values(), key=lambda batch: batch.last_event.sequence)
    assert batches[0].state is BatchStatus.FAILED
    assert batches[1].state is BatchStatus.JOINED
    assert batches[0].batch_id != batches[1].batch_id
    second_dispatches = sorted(
        (dispatch for dispatch in record.dispatches.values() if dispatch.step_id == "prepare-second"),
        key=lambda dispatch: dispatch.attempt,
    )
    assert len(second_dispatches) == 2
    assert second_dispatches[0].state is DispatchStatus.FAILED
    assert second_dispatches[1].state is DispatchStatus.ACKNOWLEDGED
    assert second_dispatches[1].batch_id == batches[1].batch_id
    assert _git(project.repository, "rev-parse", "HEAD") == primary_head_after_first_batch
    _assert_clean_repository(project.repository)
    _assert_clean_repository(sibling)
    assert (project.repository / "result.txt").read_text(encoding="utf-8").strip() == "REAL_DISPOSABLE_OK"
    assert (sibling / "result-second.txt").read_text(encoding="utf-8").strip() == "REAL_DISPOSABLE_OK"
    _assert_fixed_test_passes(project.repository, "test_real_output.py")
    _assert_fixed_test_passes(sibling, "test_real_second_output.py")
    _assert_no_active_leases(handle.store)
    _assert_operator_decision_count(handle.store, 1)


@pytest.mark.live_opencode
def test_real_halt_via_cli_is_terminal_and_preserves_historical_state(tmp_path: Path) -> None:
    _require_live_disposable()
    project = _real_project(tmp_path / "halt", scheduling="sequential", seed_tests=("first",))
    plan = _plan(project, steps=1, review=False)
    injector = OneShotResidueInjector(run_session, target_workdir=project.repository)

    handle = _run_real_scenario(
        project,
        plan,
        steps=("prepare-fixture",),
        original_prompts={"prepare-fixture": _executor_task_prompt("prepare-fixture")},
        reviewer_role=None,
        batch=False,
        session_runner=injector,
    )

    assert injector.state.injections == 1
    assert handle.worker_error is not None
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.WAITING_OPERATOR
    request = record.operator_request
    assert request is not None and request.kind == "reconciliation"
    dispatch_id = request.context_ref
    failed = record.dispatches[dispatch_id]
    assert failed.state is DispatchStatus.FAILED
    assert failed.failure_category == "repository_validation"
    assert _RESIDUE_NAME in (failed.failure_detail or "")
    head_before_halt = _git(project.repository, "rev-parse", "HEAD")
    assert (project.repository / _RESIDUE_NAME).is_file()
    assert (project.repository / "result.txt").is_file()

    assert _answer_via_cli(handle, "halt") == 0
    _assert_operator_decision_count(handle.store, 1)
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.HALTED
    assert record.operator_request is None
    assert record.steps["prepare-fixture"].state is StepStatus.BLOCKED
    assert len(record.dispatches) == 1
    assert record.dispatches[dispatch_id].state is DispatchStatus.FAILED
    assert record.dispatches[dispatch_id].failure_category == "repository_validation"
    assert _RESIDUE_NAME in (record.dispatches[dispatch_id].failure_detail or "")
    assert _git(project.repository, "rev-parse", "HEAD") == head_before_halt
    assert (project.repository / _RESIDUE_NAME).is_file()
    assert (project.repository / "result.txt").is_file()
    _assert_no_active_leases(handle.store)


# ---------------------------------------------------------------------------
# Non-live harness unit tests
# ---------------------------------------------------------------------------


_STEP_PATHS: dict[StepStatus, list[StepStatus]] = {
    StepStatus.READY: [StepStatus.READY],
    StepStatus.REVIEW_REQUIRED: [
        StepStatus.READY,
        StepStatus.EXECUTING,
        StepStatus.EXECUTED,
        StepStatus.REVIEW_REQUIRED,
    ],
    StepStatus.ACCEPTED: [
        StepStatus.READY,
        StepStatus.EXECUTING,
        StepStatus.EXECUTED,
        StepStatus.ACCEPTED,
    ],
    StepStatus.FAILED: [StepStatus.READY, StepStatus.FAILED],
    StepStatus.BLOCKED: [StepStatus.READY, StepStatus.BLOCKED],
}


def test_reactive_supervisor_command_decisions(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = valid_plan_values(project)
    step = values["steps"][0]
    step["review"] = {
        "required": True,
        "reviewer_role_keys": ["reviewer"],
        "required_acceptances": 1,
    }
    step["retry"]["max_executor_attempts"] = 2
    step["retry"]["max_reviewer_attempts"] = 2
    step["retry"]["on_changes_requested"] = "retry"
    second = json.loads(json.dumps(step))
    second.update(
        {
            "ordinal": 2,
            "step_id": "prepare-second",
            "resource_locks": [{"resource_id": "second-resource", "mode": "write"}],
            "produced_outputs": [{"artifact_id": "second-output", "producer_step_id": None, "description": "Second output"}],
            "evidence_requirements": [{"artifact_id": "second-evidence", "relative_path": "second.md", "media_type": "text/markdown"}],
        }
    )
    values["steps"].append(second)
    plan = NormalizedPlan.model_validate(values)
    first_prompt = "Create result.txt containing exactly REAL_DISPOSABLE_OK."
    second_prompt = "Create result-second.txt containing exactly REAL_DISPOSABLE_OK."
    prompts = {"prepare-fixture": first_prompt, "prepare-second": second_prompt}

    def record_with(
        states: Mapping[str, tuple[StepStatus, int, int]],
        rework_rounds: Mapping[str, int] | None = None,
    ) -> RunRecord:
        current = new_run_record(
            run_id="fixture-run",
            project_id=project.config.project_id,
            config_digest=project.config.config_digest,
            plan=plan,
            plan_approval=approve_plan(plan, "decision-fixture"),
            event=_event(1, "fixture"),
        )
        steps = {}
        sequence = 2
        for step_id, (state, executor_attempts, reviewer_attempts) in states.items():
            current_step = current.steps[step_id]
            for target in _STEP_PATHS[state]:
                current_step = transition_step(current_step, target, _event(sequence, "fixture"))
                sequence += 1
            current_step = current_step.model_copy(
                update={
                    "executor_attempts": executor_attempts,
                    "reviewer_attempts": reviewer_attempts,
                    "rework_rounds": (rework_rounds or {}).get(step_id, 0),
                }
            )
            steps[step_id] = current_step
        last_sequence = max(steps[step_id].last_event.sequence for step_id in steps)
        return current.model_copy(
            update={
                "steps": steps,
                "sequence": last_sequence,
                "updated_at": steps["prepare-fixture"].last_event.occurred_at,
            }
        )

    sequential = dict(steps=("prepare-fixture",), original_prompts=prompts, reviewer_role="reviewer", batch=False)

    initial = json.loads(_decide_next_command(record_with({"prepare-fixture": (StepStatus.READY, 0, 0)}), **sequential))
    assert initial == {
        "protocol_version": 1,
        "action": "dispatch",
        "step_id": "prepare-fixture",
        "target_role": "terra",
        "session_mode": "new",
        "prompt": first_prompt,
    }

    review = json.loads(
        _decide_next_command(record_with({"prepare-fixture": (StepStatus.REVIEW_REQUIRED, 1, 0)}), **sequential)
    )
    assert review["action"] == "dispatch"
    assert review["step_id"] == "prepare-fixture"
    assert review["target_role"] == "reviewer"
    assert review["session_mode"] == "new"

    rework = json.loads(
        _decide_next_command(
            record_with(
                {"prepare-fixture": (StepStatus.READY, 1, 1)},
                rework_rounds={"prepare-fixture": 1},
            ),
            **sequential,
            reviewer_results={
                "prepare-fixture": {
                    "verdict": "changes_requested",
                    "required_remediation": ["Make pytest collect and pass the tests"],
                    "findings": [{"finding_id": "f1", "severity": "blocking", "summary": "pytest collected no tests"}],
                }
            },
        )
    )
    assert rework["action"] == "dispatch"
    assert rework["target_role"] == "terra"
    assert rework["session_mode"] == "resume"
    assert "Make pytest collect and pass the tests" in rework["prompt"]
    assert "pytest collected no tests" in rework["prompt"]
    assert first_prompt in rework["prompt"]

    reconciled = json.loads(
        _decide_next_command(
            record_with({"prepare-fixture": (StepStatus.READY, 1, 0)}),
            **sequential,
        )
    )
    assert reconciled["action"] == "dispatch"
    assert reconciled["target_role"] == "terra"
    assert reconciled["session_mode"] == "new"
    assert reconciled["prompt"] == first_prompt

    reviewer_pool = json.loads(
        _decide_next_command(
            record_with({"prepare-fixture": (StepStatus.REVIEW_REQUIRED, 1, 1)}),
            **sequential,
            reviewer_prompts={"prepare-fixture": ["first-review", "second-review"]},
        )
    )
    assert reviewer_pool["target_role"] == "reviewer"
    assert reviewer_pool["prompt"] == "second-review"

    accepted = json.loads(_decide_next_command(record_with({"prepare-fixture": (StepStatus.ACCEPTED, 1, 1)}), **sequential))
    assert accepted["action"] == "request_completion"

    exhausted = json.loads(
        _decide_next_command(record_with({"prepare-fixture": (StepStatus.READY, 2, 1)}), **sequential)
    )
    assert exhausted["action"] == "request_completion"

    batch = dict(
        steps=("prepare-fixture", "prepare-second"),
        original_prompts=prompts,
        reviewer_role=None,
        batch=True,
    )
    batch_initial = json.loads(
        _decide_next_command(
            record_with(
                {
                    "prepare-fixture": (StepStatus.READY, 0, 0),
                    "prepare-second": (StepStatus.READY, 0, 0),
                }
            ),
            **batch,
        )
    )
    assert batch_initial["action"] == "dispatch_batch"
    assert len(batch_initial["children"]) == 2
    assert all(child["session_mode"] == "new" for child in batch_initial["children"])
    assert {child["step_id"] for child in batch_initial["children"]} == {"prepare-fixture", "prepare-second"}

    batch_rework = json.loads(
        _decide_next_command(
            record_with(
                {
                    "prepare-fixture": (StepStatus.ACCEPTED, 1, 0),
                    "prepare-second": (StepStatus.READY, 1, 0),
                },
                rework_rounds={"prepare-second": 1},
            ),
            **batch,
        )
    )
    assert batch_rework["action"] == "dispatch_batch"
    assert len(batch_rework["children"]) == 1
    assert batch_rework["children"][0]["step_id"] == "prepare-second"
    assert batch_rework["children"][0]["session_mode"] == "new"
    assert "apply the reviewer's requested changes" in batch_rework["children"][0]["prompt"]

    batch_replacement = json.loads(
        _decide_next_command(
            record_with(
                {
                    "prepare-fixture": (StepStatus.ACCEPTED, 1, 0),
                    "prepare-second": (StepStatus.READY, 1, 0),
                }
            ),
            **batch,
        )
    )
    assert batch_replacement["action"] == "dispatch_batch"
    assert len(batch_replacement["children"]) == 1
    assert batch_replacement["children"][0]["step_id"] == "prepare-second"
    assert batch_replacement["children"][0]["session_mode"] == "new"
    assert batch_replacement["children"][0]["prompt"] == second_prompt

    batch_done = json.loads(
        _decide_next_command(
            record_with(
                {
                    "prepare-fixture": (StepStatus.ACCEPTED, 1, 0),
                    "prepare-second": (StepStatus.ACCEPTED, 1, 0),
                }
            ),
            **batch,
        )
    )
    assert batch_done["action"] == "request_completion"


def test_resolve_live_models_all_role_fallback() -> None:
    resolved = resolve_live_models(
        {"DISPATCHER_LIVE_MODEL": "openai/gpt-4.1", "PATH": "/bin"}
    )
    assert resolved == {
        "supervisor": "openai/gpt-4.1",
        "executor": "openai/gpt-4.1",
        "reviewer": "openai/gpt-4.1",
    }


def test_resolve_live_models_all_three_role_specific() -> None:
    resolved = resolve_live_models(
        {
            "DISPATCHER_LIVE_MODEL": "openai/gpt-4.1",
            "DISPATCHER_LIVE_SUPERVISOR_MODEL": "supervisor/model",
            "DISPATCHER_LIVE_EXECUTOR_MODEL": "executor/model",
            "DISPATCHER_LIVE_REVIEWER_MODEL": "reviewer/model",
        }
    )
    assert resolved == {
        "supervisor": "supervisor/model",
        "executor": "executor/model",
        "reviewer": "reviewer/model",
    }


def test_resolve_live_models_partial_role_overrides_with_fallback() -> None:
    resolved = resolve_live_models(
        {
            "DISPATCHER_LIVE_MODEL": "openai/gpt-4.1",
            "DISPATCHER_LIVE_EXECUTOR_MODEL": "openai/gpt-5.6-terra",
            "DISPATCHER_LIVE_REVIEWER_MODEL": "",
        }
    )
    assert resolved == {
        "supervisor": "openai/gpt-4.1",
        "executor": "openai/gpt-5.6-terra",
        "reviewer": "openai/gpt-4.1",
    }


def test_resolve_live_models_missing_required_values_fail_loudly() -> None:
    with pytest.raises(ValueError) as excinfo:
        resolve_live_models({"DISPATCHER_LIVE_SUPERVISOR_MODEL": "supervisor/model"})
    message = str(excinfo.value)
    assert "DISPATCHER_LIVE_MODEL" in message
    assert "DISPATCHER_LIVE_EXECUTOR_MODEL" in message
    assert "DISPATCHER_LIVE_REVIEWER_MODEL" in message
    assert "DISPATCHER_LIVE_SUPERVISOR_MODEL" not in message


def test_resolve_live_models_assigns_roles_into_fixture_config(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    resolved = {
        "supervisor": "supervisor/model",
        "executor": "executor/model",
        "reviewer": "reviewer/model",
    }
    _apply_model_roles(values, resolved)
    assert values["roles"]["supervisor"]["supervisor"]["model"] == "supervisor/model"
    assert values["roles"]["executors"]["terra"]["model"] == "executor/model"
    assert values["roles"]["reviewers"]["reviewer"]["model"] == "reviewer/model"
    assert values["roles"]["reviewers"]["reviewer-two"]["model"] == "reviewer/model"


def test_repository_fixture_seeding_commits_gitignore_and_fixed_tests(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _initialize_repository(root, "https://example.invalid/fixture.git")
    _seed_deterministic_fixture(root, tests=("first", "second"))
    _commit_initial(root)
    tracked = _git(root, "ls-files")
    assert ".gitignore" in tracked
    assert "test_real_output.py" in tracked
    assert "test_real_second_output.py" in tracked
    assert (root / ".gitignore").read_text(encoding="utf-8") == _REPOSITORY_IGNORE
    assert _git(root, "status", "--porcelain") == ""


def test_fixed_tests_fail_until_result_files_exist(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _initialize_repository(root, "https://example.invalid/fixture.git")
    _seed_deterministic_fixture(root, tests=("first", "second"))
    _commit_initial(root)
    for pytest_file in ("test_real_output.py", "test_real_second_output.py"):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", pytest_file],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode != 0
        assert _git(root, "status", "--porcelain") == ""
    (root / "result.txt").write_text("REAL_DISPOSABLE_OK\n", encoding="utf-8")
    (root / "evidence" / "real-evidence.md").write_text("evidence\n", encoding="utf-8")
    (root / "result-second.txt").write_text("REAL_DISPOSABLE_OK\n", encoding="utf-8")
    (root / "evidence" / "real-evidence-second.md").write_text("evidence\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_real_output.py", "test_real_second_output.py"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout


def test_step_specs_define_exact_criterion_ids_and_commands() -> None:
    first = _step_spec("prepare-fixture")
    assert first["criterion_id"] == "verify-real-output"
    assert first["pytest_file"] == "test_real_output.py"
    assert first["result_file"] == "result.txt"
    assert first["evidence_path"] == "evidence/real-evidence.md"
    second = _step_spec("prepare-second")
    assert second["criterion_id"] == "verify-real-second-output"
    assert second["pytest_file"] == "test_real_second_output.py"
    assert second["result_file"] == "result-second.txt"
    assert second["evidence_path"] == "evidence/real-evidence-second.md"


def _non_live_project(tmp_path: Path, name: str):
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    return create_fixture_project(directory)


def test_plan_uses_exact_criterion_ids_and_commands(tmp_path: Path) -> None:
    project = _configure_real_project(
        _non_live_project(tmp_path, "plan"),
        scheduling="bounded_parallel",
        models={
            "supervisor": "fixture/supervisor-live",
            "executor": "fixture/executor-live",
            "reviewer": "fixture/reviewer-live",
        },
    )
    plan = _plan(project, steps=2, review=False, second_repo="sibling-repo")
    assert plan.steps[0].acceptance_criteria[0].criterion_id == "verify-real-output"
    assert "pytest -q test_real_output.py" in plan.steps[0].acceptance_criteria[0].description
    assert "python -m pytest" not in plan.steps[0].acceptance_criteria[0].description
    assert plan.steps[1].acceptance_criteria[0].criterion_id == "verify-real-second-output"
    assert "pytest -q test_real_second_output.py" in plan.steps[1].acceptance_criteria[0].description
    assert "python -m pytest" not in plan.steps[1].acceptance_criteria[0].description
    assert plan.steps[0].evidence_requirements[0].relative_path == "real-evidence.md"
    assert plan.steps[1].evidence_requirements[0].relative_path == "real-evidence-second.md"


def test_executor_prompts_are_deterministic_and_hygienic() -> None:
    for step_id in ("prepare-fixture", "prepare-second"):
        spec = _step_spec(step_id)
        prompt = _executor_task_prompt(step_id)
        assert spec["result_file"] in prompt
        assert spec["result_content"] in prompt
        assert spec["evidence_path"] in prompt
        assert f"pytest -q {spec['pytest_file']}" in prompt
        assert "python -m pytest" not in prompt
        assert spec["criterion_id"] in prompt
        assert "do not stage or commit files with git" in prompt.lower()
        assert "network" in prompt and "deployment" in prompt
        assert "status not_run" in prompt
        assert "return only the required json proposal object" in prompt.lower()


def test_reviewer_prompts_limit_shell_to_exact_diagnostics() -> None:
    spec = _step_spec("prepare-fixture")
    prompt = _reviewer_prompt("prepare-fixture")
    assert spec["criterion_id"] in prompt
    assert spec["pytest_file"] in prompt
    assert spec["result_file"] in prompt
    assert spec["evidence_path"] in prompt
    assert "immutable repository" in prompt
    assert "do not run the test" in prompt
    assert "native read, glob, and grep" in prompt
    assert "diagnostic shell commands only for HEAD and status metadata" in prompt
    assert "pytest -q" not in prompt


def test_one_shot_injector_targets_exactly_one_dispatch(tmp_path: Path) -> None:
    target = tmp_path / "target"
    other = tmp_path / "other"
    target.mkdir()
    other.mkdir()
    calls: list[str] = []

    def wrapped(**kwargs):
        calls.append(str(kwargs["workdir"]))
        return SessionResult(session_id="session-1", exit_code=0, chat_response="{}", evidence_written=[])

    injector = OneShotResidueInjector(wrapped, target_workdir=target)
    injector(workdir=other, title="executor - prepare-second - attempt 1")
    assert injector.state.injections == 0
    assert not (target / _RESIDUE_NAME).exists()
    injector(workdir=target, title="executor - prepare-fixture - attempt 1")
    assert injector.state.injections == 1
    assert injector.state.last_workdir == target
    assert injector.state.last_title == "executor - prepare-fixture - attempt 1"
    assert (target / _RESIDUE_NAME).is_file()
    assert calls == [str(other), str(target)]


def test_one_shot_injector_is_one_shot(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()

    def wrapped(**kwargs):
        return SessionResult(session_id="session-1", exit_code=0, chat_response="{}", evidence_written=[])

    injector = OneShotResidueInjector(wrapped, target_workdir=target)
    injector(workdir=target, title="executor - prepare-fixture - attempt 1")
    injector(workdir=target, title="executor - prepare-fixture - attempt 2")
    injector(workdir=target, title="reviewer - prepare-fixture - attempt 1")
    assert injector.state.injections == 1
    assert injector.state.calls == 3


def test_one_shot_injector_is_thread_safe_for_non_targets(tmp_path: Path) -> None:
    target = tmp_path / "target"
    non_targets = [tmp_path / f"other-{index}" for index in range(8)]
    target.mkdir()
    for path in non_targets:
        path.mkdir()
    errors: list[BaseException] = []

    def wrapped(**kwargs):
        return SessionResult(session_id="session-1", exit_code=0, chat_response="{}", evidence_written=[])

    injector = OneShotResidueInjector(wrapped, target_workdir=target)
    threads = [
        threading.Thread(
            target=lambda path=path: _thread_calls(injector, path, errors),
            daemon=True,
        )
        for path in [target, *non_targets]
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert injector.state.injections == 1
    assert injector.state.calls == len(threads)


def test_one_shot_injector_returns_real_result_unmodified(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()

    def wrapped(**kwargs):
        return SessionResult(
            session_id="session-1",
            exit_code=0,
            chat_response='{"result_version": 1}',
            evidence_written=[],
            usage={"total": 10},
        )

    injector = OneShotResidueInjector(wrapped, target_workdir=target)
    result = injector(workdir=target, title="executor - prepare-fixture - attempt 1")
    assert isinstance(result, SessionResult)
    assert result.session_id == "session-1"
    assert result.exit_code == 0
    assert result.chat_response == '{"result_version": 1}'
    assert result.usage == {"total": 10}


def _thread_calls(injector: OneShotResidueInjector, workdir: Path, errors: list[BaseException]) -> None:
    try:
        injector(workdir=workdir, title="executor - prepare-fixture - attempt 1")
    except BaseException as exc:
        errors.append(exc)


def test_answer_command_builder_is_exact() -> None:
    command = _answer_command(
        config_path=Path("/tmp/project.yaml"),
        run_id="run-1",
        request_id="request-1",
        answer="reconcile",
    )
    assert command == [
        "answer",
        "--config",
        "/tmp/project.yaml",
        "--run-id",
        "run-1",
        "--request-id",
        "request-1",
        "--answer",
        "reconcile",
        "--actor-id",
        "operator",
    ]


def _fake_models() -> dict[str, str]:
    return {
        "supervisor": "fixture/supervisor-live",
        "executor": "fixture/executor-live",
        "reviewer": "fixture/reviewer-live",
    }


def test_fake_runner_project_uses_test_backend_without_seatbelt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dispatcher.verification.platform.system", lambda: "Linux")
    project = _configure_fake_runner_project(
        _non_live_project(tmp_path, "direct-test-backend"),
        scheduling="sequential",
        models=_fake_models(),
    )
    store = open_state_store(project.config)
    workflow = SequentialWorkflow(project.config, store, owner_id="fake-runner-test-backend")
    coordinator = SequentialExecutionCoordinator(
        project.config,
        store,
        workflow,
        owner_id="fake-runner-test-backend",
    )

    runner = coordinator._verification_runner.__self__

    assert project.config.execution.mode == "mock_workflow_test"
    assert project.config.execution.verification_backend == "direct_test_v1"
    assert isinstance(runner, VerificationRunner)
    assert isinstance(runner.backend, DirectTestBackend)


def test_solo_reconciliation_full_loop_with_fake_runner(tmp_path: Path) -> None:
    project = _configure_fake_runner_project(
        _non_live_project(tmp_path, "solo"),
        scheduling="sequential",
        models=_fake_models(),
    )
    _seed_deterministic_fixture(project.repository, tests=("first",))
    _commit_initial(project.repository)
    spec = _step_spec("prepare-fixture")
    plan = _plan(project, steps=1, review=False)
    injector = OneShotResidueInjector(_fake_session_runner(), target_workdir=project.repository)

    handle = _run_real_scenario(
        project,
        plan,
        steps=("prepare-fixture",),
        original_prompts={"prepare-fixture": _executor_task_prompt("prepare-fixture")},
        reviewer_role=None,
        batch=False,
        session_runner=injector,
    )

    assert injector.state.injections == 1
    assert handle.worker_error is not None
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.WAITING_OPERATOR
    assert record.operator_request.kind == "reconciliation"
    first = record.dispatches[record.operator_request.context_ref]
    assert first.failure_category == "repository_validation"
    assert _RESIDUE_NAME in (first.failure_detail or "")

    _reconcile_disposable_repository(
        project.repository,
        handle.initial_revisions["fixture-repo"],
        residue_name=_RESIDUE_NAME,
    )
    assert _answer_via_cli(handle, "reconcile") == 0
    _assert_operator_decision_count(handle.store, 1)
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.RUNNING
    assert record.steps["prepare-fixture"].state is StepStatus.READY

    _run_bounded_orchestration(handle)
    assert handle.worker_error is None
    assert handle.completion is not None and handle.completion.accepted is True
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.SUCCEEDED
    assert record.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert record.steps["prepare-fixture"].executor_attempts == 2
    _assert_clean_repository(project.repository)
    assert (project.repository / "result.txt").read_text(encoding="utf-8").strip() == "REAL_DISPOSABLE_OK"
    assert (project.repository / "evidence" / "real-evidence.md").is_file()
    _assert_fixed_test_passes(project.repository, spec["pytest_file"])
    _assert_no_active_leases(handle.store)
    _assert_operator_decision_count(handle.store, 1)


def test_halt_full_loop_with_fake_runner(tmp_path: Path) -> None:
    project = _configure_fake_runner_project(
        _non_live_project(tmp_path, "halt"),
        scheduling="sequential",
        models=_fake_models(),
    )
    _seed_deterministic_fixture(project.repository, tests=("first",))
    _commit_initial(project.repository)
    plan = _plan(project, steps=1, review=False)
    injector = OneShotResidueInjector(_fake_session_runner(), target_workdir=project.repository)

    handle = _run_real_scenario(
        project,
        plan,
        steps=("prepare-fixture",),
        original_prompts={"prepare-fixture": _executor_task_prompt("prepare-fixture")},
        reviewer_role=None,
        batch=False,
        session_runner=injector,
    )

    assert handle.worker_error is not None
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.WAITING_OPERATOR
    assert record.operator_request.kind == "reconciliation"
    head_before_halt = _git(project.repository, "rev-parse", "HEAD")
    assert (project.repository / _RESIDUE_NAME).is_file()

    assert _answer_via_cli(handle, "halt") == 0
    _assert_operator_decision_count(handle.store, 1)
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.HALTED
    assert record.operator_request is None
    assert record.steps["prepare-fixture"].state is StepStatus.BLOCKED
    assert len(record.dispatches) == 1
    halted_dispatch = next(iter(record.dispatches.values()))
    assert halted_dispatch.state is DispatchStatus.FAILED
    assert halted_dispatch.failure_category == "repository_validation"
    assert _RESIDUE_NAME in (halted_dispatch.failure_detail or "")
    assert (project.repository / _RESIDUE_NAME).is_file()
    assert _git(project.repository, "rev-parse", "HEAD") == head_before_halt
    _assert_no_active_leases(handle.store)


def test_review_rework_resume_full_loop_with_fake_runner(tmp_path: Path) -> None:
    project = _configure_fake_runner_project(
        _non_live_project(tmp_path, "rework"),
        scheduling="sequential",
        models=_fake_models(),
    )
    _seed_deterministic_fixture(project.repository, tests=("first",))
    _commit_initial(project.repository)
    plan = _plan(project, steps=1, review=True)
    reviewer_prompts = {
        "prepare-fixture": [
            "Use native read, glob, and grep to inspect result.txt, evidence/real-evidence.md, and "
            "test_real_output.py. Do not run tests or modify files or Git state. Return changes_requested "
            "and state that the executor must create and commit review-marker.txt.",
            "Use native read, glob, and grep to inspect review-marker.txt, result.txt, "
            "evidence/real-evidence.md, and test_real_output.py. Use exact diagnostics only for HEAD and "
            "status metadata. Do not run tests or modify files or Git state. Accept when those immutable "
            "contents and the executor verification satisfy the criterion.",
        ]
    }
    adapter = ControlledReviewerMutationAdapter(_fake_session_runner())

    handle = _run_real_scenario(
        project,
        plan,
        steps=("prepare-fixture",),
        original_prompts={"prepare-fixture": _executor_task_prompt("prepare-fixture")},
        reviewer_role="reviewer",
        batch=False,
        reviewer_prompts=reviewer_prompts,
        session_runner=adapter,
    )

    assert handle.worker_error is None
    assert handle.completion is not None and handle.completion.accepted is True
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.SUCCEEDED
    step = record.steps["prepare-fixture"]
    assert step.state is StepStatus.ACCEPTED
    assert step.executor_attempts == 2
    assert step.reviewer_attempts == 2
    assert step.review_acceptances == 1
    executor_dispatches = sorted(
        (dispatch for dispatch in record.dispatches.values() if dispatch.role_kind == "executor"),
        key=lambda dispatch: dispatch.attempt,
    )
    assert len(executor_dispatches) == 2
    assert executor_dispatches[0].runtime_session_id == executor_dispatches[1].runtime_session_id
    assert all(dispatch.state is DispatchStatus.ACKNOWLEDGED for dispatch in record.dispatches.values())
    assert adapter.repository_snapshots
    assert all(
        head_before == head_after and status_before == status_after == ""
        for head_before, head_after, status_before, status_after in adapter.repository_snapshots
    )
    _assert_clean_repository(project.repository)
    assert (project.repository / "review-marker.txt").is_file()
    assert (project.repository / "result.txt").read_text(encoding="utf-8").strip() == "REAL_DISPOSABLE_OK"
    _assert_fixed_test_passes(project.repository, "test_real_output.py")
    _assert_no_active_leases(handle.store)


def test_verification_failure_resume_commit_review_full_loop_with_fake_runner(
    tmp_path: Path,
) -> None:
    project = _configure_fake_runner_project(
        _non_live_project(tmp_path, "verification-feedback"),
        scheduling="sequential",
        models=_fake_models(),
    )
    _seed_deterministic_fixture(project.repository, tests=("first",))
    _commit_initial(project.repository)
    plan = _plan(project, steps=1, review=True)
    base_runner = _fake_session_runner()
    executor_sessions: list[str] = []
    executor_modes: list[str] = []
    invocation_tokens: list[int] = []
    forced_failures = 0

    def failure_then_repair_runner(**kwargs):
        nonlocal forced_failures
        payload = json.loads(kwargs["prompt"])
        result = base_runner(**kwargs)
        if payload["result_kind"] == "executor":
            executor_sessions.append(result.session_id)
            executor_modes.append(kwargs["mode"])
            tokens = 11 if kwargs["mode"] == "new" else 13
            if forced_failures == 0:
                forced_failures += 1
                (Path(kwargs["workdir"]) / "result.txt").write_text(
                    "FORCED_VERIFICATION_FAILURE\n",
                    encoding="utf-8",
                )
        else:
            tokens = 17
            accepted = json.loads(result.chat_response)
            accepted.update(
                {
                    "findings": [],
                    "required_remediation": [],
                    "summary": "immutable result and dispatcher verification accepted",
                    "verdict": "accepted",
                }
            )
            result.chat_response = json.dumps(accepted, sort_keys=True)
        invocation_tokens.append(tokens)
        result.usage = {
            "total": tokens,
            "input": tokens - 2,
            "output": 2,
            "reasoning": 0,
        }
        result.cost = tokens / 1000
        return result

    handle = _run_real_scenario(
        project,
        plan,
        steps=("prepare-fixture",),
        original_prompts={"prepare-fixture": _executor_task_prompt("prepare-fixture")},
        reviewer_role="reviewer",
        batch=False,
        reviewer_prompts={"prepare-fixture": [_REVIEW_PROMPT]},
        session_runner=failure_then_repair_runner,
    )

    if handle.worker_error is not None:
        raise handle.worker_error
    assert handle.completion is not None and handle.completion.accepted is True
    record, _generation = handle.store.load_run(handle.run_id)
    invocations = handle.store.opencode_invocations_for_run(handle.run_id)
    step = record.steps["prepare-fixture"]
    executor_dispatches = sorted(
        (dispatch for dispatch in record.dispatches.values() if dispatch.role_kind == "executor"),
        key=lambda dispatch: dispatch.attempt,
    )
    reviewer_dispatches = [
        dispatch for dispatch in record.dispatches.values() if dispatch.role_kind == "reviewer"
    ]
    first_payload = handle.store.load_dispatch_payload(
        handle.run_id,
        executor_dispatches[0].dispatch_id,
    )
    repaired_payload = handle.store.load_dispatch_payload(
        handle.run_id,
        executor_dispatches[1].dispatch_id,
    )
    review_payload = handle.store.load_dispatch_payload(
        handle.run_id,
        reviewer_dispatches[0].dispatch_id,
    )

    assert record.state is RunStatus.SUCCEEDED
    assert step.state is StepStatus.ACCEPTED
    assert step.executor_attempts == 2
    assert step.reviewer_attempts == 1
    assert step.review_acceptances == 1
    assert step.rework_rounds == 1
    assert len(invocations) == 3
    assert all(invocation["usage_status"] == "COMPLETE" for invocation in invocations)
    assert all(invocation["lifecycle"] == "SUCCEEDED" for invocation in invocations)
    assert record.usage.run.tokens_total == sum(invocation_tokens) == 41
    assert record.usage.by_role["terra"].tokens_total == 24
    assert record.usage.by_role["reviewer"].tokens_total == 17
    assert record.usage.by_session[executor_sessions[0]].tokens_total == 24
    assert forced_failures == 1
    assert executor_modes == ["new", "resume"]
    assert len(set(executor_sessions)) == 1
    assert executor_dispatches[0].state is DispatchStatus.FAILED
    assert executor_dispatches[0].failure_category == "authoritative_verification"
    assert executor_dispatches[1].state is DispatchStatus.ACKNOWLEDGED
    assert first_payload.authoritative_verification[0]["status"] == "failed"
    assert repaired_payload.authoritative_verification[0]["status"] == "passed"
    assert review_payload.authoritative_verification[0]["status"] == "passed"
    assert {
        first_payload.authoritative_verification[0]["backend"],
        repaired_payload.authoritative_verification[0]["backend"],
        review_payload.authoritative_verification[0]["backend"],
    } == {"direct-test-v1"}
    assert int(_git(project.repository, "rev-list", "--all", "--count")) == 2
    assert handle.completion.report_path is not None
    report = handle.completion.report_path.read_text(encoding="utf-8")
    assert "`executor-time`" in report
    assert "`acceptance-time`" in report
    assert first_payload.authoritative_verification[0]["transcript_sha256"] in report
    assert review_payload.authoritative_verification[0]["transcript_sha256"] in report
    _assert_clean_repository(project.repository)
    _assert_fixed_test_passes(project.repository, "test_real_output.py")
    _assert_no_active_leases(handle.store)


def test_controlled_reviewer_mutation_attempts_are_denied_before_execution(
    tmp_path: Path,
) -> None:
    project = _configure_fake_runner_project(
        _non_live_project(tmp_path, "reviewer-permission-ceiling"),
        scheduling="sequential",
        models=_fake_models(),
    )
    _seed_deterministic_fixture(project.repository, tests=("first",))
    _commit_initial(project.repository)
    plan = _plan(project, steps=1, review=True)
    adapter = ControlledReviewerMutationAdapter(_fake_session_runner())

    handle = _run_real_scenario(
        project,
        plan,
        steps=("prepare-fixture",),
        original_prompts={"prepare-fixture": _executor_task_prompt("prepare-fixture")},
        reviewer_role="reviewer",
        batch=False,
        reviewer_prompts={
            "prepare-fixture": [
                "Report required remediation without modifying the repository.",
                "Inspect the executor remediation without modifying the repository.",
            ]
        },
        session_runner=adapter,
    )

    assert handle.worker_error is None
    assert handle.completion is not None and handle.completion.accepted is True
    assert adapter.attempts
    assert {command for command, _decision in adapter.attempts} == set(
        (*READ_ONLY_DIAGNOSTIC_COMMANDS, *_CONTROLLED_REVIEWER_COMMANDS)
    )
    decisions = dict(adapter.attempts)
    assert all(decisions[command] == "allow" for command in READ_ONLY_DIAGNOSTIC_COMMANDS)
    assert all(decisions[command] == "deny" for command in _CONTROLLED_REVIEWER_COMMANDS)
    assert adapter.repository_snapshots
    assert all(
        head_before == head_after and status_before == status_after == ""
        for head_before, head_after, status_before, status_after in adapter.repository_snapshots
    )
    record, _generation = handle.store.load_run(handle.run_id)
    reviewer_dispatches = [
        dispatch for dispatch in record.dispatches.values() if dispatch.role_kind == "reviewer"
    ]
    assert reviewer_dispatches
    assert all(
        handle.store.load_dispatch_payload(handle.run_id, dispatch.dispatch_id).policy[
            "permission"
        ]["bash"]
        == read_only_diagnostic_bash_rules()
        for dispatch in reviewer_dispatches
    )
    _assert_clean_repository(project.repository)


def test_batch_reconciliation_full_loop_with_fake_runner(tmp_path: Path) -> None:
    project = _configure_fake_runner_project(
        _non_live_project(tmp_path, "batch"),
        scheduling="bounded_parallel",
        models=_fake_models(),
    )
    _seed_deterministic_fixture(project.repository, tests=("first",))
    _commit_initial(project.repository)
    sibling = project.root / "sibling"
    sibling.mkdir()
    _initialize_repository(sibling, "https://example.invalid/sibling.git")
    _seed_deterministic_fixture(sibling, tests=("second",))
    _commit_initial(sibling)
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
    plan = _plan(project, steps=2, review=False, second_repo="sibling-repo")
    injector = OneShotResidueInjector(_fake_session_runner(), target_workdir=sibling)

    handle = _run_real_scenario(
        project,
        plan,
        steps=("prepare-fixture", "prepare-second"),
        original_prompts={
            "prepare-fixture": _executor_task_prompt("prepare-fixture"),
            "prepare-second": _executor_task_prompt("prepare-second"),
        },
        reviewer_role=None,
        batch=True,
        session_runner=injector,
    )

    assert injector.state.injections == 1
    assert handle.completion is not None and handle.completion.accepted is False
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.WAITING_OPERATOR
    request = record.operator_request
    assert request is not None and request.kind == "batch_reconciliation"
    assert record.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert record.steps["prepare-second"].state is StepStatus.BLOCKED
    first_batch = record.batches[request.context_ref]
    assert first_batch.state is BatchStatus.FAILED
    failed_dispatch = record.dispatches[first_batch.failed_dispatch_ids[0]]
    assert failed_dispatch.failure_category == "repository_validation"
    assert _RESIDUE_NAME in (failed_dispatch.failure_detail or "")
    successful_dispatch = next(
        record.dispatches[dispatch_id]
        for dispatch_id in first_batch.dispatch_ids
        if dispatch_id not in first_batch.failed_dispatch_ids
    )
    assert successful_dispatch.step_id == "prepare-fixture"
    assert successful_dispatch.state is DispatchStatus.FORWARDED
    primary_head_after_first_batch = _git(project.repository, "rev-parse", "HEAD")
    _assert_clean_repository(project.repository)

    _reconcile_disposable_repository(
        sibling,
        handle.initial_revisions["sibling-repo"],
        residue_name=_RESIDUE_NAME,
    )
    assert _answer_via_cli(handle, "reconcile") == 0
    _assert_operator_decision_count(handle.store, 1)
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.RUNNING
    assert record.steps["prepare-second"].state is StepStatus.READY

    resume_prompt_index = len(handle.supervisor_prompts)
    _run_bounded_orchestration(handle)
    assert handle.worker_error is None
    assert handle.completion is not None and handle.completion.accepted is True
    resume_envelope = json.loads(handle.supervisor_prompts[resume_prompt_index])
    assert resume_envelope["kind"] == "orchestration_resume"
    assert isinstance(resume_envelope["bootstrap"], str) and resume_envelope["bootstrap"]
    assert resume_envelope["pending_forwardings"] == [
        {
            "dispatch_id": successful_dispatch.dispatch_id,
            "payload": {
                **json.loads(
                    handle.store.load_dispatch_payload(
                        handle.run_id,
                        successful_dispatch.dispatch_id,
                    ).forwarding_payload
                )
            },
        }
    ]
    record, _generation = handle.store.load_run(handle.run_id)
    assert record.state is RunStatus.SUCCEEDED
    assert record.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert record.steps["prepare-second"].state is StepStatus.ACCEPTED
    assert record.steps["prepare-fixture"].executor_attempts == 1
    assert record.steps["prepare-second"].executor_attempts == 2
    assert record.dispatches[successful_dispatch.dispatch_id].state is DispatchStatus.ACKNOWLEDGED
    assert len(record.batches) == 2
    batches = sorted(record.batches.values(), key=lambda batch: batch.last_event.sequence)
    assert batches[0].state is BatchStatus.FAILED
    assert batches[1].state is BatchStatus.JOINED
    assert _git(project.repository, "rev-parse", "HEAD") == primary_head_after_first_batch
    _assert_clean_repository(project.repository)
    _assert_clean_repository(sibling)
    _assert_fixed_test_passes(project.repository, "test_real_output.py")
    _assert_fixed_test_passes(sibling, "test_real_second_output.py")
    assert not any(
        obligation.code == "dispatch_in_flight"
        for obligation in completion_obligations(record)
    )
    _assert_no_active_leases(handle.store)
    _assert_operator_decision_count(handle.store, 1)
