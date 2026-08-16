from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from helpers import FixtureProject, create_fixture_project, valid_plan_values

from dispatcher.cli import main
from dispatcher.cluster_operation_lifecycle import (
    ActionDigest,
    ClusterOperationApproval,
    ClusterOperationApprovalSnapshot,
    ClusterOperationLifecycleError,
    ClusterOperationStatus,
    ClusterResourceFingerprint,
    NamedDigest,
    assert_cluster_operation_safe_payload,
    attach_cluster_operation_approval,
    attach_cluster_operation_snapshot,
    create_auto_approved_cluster_operation_approval,
    load_sanitized_cluster_operation_snapshot,
    new_cluster_operation_lifecycle_record,
    transition_cluster_operation,
)
from dispatcher.cluster_operations import ClusterOperationManifest, ValidatedClusterOperation
from dispatcher.operation import (
    ClusterOperationEnvelope,
    RealOperationApproval,
    RealOperationScopeManifest,
    RolePermissionManifest,
    StructuredGitCapability,
    digest_json,
)
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.state_store import StateStore, StateStoreConflictError, StateStoreCorruptionError
from dispatcher.workflow import TransitionEvent, new_run_record

_SOURCE_REVISION = "a" * 40


@pytest.fixture
def project(tmp_path: Path) -> FixtureProject:
    return create_fixture_project(tmp_path)


def _now() -> datetime:
    return datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def _validated_operation() -> ValidatedClusterOperation:
    deployment = {
        "api_version": "apps/v1",
        "kind": "Deployment",
        "namespace": "platform",
        "name": "sample-app",
    }
    manifest = ClusterOperationManifest.model_validate(
        {
            "schema_version": 1,
            "operation_id": "sample-app-deploy",
            "context": "fixture-context",
            "source_identity": {"repository_id": "fixture-repo", "revision": "approval_snapshot"},
            "allowed_namespaces": ["platform"],
            "allowed_files": [{"path": "deploy/sample-app.yaml", "sha256": "approval_snapshot"}],
            "actions": [
                {
                    "action": "kubectl_server_dry_run",
                    "namespace": "platform",
                    "timeout_seconds": 30,
                    "expected_resources": [deployment],
                    "readiness_probes": [{"probe": "deployment_available", "resource": deployment}],
                    "manifest_files": [
                        {"path": "deploy/sample-app.yaml", "sha256": "approval_snapshot"}
                    ],
                }
            ],
            "rollback": {"automatic": True, "strategy": "restore_approval_snapshot"},
        }
    )
    return ValidatedClusterOperation(
        step_id="prepare-fixture",
        target_name="fixture-target",
        repository_root=Path("/fixture"),
        manifest_path=Path("/fixture/deploy/operation.yaml"),
        manifest=manifest,
    )


def _record(now: datetime | None = None):
    captured = now or _now()
    return new_cluster_operation_lifecycle_record(
        _validated_operation(),
        run_id="fixture-run",
        source_revision=_SOURCE_REVISION,
        plan_digest="b" * 64,
        config_digest="c" * 64,
        max_snapshot_age_seconds=900,
        now=captured,
    )


def _snapshot(record, *, now: datetime | None = None, source_revision: str = _SOURCE_REVISION):
    captured = now or _now()
    return ClusterOperationApprovalSnapshot(
        run_id=record.run_id,
        step_id=record.step_id,
        operation_id=record.operation_id,
        source_revision=source_revision,
        plan_digest=record.plan_digest,
        config_digest=record.config_digest,
        validated_manifest_digest=record.validated_manifest_digest,
        cluster_preflight_result_digest="d" * 64,
        binary_identity_digests=(NamedDigest(name="helm", sha256="e" * 64),),
        toolchain_identity_digests=(
            NamedDigest(name="helm", sha256="e" * 64),
            NamedDigest(name="kubectl", sha256="f" * 64),
        ),
        tier1_invariant_snapshot_digest="1" * 64,
        action_digests=record.static_action_digests,
        source_file_digests=({"path": "deploy/sample-app.yaml", "sha256": "3" * 64},),
        resource_fingerprints=(
            ClusterResourceFingerprint(
                resource={
                    "api_version": "apps/v1",
                    "kind": "Deployment",
                    "namespace": "platform",
                    "name": "sample-app",
                },
                sha256="2" * 64,
            ),
        ),
        release_fingerprints=(),
        release_rollback_snapshots=(),
        image_fingerprints=(),
        secret_metadata_fingerprints=(),
        captured_at=captured,
        expires_at=captured + timedelta(minutes=10),
    )


