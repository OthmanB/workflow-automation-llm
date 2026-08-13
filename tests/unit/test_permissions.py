from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from helpers import config_values, create_fixture_project, write_config

from dispatcher.config import PERMISSION_ACTIONS, ConfigError
from dispatcher.permissions import (
    READ_ONLY_DIAGNOSTIC_COMMANDS,
    READ_ONLY_NATIVE_TOOLS,
    PermissionError,
    compile_effective_policy,
    compile_policy_layers,
    generate_opencode_config,
    opencode_config_env,
    read_only_diagnostic_bash_rules,
    role_scoped_authorized_actions,
    should_auto_approve,
    write_opencode_config,
)

_ALL_ACTIONS = (
    "inspect",
    "modify",
    "verify",
    "commit",
    "push",
    "force_push",
    "create_branch",
)

_EXPECTED_DIAGNOSTIC_BASH = {
    "*": "deny",
    "pwd": "allow",
    "ls": "allow",
    "git status --porcelain=v1": "allow",
    "git branch --show-current": "allow",
    "git rev-parse HEAD": "allow",
    "git diff --no-ext-diff --no-textconv": "allow",
}

_DENIED_DIAGNOSTIC_VARIANTS = (
    "ls -la",
    "ls .",
    "ls > marker.txt",
    "ls >> marker.txt",
    "ls | tee marker.txt",
    "ls && git status --porcelain=v1",
    "pwd > marker.txt",
    "git status",
    "git status --short",
    "git status --porcelain=v1 > marker.txt",
    "git branch",
    "git branch new-branch",
    "git branch --delete branch-name",
    "git rev-parse --show-toplevel",
    "git rev-parse HEAD > marker.txt",
    "git diff",
    "git diff --stat",
    "git diff --no-ext-diff --no-textconv > marker.txt",
    "pytest -q",
    "python -m pytest",
    "ruff check",
    "mypy .",
    "git add file",
    "git commit -m message",
    "git push",
)


def test_permission_rule_mapping_uses_the_canonical_action_order() -> None:
    assert _ALL_ACTIONS == PERMISSION_ACTIONS


def test_executor_role_scoping_removes_dispatcher_owned_and_git_actions() -> None:
    actions = ("verify", "inspect", "commit", "modify")

    assert role_scoped_authorized_actions(actions, "executor") == (
        "inspect",
        "modify",
    )


def test_reviewer_role_scoping_returns_inspect_only() -> None:
    assert role_scoped_authorized_actions(_ALL_ACTIONS, "reviewer") == ("inspect",)


def test_reviewer_role_scoping_requires_step_inspect_authorization() -> None:
    with pytest.raises(PermissionError, match="requires step authorization to include inspect"):
        role_scoped_authorized_actions(("modify", "commit"), "reviewer")


def test_supervisor_role_scoping_returns_inspect_only() -> None:
    assert role_scoped_authorized_actions(_ALL_ACTIONS, "supervisor") == ("inspect",)


def test_unknown_role_kind_fails_loudly() -> None:
    with pytest.raises(PermissionError, match="unknown role kind"):
        role_scoped_authorized_actions(_ALL_ACTIONS, "unknown")  # type: ignore[arg-type]


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
            "git add *": "deny",
            "git status *": "deny",
            "git diff *": "deny",
            "git rev-parse *": "deny",
            "git commit *": "deny",
            "git push *": "deny",
            "git push --force *": "deny",
            "git branch *": "deny",
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


def test_permissive_config_cannot_grant_reviewer_mutation_or_unlisted_bash(
    tmp_path: Path,
) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    for policy in values["permission_policies"]["policies"].values():
        policy["default"] = "allow"
        policy["actions"] = {action: "allow" for action in _ALL_ACTIONS}
    config = write_config(project, values)

    rules = compile_effective_policy(
        config,
        repo_id="fixture-repo",
        role_key="reviewer",
        dispatch_authorized_actions=_ALL_ACTIONS,
    )

    assert rules["edit"] == "deny"
    assert rules["write"] == "deny"
    assert rules["bash"] == _EXPECTED_DIAGNOSTIC_BASH


