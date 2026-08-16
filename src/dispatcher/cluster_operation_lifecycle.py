"""Approval-bound, non-executing lifecycle contracts for cluster operations.

These contracts intentionally bind only sanitized identifiers and digests. They
do not collect snapshots, execute tools, inspect a cluster, or represent command
output, manifests, credentials, or Secret values.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from .cluster_operations import (
    AutomaticRollbackIntent,
    ClusterOperationManifest,
    ExpectedResourceIdentity,
    HelmUpgradeInstallAction,
    SecretKeyName,
    ValidatedClusterOperation,
)
from .config import (
    ContractModel,
    Identifier,
    KubernetesContext,
    KubernetesName,
    validate_normalized_relative_path,
)

if TYPE_CHECKING:
    from .operation import RealOperationApproval


class ClusterOperationLifecycleError(ValueError):
    """A cluster-operation lifecycle input is stale, unsafe, or inconsistent."""


Sha256Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CommittedSourceRevision = Annotated[str, Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")]


class ClusterOperationStatus(str, Enum):
    """The complete durable lifecycle vocabulary for a cluster operation."""

    DISCOVERED = "DISCOVERED"
    STATIC_VALIDATED = "STATIC_VALIDATED"
    SNAPSHOT_CAPTURED = "SNAPSHOT_CAPTURED"
    APPROVED = "APPROVED"
    SERVER_DRY_RUN_PASSED = "SERVER_DRY_RUN_PASSED"
    PORT_FORWARD_INTENT = "PORT_FORWARD_INTENT"
    PORT_FORWARD_STARTED = "PORT_FORWARD_STARTED"
    TLS_DC8_PROBING = "TLS_DC8_PROBING"
    MUTATION_STARTED = "MUTATION_STARTED"
    MUTATED = "MUTATED"
    PROBING = "PROBING"
    SUCCEEDED = "SUCCEEDED"
    ROLLBACK_STARTED = "ROLLBACK_STARTED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


CLUSTER_OPERATION_TRANSITIONS: dict[ClusterOperationStatus, frozenset[ClusterOperationStatus]] = {
    ClusterOperationStatus.DISCOVERED: frozenset(
        {ClusterOperationStatus.STATIC_VALIDATED, ClusterOperationStatus.FAILED}
    ),
    ClusterOperationStatus.STATIC_VALIDATED: frozenset(
        {ClusterOperationStatus.SNAPSHOT_CAPTURED, ClusterOperationStatus.FAILED}
    ),
    ClusterOperationStatus.SNAPSHOT_CAPTURED: frozenset(
        {ClusterOperationStatus.APPROVED, ClusterOperationStatus.FAILED}
    ),
    ClusterOperationStatus.APPROVED: frozenset(
        {
            ClusterOperationStatus.SERVER_DRY_RUN_PASSED,
            ClusterOperationStatus.PORT_FORWARD_INTENT,
            ClusterOperationStatus.FAILED,
        }
    ),
    ClusterOperationStatus.SERVER_DRY_RUN_PASSED: frozenset(
        {
            ClusterOperationStatus.MUTATION_STARTED,
            ClusterOperationStatus.PORT_FORWARD_INTENT,
            ClusterOperationStatus.FAILED,
        }
    ),
    ClusterOperationStatus.PORT_FORWARD_INTENT: frozenset(
        {
            ClusterOperationStatus.PORT_FORWARD_STARTED,
            ClusterOperationStatus.RECONCILIATION_REQUIRED,
        }
    ),
    ClusterOperationStatus.PORT_FORWARD_STARTED: frozenset(
        {
            ClusterOperationStatus.TLS_DC8_PROBING,
            ClusterOperationStatus.ROLLBACK_STARTED,
            ClusterOperationStatus.FAILED,
            ClusterOperationStatus.RECONCILIATION_REQUIRED,
        }
    ),
    ClusterOperationStatus.TLS_DC8_PROBING: frozenset(
        {
            ClusterOperationStatus.PORT_FORWARD_INTENT,
            ClusterOperationStatus.SUCCEEDED,
            ClusterOperationStatus.ROLLBACK_STARTED,
            ClusterOperationStatus.FAILED,
            ClusterOperationStatus.RECONCILIATION_REQUIRED,
        }
    ),
    ClusterOperationStatus.MUTATION_STARTED: frozenset(
        {
            ClusterOperationStatus.MUTATED,
            ClusterOperationStatus.ROLLBACK_STARTED,
            ClusterOperationStatus.FAILED,
            ClusterOperationStatus.RECONCILIATION_REQUIRED,
        }
    ),
    ClusterOperationStatus.MUTATED: frozenset(
        {
            ClusterOperationStatus.PROBING,
            ClusterOperationStatus.ROLLBACK_STARTED,
            ClusterOperationStatus.FAILED,
            ClusterOperationStatus.RECONCILIATION_REQUIRED,
        }
    ),
    ClusterOperationStatus.PROBING: frozenset(
        {
            ClusterOperationStatus.PORT_FORWARD_INTENT,
            ClusterOperationStatus.SUCCEEDED,
            ClusterOperationStatus.ROLLBACK_STARTED,
            ClusterOperationStatus.FAILED,
            ClusterOperationStatus.RECONCILIATION_REQUIRED,
        }
    ),
    ClusterOperationStatus.SUCCEEDED: frozenset(),
    ClusterOperationStatus.ROLLBACK_STARTED: frozenset(
        {
            ClusterOperationStatus.ROLLED_BACK,
            ClusterOperationStatus.FAILED,
            ClusterOperationStatus.RECONCILIATION_REQUIRED,
        }
    ),
    ClusterOperationStatus.ROLLED_BACK: frozenset(),
    ClusterOperationStatus.FAILED: frozenset(),
    ClusterOperationStatus.RECONCILIATION_REQUIRED: frozenset(
        {ClusterOperationStatus.ROLLBACK_STARTED, ClusterOperationStatus.FAILED}
    ),
}


class NamedDigest(ContractModel):
    """One named exact identity digest, without retaining the identity value."""

    name: Identifier
    sha256: Sha256Digest


class ActionDigest(ContractModel):
    """One static action position and its canonical manifest digest."""

    action_id: Identifier
    sha256: Sha256Digest


class ClusterResourceFingerprint(ContractModel):
    """A normalized observed resource identity represented only by its fingerprint."""

    resource: ExpectedResourceIdentity
    sha256: Sha256Digest


class ReleaseFingerprint(ContractModel):
    """A normalized Helm release identity represented only by its fingerprint."""

    namespace: KubernetesName
    release: KubernetesName
    pre_snapshot_state: Literal["new", "existing"] | None = None
    pre_snapshot_revision: int | None = Field(default=None, ge=1)
    chart_version: str | None = Field(default=None, min_length=1, max_length=128)
    app_version: str | None = Field(default=None, min_length=1, max_length=128)
    status: Literal["deployed"] | None = None
    sha256: Sha256Digest

    @model_validator(mode="after")
    def validate_release_metadata(self) -> Self:
        existing_fields = (
            self.pre_snapshot_revision,
            self.chart_version,
            self.app_version,
            self.status,
        )
        if self.pre_snapshot_state == "new" and any(value is not None for value in existing_fields):
            raise ValueError("new release fingerprints must not retain release metadata")
        if self.pre_snapshot_state == "existing" and any(
            value is None for value in existing_fields
        ):
            raise ValueError("existing release fingerprints require complete safe release metadata")
        return self


class ApprovedSourceFileDigest(ContractModel):
    """One exact repository file digest bound into an approval-time snapshot."""

    path: str = Field(min_length=1, max_length=500)
    sha256: Sha256Digest

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_normalized_relative_path(value, "approved source file path")


class HelmReleaseRollbackSnapshot(ContractModel):
    """The only approved release state from which deterministic rollback may run."""

    namespace: KubernetesName
    release: KubernetesName
    pre_snapshot_state: Literal["new", "existing"]
    pre_snapshot_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_rollback_state(self) -> Self:
        if self.pre_snapshot_state == "new" and self.pre_snapshot_revision is not None:
            raise ValueError("new releases must not have a pre_snapshot_revision")
        if self.pre_snapshot_state == "existing" and self.pre_snapshot_revision is None:
            raise ValueError("existing releases require a pre_snapshot_revision")
        return self


class ImageFingerprint(ContractModel):
    """A normalized image slot represented only by an exact approval-time digest."""

    image_id: Identifier
    sha256: Sha256Digest


class SecretMetadataFingerprint(ContractModel):
    """Secret namespace/name/key metadata with no representable Secret value."""

    namespace: KubernetesName
    name: KubernetesName
    keys: tuple[SecretKeyName, ...] = Field(min_length=1, max_length=50)
    sha256: Sha256Digest

    @field_validator("keys", mode="before")
    @classmethod
    def freeze_keys(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("keys")
    @classmethod
    def validate_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(value)) != value or len(set(value)) != len(value):
            raise ValueError("secret metadata keys must be sorted and unique")
        return value


class ClusterOperationCommandEvidence(ContractModel):
    """Bounded digest-only result metadata for one dispatcher-owned fixed command."""

    command_id: Identifier
    action_id: Identifier
    kind: Literal["server_dry_run", "mutation", "readiness_probe", "rollback"]
    status: Literal["succeeded", "failed"]
    returncode: int = Field(ge=-255, le=255)
    duration_milliseconds: int = Field(ge=0, le=3_600_000)
    stdout_sha256: Sha256Digest
    stderr_sha256: Sha256Digest


class PortForwardOwnershipState(str, Enum):
    """The durable ownership state of one dispatcher-spawned port-forward child."""

    INTENT_PERSISTED = "INTENT_PERSISTED"
    STARTED = "STARTED"
    STOPPED = "STOPPED"
    CLEANUP_AMBIGUOUS = "CLEANUP_AMBIGUOUS"


class PortForwardOwnership(ContractModel):
    """Sanitized identity for one owned loopback port-forward child process."""

    action_id: Identifier
    context: KubernetesContext
    resource: ExpectedResourceIdentity
    bind_address: Literal["127.0.0.1"]
    local_port: int = Field(ge=1024, le=65535)
    remote_port: int = Field(ge=1, le=65535)
    argv_sha256: Sha256Digest
    state: PortForwardOwnershipState
    intent_at: datetime
    pid: int | None = Field(default=None, ge=1)
    process_created_at: datetime | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None

    @field_validator("intent_at", "process_created_at", "started_at", "stopped_at", mode="before")
    @classmethod
    def parse_persisted_timestamps(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            return datetime.fromisoformat(value.replace("Z", "+00"))
        except ValueError as exc:
            raise ValueError("port-forward timestamp is invalid") from exc

    @model_validator(mode="after")
    def validate_owned_service_process(self) -> Self:
        if (self.resource.api_version, self.resource.kind) != ("v1", "Service"):
            raise ValueError("port-forward ownership resource must be an exact core v1 Service")
        _require_utc(self.intent_at, "port-forward intent_at")
        for field, value in (
            ("port-forward process_created_at", self.process_created_at),
            ("port-forward started_at", self.started_at),
            ("port-forward stopped_at", self.stopped_at),
        ):
            if value is not None:
                _require_utc(value, field)
        has_process_identity = (
            self.pid is not None
            and self.process_created_at is not None
            and self.started_at is not None
        )
        if self.state is PortForwardOwnershipState.INTENT_PERSISTED:
            if any(
                value is not None
                for value in (self.pid, self.process_created_at, self.started_at, self.stopped_at)
            ):
                raise ValueError("port-forward intent must not include a process identity")
        elif self.state is PortForwardOwnershipState.STARTED:
            if not has_process_identity or self.stopped_at is not None:
                raise ValueError("started port-forward requires identity and no stop timestamp")
        elif self.state is PortForwardOwnershipState.STOPPED:
            if not has_process_identity or self.stopped_at is None:
                raise ValueError("stopped port-forward requires identity and stop timestamp")
        elif not has_process_identity or self.stopped_at is not None:
            raise ValueError("ambiguous port-forward cleanup requires an unclosed process identity")
        if self.started_at is not None and self.started_at < self.intent_at:
            raise ValueError("port-forward start cannot precede its intent")
        if self.stopped_at is not None and self.started_at is not None and self.stopped_at < self.started_at:
            raise ValueError("port-forward stop cannot precede its start")
        return self


class TlsDc8ProbeEvidence(ContractModel):
    """Digest-only outcome of one fixed no-client-certificate TLS/DC8 probe."""

    action_id: Identifier
    port_forward_action_id: Identifier
    outcome: Literal[
        "client_certificate_required",
        "unauthenticated_listener_rejected",
        "unauthenticated_handshake_succeeded",
        "unexpected_listener_behavior",
        "timeout",
        "ambiguous",
    ]
    evidence_sha256: Sha256Digest
    duration_milliseconds: int = Field(ge=0, le=600_000)


class ClusterOperationApprovalSnapshot(ContractModel):
    """A sanitized approval-time snapshot with digest-only observation bindings."""

    snapshot_version: Literal[1] = 1
    run_id: Identifier
    step_id: Identifier
    operation_id: Identifier
    source_revision: CommittedSourceRevision
    plan_digest: Sha256Digest
    config_digest: Sha256Digest
    validated_manifest_digest: Sha256Digest
    envelope_digest: Sha256Digest | None = None
    cluster_preflight_result_digest: Sha256Digest
    binary_identity_digests: tuple[NamedDigest, ...] = Field(min_length=1, max_length=50)
    toolchain_identity_digests: tuple[NamedDigest, ...] = Field(min_length=1, max_length=50)
    tier1_invariant_snapshot_digest: Sha256Digest
    action_digests: tuple[ActionDigest, ...] = Field(min_length=1, max_length=20)
    source_file_digests: tuple[ApprovedSourceFileDigest, ...] = Field(min_length=1, max_length=100)
    resource_fingerprints: tuple[ClusterResourceFingerprint, ...] = Field(
        min_length=1, max_length=100
    )
    release_fingerprints: tuple[ReleaseFingerprint, ...] = Field(max_length=100)
    release_rollback_snapshots: tuple[HelmReleaseRollbackSnapshot, ...] = Field(max_length=20)
    image_fingerprints: tuple[ImageFingerprint, ...] = Field(max_length=100)
    secret_metadata_fingerprints: tuple[SecretMetadataFingerprint, ...] = Field(max_length=100)
    captured_at: datetime
    expires_at: datetime

    @field_validator(
        "binary_identity_digests",
        "toolchain_identity_digests",
        "action_digests",
        "source_file_digests",
        "resource_fingerprints",
        "release_fingerprints",
        "release_rollback_snapshots",
        "image_fingerprints",
        "secret_metadata_fingerprints",
        mode="before",
    )
    @classmethod
    def freeze_collections(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_normalized_collections(self) -> Self:
        _require_ordered_unique(
            self.binary_identity_digests, lambda value: value.name, "binary identities"
        )
        _require_ordered_unique(
            self.toolchain_identity_digests, lambda value: value.name, "toolchain identities"
        )
        _require_unique(self.action_digests, lambda value: value.action_id, "action digests")
        _require_ordered_unique(
            self.source_file_digests, lambda value: value.path, "source file digests"
        )
        _require_ordered_unique(
            self.resource_fingerprints,
            lambda value: (
                value.resource.api_version,
                value.resource.kind,
                value.resource.namespace,
                value.resource.name,
            ),
            "resource fingerprints",
        )
        _require_ordered_unique(
            self.release_fingerprints,
            lambda value: (value.namespace, value.release),
            "release fingerprints",
        )
        _require_ordered_unique(
            self.release_rollback_snapshots,
            lambda value: (value.namespace, value.release),
            "release rollback snapshots",
        )
        _require_ordered_unique(
            self.image_fingerprints, lambda value: value.image_id, "image fingerprints"
        )
        _require_ordered_unique(
            self.secret_metadata_fingerprints,
            lambda value: (value.namespace, value.name),
            "secret metadata fingerprints",
        )
        _require_utc(self.captured_at, "captured_at")
        _require_utc(self.expires_at, "expires_at")
        if self.expires_at <= self.captured_at:
            raise ValueError("snapshot expiry must be after capture time")
        return self

    @property
    def digest(self) -> str:
        """Return the stable digest that approvals bind exactly."""
        return _digest_json(self.model_dump(mode="json"))


class ClusterOperationApproval(ContractModel):
    """Owner or envelope-derived approval tied to one fresh approval-time snapshot."""

    approval_version: Literal[1] = 1
    approval_source: Literal["owner_snapshot", "preauthorized_envelope"] = "owner_snapshot"
    owner_ref: Identifier
    run_id: Identifier
    step_id: Identifier
    operation_id: Identifier
    source_revision: CommittedSourceRevision
    snapshot_digest: Sha256Digest | None = None
    envelope_digest: Sha256Digest | None = None
    allowed_actions: tuple[ActionDigest, ...] = Field(min_length=1, max_length=20)
    rollback_intent: AutomaticRollbackIntent
    rollback_digest: Sha256Digest
    issued_at: datetime
    expires_at: datetime

    @field_validator("allowed_actions", mode="before")
    @classmethod
    def freeze_actions(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_approval_timing_and_actions(self) -> Self:
        _require_unique(self.allowed_actions, lambda value: value.action_id, "allowed actions")
        if self.approval_source == "owner_snapshot":
            if self.snapshot_digest is None or self.envelope_digest is not None:
                raise ValueError("owner approval requires a snapshot and no envelope digest")
        elif self.envelope_digest is None:
            raise ValueError("preauthorized approval requires an envelope digest")
        _require_utc(self.issued_at, "issued_at")
        _require_utc(self.expires_at, "expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("approval expiry must be after issue time")
        return self

    @property
    def digest(self) -> str:
        """Return the stable digest for audit correlation without retaining raw inputs."""
        return _digest_json(self.model_dump(mode="json"))


class ClusterOperationLifecycleRecord(ContractModel):
    """One journal row with immutable identity and its approval-bound lifecycle."""

    lifecycle_version: Literal[1] = 1
    run_id: Identifier
    step_id: Identifier
    operation_id: Identifier
    source_revision: CommittedSourceRevision
    target_name: Identifier
    plan_digest: Sha256Digest
    config_digest: Sha256Digest
    validated_manifest_digest: Sha256Digest
    static_action_digests: tuple[ActionDigest, ...] = Field(min_length=1, max_length=20)
    rollback_intent: AutomaticRollbackIntent
    rollback_digest: Sha256Digest
    max_snapshot_age_seconds: int = Field(ge=1, le=86_400)
    status: ClusterOperationStatus
    generation: int = Field(ge=1)
    snapshot: ClusterOperationApprovalSnapshot | None = None
    snapshot_digest: Sha256Digest | None = None
    approval: ClusterOperationApproval | None = None
    approval_digest: Sha256Digest | None = None
    command_evidence: tuple[ClusterOperationCommandEvidence, ...] = Field(
        default=(), max_length=200
    )
    port_forwards: tuple[PortForwardOwnership, ...] = Field(default=(), max_length=20)
    tls_dc8_probe_evidence: tuple[TlsDc8ProbeEvidence, ...] = Field(default=(), max_length=20)
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "static_action_digests",
        "command_evidence",
        "port_forwards",
        "tls_dc8_probe_evidence",
        mode="before",
    )
    @classmethod
    def freeze_static_actions(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_record_bindings(self) -> Self:
        _require_unique(
            self.static_action_digests, lambda value: value.action_id, "static action digests"
        )
        _require_unique(self.command_evidence, lambda value: value.command_id, "command evidence")
        _require_unique(self.port_forwards, lambda value: value.action_id, "port-forward ownership")
        _require_unique(self.tls_dc8_probe_evidence, lambda value: value.action_id, "TLS/DC8 probe evidence")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        snapshot_fields = (self.snapshot, self.snapshot_digest)
        if any(value is None for value in snapshot_fields) and any(
            value is not None for value in snapshot_fields
        ):
            raise ValueError("snapshot and snapshot_digest must be present together")
        if self.snapshot is not None:
            if self.snapshot.digest != self.snapshot_digest:
                raise ValueError("snapshot digest does not match snapshot payload")
            _validate_snapshot_identity(self, self.snapshot)
        approval_fields = (self.approval, self.approval_digest)
        if any(value is None for value in approval_fields) and any(
            value is not None for value in approval_fields
        ):
            raise ValueError("approval and approval_digest must be present together")
        if self.approval is not None:
            if self.approval.digest != self.approval_digest:
                raise ValueError("approval digest does not match approval payload")
            _validate_approval_identity(self, self.approval)
        if (
            self.status
            in {
                ClusterOperationStatus.DISCOVERED,
                ClusterOperationStatus.STATIC_VALIDATED,
            }
            and self.snapshot is not None
        ):
            raise ValueError("snapshot cannot exist before SNAPSHOT_CAPTURED")
        if self.status is ClusterOperationStatus.SNAPSHOT_CAPTURED and self.approval is not None:
            raise ValueError("approval cannot exist before APPROVED")
        if self.status in _APPROVAL_REQUIRED_STATES and self.approval is None:
            raise ValueError("approval is required for this lifecycle state")
        if self.status in {
            ClusterOperationStatus.SUCCEEDED,
            ClusterOperationStatus.FAILED,
            ClusterOperationStatus.ROLLED_BACK,
        } and any(item.state is not PortForwardOwnershipState.STOPPED for item in self.port_forwards):
            raise ValueError("terminal operations cannot retain an owned port-forward")
        return self

    @property
    def identity(self) -> tuple[str, str, str]:
        """The immutable SQLite primary key for this operation lifecycle."""
        return (self.run_id, self.operation_id, self.source_revision)


_APPROVAL_REQUIRED_STATES = frozenset(
    {
        ClusterOperationStatus.APPROVED,
        ClusterOperationStatus.SERVER_DRY_RUN_PASSED,
        ClusterOperationStatus.PORT_FORWARD_INTENT,
        ClusterOperationStatus.PORT_FORWARD_STARTED,
        ClusterOperationStatus.TLS_DC8_PROBING,
        ClusterOperationStatus.MUTATION_STARTED,
        ClusterOperationStatus.MUTATED,
        ClusterOperationStatus.PROBING,
        ClusterOperationStatus.SUCCEEDED,
        ClusterOperationStatus.ROLLBACK_STARTED,
        ClusterOperationStatus.ROLLED_BACK,
    }
)
_FUTURE_EXECUTION_STATES = frozenset(
    {
        ClusterOperationStatus.SERVER_DRY_RUN_PASSED,
        ClusterOperationStatus.PORT_FORWARD_INTENT,
        ClusterOperationStatus.PORT_FORWARD_STARTED,
        ClusterOperationStatus.TLS_DC8_PROBING,
        ClusterOperationStatus.MUTATION_STARTED,
        ClusterOperationStatus.MUTATED,
        ClusterOperationStatus.PROBING,
        ClusterOperationStatus.SUCCEEDED,
        ClusterOperationStatus.ROLLBACK_STARTED,
        ClusterOperationStatus.ROLLED_BACK,
    }
)
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "authorization",
        "certificate",
        "command_output",
        "data",
        "kubeconfig",
        "key",
        "password",
        "private_key",
        "raw_manifest",
        "raw_output",
        "secret",
        "stringdata",
        "token",
    }
)
_SECRET_TEXT = re.compile(
    r"(?i)(-----BEGIN (?:[A-Z ]+)?(?:PRIVATE KEY|CERTIFICATE)-----|"
    r"\b(?:authorization|password|secret|token)\s*[:=]|\bkubeconfig\b)"
)


def new_cluster_operation_lifecycle_record(
    operation: ValidatedClusterOperation,
    *,
    run_id: str,
    source_revision: str,
    plan_digest: str,
    config_digest: str,
    max_snapshot_age_seconds: int,
    now: datetime,
) -> ClusterOperationLifecycleRecord:
    """Create the initial DISCOVERED record from one post-commit static validation."""
    manifest = operation.manifest
    action_digests = _manifest_action_digests(manifest)
    return ClusterOperationLifecycleRecord(
        run_id=run_id,
        step_id=operation.step_id,
        operation_id=manifest.operation_id,
        source_revision=source_revision,
        target_name=operation.target_name,
        plan_digest=plan_digest,
        config_digest=config_digest,
        validated_manifest_digest=_digest_json(manifest.model_dump(mode="json")),
        static_action_digests=action_digests,
        rollback_intent=manifest.rollback,
        rollback_digest=_digest_json(manifest.rollback.model_dump(mode="json")),
        max_snapshot_age_seconds=max_snapshot_age_seconds,
        status=ClusterOperationStatus.DISCOVERED,
        generation=1,
        created_at=now,
        updated_at=now,
    )


def transition_cluster_operation(
    record: ClusterOperationLifecycleRecord,
    target: ClusterOperationStatus,
    *,
    now: datetime,
) -> ClusterOperationLifecycleRecord:
    """Move through the explicit lifecycle table without collecting or executing anything."""
    _require_utc(now, "now")
    if target in {ClusterOperationStatus.SNAPSHOT_CAPTURED, ClusterOperationStatus.APPROVED}:
        raise ClusterOperationLifecycleError(
            f"{target.value} requires attach_cluster_operation_snapshot or attach_cluster_operation_approval"
        )
    _require_transition(record.status, target)
    if target in _FUTURE_EXECUTION_STATES:
        _require_active_approval(record, now)
    return _validated_replace(record, status=target, updated_at=now)


def attach_cluster_operation_snapshot(
    record: ClusterOperationLifecycleRecord,
    snapshot: ClusterOperationApprovalSnapshot,
    *,
    now: datetime,
) -> ClusterOperationLifecycleRecord:
    """Bind one fresh sanitized snapshot after static validation and before approval."""
    _require_utc(now, "now")
    _require_transition(record.status, ClusterOperationStatus.SNAPSHOT_CAPTURED)
    _validate_snapshot_identity(record, snapshot)
    if snapshot.captured_at > now:
        raise ClusterOperationLifecycleError("snapshot capture time is in the future")
    if snapshot.expires_at <= now:
        raise ClusterOperationLifecycleError("snapshot has expired")
    if (
        snapshot.expires_at - snapshot.captured_at
    ).total_seconds() > record.max_snapshot_age_seconds:
        raise ClusterOperationLifecycleError("snapshot exceeds the target maximum age")
    return _validated_replace(
        record,
        status=ClusterOperationStatus.SNAPSHOT_CAPTURED,
        snapshot=snapshot,
        snapshot_digest=snapshot.digest,
        updated_at=now,
    )


def attach_cluster_operation_approval(
    record: ClusterOperationLifecycleRecord,
    approval: ClusterOperationApproval,
    *,
    now: datetime,
) -> ClusterOperationLifecycleRecord:
    """Bind one owner approval only to the record's exact fresh snapshot."""
    _require_utc(now, "now")
    _require_transition(record.status, ClusterOperationStatus.APPROVED)
    _require_active_snapshot(record, now)
    if approval.approval_source == "preauthorized_envelope" and approval.snapshot_digest is None:
        assert record.snapshot is not None
        approval = approval.model_copy(
            update={
                "snapshot_digest": record.snapshot_digest,
                "expires_at": min(approval.expires_at, record.snapshot.expires_at),
            }
        )
    _validate_approval_identity(record, approval)
    if approval.issued_at > now:
        raise ClusterOperationLifecycleError("approval issue time is in the future")
    assert record.snapshot is not None
    if (
        approval.approval_source == "owner_snapshot"
        and approval.issued_at < record.snapshot.captured_at
    ):
        raise ClusterOperationLifecycleError("approval issue time cannot precede snapshot capture")
    if approval.expires_at <= now:
        raise ClusterOperationLifecycleError("approval has expired")
    if approval.expires_at > record.snapshot.expires_at:
        raise ClusterOperationLifecycleError("approval expiry cannot outlast its snapshot")
    return _validated_replace(
        record,
        status=ClusterOperationStatus.APPROVED,
        approval=approval,
        approval_digest=approval.digest,
        command_evidence=(),
        updated_at=now,
    )


