from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml
from helpers import (
    FixtureProject,
    config_values,
    create_fixture_project,
    valid_plan_values,
    write_config,
)

from dispatcher.cluster_operations import (
    ClusterOperationError,
    load_cluster_operation_manifest,
    validate_cluster_operations_for_plan,
)
from dispatcher.config import ConfigError
from dispatcher.plan import NormalizedPlan, load_normalized_plan, validate_plan_for_config


@pytest.fixture
def project(tmp_path: Path) -> FixtureProject:
    return create_fixture_project(tmp_path)


def _cluster_config(project: FixtureProject) -> dict[str, Any]:
    values = config_values(project)
    tools = project.root / "mutation-tools"
    tools.mkdir(exist_ok=True)
    kubectl = tools / "kubectl"
    helm = tools / "helm"
    kubectl.write_bytes(b"fake-kubectl")
    helm.write_bytes(b"fake-helm")
    kubectl.chmod(0o700)
    helm.chmod(0o700)
    values["cluster_preflight"] = {
        "capability_version": 1,
        "target_id": "t2-5a-readiness",
        "context": "t2-5a-integration",
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
        "required_api_resources": [
            {"resource": "deployments.apps"},
            {"resource": "services"},
            {"resource": "pods"},
        ],
        "auth_checks": [
            {"verb": "get", "resource": "deployments.apps", "namespace": "platform"},
            {"verb": "create", "resource": "pods/portforward", "namespace": "platform"},
        ],
    }
    values["cluster_mutation"] = {
        "capability_version": 1,
        "targets": {
            "t2-5a": {
                "context": "t2-5a-integration",
                "toolchain": {
                    "kubectl": {
                        "path": str(kubectl),
                        "sha256": hashlib.sha256(kubectl.read_bytes()).hexdigest(),
                    },
                    "helm": {
                        "path": str(helm),
                        "sha256": hashlib.sha256(helm.read_bytes()).hexdigest(),
                    },
                },
                "allowed_repository_ids": ["fixture-repo"],
                "operation_manifest_roots": ["deploy/operations"],
                "source_file_roots": ["deploy"],
                "max_snapshot_age_seconds": 900,
                "max_action_timeout_seconds": 120,
                "preflight_target_id": "t2-5a-readiness",
            }
        },
    }
    return values


