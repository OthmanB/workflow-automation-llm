"""Dispatcher-owned, read-only approval snapshots for cluster operations.

This module is deliberately separate from ``ClusterOperationRunner.execute``.
It accepts only an injected tuple-argv runner; the CLI explicitly injects the
small local subprocess adapter when an operator asks to capture a snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from .cluster_operation_lifecycle import (
    ApprovedSourceFileDigest,
    ClusterOperationApprovalSnapshot,
    ClusterOperationLifecycleError,
    ClusterResourceFingerprint,
    HelmReleaseRollbackSnapshot,
    NamedDigest,
    ReleaseFingerprint,
    SecretMetadataFingerprint,
    assert_cluster_operation_safe_payload,
    create_auto_approved_cluster_operation_approval,
)
from .cluster_operations import (
    ClusterOperationError,
    HelmUpgradeInstallAction,
    SecretRequirement,
    TlsDc8NoClientCertificateProbeAction,
    ValidatedClusterOperation,
    validate_validated_cluster_operation,
)
from .config import ClusterMutationTargetDefinition, Config
from .operation import RealOperationApproval

_MAX_COMMAND_OUTPUT_BYTES = 65_536
_SAFE_METADATA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_/-]{0,127}$")
_SAFE_UID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRET_KEY = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
_CHART_VERSION = re.compile(r"^.*-((?:v)?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)$")
_RESOURCE_JSONPATH = (
    r"jsonpath={.apiVersion}{'\t'}{.kind}{'\t'}{.metadata.namespace}{'\t'}"
    r"{.metadata.name}{'\t'}{.metadata.uid}{'\t'}{.metadata.resourceVersion}"
)
_SECRET_METADATA_TEMPLATE = (
    'go-template={{.metadata.uid}}{{"\\t"}}{{.metadata.resourceVersion}}{{"\\t"}}'
    '{{.type}}{{"\\t"}}{{range $key, $_ := .data}}{{$key}}{{","}}{{end}}'
)


class ClusterOperationSnapshotError(RuntimeError):
    """A read-only snapshot could not be safely captured or bound."""


@dataclass(frozen=True)
class ClusterOperationSnapshotCommandResult:
    """Bounded result returned by the collector's injected fixed-argv runner."""

    returncode: int
    stdout: bytes
    stderr: bytes = b""


ClusterOperationSnapshotCommandRunner = Callable[
    [tuple[str, ...], int], ClusterOperationSnapshotCommandResult
]


