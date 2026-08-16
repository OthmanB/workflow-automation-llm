from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from helpers import (
    FixtureProject,
    config_values,
    create_fixture_project,
    valid_plan_values,
    write_config,
)

from dispatcher import sessions
from dispatcher.baseline import BaselineDecision, approve_baseline, inspect_baseline
from dispatcher.cli import main
from dispatcher.operation import (
    LiveSmokeProof,
    approve_real_operation,
    compile_real_operation_scope_manifest,
    digest_json,
)
from dispatcher.permissions import read_only_diagnostic_bash_rules
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.state import open_state_store
from dispatcher.state_store import StateStore
from dispatcher.workflow import (
    RunRecord,
    RunStatus,
    TransitionEvent,
    new_run_record,
    transition_run,
)


@dataclass(frozen=True)
class ExecuteFixture:
    project: FixtureProject
    store: StateStore
    record: RunRecord
    plan: NormalizedPlan
    plan_path: Path
    smoke_proof_path: Path
    approval_path: Path
    expected_revision: str
    permission_digests: dict[str, str]
    stall_policy_digest: str


class DirectProductionTestBackend:
    name = "linux-bwrap-v1"
    production_ready = True

    def command(self, argv: tuple[str, ...], _workspace: Path, _home: Path) -> list[str]:
        return list(argv)


def test_execute_command_completes_disposable_fake_opencode_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _prepare_execute_fixture(tmp_path)
    fake_opencode = _install_fake_opencode(tmp_path)
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(fake_opencode))
    monkeypatch.setattr("dispatcher.cli.refresh_opencode_credentials", lambda _state_dir: None)
    backend = DirectProductionTestBackend()
    monkeypatch.setattr("dispatcher.operation.verification_backend", lambda _config: backend)
    monkeypatch.setattr("dispatcher.verification.verification_backend", lambda _config: backend)

    assert main(_execute_argv(fixture)) == 0
    assert "execute: completed accepted=True" in capsys.readouterr().out
    calls = [
        json.loads(line)
        for line in (fake_opencode.parent / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(calls) == 9
    reviewer_calls = [call for call in calls if call["role"] == "reviewer"]
    assert reviewer_calls
    assert all(
        call["policy"]["permission"]["edit"] == "deny"
        and call["policy"]["permission"]["write"] == "deny"
        and call["policy"]["permission"]["bash"] == read_only_diagnostic_bash_rules()
        for call in reviewer_calls
    )

    final_record, _final_generation = fixture.store.load_run(fixture.record.run_id)
    assert final_record.state is RunStatus.SUCCEEDED
    for dispatch in final_record.dispatches.values():
        if dispatch.state.value == "ACKNOWLEDGED":
            payload = fixture.store.load_dispatch_payload(
                fixture.record.run_id,
                dispatch.dispatch_id,
            )
            assert payload.authoritative_verification is not None
            assert all(item["status"] == "passed" for item in payload.authoritative_verification)
    assert _git(fixture.project.repository, "status", "--porcelain") == ""
    assert _git(fixture.project.repository, "rev-list", "--count", "HEAD") == "3"
    assert (fixture.project.repository / "src" / "value.txt").read_text(encoding="utf-8") == "value=2\n"
    assert (fixture.project.repository / "evidence" / "fixture.md").read_text(
        encoding="utf-8"
    ) == "fixture evidence attempt 2\n"

    with sqlite3.connect(fixture.store.database_path) as connection:
        audit_kinds = [
            row[0]
            for row in connection.execute(
                "SELECT kind FROM audit_events WHERE run_id = ? ORDER BY created_at, event_id",
                (fixture.record.run_id,),
            )
        ]
    assert "real_operation_approved" in audit_kinds


def test_execute_command_requires_approval_record_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "execute",
                "--config",
                "project.yaml",
                "--run-id",
                "execute-run",
                "--plan",
                "plan.yaml",
                "--repo-id",
                "fixture-repo",
                "--smoke-proof",
                "smoke.json",
                "--smoke-model",
                "fixture/executor",
                "--permission-digest",
                f"supervisor={'0' * 64}",
                "--stall-policy-digest",
                "0" * 64,
                "--expected-revision",
                "0" * 40,
                "--confirm-real-operation",
            ]
        )

    assert error.value.code == 2
    assert "the following arguments are required: --approval-record" in capsys.readouterr().err


