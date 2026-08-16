from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from helpers import FixtureProject, config_values, create_fixture_project, write_config

from dispatcher.cluster_operation_lifecycle import (
    ClusterOperationLifecycleError,
    ClusterOperationStatus,
    attach_cluster_operation_snapshot,
    new_cluster_operation_lifecycle_record,
    transition_cluster_operation,
)
from dispatcher.cluster_operation_snapshot import (
    ClusterOperationSnapshotCommandResult,
    ClusterOperationSnapshotError,
    capture_cluster_operation_snapshot,
)
from dispatcher.cluster_operations import ClusterOperationManifest, ValidatedClusterOperation
from dispatcher.config import Config
from dispatcher.operation import (
    ClusterOperationEnvelope,
    RealOperationApproval,
    RealOperationScopeManifest,
    RolePermissionManifest,
    StructuredGitCapability,
    digest_json,
)

_SOURCE_REVISION = "a" * 40
_TIER1_DIGEST = "1" * 64


@dataclass
class FakeReadOnlyRunner:
    expected_argv: list[tuple[str, ...]]
    results: list[ClusterOperationSnapshotCommandResult]
    calls: list[tuple[tuple[str, ...], int]]
    mutate_source: Path | None = None

    def __call__(
        self, argv: tuple[str, ...], timeout_seconds: int
    ) -> ClusterOperationSnapshotCommandResult:
        self.calls.append((argv, timeout_seconds))
        assert self.expected_argv.pop(0) == argv
        if self.mutate_source is not None and len(self.calls) == 1:
            self.mutate_source.write_text("replicaCount: 2\n", encoding="utf-8")
        return self.results.pop(0)

    def assert_complete(self) -> None:
        assert self.expected_argv == []
        assert self.results == []


@dataclass(frozen=True)
class SnapshotFixture:
    project: FixtureProject
    config: Config
    operation: ValidatedClusterOperation
    approval: RealOperationApproval
    kubectl: Path
    helm: Path
    values_file: Path


