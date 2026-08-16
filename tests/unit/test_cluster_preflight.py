from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import config_values, create_fixture_project, write_config

import dispatcher.cli as cli
import dispatcher.cluster_preflight as cluster_preflight
from dispatcher.cluster_preflight import (
    ClusterPreflightError,
    ClusterPreflightResult,
    CommandResult,
    run_cluster_preflight,
)
from dispatcher.config import (
    ClusterPreflightConfig,
    ClusterPreflightDefinition,
    ConfigError,
    load_cluster_preflight_config,
)


def _definition(kubectl_path: str | None = None) -> ClusterPreflightDefinition:
    values = {
        "capability_version": 1,
        "context": "kind-local-test-cluster",
        "minimum_client_version": "v1.27.0",
        "minimum_server_version": "v1.27.0",
        "request_timeout_seconds": 10,
        "required_namespaces": ["vector-system", "ml-components"],
        "required_helm_releases": [
            {
                "release": "vector-logs",
                "namespace": "vector-system",
                "chart": "vector",
                "minimum_chart_version": "0.50.0",
            }
        ],
        "required_api_resources": [
            {"resource": "deployments.apps"},
            {"resource": "pods"},
        ],
        "auth_checks": [
            {"verb": "get", "resource": "deployments.apps", "namespace": "ml-components"},
            {"verb": "create", "resource": "pods/portforward", "namespace": "ml-components"},
        ],
    }
    if kubectl_path is not None:
        values["kubectl_path"] = kubectl_path
    return ClusterPreflightDefinition.model_validate(values)


def _config() -> ClusterPreflightConfig:
    return ClusterPreflightConfig(
        project_id="fixture-project",
        config_digest="a" * 64,
        definition=_definition(),
    )


def _successful_responses() -> dict[tuple[str, ...], CommandResult]:
    context = "kind-local-test-cluster"
    timeout = "--request-timeout=10s"
    return {
        ("kubectl", "config", "current-context"): CommandResult(0, f"{context}\n".encode()),
        ("kubectl", "--context", context, timeout, "version", "--output=json"): CommandResult(
            0,
            b'{"clientVersion":{"gitVersion":"v1.28.0-rc.1+build.7"},"serverVersion":{"gitVersion":"v1.27.3+k3s1"}}',
        ),
        ("helm", "version", "--template={{.Version}}"): CommandResult(0, b"v3.17.0\n"),
        (
            "kubectl",
            "--context",
            context,
            timeout,
            "get",
            "namespace",
            "vector-system",
            "--output=name",
        ): CommandResult(0, b"namespace/vector-system\n"),
        (
            "kubectl",
            "--context",
            context,
            timeout,
            "get",
            "namespace",
            "ml-components",
            "--output=name",
        ): CommandResult(0, b"namespace/ml-components\n"),
        (
            "helm",
            "--kube-context",
            context,
            "list",
            "--namespace",
            "vector-system",
            "--filter=^vector-logs$",
            "--output=json",
        ): CommandResult(
            0,
            b'[{"name":"vector-logs","namespace":"vector-system","chart":"vector-0.50.1","status":"deployed"}]',
        ),
        ("kubectl", "--context", context, timeout, "api-resources", "--output=name"): CommandResult(
            0,
            b"deployments.apps\npods\n",
        ),
        (
            "kubectl",
            "--context",
            context,
            timeout,
            "auth",
            "can-i",
            "get",
            "deployments.apps",
            "--namespace",
            "ml-components",
        ): CommandResult(0, b"yes\n"),
        (
            "kubectl",
            "--context",
            context,
            timeout,
            "auth",
            "can-i",
            "create",
            "pods/portforward",
            "--namespace",
            "ml-components",
        ): CommandResult(0, b"yes\n"),
    }