def capture_cluster_operation_snapshot(
    *,
    config: Config,
    operation: ValidatedClusterOperation,
    source_revision: str,
    real_operation_approval: RealOperationApproval,
    tier1_invariant_snapshot_digest: str,
    command_runner: ClusterOperationSnapshotCommandRunner,
    target: ClusterMutationTargetDefinition | None = None,
    now: datetime | None = None,
) -> ClusterOperationApprovalSnapshot:
    """Capture one strictly bounded read-only snapshot for a committed operation.

    All filesystem, envelope, and binary checks complete before the first
    command. The runner is mandatory so library callers cannot accidentally
    acquire a subprocess or network fallback.
    """
    captured_at = now or datetime.now(UTC)
    _require_utc(captured_at)
    _require_sha256(tier1_invariant_snapshot_digest, "Tier 1 invariant snapshot digest")

    try:
        configured_target = validate_validated_cluster_operation(config=config, operation=operation)
    except ClusterOperationError as exc:
        raise ClusterOperationSnapshotError("full static validation failed") from exc
    if target is not None and target != configured_target:
        raise ClusterOperationSnapshotError(
            "provided target does not match configured operation target"
        )
    if config.cluster_preflight is None:
        raise ClusterOperationSnapshotError("cluster preflight configuration is required")
    if real_operation_approval.project_id != config.project_id:
        raise ClusterOperationSnapshotError(
            "real-operation approval project does not match configuration"
        )
    if real_operation_approval.config_digest != config.config_digest:
        raise ClusterOperationSnapshotError(
            "real-operation approval config does not match configuration"
        )

    # This validates the exact envelope before any file hash or cluster command.
    try:
        envelope_approval = create_auto_approved_cluster_operation_approval(
            operation,
            source_revision,
            real_operation_approval,
            now=captured_at,
        )
    except ClusterOperationLifecycleError as exc:
        raise ClusterOperationSnapshotError("preauthorized envelope validation failed") from exc
    if envelope_approval.envelope_digest is None:
        raise ClusterOperationSnapshotError("preauthorized envelope digest is unavailable")

    tool_digests = _verified_tool_digests(configured_target)
    source_file_digests = _source_file_digests(operation)
    preflight_digest = _capture_preflight(
        config=config,
        target=configured_target,
        command_runner=command_runner,
    )
    resource_fingerprints = _capture_resource_fingerprints(
        operation=operation,
        target=configured_target,
        timeout_seconds=config.cluster_preflight.request_timeout_seconds,
        command_runner=command_runner,
    )
    secret_fingerprints = _capture_secret_fingerprints(
        operation=operation,
        target=configured_target,
        timeout_seconds=config.cluster_preflight.request_timeout_seconds,
        command_runner=command_runner,
    )
    release_fingerprints, rollback_snapshots = _capture_release_fingerprints(
        operation=operation,
        target=configured_target,
        timeout_seconds=config.cluster_preflight.request_timeout_seconds,
        command_runner=command_runner,
    )

    # Detect source or binary replacement while a read-only command was in flight.
    if _source_file_digests(operation) != source_file_digests:
        raise ClusterOperationSnapshotError("declared source files changed during snapshot capture")
    if _verified_tool_digests(configured_target) != tool_digests:
        raise ClusterOperationSnapshotError(
            "configured tool binaries changed during snapshot capture"
        )

    expires_at = min(
        captured_at + timedelta(seconds=configured_target.max_snapshot_age_seconds),
        envelope_approval.expires_at,
    )
    return ClusterOperationApprovalSnapshot(
        run_id=envelope_approval.run_id,
        step_id=envelope_approval.step_id,
        operation_id=envelope_approval.operation_id,
        source_revision=source_revision,
        plan_digest=real_operation_approval.plan_digest,
        config_digest=config.config_digest,
        validated_manifest_digest=_canonical_digest(operation.manifest.model_dump(mode="json")),
        envelope_digest=envelope_approval.envelope_digest,
        cluster_preflight_result_digest=preflight_digest,
        binary_identity_digests=tool_digests,
        toolchain_identity_digests=tool_digests,
        tier1_invariant_snapshot_digest=tier1_invariant_snapshot_digest,
        action_digests=envelope_approval.allowed_actions,
        source_file_digests=source_file_digests,
        resource_fingerprints=resource_fingerprints,
        release_fingerprints=release_fingerprints,
        release_rollback_snapshots=rollback_snapshots,
        image_fingerprints=(),
        secret_metadata_fingerprints=secret_fingerprints,
        captured_at=captured_at,
        expires_at=expires_at,
    )


