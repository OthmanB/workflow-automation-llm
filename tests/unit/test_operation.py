from __future__ import annotations

import copy
import json
import subprocess
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
    compile_real_operation_scope_manifest,
    compile_role_permission_manifest,
    digest_json,
    parse_permission_digest_args,
    parse_repository_revision_args,
    validate_real_operation_prerequisites,
)
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.repository import RepositorySnapshot, inspect_repository
from dispatcher.state_store import StateStore
from dispatcher.verification import verification_backend
from dispatcher.workflow import StepStatus, TransitionEvent, new_run_record


class AvailableDarwinSeatbeltBackend:
    name = "darwin-seatbelt-v1"
    production_ready = True


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
    expected_repository_revisions: dict[str, str] | None = None,
) -> Path:
    permission_digests = _permission_digests(config, record, plan, repo_id)
    scope_manifest = compile_real_operation_scope_manifest(
        config=config,
        record=record,
        plan=plan,
        repo_id=repo_id,
    )
    if expected_repository_revisions is None:
        scope_repo_ids = tuple(dict.fromkeys(step.repo_id for step in scope_manifest.steps))
        if len(scope_repo_ids) > 1:
            expected_repository_revisions = {
                scoped_repo_id: inspect_repository(config, scoped_repo_id, require_clean=True).revision
                for scoped_repo_id in scope_repo_ids
            }
    approval = approve_real_operation(
        config=config,
        record=record,
        plan=plan,
        repo_id=repo_id,
        approval_ref="decision-real-operation",
        permission_digests=permission_digests,
        scope_manifest_digest=scope_manifest.digest if len(scope_manifest.steps) > 1 else None,
        expected_repository_revisions=expected_repository_revisions,
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


def _two_step_plan_values(project) -> dict:
    values = valid_plan_values(project)
    first = values["steps"][0]
    first["authorization"] = {
        "authorized_actions": ["inspect", "modify", "commit"],
        "writable_paths": ["evidence/", "first.txt"],
        "requires_operator_approval": False,
    }
    second = copy.deepcopy(first)
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
                "authorized_actions": ["inspect", "modify", "commit"],
                "writable_paths": ["evidence/", "second.txt"],
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
    values["steps"].append(second)
    return values


def _cross_repository_plan_values(project) -> dict:
    values = _two_step_plan_values(project)
    values["steps"][1]["repo_id"] = "sibling-repo"
    return values


def _scope_gap_plan_values(project) -> dict:
    values = _two_step_plan_values(project)
    first, runnable = values["steps"]
    runnable.update(
        {
            "step_id": "prepare-runnable",
            "title": "Prepare runnable fixture",
            "depends_on": [],
            "required_inputs": [],
            "produced_outputs": [
                {
                    "artifact_id": "runnable-output",
                    "producer_step_id": None,
                    "description": "Runnable fixture output",
                }
            ],
            "resource_locks": [{"resource_id": "runnable-resource", "mode": "write"}],
            "evidence_requirements": [
                {
                    "artifact_id": "runnable-evidence",
                    "relative_path": "runnable.md",
                    "media_type": "text/markdown",
                }
            ],
        }
    )
    blocked = copy.deepcopy(runnable)
    blocked.update(
        {
            "ordinal": 3,
            "step_id": "prepare-blocked",
            "title": "Prepare blocked fixture",
            "depends_on": [first["step_id"]],
            "required_inputs": [
                {
                    "artifact_id": "fixture-output",
                    "producer_step_id": first["step_id"],
                    "description": "Blocked fixture input",
                }
            ],
            "produced_outputs": [
                {
                    "artifact_id": "blocked-output",
                    "producer_step_id": None,
                    "description": "Blocked fixture output",
                }
            ],
            "resource_locks": [{"resource_id": "blocked-resource", "mode": "write"}],
            "evidence_requirements": [
                {
                    "artifact_id": "blocked-evidence",
                    "relative_path": "blocked.md",
                    "media_type": "text/markdown",
                }
            ],
        }
    )
    later = copy.deepcopy(runnable)
    later.update(
        {
            "ordinal": 4,
            "step_id": "prepare-later",
            "title": "Prepare later fixture",
            "depends_on": [runnable["step_id"]],
            "required_inputs": [
                {
                    "artifact_id": "runnable-output",
                    "producer_step_id": runnable["step_id"],
                    "description": "Runnable fixture input",
                }
            ],
            "produced_outputs": [
                {
                    "artifact_id": "later-output",
                    "producer_step_id": None,
                    "description": "Later fixture output",
                }
            ],
            "resource_locks": [{"resource_id": "later-resource", "mode": "write"}],
            "evidence_requirements": [
                {
                    "artifact_id": "later-evidence",
                    "relative_path": "later.md",
                    "media_type": "text/markdown",
                }
            ],
        }
    )
    values["steps"] = [first, runnable, blocked, later]
    return values


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
    values["execution"]["verification_backend"] = "darwin_seatbelt_v1"
    config = write_config(project, values)
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    record = _record(project, plan).model_copy(update={"config_digest": config.config_digest})
    store = StateStore(config.state_dir, heartbeat_seconds=30, stale_after_seconds=120)
    approval_path = _write_approval(
        tmp_path / "approval.json",
        config=config,
        record=record,
        plan=plan,
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
    values["execution"]["verification_backend"] = "darwin_seatbelt_v1"
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
        tmp_path / "approval.json",
        config=config,
        record=record,
        plan=plan,
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
    two_steps: bool = False,
    cross_repository: bool = False,
) -> dict[str, object]:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["execution"]["mode"] = "real_operation"
    values["execution"]["verification_backend"] = "darwin_seatbelt_v1"
    if two_steps or cross_repository:
        values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
        values["permission_policies"]["policies"]["executor-class"]["actions"]["commit"] = "allow"
    if cross_repository:
        sibling = project.root / "sibling-repository"
        subprocess.run(
            ["git", "init", "--quiet", str(sibling)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", "https://example.invalid/sibling.git"],
            cwd=sibling,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        (sibling / "evidence").mkdir()
        values["repositories"]["sibling-repo"] = {
            **values["repositories"]["fixture-repo"],
            "root": str(sibling),
            "expected_remote": {"name": "origin", "url": "https://example.invalid/sibling.git"},
        }
    config = write_config(project, values)
    plan_path = tmp_path / "plan.yaml"
    plan_values = (
        _cross_repository_plan_values(project)
        if cross_repository
        else _two_step_plan_values(project)
        if two_steps
        else valid_plan_values(project)
    )
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
    revisions = {"fixture-repo": "ab" * 20}
    if cross_repository:
        revisions["sibling-repo"] = "cd" * 20
    snapshot = RepositorySnapshot(
        repo_id="fixture-repo",
        branch="main",
        revision=revisions["fixture-repo"],
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
    snapshots = {
        repo_id: snapshot.model_copy(update={"repo_id": repo_id, "revision": revision})
        for repo_id, revision in revisions.items()
    }
    monkeypatch.setattr(
        "dispatcher.operation.inspect_repository",
        lambda _config, repo_id, **_kwargs: snapshots[repo_id],
    )
    monkeypatch.setattr("dispatcher.operation.validate_approved_baseline", lambda **kwargs: None)
    monkeypatch.setattr(
        "dispatcher.operation.verification_backend",
        lambda _config: AvailableDarwinSeatbeltBackend(),
    )
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
        tmp_path / "approval.json",
        config=config,
        record=record,
        plan=plan,
        expected_repository_revisions=revisions if cross_repository else None,
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
        "expected_revision": None if cross_repository else revisions["fixture-repo"],
        "expected_repository_revisions": revisions if cross_repository else None,
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


def test_real_operation_scope_binds_every_reachable_two_step_manifest(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    values["permission_policies"]["policies"]["executor-class"]["actions"]["commit"] = "allow"
    object.__setattr__(project, "config", write_config(project, values))
    plan = NormalizedPlan.model_validate(_two_step_plan_values(project))
    record = _record(project, plan)
    scope = compile_real_operation_scope_manifest(
        config=project.config,
        plan=plan,
        record=record,
        repo_id="fixture-repo",
    )

    assert [manifest.step_id for manifest in scope.steps] == ["prepare-fixture", "prepare-second"]
    assert scope.steps[0].structured_git.writable_paths == ("evidence/", "first.txt")
    assert scope.steps[1].structured_git.writable_paths == ("evidence/", "second.txt")
    assert scope.steps[1].structured_git.evidence_paths == ("evidence/second.md",)

    with pytest.raises(RealOperationError, match="scope-manifest-digest"):
        approve_real_operation(
            config=project.config,
            record=record,
            plan=plan,
            repo_id="fixture-repo",
            approval_ref="decision-real-operation",
            permission_digests=_permission_digests(project.config, record, plan),
        )

    approval = approve_real_operation(
        config=project.config,
        record=record,
        plan=plan,
        repo_id="fixture-repo",
        approval_ref="decision-real-operation",
        permission_digests=_permission_digests(project.config, record, plan),
        scope_manifest_digest=scope.digest,
    )
    assert approval.scope_manifest == scope


@pytest.mark.parametrize("blocked_by", ["dependency", "operator_gate"])
def test_scope_stops_before_an_unreachable_middle_step(
    tmp_path: Path,
    blocked_by: str,
) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    values["permission_policies"]["policies"]["executor-class"]["actions"]["commit"] = "allow"
    object.__setattr__(project, "config", write_config(project, values))
    plan = NormalizedPlan.model_validate(_scope_gap_plan_values(project))
    record = _record(project, plan)
    states = {
        **record.steps,
        "prepare-fixture": record.steps["prepare-fixture"].model_copy(
            update={"state": StepStatus.REVIEW_REQUIRED}
        ),
    }
    if blocked_by == "operator_gate":
        states["prepare-blocked"] = states["prepare-blocked"].model_copy(
            update={"state": StepStatus.REVIEW_REQUIRED, "operator_gate_resolved": False}
        )
    blocked = record.model_copy(update={"steps": states})

    scope = compile_real_operation_scope_manifest(
        config=project.config,
        plan=plan,
        record=blocked,
        repo_id="fixture-repo",
    )

    assert [manifest.step_id for manifest in scope.steps] == ["prepare-runnable"]


def test_execute_gate_validates_cross_repository_scope_revisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(
        tmp_path,
        monkeypatch,
        datetime.now(UTC),
        cross_repository=True,
    )
    approval = RealOperationApproval.model_validate_json(
        Path(kwargs["approval_record_path"]).read_text(encoding="utf-8")
    )
    assert approval.repository_revisions is not None
    assert [(item.repo_id, item.revision) for item in approval.repository_revisions] == [
        ("fixture-repo", "ab" * 20),
        ("sibling-repo", "cd" * 20),
    ]
    kwargs["stall_policy_digest"] = digest_json(
        kwargs["config"].execution.stall_policy.model_dump(mode="json")
    )

    validated = validate_real_operation_prerequisites(**kwargs)

    assert validated["repository_revisions"] == [
        {"repo_id": "fixture-repo", "revision": "ab" * 20},
        {"repo_id": "sibling-repo", "revision": "cd" * 20},
    ]
    record = kwargs["record"]
    resumed = record.model_copy(
        update={
            "steps": {
                **record.steps,
                "prepare-fixture": record.steps["prepare-fixture"].model_copy(
                    update={"state": StepStatus.ACCEPTED}
                ),
            }
        }
    )
    kwargs["record"] = resumed
    kwargs["permission_digests"] = _permission_digests(
        kwargs["config"],
        resumed,
        resumed.plan,
        "sibling-repo",
    )
    assert validate_real_operation_prerequisites(**kwargs)["step_id"] == "prepare-second"
    incomplete = {**kwargs, "expected_repository_revisions": {"fixture-repo": "ab" * 20}}
    with pytest.raises(RealOperationError, match="missing: sibling-repo"):
        validate_real_operation_prerequisites(**incomplete)


@pytest.mark.parametrize("failure", ["dirty", "revision"])
def test_execute_gate_rejects_later_cross_repository_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(
        tmp_path,
        monkeypatch,
        datetime.now(UTC),
        cross_repository=True,
    )
    kwargs["stall_policy_digest"] = digest_json(
        kwargs["config"].execution.stall_policy.model_dump(mode="json")
    )

    def snapshot(repo_id: str) -> RepositorySnapshot:
        sibling = repo_id == "sibling-repo"
        return RepositorySnapshot(
            repo_id=repo_id,
            branch="main",
            revision=("ef" * 20 if sibling and failure == "revision" else "cd" * 20 if sibling else "ab" * 20),
            worktree_id="cd" * 32,
            remote_name="origin",
            remote_url="https://example.invalid/fixture.git",
            clean=not (sibling and failure == "dirty"),
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
        "dispatcher.operation.inspect_repository",
        lambda _config, repo_id, **_kwargs: snapshot(repo_id),
    )

    with pytest.raises(RealOperationError, match="(sibling-repo.*not clean|expected revision: sibling-repo)"):
        validate_real_operation_prerequisites(**kwargs)


@pytest.mark.parametrize("mutation", ["missing", "reordered", "extra", "later_role", "writable", "evidence"])
def test_execute_gate_rejects_changed_two_step_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(
        tmp_path,
        monkeypatch,
        datetime.now(UTC),
        two_steps=True,
    )
    approval_path = Path(kwargs["approval_record_path"])
    approval = RealOperationApproval.model_validate_json(approval_path.read_text(encoding="utf-8"))
    assert approval.scope_manifest is not None
    scope = approval.scope_manifest
    first, second = scope.steps
    if mutation == "missing":
        steps = (first,)
    elif mutation == "reordered":
        steps = (second, first)
    elif mutation == "extra":
        steps = (first, second, first)
    elif mutation == "later_role":
        role = second.roles["supervisor"].model_copy(update={"digest": "0" * 64})
        steps = (first, second.model_copy(update={"roles": {**second.roles, "supervisor": role}}))
    elif mutation == "writable":
        capability = second.structured_git.model_copy(update={"writable_paths": ("other.txt",)})
        steps = (first, second.model_copy(update={"structured_git": capability}))
    else:
        capability = second.structured_git.model_copy(update={"evidence_paths": ("evidence/other.md",)})
        steps = (first, second.model_copy(update={"structured_git": capability}))
    approval_path.write_text(
        approval.model_copy(update={"scope_manifest": scope.model_copy(update={"steps": steps})}).model_dump_json(),
        encoding="utf-8",
    )

    with pytest.raises(RealOperationError, match="complete approval scope"):
        validate_real_operation_prerequisites(**kwargs)


def test_execute_gate_accepts_legacy_single_step_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(tmp_path, monkeypatch, datetime.now(UTC))
    approval_path = Path(kwargs["approval_record_path"])
    legacy = RealOperationApproval.model_validate_json(approval_path.read_text(encoding="utf-8")).model_dump(
        mode="json"
    )
    legacy.pop("scope_manifest")
    approval_path.write_text(json.dumps(legacy), encoding="utf-8")
    config = kwargs["config"]
    kwargs["stall_policy_digest"] = digest_json(config.execution.stall_policy.model_dump(mode="json"))

    assert validate_real_operation_prerequisites(**kwargs)["step_id"] == "prepare-fixture"


def test_execute_gate_rejects_legacy_single_step_approval_for_two_step_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(
        tmp_path,
        monkeypatch,
        datetime.now(UTC),
        two_steps=True,
    )
    approval_path = Path(kwargs["approval_record_path"])
    legacy = RealOperationApproval.model_validate_json(approval_path.read_text(encoding="utf-8")).model_dump(
        mode="json"
    )
    legacy.pop("scope_manifest")
    approval_path.write_text(json.dumps(legacy), encoding="utf-8")

    with pytest.raises(RealOperationError, match="legacy single-step"):
        validate_real_operation_prerequisites(**kwargs)


def test_execute_gate_resumes_complete_scope_from_first_review_required_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(
        tmp_path,
        monkeypatch,
        datetime.now(UTC),
        two_steps=True,
    )
    record = kwargs["record"]
    first = record.steps["prepare-fixture"].model_copy(update={"state": StepStatus.REVIEW_REQUIRED})
    kwargs["record"] = record.model_copy(
        update={"steps": {**record.steps, "prepare-fixture": first}}
    )
    kwargs["stall_policy_digest"] = digest_json(
        kwargs["config"].execution.stall_policy.model_dump(mode="json")
    )

    validated = validate_real_operation_prerequisites(**kwargs)

    assert validated["step_id"] == "prepare-fixture"
    assert [item["step_id"] for item in validated["scope_manifest"]["steps"]] == [
        "prepare-fixture",
        "prepare-second",
    ]


def test_execute_gate_resumes_approved_suffix_after_first_step_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(
        tmp_path,
        monkeypatch,
        datetime.now(UTC),
        two_steps=True,
    )
    record = kwargs["record"]
    first = record.steps["prepare-fixture"].model_copy(update={"state": StepStatus.ACCEPTED})
    resumed = record.model_copy(update={"steps": {**record.steps, "prepare-fixture": first}})
    kwargs["record"] = resumed
    kwargs["permission_digests"] = _permission_digests(
        kwargs["config"],
        resumed,
        resumed.plan,
    )
    kwargs["stall_policy_digest"] = digest_json(
        kwargs["config"].execution.stall_policy.model_dump(mode="json")
    )

    validated = validate_real_operation_prerequisites(**kwargs)

    assert validated["step_id"] == "prepare-second"
    assert validated["permission_manifest"]["step_id"] == "prepare-second"
    assert [item["step_id"] for item in validated["scope_manifest"]["steps"]] == [
        "prepare-fixture",
        "prepare-second",
    ]


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


def test_repository_revision_arguments_reject_malformed_and_duplicate_repositories() -> None:
    with pytest.raises(RealOperationError, match="REPOSITORY=REVISION"):
        parse_repository_revision_args(["not-a-pair"])
    with pytest.raises(RealOperationError, match="full lowercase Git object ID"):
        parse_repository_revision_args(["fixture-repo=bad"])
    with pytest.raises(RealOperationError, match="duplicate repository"):
        parse_repository_revision_args(
            [f"fixture-repo={'0' * 40}", f"fixture-repo={'1' * 40}"]
        )


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


def test_execute_gate_rejects_unavailable_darwin_seatbelt_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _real_operation_kwargs_with_smoke_proof(tmp_path, monkeypatch, datetime.now(UTC))
    config = kwargs["config"]
    kwargs["stall_policy_digest"] = digest_json(
        config.execution.stall_policy.model_dump(mode="json")
    )
    monkeypatch.setattr("dispatcher.operation.verification_backend", verification_backend)
    monkeypatch.setattr("dispatcher.verification.platform.system", lambda: "Linux")

    with pytest.raises(RealOperationError, match="production verification isolation is unavailable"):
        validate_real_operation_prerequisites(**kwargs)