def _now() -> datetime:
    return datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> SnapshotFixture:
    project = create_fixture_project(tmp_path)
    tools = project.root / "mutation-tools"
    tools.mkdir()
    kubectl = tools / "kubectl"
    helm = tools / "helm"
    kubectl.write_bytes(b"fake-kubectl")
    helm.write_bytes(b"fake-helm")
    kubectl.chmod(0o700)
    helm.chmod(0o700)

    manifest_file = project.repository / "deploy/manifests/sample-app.yaml"
    chart_path = project.repository / "deploy/charts/sample-app"
    values_file = project.repository / "deploy/values/sample-app.yaml"
    operation_path = project.repository / "deploy/operations/sample-app.yaml"
    for path in (manifest_file.parent, chart_path, values_file.parent, operation_path.parent):
        path.mkdir(parents=True, exist_ok=True)
    manifest_file.write_text("apiVersion: apps/v1\nkind: Deployment\n", encoding="utf-8")
    (chart_path / "Chart.lock").write_text("dependencies: []\n", encoding="utf-8")
    values_file.write_text("replicaCount: 1\n", encoding="utf-8")

    values = config_values(project)
    values["cluster_preflight"] = {
        "capability_version": 1,
        "target_id": "fixture-readiness",
        "context": "fixture-context",
        "minimum_client_version": "v1.27.0",
        "minimum_server_version": "v1.27.0",
        "request_timeout_seconds": 10,
        "required_namespaces": ["platform"],
        "required_helm_releases": [
            {
                "release": "sample-app",
                "namespace": "platform",
                "chart": "sample-app",
                "minimum_chart_version": "1.0.0",
            }
        ],
        "required_api_resources": [{"resource": "deployments.apps"}],
        "auth_checks": [{"verb": "get", "resource": "deployments.apps", "namespace": "platform"}],
    }
    values["cluster_mutation"] = {
        "capability_version": 1,
        "targets": {
            "fixture-target": {
                "context": "fixture-context",
                "toolchain": {
                    "kubectl": {"path": str(kubectl), "sha256": _digest(kubectl)},
                    "helm": {"path": str(helm), "sha256": _digest(helm)},
                },
                "allowed_repository_ids": ["fixture-repo"],
                "operation_manifest_roots": ["deploy/operations"],
                "source_file_roots": ["deploy"],
                "max_snapshot_age_seconds": 900,
                "max_action_timeout_seconds": 120,
                "preflight_target_id": "fixture-readiness",
            }
        },
    }
    config = write_config(project, values)
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
            "allowed_files": [
                {"path": "deploy/charts/sample-app/Chart.lock", "sha256": "approval_snapshot"},
                {"path": "deploy/manifests/sample-app.yaml", "sha256": "approval_snapshot"},
                {"path": "deploy/values/sample-app.yaml", "sha256": "approval_snapshot"},
            ],
            "secret_requirements": [
                {
                    "namespace": "platform",
                    "name": "sample-app-secret",
                    "keys": ["username", "password"],
                }
            ],
            "actions": [
                {
                    "action": "kubectl_server_dry_run",
                    "namespace": "platform",
                    "timeout_seconds": 30,
                    "expected_resources": [deployment],
                    "readiness_probes": [{"probe": "deployment_available", "resource": deployment}],
                    "manifest_files": [
                        {"path": "deploy/manifests/sample-app.yaml", "sha256": "approval_snapshot"}
                    ],
                },
                {
                    "action": "helm_upgrade_install",
                    "namespace": "platform",
                    "timeout_seconds": 60,
                    "expected_resources": [deployment],
                    "readiness_probes": [{"probe": "deployment_available", "resource": deployment}],
                    "release": "sample-app",
                    "chart_path": "deploy/charts/sample-app",
                    "chart_lock_file": {
                        "path": "deploy/charts/sample-app/Chart.lock",
                        "sha256": "approval_snapshot",
                    },
                    "values_files": [
                        {"path": "deploy/values/sample-app.yaml", "sha256": "approval_snapshot"}
                    ],
                },
            ],
            "rollback": {"automatic": True, "strategy": "restore_approval_snapshot"},
        }
    )
    operation_path.write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    operation = ValidatedClusterOperation(
        step_id="prepare-fixture",
        target_name="fixture-target",
        repository_root=project.repository,
        manifest_path=operation_path,
        manifest=manifest,
    )
    return SnapshotFixture(
        project=project,
        config=config,
        operation=operation,
        approval=_real_approval(operation, config),
        kubectl=kubectl,
        helm=helm,
        values_file=values_file,
    )


def _real_approval(operation: ValidatedClusterOperation, config: Config) -> RealOperationApproval:
    envelope_payload = {
        "envelope_version": 1,
        "run_id": "fixture-run",
        "step_id": operation.step_id,
        "repo_id": "fixture-repo",
        "target_name": operation.target_name,
        "context": operation.manifest.context,
        "operation_manifest_path": "deploy/operations/sample-app.yaml",
        "allowed_actions": ["kubectl_server_dry_run", "helm_upgrade_install"],
        "automatic_rollback": True,
        "operation_manifest_roots": ["deploy/operations"],
        "source_file_roots": ["deploy"],
        "max_snapshot_age_seconds": 900,
        "plan_digest": "b" * 64,
        "config_digest": config.config_digest,
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
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan_digest="b" * 64,
        run_id="fixture-run",
        repo_id="fixture-repo",
        step_id=operation.step_id,
        permission_manifest=permission,
        scope_manifest=scope,
        cluster_operation_envelopes=(envelope,),
        decided_at=_now(),
    )