def run_cluster_operation_snapshot_subprocess(
    argv: tuple[str, ...], timeout_seconds: int
) -> ClusterOperationSnapshotCommandResult:
    """Run collector-generated argv without a shell and with hard output bounds.

    This adapter is intentionally not a fallback inside
    ``capture_cluster_operation_snapshot``. The local-only CLI injects it after
    all local journal and static checks have passed.
    """
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    outputs: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=2)

    def read_stream(name: str, stream: Any) -> None:
        try:
            output = stream.read(_MAX_COMMAND_OUTPUT_BYTES + 1)
            outputs.put((name, output))
            if len(output) > _MAX_COMMAND_OUTPUT_BYTES:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        finally:
            stream.close()

    readers = [
        threading.Thread(target=read_stream, args=(name, stream), daemon=True)
        for name, stream in streams.items()
        if stream is not None
    ]
    for reader in readers:
        reader.start()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        for reader in readers:
            reader.join()
    result = {name: output for name, output in (outputs.get() for _ in readers)}
    stdout = result.get("stdout", b"")
    stderr = result.get("stderr", b"")
    if len(stdout) > _MAX_COMMAND_OUTPUT_BYTES or len(stderr) > _MAX_COMMAND_OUTPUT_BYTES:
        raise ClusterOperationSnapshotError("fixed read-only command returned unbounded output")
    if process.returncode is None:
        raise ClusterOperationSnapshotError("fixed read-only command did not complete")
    return ClusterOperationSnapshotCommandResult(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _capture_preflight(
    *,
    config: Config,
    target: ClusterMutationTargetDefinition,
    command_runner: ClusterOperationSnapshotCommandRunner,
) -> str:
    definition = config.cluster_preflight
    assert definition is not None
    timeout_seconds = definition.request_timeout_seconds
    kubectl = target.toolchain.kubectl.path
    context = _required_success(
        _run(command_runner, (kubectl, "config", "current-context"), timeout_seconds),
        "kubectl current context",
    )
    if _safe_text(context.stdout).strip() != target.context:
        raise ClusterOperationSnapshotError(
            "active kubectl context does not match configured target"
        )
    version = _required_success(
        _run(
            command_runner,
            (
                kubectl,
                "--context",
                target.context,
                f"--request-timeout={timeout_seconds}s",
                "version",
                "--output=json",
            ),
            timeout_seconds,
        ),
        "kubectl version",
    )
    payload = _safe_json(version.stdout)
    if not isinstance(payload, dict):
        raise ClusterOperationSnapshotError("kubectl version metadata is invalid")
    try:
        client_version = payload["clientVersion"]["gitVersion"]
        server_version = payload["serverVersion"]["gitVersion"]
    except (KeyError, TypeError) as exc:
        raise ClusterOperationSnapshotError("kubectl version metadata is invalid") from exc

    try:
        from .cluster_preflight import _compare_semver, _parse_kubernetes_semver

        client = _parse_kubernetes_semver(client_version)
        server = _parse_kubernetes_semver(server_version)
        minimum_client = _parse_kubernetes_semver(definition.minimum_client_version)
        minimum_server = _parse_kubernetes_semver(definition.minimum_server_version)
    except ValueError as exc:
        raise ClusterOperationSnapshotError("kubectl version metadata is invalid") from exc
    if _compare_semver(client, minimum_client) < 0 or _compare_semver(server, minimum_server) < 0:
        raise ClusterOperationSnapshotError(
            "kubectl version is below the configured readiness floor"
        )
    if client.major != server.major or abs(client.minor - server.minor) > 1:
        raise ClusterOperationSnapshotError(
            "kubectl client/server versions are outside the supported skew"
        )
    return _canonical_digest(
        {
            "preflight_target_id": definition.target_id,
            "context": target.context,
            "kubectl_client_version": client.value,
            "kubectl_server_version": server.value,
            "minimum_client_version": minimum_client.value,
            "minimum_server_version": minimum_server.value,
        }
    )


def _capture_resource_fingerprints(
    *,
    operation: ValidatedClusterOperation,
    target: ClusterMutationTargetDefinition,
    timeout_seconds: int,
    command_runner: ClusterOperationSnapshotCommandRunner,
) -> tuple[ClusterResourceFingerprint, ...]:
    resources = {
        (resource.api_version, resource.kind, resource.namespace, resource.name): resource
        for action in operation.manifest.actions
        if not isinstance(action, TlsDc8NoClientCertificateProbeAction)
        for resource in action.expected_resources
    }
    fingerprints: list[ClusterResourceFingerprint] = []
    for _key, resource in sorted(resources.items()):
        result = _required_success(
            _run(
                command_runner,
                (
                    target.toolchain.kubectl.path,
                    "--context",
                    target.context,
                    "--namespace",
                    resource.namespace,
                    f"--request-timeout={timeout_seconds}s",
                    "get",
                    f"{resource.kind.lower()}/{resource.name}",
                    f"--output={_RESOURCE_JSONPATH}",
                ),
                timeout_seconds,
            ),
            "declared resource metadata",
        )
        actual = _safe_tab_fields(result.stdout, 6, "declared resource metadata")
        if actual[:4] != (resource.api_version, resource.kind, resource.namespace, resource.name):
            raise ClusterOperationSnapshotError(
                "declared resource metadata does not match its static identity"
            )
        uid, resource_version = actual[4:]
        if _SAFE_UID.fullmatch(uid) is None or _SAFE_METADATA.fullmatch(resource_version) is None:
            raise ClusterOperationSnapshotError("declared resource metadata is unsafe")
        normalized: dict[str, object] = {
            "api_version": resource.api_version,
            "kind": resource.kind,
            "namespace": resource.namespace,
            "name": resource.name,
            "uid": uid,
            "resource_version": resource_version,
        }
        fingerprints.append(
            ClusterResourceFingerprint(resource=resource, sha256=_canonical_digest(normalized))
        )
    return tuple(fingerprints)


def _capture_secret_fingerprints(
    *,
    operation: ValidatedClusterOperation,
    target: ClusterMutationTargetDefinition,
    timeout_seconds: int,
    command_runner: ClusterOperationSnapshotCommandRunner,
) -> tuple[SecretMetadataFingerprint, ...]:
    requirements: dict[tuple[str, str], SecretRequirement] = {}
    for requirement in operation.manifest.secret_requirements:
        key = (requirement.namespace, requirement.name)
        if key in requirements:
            raise ClusterOperationSnapshotError(
                "static manifest repeats a secret metadata requirement"
            )
        requirements[key] = requirement

    fingerprints: list[SecretMetadataFingerprint] = []
    for _key, requirement in sorted(requirements.items()):
        result = _required_success(
            _run(
                command_runner,
                (
                    target.toolchain.kubectl.path,
                    "--context",
                    target.context,
                    "--namespace",
                    requirement.namespace,
                    f"--request-timeout={timeout_seconds}s",
                    "get",
                    f"secret/{requirement.name}",
                    f"--output={_SECRET_METADATA_TEMPLATE}",
                ),
                timeout_seconds,
            ),
            "secret metadata",
        )
        uid, resource_version, secret_type, key_names = _safe_tab_fields(
            result.stdout, 4, "secret metadata"
        )
        keys = tuple(sorted(name for name in key_names.removesuffix(",").split(",") if name))
        if (
            not keys
            or len(keys) > 50
            or len(set(keys)) != len(keys)
            or any(_SECRET_KEY.fullmatch(key) is None for key in keys)
            or _SAFE_UID.fullmatch(uid) is None
            or _SAFE_METADATA.fullmatch(resource_version) is None
            or _SAFE_METADATA.fullmatch(secret_type) is None
        ):
            raise ClusterOperationSnapshotError("secret metadata output is unsafe")
        if keys != tuple(sorted(requirement.keys)):
            raise ClusterOperationSnapshotError(
                "secret metadata keys do not match the static requirement"
            )
        normalized = {
            "namespace": requirement.namespace,
            "name": requirement.name,
            "uid": uid,
            "resource_version": resource_version,
            "type": secret_type,
            "keys": keys,
        }
        fingerprints.append(
            SecretMetadataFingerprint(
                namespace=requirement.namespace,
                name=requirement.name,
                keys=keys,
                sha256=_canonical_digest(normalized),
            )
        )
    return tuple(fingerprints)


def _capture_release_fingerprints(
    *,
    operation: ValidatedClusterOperation,
    target: ClusterMutationTargetDefinition,
    timeout_seconds: int,
    command_runner: ClusterOperationSnapshotCommandRunner,
) -> tuple[tuple[ReleaseFingerprint, ...], tuple[HelmReleaseRollbackSnapshot, ...]]:
    releases: list[ReleaseFingerprint] = []
    rollbacks: list[HelmReleaseRollbackSnapshot] = []
    for action in operation.manifest.actions:
        if not isinstance(action, HelmUpgradeInstallAction):
            continue
        status = _run(
            command_runner,
            (
                target.toolchain.helm.path,
                "status",
                action.release,
                "--namespace",
                action.namespace,
                "--kube-context",
                target.context,
                "--output=json",
            ),
            timeout_seconds,
        )
        history = _run(
            command_runner,
            (
                target.toolchain.helm.path,
                "history",
                action.release,
                "--namespace",
                action.namespace,
                "--kube-context",
                target.context,
                "--output=json",
            ),
            timeout_seconds,
        )
        if status.returncode != 0:
            if not (_is_helm_not_found(status) and _is_helm_not_found(history)):
                raise ClusterOperationSnapshotError(
                    "Helm release state is unavailable or unexpected"
                )
            normalized = {
                "namespace": action.namespace,
                "release": action.release,
                "pre_snapshot_state": "new",
                "pre_snapshot_revision": None,
                "chart_version": None,
                "app_version": None,
                "status": None,
            }
            releases.append(
                ReleaseFingerprint(
                    namespace=action.namespace,
                    release=action.release,
                    pre_snapshot_state="new",
                    sha256=_canonical_digest(normalized),
                )
            )
            rollbacks.append(
                HelmReleaseRollbackSnapshot(
                    namespace=action.namespace,
                    release=action.release,
                    pre_snapshot_state="new",
                )
            )
            continue
        if history.returncode != 0:
            raise ClusterOperationSnapshotError("Helm release history is unavailable")
        revision, chart_version, app_version, release_status = _existing_release_metadata(
            status.stdout,
            history.stdout,
            action,
        )
        existing_normalized: dict[str, object] = {
            "namespace": action.namespace,
            "release": action.release,
            "pre_snapshot_state": "existing",
            "pre_snapshot_revision": revision,
            "chart_version": chart_version,
            "app_version": app_version,
            "status": release_status,
        }
        releases.append(
            ReleaseFingerprint(
                namespace=action.namespace,
                release=action.release,
                pre_snapshot_state="existing",
                pre_snapshot_revision=revision,
                chart_version=chart_version,
                app_version=app_version,
                status=release_status,
                sha256=_canonical_digest(existing_normalized),
            )
        )
        rollbacks.append(
            HelmReleaseRollbackSnapshot(
                namespace=action.namespace,
                release=action.release,
                pre_snapshot_state="existing",
                pre_snapshot_revision=revision,
            )
        )
    return (
        tuple(sorted(releases, key=lambda item: (item.namespace, item.release))),
        tuple(sorted(rollbacks, key=lambda item: (item.namespace, item.release))),
    )


def _existing_release_metadata(
    status_output: bytes,
    history_output: bytes,
    action: HelmUpgradeInstallAction,
) -> tuple[int, str, str, Literal["deployed"]]:
    status_payload = _safe_json(status_output)
    history_payload = _safe_json(history_output)
    if (
        not isinstance(status_payload, dict)
        or not isinstance(history_payload, list)
        or not history_payload
    ):
        raise ClusterOperationSnapshotError("Helm release metadata is invalid")
    if (
        status_payload.get("name") != action.release
        or status_payload.get("namespace") != action.namespace
    ):
        raise ClusterOperationSnapshotError(
            "Helm release metadata does not match the static action"
        )
    revision = status_payload.get("version", status_payload.get("revision"))
    status = _release_status(status_payload)
    chart_version, app_version = _status_chart_versions(status_payload)
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or status != "deployed"
    ):
        raise ClusterOperationSnapshotError("Helm release has an unexpected state")
    history_entries = [item for item in history_payload if isinstance(item, dict)]
    if len(history_entries) != len(history_payload):
        raise ClusterOperationSnapshotError("Helm release history is invalid")
    matching = [item for item in history_entries if item.get("revision") == revision]
    if len(matching) != 1:
        raise ClusterOperationSnapshotError("Helm release history does not match release revision")
    history = matching[0]
    history_chart = _chart_version_from_history(history.get("chart"))
    history_app = _safe_metadata(history.get("app_version", history.get("appVersion")))
    if (
        history.get("status") != status
        or history_chart != chart_version
        or history_app != app_version
    ):
        raise ClusterOperationSnapshotError("Helm release status and history metadata do not match")
    return revision, chart_version, app_version, "deployed"