def test_execute_command_rejects_wrong_revision_without_launching_fake_opencode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _prepare_execute_fixture(tmp_path)
    fake_opencode = _install_fake_opencode(tmp_path)
    monkeypatch.setattr(sessions, "OPENCODE_BIN", str(fake_opencode))
    monkeypatch.setattr("dispatcher.cli.refresh_opencode_credentials", lambda _state_dir: None)
    before_revision = _git(fixture.project.repository, "rev-parse", "HEAD")
    before_status = _git(fixture.project.repository, "status", "--porcelain")

    assert main(_execute_argv(fixture, expected_revision="0" * 40)) == 2

    assert "execute: FAILED - repository is not at the expected revision" in capsys.readouterr().err
    assert _git(fixture.project.repository, "rev-parse", "HEAD") == before_revision
    assert _git(fixture.project.repository, "status", "--porcelain") == before_status == ""
    assert not (fake_opencode.parent / "calls.jsonl").exists()


def test_execute_command_accepts_complete_two_step_scope_before_worker_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _prepare_execute_fixture(tmp_path, two_steps=True)
    monkeypatch.setattr("dispatcher.cli.refresh_opencode_credentials", lambda _state_dir: None)
    backend = DirectProductionTestBackend()
    monkeypatch.setattr("dispatcher.operation.verification_backend", lambda _config: backend)
    monkeypatch.setattr("dispatcher.verification.verification_backend", lambda _config: backend)

    class CompletedWithoutWorker:
        accepted = True
        report_path = tmp_path / "report.json"

    def complete_without_worker(coordinator, *_args, **_kwargs):
        context = coordinator._real_operation_context
        assert context is not None
        assert context.approval.approval_ref == "decision-execute-run"
        assert context.cluster_operation_envelopes == ()
        return CompletedWithoutWorker()

    monkeypatch.setattr(
        "dispatcher.execution.SequentialExecutionCoordinator.run_to_completion",
        complete_without_worker,
    )

    assert main(_execute_argv(fixture)) == 0
    assert "execute: completed accepted=True" in capsys.readouterr().out
    approval = json.loads(fixture.approval_path.read_text(encoding="utf-8"))
    assert [item["step_id"] for item in approval["scope_manifest"]["steps"]] == [
        "prepare-fixture",
        "prepare-second",
    ]


