from __future__ import annotations

import copy
import json
import subprocess
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

import yaml
from helpers import create_fixture_project, valid_plan_values

import dispatcher.cli as cli
import dispatcher.cluster_operation_adapters as cluster_operation_adapters
import dispatcher.config as config_mod
import dispatcher.execution as execution_mod
import dispatcher.operation as operation_mod
import dispatcher.preflight as preflight_mod
import dispatcher.state as state_mod
from dispatcher.cli import main
from dispatcher.operation import RealOperationApproval, compile_real_operation_scope_manifest
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.sessions import SessionResult
from dispatcher.workflow import TransitionEvent, new_run_record


def test_real_run_is_blocked_before_config_loading(tmp_path) -> None:
    missing_config = tmp_path / "does-not-exist.yaml"

    result = main(["run", "--config", str(missing_config)])

    assert result == 2


def test_smoke_proof_command_refuses_without_live_environment(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("DISPATCHER_LIVE_OPENCODE", raising=False)
    calls = []

    def runner(**_kwargs):
        calls.append(True)
        raise AssertionError("smoke runner must not be called")

    assert main(
        [
            "smoke-proof",
            "--config",
            str(tmp_path / "missing.yaml"),
            "--model",
            "fixture/model",
            "--output",
            str(tmp_path / "smoke.json"),
        ]
    ) == 2
    result = cli._cmd_smoke_proof(
        Namespace(
            config=str(tmp_path / "missing.yaml"),
            model="fixture/model",
            output=str(tmp_path / "smoke.json"),
        ),
        run_session=runner,
    )

    assert result == 2
    assert calls == []
    assert "set DISPATCHER_LIVE_OPENCODE=1 to run" in capsys.readouterr().err


def test_smoke_proof_command_writes_successful_proof(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = create_fixture_project(tmp_path)
    output = tmp_path / "smoke.json"
    monkeypatch.setenv("DISPATCHER_LIVE_OPENCODE", "1")
    calls = []

    def runner(**kwargs) -> SessionResult:
        calls.append(kwargs)
        workdir = Path(kwargs["workdir"])
        assert list(workdir.iterdir()) == []
        assert Path(kwargs["snapshot_dirs"][0]) == workdir
        assert Path(kwargs["state_dir"]) != workdir
        assert Path(kwargs["credential_state_dir"]) == Path(project.config.state_dir)
        assert kwargs["prompt"] == "Reply with exactly LIVE_SMOKE_OK. Do not use tools or inspect files."
        assert kwargs["permission_config"] == {
            "permission": {"*": "deny", "read": "allow", "glob": "allow", "grep": "allow"}
        }
        return SessionResult(
            session_id="ses-live-smoke",
            exit_code=0,
            chat_response="  LIVE_SMOKE_OK\n",
            evidence_written=[],
            opencode_version="1.18.18",
        )

    monkeypatch.setattr(cli, "refresh_opencode_credentials", lambda state_dir: None)
    result = cli._cmd_smoke_proof(
        Namespace(config=str(project.config_path), model="fixture/model", output=str(output)),
        run_session=runner,
    )

    assert result == 0
    assert len(calls) == 1
    proof = cli.LiveSmokeProof.model_validate_json(output.read_text(encoding="utf-8"))
    assert proof.passed is True
    assert proof.config_digest == project.config.config_digest
    assert proof.model == "fixture/model"
    assert proof.opencode_version == "1.18.18"
    assert proof.session_id_present is True
    assert proof.workdir_clean is True
    assert proof.evidence_written == []
    assert proof.response == "LIVE_SMOKE_OK"
    assert proof.completed_at.tzinfo is not None


def test_smoke_proof_command_rejects_nonmatching_result(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    project = create_fixture_project(tmp_path)
    output = tmp_path / "smoke.json"
    monkeypatch.setenv("DISPATCHER_LIVE_OPENCODE", "1")
    monkeypatch.setattr(cli, "refresh_opencode_credentials", lambda state_dir: None)

    def runner(**_kwargs) -> SessionResult:
        return SessionResult(
            session_id="ses-live-smoke",
            exit_code=0,
            chat_response="NOT_OK",
            evidence_written=[],
            opencode_version="1.18.18",
        )

    result = cli._cmd_smoke_proof(
        Namespace(config=str(project.config_path), model="fixture/model", output=str(output)),
        run_session=runner,
    )

    assert result == 2
    assert output.exists()
    assert cli.LiveSmokeProof.model_validate_json(output.read_text(encoding="utf-8")).passed is False
    assert "did not meet expectations" in capsys.readouterr().err


def test_smoke_proof_command_reports_runner_failure_without_proof(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    project = create_fixture_project(tmp_path)
    output = tmp_path / "smoke.json"
    monkeypatch.setenv("DISPATCHER_LIVE_OPENCODE", "1")
    monkeypatch.setattr(cli, "refresh_opencode_credentials", lambda state_dir: None)

    def runner(**_kwargs) -> SessionResult:
        raise RuntimeError("fake OpenCode failure")

    result = cli._cmd_smoke_proof(
        Namespace(config=str(project.config_path), model="fixture/model", output=str(output)),
        run_session=runner,
    )

    assert result == 2
    assert not output.exists()
    assert "fake OpenCode failure" in capsys.readouterr().err


def test_approve_real_operation_command_writes_exact_bound_record(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    plan_path = tmp_path / "plan.yaml"
    plan_values = valid_plan_values(project)
    second = copy.deepcopy(plan_values["steps"][0])
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
    plan_path.write_text(yaml.safe_dump(plan_values, sort_keys=False), encoding="utf-8")
    plan = NormalizedPlan.model_validate(plan_values)
    record = new_run_record(
        run_id="approval-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-approve-plan"),
        event=TransitionEvent(
            event_id="event-approval-run",
            sequence=1,
            actor="operator",
            reason="approval fixture",
            correlation_id="approval-run",
            occurred_at=datetime.now(UTC),
        ),
    )
    record_path = tmp_path / "approval-run.json"
    output = tmp_path / "approval.json"
    manifest_output = tmp_path / "permission-manifest.json"
    record_path.write_text(record.model_dump_json(), encoding="utf-8")
    scope_manifest = compile_real_operation_scope_manifest(
        config=project.config,
        plan=plan,
        record=record,
        repo_id="fixture-repo",
    )
    digest_args = [
        item
        for role_key, entry in scope_manifest.steps[0].roles.items()
        for item in ("--permission-digest", f"{role_key}={entry.digest}")
    ]

    assert main(["start", "--config", str(project.config_path), "--run-record", str(record_path)]) == 0
    assert (
        main(
            [
                "permission-manifest",
                "--config",
                str(project.config_path),
                "--run-id",
                record.run_id,
                "--plan",
                str(plan_path),
                "--repo-id",
                "fixture-repo",
                "--output",
                str(manifest_output),
            ]
        )
        == 0
    )
    assert json.loads(manifest_output.read_text(encoding="utf-8")) == scope_manifest.model_dump(mode="json")
    assert (
        main(
            [
                "approve-real-operation",
                "--config",
                str(project.config_path),
                "--run-id",
                record.run_id,
                "--plan",
                str(plan_path),
                "--repo-id",
                "fixture-repo",
                "--approval-ref",
                "decision-real-operation",
                *digest_args,
                "--scope-manifest-digest",
                scope_manifest.digest,
                "--output",
                str(output),
            ]
        )
        == 0
    )

    approval = RealOperationApproval.model_validate_json(output.read_text(encoding="utf-8"))
    assert approval.approval_ref == "decision-real-operation"
    assert approval.project_id == project.config.project_id
    assert approval.config_digest == record.config_digest
    assert approval.plan_digest == record.plan_digest
    assert approval.run_id == record.run_id
    assert approval.repo_id == "fixture-repo"
    assert approval.step_id == plan.steps[0].step_id
    assert approval.permission_manifest == scope_manifest.steps[0]
    assert approval.scope_manifest == scope_manifest
    assert approval.decided_at.tzinfo is not None


def test_start_and_resume_use_explicit_sqlite_run_identity(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    event = TransitionEvent(
        event_id="event-start",
        sequence=1,
        actor="dispatcher",
        reason="fixture start",
        correlation_id="fixture-correlation",
        occurred_at=datetime.now(UTC),
    )
    record = new_run_record(
        run_id="fixture-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-approve-plan"),
        event=event,
    )
    record_path = tmp_path / "run-record.json"
    record_path.write_text(record.model_dump_json(), encoding="utf-8")

    assert main(["start", "--config", str(project.config_path), "--run-record", str(record_path)]) == 0
    assert main(["resume", "--config", str(project.config_path), "--run-id", record.run_id]) == 0
    assert main(["start", "--config", str(project.config_path), "--run-record", str(record_path)]) == 2


def test_status_json_and_support_export_are_derived_from_authoritative_state(tmp_path: Path, capsys) -> None:
    project = create_fixture_project(tmp_path)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = new_run_record(
        run_id="status-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-status"),
        event=TransitionEvent(
            event_id="event-status",
            sequence=1,
            actor="dispatcher",
            reason="fixture status",
            correlation_id="status-run",
            occurred_at=datetime.now(UTC),
        ),
    )
    record_path = tmp_path / "status-run.json"
    record_path.write_text(record.model_dump_json(), encoding="utf-8")
    assert main(["start", "--config", str(project.config_path), "--run-record", str(record_path)]) == 0
    capsys.readouterr()

    assert main(["status", "--config", str(project.config_path), "--run-id", record.run_id, "--format", "json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["run"]["run_id"] == record.run_id
    assert status["blocked_steps"][0]["reasons"] == ["state is PENDING"]

    assert main(["support", "--config", str(project.config_path), "--run-id", record.run_id]) == 0
    assert "support: exported" in capsys.readouterr().out
    assert main(["prune", "--config", str(project.config_path)]) == 2


def test_baseline_cli_inspect_and_approve_are_read_only_until_approval(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    (project.evidence / "fixture.md").write_text("fixture evidence\n", encoding="utf-8")
    _commit_baseline_evidence(project.repository)
    plan_path = tmp_path / "plan.yaml"
    observation_path = tmp_path / "baseline-observation.json"
    decisions_path = tmp_path / "baseline-decisions.json"
    plan_path.write_text(yaml.safe_dump(valid_plan_values(project), sort_keys=False), encoding="utf-8")

    assert (
        main(
            [
                "baseline",
                "inspect",
                "--config",
                str(project.config_path),
                "--plan",
                str(plan_path),
                "--output",
                str(observation_path),
            ]
        )
        == 0
    )
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    decisions_path.write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "step_id": step["step_id"],
                        "state": "ACCEPTED",
                        "reason": "fixture evidence is present",
                        "operator_decision_ref": "decision-step-accept",
                    }
                    for step in observation["steps"]
                ]
            }
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "baseline",
                "approve",
                "--config",
                str(project.config_path),
                "--plan",
                str(plan_path),
                "--observation",
                str(observation_path),
                "--decisions",
                str(decisions_path),
                "--approval-decision-ref",
                "decision-approve-baseline",
            ]
        )
        == 0
    )
    baseline_plan = NormalizedPlan.model_validate(valid_plan_values(project))
    baseline_record = new_run_record(
        run_id="baseline-start-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=baseline_plan,
        plan_approval=approve_plan(baseline_plan, "decision-baseline-start"),
        event=TransitionEvent(
            event_id="event-baseline-start",
            sequence=1,
            actor="dispatcher",
            reason="baseline start fixture",
            correlation_id="baseline-start-run",
            occurred_at=datetime.now(UTC),
        ),
    )
    record_path = tmp_path / "baseline-start-run.json"
    record_path.write_text(baseline_record.model_dump_json(), encoding="utf-8")

    assert (
        main(
            [
                "start",
                "--config",
                str(project.config_path),
                "--run-record",
                str(record_path),
                "--use-approved-baseline",
            ]
        )
        == 0
    )


