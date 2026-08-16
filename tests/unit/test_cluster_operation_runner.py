from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from helpers import (
    FixtureProject,
    config_values,
    create_fixture_project,
    valid_plan_values,
    write_config,
)

from dispatcher.cluster_operation_lifecycle import (
    ClusterOperationApproval,
    ClusterOperationApprovalSnapshot,
    ClusterOperationStatus,
    ClusterResourceFingerprint,
    NamedDigest,
    PortForwardOwnership,
    PortForwardOwnershipState,
    new_cluster_operation_lifecycle_record,
)
from dispatcher.cluster_operation_runner import (
    ClusterOperationCommandResult,
    ClusterOperationRunner,
    ClusterOperationRunnerError,
    PortForwardChild,
    PortForwardCleanupOutcome,
    PortForwardReadiness,
    PortForwardSpawnRequest,
    TlsDc8ProbeOutcome,
    TlsDc8ProbeRequest,
    TlsDc8ProbeResult,
    inspect_cluster_operation_recovery,
)
from dispatcher.cluster_operations import ClusterOperationManifest, ValidatedClusterOperation
from dispatcher.config import Config
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.state_store import StateStore
from dispatcher.workflow import TransitionEvent, new_run_record

_SOURCE_REVISION = "a" * 40


@dataclass
class FakeRunner:
    expected_argv: list[tuple[str, ...]]
    results: list[ClusterOperationCommandResult]
    calls: list[tuple[tuple[str, ...], int]]

    def __call__(self, argv: tuple[str, ...], timeout_seconds: int) -> ClusterOperationCommandResult:
        self.calls.append((argv, timeout_seconds))
        assert self.expected_argv.pop(0) == argv
        return self.results.pop(0)

    def assert_complete(self) -> None:
        assert self.expected_argv == []
        assert self.results == []


@dataclass
class FakePortForwardProcessAdapter:
    expected_argv: tuple[str, ...]
    readiness: PortForwardReadiness = PortForwardReadiness.READY
    cleanup: PortForwardCleanupOutcome = PortForwardCleanupOutcome.CLOSED
    spawn_error: bool = False
    calls: list[str] | None = None
    ownership: object | None = None

    def __post_init__(self) -> None:
        self.calls = [] if self.calls is None else self.calls

    def spawn(self, request: PortForwardSpawnRequest) -> PortForwardChild:
        assert request.argv == self.expected_argv
        assert request.action.startup_timeout_seconds == 10
        assert self.calls is not None
        self.calls.append("spawn")
        if self.spawn_error:
            raise RuntimeError("fake spawn failure")
        return PortForwardChild(
            pid=4242,
            process_created_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
            argv_sha256=hashlib.sha256("\0".join(request.argv).encode("utf-8")).hexdigest(),
        )

    def wait_ready(self, ownership: PortForwardOwnership, timeout_seconds: int) -> PortForwardReadiness:
        assert timeout_seconds == 10
        assert ownership.pid == 4242
        assert ownership.bind_address == "127.0.0.1"
        assert self.calls is not None
        self.calls.append("ready")
        self.ownership = ownership
        return self.readiness

    def close_owned(
        self, ownership: PortForwardOwnership, timeout_seconds: int
    ) -> PortForwardCleanupOutcome:
        assert timeout_seconds == 30
        assert ownership.pid == 4242
        assert ownership.argv_sha256 == hashlib.sha256(
            "\0".join(self.expected_argv).encode("utf-8")
        ).hexdigest()
        assert self.calls is not None
        self.calls.append("cleanup")
        self.ownership = ownership
        return self.cleanup


@dataclass
class FakeTlsDc8ProbeAdapter:
    result: TlsDc8ProbeResult | None = None
    raise_ambiguous: bool = False
    requests: list[TlsDc8ProbeRequest] | None = None

    def __post_init__(self) -> None:
        self.requests = [] if self.requests is None else self.requests

    def probe_no_client_certificate(self, request: TlsDc8ProbeRequest) -> TlsDc8ProbeResult:
        assert request.bind_address == "127.0.0.1"
        assert request.local_port == 18080
        assert request.timeout_seconds == 10
        assert self.requests is not None
        self.requests.append(request)
        if self.raise_ambiguous:
            raise RuntimeError("fake ambiguous TLS result")
        assert self.result is not None
        return self.result


