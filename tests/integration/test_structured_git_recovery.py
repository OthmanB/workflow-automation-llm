from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import (
    FixtureProject,
    config_values,
    create_fixture_project,
    valid_plan_values,
    write_config,
)

from dispatcher.cli import main
from dispatcher.config import Config
from dispatcher.git_commit import execute_structured_git_commit, prepare_structured_git_intent
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.repository import authoritative_evidence, inspect_repository
from dispatcher.results import parse_executor_proposal
from dispatcher.sequential import PreparedDispatch, SequentialWorkflow, SequentialWorkflowError
from dispatcher.state_store import StateStore
from dispatcher.verification import AuthoritativeVerification
from dispatcher.workflow import DispatchStatus, TransitionEvent, new_run_record


@dataclass(frozen=True)
class InterruptedCommitFixture:
    project: FixtureProject
    config: Config
    run_id: str
    dispatch_id: str
    result_revision: str


def test_recovery_adopts_one_exact_commit_after_final_state_write_was_interrupted(
    tmp_path: Path,
) -> None:
    fixture = _interrupted_commit_fixture(tmp_path)
    store = StateStore(
        fixture.config.state_dir,
        heartbeat_seconds=fixture.config.lease_heartbeat_seconds,
        stale_after_seconds=fixture.config.lease_stale_after_seconds,
    )
    workflow = SequentialWorkflow(fixture.config, store, owner_id="structured-git-recovery-owner")
    assert store.classify_recovery(fixture.run_id)[0].disposition == (
        "structured_commit_adoption_required"
    )

    recovered, _generation, forwarding = workflow.adopt_interrupted_structured_commit(
        fixture.run_id,
        fixture.dispatch_id,
    )

    dispatch = recovered.dispatches[fixture.dispatch_id]
    structured = store.load_structured_git_record(fixture.run_id, dispatch.dispatch_id)
    payload = store.load_dispatch_payload(fixture.run_id, dispatch.dispatch_id)
    assert dispatch.state is DispatchStatus.FORWARDED
    assert structured.state == "COMMITTED"
    assert structured.result_revision == fixture.result_revision
    assert structured.commit is not None
    assert structured.commit["recovery_kind"] == "exact_head_adoption"
    assert payload.result is not None
    assert payload.result["repository"]["result_revision"] == fixture.result_revision
    assert "recover command fixture completed" in forwarding
    assert _git(fixture.project.repository, "rev-list", "--count", "HEAD") == "2"
    assert _git(fixture.project.repository, "status", "--porcelain") == ""


def test_recover_command_adopts_the_exact_interrupted_commit(
    tmp_path: Path,
    capsys,
) -> None:
    fixture = _interrupted_commit_fixture(tmp_path)

    assert main(
        [
            "recover",
            "--config",
            str(fixture.project.config_path),
            "--run-id",
            fixture.run_id,
        ]
    ) == 0

    output = capsys.readouterr().out
    assert "adopted exact interrupted structured Git commit" in output
    store = StateStore(
        fixture.config.state_dir,
        heartbeat_seconds=fixture.config.lease_heartbeat_seconds,
        stale_after_seconds=fixture.config.lease_stale_after_seconds,
    )
    structured = store.load_structured_git_record(fixture.run_id, fixture.dispatch_id)
    assert structured.state == "COMMITTED"
    assert store.classify_recovery(fixture.run_id)[0].disposition == "acknowledgement_required"


def test_recover_command_marks_a_tampered_commit_for_reconciliation(
    tmp_path: Path,
) -> None:
    fixture = _interrupted_commit_fixture(tmp_path)
    _git(fixture.project.repository, "commit", "--amend", "-m", "tampered recovery commit")

    assert main(
        [
            "recover",
            "--config",
            str(fixture.project.config_path),
            "--run-id",
            fixture.run_id,
        ]
    ) == 1

    store = StateStore(
        fixture.config.state_dir,
        heartbeat_seconds=fixture.config.lease_heartbeat_seconds,
        stale_after_seconds=fixture.config.lease_stale_after_seconds,
    )
    structured = store.load_structured_git_record(fixture.run_id, fixture.dispatch_id)
    assert structured.state == "RECONCILIATION_REQUIRED"
    recovery = store.classify_recovery(fixture.run_id)
    assert recovery[0].disposition == "operator_reconciliation_required"