def create_auto_approved_cluster_operation_approval(
    operation: ValidatedClusterOperation,
    source_revision: str,
    real_operation_approval: "RealOperationApproval",
    *,
    now: datetime,
) -> ClusterOperationApproval:
    """Derive a snapshot-pending lifecycle approval from one exact preauthorized envelope.

    This boundary is deliberately post-commit and non-executing. The returned
    approval has no snapshot digest yet, so attaching it still requires a fresh
    snapshot with the exact source, manifest, action, toolchain, and expiry
    bindings before the lifecycle can advance.
    """
    _require_utc(now, "now")
    manifest = operation.manifest
    repo_id = manifest.source_identity.repository_id
    matching = tuple(
        envelope
        for envelope in real_operation_approval.cluster_operation_envelopes
        if envelope.step_id == operation.step_id and envelope.repo_id == repo_id
    )
    if len(matching) != 1:
        raise ClusterOperationLifecycleError(
            "real-operation approval has no exact preauthorized envelope for this cluster operation"
        )
    envelope = matching[0]
    try:
        manifest_path = (
            operation.manifest_path.resolve()
            .relative_to(operation.repository_root.resolve())
            .as_posix()
        )
    except ValueError as exc:
        raise ClusterOperationLifecycleError(
            "validated operation manifest path escapes its repository"
        ) from exc
    action_types = tuple(action.action for action in manifest.actions)
    expected_identity = (
        real_operation_approval.run_id,
        real_operation_approval.plan_digest,
        real_operation_approval.config_digest,
        operation.step_id,
        repo_id,
        operation.target_name,
        manifest.context,
        manifest_path,
    )
    actual_identity = (
        envelope.run_id,
        envelope.plan_digest,
        envelope.config_digest,
        envelope.step_id,
        envelope.repo_id,
        envelope.target_name,
        envelope.context,
        envelope.operation_manifest_path,
    )
    if actual_identity != expected_identity:
        raise ClusterOperationLifecycleError(
            "cluster operation does not match its immutable preauthorized envelope"
        )
    if action_types != envelope.allowed_actions:
        raise ClusterOperationLifecycleError(
            "cluster operation manifest actions do not match the preauthorized envelope"
        )
    if manifest.rollback.automatic is not envelope.automatic_rollback:
        raise ClusterOperationLifecycleError(
            "cluster operation rollback intent does not match the preauthorized envelope"
        )
    if not _path_is_in_any_root(manifest_path, envelope.operation_manifest_roots):
        raise ClusterOperationLifecycleError(
            "cluster operation manifest path is outside the preauthorized operation roots"
        )
    source_paths = [item.path for item in manifest.allowed_files]
    source_paths.extend(
        action.chart_path
        for action in manifest.actions
        if isinstance(action, HelmUpgradeInstallAction)
    )
    if any(not _path_is_in_any_root(path, envelope.source_file_roots) for path in source_paths):
        raise ClusterOperationLifecycleError(
            "cluster operation source path is outside the preauthorized source roots"
        )
    return ClusterOperationApproval(
        approval_source="preauthorized_envelope",
        owner_ref=real_operation_approval.approval_ref,
        run_id=envelope.run_id,
        step_id=envelope.step_id,
        operation_id=manifest.operation_id,
        source_revision=source_revision,
        allowed_actions=_manifest_action_digests(manifest),
        rollback_intent=manifest.rollback,
        rollback_digest=_digest_json(manifest.rollback.model_dump(mode="json")),
        envelope_digest=envelope.digest,
        issued_at=now,
        expires_at=now + timedelta(seconds=envelope.max_snapshot_age_seconds),
    )