def _approval(record, snapshot, *, now: datetime | None = None, digest: str | None = None):
    issued = now or _now()
    return ClusterOperationApproval(
        owner_ref="release-owner",
        run_id=record.run_id,
        step_id=record.step_id,
        operation_id=record.operation_id,
        source_revision=record.source_revision,
        snapshot_digest=snapshot.digest,
        allowed_actions=(
            ActionDigest(
                action_id=record.static_action_digests[0].action_id,
                sha256=digest or record.static_action_digests[0].sha256,
            ),
        ),
        rollback_intent=record.rollback_intent,
        rollback_digest=record.rollback_digest,
        issued_at=issued,
        expires_at=issued + timedelta(minutes=5),
    )


def _real_operation_approval(
    operation: ValidatedClusterOperation,
    *,
    allowed_actions: tuple[str, ...] = ("kubectl_server_dry_run",),
) -> RealOperationApproval:
    envelope_payload = {
        "envelope_version": 1,
        "run_id": "fixture-run",
        "step_id": operation.step_id,
        "repo_id": operation.manifest.source_identity.repository_id,
        "target_name": operation.target_name,
        "context": operation.manifest.context,
        "operation_manifest_path": "deploy/operation.yaml",
        "allowed_actions": list(allowed_actions),
        "automatic_rollback": True,
        "operation_manifest_roots": ["deploy"],
        "source_file_roots": ["deploy"],
        "max_snapshot_age_seconds": 900,
        "plan_digest": "b" * 64,
        "config_digest": "c" * 64,
    }
    envelope = ClusterOperationEnvelope(**envelope_payload, digest=digest_json(envelope_payload))
    capability = StructuredGitCapability(
        capability_version=1,
        safety_policy_version=1,
        repo_id="fixture-repo",
        step_id=operation.step_id,
        commit_policy="required",
        commit_authorized=True,
        writable_paths=("deploy/",),
        evidence_paths=("evidence/operation.md",),
        message_format="dispatcher: <step_id> attempt <n>",
        identity_digest="d" * 64,
        digest="e" * 64,
    )
    permission = RolePermissionManifest(
        manifest_version=2,
        repo_id="fixture-repo",
        step_id=operation.step_id,
        roles={},
        structured_git=capability,
    )
    scope_payload = {
        "scope_version": 1,
        "steps": [permission.model_dump(mode="json")],
        "cluster_operation_envelopes": [envelope.model_dump(mode="json")],
    }
    scope = RealOperationScopeManifest(
        scope_version=1,
        steps=(permission,),
        cluster_operation_envelopes=(envelope,),
        digest=digest_json(scope_payload),
    )
    return RealOperationApproval(
        approval_ref="real-operation-decision",
        project_id="fixture-project",
        config_digest="c" * 64,
        plan_digest="b" * 64,
        run_id="fixture-run",
        repo_id="fixture-repo",
        step_id=operation.step_id,
        permission_manifest=permission,
        scope_manifest=scope,
        cluster_operation_envelopes=(envelope,),
        decided_at=_now(),
    )


def _store_with_run(project: FixtureProject) -> StateStore:
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    workflow_record = new_run_record(
        run_id="fixture-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-fixture"),
        event=TransitionEvent(
            event_id="event-fixture",
            sequence=1,
            actor="dispatcher",
            reason="fixture",
            correlation_id="fixture-run",
            occurred_at=_now(),
        ),
    )
    store = StateStore(
        project.state,
        heartbeat_seconds=project.config.lease_heartbeat_seconds,
        stale_after_seconds=project.config.lease_stale_after_seconds,
    )
    store.create_run(workflow_record)
    return store


def test_lifecycle_requires_explicit_legal_transitions_and_fresh_snapshot() -> None:
    record = _record()

    with pytest.raises(ClusterOperationLifecycleError, match="requires attach"):
        transition_cluster_operation(record, ClusterOperationStatus.APPROVED, now=_now())
    with pytest.raises(
        ClusterOperationLifecycleError, match="illegal cluster operation transition"
    ):
        attach_cluster_operation_snapshot(record, _snapshot(record), now=_now())

    validated = transition_cluster_operation(
        record, ClusterOperationStatus.STATIC_VALIDATED, now=_now()
    )
    captured = attach_cluster_operation_snapshot(validated, _snapshot(validated), now=_now())
    approved = attach_cluster_operation_approval(
        captured, _approval(captured, captured.snapshot), now=_now()
    )

    assert approved.status is ClusterOperationStatus.APPROVED
    assert (
        transition_cluster_operation(
            approved, ClusterOperationStatus.SERVER_DRY_RUN_PASSED, now=_now()
        ).status
        is ClusterOperationStatus.SERVER_DRY_RUN_PASSED
    )