def test_adoption_rejects_a_mismatched_durable_dirty_snapshot(tmp_path: Path) -> None:
    fixture = _interrupted_commit_fixture(tmp_path)
    store = StateStore(
        fixture.config.state_dir,
        heartbeat_seconds=fixture.config.lease_heartbeat_seconds,
        stale_after_seconds=fixture.config.lease_stale_after_seconds,
    )
    workflow = SequentialWorkflow(fixture.config, store, owner_id="dirty-mismatch-owner")
    _tamper_checked_json(
        store,
        fixture.run_id,
        fixture.dispatch_id,
        mutate=lambda checked: checked["repository_dirty"].update(
            {"manifest_sha256": "0" * 64}
        ),
    )

    with pytest.raises(SequentialWorkflowError, match="dirty snapshot does not match"):
        workflow.adopt_interrupted_structured_commit(fixture.run_id, fixture.dispatch_id)

    structured = store.load_structured_git_record(fixture.run_id, fixture.dispatch_id)
    assert structured.state == "RECONCILIATION_REQUIRED"
    assert store.classify_recovery(fixture.run_id)[0].disposition == (
        "operator_reconciliation_required"
    )


def test_adoption_rejects_durable_dirty_metadata_mutation(tmp_path: Path) -> None:
    fixture = _interrupted_commit_fixture(tmp_path)
    store = StateStore(
        fixture.config.state_dir,
        heartbeat_seconds=fixture.config.lease_heartbeat_seconds,
        stale_after_seconds=fixture.config.lease_stale_after_seconds,
    )
    workflow = SequentialWorkflow(fixture.config, store, owner_id="metadata-mismatch-owner")
    _tamper_checked_json(
        store,
        fixture.run_id,
        fixture.dispatch_id,
        mutate=lambda checked: checked["repository_dirty"].update(
            {"git_metadata_sha256": "1" * 64}
        ),
    )

    with pytest.raises(SequentialWorkflowError, match="records Git metadata mutation"):
        workflow.adopt_interrupted_structured_commit(fixture.run_id, fixture.dispatch_id)

    structured = store.load_structured_git_record(fixture.run_id, fixture.dispatch_id)
    assert structured.state == "RECONCILIATION_REQUIRED"


def _tamper_checked_json(
    store: StateStore,
    run_id: str,
    dispatch_id: str,
    *,
    mutate,
) -> None:
    import sqlite3

    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT checked_json FROM structured_git_commits WHERE run_id = ? AND dispatch_id = ?",
            (run_id, dispatch_id),
        ).fetchone()
        assert row is not None
        checked = json.loads(row[0])
        mutate(checked)
        connection.execute(
            "UPDATE structured_git_commits SET checked_json = ? WHERE run_id = ? AND dispatch_id = ?",
            (json.dumps(checked), run_id, dispatch_id),
        )


def test_recovery_applies_durable_measured_usage_exactly_once(tmp_path: Path) -> None:
    fixture = _interrupted_commit_fixture(tmp_path, budget_enabled=True)
    store = StateStore(
        fixture.config.state_dir,
        heartbeat_seconds=fixture.config.lease_heartbeat_seconds,
        stale_after_seconds=fixture.config.lease_stale_after_seconds,
    )
    workflow = SequentialWorkflow(fixture.config, store, owner_id="budget-recovery-owner")

    recovered, _generation, _forwarding = workflow.adopt_interrupted_structured_commit(
        fixture.run_id,
        fixture.dispatch_id,
    )

    assert recovered.usage.run.cost_usd == 0.25
    assert recovered.usage.run.tokens_total == 10
    assert recovered.usage.by_step["prepare-fixture"].tokens_total == 10
    assert recovered.usage.by_session["session-recover-command"].tokens_total == 10


