from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from helpers import FixtureProject, config_values, create_fixture_project

from dispatcher.config import ConfigError, load_config


@pytest.fixture
def project(tmp_path: Path) -> FixtureProject:
    return create_fixture_project(tmp_path)


def _write_and_load(project: FixtureProject, values: dict[str, Any]) -> None:
    project.config_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    load_config(project.config_path)


def test_minimal_schema_v1_fixture_loads(project: FixtureProject) -> None:
    config = project.config

    assert config.project_id == "fixture-project"
    assert config.default_repository_id == "fixture-repo"
    assert config.role_kind("terra") == "executor"
    assert config.role_kind("reviewer") == "reviewer"
    assert config.profiles.default == "balanced"
    assert len(config.config_digest) == 64
    assert config.model_json_schema()["properties"]["schema_version"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda values: values.update({"unknown": True}), "unknown: Extra inputs are not permitted"),
        (
            lambda values: values["execution"].update({"timeout_seconds": "60"}),
            "execution.timeout_seconds: Input should be a valid integer",
        ),
        (
            lambda values: values["state"].update({"lease_stale_after_seconds": 30}),
            "state: Value error, lease_stale_after_seconds must exceed lease_heartbeat_seconds",
        ),
        (
            lambda values: values["execution"].update({"mode": "real"}),
            "execution.mode: Input should be 'mock_workflow_test' or 'real_operation'",
        ),
        (
            lambda values: values["execution"].update({"max_rounds_per_step": 0}),
            "execution.max_rounds_per_step: Input should be greater than or equal to 1",
        ),
        (
            lambda values: values["roles"]["executors"]["terra"].update(
                {"model": "invalid-model"}
            ),
            "roles.executors.terra.model: String should match pattern",
        ),
        (
            lambda values: values.update({"policy": {}}),
            "policy: Extra inputs are not permitted",
        ),
    ],
)
def test_invalid_schema_values_fail_with_exact_field_paths(
    project: FixtureProject,
    mutate: Any,
    message: str,
) -> None:
    values = config_values(project)
    mutate(values)

    with pytest.raises(ConfigError, match=message):
        _write_and_load(project, values)


@pytest.mark.parametrize(
    "path",
    [
        ("schema_version",),
        ("state", "directory"),
        ("state", "lease_heartbeat_seconds"),
        ("profile", "profiles_file"),
        ("execution", "timeout_seconds"),
        ("execution", "concurrency"),
        ("repositories", "fixture-repo", "commit_policy"),
        ("repositories", "fixture-repo", "external_roots"),
        ("permission_policies", "policies", "executor", "default"),
    ],
)
def test_missing_required_values_fail_before_preflight(
    project: FixtureProject,
    path: tuple[str, ...],
) -> None:
    values = config_values(project)
    parent: dict[str, Any] = values
    for part in path[:-1]:
        parent = parent[part]
    del parent[path[-1]]

    with pytest.raises(ConfigError, match="Field required"):
        _write_and_load(project, values)


def test_relative_paths_are_independent_of_current_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_fixture_project(tmp_path, config_relative_paths=True)
    expected_root = project.config.default_repository.root
    expected_state = project.config.state_dir
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    reloaded = load_config(project.config_path)

    assert reloaded.default_repository.root == expected_root
    assert reloaded.state_dir == expected_state


def test_repository_default_branch_accepts_standard_git_ref_with_slashes(project: FixtureProject) -> None:
    values = config_values(project)
    values["repositories"]["fixture-repo"]["default_branch"] = "release/tier1"

    _write_and_load(project, values)


def test_profile_selection_must_exist_in_strict_profiles_document(project: FixtureProject) -> None:
    values = config_values(project)
    values["profile"]["profile_id"] = "missing"

    with pytest.raises(ConfigError, match="profile.profile_id must reference"):
        _write_and_load(project, values)


def test_unquoted_yaml_boolean_profile_value_is_rejected(project: FixtureProject) -> None:
    project.profiles_path.write_text(
        """\
schema_version: 1
profiles:
  balanced:
    review_schedule: critical
    multi_review: on
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="profiles.balanced.multi_review"):
        load_config(project.config_path)


def test_two_sibling_repositories_validate_with_exact_ids(project: FixtureProject) -> None:
    sibling = project.root / "sibling"
    sibling_evidence = sibling / "evidence"
    sibling_evidence.mkdir(parents=True)
    _initialize_repository(sibling, "https://example.invalid/sibling.git")
    values = config_values(project)
    values["repositories"]["sibling-repo"] = {
        "root": str(sibling),
        "expected_remote": {"name": "origin", "url": "https://example.invalid/sibling.git"},
        "default_branch": "main",
        "evidence_roots": ["evidence"],
        "writable_roots": ["."],
        "external_roots": [],
        "commit_policy": "required",
        "permission_policy": "executor",
        "allow_shared_writable_roots": False,
    }

    _write_and_load(project, values)


def test_duplicate_repository_roots_are_rejected(project: FixtureProject) -> None:
    values = config_values(project)
    values["repositories"]["duplicate-repo"] = dict(values["repositories"]["fixture-repo"])

    with pytest.raises(ConfigError, match="repository roots must be unique"):
        _write_and_load(project, values)


def test_evidence_symlink_escape_is_rejected(project: FixtureProject) -> None:
    outside = project.root / "outside"
    outside.mkdir()
    (project.repository / "escaped").symlink_to(outside, target_is_directory=True)
    values = config_values(project)
    values["repositories"]["fixture-repo"]["evidence_roots"] = ["escaped"]

    with pytest.raises(ConfigError, match="path escapes repository root"):
        _write_and_load(project, values)


def test_remote_mismatch_is_rejected(project: FixtureProject) -> None:
    values = config_values(project)
    values["repositories"]["fixture-repo"]["expected_remote"]["url"] = (
        "https://example.invalid/incorrect.git"
    )

    with pytest.raises(ConfigError, match="expected_remote.url mismatch"):
        _write_and_load(project, values)


def _initialize_repository(path: Path, remote_url: str) -> None:
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", remote_url],
        check=True,
        capture_output=True,
        text=True,
    )
