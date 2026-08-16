"""Bounded, dispatcher-owned read-only Kubernetes readiness checks."""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, NoReturn

from pydantic import Field

from .config import (
    ClusterPreflightConfig,
    ContractModel,
    Identifier,
    KubernetesContext,
    validate_cluster_preflight_kubectl_path,
)

_MAX_COMMAND_OUTPUT_BYTES = 65_536
_MAX_SUPPORTED_KUBECTL_SERVER_MINOR_SKEW = 1
_SEMVER_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER = re.compile(
    rf"^v?(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    rf"(?:-(?P<prerelease>{_SEMVER_IDENTIFIER}(?:\.{_SEMVER_IDENTIFIER})*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@dataclass(frozen=True)
class CommandResult:
    """The bounded data returned by one injected read-only command runner."""

    returncode: int
    stdout: bytes
    stderr: bytes = b""


@dataclass(frozen=True)
class _SemanticVersion:
    """A validated semantic version with comparable precedence components."""

    value: str
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] | None


@dataclass(frozen=True)
class _HelmRelease:
    """Safe Helm release attributes needed for the minimum-version check."""

    chart_version: _SemanticVersion
    status: str


ClusterCommandRunner = Callable[[tuple[str, ...], int], CommandResult]


class ClusterPreflightMetadata(ContractModel):
    """Safe tool-version metadata recorded with a successful readiness result."""

    kubectl_client_version: str = Field(min_length=1, max_length=128)
    kubectl_server_version: str = Field(min_length=1, max_length=128)
    helm_version: str = Field(min_length=1, max_length=128)


class ClusterPreflightCheck(ContractModel):
    """One selected readiness assertion, with no raw command output."""

    check_id: Identifier
    kind: Literal[
        "context",
        "metadata",
        "version_floor",
        "version_skew",
        "namespace",
        "helm_release",
        "api_resource",
        "auth",
    ]
    target: str = Field(min_length=1, max_length=512)
    expected: str = Field(min_length=1, max_length=512)
    actual: str = Field(min_length=1, max_length=512)
    status: Literal["passed", "failed"]


class ClusterPreflightResult(ContractModel):
    """Structured, sanitized readiness evidence for a cluster preflight attempt."""

    result_version: Literal[1] = 1
    project_id: Identifier
    config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    context: KubernetesContext
    observed_at: datetime
    passed: bool
    metadata: ClusterPreflightMetadata | None
    checks: tuple[ClusterPreflightCheck, ...] = Field(min_length=1)


class ClusterPreflightError(RuntimeError):
    """A cluster readiness requirement failed closed."""

    def __init__(self, message: str, result: ClusterPreflightResult) -> None:
        super().__init__(message)
        self.result = result


class _CommandError(ValueError):
    """A bounded command could not yield safe output."""


def run_cluster_preflight(
    config: ClusterPreflightConfig,
    *,
    runner: ClusterCommandRunner | None = None,
) -> ClusterPreflightResult:
    """Verify one configured cluster using only fixed read-only kubectl/helm argv.

    The function never reads object data, including Secret data, and it never
    invokes a mutating Kubernetes or Helm operation. An injected runner keeps
    unit tests independent of a live cluster.
    """
    command_runner = runner or _run_subprocess
    definition = config.definition
    validate_cluster_preflight_kubectl_path(definition)
    kubectl_path = definition.kubectl_path or "kubectl"
    context = definition.context
    checks: list[ClusterPreflightCheck] = []
    metadata: ClusterPreflightMetadata | None = None

    current_context = _run_or_fail(
        config,
        context,
        metadata,
        checks,
        command_runner,
        (kubectl_path, "config", "current-context"),
        definition.request_timeout_seconds,
        check_id="context",
        kind="context",
        target="kubectl config current-context",
        expected=context,
    ).strip()
    safe_context = _safe_token(current_context)
    if safe_context != context:
        checks.append(
            ClusterPreflightCheck(
                check_id="context",
                kind="context",
                target="kubectl config current-context",
                expected=context,
                actual=safe_context,
                status="failed",
            )
        )
        _fail(
            config,
            context,
            metadata,
            checks,
            f"active kubectl context {safe_context!r} does not match required context {context!r}",
        )
    checks.append(
        ClusterPreflightCheck(
            check_id="context",
            kind="context",
            target="kubectl config current-context",
            expected=context,
            actual=context,
            status="passed",
        )
    )

    kubectl_version_text = _run_or_fail(
        config,
        context,
        metadata,
        checks,
        command_runner,
        (
            kubectl_path,
            "--context",
            context,
            f"--request-timeout={definition.request_timeout_seconds}s",
            "version",
            "--output=json",
        ),
        definition.request_timeout_seconds,
        check_id="kubectl-metadata",
        kind="metadata",
        target="kubectl version",
        expected="valid client and server version metadata",
    )
    try:
        kubectl_client_version, kubectl_server_version = _parse_kubectl_version(kubectl_version_text)
    except ValueError as exc:
        checks.append(
            ClusterPreflightCheck(
                check_id="kubectl-metadata",
                kind="metadata",
                target="kubectl version",
                expected="valid client and server version metadata",
                actual="invalid",
                status="failed",
            )
        )
        _fail(config, context, metadata, checks, f"kubectl version metadata is invalid: {exc}")
    checks.append(
        ClusterPreflightCheck(
            check_id="kubectl-metadata",
            kind="metadata",
            target="kubectl version",
            expected="valid client and server version metadata",
            actual="valid",
            status="passed",
        )
    )

    helm_version_text = _run_or_fail(
        config,
        context,
        metadata,
        checks,
        command_runner,
        ("helm", "version", "--template={{.Version}}"),
        definition.request_timeout_seconds,
        check_id="helm-metadata",
        kind="metadata",
        target="helm version",
        expected="a safe Helm version token",
    ).strip()
    helm_version = _safe_metadata_token(helm_version_text)
    if helm_version is None:
        checks.append(
            ClusterPreflightCheck(
                check_id="helm-metadata",
                kind="metadata",
                target="helm version",
                expected="a safe Helm version token",
                actual="invalid",
                status="failed",
            )
        )
        _fail(config, context, metadata, checks, "helm version metadata is invalid")
    checks.append(
        ClusterPreflightCheck(
            check_id="helm-metadata",
            kind="metadata",
            target="helm version",
            expected="a safe Helm version token",
            actual="valid",
            status="passed",
        )
    )
    metadata = ClusterPreflightMetadata(
        kubectl_client_version=kubectl_client_version.value,
        kubectl_server_version=kubectl_server_version.value,
        helm_version=helm_version,
    )

    for check_id, target, actual_version, minimum_version in (
        (
            "client-version-floor",
            "kubectl client version",
            kubectl_client_version,
            _parse_kubernetes_semver(definition.minimum_client_version),
        ),
        (
            "server-version-floor",
            "kubectl server version",
            kubectl_server_version,
            _parse_kubernetes_semver(definition.minimum_server_version),
        ),
    ):
        meets_floor = _compare_semver(actual_version, minimum_version) >= 0
        checks.append(
            ClusterPreflightCheck(
                check_id=check_id,
                kind="version_floor",
                target=target,
                expected=f">= {minimum_version.value}",
                actual=actual_version.value,
                status="passed" if meets_floor else "failed",
            )
        )
        if not meets_floor:
            _fail(
                config,
                context,
                metadata,
                checks,
                f"{target} {actual_version.value} is below configured minimum {minimum_version.value}",
            )

    versions_are_compatible = (
        kubectl_client_version.major == kubectl_server_version.major
        and abs(kubectl_client_version.minor - kubectl_server_version.minor)
        <= _MAX_SUPPORTED_KUBECTL_SERVER_MINOR_SKEW
    )
    checks.append(
        ClusterPreflightCheck(
            check_id="version-skew",
            kind="version_skew",
            target="kubectl client/server versions",
            expected=(
                "Kubernetes-supported: same major version and "
                f"minor skew <= {_MAX_SUPPORTED_KUBECTL_SERVER_MINOR_SKEW}"
            ),
            actual=(
                f"client {kubectl_client_version.value}; server {kubectl_server_version.value}"
            ),
            status="passed" if versions_are_compatible else "failed",
        )
    )
    if not versions_are_compatible:
        _fail(
            config,
            context,
            metadata,
            checks,
            "kubectl client version "
            f"{kubectl_client_version.value} and server version {kubectl_server_version.value} "
            "are outside the Kubernetes-supported compatibility window "
            "(same major version; "
            f"minor skew <= {_MAX_SUPPORTED_KUBECTL_SERVER_MINOR_SKEW})",
        )

    for index, namespace in enumerate(definition.required_namespaces, start=1):
        check_id = f"namespace-{index}"
        _run_or_fail(
            config,
            context,
            metadata,
            checks,
            command_runner,
            (
                kubectl_path,
                "--context",
                context,
                f"--request-timeout={definition.request_timeout_seconds}s",
                "get",
                "namespace",
                namespace,
                "--output=name",
            ),
            definition.request_timeout_seconds,
            check_id=check_id,
            kind="namespace",
            target=namespace,
            expected="present and readable",
        )
        checks.append(
            ClusterPreflightCheck(
                check_id=check_id,
                kind="namespace",
                target=namespace,
                expected="present and readable",
                actual="present",
                status="passed",
            )
        )

    for index, helm_requirement in enumerate(definition.required_helm_releases, start=1):
        check_id = f"helm-release-{index}"
        payload = _run_or_fail(
            config,
            context,
            metadata,
            checks,
            command_runner,
            (
                "helm",
                "--kube-context",
                context,
                "list",
                "--namespace",
                helm_requirement.namespace,
                f"--filter=^{helm_requirement.release}$",
                "--output=json",
            ),
            definition.request_timeout_seconds,
            check_id=check_id,
            kind="helm_release",
            target=f"{helm_requirement.namespace}/{helm_requirement.release}",
            expected=(
                f"{helm_requirement.chart} >= {helm_requirement.minimum_chart_version} deployed"
            ),
        )
        actual_release = _helm_release_actual(
            payload,
            helm_requirement.release,
            helm_requirement.namespace,
            helm_requirement.chart,
        )
        minimum_chart_version = _parse_semver(helm_requirement.minimum_chart_version)
        expected = f"{helm_requirement.chart} >= {minimum_chart_version.value} deployed"
        actual = (
            f"{helm_requirement.chart}-{actual_release.chart_version.value} {actual_release.status}"
            if actual_release is not None
            else "missing or invalid"
        )
        release_is_supported = (
            actual_release is not None
            and actual_release.status == "deployed"
            and _compare_semver(actual_release.chart_version, minimum_chart_version) >= 0
        )
        if not release_is_supported:
            checks.append(
                ClusterPreflightCheck(
                    check_id=check_id,
                    kind="helm_release",
                    target=f"{helm_requirement.namespace}/{helm_requirement.release}",
                    expected=expected,
                    actual=actual,
                    status="failed",
                )
            )
            _fail(
                config,
                context,
                metadata,
                checks,
                "required Helm release "
                f"{helm_requirement.namespace}/{helm_requirement.release} is missing, not deployed, or has a "
                f"chart version below minimum {minimum_chart_version.value}",
            )
        checks.append(
            ClusterPreflightCheck(
                check_id=check_id,
                kind="helm_release",
                target=f"{helm_requirement.namespace}/{helm_requirement.release}",
                expected=expected,
                actual=actual,
                status="passed",
            )
        )

    api_resources_text = _run_or_fail(
        config,
        context,
        metadata,
        checks,
        command_runner,
        (
            kubectl_path,
            "--context",
            context,
            f"--request-timeout={definition.request_timeout_seconds}s",
            "api-resources",
            "--output=name",
        ),
        definition.request_timeout_seconds,
        check_id="api-resources",
        kind="api_resource",
        target="Kubernetes API discovery",
        expected="configured API resources",
    )
    available_resources = {line.strip() for line in api_resources_text.splitlines()}
    for index, api_requirement in enumerate(definition.required_api_resources, start=1):
        check_id = f"api-resource-{index}"
        present = api_requirement.resource in available_resources
        checks.append(
            ClusterPreflightCheck(
                check_id=check_id,
                kind="api_resource",
                target=api_requirement.resource,
                expected="advertised by API discovery",
                actual="present" if present else "missing",
                status="passed" if present else "failed",
            )
        )
        if not present:
            _fail(
                config,
                context,
                metadata,
                checks,
                f"required Kubernetes API resource {api_requirement.resource!r} is not advertised by discovery",
            )

    for index, auth_requirement in enumerate(definition.auth_checks, start=1):
        check_id = f"auth-{index}"
        response = _run_or_fail(
            config,
            context,
            metadata,
            checks,
            command_runner,
            (
                kubectl_path,
                "--context",
                context,
                f"--request-timeout={definition.request_timeout_seconds}s",
                "auth",
                "can-i",
                auth_requirement.verb,
                auth_requirement.resource,
                "--namespace",
                auth_requirement.namespace,
            ),
            definition.request_timeout_seconds,
            check_id=check_id,
            kind="auth",
            target=f"{auth_requirement.namespace}/{auth_requirement.resource}",
            expected=f"can {auth_requirement.verb}",
        ).strip()
        authorized = response.lower() == "yes"
        checks.append(
            ClusterPreflightCheck(
                check_id=check_id,
                kind="auth",
                target=f"{auth_requirement.namespace}/{auth_requirement.resource}",
                expected=f"can {auth_requirement.verb}",
                actual="yes" if authorized else "no",
                status="passed" if authorized else "failed",
            )
        )
        if not authorized:
            _fail(
                config,
                context,
                metadata,
                checks,
                "authorization is insufficient: expected kubectl auth can-i "
                f"{auth_requirement.verb} {auth_requirement.resource} "
                f"--namespace {auth_requirement.namespace}",
            )

    return _result(config, context, metadata, checks, passed=True)


def _run_subprocess(argv: tuple[str, ...], timeout_seconds: int) -> CommandResult:
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=False,
    )
    stdout = process.stdout
    if stdout is None:
        raise _CommandError("command stdout pipe is unavailable")

    output_queue: queue.Queue[bytes] = queue.Queue(maxsize=1)

    def read_stdout() -> None:
        output = stdout.read(_MAX_COMMAND_OUTPUT_BYTES + 1)
        output_queue.put(output)
        if len(output) > _MAX_COMMAND_OUTPUT_BYTES:
            try:
                process.kill()
            except ProcessLookupError:
                pass

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        reader.join()
        stdout.close()
    output = output_queue.get()
    if len(output) > _MAX_COMMAND_OUTPUT_BYTES:
        raise _CommandError(f"stdout exceeds {_MAX_COMMAND_OUTPUT_BYTES} bytes")
    return CommandResult(
        returncode=process.returncode,
        stdout=output,
    )


def _run_or_fail(
    config: ClusterPreflightConfig,
    context: str,
    metadata: ClusterPreflightMetadata | None,
    checks: list[ClusterPreflightCheck],
    runner: ClusterCommandRunner,
    argv: tuple[str, ...],
    timeout_seconds: int,
    *,
    check_id: str,
    kind: Literal[
        "context",
        "metadata",
        "version_floor",
        "version_skew",
        "namespace",
        "helm_release",
        "api_resource",
        "auth",
    ],
    target: str,
    expected: str,
) -> str:
    try:
        result = runner(argv, timeout_seconds)
        if not isinstance(result, CommandResult):
            raise _CommandError("runner returned an invalid result")
        if not isinstance(result.returncode, int):
            raise _CommandError("runner returned an invalid exit status")
        _bounded_output(result.stdout, "stdout")
        _bounded_output(result.stderr, "stderr")
        if result.returncode != 0:
            raise _CommandError(f"command exited with status {result.returncode}")
        return _bounded_output(result.stdout, "stdout").decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        error = "command could not complete within the configured bound"
    except _CommandError as exc:
        error = str(exc)
    checks.append(
        ClusterPreflightCheck(
            check_id=check_id,
            kind=kind,
            target=target,
            expected=expected,
            actual="unavailable",
            status="failed",
        )
    )
    _fail(config, context, metadata, checks, f"{target}: {error}")


def _bounded_output(output: bytes, stream_name: str) -> bytes:
    if not isinstance(output, bytes):
        raise _CommandError(f"runner returned non-bytes {stream_name}")
    if len(output) > _MAX_COMMAND_OUTPUT_BYTES:
        raise _CommandError(f"{stream_name} exceeds {_MAX_COMMAND_OUTPUT_BYTES} bytes")
    return output


def _parse_kubectl_version(value: str) -> tuple[_SemanticVersion, _SemanticVersion]:
    try:
        payload = json.loads(value)
        client = payload["clientVersion"]["gitVersion"]
        server = payload["serverVersion"]["gitVersion"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("expected clientVersion.gitVersion and serverVersion.gitVersion") from exc
    return _parse_kubernetes_semver(client), _parse_kubernetes_semver(server)


def _parse_kubernetes_semver(value: object) -> _SemanticVersion:
    return _parse_semver(value, "version values must be Kubernetes semantic versions")


def _parse_semver(value: object, error_message: str = "version values must be semantic versions") -> _SemanticVersion:
    safe_value = _safe_metadata_token(value)
    if safe_value is None:
        raise ValueError("version values are unsafe or missing")
    match = _SEMVER.fullmatch(safe_value)
    if match is None:
        raise ValueError(error_message)
    prerelease = match.group("prerelease")
    return _SemanticVersion(
        value=safe_value,
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        prerelease=tuple(prerelease.split(".")) if prerelease is not None else None,
    )


def _compare_semver(left: _SemanticVersion, right: _SemanticVersion) -> int:
    """Compare semantic versions, including SemVer pre-release precedence."""

    left_core = (left.major, left.minor, left.patch)
    right_core = (right.major, right.minor, right.patch)
    if left_core != right_core:
        return 1 if left_core > right_core else -1
    if left.prerelease is None:
        return 0 if right.prerelease is None else 1
    if right.prerelease is None:
        return -1
    for left_part, right_part in zip(left.prerelease, right.prerelease, strict=False):
        if left_part == right_part:
            continue
        left_numeric = left_part.isdecimal()
        right_numeric = right_part.isdecimal()
        if left_numeric and right_numeric:
            return 1 if int(left_part) > int(right_part) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_part > right_part else -1
    if len(left.prerelease) == len(right.prerelease):
        return 0
    return 1 if len(left.prerelease) > len(right.prerelease) else -1


def _helm_release_actual(
    payload: str, release: str, namespace: str, expected_chart: str
) -> _HelmRelease | None:
    try:
        releases = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(releases, list) or len(releases) != 1:
        return None
    item = releases[0]
    if not isinstance(item, dict) or item.get("name") != release or item.get("namespace") != namespace:
        return None
    chart = _safe_metadata_token(item.get("chart"))
    status = _safe_metadata_token(item.get("status"))
    chart_prefix = f"{expected_chart}-"
    if chart is None or status is None or not chart.startswith(chart_prefix):
        return None
    try:
        chart_version = _parse_semver(chart.removeprefix(chart_prefix))
    except ValueError:
        return None
    return _HelmRelease(chart_version=chart_version, status=status)


def _safe_token(value: str) -> str:
    if not value or len(value) > 254:
        return "invalid"
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:@/-")
    return value if set(value) <= allowed else "invalid"


def _safe_metadata_token(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.+_-/")
    return value if set(value) <= allowed else None


def _result(
    config: ClusterPreflightConfig,
    context: str,
    metadata: ClusterPreflightMetadata | None,
    checks: list[ClusterPreflightCheck],
    *,
    passed: bool,
) -> ClusterPreflightResult:
    return ClusterPreflightResult(
        project_id=config.project_id,
        config_digest=config.config_digest,
        context=context,
        observed_at=datetime.now(UTC),
        passed=passed,
        metadata=metadata,
        checks=tuple(checks),
    )


def _fail(
    config: ClusterPreflightConfig,
    context: str,
    metadata: ClusterPreflightMetadata | None,
    checks: list[ClusterPreflightCheck],
    message: str,
) -> NoReturn:
    raise ClusterPreflightError(message, _result(config, context, metadata, checks, passed=False))
