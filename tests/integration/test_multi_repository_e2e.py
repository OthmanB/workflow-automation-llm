from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from helpers import config_values, create_fixture_project, valid_plan_values, write_config

from dispatcher import sessions
from dispatcher.execution import SequentialExecutionCoordinator
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.sequential import CompletionDecision, PreparedDispatch, SequentialWorkflow
from dispatcher.state import open_state_store
from dispatcher.workflow import RunStatus, TransitionEvent, new_run_record


def test_two_repository_plan_routes_each_worker_to_its_normalized_repository(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = create_fixture_project(tmp_path)
    sibling = project.root / "sibling-repository"
    _commit_initial_fixture(project.repository)
    _initialize_sibling_fixture(sibling)
    config = _configure_two_repositories(project, sibling)
    plan_values = valid_plan_values(project)
    first = plan_values["steps"][0]
    first["authorization"] = {
        "authorized_actions": ["inspect", "modify", "verify", "commit"],
        "writable_paths": ["evidence/", "src/value.txt"],
        "requires_operator_approval": False,
    }
    second = json.loads(json.dumps(first))
    second.update(
        {
            "ordinal": 2,
            "step_id": "prepare-sibling",
            "title": "Prepare sibling fixture",
            "repo_id": "sibling-repo",
            "depends_on": ["prepare-fixture"],
            "produced_outputs": [
                {
                    "artifact_id": "sibling-output",
                    "producer_step_id": None,
                    "description": "Sibling fixture output",
                }
            ],
            "resource_locks": [{"resource_id": "sibling-resource", "mode": "write"}],
        }
    )
    plan_values["steps"].append(second)
    plan = NormalizedPlan.model_validate(plan_values)
    event = TransitionEvent(
        event_id="event-multi-repository-start",
        sequence=1,
        actor="dispatcher",
        reason="two-repository integration fixture created",
        correlation_id="multi-repository-run",
        occurred_at=datetime.now(UTC),
    )
    record = new_run_record(
        run_id="multi-repository-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-multi-repository-plan"),
        event=event,
    )
    store = open_state_store(config)
    generation = store.create_run(record)
    fake_opencode = _install_fake_opencode(tmp_path)
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(fake_opencode))
    workflow = SequentialWorkflow(config, store, owner_id="multi-repository-owner")
    coordinator = SequentialExecutionCoordinator(
        config,
        store,
        workflow,
        owner_id="multi-repository-owner",
    )
    coordinator.acquire_run(record.run_id)
    try:
        active, generation = workflow.activate(record.run_id, expected_generation=generation)
        first_prepared = _prepare(workflow, active.run_id, generation, "prepare-fixture", "fixture-repo")
        first_outcome = coordinator.execute_worker(first_prepared)
        record, generation = workflow.acknowledge_forwarding(
            record.run_id,
            expected_generation=first_outcome.generation,
            dispatch_id=first_outcome.dispatch_id,
        )
        record, generation = workflow.refresh_readiness(record, generation)
        second_prepared = _prepare(
            workflow,
            record.run_id,
            generation,
            "prepare-sibling",
            "sibling-repo",
        )
        second_outcome = coordinator.execute_worker(second_prepared)
        record, generation = workflow.acknowledge_forwarding(
            record.run_id,
            expected_generation=second_outcome.generation,
            dispatch_id=second_outcome.dispatch_id,
        )
        decision = workflow.prepare_from_supervisor(
            record.run_id,
            expected_generation=generation,
            supervisor_text='{"protocol_version":1,"action":"request_completion"}',
        )
    finally:
        coordinator.release_run()

    assert isinstance(decision, CompletionDecision)
    assert decision.accepted
    final, _generation = store.load_run(record.run_id)
    assert final.state is RunStatus.SUCCEEDED
    dispatches = sorted(final.dispatches.values(), key=lambda dispatch: dispatch.step_id)
    assert [dispatch.intent.repository.repo_id for dispatch in dispatches] == [
        "fixture-repo",
        "sibling-repo",
    ]
    assert all(dispatch.intent.repository.base_branch == "main" for dispatch in dispatches)
    assert all(dispatch.intent.repository.working_branch == "main" for dispatch in dispatches)
    assert all(dispatch.intent.repository.worktree_id for dispatch in dispatches)
    assert all(dispatch.intent.repository.remote_url for dispatch in dispatches)
    sibling_payload = store.load_dispatch_payload(record.run_id, second_outcome.dispatch_id)
    assert sibling_payload.repository_before is not None
    assert sibling_payload.repository_after is not None
    assert sibling_payload.repository_before["repo_id"] == "sibling-repo"
    assert sibling_payload.repository_after["repo_id"] == "sibling-repo"
    assert sibling_payload.policy["permission"]["bash"]["git commit *"] == "deny"
    assert decision.report_path is not None
    report = decision.report_path.read_text(encoding="utf-8")
    assert "## Repository Coordinates" in report
    assert "## Inspected Evidence Manifests" in report
    assert sibling_payload.repository_after["manifest_sha256"] in report

    calls = [
        json.loads(line)
        for line in (fake_opencode.parent / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [Path(_option(call["argv"], "--dir")).resolve() for call in calls] == [
        project.repository.resolve(),
        sibling.resolve(),
    ]


def _prepare(
    workflow: SequentialWorkflow,
    run_id: str,
    generation: int,
    step_id: str,
    repo_id: str,
) -> PreparedDispatch:
    prepared = workflow.prepare_from_supervisor(
        run_id,
        expected_generation=generation,
        supervisor_text=json.dumps(
            {
                "protocol_version": 1,
                "action": "dispatch",
                "step_id": step_id,
                "target_role": "terra",
                "session_mode": "new",
                "repo_id": repo_id,
                "prompt": "Perform the approved fixture work.",
            }
        ),
    )
    assert isinstance(prepared, PreparedDispatch)
    return prepared


def _configure_two_repositories(project, sibling: Path):
    values = config_values(project)
    values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    values["permission_policies"]["policies"]["executor-class"]["actions"]["commit"] = "allow"
    values["permission_policies"]["policies"]["repository-b"] = {
        "default": "deny",
        "actions": {
            "inspect": "allow",
            "modify": "allow",
            "verify": "allow",
            "commit": "allow",
        },
    }
    values["repositories"]["sibling-repo"] = {
        "root": str(sibling),
        "expected_remote": {"name": "origin", "url": "https://example.invalid/sibling.git"},
        "default_branch": "main",
        "evidence_roots": ["evidence"],
        "writable_roots": ["."],
        "external_roots": [],
        "commit_policy": "required",
        "permission_policy": "repository-b",
        "allow_shared_writable_roots": False,
    }
    return write_config(project, values)


def _initialize_sibling_fixture(repository: Path) -> None:
    (repository / "src").mkdir(parents=True)
    (repository / "evidence").mkdir()
    _git(repository.parent, "init", "--quiet", str(repository))
    _git(repository, "remote", "add", "origin", "https://example.invalid/sibling.git")
    _commit_initial_fixture(repository)


def _commit_initial_fixture(repository: Path) -> None:
    value_path = repository / "src" / "value.txt"
    evidence_path = repository / "evidence" / "fixture.md"
    value_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    value_path.write_text("value=0\n", encoding="utf-8")
    evidence_path.write_text("initial fixture evidence\n", encoding="utf-8")
    _git(repository, "config", "user.name", "Fixture Initializer")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "branch", "-M", "main")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial fixture")


def _install_fake_opencode(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1] / "fixtures" / "opencode" / "fake_cli.py"
    target_dir = tmp_path / "fake-opencode"
    target_dir.mkdir()
    target = target_dir / "opencode"
    shutil.copy2(source, target)
    target.chmod(0o700)
    return target


def _option(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()