def _t25a_manifest() -> dict[str, Any]:
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
    return {
        "schema_version": 1,
        "operation_id": "t2-5a-sample-app",
        "context": "t2-5a-integration",
        "source_identity": {
            "repository_id": "fixture-repo",
            "revision": "approval_snapshot",
        },
        "allowed_namespaces": ["platform"],
        "allowed_files": [
            {"path": "deploy/manifests/sample-app.yaml", "sha256": "approval_snapshot"},
            {"path": "deploy/charts/sample-app/Chart.lock", "sha256": "approval_snapshot"},
            {"path": "deploy/values/sample-app.yaml", "sha256": "approval_snapshot"},
        ],
        "secret_requirements": [
            {"namespace": "platform", "name": "sample-app-credentials", "keys": ["username"]}
        ],
        "actions": [
            {
                "action": "kubectl_server_dry_run",
                "namespace": "platform",
                "timeout_seconds": 60,
                "expected_resources": [deployment],
                "readiness_probes": [{"probe": "deployment_available", "resource": deployment}],
                "manifest_files": [
                    {"path": "deploy/manifests/sample-app.yaml", "sha256": "approval_snapshot"}
                ],
            },
            {
                "action": "helm_upgrade_install",
                "namespace": "platform",
                "timeout_seconds": 90,
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
            {
                "action": "port_forward",
                "action_id": "sample-app-forward",
                "namespace": "platform",
                "expected_resources": [service],
                "resource": service,
                "local_port": 18080,
                "remote_port": 8443,
                "startup_timeout_seconds": 30,
                "probe_timeout_seconds": 30,
                "lifetime_timeout_seconds": 90,
            },
            {
                "action": "tls_dc8_no_client_certificate_rejection",
                "port_forward_action_id": "sample-app-forward",
            },
        ],
        "rollback": {"automatic": True, "strategy": "restore_approval_snapshot"},
    }


def _write_t25a_manifest(project: FixtureProject, manifest: dict[str, Any] | None = None) -> Path:
    repository = project.repository
    (repository / "deploy/operations").mkdir(parents=True)
    (repository / "deploy/manifests").mkdir(parents=True)
    (repository / "deploy/charts/sample-app").mkdir(parents=True)
    (repository / "deploy/values").mkdir(parents=True)
    (repository / "deploy/manifests/sample-app.yaml").write_text("kind: Deployment\n", encoding="utf-8")
    (repository / "deploy/charts/sample-app/Chart.lock").write_text("dependencies: []\n", encoding="utf-8")
    (repository / "deploy/values/sample-app.yaml").write_text("replicaCount: 1\n", encoding="utf-8")
    path = repository / "deploy/operations/t2-5a.yaml"
    path.write_text(yaml.safe_dump(manifest or _t25a_manifest(), sort_keys=False), encoding="utf-8")
    return path


def _t25a_plan(project: FixtureProject) -> NormalizedPlan:
    values = valid_plan_values(project)
    values["steps"][0]["cluster_operation"] = {
        "target_name": "t2-5a",
        "operation_manifest_path": "deploy/operations/t2-5a.yaml",
        "requires_cluster_approval": True,
        "preauthorized_actions": [
            "kubectl_server_dry_run",
            "helm_upgrade_install",
            "port_forward",
            "tls_dc8_no_client_certificate_rejection",
        ],
        "requires_automatic_rollback": True,
    }
    return NormalizedPlan.model_validate(values)


def _move_chart_outside_source_root(manifest: dict[str, Any]) -> None:
    action = manifest["actions"][1]
    action["chart_path"] = "outside/chart"
    action["chart_lock_file"]["path"] = "outside/chart/Chart.lock"


def test_t25a_shaped_manifest_is_statically_validated_without_cluster_execution(
    project: FixtureProject,
) -> None:
    _write_t25a_manifest(project)
    config = write_config(project, _cluster_config(project))
    plan = _t25a_plan(project)

    validated = validate_cluster_operations_for_plan(config=config, plan=plan)
    validate_plan_for_config(plan, config)

    operation = validated["prepare-fixture"]
    assert operation.target_name == "t2-5a"
    assert operation.manifest.rollback.automatic is True
    assert operation.manifest.source_identity.revision == "approval_snapshot"
    assert {action.action for action in operation.manifest.actions} == {
            "kubectl_server_dry_run",
            "helm_upgrade_install",
            "port_forward",
            "tls_dc8_no_client_certificate_rejection",
    }


@pytest.mark.parametrize(
    ("actions", "message"),
    [
        (["kubectl_server_dry_run", "helm_upgrade_install"], "do not match the preauthorized action order"),
        (
            ["helm_upgrade_install", "kubectl_server_dry_run", "port_forward"],
            "do not match the preauthorized action order",
        ),
    ],
)
def test_post_commit_manifest_actions_must_match_the_plan_envelope_contract(
    project: FixtureProject,
    actions: list[str],
    message: str,
) -> None:
    _write_t25a_manifest(project)
    config = write_config(project, _cluster_config(project))
    plan_values = _t25a_plan(project).model_dump(mode="json")
    plan_values["steps"][0]["cluster_operation"]["preauthorized_actions"] = actions

    with pytest.raises(ClusterOperationError, match=message):
        validate_cluster_operations_for_plan(
            config=config,
            plan=NormalizedPlan.model_validate(plan_values),
        )


def test_legacy_plan_and_config_require_no_cluster_configuration(project: FixtureProject) -> None:
    plan = NormalizedPlan.model_validate(valid_plan_values(project))

    assert validate_cluster_operations_for_plan(config=project.config, plan=plan) == {}
    validate_plan_for_config(plan, project.config)


def test_cluster_operation_admission_precedes_executor_created_manifest(
    project: FixtureProject,
) -> None:
    config = write_config(project, _cluster_config(project))
    sidecar = project.root / "cluster-plan.yaml"
    sidecar.write_text(yaml.safe_dump(_t25a_plan(project).model_dump(mode="json")), encoding="utf-8")

    plan = load_normalized_plan(sidecar, config)

    with pytest.raises(ClusterOperationError, match="cluster operation manifest not found"):
        validate_cluster_operations_for_plan(config=config, plan=plan)

    _write_t25a_manifest(project)

    assert "prepare-fixture" in validate_cluster_operations_for_plan(config=config, plan=plan)


@pytest.mark.parametrize("value", [False, "true", 1])
def test_cluster_operation_reference_requires_explicit_true_approval_marker(
    project: FixtureProject, value: object
) -> None:
    values = valid_plan_values(project)
    values["steps"][0]["cluster_operation"] = {
        "target_name": "t2-5a",
        "operation_manifest_path": "deploy/operations/t2-5a.yaml",
        "requires_cluster_approval": value,
        "preauthorized_actions": ["kubectl_server_dry_run"],
        "requires_automatic_rollback": True,
    }

    with pytest.raises(ValueError, match="requires_cluster_approval"):
        NormalizedPlan.model_validate(values)


@pytest.mark.parametrize("path", ["/operation.yaml", "../operation.yaml", "deploy/*.yaml", "$HOME/op.yaml"])
def test_cluster_operation_reference_requires_normalized_manifest_path(
    project: FixtureProject, path: str
) -> None:
    values = valid_plan_values(project)
    values["steps"][0]["cluster_operation"] = {
        "target_name": "t2-5a",
        "operation_manifest_path": path,
        "requires_cluster_approval": True,
        "preauthorized_actions": ["kubectl_server_dry_run"],
        "requires_automatic_rollback": True,
    }

    with pytest.raises(ValueError, match="operation_manifest_path"):
        NormalizedPlan.model_validate(values)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda values: values["cluster_mutation"]["targets"]["t2-5a"].update(
                {"preflight_target_id": "missing"}
            ),
            "preflight_target_id",
        ),
        (
            lambda values: values["cluster_mutation"]["targets"]["t2-5a"].update(
                {"context": "other-context"}
            ),
            "context must match",
        ),
        (
            lambda values: values["cluster_mutation"]["targets"]["t2-5a"].update(
                {"allowed_repository_ids": ["missing-repository"]}
            ),
            "allowed_repository_ids",
        ),
        (
            lambda values: values["cluster_mutation"]["targets"]["t2-5a"].update(
                {"source_file_roots": ["../outside"]}
            ),
            "cluster mutation root",
        ),
    ],
)
def test_cluster_mutation_target_must_align_with_read_only_preflight(
    project: FixtureProject,
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    values = _cluster_config(project)
    mutate(values)

    with pytest.raises(ConfigError, match=message):
        write_config(project, values)


def test_cluster_operation_requires_configured_target_and_aligned_repository_paths(
    project: FixtureProject,
) -> None:
    _write_t25a_manifest(project)
    plan = _t25a_plan(project)

    with pytest.raises(ClusterOperationError, match="requires cluster_mutation"):
        validate_cluster_operations_for_plan(config=project.config, plan=plan)

    config = write_config(project, _cluster_config(project))
    plan_values = plan.model_dump(mode="json")
    plan_values["steps"][0]["cluster_operation"]["operation_manifest_path"] = "deploy/outside.yaml"
    with pytest.raises(ClusterOperationError, match="outside target operation_manifest_roots"):
        validate_cluster_operations_for_plan(
            config=config,
            plan=NormalizedPlan.model_validate(plan_values),
        )


def test_cluster_operation_rejects_source_file_symlink_outside_allowed_root(
    project: FixtureProject,
) -> None:
    _write_t25a_manifest(project)
    outside_source_root = project.repository / "other.yaml"
    outside_source_root.write_text("kind: Deployment\n", encoding="utf-8")
    allowed_file = project.repository / "deploy/manifests/sample-app.yaml"
    allowed_file.unlink()
    allowed_file.symlink_to(outside_source_root)
    config = write_config(project, _cluster_config(project))

    with pytest.raises(ClusterOperationError, match="outside target source_file_roots"):
        validate_cluster_operations_for_plan(config=config, plan=_t25a_plan(project))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda manifest: manifest["source_identity"].update({"repository_id": "other-repo"}),
            "source_identity.repository_id mismatch",
        ),
        (lambda manifest: manifest.update({"context": "other-context"}), "context does not match"),
        (
            lambda manifest: manifest["allowed_files"][0].update({"path": "other/sample.yaml"}),
            "outside target source_file_roots",
        ),
        (_move_chart_outside_source_root, "chart_path is outside target source_file_roots"),
        (
            lambda manifest: manifest["actions"][0]["manifest_files"][0].update(
                {"path": "deploy/manifests/undeclared.yaml"}
            ),
            "must appear exactly in allowed_files",
        ),
        (lambda manifest: manifest["actions"][0].update({"timeout_seconds": 121}), "timeout exceeds"),
    ],
)
def test_static_validation_aligns_manifest_with_target_and_plan(
    project: FixtureProject,
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    manifest = _t25a_manifest()
    mutate(manifest)
    _write_t25a_manifest(project, manifest)
    config = write_config(project, _cluster_config(project))

    with pytest.raises(ClusterOperationError, match=message):
        validate_cluster_operations_for_plan(config=config, plan=_t25a_plan(project))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda manifest: manifest["actions"][2].update(
                {"resource": deepcopy(manifest["actions"][0]["expected_resources"][0])}
            ),
            "exact core v1 Service",
        ),
        (
            lambda manifest: manifest["actions"][2].update(
                {
                    "expected_resources": [
                        *manifest["actions"][2]["expected_resources"],
                        deepcopy(manifest["actions"][0]["expected_resources"][0]),
                    ]
                }
            ),
            "only its exact Service",
        ),
        (
            lambda manifest: manifest["actions"].insert(
                4,
                {
                    **deepcopy(manifest["actions"][2]),
                    "action_id": "second-forward",
                    "local_port": 18080,
                },
            ),
            "local_port values must not repeat",
        ),
        (
            lambda manifest: manifest["actions"][3].update({"port_forward_action_id": "missing"}),
            "every port_forward requires exactly one linked TLS/DC8 probe",
        ),
        (
            lambda manifest: manifest["actions"][2].update({"lifetime_timeout_seconds": 121}),
            "port_forward timeout exceeds",
        ),
    ],
)
def test_port_forward_and_tls_dc8_contracts_are_exact_and_bounded(
    project: FixtureProject,
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    manifest = _t25a_manifest()
    mutate(manifest)
    _write_t25a_manifest(project, manifest)
    config = write_config(project, _cluster_config(project))

    with pytest.raises(ClusterOperationError, match=message):
        validate_cluster_operations_for_plan(config=config, plan=_t25a_plan(project))


@pytest.mark.parametrize(
    ("shape", "mutate", "sensitive_value"),
    [
        ("unknown action", lambda manifest: manifest["actions"][0].update({"action": "kubectl_apply"}), None),
        ("generic argv", lambda manifest: manifest["actions"][0].update({"argv": ["kubectl"]}), None),
        ("shell", lambda manifest: manifest["actions"][0].update({"shell": "sh -c true"}), None),
        ("port-forward host", lambda manifest: manifest["actions"][2].update({"host": "example.invalid"}), None),
        (
            "TLS client certificate",
            lambda manifest: manifest["actions"][3].update({"client_certificate": "client.pem"}),
            None,
        ),
        ("TLS request payload", lambda manifest: manifest["actions"][3].update({"payload": "GET /"}), None),
        ("Helm repository", lambda manifest: manifest["actions"][1].update({"repository": "https://example.invalid"}), None),
        ("OCI chart URL", lambda manifest: manifest["actions"][1].update({"chart_path": "oci://registry/chart"}), None),
        ("Helm --set", lambda manifest: manifest["actions"][1].update({"--set": "image.tag=latest"}), None),
        (
            "inline Secret object",
            lambda manifest: manifest["actions"][0].update({"objects": [{"kind": "Secret"}]}),
            None,
        ),
        ("Secret data", lambda manifest: manifest["actions"][0].update({"data": {"key": "value"}}), None),
        (
            "Secret stringData",
            lambda manifest: manifest["actions"][0].update({"stringData": {"key": "value"}}),
            None,
        ),
        (
            "PEM value",
            lambda manifest: manifest.update({"private_key": "-----BEGIN PRIVATE KEY-----PRIVATE_MATERIAL"}),
            "PRIVATE_MATERIAL",
        ),
        (
            "token value",
            lambda manifest: manifest.update({"token": "TOKEN_MATERIAL_DO_NOT_PERSIST"}),
            "TOKEN_MATERIAL_DO_NOT_PERSIST",
        ),
        (
            "kubeconfig value",
            lambda manifest: manifest.update({"kubeconfig": "apiVersion: v1\nusers:"}),
            None,
        ),
        (
            "bound revision before approval",
            lambda manifest: manifest["source_identity"].update({"revision": "a" * 40}),
            None,
        ),
        (
            "environment expansion",
            lambda manifest: manifest["allowed_files"][0].update({"path": "$HOME/manifest.yaml"}),
            None,
        ),
        (
            "arbitrary field manager",
            lambda manifest: manifest["actions"][0].update({"field_manager": "operator"}),
            None,
        ),
        (
            "wildcard path",
            lambda manifest: manifest["allowed_files"][0].update({"path": "deploy/manifests/*.yaml"}),
            None,
        ),
        (
            "absolute path",
            lambda manifest: manifest["allowed_files"][0].update({"path": "/etc/passwd"}),
            None,
        ),
        (
            "traversal path",
            lambda manifest: manifest["allowed_files"][0].update({"path": "../outside.yaml"}),
            None,
        ),
        (
            "Secret requirement value",
            lambda manifest: manifest["secret_requirements"][0].update({"value": "SECRET_VALUE_DO_NOT_PERSIST"}),
            "SECRET_VALUE_DO_NOT_PERSIST",
        ),
        ("unknown field", lambda manifest: manifest.update({"unexpected": True}), None),
    ],
)
def test_dangerous_manifest_shapes_are_rejected_without_echoing_secret_values(
    project: FixtureProject,
    shape: str,
    mutate: Callable[[dict[str, Any]], None],
    sensitive_value: str | None,
) -> None:
    manifest = deepcopy(_t25a_manifest())
    mutate(manifest)
    path = project.root / f"dangerous-{shape.replace(' ', '-')}.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    with pytest.raises(ClusterOperationError) as exc_info:
        load_cluster_operation_manifest(path)

    if sensitive_value is not None:
        assert sensitive_value not in str(exc_info.value)