def load_sanitized_cluster_operation_snapshot(path: str | Path) -> ClusterOperationApprovalSnapshot:
    """Load one duplicate-free snapshot JSON while rejecting secret-shaped payloads."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        raw = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        assert_cluster_operation_safe_payload(raw)
        if not isinstance(raw, dict):
            raise ClusterOperationLifecycleError("snapshot JSON must be an object")
        return ClusterOperationApprovalSnapshot.model_validate_json(text)
    except (OSError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        if isinstance(exc, OSError):
            raise ClusterOperationLifecycleError("snapshot JSON cannot be read") from exc
        raise ClusterOperationLifecycleError(
            "snapshot JSON is not a valid sanitized approval snapshot"
        ) from None


def assert_cluster_operation_safe_payload(value: object) -> None:
    """Reject rather than redact inputs that could carry secrets or raw command material."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _FORBIDDEN_FIELD_NAMES:
                raise ClusterOperationLifecycleError(
                    "cluster operation payload contains a forbidden sensitive field"
                )
            assert_cluster_operation_safe_payload(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_cluster_operation_safe_payload(item)
    elif isinstance(value, str) and _SECRET_TEXT.search(value):
        raise ClusterOperationLifecycleError("cluster operation payload contains secret-like text")


def cluster_operation_record_digest(record: ClusterOperationLifecycleRecord) -> str:
    """Return a digest suitable for a safe append-only journal audit event."""
    return _digest_json(record.model_dump(mode="json"))


def append_cluster_operation_command_evidence(
    record: ClusterOperationLifecycleRecord,
    evidence: ClusterOperationCommandEvidence,
    *,
    now: datetime,
) -> ClusterOperationLifecycleRecord:
    """Append one digest-only command result without changing the lifecycle state."""
    _require_utc(now, "now")
    if record.status in {ClusterOperationStatus.SUCCEEDED, ClusterOperationStatus.ROLLED_BACK}:
        raise ClusterOperationLifecycleError(
            "cannot append command evidence to a terminal operation"
        )
    if any(item.command_id == evidence.command_id for item in record.command_evidence):
        raise ClusterOperationLifecycleError("command evidence id already exists")
    return _validated_replace(
        record,
        command_evidence=(*record.command_evidence, evidence),
        updated_at=now,
    )


def begin_cluster_operation_port_forward(
    record: ClusterOperationLifecycleRecord,
    ownership: PortForwardOwnership,
    *,
    now: datetime,
) -> ClusterOperationLifecycleRecord:
    """Durably persist port-forward intent before a dispatcher may spawn a child."""
    _require_utc(now, "now")
    if ownership.state is not PortForwardOwnershipState.INTENT_PERSISTED:
        raise ClusterOperationLifecycleError("port-forward intent must not contain a child process")
    if ownership.intent_at != now:
        raise ClusterOperationLifecycleError("port-forward intent timestamp must match its journal write")
    _require_transition(record.status, ClusterOperationStatus.PORT_FORWARD_INTENT)
    if any(item.action_id == ownership.action_id for item in record.port_forwards):
        raise ClusterOperationLifecycleError("port-forward action identity already exists")
    return _validated_replace(
        record,
        status=ClusterOperationStatus.PORT_FORWARD_INTENT,
        port_forwards=(*record.port_forwards, ownership),
        updated_at=now,
    )


def mark_cluster_operation_port_forward_started(
    record: ClusterOperationLifecycleRecord,
    *,
    action_id: str,
    pid: int,
    process_created_at: datetime,
    now: datetime,
) -> ClusterOperationLifecycleRecord:
    """Persist an exact child identity immediately after its successful spawn."""
    _require_utc(now, "now")
    _require_utc(process_created_at, "port-forward process_created_at")
    _require_transition(record.status, ClusterOperationStatus.PORT_FORWARD_STARTED)
    ownership = _port_forward_ownership(record, action_id)
    if ownership.state is not PortForwardOwnershipState.INTENT_PERSISTED:
        raise ClusterOperationLifecycleError("port-forward child identity was already recorded")
    started = ownership.model_copy(
        update={
            "state": PortForwardOwnershipState.STARTED,
            "pid": pid,
            "process_created_at": process_created_at,
            "started_at": now,
        }
    )
    return _validated_replace(
        record,
        status=ClusterOperationStatus.PORT_FORWARD_STARTED,
        port_forwards=_replace_port_forward_ownership(record.port_forwards, started),
        updated_at=now,
    )


def mark_cluster_operation_port_forward_stopped(
    record: ClusterOperationLifecycleRecord,
    *,
    action_id: str,
    now: datetime,
) -> ClusterOperationLifecycleRecord:
    """Record successful cleanup of the exact owned child without storing streams."""
    _require_utc(now, "now")
    ownership = _port_forward_ownership(record, action_id)
    if ownership.state is not PortForwardOwnershipState.STARTED:
        raise ClusterOperationLifecycleError("only a started port-forward can be stopped")
    stopped = ownership.model_copy(
        update={"state": PortForwardOwnershipState.STOPPED, "stopped_at": now}
    )
    return _validated_replace(
        record,
        port_forwards=_replace_port_forward_ownership(record.port_forwards, stopped),
        updated_at=now,
    )


def mark_cluster_operation_port_forward_cleanup_ambiguous(
    record: ClusterOperationLifecycleRecord,
    *,
    action_id: str,
    now: datetime,
) -> ClusterOperationLifecycleRecord:
    """Record that an owned child could not be safely confirmed closed."""
    _require_utc(now, "now")
    ownership = _port_forward_ownership(record, action_id)
    if ownership.state is not PortForwardOwnershipState.STARTED:
        raise ClusterOperationLifecycleError("only a started port-forward can need reconciliation")
    ambiguous = ownership.model_copy(update={"state": PortForwardOwnershipState.CLEANUP_AMBIGUOUS})
    return _validated_replace(
        record,
        port_forwards=_replace_port_forward_ownership(record.port_forwards, ambiguous),
        updated_at=now,
    )


def append_cluster_operation_tls_dc8_probe_evidence(
    record: ClusterOperationLifecycleRecord,
    evidence: TlsDc8ProbeEvidence,
    *,
    now: datetime,
) -> ClusterOperationLifecycleRecord:
    """Append one digest-only direct TLS/DC8 outcome for the active forward."""
    _require_utc(now, "now")
    if record.status is not ClusterOperationStatus.TLS_DC8_PROBING:
        raise ClusterOperationLifecycleError("TLS/DC8 evidence requires an active TLS/DC8 probe")
    if any(item.action_id == evidence.action_id for item in record.tls_dc8_probe_evidence):
        raise ClusterOperationLifecycleError("TLS/DC8 probe evidence action id already exists")
    ownership = _port_forward_ownership(record, evidence.port_forward_action_id)
    if ownership.state is not PortForwardOwnershipState.STARTED:
        raise ClusterOperationLifecycleError("TLS/DC8 evidence requires a started port-forward")
    return _validated_replace(
        record,
        tls_dc8_probe_evidence=(*record.tls_dc8_probe_evidence, evidence),
        updated_at=now,
    )


def _port_forward_ownership(
    record: ClusterOperationLifecycleRecord, action_id: str
) -> PortForwardOwnership:
    matching = tuple(item for item in record.port_forwards if item.action_id == action_id)
    if len(matching) != 1:
        raise ClusterOperationLifecycleError("port-forward ownership identity is unavailable")
    return matching[0]


def _replace_port_forward_ownership(
    values: tuple[PortForwardOwnership, ...], replacement: PortForwardOwnership
) -> tuple[PortForwardOwnership, ...]:
    return tuple(item if item.action_id != replacement.action_id else replacement for item in values)


def _manifest_action_digests(manifest: ClusterOperationManifest) -> tuple[ActionDigest, ...]:
    return tuple(
        ActionDigest(
            action_id=f"action-{index}", sha256=_digest_json(action.model_dump(mode="json"))
        )
        for index, action in enumerate(manifest.actions, start=1)
    )


def _validate_snapshot_identity(
    record: ClusterOperationLifecycleRecord,
    snapshot: ClusterOperationApprovalSnapshot,
) -> None:
    expected = (
        record.run_id,
        record.step_id,
        record.operation_id,
        record.source_revision,
        record.plan_digest,
        record.config_digest,
        record.validated_manifest_digest,
        record.static_action_digests,
    )
    actual = (
        snapshot.run_id,
        snapshot.step_id,
        snapshot.operation_id,
        snapshot.source_revision,
        snapshot.plan_digest,
        snapshot.config_digest,
        snapshot.validated_manifest_digest,
        snapshot.action_digests,
    )
    if actual != expected:
        raise ClusterOperationLifecycleError("snapshot does not match immutable operation identity")


def _validate_approval_identity(
    record: ClusterOperationLifecycleRecord,
    approval: ClusterOperationApproval,
) -> None:
    if record.snapshot is None or record.snapshot_digest is None:
        raise ClusterOperationLifecycleError("approval requires an attached snapshot")
    expected = (
        record.run_id,
        record.step_id,
        record.operation_id,
        record.source_revision,
        record.snapshot_digest,
        record.rollback_intent,
        record.rollback_digest,
    )
    actual = (
        approval.run_id,
        approval.step_id,
        approval.operation_id,
        approval.source_revision,
        approval.snapshot_digest,
        approval.rollback_intent,
        approval.rollback_digest,
    )
    if actual != expected:
        raise ClusterOperationLifecycleError(
            "approval does not match immutable snapshot or rollback identity"
        )
    allowed = {item.action_id: item.sha256 for item in record.static_action_digests}
    if any(allowed.get(item.action_id) != item.sha256 for item in approval.allowed_actions):
        raise ClusterOperationLifecycleError(
            "approval contains an action not in the static operation"
        )
    static_order = {
        item.action_id: index for index, item in enumerate(record.static_action_digests)
    }
    action_order = tuple(static_order[item.action_id] for item in approval.allowed_actions)
    if tuple(sorted(action_order)) != action_order:
        raise ClusterOperationLifecycleError("approval actions must retain static operation order")
    if (
        approval.approval_source == "preauthorized_envelope"
        and approval.allowed_actions != record.static_action_digests
    ):
        raise ClusterOperationLifecycleError(
            "preauthorized approval actions must exactly match the static operation"
        )
    if (
        approval.approval_source == "preauthorized_envelope"
        and record.snapshot.envelope_digest != approval.envelope_digest
    ):
        raise ClusterOperationLifecycleError("snapshot does not match the preauthorized envelope")


def _path_is_in_any_root(path: str, roots: tuple[str, ...]) -> bool:
    candidate = PurePosixPath(path)
    for root in roots:
        try:
            candidate.relative_to(PurePosixPath(root))
        except ValueError:
            continue
        return True
    return False


def _require_active_snapshot(record: ClusterOperationLifecycleRecord, now: datetime) -> None:
    if record.snapshot is None or record.snapshot_digest is None:
        raise ClusterOperationLifecycleError("operation has no approval snapshot")
    if record.snapshot.expires_at <= now:
        raise ClusterOperationLifecycleError("approval snapshot has expired")


def _require_active_approval(record: ClusterOperationLifecycleRecord, now: datetime) -> None:
    _require_active_snapshot(record, now)
    if record.approval is None or record.approval_digest is None:
        raise ClusterOperationLifecycleError("operation has no approval")
    if record.approval.expires_at <= now:
        raise ClusterOperationLifecycleError("approval has expired")


def _require_transition(current: ClusterOperationStatus, target: ClusterOperationStatus) -> None:
    if target not in CLUSTER_OPERATION_TRANSITIONS[current]:
        raise ClusterOperationLifecycleError(
            f"illegal cluster operation transition: {current.value} -> {target.value}"
        )


def _validated_replace(
    record: ClusterOperationLifecycleRecord,
    **updates: object,
) -> ClusterOperationLifecycleRecord:
    return ClusterOperationLifecycleRecord.model_validate({**record.model_dump(), **updates})


def _require_ordered_unique(
    values: tuple[Any, ...],
    key: Any,
    field: str,
) -> None:
    keys = tuple(key(value) for value in values)
    if len(set(keys)) != len(keys) or tuple(sorted(keys)) != keys:
        raise ValueError(f"{field} must be sorted and unique")


def _require_unique(values: tuple[Any, ...], key: Any, field: str) -> None:
    keys = tuple(key(value) for value in values)
    if len(set(keys)) != len(keys):
        raise ValueError(f"{field} must be unique")


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field} must use UTC")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ClusterOperationLifecycleError("snapshot JSON contains a duplicate key")
        result[key] = value
    return result


def _digest_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()