def _release_status(payload: dict[str, object]) -> str | None:
    info = payload.get("info")
    value = info.get("status") if isinstance(info, dict) else payload.get("status")
    return value if value == "deployed" else None


def _status_chart_versions(payload: dict[str, object]) -> tuple[str, str]:
    chart = payload.get("chart")
    metadata = chart.get("metadata") if isinstance(chart, dict) else None
    if not isinstance(metadata, dict):
        raise ClusterOperationSnapshotError("Helm chart metadata is invalid")
    chart_version = _safe_metadata(metadata.get("version"))
    app_version = _safe_metadata(metadata.get("appVersion", metadata.get("app_version")))
    if chart_version is None or app_version is None:
        raise ClusterOperationSnapshotError("Helm chart metadata is invalid")
    return chart_version, app_version


def _chart_version_from_history(value: object) -> str | None:
    chart = _safe_metadata(value)
    if chart is None:
        return None
    match = _CHART_VERSION.fullmatch(chart)
    return match.group(1) if match is not None else None


def _source_file_digests(
    operation: ValidatedClusterOperation,
) -> tuple[ApprovedSourceFileDigest, ...]:
    root = operation.repository_root.resolve()
    declared_paths = {item.path for item in operation.manifest.allowed_files}
    for action in operation.manifest.actions:
        if not isinstance(action, HelmUpgradeInstallAction):
            continue
        chart_root = _source_path(root, action.chart_path)
        if not chart_root.is_dir():
            raise ClusterOperationSnapshotError("declared local Helm chart is unavailable")
        for chart_file in chart_root.rglob("*"):
            if not chart_file.is_file():
                continue
            try:
                relative_path = chart_file.resolve().relative_to(root).as_posix()
            except ValueError as exc:
                raise ClusterOperationSnapshotError(
                    "declared local Helm chart escapes repository"
                ) from exc
            if relative_path not in declared_paths:
                raise ClusterOperationSnapshotError(
                    "local Helm chart contains an undeclared source file"
                )
    digests: list[ApprovedSourceFileDigest] = []
    for relative_path in sorted(declared_paths):
        path = _source_path(root, relative_path)
        if not path.is_file():
            raise ClusterOperationSnapshotError("declared source file is unavailable")
        digests.append(ApprovedSourceFileDigest(path=relative_path, sha256=_file_sha256(path)))
    return tuple(digests)