def _expected_argv(fixture: SnapshotFixture) -> list[tuple[str, ...]]:
    resource_jsonpath = (
        r"jsonpath={.apiVersion}{'\t'}{.kind}{'\t'}{.metadata.namespace}{'\t'}"
        r"{.metadata.name}{'\t'}{.metadata.uid}{'\t'}{.metadata.resourceVersion}"
    )
    secret_template = (
        'go-template={{.metadata.uid}}{{"\\t"}}{{.metadata.resourceVersion}}{{"\\t"}}'
        '{{.type}}{{"\\t"}}{{range $key, $_ := .data}}{{$key}}{{","}}{{end}}'
    )
    return [
        (str(fixture.kubectl), "config", "current-context"),
        (
            str(fixture.kubectl),
            "--context",
            "fixture-context",
            "--request-timeout=10s",
            "version",
            "--output=json",
        ),
        (
            str(fixture.kubectl),
            "--context",
            "fixture-context",
            "--namespace",
            "platform",
            "--request-timeout=10s",
            "get",
            "deployment/sample-app",
            f"--output={resource_jsonpath}",
        ),
        (
            str(fixture.kubectl),
            "--context",
            "fixture-context",
            "--namespace",
            "platform",
            "--request-timeout=10s",
            "get",
            "secret/sample-app-secret",
            f"--output={secret_template}",
        ),
        (
            str(fixture.helm),
            "status",
            "sample-app",
            "--namespace",
            "platform",
            "--kube-context",
            "fixture-context",
            "--output=json",
        ),
        (
            str(fixture.helm),
            "history",
            "sample-app",
            "--namespace",
            "platform",
            "--kube-context",
            "fixture-context",
            "--output=json",
        ),
    ]


def _existing_results() -> list[ClusterOperationSnapshotCommandResult]:
    return [
        ClusterOperationSnapshotCommandResult(0, b"fixture-context\n"),
        ClusterOperationSnapshotCommandResult(
            0,
            b'{"clientVersion":{"gitVersion":"v1.28.0"},"serverVersion":{"gitVersion":"v1.27.3"}}',
        ),
        ClusterOperationSnapshotCommandResult(
            0, b"apps/v1\tDeployment\tplatform\tsample-app\tresource-uid\t42\n"
        ),
        ClusterOperationSnapshotCommandResult(0, b"secret-uid\t7\tOpaque\tpassword,username,"),
        ClusterOperationSnapshotCommandResult(
            0,
            b'{"name":"sample-app","namespace":"platform","version":7,'
            b'"info":{"status":"deployed"},'
            b'"chart":{"metadata":{"version":"1.2.3","appVersion":"2.3.4"}}}',
        ),
        ClusterOperationSnapshotCommandResult(
            0,
            b'[{"revision":7,"status":"deployed","chart":"sample-app-1.2.3",'
            b'"app_version":"2.3.4"}]',
        ),
    ]


def _capture(fixture: SnapshotFixture, runner: FakeReadOnlyRunner):
    return capture_cluster_operation_snapshot(
        config=fixture.config,
        operation=fixture.operation,
        source_revision=_SOURCE_REVISION,
        real_operation_approval=fixture.approval,
        tier1_invariant_snapshot_digest=_TIER1_DIGEST,
        command_runner=runner,
        now=_now(),
    )


