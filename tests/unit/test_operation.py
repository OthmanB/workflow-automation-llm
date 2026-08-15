from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from helpers import config_values, create_fixture_project, valid_plan_values, write_config

from dispatcher.operation import (
    LiveSmokeProof,
    RealOperationApproval,
    RealOperationError,
    approve_real_operation,
    compile_role_permission_manifest,
    digest_json,
    parse_permission_digest_args,
    validate_real_operation_prerequisites,
)
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.repository import RepositorySnapshot
from dispatcher.state_store import StateStore
from dispatcher.workflow import TransitionEvent, new_run_record


def _record(project, plan: NormalizedPlan):
    return new_run_record(
        run_id="real-operation-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-real-operation-plan"),
        event=TransitionEvent(
            event_id="event-real-operation-run",
            sequence=1,
            actor="operator",
            reason="real operation fixture",
            correlation_id="real-operation-run",
            occurred_at=datetime.now(UTC),
        ),
    )


def _write_approval(
    path: Path,
    *,
    config,
    record,
    plan: NormalizedPlan,
    repo_id: str = "fixture-repo",
) -> Path:
    permission_digests = _permission_digests(config, record, plan, repo_id)
    approval = approve_real_operation(
        config=config,
        record=record,
        plan=plan,
        repo_id=repo_id,
        approval_ref="decision-real-operation",
        permission_digests=permission_digests,
    )
    path.write_text(approval.model_dump_json(), encoding="utf-8")
    return path


def _permission_digests(config, record, plan: NormalizedPlan, repo_id: str = "fixture-repo"):
    manifest = compile_role_permission_manifest(
        config=config,
        plan=plan,
        record=record,
        repo_id=repo_id,
    )
    return {role_key: entry.digest for role_key, entry in manifest.roles.items()}