@dataclass(frozen=True)
class RunnerFixture:
    config: Config
    store: StateStore
    operation: ValidatedClusterOperation
    record_run_id: str
    kubectl: Path
    helm: Path
    manifest_file: Path
    chart_path: Path
    values_file: Path


def _now() -> datetime:
    return datetime.now(UTC)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configured_project(project: FixtureProject) -> tuple[Config, Path, Path]:
    tools = project.root / "mutation-tools"
    tools.mkdir()
    kubectl = tools / "kubectl"
    helm = tools / "helm"
    kubectl.write_bytes(b"fake-kubectl")
    helm.write_bytes(b"fake-helm")
    kubectl.chmod(0o700)
    helm.chmod(0o700)
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
        "auth_checks": [
            {"verb": "get", "resource": "deployments.apps", "namespace": "platform"}
        ],
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
    return write_config(project, values), kubectl, helm


def _operation(project: FixtureProject, actions: tuple[str, ...]) -> tuple[ValidatedClusterOperation, Path, Path, Path]:
    manifest_file = project.repository / "deploy/manifests/sample-app.yaml"
    chart_path = project.repository / "deploy/charts/sample-app"
    values_file = project.repository / "deploy/values/sample-app.yaml"
    for path in (manifest_file.parent, chart_path, values_file.parent):
        path.mkdir(parents=True, exist_ok=True)
    manifest_file.write_text("apiVersion: apps/v1\nkind: Deployment\n", encoding="utf-8")
    (chart_path / "Chart.lock").write_text("dependencies: []\n", encoding="utf-8")
    values_file.write_text("replicaCount: 1\n", encoding="utf-8")
    deployment = {
        "api_version": "apps/v1",
        "kind": "Deployment",
        "namespace": "platform",
        "name": "sample-app",
    }
    service = {
        "api_version": "v1",
        "kind": "Service",
        "namespace": "platform",
        "name": "sample-app",
    }
    action_values: list[dict[str, object]] = []
    if "dry_run" in actions:
        action_values.append(
            {
                "action": "kubectl_server_dry_run",
                "namespace": "platform",
                "timeout_seconds": 30,
                "expected_resources": [deployment],
                "readiness_probes": [{"probe": "deployment_available", "resource": deployment}],
                "manifest_files": [
                    {"path": "deploy/manifests/sample-app.yaml", "sha256": "approval_snapshot"}
                ],
            }
        )
    if "helm" in actions:
        action_values.append(
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
            }
        )
    if "port_forward" in actions:
        action_values.append(
            {
                "action": "port_forward",
                "action_id": "sample-app-forward",
                "namespace": "platform",
                "expected_resources": [service],
                "resource": service,
                "local_port": 18080,
                "remote_port": 8443,
                "startup_timeout_seconds": 10,
                "probe_timeout_seconds": 10,
                "lifetime_timeout_seconds": 30,
            }
        )
        action_values.append(
            {
                "action": "tls_dc8_no_client_certificate_rejection",
                "port_forward_action_id": "sample-app-forward",
            }
        )
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
            "actions": action_values,
            "rollback": {"automatic": True, "strategy": "restore_approval_snapshot"},
        }
    )
    return (
        ValidatedClusterOperation(
            step_id="prepare-fixture",
            target_name="fixture-target",
            repository_root=project.repository,
            manifest_path=project.repository / "deploy/operations/sample-app.yaml",
            manifest=manifest,
        ),
        manifest_file,
        chart_path,
        values_file,
    )