def _verified_tool_digests(target: ClusterMutationTargetDefinition) -> tuple[NamedDigest, ...]:
    observed: list[NamedDigest] = []
    for name, tool in (("helm", target.toolchain.helm), ("kubectl", target.toolchain.kubectl)):
        path = Path(tool.path)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ClusterOperationSnapshotError(f"configured {name} binary is unavailable")
        try:
            digest = _file_sha256(path)
        except OSError as exc:
            raise ClusterOperationSnapshotError(f"configured {name} binary cannot be read") from exc
        if digest != tool.sha256:
            raise ClusterOperationSnapshotError(f"configured {name} binary digest does not match")
        observed.append(NamedDigest(name=name, sha256=digest))
    return tuple(observed)


def _run(
    command_runner: ClusterOperationSnapshotCommandRunner,
    argv: tuple[str, ...],
    timeout_seconds: int,
) -> ClusterOperationSnapshotCommandResult:
    try:
        result = command_runner(argv, timeout_seconds)
    except Exception as exc:
        raise ClusterOperationSnapshotError("fixed read-only command did not complete") from exc
    if (
        not isinstance(result, ClusterOperationSnapshotCommandResult)
        or not isinstance(result.returncode, int)
        or not -255 <= result.returncode <= 255
    ):
        raise ClusterOperationSnapshotError("fixed read-only runner returned an invalid result")
    _safe_text(result.stdout)
    _safe_text(result.stderr)
    return result