def test_permission_manifest_binds_mcp_tools_and_rejects_drift(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["mcp"] = {
        "environment_passthrough": [],
        "servers": {
            "fixture": {
                "type": "local",
                "enabled": True,
                "command": ["/usr/bin/fixture-mcp"],
                "environment": {},
            }
        },
    }
    for pool in values["roles"].values():
        for role_key in pool:
            pool[role_key]["mcp_tools"] = ["fixture_echo"]
    config = write_config(project, values)
    object.__setattr__(project, "config", config)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = _record(project, plan)
    manifest = compile_role_permission_manifest(
        config=config,
        plan=plan,
        record=record,
        repo_id="fixture-repo",
    )
    assert manifest.roles["terra"].mcp_tools == ("fixture_echo",)
    base = manifest.roles["terra"].digest

    changed_values = config_values(project)
    changed_values["roles"]["executors"]["terra"]["mcp_tools"] = []
    changed_config = write_config(project, changed_values)
    changed_manifest = compile_role_permission_manifest(
        config=changed_config,
        plan=plan,
        record=record,
        repo_id="fixture-repo",
    )
    assert changed_manifest.roles["terra"].digest != base
    assert changed_manifest.roles["terra"].mcp_tools == ()





def test_real_operation_rejects_public_mock_mode_before_other_checks(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = _record(project, plan)
    store = StateStore(project.state, heartbeat_seconds=30, stale_after_seconds=120)
    approval_path = _write_approval(
        tmp_path / "approval.json", config=project.config, record=record, plan=plan
    )

    with pytest.raises(RealOperationError, match="real_operation"):
        validate_real_operation_prerequisites(
            config=project.config,
            store=store,
            record=record,
            plan_path=project.plans / "plan.md",
            repo_id="fixture-repo",
            smoke_proof_path=project.root / "smoke.json",
            smoke_model="fixture/executor",
            permission_digests={},
            stall_policy_digest="0" * 64,
            expected_revision="ab" * 20,
            approval_record_path=approval_path,
            confirm=True,
        )


def test_real_operation_requires_explicit_confirmation_and_schema_v2(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["execution"]["mode"] = "real_operation"
    config = write_config(project, values)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = _record(project, plan).model_copy(update={"config_digest": config.config_digest})
    store = StateStore(config.state_dir, heartbeat_seconds=30, stale_after_seconds=120)
    approval_path = _write_approval(
        tmp_path / "approval.json", config=config, record=record, plan=plan
    )

    with pytest.raises(RealOperationError, match="confirm-real-operation"):
        validate_real_operation_prerequisites(
            config=config,
            store=store,
            record=record,
            plan_path=project.plans / "plan.md",
            repo_id="fixture-repo",
            smoke_proof_path=project.root / "smoke.json",
            smoke_model="fixture/executor",
            permission_digests={},
            stall_policy_digest="0" * 64,
            expected_revision="ab" * 20,
            approval_record_path=approval_path,
            confirm=False,
        )


def test_real_operation_gates_on_the_expected_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["execution"]["mode"] = "real_operation"
    config = write_config(project, values)
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(
        yaml.safe_dump(valid_plan_values(project), sort_keys=False),
        encoding="utf-8",
    )
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = _record(project, plan).model_copy(update={"config_digest": config.config_digest})
    store = StateStore(config.state_dir, heartbeat_seconds=30, stale_after_seconds=120)
    store.create_run(record)
    revision = "ab" * 20
    snapshot = RepositorySnapshot(
        repo_id="fixture-repo",
        branch="main",
        revision=revision,
        worktree_id="cd" * 32,
        remote_name="origin",
        remote_url="https://example.invalid/fixture.git",
        clean=True,
        evidence=(),
        external=(),
        changes=(),
        manifest_sha256="ef" * 32,
        ignored=(),
        dirty_patch_sha256="aa" * 32,
        git_metadata_sha256="ab" * 32,
        git_refs_sha256="cd" * 32,
    )
    monkeypatch.setattr(
        "dispatcher.operation.inspect_repository", lambda *args, **kwargs: snapshot
    )
    monkeypatch.setattr("dispatcher.operation.validate_approved_baseline", lambda **kwargs: None)
    approval_path = _write_approval(
        tmp_path / "approval.json", config=config, record=record, plan=plan
    )

    kwargs = dict(
        config=config,
        store=store,
        record=record,
        plan_path=plan_path,
        repo_id="fixture-repo",
        smoke_proof_path=project.root / "smoke.json",
        smoke_model="fixture/executor",
        permission_digests={},
        stall_policy_digest="0" * 64,
        approval_record_path=approval_path,
        confirm=True,
    )
    with pytest.raises(RealOperationError, match="expected revision"):
        validate_real_operation_prerequisites(**kwargs, expected_revision="00" * 20)
    with pytest.raises(RealOperationError, match="live smoke proof"):
        validate_real_operation_prerequisites(**kwargs, expected_revision=revision)


def _real_operation_kwargs_with_smoke_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completed_at: datetime,
    *,
    review_required: bool = False,
) -> dict[str, object]:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["execution"]["mode"] = "real_operation"
    config = write_config(project, values)
    plan_path = tmp_path / "plan.yaml"
    plan_values = valid_plan_values(project)
    if review_required:
        plan_values["steps"][0]["review"] = {
            "required": True,
            "reviewer_role_keys": ["reviewer"],
            "required_acceptances": 1,
        }
        plan_values["steps"][0]["retry"]["max_reviewer_attempts"] = 1
    plan_path.write_text(yaml.safe_dump(plan_values, sort_keys=False), encoding="utf-8")
    plan = NormalizedPlan.model_validate(plan_values)
    record = _record(project, plan).model_copy(update={"config_digest": config.config_digest})
    store = StateStore(config.state_dir, heartbeat_seconds=30, stale_after_seconds=120)
    store.create_run(record)
    revision = "ab" * 20
    snapshot = RepositorySnapshot(
        repo_id="fixture-repo",
        branch="main",
        revision=revision,
        worktree_id="cd" * 32,
        remote_name="origin",
        remote_url="https://example.invalid/fixture.git",
        clean=True,
        evidence=(),
        external=(),
        changes=(),
        manifest_sha256="ef" * 32,
        ignored=(),
        dirty_patch_sha256="aa" * 32,
        git_metadata_sha256="ab" * 32,
        git_refs_sha256="cd" * 32,
    )
    monkeypatch.setattr("dispatcher.operation.inspect_repository", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr("dispatcher.operation.validate_approved_baseline", lambda **kwargs: None)
    smoke_path = project.root / "smoke.json"
    proof = LiveSmokeProof(
        proof_version=1,
        config_digest=config.config_digest,
        model="fixture/executor",
        opencode_version="1.18.18",
        passed=True,
        session_id_present=True,
        workdir_clean=True,
        evidence_written=[],
        response="LIVE_SMOKE_OK",
        completed_at=completed_at,
    )
    smoke_path.write_text(proof.model_dump_json(), encoding="utf-8")
    approval_path = _write_approval(
        tmp_path / "approval.json", config=config, record=record, plan=plan
    )
    return {
        "config": config,
        "store": store,
        "record": record,
        "plan_path": plan_path,
        "repo_id": "fixture-repo",
        "smoke_proof_path": smoke_path,
        "smoke_model": "fixture/executor",
        "permission_digests": _permission_digests(config, record, plan),
        "stall_policy_digest": "0" * 64,
        "expected_revision": revision,
        "approval_record_path": approval_path,
        "confirm": True,
    }


def test_real_operation_rejects_stale_live_smoke_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(
        tmp_path,
        monkeypatch,
        datetime.now(UTC) - timedelta(hours=2),
    )

    with pytest.raises(RealOperationError, match="stale"):
        validate_real_operation_prerequisites(**kwargs)


def test_real_operation_accepts_recent_live_smoke_proof_for_freshness_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(
        tmp_path,
        monkeypatch,
        datetime.now(UTC),
    )

    with pytest.raises(RealOperationError, match="stall-policy digest"):
        validate_real_operation_prerequisites(**kwargs)


def test_real_operation_accepts_matching_approval_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(tmp_path, monkeypatch, datetime.now(UTC))

    with pytest.raises(RealOperationError, match="stall-policy digest"):
        validate_real_operation_prerequisites(**kwargs)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("project_id", "other-project", "current project"),
        ("config_digest", "0" * 64, "current config"),
        ("plan_digest", "0" * 64, "current plan"),
        ("run_id", "other-run", "current run"),
        ("repo_id", "other-repo", "current repository"),
        ("step_id", "other-step", "current step"),
    ],
)
def test_real_operation_rejects_mismatched_approval_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    message: str,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(tmp_path, monkeypatch, datetime.now(UTC))
    approval_path = Path(kwargs["approval_record_path"])
    approval = RealOperationApproval.model_validate_json(approval_path.read_text(encoding="utf-8"))
    approval_path.write_text(
        approval.model_copy(update={field: value}).model_dump_json(), encoding="utf-8"
    )

    with pytest.raises(RealOperationError, match=message):
        validate_real_operation_prerequisites(**kwargs)


def test_real_operation_rejects_naive_live_smoke_proof_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(
        tmp_path,
        monkeypatch,
        datetime.now(),
    )

    with pytest.raises(RealOperationError, match="timezone-aware"):
        validate_real_operation_prerequisites(**kwargs)


def test_permission_digest_arguments_reject_malformed_and_duplicate_roles() -> None:
    with pytest.raises(RealOperationError, match="ROLE=SHA256"):
        parse_permission_digest_args(["not-a-pair"])
    with pytest.raises(RealOperationError, match="not a SHA-256"):
        parse_permission_digest_args(["terra=bad"])
    with pytest.raises(RealOperationError, match="duplicate.*terra"):
        parse_permission_digest_args([f"terra={'0' * 64}", f"terra={'1' * 64}"])


def test_permission_manifest_covers_supervisor_all_executors_and_required_reviewers(
    tmp_path: Path,
) -> None:
    project = create_fixture_project(tmp_path)
    plan_values = valid_plan_values(project)
    plan_values["steps"][0]["review"] = {
        "required": True,
        "reviewer_role_keys": ["reviewer"],
        "required_acceptances": 1,
    }
    plan_values["steps"][0]["retry"]["max_reviewer_attempts"] = 1
    plan = NormalizedPlan.model_validate(plan_values)
    record = _record(project, plan)

    manifest = compile_role_permission_manifest(
        config=project.config,
        plan=plan,
        record=record,
        repo_id="fixture-repo",
    )

    assert set(manifest.roles) == {"supervisor", "terra", "reviewer"}
    assert manifest.roles["supervisor"].role_kind == "supervisor"
    assert manifest.roles["supervisor"].authorized_actions == ("inspect",)
    assert manifest.roles["terra"].role_kind == "executor"
    assert manifest.roles["terra"].authorized_actions == ("inspect",)
    assert manifest.roles["reviewer"].role_kind == "reviewer"
    assert manifest.roles["reviewer"].authorized_actions == ("inspect",)
    assert manifest.manifest_version == 2
    assert manifest.structured_git.commit_policy == "required"
    assert not manifest.structured_git.commit_authorized
    assert len(manifest.structured_git.digest) == 64


def test_structured_git_capability_digest_binds_paths_and_identity(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    base_plan = NormalizedPlan.model_validate(valid_plan_values(project))
    base = compile_role_permission_manifest(
        config=project.config,
        plan=base_plan,
        record=_record(project, base_plan),
        repo_id="fixture-repo",
    )

    values = valid_plan_values(project)
    values["steps"][0]["authorization"] = {
        "authorized_actions": ["inspect", "modify", "commit"],
        "writable_paths": ["evidence/", "result.txt"],
        "requires_operator_approval": False,
    }
    config_values_data = config_values(project)
    config_values_data["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    config_values_data["execution"]["structured_git"]["committer_name"] = "Other Committer"
    config = write_config(project, config_values_data)
    changed_plan = NormalizedPlan.model_validate(values)
    changed = compile_role_permission_manifest(
        config=config,
        plan=changed_plan,
        record=_record(project, changed_plan).model_copy(update={"config_digest": config.config_digest}),
        repo_id="fixture-repo",
    )

    assert changed.structured_git.commit_authorized
    assert changed.structured_git.writable_paths == ("evidence/", "result.txt")
    assert changed.structured_git.digest != base.structured_git.digest
    assert changed.structured_git.identity_digest != base.structured_git.identity_digest


def test_permission_manifest_includes_every_configured_executor(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["roles"]["executors"]["luna"] = {
        **values["roles"]["executors"]["terra"],
        "model": "fixture/luna",
        "display": "Luna",
    }
    values["execution"]["concurrency"]["role_capacities"]["luna"] = 1
    config = write_config(project, values)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = _record(project, plan).model_copy(update={"config_digest": config.config_digest})

    manifest = compile_role_permission_manifest(
        config=config,
        plan=plan,
        record=record,
        repo_id="fixture-repo",
    )

    assert set(manifest.roles) == {"supervisor", "terra", "luna"}
    assert manifest.roles["luna"].role_kind == "executor"


def test_permission_manifest_omits_reviewers_when_review_is_not_required(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = _record(project, plan)

    manifest = compile_role_permission_manifest(
        config=project.config,
        plan=plan,
        record=record,
        repo_id="fixture-repo",
    )

    assert set(manifest.roles) == {"supervisor", "terra"}


def test_approval_rejects_mismatched_reviewer_permission_digest(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    plan_values = valid_plan_values(project)
    plan_values["steps"][0]["review"] = {
        "required": True,
        "reviewer_role_keys": ["reviewer"],
        "required_acceptances": 1,
    }
    plan_values["steps"][0]["retry"]["max_reviewer_attempts"] = 1
    plan = NormalizedPlan.model_validate(plan_values)
    record = _record(project, plan)
    digests = _permission_digests(project.config, record, plan)
    digests["reviewer"] = "0" * 64

    with pytest.raises(RealOperationError, match="reviewer"):
        approve_real_operation(
            config=project.config,
            record=record,
            plan=plan,
            repo_id="fixture-repo",
            approval_ref="decision-real-operation",
            permission_digests=digests,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda digests: digests.pop("supervisor"),
        lambda digests: digests.update({"unexpected": "0" * 64}),
    ],
)
def test_approval_rejects_missing_or_extra_permission_roles(tmp_path: Path, mutate) -> None:
    project = create_fixture_project(tmp_path)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = _record(project, plan)
    digests = _permission_digests(project.config, record, plan)
    mutate(digests)

    with pytest.raises(RealOperationError, match="role set does not match"):
        approve_real_operation(
            config=project.config,
            record=record,
            plan=plan,
            repo_id="fixture-repo",
            approval_ref="decision-real-operation",
            permission_digests=digests,
        )


def test_execute_gate_rejects_supplied_reviewer_permission_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(
        tmp_path,
        monkeypatch,
        datetime.now(UTC),
        review_required=True,
    )
    digests = dict(kwargs["permission_digests"])
    digests["reviewer"] = "0" * 64
    kwargs["permission_digests"] = digests

    with pytest.raises(RealOperationError, match="reviewer"):
        validate_real_operation_prerequisites(**kwargs)


def test_execute_gate_rejects_tampered_reviewer_permission_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(
        tmp_path,
        monkeypatch,
        datetime.now(UTC),
        review_required=True,
    )
    approval_path = Path(kwargs["approval_record_path"])
    approval = RealOperationApproval.model_validate_json(approval_path.read_text(encoding="utf-8"))
    reviewer = approval.permission_manifest.roles["reviewer"]
    tampered_roles = {
        **approval.permission_manifest.roles,
        "reviewer": reviewer.model_copy(update={"digest": "0" * 64}),
    }
    tampered_manifest = approval.permission_manifest.model_copy(update={"roles": tampered_roles})
    approval_path.write_text(
        approval.model_copy(update={"permission_manifest": tampered_manifest}).model_dump_json(),
        encoding="utf-8",
    )

    with pytest.raises(RealOperationError, match="role permission manifest"):
        validate_real_operation_prerequisites(**kwargs)


def test_execute_gate_accepts_supported_macos_verification_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(
        tmp_path,
        monkeypatch,
        datetime.now(UTC),
    )
    config = kwargs["config"]
    kwargs["stall_policy_digest"] = digest_json(
        config.execution.stall_policy.model_dump(mode="json")
    )

    result = validate_real_operation_prerequisites(**kwargs)

    assert result["verification_backend"] == "darwin-seatbelt-v1"
