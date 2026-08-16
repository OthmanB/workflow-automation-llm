"""Dispatcher-only fixed-argv execution for approved cluster operations.

This module is intentionally not connected to the dispatcher CLI or worker
workflow. Callers must inject the command runner; no production subprocess
fallback exists here.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

from .cluster_operation_lifecycle import (
    ActionDigest,
    ClusterOperationCommandEvidence,
    ClusterOperationLifecycleRecord,
    ClusterOperationStatus,
    HelmReleaseRollbackSnapshot,
    PortForwardOwnership,
    PortForwardOwnershipState,
    TlsDc8ProbeEvidence,
)
from .cluster_operations import (
    ClusterAction,
    HelmUpgradeInstallAction,
    KubectlServerDryRunAction,
    PortForwardAction,
    ReadinessProbe,
    TlsDc8NoClientCertificateProbeAction,
    ValidatedClusterOperation,
)
from .config import ClusterMutationTargetDefinition, Config
from .state_store import StateStore

_MAX_COMMAND_OUTPUT_BYTES = 65_536
_AMBIGUOUS_RECOVERY_STATES = frozenset(
    {
        ClusterOperationStatus.MUTATION_STARTED,
        ClusterOperationStatus.PROBING,
        ClusterOperationStatus.PORT_FORWARD_INTENT,
        ClusterOperationStatus.PORT_FORWARD_STARTED,
        ClusterOperationStatus.TLS_DC8_PROBING,
        ClusterOperationStatus.ROLLBACK_STARTED,
    }
)


class ClusterOperationRunnerError(RuntimeError):
    """A fixed dispatcher-owned operation cannot be safely launched or completed."""


@dataclass(frozen=True)
class ClusterOperationCommandResult:
    """The bounded result returned by an injected fixed-argv command runner."""

    returncode: int
    stdout: bytes
    stderr: bytes = b""


ClusterOperationCommandRunner = Callable[[tuple[str, ...], int], ClusterOperationCommandResult]


@dataclass(frozen=True)
class PortForwardChild:
    """Identity returned by the injected dispatcher-owned process adapter after spawn."""

    pid: int
    process_created_at: datetime
    argv_sha256: str


@dataclass(frozen=True)
class PortForwardSpawnRequest:
    """The runner-owned, typed input for one exact port-forward child process."""

    argv: tuple[str, ...]
    target: ClusterMutationTargetDefinition
    action: PortForwardAction


class PortForwardReadiness(str, Enum):
    """Bounded, stream-free readiness result for a spawned port-forward child."""

    READY = "ready"
    TIMEOUT = "timeout"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


class PortForwardCleanupOutcome(str, Enum):
    """Result of a process adapter attempting to close a durable owned child."""

    CLOSED = "closed"
    PID_REUSED = "pid_reused"
    UNOWNED = "unowned"
    TIMEOUT = "timeout"
    FAILED = "failed"


class TlsDc8ProbeOutcome(str, Enum):
    """The finite outcomes permitted for the fixed no-client-certificate TLS check."""

    CLIENT_CERTIFICATE_REQUIRED = "client_certificate_required"
    UNAUTHENTICATED_LISTENER_REJECTED = "unauthenticated_listener_rejected"
    UNAUTHENTICATED_HANDSHAKE_SUCCEEDED = "unauthenticated_handshake_succeeded"
    UNEXPECTED_LISTENER_BEHAVIOR = "unexpected_listener_behavior"
    TIMEOUT = "timeout"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class TlsDc8ProbeRequest:
    """Fixed loopback connection parameters with no client credential or payload surface."""

    port_forward_action_id: str
    bind_address: Literal["127.0.0.1"]
    local_port: int
    timeout_seconds: int


@dataclass(frozen=True)
class TlsDc8ProbeResult:
    """Digest-only result from a direct TLS handshake with no client certificate."""

    outcome: TlsDc8ProbeOutcome
    evidence_sha256: str


class PortForwardProcessAdapter(Protocol):
    """Dispatcher-only process seam; implementations must never expose child streams."""

    def spawn(self, request: PortForwardSpawnRequest) -> PortForwardChild:
        """Spawn the runner-generated exact child with bounded typed timeouts."""

    def wait_ready(
        self, ownership: PortForwardOwnership, timeout_seconds: int
    ) -> PortForwardReadiness:
        """Wait for a bounded readiness signal without returning process output."""

    def close_owned(
        self, ownership: PortForwardOwnership, timeout_seconds: int
    ) -> PortForwardCleanupOutcome:
        """Signal only when PID, creation time, and argv digest still match the owned child."""


class TlsDc8ProbeAdapter(Protocol):
    """Dispatcher-only seam for exactly one direct no-client-certificate TLS handshake."""

    def probe_no_client_certificate(self, request: TlsDc8ProbeRequest) -> TlsDc8ProbeResult:
        """Probe the fixed loopback listener without a client certificate or payload."""


@dataclass(frozen=True)
class ClusterOperationExecutionResult:
    """The latest durable record plus its execution or recovery disposition."""

    record: ClusterOperationLifecycleRecord
    status: ClusterOperationStatus


def inspect_cluster_operation_recovery(record: object) -> ClusterOperationStatus:
    """Return reconciliation for every state from which command outcome is ambiguous."""
    status = getattr(record, "status", None)
    if not isinstance(status, ClusterOperationStatus) or status in _AMBIGUOUS_RECOVERY_STATES:
        return ClusterOperationStatus.RECONCILIATION_REQUIRED
    return status


class ClusterOperationRunner:
    """Run one approval-bound operation through a fake- or owner-supplied runner only."""

    def __init__(
        self,
        *,
        config: Config,
        state_store: StateStore,
        command_runner: ClusterOperationCommandRunner,
        port_forward_process_adapter: PortForwardProcessAdapter | None = None,
        tls_dc8_probe_adapter: TlsDc8ProbeAdapter | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._state_store = state_store
        self._command_runner = command_runner
        self._port_forward_process_adapter = port_forward_process_adapter
        self._tls_dc8_probe_adapter = tls_dc8_probe_adapter
        self._now = now or (lambda: datetime.now(UTC))

    def execute(
        self,
        *,
        operation: ValidatedClusterOperation,
        run_id: str,
        source_revision: str,
    ) -> ClusterOperationExecutionResult:
        """Launch an approved fixed-argv operation, or return a recovery disposition.

        A persisted mutation/probe/rollback boundary is never resumed automatically.
        """
        record = self._state_store.load_cluster_operation(
            run_id=run_id,
            operation_id=operation.manifest.operation_id,
            source_revision=source_revision,
        )
        return self._execute_loaded_record(operation, record)

    def execute_record(
        self,
        *,
        operation: ValidatedClusterOperation,
        record: ClusterOperationLifecycleRecord,
    ) -> ClusterOperationExecutionResult:
        """Execute an exact caller-loaded record after reloading it with its durable identity."""
        current = self._state_store.load_cluster_operation(
            run_id=record.run_id,
            operation_id=record.operation_id,
            source_revision=record.source_revision,
        )
        if current.identity != record.identity:
            raise ClusterOperationRunnerError("cluster operation identity changed before launch")
        return self._execute_loaded_record(operation, current)

    def _execute_loaded_record(
        self,
        operation: ValidatedClusterOperation,
        record: ClusterOperationLifecycleRecord,
    ) -> ClusterOperationExecutionResult:
        recovery_status = inspect_cluster_operation_recovery(record)
        if recovery_status is ClusterOperationStatus.RECONCILIATION_REQUIRED:
            if record.status in {
                ClusterOperationStatus.PORT_FORWARD_INTENT,
                ClusterOperationStatus.PORT_FORWARD_STARTED,
                ClusterOperationStatus.TLS_DC8_PROBING,
            }:
                return self._reconciliation_result(record)
            return ClusterOperationExecutionResult(record=record, status=recovery_status)
        if record.status in {
            ClusterOperationStatus.SUCCEEDED,
            ClusterOperationStatus.ROLLED_BACK,
            ClusterOperationStatus.FAILED,
        }:
            return ClusterOperationExecutionResult(record=record, status=record.status)
        if record.status not in {
            ClusterOperationStatus.APPROVED,
            ClusterOperationStatus.SERVER_DRY_RUN_PASSED,
        }:
            raise ClusterOperationRunnerError("cluster operation is not approved for dispatcher execution")

        target = self._validate_launch(operation, record)
        actions = self._approved_actions(operation, record)
        self._verify_toolchain(target)

        dry_runs: list[tuple[str, KubectlServerDryRunAction]] = [
            (action_id, action)
            for action_id, action in actions
            if isinstance(action, KubectlServerDryRunAction)
        ]
        mutations: list[tuple[str, HelmUpgradeInstallAction]] = [
            (action_id, action)
            for action_id, action in actions
            if isinstance(action, HelmUpgradeInstallAction)
        ]
        port_forward_pairs = self._port_forward_pairs(actions)
        if port_forward_pairs and (
            self._port_forward_process_adapter is None or self._tls_dc8_probe_adapter is None
        ):
            raise ClusterOperationRunnerError("port-forward/TLS execution requires dispatcher-owned adapters")
        if mutations and not dry_runs and record.status is ClusterOperationStatus.APPROVED:
            raise ClusterOperationRunnerError("mutation requires an approved server-side dry-run")
        if any(
            isinstance(action, KubectlServerDryRunAction)
            for _action_id, action in actions[len(dry_runs) :]
        ):
            raise ClusterOperationRunnerError("server-side dry-runs must precede Helm mutations")

        if record.status is ClusterOperationStatus.APPROVED:
            for action_id, dry_action in dry_runs:
                record, result = self._run_fixed_command(
                    record=record,
                    action_id=action_id,
                    command_id=action_id,
                    kind="server_dry_run",
                    argv=self._kubectl_server_dry_run_argv(target, operation, dry_action),
                    timeout_seconds=dry_action.timeout_seconds,
                )
                if result.returncode != 0:
                    record = self._transition(record, ClusterOperationStatus.FAILED)
                    return ClusterOperationExecutionResult(record=record, status=record.status)
            if dry_runs:
                record = self._transition(record, ClusterOperationStatus.SERVER_DRY_RUN_PASSED)

        if not mutations:
            if port_forward_pairs:
                return self._run_port_forward_pairs(
                    record=record,
                    target=target,
                    pairs=port_forward_pairs,
                    started_mutations=[],
                )
            return ClusterOperationExecutionResult(record=record, status=record.status)
        if record.status is not ClusterOperationStatus.SERVER_DRY_RUN_PASSED:
            raise ClusterOperationRunnerError("mutation requires SERVER_DRY_RUN_PASSED")

        # This durable write is intentionally immediately before the first mutation invocation.
        record = self._transition(record, ClusterOperationStatus.MUTATION_STARTED)
        started_mutations: list[tuple[str, HelmUpgradeInstallAction]] = []
        for action_id, mutation_action in mutations:
            started_mutations.append((action_id, mutation_action))
            try:
                record, result = self._run_fixed_command(
                    record=record,
                    action_id=action_id,
                    command_id=action_id,
                    kind="mutation",
                    argv=self._helm_upgrade_install_argv(target, operation, mutation_action),
                    timeout_seconds=mutation_action.timeout_seconds,
                )
            except ClusterOperationRunnerError:
                record = self._transition(record, ClusterOperationStatus.RECONCILIATION_REQUIRED)
                return ClusterOperationExecutionResult(record=record, status=record.status)
            if result.returncode != 0:
                return self._rollback(record, target, started_mutations)

        record = self._transition(record, ClusterOperationStatus.MUTATED)
        record = self._transition(record, ClusterOperationStatus.PROBING)
        for action_id, mutation_action in mutations:
            for probe_index, probe in enumerate(mutation_action.readiness_probes, start=1):
                try:
                    record, result = self._run_fixed_command(
                        record=record,
                        action_id=action_id,
                        command_id=f"{action_id}-probe-{probe_index}",
                        kind="readiness_probe",
                        argv=self._kubectl_rollout_status_argv(
                            target, probe, mutation_action.timeout_seconds
                        ),
                        timeout_seconds=mutation_action.timeout_seconds,
                    )
                except ClusterOperationRunnerError:
                    record = self._transition(record, ClusterOperationStatus.RECONCILIATION_REQUIRED)
                    return ClusterOperationExecutionResult(record=record, status=record.status)
                if result.returncode != 0:
                    return self._rollback(record, target, mutations)

        if port_forward_pairs:
            return self._run_port_forward_pairs(
                record=record,
                target=target,
                pairs=port_forward_pairs,
                started_mutations=mutations,
            )
        record = self._transition(record, ClusterOperationStatus.SUCCEEDED)
        return ClusterOperationExecutionResult(record=record, status=record.status)

    def _validate_launch(
        self,
        operation: ValidatedClusterOperation,
        record: ClusterOperationLifecycleRecord,
    ) -> ClusterMutationTargetDefinition:
        if self._config.cluster_mutation is None:
            raise ClusterOperationRunnerError("cluster mutation configuration is required")
        try:
            target = self._config.cluster_mutation.targets[operation.target_name]
        except KeyError as exc:
            raise ClusterOperationRunnerError("cluster mutation target is no longer configured") from exc
        if (
            record.target_name != operation.target_name
            or record.operation_id != operation.manifest.operation_id
            or record.step_id != operation.step_id
            or record.config_digest != self._config.config_digest
        ):
            raise ClusterOperationRunnerError("operation does not match the approved journal identity")
        if operation.manifest.context != target.context:
            raise ClusterOperationRunnerError("operation context does not match configured target")
        if operation.manifest.source_identity.repository_id not in target.allowed_repository_ids:
            raise ClusterOperationRunnerError("operation repository is not allowed by configured target")
        declared_files = {item.path for item in operation.manifest.allowed_files}
        for action in operation.manifest.actions:
            if isinstance(action, TlsDc8NoClientCertificateProbeAction):
                continue
            if isinstance(action, PortForwardAction):
                if max(
                    action.startup_timeout_seconds,
                    action.probe_timeout_seconds,
                    action.lifetime_timeout_seconds,
                ) > target.max_action_timeout_seconds:
                    raise ClusterOperationRunnerError(
                        "port-forward timeout exceeds configured target maximum"
                    )
                continue
            if action.timeout_seconds > target.max_action_timeout_seconds:
                raise ClusterOperationRunnerError("operation action timeout exceeds configured target maximum")
            if isinstance(action, KubectlServerDryRunAction):
                if any(item.path not in declared_files for item in action.manifest_files):
                    raise ClusterOperationRunnerError("dry-run files are not approved by the static manifest")
            if isinstance(action, HelmUpgradeInstallAction) and any(
                item.path not in declared_files for item in (action.chart_lock_file, *action.values_files)
            ):
                raise ClusterOperationRunnerError("Helm inputs are not approved by the static manifest")
        if _canonical_digest(operation.manifest.model_dump(mode="json")) != record.validated_manifest_digest:
            raise ClusterOperationRunnerError("operation manifest digest does not match approved journal")
        expected_actions = tuple(
            ActionDigest(action_id=f"action-{index}", sha256=_canonical_digest(action.model_dump(mode="json")))
            for index, action in enumerate(operation.manifest.actions, start=1)
        )
        if expected_actions != record.static_action_digests:
            raise ClusterOperationRunnerError("operation actions do not match approved journal")
        if record.snapshot is None or record.approval is None:
            raise ClusterOperationRunnerError("operation has no approved snapshot")

        toolchain_digests = {item.name: item.sha256 for item in record.snapshot.toolchain_identity_digests}
        expected_toolchain = {
            "kubectl": target.toolchain.kubectl.sha256,
            "helm": target.toolchain.helm.sha256,
        }
        if any(toolchain_digests.get(name) != digest for name, digest in expected_toolchain.items()):
            raise ClusterOperationRunnerError("approved snapshot does not match configured mutation toolchain")
        self._verify_approved_source_files(operation, record)

        helm_actions = [action for action in operation.manifest.actions if isinstance(action, HelmUpgradeInstallAction)]
        rollback_entries = {
            (item.namespace, item.release): item for item in record.snapshot.release_rollback_snapshots
        }
        expected_releases = {(action.namespace, action.release) for action in helm_actions}
        if len(expected_releases) != len(helm_actions) or set(rollback_entries) != expected_releases:
            raise ClusterOperationRunnerError("approved snapshot lacks exact Helm rollback state")
        return target

    def _approved_actions(
        self,
        operation: ValidatedClusterOperation,
        record: ClusterOperationLifecycleRecord,
    ) -> list[tuple[str, ClusterAction]]:
        assert record.approval is not None
        approved = {item.action_id: item.sha256 for item in record.approval.allowed_actions}
        actions: list[tuple[str, ClusterAction]] = []
        for index, action in enumerate(operation.manifest.actions, start=1):
            action_id = f"action-{index}"
            digest = _canonical_digest(action.model_dump(mode="json"))
            if action_id in approved:
                if approved[action_id] != digest:
                    raise ClusterOperationRunnerError("approval action digest does not match static action")
                actions.append((action_id, action))
            elif isinstance(action, (PortForwardAction, TlsDc8NoClientCertificateProbeAction)):
                raise ClusterOperationRunnerError("approval omits a required port-forward/TLS action")
        if not actions:
            raise ClusterOperationRunnerError("approval contains no launchable cluster action")
        return actions

    @staticmethod
    def _port_forward_pairs(
        actions: list[tuple[str, ClusterAction]],
    ) -> list[tuple[str, PortForwardAction, str, TlsDc8NoClientCertificateProbeAction]]:
        pairs: list[tuple[str, PortForwardAction, str, TlsDc8NoClientCertificateProbeAction]] = []
        for index, (action_id, action) in enumerate(actions):
            if not isinstance(action, PortForwardAction):
                continue
            try:
                probe_action_id, probe = actions[index + 1]
            except IndexError as exc:
                raise ClusterOperationRunnerError("port-forward has no linked TLS/DC8 probe") from exc
            if (
                not isinstance(probe, TlsDc8NoClientCertificateProbeAction)
                or probe.port_forward_action_id != action.action_id
            ):
                raise ClusterOperationRunnerError("port-forward is not followed by its linked TLS/DC8 probe")
            pairs.append((action_id, action, probe_action_id, probe))
        return pairs

    def _run_port_forward_pairs(
        self,
        *,
        record: ClusterOperationLifecycleRecord,
        target: ClusterMutationTargetDefinition,
        pairs: list[tuple[str, PortForwardAction, str, TlsDc8NoClientCertificateProbeAction]],
        started_mutations: list[tuple[str, HelmUpgradeInstallAction]],
    ) -> ClusterOperationExecutionResult:
        """Run finite forward/probe pairs, closing a verified child before every result."""
        process_adapter = self._port_forward_process_adapter
        probe_adapter = self._tls_dc8_probe_adapter
        if process_adapter is None or probe_adapter is None:
            raise ClusterOperationRunnerError("port-forward/TLS execution requires dispatcher-owned adapters")

        for _forward_action_id, forward, probe_action_id, probe in pairs:
            argv = self._port_forward_argv(target, forward)
            now = self._now()
            ownership = PortForwardOwnership(
                action_id=forward.action_id,
                context=target.context,
                resource=forward.resource,
                bind_address="127.0.0.1",
                local_port=forward.local_port,
                remote_port=forward.remote_port,
                argv_sha256=_sha256("\0".join(argv).encode("utf-8")),
                state=PortForwardOwnershipState.INTENT_PERSISTED,
                intent_at=now,
            )
            try:
                record = self._state_store.persist_cluster_operation_port_forward_intent(
                    run_id=record.run_id,
                    operation_id=record.operation_id,
                    source_revision=record.source_revision,
                    expected_generation=record.generation,
                    ownership=ownership,
                    now=now,
                )
            except Exception as exc:
                raise ClusterOperationRunnerError("port-forward intent could not be persisted") from exc

            try:
                child = process_adapter.spawn(
                    PortForwardSpawnRequest(argv=argv, target=target, action=forward)
                )
            except Exception:
                return self._reconciliation_result(record)
            if not self._valid_port_forward_child(child, ownership.argv_sha256):
                return self._reconciliation_result(record)
            try:
                record = self._state_store.persist_cluster_operation_port_forward_started(
                    run_id=record.run_id,
                    operation_id=record.operation_id,
                    source_revision=record.source_revision,
                    expected_generation=record.generation,
                    action_id=forward.action_id,
                    pid=child.pid,
                    process_created_at=child.process_created_at,
                    now=self._now(),
                )
            except Exception:
                # The child exists but lacks a durable exact identity; never signal it.
                return self._reconciliation_result(record)

            lifetime_deadline = time.monotonic() + forward.lifetime_timeout_seconds
            readiness_timeout = self._remaining_timeout(
                lifetime_deadline, forward.startup_timeout_seconds
            )
            try:
                readiness = (
                    process_adapter.wait_ready(
                        self._owned_port_forward(record, forward.action_id), readiness_timeout
                    )
                    if readiness_timeout > 0
                    else PortForwardReadiness.TIMEOUT
                )
            except Exception:
                readiness = PortForwardReadiness.AMBIGUOUS
            if readiness is not PortForwardReadiness.READY:
                active_record = record
                cleaned_record = self._cleanup_port_forward(record, forward)
                if cleaned_record is None:
                    return self._reconciliation_result(active_record)
                record = cleaned_record
                if readiness is PortForwardReadiness.AMBIGUOUS:
                    return self._reconciliation_result(record)
                return self._port_forward_failure(record, target, started_mutations)

            record = self._transition(record, ClusterOperationStatus.TLS_DC8_PROBING)
            probe_started = time.monotonic()
            probe_timeout = self._remaining_timeout(lifetime_deadline, forward.probe_timeout_seconds)
            ambiguous = False
            try:
                probe_result = (
                    probe_adapter.probe_no_client_certificate(
                        TlsDc8ProbeRequest(
                            port_forward_action_id=forward.action_id,
                            bind_address="127.0.0.1",
                            local_port=forward.local_port,
                            timeout_seconds=probe_timeout,
                        )
                    )
                    if probe_timeout > 0
                    else TlsDc8ProbeResult(
                        outcome=TlsDc8ProbeOutcome.TIMEOUT,
                        evidence_sha256=_canonical_digest({"outcome": "timeout"}),
                    )
                )
                if not isinstance(probe_result, TlsDc8ProbeResult):
                    raise TypeError("invalid TLS/DC8 probe result")
                outcome = probe_result.outcome
                evidence = TlsDc8ProbeEvidence(
                    action_id=probe_action_id,
                    port_forward_action_id=probe.port_forward_action_id,
                    outcome=outcome.value,
                    evidence_sha256=probe_result.evidence_sha256,
                    duration_milliseconds=min(
                        int((time.monotonic() - probe_started) * 1000),
                        forward.probe_timeout_seconds * 1000,
                    ),
                )
            except Exception:
                ambiguous = True
                outcome = TlsDc8ProbeOutcome.AMBIGUOUS
                evidence = TlsDc8ProbeEvidence(
                    action_id=probe_action_id,
                    port_forward_action_id=probe.port_forward_action_id,
                    outcome=outcome.value,
                    evidence_sha256=_canonical_digest({"outcome": outcome.value}),
                    duration_milliseconds=min(
                        int((time.monotonic() - probe_started) * 1000),
                        forward.probe_timeout_seconds * 1000,
                    ),
                )
            try:
                record = self._state_store.append_cluster_operation_tls_dc8_probe_evidence(
                    run_id=record.run_id,
                    operation_id=record.operation_id,
                    source_revision=record.source_revision,
                    expected_generation=record.generation,
                    evidence=evidence,
                    now=self._now(),
                )
            except Exception:
                active_record = record
                cleaned_record = self._cleanup_port_forward(record, forward)
                return self._reconciliation_result(cleaned_record or active_record)

            active_record = record
            cleaned_record = self._cleanup_port_forward(record, forward)
            if cleaned_record is None:
                return self._reconciliation_result(active_record)
            record = cleaned_record
            if ambiguous or outcome is TlsDc8ProbeOutcome.AMBIGUOUS:
                return self._reconciliation_result(record)
            if outcome in {
                TlsDc8ProbeOutcome.CLIENT_CERTIFICATE_REQUIRED,
                TlsDc8ProbeOutcome.UNAUTHENTICATED_LISTENER_REJECTED,
            }:
                continue
            return self._port_forward_failure(record, target, started_mutations)

        record = self._transition(record, ClusterOperationStatus.SUCCEEDED)
        return ClusterOperationExecutionResult(record=record, status=record.status)

    def _cleanup_port_forward(
        self,
        record: ClusterOperationLifecycleRecord,
        action: PortForwardAction,
    ) -> ClusterOperationLifecycleRecord | None:
        """Close only the persisted exact child; PID reuse and all uncertainty reconcile."""
        adapter = self._port_forward_process_adapter
        if adapter is None:
            return None
        ownership = self._owned_port_forward(record, action.action_id)
        try:
            outcome = adapter.close_owned(ownership, action.lifetime_timeout_seconds)
        except Exception:
            outcome = None
        if outcome is PortForwardCleanupOutcome.CLOSED:
            try:
                return self._state_store.persist_cluster_operation_port_forward_stopped(
                    run_id=record.run_id,
                    operation_id=record.operation_id,
                    source_revision=record.source_revision,
                    expected_generation=record.generation,
                    action_id=action.action_id,
                    now=self._now(),
                )
            except Exception:
                return None
        try:
            self._state_store.persist_cluster_operation_port_forward_cleanup_ambiguity(
                run_id=record.run_id,
                operation_id=record.operation_id,
                source_revision=record.source_revision,
                expected_generation=record.generation,
                action_id=action.action_id,
                now=self._now(),
            )
        except Exception:
            pass
        return None

    def _port_forward_failure(
        self,
        record: ClusterOperationLifecycleRecord,
        target: ClusterMutationTargetDefinition,
        started_mutations: list[tuple[str, HelmUpgradeInstallAction]],
    ) -> ClusterOperationExecutionResult:
        if started_mutations:
            try:
                return self._rollback(record, target, started_mutations)
            except Exception:
                return self._reconciliation_result(record)
        record = self._transition(record, ClusterOperationStatus.FAILED)
        return ClusterOperationExecutionResult(record=record, status=record.status)

    def _reconciliation_result(
        self, record: ClusterOperationLifecycleRecord
    ) -> ClusterOperationExecutionResult:
        record = self._state_store.load_cluster_operation(
            run_id=record.run_id,
            operation_id=record.operation_id,
            source_revision=record.source_revision,
        )
        if record.status is not ClusterOperationStatus.RECONCILIATION_REQUIRED:
            record = self._transition(record, ClusterOperationStatus.RECONCILIATION_REQUIRED)
        return ClusterOperationExecutionResult(
            record=record, status=ClusterOperationStatus.RECONCILIATION_REQUIRED
        )

    @staticmethod
    def _valid_port_forward_child(child: object, argv_sha256: str) -> bool:
        if not isinstance(child, PortForwardChild):
            return False
        if not isinstance(child.pid, int) or isinstance(child.pid, bool) or child.pid < 1:
            return False
        if child.argv_sha256 != argv_sha256:
            return False
        created_at = child.process_created_at
        return (
            created_at.tzinfo is not None
            and created_at.utcoffset() == UTC.utcoffset(created_at)
        )

    @staticmethod
    def _owned_port_forward(
        record: ClusterOperationLifecycleRecord, action_id: str
    ) -> PortForwardOwnership:
        matching = tuple(item for item in record.port_forwards if item.action_id == action_id)
        if len(matching) != 1:
            raise ClusterOperationRunnerError("durable port-forward ownership identity is unavailable")
        return matching[0]

    @staticmethod
    def _remaining_timeout(deadline: float, ceiling: int) -> int:
        remaining = deadline - time.monotonic()
        return 0 if remaining <= 0 else min(ceiling, max(1, int(remaining)))

    def _verify_toolchain(self, target: ClusterMutationTargetDefinition) -> None:
        observed: dict[str, str] = {}
        errors: list[str] = []
        for name, tool in (("kubectl", target.toolchain.kubectl), ("helm", target.toolchain.helm)):
            path = Path(tool.path)
            if not path.is_file() or not os.access(path, os.X_OK):
                errors.append(f"{name} executable is unavailable")
                continue
            try:
                observed[name] = _file_sha256(path)
            except OSError:
                errors.append(f"{name} executable cannot be read")
        for name, tool in (("kubectl", target.toolchain.kubectl), ("helm", target.toolchain.helm)):
            if observed.get(name) != tool.sha256:
                errors.append(f"{name} checksum mismatch")
        if errors:
            raise ClusterOperationRunnerError("mutation toolchain verification failed: " + "; ".join(errors))

    def _verify_approved_source_files(
        self,
        operation: ValidatedClusterOperation,
        record: ClusterOperationLifecycleRecord,
    ) -> None:
        assert record.snapshot is not None
        expected_paths = {item.path for item in operation.manifest.allowed_files}
        approved = {item.path: item.sha256 for item in record.snapshot.source_file_digests}
        if set(approved) != expected_paths:
            raise ClusterOperationRunnerError("approved source files do not exactly match the static manifest")
        repository_root = operation.repository_root.resolve()
        for relative_path, expected_digest in approved.items():
            path = _source_path(repository_root, relative_path)
            if not path.is_file() or _file_sha256(path) != expected_digest:
                raise ClusterOperationRunnerError("approved source file digest no longer matches")
        for action in operation.manifest.actions:
            if not isinstance(action, HelmUpgradeInstallAction):
                continue
            chart_root = _source_path(repository_root, action.chart_path)
            if not chart_root.is_dir():
                raise ClusterOperationRunnerError("approved local Helm chart is unavailable")
            for chart_file in chart_root.rglob("*"):
                if not chart_file.is_file():
                    continue
                try:
                    chart_relative_path = chart_file.resolve().relative_to(repository_root).as_posix()
                except ValueError as exc:
                    raise ClusterOperationRunnerError("local Helm chart escapes repository root") from exc
                if chart_relative_path not in approved:
                    raise ClusterOperationRunnerError("local Helm chart has an unapproved source file")

    def _run_fixed_command(
        self,
        *,
        record: ClusterOperationLifecycleRecord,
        action_id: str,
        command_id: str,
        kind: Literal["server_dry_run", "mutation", "readiness_probe", "rollback"],
        argv: tuple[str, ...],
        timeout_seconds: int,
    ) -> tuple[ClusterOperationLifecycleRecord, ClusterOperationCommandResult]:
        started = time.monotonic()
        try:
            result = self._command_runner(argv, timeout_seconds)
        except Exception as exc:
            raise ClusterOperationRunnerError("fixed command runner did not complete") from exc
        duration_milliseconds = min(int((time.monotonic() - started) * 1000), timeout_seconds * 1000)
        if (
            not isinstance(result, ClusterOperationCommandResult)
            or not isinstance(result.returncode, int)
            or not -255 <= result.returncode <= 255
        ):
            raise ClusterOperationRunnerError("fixed command runner returned an invalid result")
        stdout = _bounded_output(result.stdout)
        stderr = _bounded_output(result.stderr)
        evidence = ClusterOperationCommandEvidence(
            command_id=command_id,
            action_id=action_id,
            kind=kind,
            status="succeeded" if result.returncode == 0 else "failed",
            returncode=result.returncode,
            duration_milliseconds=duration_milliseconds,
            stdout_sha256=_sha256(stdout),
            stderr_sha256=_sha256(stderr),
        )
        return (
            self._state_store.append_cluster_operation_command_evidence(
                run_id=record.run_id,
                operation_id=record.operation_id,
                source_revision=record.source_revision,
                expected_generation=record.generation,
                evidence=evidence,
                now=self._now(),
            ),
            result,
        )

    def _rollback(
        self,
        record: ClusterOperationLifecycleRecord,
        target: ClusterMutationTargetDefinition,
        mutations: list[tuple[str, HelmUpgradeInstallAction]],
    ) -> ClusterOperationExecutionResult:
        assert record.snapshot is not None
        rollbacks = {
            (item.namespace, item.release): item for item in record.snapshot.release_rollback_snapshots
        }
        record = self._transition(record, ClusterOperationStatus.ROLLBACK_STARTED)
        rollback_failed = False
        for action_id, action in reversed(mutations):
            snapshot = rollbacks[(action.namespace, action.release)]
            try:
                record, result = self._run_fixed_command(
                    record=record,
                    action_id=action_id,
                    command_id=f"{action_id}-rollback",
                    kind="rollback",
                    argv=self._helm_rollback_argv(target, action, snapshot),
                    timeout_seconds=action.timeout_seconds,
                )
                rollback_failed = rollback_failed or result.returncode != 0
            except ClusterOperationRunnerError:
                rollback_failed = True
        record = self._transition(
            record,
            ClusterOperationStatus.RECONCILIATION_REQUIRED
            if rollback_failed
            else ClusterOperationStatus.ROLLED_BACK,
        )
        return ClusterOperationExecutionResult(record=record, status=record.status)

    def _transition(
        self,
        record: ClusterOperationLifecycleRecord,
        target: ClusterOperationStatus,
    ) -> ClusterOperationLifecycleRecord:
        return self._state_store.transition_cluster_operation(
            run_id=record.run_id,
            operation_id=record.operation_id,
            source_revision=record.source_revision,
            expected_generation=record.generation,
            target=target,
            now=self._now(),
        )

    @staticmethod
    def _kubectl_server_dry_run_argv(
        target: ClusterMutationTargetDefinition,
        operation: ValidatedClusterOperation,
        action: KubectlServerDryRunAction,
    ) -> tuple[str, ...]:
        repository_root = operation.repository_root.resolve()
        files = tuple(_source_path(repository_root, item.path) for item in action.manifest_files)
        argv: tuple[str, ...] = (
            target.toolchain.kubectl.path,
            "--context",
            target.context,
            "--namespace",
            action.namespace,
            "apply",
            "--server-side",
            "--dry-run=server",
            "--field-manager=dispatcher-cluster-operation",
        )
        for path in files:
            argv += ("--filename", str(path))
        return argv

    @staticmethod
    def _port_forward_argv(
        target: ClusterMutationTargetDefinition,
        action: PortForwardAction,
    ) -> tuple[str, ...]:
        return (
            target.toolchain.kubectl.path,
            "--context",
            target.context,
            "--namespace",
            action.resource.namespace,
            "port-forward",
            "--address",
            "127.0.0.1",
            f"service/{action.resource.name}",
            f"{action.local_port}:{action.remote_port}",
        )

    @staticmethod
    def _helm_upgrade_install_argv(
        target: ClusterMutationTargetDefinition,
        operation: ValidatedClusterOperation,
        action: HelmUpgradeInstallAction,
    ) -> tuple[str, ...]:
        repository_root = operation.repository_root.resolve()
        argv: tuple[str, ...] = (
            target.toolchain.helm.path,
            "upgrade",
            "--install",
            action.release,
            str(_source_path(repository_root, action.chart_path)),
            "--namespace",
            action.namespace,
            "--kube-context",
            target.context,
            "--wait",
            "--rollback-on-failure",
            f"--timeout={action.timeout_seconds}s",
        )
        for value_file in action.values_files:
            argv += ("--values", str(_source_path(repository_root, value_file.path)))
        return argv

    @staticmethod
    def _kubectl_rollout_status_argv(
        target: ClusterMutationTargetDefinition,
        probe: ReadinessProbe,
        timeout_seconds: int,
    ) -> tuple[str, ...]:
        resource_kind = {
            "deployment_available": "deployment",
            "statefulset_ready": "statefulset",
            "job_complete": "job",
        }[probe.probe]
        return (
            target.toolchain.kubectl.path,
            "--context",
            target.context,
            "--namespace",
            probe.resource.namespace,
            "rollout",
            "status",
            f"{resource_kind}/{probe.resource.name}",
            f"--timeout={timeout_seconds}s",
        )

    @staticmethod
    def _helm_rollback_argv(
        target: ClusterMutationTargetDefinition,
        action: HelmUpgradeInstallAction,
        snapshot: HelmReleaseRollbackSnapshot,
    ) -> tuple[str, ...]:
        base = (
            target.toolchain.helm.path,
            "--namespace",
            action.namespace,
            "--kube-context",
            target.context,
        )
        if snapshot.pre_snapshot_state == "new":
            return (
                target.toolchain.helm.path,
                "uninstall",
                action.release,
                "--namespace",
                action.namespace,
                "--kube-context",
                target.context,
                "--wait",
                f"--timeout={action.timeout_seconds}s",
            )
        assert snapshot.pre_snapshot_revision is not None
        return (
            target.toolchain.helm.path,
            "rollback",
            action.release,
            str(snapshot.pre_snapshot_revision),
            *base[1:],
            "--wait",
            f"--timeout={action.timeout_seconds}s",
        )


def _source_path(repository_root: Path, relative_path: str) -> Path:
    path = (repository_root / relative_path).resolve()
    try:
        path.relative_to(repository_root)
    except ValueError as exc:
        raise ClusterOperationRunnerError("approved source path escapes repository root") from exc
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_output(value: object) -> bytes:
    if not isinstance(value, bytes) or len(value) > _MAX_COMMAND_OUTPUT_BYTES:
        raise ClusterOperationRunnerError("fixed command runner returned unbounded output")
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_digest(value: object) -> str:
    import json

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