def test_cluster_preflight_uses_only_bounded_read_only_commands() -> None:
    responses = _successful_responses()
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], timeout_seconds: int) -> CommandResult:
        assert timeout_seconds == 10
        calls.append(argv)
        return responses[argv]

    result = run_cluster_preflight(_config(), runner=runner)

    assert result.passed is True
    assert result.metadata is not None
    assert result.metadata.kubectl_server_version == "v1.27.3+k3s1"
    assert result.metadata.helm_version == "v3.17.0"
    assert all(check.status == "passed" for check in result.checks)
    assert all(
        blocked not in argument
        for call in calls
        for argument in call
        for blocked in ("apply", "delete", "patch", "rollout", "port-forward")
    )
    assert all(call[0] in {"kubectl", "helm"} for call in calls)


def test_cluster_preflight_uses_configured_kubectl_path_in_every_kubectl_argv() -> None:
    kubectl_path = "/bin/sh"
    responses = {
        (kubectl_path, *argv[1:]) if argv[0] == "kubectl" else argv: result
        for argv, result in _successful_responses().items()
    }
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], _timeout_seconds: int) -> CommandResult:
        calls.append(argv)
        return responses[argv]

    result = run_cluster_preflight(
        ClusterPreflightConfig(
            project_id="fixture-project",
            config_digest="a" * 64,
            definition=_definition(kubectl_path),
        ),
        runner=runner,
    )

    assert result.passed is True
    kubectl_calls = [call for call in calls if call[0] != "helm"]
    assert kubectl_calls
    assert all(call[0] == kubectl_path for call in kubectl_calls)
    assert ("helm", "version", "--template={{.Version}}") in calls


@pytest.mark.parametrize(
    ("client_version", "server_version"),
    [
        ("v1.34.1", "v1.27.3"),
        ("v1.34.1-alpha.1+build.7", "v1.27.3+build.3"),
        ("v2.27.3", "v1.27.3"),
    ],
)
def test_cluster_preflight_fails_closed_for_incompatible_kubectl_versions(
    client_version: str,
    server_version: str,
) -> None:
    responses = _successful_responses()
    context = "kind-local-test-cluster"
    responses[("kubectl", "--context", context, "--request-timeout=10s", "version", "--output=json")] = (
        CommandResult(
            0,
            json.dumps(
                {
                    "clientVersion": {"gitVersion": client_version},
                    "serverVersion": {"gitVersion": server_version},
                }
            ).encode(),
        )
    )
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], _timeout_seconds: int) -> CommandResult:
        calls.append(argv)
        return responses[argv]

    with pytest.raises(ClusterPreflightError) as exc_info:
        run_cluster_preflight(_config(), runner=runner)

    result = exc_info.value.result
    version_skew = result.checks[-1]
    assert result.passed is False
    assert result.metadata is not None
    assert version_skew.model_dump() == {
        "check_id": "version-skew",
        "kind": "version_skew",
        "target": "kubectl client/server versions",
        "expected": "Kubernetes-supported: same major version and minor skew <= 1",
        "actual": f"client {client_version}; server {server_version}",
        "status": "failed",
    }
    assert str(exc_info.value) == (
        f"kubectl client version {client_version} and server version {server_version} "
        "are outside the Kubernetes-supported compatibility window "
        "(same major version; minor skew <= 1)"
    )
    assert calls == [
        ("kubectl", "config", "current-context"),
        ("kubectl", "--context", context, "--request-timeout=10s", "version", "--output=json"),
        ("helm", "version", "--template={{.Version}}"),
    ]


@pytest.mark.parametrize(
    ("version_key", "observed_version", "check_id", "target"),
    [
        ("clientVersion", "v1.26.9", "client-version-floor", "kubectl client version"),
        ("serverVersion", "v1.26.9", "server-version-floor", "kubectl server version"),
    ],
)
def test_cluster_preflight_fails_closed_for_kubernetes_version_below_floor(
    version_key: str, observed_version: str, check_id: str, target: str
) -> None:
    responses = _successful_responses()
    context = "kind-local-test-cluster"
    observed = {
        "clientVersion": "v1.27.3",
        "serverVersion": "v1.27.3",
        version_key: observed_version,
    }
    responses[("kubectl", "--context", context, "--request-timeout=10s", "version", "--output=json")] = (
        CommandResult(
            0,
            json.dumps(
                {
                    "clientVersion": {"gitVersion": observed["clientVersion"]},
                    "serverVersion": {"gitVersion": observed["serverVersion"]},
                }
            ).encode(),
        )
    )

    with pytest.raises(ClusterPreflightError) as exc_info:
        run_cluster_preflight(_config(), runner=lambda argv, _: responses[argv])

    assert exc_info.value.result.checks[-1].model_dump() == {
        "check_id": check_id,
        "kind": "version_floor",
        "target": target,
        "expected": ">= v1.27.0",
        "actual": observed_version,
        "status": "failed",
    }
    assert str(exc_info.value) == f"{target} {observed_version} is below configured minimum v1.27.0"