def test_reviewer_class_silence_on_commit_is_a_config_error(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    del values["permission_policies"]["policies"]["reviewer-class"]["actions"]["commit"]

    with pytest.raises(ConfigError, match="reviewer.*missing: commit"):
        write_config(project, values)


def test_concrete_reviewer_allow_all_cannot_bypass_ceiling(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["permission_policies"]["policies"]["reviewer"]["actions"] = {
        action: "allow" for action in _ALL_ACTIONS
    }
    config = write_config(project, values)

    rules = compile_effective_policy(
        config,
        repo_id="fixture-repo",
        role_key="reviewer",
        dispatch_authorized_actions=_ALL_ACTIONS,
    )

    assert rules["edit"] == "deny"
    assert rules["write"] == "deny"
    assert rules["bash"] == _EXPECTED_DIAGNOSTIC_BASH


def test_permissive_config_cannot_grant_supervisor_mutation_or_unlisted_bash(
    tmp_path: Path,
) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    for policy in values["permission_policies"]["policies"].values():
        policy["default"] = "allow"
        policy["actions"] = {action: "allow" for action in _ALL_ACTIONS}
    config = write_config(project, values)

    rules = compile_effective_policy(
        config,
        repo_id="fixture-repo",
        role_key="supervisor",
        dispatch_authorized_actions=_ALL_ACTIONS,
    )

    assert rules["edit"] == "deny"
    assert rules["write"] == "deny"
    assert rules["bash"] == _EXPECTED_DIAGNOSTIC_BASH


def test_read_only_diagnostic_bash_rules_are_exact_and_deny_unlisted_commands() -> None:
    rules = read_only_diagnostic_bash_rules()

    assert READ_ONLY_DIAGNOSTIC_COMMANDS == (
        "pwd",
        "ls",
        "git status --porcelain=v1",
        "git branch --show-current",
        "git rev-parse HEAD",
        "git diff --no-ext-diff --no-textconv",
    )
    assert rules == _EXPECTED_DIAGNOSTIC_BASH
    assert list(rules) == ["*", *READ_ONLY_DIAGNOSTIC_COMMANDS]
    assert all(command not in rules for command in _DENIED_DIAGNOSTIC_VARIANTS)
    assert all(
        rules.get(command, rules["*"]) == "deny"
        for command in _DENIED_DIAGNOSTIC_VARIANTS
    )
    assert all(
        "*" not in pattern and "?" not in pattern
        for pattern, decision in rules.items()
        if decision == "allow"
    )


def test_read_only_native_tools_are_exact_and_compile_as_inspection_only(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    rules = compile_effective_policy(
        project.config,
        repo_id="fixture-repo",
        role_key="reviewer",
        dispatch_authorized_actions=["inspect"],
    )

    assert READ_ONLY_NATIVE_TOOLS == ("read", "glob", "grep")
    assert {tool: rules[tool] for tool in READ_ONLY_NATIVE_TOOLS} == {
        tool: "allow" for tool in READ_ONLY_NATIVE_TOOLS
    }
    assert rules["edit"] == rules["write"] == "deny"


def test_opencode_environment_serialization_preserves_safe_bash_rule_order() -> None:
    payload = generate_opencode_config(
        {"*": "deny", "bash": read_only_diagnostic_bash_rules()}
    )

    first = opencode_config_env(payload)
    second = opencode_config_env(payload)
    round_tripped = json.loads(first)
    bash_keys = list(round_tripped["permission"]["bash"])

    assert first == second
    assert bash_keys == [
        "*",
        "git branch --show-current",
        "git diff --no-ext-diff --no-textconv",
        "git rev-parse HEAD",
        "git status --porcelain=v1",
        "ls",
        "pwd",
    ]
    assert opencode_config_env(round_tripped) == first
    assert round_tripped["permission"]["bash"] == _EXPECTED_DIAGNOSTIC_BASH


def test_authorized_executor_policy_keeps_writes_but_denies_raw_git(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    for policy in values["permission_policies"]["policies"].values():
        policy["actions"] = {action: "allow" for action in _ALL_ACTIONS}
    config = write_config(project, values)
    scoped_actions = role_scoped_authorized_actions(_ALL_ACTIONS, "executor")
    expected = compile_policy_layers(
        config.permission_policy_layers(repo_id="fixture-repo", role_key="terra"),
        scoped_actions,
    )

    rules = compile_effective_policy(
        config,
        repo_id="fixture-repo",
        role_key="terra",
        dispatch_authorized_actions=scoped_actions,
    )

    assert rules == expected
    assert rules["edit"] == "allow"
    assert rules["write"] == "allow"
    assert all(decision == "deny" for decision in rules["bash"].values())
    for command in (
        "git add *",
        "git commit *",
        "git push *",
        "git push --force *",
        "git branch *",
        "pytest *",
    ):
        assert rules["bash"][command] == "deny"


def test_policy_environment_payload_and_audit_snapshot_are_deterministic_and_private(
    tmp_path: Path,
) -> None:
    payload = generate_opencode_config({"*": "deny", "read": "allow"})

    encoded = opencode_config_env(payload)
    path = Path(write_opencode_config(str(tmp_path), "executors", "terra", payload))

    assert json.loads(encoded) == payload
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _mcp_config(
    project,
    *,
    supervisor_tools: list[str] | None = None,
    executor_tools: list[str] | None = None,
    reviewer_tools: list[str] | None = None,
):
    values = config_values(project)
    values["mcp"] = {
        "environment_passthrough": [],
        "servers": {
            "fixture": {
                "type": "local",
                "enabled": True,
                "command": ["/usr/bin/fixture-mcp"],
                "environment": {},
            }
        },
    }
    assignments = {
        "supervisor": supervisor_tools or ["fixture_echo"],
        "terra": executor_tools or [],
        "reviewer": reviewer_tools or ["fixture_echo"],
    }
    for pool in values["roles"].values():
        for role_key in pool:
            pool[role_key]["mcp_tools"] = assignments.get(role_key, [])
    return write_config(project, values)


def test_role_mcp_tools_compile_to_exact_allow_entries_without_wildcards(
    tmp_path: Path,
) -> None:
    project = create_fixture_project(tmp_path)
    config = _mcp_config(project, reviewer_tools=["fixture_echo", "fixture_probe"])

    rules = compile_effective_policy(
        config,
        repo_id="fixture-repo",
        role_key="reviewer",
        dispatch_authorized_actions=["inspect"],
    )
    payload = generate_opencode_config(rules)

    assert rules["fixture_echo"] == "allow"
    assert rules["fixture_probe"] == "allow"
    assert "fixture_*" not in rules
    assert payload["permission"]["*"] == "deny"
    assert rules["edit"] == "deny"
    assert rules["write"] == "deny"


def test_empty_mcp_role_gets_no_mcp_allow_entries(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    config = _mcp_config(project, executor_tools=[])

    rules = compile_effective_policy(
        config,
        repo_id="fixture-repo",
        role_key="terra",
        dispatch_authorized_actions=["inspect", "modify"],
    )

    assert "fixture_echo" not in rules
    assert rules["read"] == "allow"
    assert rules["edit"] == "allow"


def test_permissive_default_with_mcp_tools_rejects_to_keep_unlisted_methods_denied(
    tmp_path: Path,
) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    for policy in values["permission_policies"]["policies"].values():
        policy["default"] = "allow"
        policy["actions"] = {action: "allow" for action in _ALL_ACTIONS}
    values["mcp"] = {
        "environment_passthrough": [],
        "servers": {
            "fixture": {
                "type": "local",
                "enabled": True,
                "command": ["/usr/bin/fixture-mcp"],
                "environment": {},
            }
        },
    }
    for pool in values["roles"].values():
        for role_key in pool:
            pool[role_key]["mcp_tools"] = ["fixture_echo"]
    config = write_config(project, values)

    with pytest.raises(PermissionError, match="compiled permission default to be deny"):
        compile_effective_policy(
            config,
            repo_id="fixture-repo",
            role_key="reviewer",
            dispatch_authorized_actions=["inspect"],
        )


def test_reviewer_and_supervisor_mutation_ceilings_survive_mcp_tools(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    config = _mcp_config(project, reviewer_tools=["fixture_echo"])

    for role_key in ("reviewer", "supervisor"):
        rules = compile_effective_policy(
            config,
            repo_id="fixture-repo",
            role_key=role_key,
            dispatch_authorized_actions=["inspect"],
        )
        assert rules["fixture_echo"] == "allow"
        assert rules["edit"] == "deny"
        assert rules["write"] == "deny"
        assert rules["bash"] == read_only_diagnostic_bash_rules()
