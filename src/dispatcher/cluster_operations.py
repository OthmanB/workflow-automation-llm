"""Static contracts for future dispatcher-owned cluster operations.

This module intentionally contains no command runner, client discovery, state
store, approval persistence, or source-content reader. It validates only typed
repository-owned operation manifests and their filesystem identities.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from .config import (
    ClusterMutationTargetDefinition,
    Config,
    ContractModel,
    Identifier,
    KubernetesContext,
    KubernetesName,
    validate_normalized_relative_path,
)
from .plan import NormalizedPlan
from .yaml_io import DuplicateYamlKeyError, load_unique_yaml


class ClusterOperationError(ValueError):
    """A static cluster operation contract is invalid or does not align with its plan."""


ApprovalSnapshotBinding = Literal["approval_snapshot"]
KubernetesApiVersion = Annotated[
    str,
    Field(
        pattern=(
            r"^(?:v[1-9][0-9]*|[a-z][a-z0-9.-]{0,251}/"
            r"v[1-9][0-9]*(?:(?:alpha|beta)[1-9][0-9]*)?)$"
        )
    ),
]
KubernetesKind = Annotated[str, Field(pattern=r"^[A-Z][A-Za-z0-9]{0,62}$")]
SecretKeyName = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,253}$")]


class ApprovalSnapshotFile(ContractModel):
    """One repository file whose digest is bound at later approval snapshot time."""

    path: str = Field(min_length=1, max_length=500)
    sha256: ApprovalSnapshotBinding

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_normalized_relative_path(value, "file path")


class SourceIdentityBinding(ContractModel):
    """Repository identity with an explicit later approval-time revision binding."""

    repository_id: Identifier
    revision: ApprovalSnapshotBinding


class ExpectedResourceIdentity(ContractModel):
    """One exact Kubernetes resource expected from a typed action."""

    api_version: KubernetesApiVersion
    kind: KubernetesKind
    namespace: KubernetesName
    name: KubernetesName

    @field_validator("kind")
    @classmethod
    def reject_secret_resources(cls, value: str) -> str:
        if value.lower() == "secret":
            raise ValueError(
                "Secret resources are not operation inputs; use secret_requirements metadata"
            )
        return value


class SecretRequirement(ContractModel):
    """A secret metadata/key requirement; secret values are intentionally unrepresentable."""

    namespace: KubernetesName
    name: KubernetesName
    keys: tuple[SecretKeyName, ...] = Field(min_length=1, max_length=50)

    @field_validator("keys", mode="before")
    @classmethod
    def freeze_keys(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("keys")
    @classmethod
    def validate_unique_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("secret requirement keys must not contain duplicates")
        return values


class ReadinessProbe(ContractModel):
    """A finite, typed readiness condition for one expected resource."""

    probe: Literal["deployment_available", "statefulset_ready", "job_complete"]
    resource: ExpectedResourceIdentity

    @model_validator(mode="after")
    def validate_resource_kind(self) -> Self:
        expected_kinds = {
            "deployment_available": ("apps/v1", "Deployment"),
            "statefulset_ready": ("apps/v1", "StatefulSet"),
            "job_complete": ("batch/v1", "Job"),
        }
        if (self.resource.api_version, self.resource.kind) != expected_kinds[self.probe]:
            raise ValueError("readiness probe type must match its typed resource identity")
        return self


def _resource_key(resource: ExpectedResourceIdentity) -> tuple[str, str, str, str]:
    return (resource.api_version, resource.kind, resource.namespace, resource.name)


def _path_is_within(path: PurePosixPath, root: PurePosixPath) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class ClusterActionBase(ContractModel):
    """Shared explicit bounds and outcome expectations for every supported action."""

    namespace: KubernetesName
    timeout_seconds: Annotated[int, Field(ge=1, le=3_600)]
    expected_resources: tuple[ExpectedResourceIdentity, ...] = Field(min_length=1, max_length=50)
    readiness_probes: tuple[ReadinessProbe, ...] = Field(min_length=1, max_length=50)

    @field_validator("expected_resources", "readiness_probes", mode="before")
    @classmethod
    def freeze_collections(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_expected_resources(self) -> Self:
        keys = [_resource_key(resource) for resource in self.expected_resources]
        if len(keys) != len(set(keys)):
            raise ValueError("expected_resources must not contain duplicates")
        if any(resource.namespace != self.namespace for resource in self.expected_resources):
            raise ValueError("expected_resources must use the action namespace")
        if any(_resource_key(probe.resource) not in set(keys) for probe in self.readiness_probes):
            raise ValueError("readiness probes must reference expected_resources")
        return self


class KubectlServerDryRunAction(ClusterActionBase):
    """A future fixed kubectl server-side dry-run over declared manifest files."""

    action: Literal["kubectl_server_dry_run"]
    manifest_files: tuple[ApprovalSnapshotFile, ...] = Field(min_length=1, max_length=50)

    @field_validator("manifest_files", mode="before")
    @classmethod
    def freeze_manifest_files(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class HelmUpgradeInstallAction(ClusterActionBase):
    """A future fixed Helm upgrade/install over a local locked chart and values files."""

    action: Literal["helm_upgrade_install"]
    release: KubernetesName
    chart_path: str = Field(min_length=1, max_length=500)
    chart_lock_file: ApprovalSnapshotFile
    values_files: tuple[ApprovalSnapshotFile, ...] = Field(min_length=1, max_length=50)

    @field_validator("chart_path")
    @classmethod
    def validate_chart_path(cls, value: str) -> str:
        return validate_normalized_relative_path(value, "chart_path")

    @field_validator("values_files", mode="before")
    @classmethod
    def freeze_values_files(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_local_locked_chart(self) -> Self:
        chart = PurePosixPath(self.chart_path)
        lock = PurePosixPath(self.chart_lock_file.path)
        if not _path_is_within(lock, chart):
            raise ValueError("chart_lock_file must be inside chart_path")
        value_paths = [item.path for item in self.values_files]
        if len(value_paths) != len(set(value_paths)):
            raise ValueError("values_files must not contain duplicates")
        return self


class PortForwardAction(ContractModel):
    """A bounded dispatcher-owned loopback forward to one exact core Service."""

    action: Literal["port_forward"]
    action_id: Identifier
    namespace: KubernetesName
    expected_resources: tuple[ExpectedResourceIdentity, ...] = Field(min_length=1, max_length=50)
    resource: ExpectedResourceIdentity
    local_port: Annotated[int, Field(ge=1024, le=65535)]
    remote_port: Annotated[int, Field(ge=1, le=65535)]
    startup_timeout_seconds: Annotated[int, Field(ge=1, le=300)]
    probe_timeout_seconds: Annotated[int, Field(ge=1, le=300)]
    lifetime_timeout_seconds: Annotated[int, Field(ge=1, le=600)]

    @field_validator("expected_resources", mode="before")
    @classmethod
    def freeze_expected_resources(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_port_forward_resource(self) -> Self:
        keys = [_resource_key(item) for item in self.expected_resources]
        if len(keys) != len(set(keys)):
            raise ValueError("port_forward expected_resources must not contain duplicates")
        if any(resource.namespace != self.namespace for resource in self.expected_resources):
            raise ValueError("port_forward expected_resources must use the action namespace")
        if self.resource.namespace != self.namespace:
            raise ValueError("port_forward resource must use the action namespace")
        if (self.resource.api_version, self.resource.kind) != ("v1", "Service"):
            raise ValueError("port_forward resource must be an exact core v1 Service")
        if set(keys) != {_resource_key(self.resource)}:
            raise ValueError("port_forward expected_resources must contain only its exact Service")
        return self


class TlsDc8NoClientCertificateProbeAction(ContractModel):
    """Require direct loopback TLS rejection when no client certificate is presented."""

    action: Literal["tls_dc8_no_client_certificate_rejection"]
    port_forward_action_id: Identifier


ClusterAction = Annotated[
    KubectlServerDryRunAction
    | HelmUpgradeInstallAction
    | PortForwardAction
    | TlsDc8NoClientCertificateProbeAction,
    Field(discriminator="action"),
]


class AutomaticRollbackIntent(ContractModel):
    """Mandatory future rollback policy, bound to the later approval snapshot."""

    automatic: Literal[True]
    strategy: Literal["restore_approval_snapshot"]


class ClusterOperationManifest(ContractModel):
    """Strict, static, repository-owned declaration of future cluster operations."""

    schema_version: Literal[1]
    operation_id: Identifier
    context: KubernetesContext
    source_identity: SourceIdentityBinding
    allowed_namespaces: tuple[KubernetesName, ...] = Field(min_length=1, max_length=20)
    allowed_files: tuple[ApprovalSnapshotFile, ...] = Field(min_length=1, max_length=100)
    secret_requirements: tuple[SecretRequirement, ...] = ()
    actions: tuple[ClusterAction, ...] = Field(min_length=1, max_length=20)
    rollback: AutomaticRollbackIntent

    @field_validator(
        "allowed_namespaces",
        "allowed_files",
        "secret_requirements",
        "actions",
        mode="before",
    )
    @classmethod
    def freeze_collections(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_declared_scope(self) -> Self:
        if len(self.allowed_namespaces) != len(set(self.allowed_namespaces)):
            raise ValueError("allowed_namespaces must not contain duplicates")
        allowed_file_paths = [item.path for item in self.allowed_files]
        if len(allowed_file_paths) != len(set(allowed_file_paths)):
            raise ValueError("allowed_files must not contain duplicate paths")
        namespaces = set(self.allowed_namespaces)
        for requirement in self.secret_requirements:
            if requirement.namespace not in namespaces:
                raise ValueError("secret_requirements namespaces must be in allowed_namespaces")
        for action in self.actions:
            if isinstance(action, TlsDc8NoClientCertificateProbeAction):
                continue
            if action.namespace not in namespaces:
                raise ValueError("action namespace must be in allowed_namespaces")
        port_forwards = [action for action in self.actions if isinstance(action, PortForwardAction)]
        port_forward_ids = [action.action_id for action in port_forwards]
        if len(port_forward_ids) != len(set(port_forward_ids)):
            raise ValueError("port_forward action_id values must not repeat")
        local_ports = [action.local_port for action in port_forwards]
        if len(local_ports) != len(set(local_ports)):
            raise ValueError("port_forward local_port values must not repeat")
        forward_indexes = {
            action.action_id: index for index, action in enumerate(self.actions) if isinstance(action, PortForwardAction)
        }
        probes = [
            action
            for action in self.actions
            if isinstance(action, TlsDc8NoClientCertificateProbeAction)
        ]
        probe_links = [action.port_forward_action_id for action in probes]
        if len(probe_links) != len(set(probe_links)):
            raise ValueError("TLS/DC8 probes must not repeat a port_forward action_id")
        if set(probe_links) != set(port_forward_ids):
            raise ValueError("every port_forward requires exactly one linked TLS/DC8 probe")
        for probe in probes:
            forward_index = forward_indexes.get(probe.port_forward_action_id)
            if forward_index is None:
                raise ValueError("TLS/DC8 probe references an unknown port_forward action_id")
            if forward_index + 1 >= len(self.actions) or self.actions[forward_index + 1] != probe:
                raise ValueError("TLS/DC8 probe must immediately follow its linked port_forward")
        if port_forwards:
            first_forward = min(forward_indexes[action.action_id] for action in port_forwards)
            if any(
                not isinstance(action, (PortForwardAction, TlsDc8NoClientCertificateProbeAction))
                for action in self.actions[first_forward:]
            ):
                raise ValueError("port_forward/TLS actions must be trailing action pairs")
        return self


@dataclass(frozen=True)
class ValidatedClusterOperation:
    """One fully aligned static operation manifest for a normalized plan step."""

    step_id: str
    target_name: str
    repository_root: Path
    manifest_path: Path
    manifest: ClusterOperationManifest


def load_cluster_operation_manifest(path: str | Path) -> ClusterOperationManifest:
    """Load one strict manifest without reading referenced source or secret content."""
    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.is_file():
        raise ClusterOperationError(f"cluster operation manifest not found: {manifest_path}")
    try:
        raw = load_unique_yaml(manifest_path)
    except (DuplicateYamlKeyError, yaml.YAMLError) as exc:
        raise ClusterOperationError(f"invalid cluster operation YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ClusterOperationError("cluster operation manifest must be a YAML mapping")
    try:
        return ClusterOperationManifest.model_validate(raw)
    except ValidationError as exc:
        raise ClusterOperationError(_format_validation_error(exc)) from exc


def validate_cluster_operations_for_plan(
    *, config: Config, plan: NormalizedPlan
) -> dict[str, ValidatedClusterOperation]:
    """Fully validate committed manifests and files before a future approval snapshot.

    Call ``validate_cluster_operation_references_for_plan`` at plan admission.
    This post-commit API deliberately fails when the executor has not yet created
    the referenced manifest or its declared repository files.
    """
    validate_cluster_operation_references_for_plan(config=config, plan=plan)
    references = [step for step in plan.steps if step.cluster_operation is not None]

    validated: dict[str, ValidatedClusterOperation] = {}
    for step in references:
        reference = step.cluster_operation
        assert reference is not None
        assert config.cluster_mutation is not None
        target = config.cluster_mutation.targets[reference.target_name]
        repository_root = config.repository_root(step.repo_id).resolve()
        manifest_path = _resolve_repository_path(
            repository_root,
            reference.operation_manifest_path,
            "operation_manifest_path",
        )
        if not _path_in_roots(
            reference.operation_manifest_path, target.operation_manifest_roots
        ) or not _resolved_path_in_roots(
            repository_root,
            manifest_path,
            target.operation_manifest_roots,
        ):
            raise ClusterOperationError(
                f"step {step.step_id} operation_manifest_path is outside target operation_manifest_roots"
            )
        manifest = load_cluster_operation_manifest(manifest_path)
        _validate_manifest_alignment(
            manifest=manifest,
            target=target,
            repository_root=repository_root,
            step_id=step.step_id,
            repo_id=step.repo_id,
        )
        if tuple(action.action for action in manifest.actions) != reference.preauthorized_actions:
            raise ClusterOperationError(
                f"step {step.step_id} manifest actions do not match the preauthorized action order"
            )
        if manifest.rollback.automatic is not reference.requires_automatic_rollback:
            raise ClusterOperationError(
                f"step {step.step_id} manifest rollback does not match the automatic rollback requirement"
            )
        validated[step.step_id] = ValidatedClusterOperation(
            step_id=step.step_id,
            target_name=reference.target_name,
            repository_root=repository_root,
            manifest_path=manifest_path,
            manifest=manifest,
        )
    return validated


def validate_validated_cluster_operation(
    *, config: Config, operation: ValidatedClusterOperation
) -> ClusterMutationTargetDefinition:
    """Revalidate one post-commit operation before a snapshot or execution boundary.

    ``ValidatedClusterOperation`` deliberately keeps filesystem paths outside the
    Pydantic manifest. Re-read the manifest here so an object retained by a
    caller cannot be used after its repository file or target binding changes.
    """
    if config.cluster_mutation is None:
        raise ClusterOperationError("cluster mutation configuration is required")
    try:
        target = config.cluster_mutation.targets[operation.target_name]
    except KeyError as exc:
        raise ClusterOperationError("cluster operation target is no longer configured") from exc

    repository_id = operation.manifest.source_identity.repository_id
    repository_root = config.repository_root(repository_id).resolve()
    if operation.repository_root.resolve() != repository_root:
        raise ClusterOperationError(
            "validated operation repository root does not match configured repository"
        )
    manifest_path = operation.manifest_path.resolve()
    try:
        manifest_relative_path = manifest_path.relative_to(repository_root).as_posix()
    except ValueError as exc:
        raise ClusterOperationError(
            "validated operation manifest path escapes its repository"
        ) from exc
    if not _path_in_roots(
        manifest_relative_path, target.operation_manifest_roots
    ) or not _resolved_path_in_roots(
        repository_root, manifest_path, target.operation_manifest_roots
    ):
        raise ClusterOperationError("validated operation manifest path is outside target roots")

    manifest = load_cluster_operation_manifest(manifest_path)
    if manifest != operation.manifest:
        raise ClusterOperationError(
            "validated operation manifest no longer matches its static validation"
        )
    _validate_manifest_alignment(
        manifest=manifest,
        target=target,
        repository_root=repository_root,
        step_id=operation.step_id,
        repo_id=repository_id,
    )
    return target


def validate_cluster_operation_references_for_plan(*, config: Config, plan: NormalizedPlan) -> None:
    """Admit only static plan references without requiring executor-created files.

    This intentionally validates no manifest content or filesystem existence.
    The full ``validate_cluster_operations_for_plan`` call remains mandatory
    after the exact repository revision containing the manifest is committed.
    """
    references = [step for step in plan.steps if step.cluster_operation is not None]
    if not references:
        return
    if config.cluster_mutation is None:
        raise ClusterOperationError("cluster_operation requires cluster_mutation configuration")
    if config.cluster_preflight is None:
        raise ClusterOperationError("cluster_mutation requires cluster_preflight")

    preflight = config.cluster_preflight
    for step in references:
        reference = step.cluster_operation
        assert reference is not None
        try:
            target = config.cluster_mutation.targets[reference.target_name]
        except KeyError as exc:
            raise ClusterOperationError(
                f"step {step.step_id} cluster_operation references unknown target {reference.target_name}"
            ) from exc
        if target.preflight_target_id != preflight.target_id or target.context != preflight.context:
            raise ClusterOperationError(
                f"step {step.step_id} cluster target does not match its read-only preflight"
            )
        if step.repo_id not in target.allowed_repository_ids:
            raise ClusterOperationError(
                f"step {step.step_id} repository is not allowed by cluster target {reference.target_name}"
            )
        if not _path_in_roots(reference.operation_manifest_path, target.operation_manifest_roots):
            raise ClusterOperationError(
                f"step {step.step_id} operation_manifest_path is outside target operation_manifest_roots"
            )


def _validate_manifest_alignment(
    *,
    manifest: ClusterOperationManifest,
    target: ClusterMutationTargetDefinition,
    repository_root: Path,
    step_id: str,
    repo_id: str,
) -> None:
    if manifest.context != target.context:
        raise ClusterOperationError(
            f"step {step_id} manifest context does not match cluster target"
        )
    if manifest.source_identity.repository_id != repo_id:
        raise ClusterOperationError(
            f"step {step_id} manifest source_identity.repository_id mismatch"
        )

    declared_files = {item.path: item for item in manifest.allowed_files}
    for file_reference in manifest.allowed_files:
        _validate_source_file(
            repository_root=repository_root,
            path=file_reference.path,
            target=target,
            field="allowed_files",
        )
    for action in manifest.actions:
        if isinstance(action, TlsDc8NoClientCertificateProbeAction):
            continue
        if isinstance(action, PortForwardAction):
            if max(
                action.startup_timeout_seconds,
                action.probe_timeout_seconds,
                action.lifetime_timeout_seconds,
            ) > target.max_action_timeout_seconds:
                raise ClusterOperationError(f"step {step_id} port_forward timeout exceeds target maximum")
            continue
        if action.timeout_seconds > target.max_action_timeout_seconds:
            raise ClusterOperationError(f"step {step_id} action timeout exceeds target maximum")
        if isinstance(action, KubectlServerDryRunAction):
            _require_declared_files(action.manifest_files, declared_files, step_id)
        elif isinstance(action, HelmUpgradeInstallAction):
            chart_path = _resolve_repository_path(repository_root, action.chart_path, "chart_path")
            if not _path_in_roots(
                action.chart_path, target.source_file_roots
            ) or not _resolved_path_in_roots(
                repository_root,
                chart_path,
                target.source_file_roots,
            ):
                raise ClusterOperationError(
                    f"step {step_id} chart_path is outside target source_file_roots"
                )
            if not chart_path.is_dir():
                raise ClusterOperationError(
                    f"step {step_id} chart_path must be an existing directory"
                )
            _require_declared_files(
                (action.chart_lock_file, *action.values_files), declared_files, step_id
            )

    helm_releases = [
        (action.namespace, action.release)
        for action in manifest.actions
        if isinstance(action, HelmUpgradeInstallAction)
    ]
    if len(helm_releases) != len(set(helm_releases)):
        raise ClusterOperationError(f"step {step_id} Helm namespace/release pairs must not repeat")


def _require_declared_files(
    references: tuple[ApprovalSnapshotFile, ...],
    declared_files: dict[str, ApprovalSnapshotFile],
    step_id: str,
) -> None:
    for reference in references:
        declared = declared_files.get(reference.path)
        if declared is None or declared.sha256 != reference.sha256:
            raise ClusterOperationError(
                f"step {step_id} action file must appear exactly in allowed_files"
            )


def _validate_source_file(
    *,
    repository_root: Path,
    path: str,
    target: ClusterMutationTargetDefinition,
    field: str,
) -> None:
    if not _path_in_roots(path, target.source_file_roots):
        raise ClusterOperationError(f"{field} path is outside target source_file_roots")
    resolved = _resolve_repository_path(repository_root, path, field)
    if not _resolved_path_in_roots(repository_root, resolved, target.source_file_roots):
        raise ClusterOperationError(f"{field} path is outside target source_file_roots")
    if not resolved.is_file():
        raise ClusterOperationError(f"{field} path must be an existing regular file")


def _resolve_repository_path(repository_root: Path, relative_path: str, field: str) -> Path:
    resolved = (repository_root / relative_path).resolve()
    try:
        resolved.relative_to(repository_root)
    except ValueError as exc:
        raise ClusterOperationError(f"{field} escapes the target repository") from exc
    return resolved


def _path_in_roots(path: str, roots: tuple[str, ...]) -> bool:
    candidate = PurePosixPath(path)
    return any(_path_is_within(candidate, PurePosixPath(root)) for root in roots)


def _resolved_path_in_roots(repository_root: Path, path: Path, roots: tuple[str, ...]) -> bool:
    return any(
        path.is_relative_to(_resolve_repository_path(repository_root, root, "cluster target root"))
        for root in roots
    )


def _format_validation_error(exc: ValidationError) -> str:
    errors = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        errors.append(f"{location}: {error['msg']}")
    return "invalid cluster operation manifest: " + "; ".join(errors)