def test_cluster_preflight_fails_closed_for_chart_below_minimum_version() -> None:
    responses = _successful_responses()
    responses[(
        "helm",
        "--kube-context",
        "kind-local-test-cluster",
        "list",
        "--namespace",
        "vector-system",
        "--filter=^vector-logs$",
        "--output=json",
    )] = CommandResult(
        0,
        b'[{"name":"vector-logs","namespace":"vector-system","chart":"vector-0.49.9","status":"deployed"}]',
    )

    with pytest.raises(ClusterPreflightError, match="chart version below minimum 0.50.0") as exc_info:
        run_cluster_preflight(_config(), runner=lambda argv, _: responses[argv])

    assert exc_info.value.result.checks[-1].model_dump() == {
        "check_id": "helm-release-1",
        "kind": "helm_release",
        "target": "vector-system/vector-logs",
        "expected": "vector >= 0.50.0 deployed",
        "actual": "vector-0.49.9 deployed",
        "status": "failed",
    }


def test_cluster_preflight_rejects_unparseable_kubectl_semver() -> None:
    responses = _successful_responses()
    context = "kind-local-test-cluster"
    responses[("kubectl", "--context", context, "--request-timeout=10s", "version", "--output=json")] = (
        CommandResult(
            0,
            b'{"clientVersion":{"gitVersion":"v1.34"},"serverVersion":{"gitVersion":"v1.27.3"}}',
        )
    )

    with pytest.raises(ClusterPreflightError, match="Kubernetes semantic versions") as exc_info:
        run_cluster_preflight(_config(), runner=lambda argv, _: responses[argv])

    assert exc_info.value.result.checks[-1].model_dump() == {
        "check_id": "kubectl-metadata",
        "kind": "metadata",
        "target": "kubectl version",
        "expected": "valid client and server version metadata",
        "actual": "invalid",
        "status": "failed",
    }


def test_cluster_preflight_fails_before_contacting_a_wrong_context() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], _timeout_seconds: int) -> CommandResult:
        calls.append(argv)
        return CommandResult(0, b"other-context\n")

    with pytest.raises(ClusterPreflightError, match="does not match required context") as exc_info:
        run_cluster_preflight(_config(), runner=runner)

    assert calls == [("kubectl", "config", "current-context")]
    assert exc_info.value.result.passed is False
    assert exc_info.value.result.checks[-1].actual == "other-context"


def test_cluster_preflight_reports_an_unauthorized_capability_without_raw_output() -> None:
    responses = _successful_responses()
    denied = next(key for key in responses if "auth" in key and "create" in key)
    responses[denied] = CommandResult(0, b"no\n", b"authorization token should not be shown")

    def runner(argv: tuple[str, ...], _timeout_seconds: int) -> CommandResult:
        return responses[argv]

    with pytest.raises(ClusterPreflightError, match="authorization is insufficient") as exc_info:
        run_cluster_preflight(_config(), runner=runner)

    assert exc_info.value.result.checks[-1].actual == "no"
    assert "authorization token" not in str(exc_info.value)
    assert "authorization token" not in exc_info.value.result.model_dump_json()