def test_expired_or_mismatched_snapshot_and_approval_are_rejected() -> None:
    validated = transition_cluster_operation(
        _record(), ClusterOperationStatus.STATIC_VALIDATED, now=_now()
    )
    with pytest.raises(ClusterOperationLifecycleError, match="immutable operation identity"):
        attach_cluster_operation_snapshot(
            validated,
            _snapshot(validated, source_revision="b" * 40),
            now=_now(),
        )

    captured = attach_cluster_operation_snapshot(validated, _snapshot(validated), now=_now())
    with pytest.raises(ClusterOperationLifecycleError, match="not in the static operation"):
        attach_cluster_operation_approval(
            captured,
            _approval(captured, captured.snapshot, digest="9" * 64),
            now=_now(),
        )
    expired = _approval(captured, captured.snapshot, now=_now())
    with pytest.raises(ClusterOperationLifecycleError, match="approval has expired"):
        attach_cluster_operation_approval(captured, expired, now=_now() + timedelta(minutes=6))

    approved = attach_cluster_operation_approval(
        captured, _approval(captured, captured.snapshot), now=_now()
    )
    with pytest.raises(ClusterOperationLifecycleError, match="approval has expired"):
        transition_cluster_operation(
            approved,
            ClusterOperationStatus.SERVER_DRY_RUN_PASSED,
            now=_now() + timedelta(minutes=6),
        )


def test_envelope_auto_approval_requires_exact_manifest_then_binds_to_fresh_snapshot() -> None:
    operation = _validated_operation()
    real_approval = _real_operation_approval(operation)

    auto_approval = create_auto_approved_cluster_operation_approval(
        operation,
        _SOURCE_REVISION,
        real_approval,
        now=_now(),
    )

    assert auto_approval.approval_source == "preauthorized_envelope"
    assert auto_approval.snapshot_digest is None
    assert auto_approval.envelope_digest == real_approval.cluster_operation_envelopes[0].digest

    validated = transition_cluster_operation(
        _record(), ClusterOperationStatus.STATIC_VALIDATED, now=_now()
    )
    captured_snapshot = _snapshot(validated).model_copy(
        update={"envelope_digest": auto_approval.envelope_digest}
    )
    captured = attach_cluster_operation_snapshot(validated, captured_snapshot, now=_now())
    approved = attach_cluster_operation_approval(captured, auto_approval, now=_now())

    assert approved.approval is not None
    assert approved.approval.snapshot_digest == captured.snapshot_digest
    assert approved.approval.expires_at == captured.snapshot.expires_at
    with pytest.raises(ClusterOperationLifecycleError, match="approval snapshot has expired"):
        transition_cluster_operation(
            approved,
            ClusterOperationStatus.SERVER_DRY_RUN_PASSED,
            now=_now() + timedelta(minutes=11),
        )

    with pytest.raises(ClusterOperationLifecycleError, match="manifest actions"):
        create_auto_approved_cluster_operation_approval(
            operation,
            _SOURCE_REVISION,
            _real_operation_approval(operation, allowed_actions=("helm_upgrade_install",)),
            now=_now(),
        )
    with pytest.raises(ClusterOperationLifecycleError, match="no exact preauthorized envelope"):
        create_auto_approved_cluster_operation_approval(
            operation,
            _SOURCE_REVISION,
            real_approval.model_copy(update={"cluster_operation_envelopes": ()}),
            now=_now(),
        )