def test_execute_command_resume_reuses_real_operation_approval_audit_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _prepare_execute_fixture(tmp_path)
    monkeypatch.setattr("dispatcher.cli.refresh_opencode_credentials", lambda _state_dir: None)
    backend = DirectProductionTestBackend()
    monkeypatch.setattr("dispatcher.operation.verification_backend", lambda _config: backend)
    monkeypatch.setattr("dispatcher.verification.verification_backend", lambda _config: backend)

    class CompletedWithoutWorker:
        accepted = True
        report_path = tmp_path / "report.json"

    launches: list[tuple[str, int, int]] = []

    def complete_without_worker(
        _coordinator: object,
        run_id: str,
        *,
        expected_generation: int,
        max_turns: int,
    ) -> CompletedWithoutWorker:
        current, generation = fixture.store.load_run(run_id)
        assert generation == expected_generation
        target = RunStatus.READY if current.state is RunStatus.NEW else RunStatus.RUNNING
        progressed = transition_run(
            current,
            target,
            TransitionEvent(
                event_id=f"event-resume-progress-{generation}",
                sequence=current.sequence + 1,
                actor="dispatcher",
                reason="disposable execute resume progression",
                correlation_id=run_id,
                occurred_at=datetime.now(UTC),
            ),
        )
        fixture.store.save_run(progressed, expected_generation=generation)
        launches.append((run_id, expected_generation, max_turns))
        return CompletedWithoutWorker()

    monkeypatch.setattr(
        "dispatcher.execution.SequentialExecutionCoordinator.run_to_completion",
        complete_without_worker,
    )

    assert main(_execute_argv(fixture)) == 0
    assert main(_execute_argv(fixture)) == 0

    assert capsys.readouterr().out.count("execute: completed accepted=True") == 2
    assert [(run_id, generation) for run_id, generation, _max_turns in launches] == [
        (fixture.record.run_id, 1),
        (fixture.record.run_id, 2),
    ]
    assert launches[0][2] == launches[1][2]
    resumed_record, _resumed_generation = fixture.store.load_run(fixture.record.run_id)
    assert resumed_record.sequence == fixture.record.sequence + 2
    with sqlite3.connect(fixture.store.database_path) as connection:
        approval_events = connection.execute(
            """
            SELECT event_id, sequence, kind, correlation_id, causation_id
            FROM audit_events
            WHERE run_id = ? AND kind = 'real_operation_approved'
            """,
            (fixture.record.run_id,),
        ).fetchall()
    assert approval_events == [
        (
            "audit-real-operation-decision-execute-run",
            fixture.record.sequence + 1,
            "real_operation_approved",
            fixture.record.run_id,
            None,
        )
    ]


def _prepare_execute_fixture(tmp_path: Path, *, two_steps: bool = False) -> ExecuteFixture:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["execution"]["mode"] = "real_operation"
    values["execution"]["verification_backend"] = "linux_bwrap_v1"
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
    _commit_initial_fixture(project.repository)

    plan_values = valid_plan_values(project)
    step = plan_values["steps"][0]
    step["authorization"] = {
        "authorized_actions": ["inspect", "modify", "verify", "commit"],
        "writable_paths": ["evidence/", "src/value.txt"],
        "requires_operator_approval": False,
    }
    step["review"] = {
        "required": True,
        "reviewer_role_keys": ["reviewer"],
        "required_acceptances": 1,
    }
    step["retry"] = {
        "max_executor_attempts": 2,
        "max_reviewer_attempts": 2,
        "on_failed": "halt",
        "on_blocked": "halt",
        "on_changes_requested": "retry",
        "escalation_role_key": None,
    }
    if two_steps:
        second = json.loads(json.dumps(step))
        second.update(
            {
                "ordinal": 2,
                "step_id": "prepare-second",
                "title": "Prepare second fixture",
                "depends_on": ["prepare-fixture"],
                "required_inputs": [
                    {
                        "artifact_id": "fixture-output",
                        "producer_step_id": "prepare-fixture",
                        "description": "Prepared fixture output",
                    }
                ],
                "produced_outputs": [
                    {
                        "artifact_id": "second-output",
                        "producer_step_id": None,
                        "description": "Second fixture output",
                    }
                ],
                "resource_locks": [{"resource_id": "second-resource", "mode": "write"}],
                "authorization": {
                    "authorized_actions": ["inspect", "modify", "verify", "commit"],
                    "writable_paths": ["evidence/second.md", "src/second.txt"],
                    "requires_operator_approval": False,
                },
                "evidence_requirements": [
                    {
                        "artifact_id": "second-evidence",
                        "relative_path": "second.md",
                        "media_type": "text/markdown",
                    }
                ],
            }
        )
        plan_values["steps"].append(second)
    plan = NormalizedPlan.model_validate(plan_values)
    plan_path = project.plans / "execute-plan.yaml"
    plan_path.write_text(yaml.safe_dump(plan_values, sort_keys=False), encoding="utf-8")

    store = open_state_store(project.config)
    observation = inspect_baseline(plan, project.config)
    approve_baseline(
        observation,
        decisions=tuple(
            BaselineDecision(
                step_id=step.step_id,
                state="PENDING",
                reason="execute command disposable fixture starts as pending",
                operator_decision_ref="decision-execute-baseline",
            )
            for step in plan.steps
        ),
        plan=plan,
        config=project.config,
        store=store,
        approval_decision_ref="decision-execute-baseline",
    )
    record = new_run_record(
        run_id="execute-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-execute-plan"),
        event=TransitionEvent(
            event_id="event-execute-run",
            sequence=1,
            actor="operator",
            reason="execute command disposable fixture",
            correlation_id="execute-run",
            occurred_at=datetime.now(UTC),
        ),
    )
    store.create_run(record)

    smoke_proof_path = project.root / "smoke-proof.json"
    smoke_proof_path.write_text(
        LiveSmokeProof(
            proof_version=1,
            config_digest=project.config.config_digest,
            model="fixture/executor",
            opencode_version="1.18.18",
            passed=True,
            session_id_present=True,
            workdir_clean=True,
            evidence_written=[],
            response="LIVE_SMOKE_OK",
            completed_at=datetime.now(UTC),
        ).model_dump_json(),
        encoding="utf-8",
    )
    approval_path = project.root / "approval.json"
    scope_manifest = compile_real_operation_scope_manifest(
        config=project.config,
        record=record,
        plan=plan,
        repo_id="fixture-repo",
    )
    permission_digests = {
        role_key: entry.digest for role_key, entry in scope_manifest.steps[0].roles.items()
    }
    approval_path.write_text(
        approve_real_operation(
            config=project.config,
            record=record,
            plan=plan,
            repo_id="fixture-repo",
            approval_ref="decision-execute-run",
            permission_digests=permission_digests,
            scope_manifest_digest=scope_manifest.digest if len(scope_manifest.steps) > 1 else None,
        ).model_dump_json(),
        encoding="utf-8",
    )
    return ExecuteFixture(
        project=project,
        store=store,
        record=record,
        plan=plan,
        plan_path=plan_path,
        smoke_proof_path=smoke_proof_path,
        approval_path=approval_path,
        expected_revision=_git(project.repository, "rev-parse", "HEAD"),
        permission_digests=permission_digests,
        stall_policy_digest=digest_json(project.config.execution.stall_policy.model_dump(mode="json")),
    )