def _required_success(
    result: ClusterOperationSnapshotCommandResult, command_name: str
) -> ClusterOperationSnapshotCommandResult:
    if result.returncode != 0:
        raise ClusterOperationSnapshotError(f"{command_name} is unavailable")
    return result


def _is_helm_not_found(result: ClusterOperationSnapshotCommandResult) -> bool:
    if result.returncode == 0:
        return False
    stdout = _safe_text(result.stdout).strip()
    stderr = _safe_text(result.stderr).strip()
    return stdout == "" and stderr in {"Error: release: not found", "release: not found"}


def _safe_text(value: object) -> str:
    if not isinstance(value, bytes) or len(value) > _MAX_COMMAND_OUTPUT_BYTES:
        raise ClusterOperationSnapshotError("fixed read-only runner returned unbounded output")
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClusterOperationSnapshotError(
            "fixed read-only command returned invalid text"
        ) from exc
    try:
        assert_cluster_operation_safe_payload(text)
    except ClusterOperationLifecycleError as exc:
        raise ClusterOperationSnapshotError(
            "fixed read-only command returned secret-like output"
        ) from exc
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError:
        return text
    try:
        assert_cluster_operation_safe_payload(payload)
    except ClusterOperationLifecycleError as exc:
        raise ClusterOperationSnapshotError(
            "fixed read-only command returned secret-like output"
        ) from exc
    return text


def _safe_json(value: bytes) -> object:
    text = _safe_text(value)
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ClusterOperationSnapshotError(
            "fixed read-only command returned invalid JSON metadata"
        ) from exc
    try:
        assert_cluster_operation_safe_payload(payload)
    except ClusterOperationLifecycleError as exc:
        raise ClusterOperationSnapshotError(
            "fixed read-only command returned secret-like metadata"
        ) from exc
    return payload


def _safe_tab_fields(value: bytes, count: int, label: str) -> tuple[str, ...]:
    text = _safe_text(value)
    if text.count("\n") > 1 or ("\n" in text and not text.endswith("\n")):
        raise ClusterOperationSnapshotError(f"{label} output is invalid")
    fields = tuple(text.rstrip("\n").split("\t"))
    if len(fields) != count or any(not field for field in fields):
        raise ClusterOperationSnapshotError(f"{label} output is invalid")
    return fields


def _safe_metadata(value: object) -> str | None:
    if not isinstance(value, str) or _SAFE_METADATA.fullmatch(value) is None:
        return None
    return value


def _source_path(root: Path, relative_path: str) -> Path:
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ClusterOperationSnapshotError("declared source path escapes repository") from exc
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _require_sha256(value: str, label: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ClusterOperationSnapshotError(f"{label} is invalid")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ClusterOperationSnapshotError("snapshot capture time must use UTC")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