def _store_with_run(project: FixtureProject, config: Config) -> StateStore:
    plan = NormalizedPlan.model_validate(valid_plan_values(project))
    workflow_record = new_run_record(
        run_id="fixture-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
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
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    store.create_run(workflow_record)
    return store


def _fixture(
    tmp_path: Path,
    *,
    actions: tuple[str, ...],
    release_state: str = "new",
) -> RunnerFixture:
    project = create_fixture_project(tmp_path)
    config, kubectl, helm = _configured_project(project)
    operation, manifest_file, chart_path, values_file = _operation(project, actions)
    store = _store_with_run(project, config)
    now = _now()
    record = new_cluster_operation_lifecycle_record(
        operation,
        run_id="fixture-run",
        source_revision=_SOURCE_REVISION,
        plan_digest="b" * 64,
        config_digest=config.config_digest,
        max_snapshot_age_seconds=900,
        now=now,
    )
    store.create_cluster_operation(record)
    record = store.transition_cluster_operation(
        run_id=record.run_id,
        operation_id=record.operation_id,
        source_revision=record.source_revision,
        expected_generation=record.generation,
        target=ClusterOperationStatus.STATIC_VALIDATED,
        now=now,
    )
    source_files = tuple(
        {"path": str(path.relative_to(project.repository)), "sha256": _digest(path)}
        for path in sorted((chart_path / "Chart.lock", manifest_file, values_file))
    )
    rollback_snapshots = (
        ({"namespace": "platform", "release": "sample-app", "pre_snapshot_state": release_state}
        if release_state == "new"
        else {
            "namespace": "platform",
            "release": "sample-app",
            "pre_snapshot_state": "existing",
            "pre_snapshot_revision": 7,
        }),
    ) if "helm" in actions else ()
    snapshot = ClusterOperationApprovalSnapshot(
        run_id=record.run_id,
        step_id=record.step_id,
        operation_id=record.operation_id,
        source_revision=record.source_revision,
        plan_digest=record.plan_digest,
        config_digest=record.config_digest,
        validated_manifest_digest=record.validated_manifest_digest,
        cluster_preflight_result_digest="d" * 64,
        binary_identity_digests=(NamedDigest(name="helm", sha256="e" * 64),),
        toolchain_identity_digests=(
            NamedDigest(name="helm", sha256=_digest(helm)),
            NamedDigest(name="kubectl", sha256=_digest(kubectl)),
        ),
        tier1_invariant_snapshot_digest="f" * 64,
        action_digests=record.static_action_digests,
        source_file_digests=source_files,
        resource_fingerprints=tuple(
            ClusterResourceFingerprint(resource=resource, sha256=f"{index:x}" * 64)
            for index, resource in enumerate(
                sorted(
                    {
                        (
                            item.api_version,
                            item.kind,
                            item.namespace,
                            item.name,
                        ): item
                        for action in operation.manifest.actions
                        for item in getattr(action, "expected_resources", ())
                    }.values(),
                    key=lambda item: (item.api_version, item.kind, item.namespace, item.name),
                ),
                start=1,
            )
        ),
        release_fingerprints=(),
        release_rollback_snapshots=rollback_snapshots,
        image_fingerprints=(),
        secret_metadata_fingerprints=(),
        captured_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    record = store.attach_cluster_operation_snapshot(
        run_id=record.run_id,
        operation_id=record.operation_id,
        source_revision=record.source_revision,
        expected_generation=record.generation,
        snapshot=snapshot,
        now=now,
    )
    approval = ClusterOperationApproval(
        owner_ref="release-owner",
        run_id=record.run_id,
        step_id=record.step_id,
        operation_id=record.operation_id,
        source_revision=record.source_revision,
        snapshot_digest=snapshot.digest,
        allowed_actions=record.static_action_digests,
        rollback_intent=record.rollback_intent,
        rollback_digest=record.rollback_digest,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    record = store.attach_cluster_operation_approval(
        run_id=record.run_id,
        operation_id=record.operation_id,
        source_revision=record.source_revision,
        expected_generation=record.generation,
        approval=approval,
        now=now,
    )
    return RunnerFixture(
        config=config,
        store=store,
        operation=operation,
        record_run_id=record.run_id,
        kubectl=kubectl,
        helm=helm,
        manifest_file=manifest_file,
        chart_path=chart_path,
        values_file=values_file,
    )


def _run(
    fixture: RunnerFixture,
    fake: FakeRunner,
    *,
    process_adapter: FakePortForwardProcessAdapter | None = None,
    probe_adapter: FakeTlsDc8ProbeAdapter | None = None,
):
    return ClusterOperationRunner(
        config=fixture.config,
        state_store=fixture.store,
        command_runner=fake,
        port_forward_process_adapter=process_adapter,
        tls_dc8_probe_adapter=probe_adapter,
    ).execute(
        operation=fixture.operation,
        run_id=fixture.record_run_id,
        source_revision=_SOURCE_REVISION,
    )


def _dry_argv(fixture: RunnerFixture) -> tuple[str, ...]:
    return (
        str(fixture.kubectl),
        "--context",
        "fixture-context",
        "--namespace",
        "platform",
        "apply",
        "--server-side",
        "--dry-run=server",
        "--field-manager=dispatcher-cluster-operation",
        "--filename",
        str(fixture.manifest_file),
    )


def _helm_argv(fixture: RunnerFixture) -> tuple[str, ...]:
    return (
        str(fixture.helm),
        "upgrade",
        "--install",
        "sample-app",
        str(fixture.chart_path),
        "--namespace",
        "platform",
        "--kube-context",
        "fixture-context",
        "--wait",
        "--rollback-on-failure",
        "--timeout=60s",
        "--values",
        str(fixture.values_file),
    )


def _probe_argv(fixture: RunnerFixture) -> tuple[str, ...]:
    return (
        str(fixture.kubectl),
        "--context",
        "fixture-context",
        "--namespace",
        "platform",
        "rollout",
        "status",
        "deployment/sample-app",
        "--timeout=60s",
    )


def _port_forward_argv(fixture: RunnerFixture) -> tuple[str, ...]:
    return (
        str(fixture.kubectl),
        "--context",
        "fixture-context",
        "--namespace",
        "platform",
        "port-forward",
        "--address",
        "127.0.0.1",
        "service/sample-app",
        "18080:8443",
    )


def test_dry_run_uses_only_exact_approved_argv_and_records_digest_only_evidence(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, actions=("dry_run",))
    fake = FakeRunner(
        expected_argv=[_dry_argv(fixture)],
        results=[ClusterOperationCommandResult(returncode=0, stdout=b"applied")],
        calls=[],
    )

    result = _run(fixture, fake)

    fake.assert_complete()
    assert result.status is ClusterOperationStatus.SERVER_DRY_RUN_PASSED
    evidence = result.record.command_evidence[0]
    assert evidence.stdout_sha256 == hashlib.sha256(b"applied").hexdigest()
    assert evidence.stderr_sha256 == hashlib.sha256(b"").hexdigest()
    assert all("--force-conflicts" not in argv and "--set" not in argv for argv, _timeout in fake.calls)


def test_tool_hash_mismatch_stops_before_any_fake_kubernetes_command(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, actions=("dry_run",))
    fixture.kubectl.write_bytes(b"replaced-kubectl")
    fake = FakeRunner(expected_argv=[], results=[], calls=[])

    with pytest.raises(ClusterOperationRunnerError, match="kubectl checksum mismatch"):
        _run(fixture, fake)

    fake.assert_complete()


def test_helm_rejects_an_unapproved_chart_file_before_any_fake_command(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, actions=("dry_run", "helm"))
    (fixture.chart_path / "Chart.yaml").write_text("apiVersion: v2\n", encoding="utf-8")
    fake = FakeRunner(expected_argv=[], results=[], calls=[])

    with pytest.raises(ClusterOperationRunnerError, match="unapproved source file"):
        _run(fixture, fake)

    fake.assert_complete()


def test_helm_success_runs_typed_probe_with_exact_allowlisted_argv(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, actions=("dry_run", "helm"))
    fake = FakeRunner(
        expected_argv=[_dry_argv(fixture), _helm_argv(fixture), _probe_argv(fixture)],
        results=[
            ClusterOperationCommandResult(returncode=0, stdout=b"dry"),
            ClusterOperationCommandResult(returncode=0, stdout=b"upgrade"),
            ClusterOperationCommandResult(returncode=0, stdout=b"ready"),
        ],
        calls=[],
    )

    result = _run(fixture, fake)

    fake.assert_complete()
    assert result.status is ClusterOperationStatus.SUCCEEDED
    assert [item.kind for item in result.record.command_evidence] == [
        "server_dry_run",
        "mutation",
        "readiness_probe",
    ]


def test_mutation_failure_uninstalls_release_proven_new_in_reverse_rollback(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, actions=("dry_run", "helm"), release_state="new")
    uninstall = (
        str(fixture.helm),
        "uninstall",
        "sample-app",
        "--namespace",
        "platform",
        "--kube-context",
        "fixture-context",
        "--wait",
        "--timeout=60s",
    )
    fake = FakeRunner(
        expected_argv=[_dry_argv(fixture), _helm_argv(fixture), uninstall],
        results=[
            ClusterOperationCommandResult(returncode=0, stdout=b"dry"),
            ClusterOperationCommandResult(returncode=1, stdout=b"", stderr=b"failed"),
            ClusterOperationCommandResult(returncode=0, stdout=b"uninstalled"),
        ],
        calls=[],
    )

    result = _run(fixture, fake)

    fake.assert_complete()
    assert result.status is ClusterOperationStatus.ROLLED_BACK


def test_mutation_failure_restores_existing_release_recorded_revision(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, actions=("dry_run", "helm"), release_state="existing")
    rollback = (
        str(fixture.helm),
        "rollback",
        "sample-app",
        "7",
        "--namespace",
        "platform",
        "--kube-context",
        "fixture-context",
        "--wait",
        "--timeout=60s",
    )
    fake = FakeRunner(
        expected_argv=[_dry_argv(fixture), _helm_argv(fixture), rollback],
        results=[
            ClusterOperationCommandResult(returncode=0, stdout=b"dry"),
            ClusterOperationCommandResult(returncode=1, stdout=b"", stderr=b"failed"),
            ClusterOperationCommandResult(returncode=0, stdout=b"rolled back"),
        ],
        calls=[],
    )

    result = _run(fixture, fake)

    fake.assert_complete()
    assert result.status is ClusterOperationStatus.ROLLED_BACK


def test_rollback_failure_requires_reconciliation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, actions=("dry_run", "helm"))
    uninstall = (
        str(fixture.helm),
        "uninstall",
        "sample-app",
        "--namespace",
        "platform",
        "--kube-context",
        "fixture-context",
        "--wait",
        "--timeout=60s",
    )
    fake = FakeRunner(
        expected_argv=[_dry_argv(fixture), _helm_argv(fixture), uninstall],
        results=[
            ClusterOperationCommandResult(returncode=0, stdout=b"dry"),
            ClusterOperationCommandResult(returncode=1, stdout=b"", stderr=b"failed"),
            ClusterOperationCommandResult(returncode=1, stdout=b"", stderr=b"rollback failed"),
        ],
        calls=[],
    )

    result = _run(fixture, fake)

    fake.assert_complete()
    assert result.status is ClusterOperationStatus.RECONCILIATION_REQUIRED


@pytest.mark.parametrize(
    "crash_states",
    [
        (ClusterOperationStatus.SERVER_DRY_RUN_PASSED, ClusterOperationStatus.MUTATION_STARTED),
        (
            ClusterOperationStatus.SERVER_DRY_RUN_PASSED,
            ClusterOperationStatus.MUTATION_STARTED,
            ClusterOperationStatus.MUTATED,
            ClusterOperationStatus.PROBING,
        ),
        (
            ClusterOperationStatus.SERVER_DRY_RUN_PASSED,
            ClusterOperationStatus.MUTATION_STARTED,
            ClusterOperationStatus.ROLLBACK_STARTED,
        ),
    ],
)
def test_crash_boundary_returns_reconciliation_without_reapplying(
    tmp_path: Path,
    crash_states: tuple[ClusterOperationStatus, ...],
) -> None:
    fixture = _fixture(tmp_path, actions=("dry_run", "helm"))
    record = fixture.store.load_cluster_operation(
        run_id=fixture.record_run_id,
        operation_id=fixture.operation.manifest.operation_id,
        source_revision=_SOURCE_REVISION,
    )
    for target in crash_states:
        record = fixture.store.transition_cluster_operation(
            run_id=record.run_id,
            operation_id=record.operation_id,
            source_revision=record.source_revision,
            expected_generation=record.generation,
            target=target,
            now=_now(),
        )
    fake = FakeRunner(expected_argv=[], results=[], calls=[])

    result = _run(fixture, fake)

    fake.assert_complete()
    assert inspect_cluster_operation_recovery(record) is ClusterOperationStatus.RECONCILIATION_REQUIRED
    assert inspect_cluster_operation_recovery(object()) is ClusterOperationStatus.RECONCILIATION_REQUIRED
    assert result.status is ClusterOperationStatus.RECONCILIATION_REQUIRED
    assert result.record.status is crash_states[-1]


def test_port_forward_uses_exact_argv_and_persists_owned_identity_before_tls_rejection_passes(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, actions=("port_forward",))
    fake = FakeRunner(expected_argv=[], results=[], calls=[])
    process = FakePortForwardProcessAdapter(expected_argv=_port_forward_argv(fixture))
    probe = FakeTlsDc8ProbeAdapter(
        result=TlsDc8ProbeResult(
            outcome=TlsDc8ProbeOutcome.CLIENT_CERTIFICATE_REQUIRED,
            evidence_sha256="a" * 64,
        )
    )

    result = _run(fixture, fake, process_adapter=process, probe_adapter=probe)

    fake.assert_complete()
    assert result.status is ClusterOperationStatus.SUCCEEDED
    assert process.calls == ["spawn", "ready", "cleanup"]
    assert probe.requests == [
        TlsDc8ProbeRequest(
            port_forward_action_id="sample-app-forward",
            bind_address="127.0.0.1",
            local_port=18080,
            timeout_seconds=10,
        )
    ]
    ownership = result.record.port_forwards[0]
    assert ownership.state.value == "STOPPED"
    assert ownership.pid == 4242
    assert ownership.context == "fixture-context"
    assert ownership.resource.kind == "Service"
    assert ownership.argv_sha256 == hashlib.sha256(
        "\0".join(_port_forward_argv(fixture)).encode("utf-8")
    ).hexdigest()
    assert [item.outcome for item in result.record.tls_dc8_probe_evidence] == [
        "client_certificate_required"
    ]
    fixture.store.close()
    restored = StateStore(
        fixture.config.state_dir,
        heartbeat_seconds=fixture.config.lease_heartbeat_seconds,
        stale_after_seconds=fixture.config.lease_stale_after_seconds,
    )
    persisted = restored.load_cluster_operation(
        run_id=fixture.record_run_id,
        operation_id=fixture.operation.manifest.operation_id,
        source_revision=_SOURCE_REVISION,
    )
    assert persisted.port_forwards == result.record.port_forwards
    assert persisted.tls_dc8_probe_evidence == result.record.tls_dc8_probe_evidence


@pytest.mark.parametrize(
    "outcome",
    [
        TlsDc8ProbeOutcome.UNAUTHENTICATED_HANDSHAKE_SUCCEEDED,
        TlsDc8ProbeOutcome.UNEXPECTED_LISTENER_BEHAVIOR,
        TlsDc8ProbeOutcome.TIMEOUT,
    ],
)
def test_port_forward_definitive_tls_failures_close_owned_child_then_fail(
    tmp_path: Path,
    outcome: TlsDc8ProbeOutcome,
) -> None:
    fixture = _fixture(tmp_path, actions=("port_forward",))
    fake = FakeRunner(expected_argv=[], results=[], calls=[])
    process = FakePortForwardProcessAdapter(expected_argv=_port_forward_argv(fixture))
    probe = FakeTlsDc8ProbeAdapter(result=TlsDc8ProbeResult(outcome=outcome, evidence_sha256="b" * 64))

    result = _run(fixture, fake, process_adapter=process, probe_adapter=probe)

    fake.assert_complete()
    assert result.status is ClusterOperationStatus.FAILED
    assert process.calls == ["spawn", "ready", "cleanup"]
    assert result.record.port_forwards[0].state.value == "STOPPED"
    assert result.record.tls_dc8_probe_evidence[0].outcome == outcome.value


def test_port_forward_readiness_timeout_closes_owned_child_without_a_tls_connection(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, actions=("port_forward",))
    fake = FakeRunner(expected_argv=[], results=[], calls=[])
    process = FakePortForwardProcessAdapter(
        expected_argv=_port_forward_argv(fixture), readiness=PortForwardReadiness.TIMEOUT
    )
    probe = FakeTlsDc8ProbeAdapter(
        result=TlsDc8ProbeResult(
            outcome=TlsDc8ProbeOutcome.CLIENT_CERTIFICATE_REQUIRED,
            evidence_sha256="f" * 64,
        )
    )

    result = _run(fixture, fake, process_adapter=process, probe_adapter=probe)

    fake.assert_complete()
    assert result.status is ClusterOperationStatus.FAILED
    assert process.calls == ["spawn", "ready", "cleanup"]
    assert probe.requests == []
    assert result.record.port_forwards[0].state.value == "STOPPED"


def test_port_forward_tls_failure_rolls_back_prior_mutation_after_owned_cleanup(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, actions=("dry_run", "helm", "port_forward"), release_state="new")
    uninstall = (
        str(fixture.helm),
        "uninstall",
        "sample-app",
        "--namespace",
        "platform",
        "--kube-context",
        "fixture-context",
        "--wait",
        "--timeout=60s",
    )
    fake = FakeRunner(
        expected_argv=[_dry_argv(fixture), _helm_argv(fixture), _probe_argv(fixture), uninstall],
        results=[
            ClusterOperationCommandResult(returncode=0, stdout=b"dry"),
            ClusterOperationCommandResult(returncode=0, stdout=b"upgrade"),
            ClusterOperationCommandResult(returncode=0, stdout=b"ready"),
            ClusterOperationCommandResult(returncode=0, stdout=b"uninstalled"),
        ],
        calls=[],
    )
    process = FakePortForwardProcessAdapter(expected_argv=_port_forward_argv(fixture))
    probe = FakeTlsDc8ProbeAdapter(
        result=TlsDc8ProbeResult(
            outcome=TlsDc8ProbeOutcome.UNAUTHENTICATED_HANDSHAKE_SUCCEEDED,
            evidence_sha256="0" * 64,
        )
    )

    result = _run(fixture, fake, process_adapter=process, probe_adapter=probe)

    fake.assert_complete()
    assert result.status is ClusterOperationStatus.ROLLED_BACK
    assert process.calls == ["spawn", "ready", "cleanup"]
    assert result.record.port_forwards[0].state.value == "STOPPED"


def test_port_forward_pid_reuse_never_kills_or_adopts_the_child_and_requires_reconciliation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, actions=("port_forward",))
    fake = FakeRunner(expected_argv=[], results=[], calls=[])
    process = FakePortForwardProcessAdapter(
        expected_argv=_port_forward_argv(fixture), cleanup=PortForwardCleanupOutcome.PID_REUSED
    )
    probe = FakeTlsDc8ProbeAdapter(
        result=TlsDc8ProbeResult(
            outcome=TlsDc8ProbeOutcome.UNAUTHENTICATED_LISTENER_REJECTED,
            evidence_sha256="c" * 64,
        )
    )

    result = _run(fixture, fake, process_adapter=process, probe_adapter=probe)

    fake.assert_complete()
    assert result.status is ClusterOperationStatus.RECONCILIATION_REQUIRED
    assert process.calls == ["spawn", "ready", "cleanup"]
    assert result.record.port_forwards[0].state.value == "CLEANUP_AMBIGUOUS"


def test_port_forward_identity_persistence_failure_reconciles_without_signalling_unjournaled_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, actions=("port_forward",))
    fake = FakeRunner(expected_argv=[], results=[], calls=[])
    process = FakePortForwardProcessAdapter(expected_argv=_port_forward_argv(fixture))
    probe = FakeTlsDc8ProbeAdapter(
        result=TlsDc8ProbeResult(
            outcome=TlsDc8ProbeOutcome.CLIENT_CERTIFICATE_REQUIRED,
            evidence_sha256="e" * 64,
        )
    )

    def fail_identity_persistence(**_kwargs: object) -> object:
        raise RuntimeError("simulated journal write failure")

    monkeypatch.setattr(
        fixture.store, "persist_cluster_operation_port_forward_started", fail_identity_persistence
    )

    result = _run(fixture, fake, process_adapter=process, probe_adapter=probe)

    fake.assert_complete()
    assert result.status is ClusterOperationStatus.RECONCILIATION_REQUIRED
    assert process.calls == ["spawn"]
    assert probe.requests == []
    assert result.record.port_forwards[0].state.value == "INTENT_PERSISTED"