def _execute_argv(fixture: ExecuteFixture, *, expected_revision: str | None = None) -> list[str]:
    return [
        "execute",
        "--config",
        str(fixture.project.config_path),
        "--run-id",
        fixture.record.run_id,
        "--plan",
        str(fixture.plan_path),
        "--repo-id",
        "fixture-repo",
        "--smoke-proof",
        str(fixture.smoke_proof_path),
        "--smoke-model",
        "fixture/executor",
        *[
            item
            for role_key, digest in fixture.permission_digests.items()
            for item in ("--permission-digest", f"{role_key}={digest}")
        ],
        "--stall-policy-digest",
        fixture.stall_policy_digest,
        "--expected-revision",
        expected_revision or fixture.expected_revision,
        "--approval-record",
        str(fixture.approval_path),
        "--confirm-real-operation",
    ]


def _install_fake_opencode(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1] / "fixtures" / "opencode" / "fake_cli.py"
    target_dir = tmp_path / "fake-opencode"
    target_dir.mkdir()
    target = target_dir / "opencode"
    shutil.copy2(source, target)
    target.chmod(0o700)
    return target


def _commit_initial_fixture(repository: Path) -> None:
    value_path = repository / "src" / "value.txt"
    evidence_path = repository / "evidence" / "fixture.md"
    value_path.parent.mkdir(parents=True, exist_ok=True)
    value_path.write_text("value=0\n", encoding="utf-8")
    evidence_path.write_text("initial fixture evidence\n", encoding="utf-8")
    _git(repository, "config", "user.name", "Fixture Initializer")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "branch", "-M", "main")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial fixture")


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