def test_capture_uses_only_exact_read_only_argv_and_binds_existing_release_metadata(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    runner = FakeReadOnlyRunner(_expected_argv(fixture), _existing_results(), [])

    snapshot = _capture(fixture, runner)

    runner.assert_complete()
    assert snapshot.envelope_digest == fixture.approval.cluster_operation_envelopes[0].digest
    assert snapshot.expires_at == _now() + timedelta(minutes=15)
    assert [item.name for item in snapshot.toolchain_identity_digests] == ["helm", "kubectl"]
    assert snapshot.release_fingerprints[0].model_dump(exclude={"sha256"}) == {
        "namespace": "platform",
        "release": "sample-app",
        "pre_snapshot_state": "existing",
        "pre_snapshot_revision": 7,
        "chart_version": "1.2.3",
        "app_version": "2.3.4",
        "status": "deployed",
    }
    assert snapshot.release_rollback_snapshots[0].pre_snapshot_revision == 7
    assert snapshot.secret_metadata_fingerprints[0].keys == ("password", "username")
    assert all(
        argument not in {"apply", "rollout", "port-forward", "upgrade", "repo"}
        for argv, _timeout in runner.calls
        for argument in argv
    )


def test_capture_preserves_a_proven_new_helm_release_without_raw_output(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    results = _existing_results()[:4] + [
        ClusterOperationSnapshotCommandResult(1, b"", b"Error: release: not found"),
        ClusterOperationSnapshotCommandResult(1, b"", b"Error: release: not found"),
    ]
    runner = FakeReadOnlyRunner(_expected_argv(fixture), results, [])

    snapshot = _capture(fixture, runner)

    runner.assert_complete()
    assert snapshot.release_fingerprints[0].pre_snapshot_state == "new"
    assert snapshot.release_fingerprints[0].pre_snapshot_revision is None
    assert snapshot.release_rollback_snapshots[0].pre_snapshot_state == "new"
    assert "not found" not in snapshot.model_dump_json()


def test_capture_rejects_secret_data_output_without_retaining_it(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    results = _existing_results()
    raw_secret = b'{"data":{"password":"TOKEN_MATERIAL_DO_NOT_PERSIST"}}'
    results[3] = ClusterOperationSnapshotCommandResult(0, raw_secret)
    runner = FakeReadOnlyRunner(_expected_argv(fixture), results, [])

    with pytest.raises(ClusterOperationSnapshotError) as exc_info:
        _capture(fixture, runner)

    assert "TOKEN_MATERIAL_DO_NOT_PERSIST" not in str(exc_info.value)
    assert len(runner.calls) == 4


def test_capture_rejects_binary_hash_mismatch_before_any_command(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.kubectl.write_bytes(b"replacement")
    runner = FakeReadOnlyRunner([], [], [])

    with pytest.raises(ClusterOperationSnapshotError, match="kubectl binary digest"):
        _capture(fixture, runner)

    runner.assert_complete()


def test_capture_rejects_static_manifest_drift_before_any_command(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    raw = yaml.safe_load(fixture.operation.manifest_path.read_text(encoding="utf-8"))
    raw["operation_id"] = "drifted-operation"
    fixture.operation.manifest_path.write_text(
        yaml.safe_dump(raw, sort_keys=False), encoding="utf-8"
    )
    runner = FakeReadOnlyRunner([], [], [])

    with pytest.raises(ClusterOperationSnapshotError, match="static validation"):
        _capture(fixture, runner)

    runner.assert_complete()


def test_capture_rejects_source_drift_during_read_only_collection(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    runner = FakeReadOnlyRunner(
        _expected_argv(fixture), _existing_results(), [], mutate_source=fixture.values_file
    )

    with pytest.raises(ClusterOperationSnapshotError, match="source files changed"):
        _capture(fixture, runner)

    runner.assert_complete()


def test_capture_snapshot_attaches_only_from_static_validated_and_expires(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    snapshot = _capture(
        fixture, FakeReadOnlyRunner(_expected_argv(fixture), _existing_results(), [])
    )
    record = new_cluster_operation_lifecycle_record(
        fixture.operation,
        run_id="fixture-run",
        source_revision=_SOURCE_REVISION,
        plan_digest="b" * 64,
        config_digest=fixture.config.config_digest,
        max_snapshot_age_seconds=900,
        now=_now(),
    )
    validated = transition_cluster_operation(
        record, ClusterOperationStatus.STATIC_VALIDATED, now=_now()
    )
    captured = attach_cluster_operation_snapshot(validated, snapshot, now=_now())

    assert captured.status is ClusterOperationStatus.SNAPSHOT_CAPTURED
    with pytest.raises(ClusterOperationLifecycleError, match="snapshot has expired"):
        attach_cluster_operation_snapshot(
            validated,
            snapshot,
            now=snapshot.expires_at,
        )