def test_journal_uses_generation_cas_reloads_and_checks_immutable_columns(
    project: FixtureProject,
) -> None:
    store = _store_with_run(project)
    record = _record(datetime.now(UTC))

    assert store.create_cluster_operation(record) == 1
    validated = store.transition_cluster_operation(
        run_id=record.run_id,
        operation_id=record.operation_id,
        source_revision=record.source_revision,
        expected_generation=1,
        target=ClusterOperationStatus.STATIC_VALIDATED,
        now=_now(),
    )
    assert validated.generation == 2
    store.close()

    restored = StateStore(
        project.state,
        heartbeat_seconds=project.config.lease_heartbeat_seconds,
        stale_after_seconds=project.config.lease_stale_after_seconds,
    )
    loaded = restored.load_cluster_operation(
        run_id=record.run_id,
        operation_id=record.operation_id,
        source_revision=record.source_revision,
    )
    assert loaded == validated
    with pytest.raises(StateStoreConflictError, match="generation conflict"):
        restored.transition_cluster_operation(
            run_id=record.run_id,
            operation_id=record.operation_id,
            source_revision=record.source_revision,
            expected_generation=1,
            target=ClusterOperationStatus.FAILED,
            now=_now(),
        )

    with sqlite3.connect(restored.database_path) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="identity is immutable"):
            connection.execute(
                """
                UPDATE cluster_operation_journal SET config_digest = ?
                WHERE run_id = ? AND operation_id = ? AND source_revision = ?
                """,
                ("0" * 64, record.run_id, record.operation_id, record.source_revision),
            )
        raw = json.loads(
            connection.execute(
                """
                SELECT record_json FROM cluster_operation_journal
                WHERE run_id = ? AND operation_id = ? AND source_revision = ?
                """,
                (record.run_id, record.operation_id, record.source_revision),
            ).fetchone()[0]
        )
        raw["config_digest"] = "0" * 64
        connection.execute(
            """
            UPDATE cluster_operation_journal SET record_json = ?
            WHERE run_id = ? AND operation_id = ? AND source_revision = ?
            """,
            (json.dumps(raw), record.run_id, record.operation_id, record.source_revision),
        )
    with pytest.raises(StateStoreCorruptionError, match="immutable record"):
        restored.load_cluster_operation(
            run_id=record.run_id,
            operation_id=record.operation_id,
            source_revision=record.source_revision,
        )


def test_secret_like_payloads_are_rejected_and_journal_audit_is_digest_only(
    project: FixtureProject,
    tmp_path: Path,
) -> None:
    raw_secret = "TOKEN_MATERIAL_DO_NOT_PERSIST"
    with pytest.raises(ClusterOperationLifecycleError) as exc_info:
        assert_cluster_operation_safe_payload({"token": raw_secret})
    assert raw_secret not in str(exc_info.value)

    snapshot_path = tmp_path / "unsafe-snapshot.json"
    snapshot_path.write_text(json.dumps({"token": raw_secret}), encoding="utf-8")
    with pytest.raises(ClusterOperationLifecycleError) as exc_info:
        load_sanitized_cluster_operation_snapshot(snapshot_path)
    assert raw_secret not in str(exc_info.value)

    store = _store_with_run(project)
    record = _record()
    store.create_cluster_operation(record)
    with sqlite3.connect(store.database_path) as connection:
        payload = connection.execute(
            "SELECT payload_json FROM cluster_operation_audit_events"
        ).fetchone()[0]
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM cluster_operation_audit_events")
    assert raw_secret not in payload
    assert "record_digest" in json.loads(payload)


def test_cluster_operation_cli_is_local_only_and_requires_static_validation(
    project: FixtureProject,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[object] = []

    real_subprocess_run = subprocess.run

    def no_cluster_subprocess(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        command = args[0] if args else kwargs.get("args")
        assert isinstance(command, list)
        assert command[0] == "git"
        return real_subprocess_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", no_cluster_subprocess)
    missing = main(
        [
            "cluster-operation",
            "status",
            "--config",
            str(project.config_path),
            "--run-id",
            "missing-run",
            "--operation-id",
            "missing-operation",
            "--source-revision",
            _SOURCE_REVISION,
        ]
    )
    assert missing == 2
    assert not (project.state / "dispatcher.sqlite3").exists()

    store = _store_with_run(project)
    record = _record(datetime.now(UTC))
    store.create_cluster_operation(record)
    record = store.transition_cluster_operation(
        run_id=record.run_id,
        operation_id=record.operation_id,
        source_revision=record.source_revision,
        expected_generation=record.generation,
        target=ClusterOperationStatus.STATIC_VALIDATED,
        now=datetime.now(UTC),
    )
    snapshot = _snapshot(record, now=datetime.now(UTC))
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(snapshot.model_dump_json(), encoding="utf-8")

    result = main(
        [
            "cluster-operation",
            "approve",
            "--config",
            str(project.config_path),
            "--run-id",
            record.run_id,
            "--operation-id",
            record.operation_id,
            "--source-revision",
            record.source_revision,
            "--snapshot",
            str(snapshot_path),
            "--owner-ref",
            "release-owner",
            "--allowed-action",
            f"{record.static_action_digests[0].action_id}={record.static_action_digests[0].sha256}",
            "--rollback-digest",
            record.rollback_digest,
            "--expires-at",
            (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        ]
    )

    assert result == 0
    assert calls
    assert "status=APPROVED" in capsys.readouterr().out
    assert (
        store.load_cluster_operation(
            run_id=record.run_id,
            operation_id=record.operation_id,
            source_revision=record.source_revision,
        ).status
        is ClusterOperationStatus.APPROVED
    )