def test_baseline_approve_rejects_duplicate_decision_keys(tmp_path: Path, capsys) -> None:
    project = create_fixture_project(tmp_path)
    (project.evidence / "fixture.md").write_text("fixture evidence\n", encoding="utf-8")
    plan_path = tmp_path / "plan.yaml"
    observation_path = tmp_path / "baseline-observation.json"
    decisions_path = tmp_path / "baseline-decisions.json"
    plan_path.write_text(yaml.safe_dump(valid_plan_values(project), sort_keys=False), encoding="utf-8")

    assert (
        main(
            [
                "baseline",
                "inspect",
                "--config",
                str(project.config_path),
                "--plan",
                str(plan_path),
                "--output",
                str(observation_path),
            ]
        )
        == 0
    )
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    decision = {
        "step_id": observation["steps"][0]["step_id"],
        "state": "WAIVED",
        "reason": "duplicate state key",
        "operator_decision_ref": "decision-step-duplicate",
    }
    raw = json.dumps({"decisions": [decision]})
    raw = raw.replace('"state": "WAIVED"', '"state": "WAIVED", "state": "PENDING"')
    decisions_path.write_text(raw, encoding="utf-8")

    result = main(
        [
            "baseline",
            "approve",
            "--config",
            str(project.config_path),
            "--plan",
            str(plan_path),
            "--observation",
            str(observation_path),
            "--decisions",
            str(decisions_path),
            "--approval-decision-ref",
            "decision-approve-baseline",
        ]
    )

    assert result == 2
    assert "duplicate JSON key in decisions file: state" in capsys.readouterr().err