def test_port_forward_tls_probe_ambiguity_reconciles_after_verified_cleanup(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, actions=("port_forward",))
    fake = FakeRunner(expected_argv=[], results=[], calls=[])
    process = FakePortForwardProcessAdapter(expected_argv=_port_forward_argv(fixture))
    probe = FakeTlsDc8ProbeAdapter(raise_ambiguous=True)

    result = _run(fixture, fake, process_adapter=process, probe_adapter=probe)

    fake.assert_complete()
    assert result.status is ClusterOperationStatus.RECONCILIATION_REQUIRED
    assert process.calls == ["spawn", "ready", "cleanup"]
    assert result.record.port_forwards[0].state.value == "STOPPED"
    assert result.record.tls_dc8_probe_evidence[0].outcome == "ambiguous"


def test_port_forward_crash_recovery_transitions_to_reconciliation_without_adapter_calls(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, actions=("port_forward",))
    record = fixture.store.load_cluster_operation(
        run_id=fixture.record_run_id,
        operation_id=fixture.operation.manifest.operation_id,
        source_revision=_SOURCE_REVISION,
    )
    ownership = PortForwardOwnership.model_validate({
        "action_id": "sample-app-forward",
        "context": "fixture-context",
        "resource": {"api_version": "v1", "kind": "Service", "namespace": "platform", "name": "sample-app"},
        "bind_address": "127.0.0.1",
        "local_port": 18080,
        "remote_port": 8443,
        "argv_sha256": hashlib.sha256("\0".join(_port_forward_argv(fixture)).encode("utf-8")).hexdigest(),
        "state": PortForwardOwnershipState.INTENT_PERSISTED,
        "intent_at": _now(),
    })
    record = fixture.store.persist_cluster_operation_port_forward_intent(
        run_id=record.run_id,
        operation_id=record.operation_id,
        source_revision=record.source_revision,
        expected_generation=record.generation,
        ownership=ownership,
        now=ownership.intent_at,
    )
    record = fixture.store.persist_cluster_operation_port_forward_started(
        run_id=record.run_id,
        operation_id=record.operation_id,
        source_revision=record.source_revision,
        expected_generation=record.generation,
        action_id="sample-app-forward",
        pid=4242,
        process_created_at=_now(),
        now=_now(),
    )
    fake = FakeRunner(expected_argv=[], results=[], calls=[])
    process = FakePortForwardProcessAdapter(expected_argv=_port_forward_argv(fixture))
    probe = FakeTlsDc8ProbeAdapter(
        result=TlsDc8ProbeResult(
            outcome=TlsDc8ProbeOutcome.CLIENT_CERTIFICATE_REQUIRED,
            evidence_sha256="d" * 64,
        )
    )

    result = _run(fixture, fake, process_adapter=process, probe_adapter=probe)

    fake.assert_complete()
    assert result.status is ClusterOperationStatus.RECONCILIATION_REQUIRED
    assert process.calls == []
    assert probe.requests == []
    assert result.record.status is ClusterOperationStatus.RECONCILIATION_REQUIRED
