from __future__ import annotations

import json
import stat
from pathlib import Path

from helpers import config_values, create_fixture_project, write_config

from dispatcher.permissions import (
    compile_effective_policy,
    generate_opencode_config,
    opencode_config_env,
    should_auto_approve,
    write_opencode_config,
)


def test_dispatch_authorization_tightens_the_effective_executor_policy(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)

    rules = compile_effective_policy(
        project.config,
        repo_id="fixture-repo",
        role_key="terra",
        dispatch_authorized_actions=["inspect"],
    )

    assert rules == {
        "*": "deny",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "edit": "deny",
        "write": "deny",
        "bash": {
            "pytest *": "deny",
            "ruff *": "deny",
            "mypy *": "deny",
            "shasum *": "deny",
            "sha256sum *": "deny",
            "ls *": "deny",
            "wc *": "deny",
            "stat *": "deny",
        },
    }
    assert should_auto_approve(rules)
    assert generate_opencode_config(rules)["permission"]["*"] == "deny"


def test_concrete_role_ask_rule_prevents_auto_approval(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["permission_policies"]["policies"]["executor"]["actions"] = {"inspect": "ask"}
    config = write_config(project, values)

    rules = compile_effective_policy(
        config,
        repo_id="fixture-repo",
        role_key="terra",
        dispatch_authorized_actions=["inspect"],
    )

    assert rules["read"] == "ask"
    assert rules["glob"] == "ask"
    assert not should_auto_approve(rules)


def test_policy_environment_payload_and_audit_snapshot_are_deterministic_and_private(
    tmp_path: Path,
) -> None:
    payload = generate_opencode_config({"*": "deny", "read": "allow"})

    encoded = opencode_config_env(payload)
    path = Path(write_opencode_config(str(tmp_path), "executors", "terra", payload))

    assert json.loads(encoded) == payload
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