def test_execute_without_cluster_envelopes_does_not_instantiate_production_adapters(
    tmp_path: Path, monkeypatch
) -> None:
    project = create_fixture_project(tmp_path)
    approval = Namespace(cluster_operation_envelopes=())
    captured_contexts = []

    class ApprovalParser:
        @staticmethod
        def model_validate_json(_value: str) -> Namespace:
            return approval

    class FakeStore:
        def load_run(self, _run_id: str):
            return Namespace(sequence=1), 7

        def append_audit_event_idempotently(self, **_kwargs) -> None:
            return None

    class FakeCoordinator:
        def __init__(self, *_args, **kwargs) -> None:
            captured_contexts.append(kwargs["real_operation_context"])

        def run_to_completion(self, *_args, **_kwargs) -> Namespace:
            return Namespace(accepted=True, report_path="fixture-report.md")

    def fail_adapter_construction(*_args, **_kwargs) -> None:
        raise AssertionError("ordinary execute must not create cluster adapters")

    monkeypatch.setattr(config_mod, "load_config", lambda _path: project.config)
    monkeypatch.setattr(cli, "refresh_opencode_credentials", lambda _state_dir: None)
    monkeypatch.setattr(state_mod, "open_state_store", lambda _config: FakeStore())
    monkeypatch.setattr(
        operation_mod,
        "validate_real_operation_prerequisites",
        lambda **_kwargs: {"approval": {"approval_ref": "fixture-approval"}},
    )
    monkeypatch.setattr(operation_mod, "RealOperationApproval", ApprovalParser)
    monkeypatch.setattr(preflight_mod, "run_preflight", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(execution_mod, "SequentialExecutionCoordinator", FakeCoordinator)
    monkeypatch.setattr(
        cluster_operation_adapters,
        "ProductionPortForwardProcessAdapter",
        fail_adapter_construction,
    )
    monkeypatch.setattr(
        cluster_operation_adapters,
        "ProductionTlsDc8ProbeAdapter",
        fail_adapter_construction,
    )

    result = cli._cmd_execute(
        Namespace(
            config=str(project.config_path),
            log_level=None,
            run_id="fixture-run",
            plan=str(tmp_path / "plan.yaml"),
            repo_id="fixture-repo",
            smoke_proof=str(tmp_path / "smoke.json"),
            smoke_model="fixture/model",
            permission_digest=["terra=" + "a" * 64],
            stall_policy_digest="a" * 64,
            expected_revision=None,
            expected_repository_revision=[],
            approval_record=str(tmp_path / "approval.json"),
            confirm_real_operation=True,
            tier1_invariant_snapshot_digest=None,
            max_turns=1,
        )
    )

    assert result == 0
    assert captured_contexts[0].port_forward_process_adapter is None
    assert captured_contexts[0].tls_dc8_probe_adapter is None


def _commit_baseline_evidence(repository: Path) -> None:
    for args in (
        ("config", "user.name", "Fixture Baseline"),
        ("config", "user.email", "baseline@example.invalid"),
        ("branch", "-M", "main"),
        ("add", "."),
        ("commit", "-m", "accepted baseline evidence"),
    ):
        subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