def _interrupted_commit_fixture(
    tmp_path: Path,
    *,
    budget_enabled: bool = False,
) -> InterruptedCommitFixture:
    project = create_fixture_project(tmp_path)
    result_path = project.repository / "result.txt"
    result_path.write_text("before\n", encoding="utf-8")
    (project.evidence / "fixture.md").write_text("before evidence\n", encoding="utf-8")
    _git(project.repository, "config", "user.name", "Fixture Initializer")
    _git(project.repository, "config", "user.email", "fixture@example.invalid")
    _git(project.repository, "branch", "-M", "main")
    _git(project.repository, "add", ".")
    _git(project.repository, "commit", "-m", "initial fixture")
    values = config_values(project)
    values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    values["permission_policies"]["policies"]["executor-class"]["actions"]["commit"] = "allow"
    values["budget"]["enabled"] = budget_enabled
    config = write_config(project, values)
    plan_values = valid_plan_values(project)
    plan_values["steps"][0]["authorization"] = {
        "authorized_actions": ["inspect", "modify", "verify", "commit"],
        "writable_paths": ["evidence/fixture.md", "result.txt"],
        "requires_operator_approval": False,
    }
    plan = NormalizedPlan.model_validate(plan_values)
    run_id = "recover-command-run"
    record = new_run_record(
        run_id=run_id,
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-recover-command"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(config, store, owner_id="recover-command-owner")
    active, generation = workflow.activate(run_id, expected_generation=generation)
    prepared = workflow.prepare_from_supervisor(
        run_id,
        expected_generation=generation,
        supervisor_text=json.dumps(
            {
                "protocol_version": 1,
                "action": "dispatch",
                "step_id": "prepare-fixture",
                "target_role": "terra",
                "session_mode": "new",
                "prompt": "write the exact fixture files",
            }
        ),
    )
    assert isinstance(prepared, PreparedDispatch)
    running = workflow.mark_running(prepared, process_id=999_999_999, process_create_time=1.0)
    running = workflow.record_session_id(running, runtime_session_id="session-recover-command")
    result_path.write_text("after\n", encoding="utf-8")
    (project.evidence / "fixture.md").write_text("after evidence\n", encoding="utf-8")
    prompt = json.loads(running.prompt)
    proposal = parse_executor_proposal(
        {
            "proposal_version": 2,
            "response_contract": "dispatcher.executor_proposal.v2",
            "dispatch_id": running.dispatch.dispatch_id,
            "attempt": running.dispatch.attempt,
            "step_id": running.dispatch.step_id,
            "repository": {
                "repo_id": running.dispatch.intent.repository.repo_id,
                "base_revision": running.dispatch.intent.repository.base_revision,
            },
            "evidence": [
                {
                    "artifact_id": item["artifact_id"],
                    "relative_path": item["relative_path"],
                    "media_type": item["media_type"],
                }
                for item in prompt["evidence_requirements"]
            ],
            "criterion_self_reports": [
                {
                    "check_id": item["criterion_id"],
                    "status": "not_run",
                    "summary": "dispatcher owns this check",
                }
                for item in prompt["acceptance_criteria"]
            ],
            "summary": "recover command fixture completed",
            "outcome": "completed",
        }
    )
    workflow.record_executor_proposal(running, proposal)
    dirty = inspect_repository(config, "fixture-repo", require_clean=False)
    verification = tuple(
        AuthoritativeVerification(
            check_id=criterion.criterion_id,
            status="passed",
            argv=criterion.check.argv,
            exit_code=0,
            timed_out=False,
            output_truncated=False,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
            transcript_sha256="c" * 64,
            duration_ms=1,
            backend="fixture-recovery",
            summary="dispatcher check passed before the interrupted commit",
        )
        for criterion in plan.steps[0].acceptance_criteria
    )
    artifacts = authoritative_evidence(
        config,
        repo_id="fixture-repo",
        snapshot=dirty,
        requirements=plan.steps[0].evidence_requirements,
        declarations=proposal.evidence,
    )
    intent = prepare_structured_git_intent(
        config,
        step=plan.steps[0],
        attempt=running.dispatch.attempt,
        worktree=project.repository,
        coordinate=running.dispatch.intent.repository,
        before=running.repository_before,
        dirty=dirty,
    )
    store.record_structured_git_checked(
        run_id=run_id,
        dispatch_id=running.dispatch.dispatch_id,
        checked={
            "authoritative_verification": [item.model_dump(mode="json") for item in verification],
            "evidence": [item.model_dump(mode="json") for item in artifacts],
            "repository_before": running.repository_before.model_dump(mode="json"),
            "repository_dirty": dirty.model_dump(mode="json"),
            "usage": (
                {
                    "cost_usd": 0.25,
                    "tokens_total": 10,
                    "tokens_input": 4,
                    "tokens_output": 5,
                    "tokens_reasoning": 1,
                }
                if budget_enabled
                else None
            ),
        },
        intent=intent.model_dump(mode="json"),
    )

    def record_staged(stage) -> None:
        store.record_structured_git_staged(
            run_id=run_id,
            dispatch_id=running.dispatch.dispatch_id,
            stage=stage.model_dump(mode="json"),
        )

    outcome = execute_structured_git_commit(
        config,
        worktree=project.repository,
        intent=intent,
        on_staged=record_staged,
    )
    store.close()
    return InterruptedCommitFixture(
        project=project,
        config=config,
        run_id=run_id,
        dispatch_id=running.dispatch.dispatch_id,
        result_revision=outcome.result_revision,
    )


def _event(sequence: int) -> TransitionEvent:
    return TransitionEvent(
        event_id=f"event-structured-git-recovery-{sequence}",
        sequence=sequence,
        actor="dispatcher",
        reason="structured Git recovery fixture",
        correlation_id="structured-git-recovery-run",
        occurred_at=datetime.now(UTC),
    )


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