def test_cluster_preflight_rejects_oversized_command_output() -> None:
    def runner(_argv: tuple[str, ...], _timeout_seconds: int) -> CommandResult:
        return CommandResult(0, b"x" * 65_537)

    with pytest.raises(ClusterPreflightError, match="stdout exceeds 65536 bytes") as exc_info:
        run_cluster_preflight(_config(), runner=runner)

    assert exc_info.value.result.checks[-1].actual == "unavailable"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda values: values["cluster_preflight"]["auth_checks"][0].update({"verb": "exec"}),
            "cluster_preflight.auth_checks.0.verb",
        ),
        (
            lambda values: values["cluster_preflight"].update({"unsafe_command": "kubectl apply"}),
            "cluster_preflight.unsafe_command: Extra inputs are not permitted",
        ),
        (
            lambda values: values["cluster_preflight"].pop("minimum_client_version"),
            "cluster_preflight.minimum_client_version: Field required",
        ),
        (
            lambda values: values["cluster_preflight"]["required_helm_releases"][0].pop(
                "minimum_chart_version"
            ),
            "cluster_preflight.required_helm_releases.0.minimum_chart_version: Field required",
        ),
        (
            lambda values: values["cluster_preflight"]["required_helm_releases"][0].update(
                {"chart_version": "0.50.0"}
            ),
            "cluster_preflight.required_helm_releases.0.chart_version: Extra inputs are not permitted",
        ),
        (
            lambda values: values["cluster_preflight"]["required_api_resources"][0].update(
                {"resource": "*"}
            ),
            "cluster_preflight.required_api_resources.0.resource",
        ),
        (
            lambda values: values["cluster_preflight"]["required_api_resources"][1].update(
                {"resource": "pods/portforward"}
            ),
            "cluster_preflight.required_api_resources.1.resource",
        ),
    ],
)
def test_cluster_preflight_config_rejects_unsafe_or_unbounded_shapes(
    tmp_path: Path,
    mutate,
    message: str,
) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["cluster_preflight"] = _definition().model_dump(mode="json")
    mutate(values)

    with pytest.raises(ConfigError, match=message):
        write_config(project, values)


def test_cluster_preflight_auth_subresource_requires_declared_parent(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["cluster_preflight"] = _definition().model_dump(mode="json")
    values["cluster_preflight"]["required_api_resources"] = [{"resource": "deployments.apps"}]

    with pytest.raises(
        ConfigError,
        match="auth_checks resources or subresource parents must appear in required_api_resources",
    ):
        write_config(project, values)


def test_cluster_preflight_config_rejects_relative_kubectl_path(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["cluster_preflight"] = _definition().model_dump(mode="json")
    values["cluster_preflight"]["kubectl_path"] = "config/tools/kubectl-v1.27.3"

    with pytest.raises(ConfigError, match="cluster_preflight.kubectl_path.*absolute path"):
        write_config(project, values)


def test_cluster_preflight_config_rejects_nonexistent_kubectl_path(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["cluster_preflight"] = _definition().model_dump(mode="json")
    values["cluster_preflight"]["kubectl_path"] = str(project.root / "missing-kubectl")
    write_config(project, values)

    with pytest.raises(ConfigError, match="cluster_preflight.kubectl_path.*existing regular file"):
        load_cluster_preflight_config(project.config_path)


def test_cluster_preflight_config_rejects_non_executable_kubectl_path(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["cluster_preflight"] = _definition().model_dump(mode="json")
    values["cluster_preflight"]["kubectl_path"] = str(project.config_path)
    write_config(project, values)

    with pytest.raises(ConfigError, match="cluster_preflight.kubectl_path.*executable"):
        load_cluster_preflight_config(project.config_path)


def test_cluster_preflight_cli_prints_structured_result_without_running_cluster_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["cluster_preflight"] = _definition().model_dump(mode="json")
    write_config(project, values)
    expected = ClusterPreflightResult(
        project_id="fixture-project",
        config_digest="a" * 64,
        context="kind-local-test-cluster",
        observed_at=datetime(2026, 8, 16, tzinfo=UTC),
        passed=True,
        metadata=None,
        checks=(
            {
                "check_id": "context",
                "kind": "context",
                "target": "kubectl config current-context",
                "expected": "kind-local-test-cluster",
                "actual": "kind-local-test-cluster",
                "status": "passed",
            },
        ),
    )
    monkeypatch.setattr(cluster_preflight, "run_cluster_preflight", lambda _config: expected)

    assert cli.main(["cluster-preflight", "--config", str(project.config_path)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True
