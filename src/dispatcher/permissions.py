"""Compile explicit dispatcher policy layers into OpenCode permission rules."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from .config import PERMISSION_ACTIONS, Config, PermissionPolicy
from .mcp import compile_mcp_permissions
from .security import atomic_write_private_text

PermissionDecision = Literal["allow", "ask", "deny"]
RoleKind = Literal["supervisor", "executor", "reviewer"]

READ_ONLY_NATIVE_TOOLS = ("read", "glob", "grep")
READ_ONLY_DIAGNOSTIC_COMMANDS = (
    "pwd",
    "ls",
    "git status --porcelain=v1",
    "git branch --show-current",
    "git rev-parse HEAD",
    "git diff --no-ext-diff --no-textconv",
)

# Each semantic action maps to the narrowest supported OpenCode permission pattern.
_ACTION_RULES: dict[str, dict[str, PermissionDecision | dict[str, PermissionDecision]]] = {
    "inspect": {tool: "allow" for tool in READ_ONLY_NATIVE_TOOLS},
    "modify": {"edit": "allow", "write": "allow"},
    "verify": {
        "bash": {
            "pytest *": "allow",
            "ruff *": "allow",
            "mypy *": "allow",
            "shasum *": "allow",
            "sha256sum *": "allow",
            "ls *": "allow",
            "wc *": "allow",
            "stat *": "allow",
        }
    },
    "commit": {
        "bash": {
            "git add *": "allow",
            "git status *": "allow",
            "git diff *": "allow",
            "git rev-parse *": "allow",
            "git commit *": "allow",
        }
    },
    "push": {"bash": {"git push *": "allow"}},
    "force_push": {"bash": {"git push --force *": "allow"}},
    "create_branch": {"bash": {"git branch *": "allow"}},
}

if tuple(_ACTION_RULES) != PERMISSION_ACTIONS:
    raise RuntimeError("permission action schema and OpenCode rule mapping are out of sync")


class PermissionError(ValueError):
    """A policy layer or dispatch authorization was incomplete or invalid."""


def role_scoped_authorized_actions(
    authorized_actions: Iterable[str],
    role_kind: RoleKind,
) -> tuple[str, ...]:
    """Narrow ordered step authorization to the actions available to one role kind."""
    actions = tuple(authorized_actions)
    if role_kind == "executor":
        return tuple(action for action in actions if action in {"inspect", "modify"})
    if role_kind == "reviewer":
        if "inspect" not in actions:
            raise PermissionError(
                "reviewer dispatch requires step authorization to include inspect"
            )
        return ("inspect",)
    if role_kind == "supervisor":
        return ("inspect",)
    raise PermissionError(f"unknown role kind for authorization scoping: {role_kind}")


def read_only_diagnostic_bash_rules() -> dict[str, PermissionDecision]:
    """Return the non-overridable deny-first exact diagnostic Bash rules."""
    return {
        "*": "deny",
        **{command: "allow" for command in READ_ONLY_DIAGNOSTIC_COMMANDS},
    }


def compile_effective_policy(
    config: Config,
    *,
    repo_id: str,
    role_key: str,
    dispatch_authorized_actions: Iterable[str],
) -> dict[str, Any]:
    """Compile global through concrete-role layers for one authorized dispatch.

    Layer precedence is global, project, repository, role class, then concrete
    role, with later configured layers overriding earlier decisions. Dispatch
    authorization then denies every undeclared semantic action, and immutable
    role ceilings are applied last.
    """
    layers = config.permission_policy_layers(repo_id=repo_id, role_key=role_key)
    rules = compile_policy_layers(layers, dispatch_authorized_actions)
    if config.role_kind(role_key) in {"reviewer", "supervisor"}:
        rules["edit"] = "deny"
        rules["write"] = "deny"
        rules["bash"] = read_only_diagnostic_bash_rules()
    mcp_tools = compile_mcp_permissions(config, role_key)
    if mcp_tools and rules.get("*") != "deny":
        raise PermissionError(
            "role MCP tools require the compiled permission default to be deny so that "
            "unlisted MCP methods stay denied"
        )
    rules.update(mcp_tools)
    return rules


def compile_policy_layers(
    layers: Iterable[PermissionPolicy],
    dispatch_authorized_actions: Iterable[str],
) -> dict[str, Any]:
    """Compile already-validated layers without relying on free-form manifests."""
    layer_list = tuple(layers)
    if not layer_list:
        raise PermissionError("at least one permission policy layer is required")

    effective_default: PermissionDecision = "deny"
    action_decisions: dict[str, PermissionDecision] = {}
    for layer in layer_list:
        effective_default = layer.default
        for action, decision in layer.actions.items():
            action_decisions[action] = decision

    authorized = set(dispatch_authorized_actions)
    unknown_authorizations = authorized - set(_ACTION_RULES)
    if unknown_authorizations:
        unknown = ", ".join(sorted(unknown_authorizations))
        raise PermissionError(f"unknown dispatch authorization actions: {unknown}")

    rules: dict[str, Any] = {"*": effective_default}
    for configured_action, decision in action_decisions.items():
        if configured_action not in _ACTION_RULES:
            raise PermissionError(f"unknown configured permission action: {configured_action}")
        if configured_action not in authorized:
            decision = "deny"
        _merge_action_rules(rules, _ACTION_RULES[configured_action], decision)
    return rules


def generate_opencode_config(
    permission_rules: Mapping[str, Any],
    *,
    mcp_servers: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate the isolated inline OpenCode configuration for one child process."""
    if "*" not in permission_rules:
        raise PermissionError("compiled OpenCode permission rules must preserve the global default")
    payload: dict[str, Any] = {"permission": dict(permission_rules)}
    if mcp_servers:
        payload["mcp"] = dict(mcp_servers)
    return payload


def should_auto_approve(permission_rules: Mapping[str, Any]) -> bool:
    """Enable ``--auto`` only when no applicable OpenCode operation asks."""
    return not _contains_ask(permission_rules)


def write_opencode_config(
    permissions_dir: str,
    role_pool: str,
    role_key: str,
    config_payload: Mapping[str, Any],
) -> str:
    """Write a private policy snapshot for auditability without exposing secrets."""
    filename = f"opencode-{role_pool}-{role_key}.json"
    path = f"{permissions_dir}/{filename}"
    atomic_write_private_text(path, json.dumps(config_payload, indent=2, sort_keys=True) + "\n")
    return path


def opencode_config_env(config_payload: Mapping[str, Any]) -> str:
    """Return the exact JSON payload assigned to ``OPENCODE_CONFIG_CONTENT``."""
    return json.dumps(config_payload, separators=(",", ":"), sort_keys=True)


def _merge_action_rules(
    target: dict[str, Any],
    action_rules: Mapping[str, PermissionDecision | Mapping[str, PermissionDecision]],
    decision: PermissionDecision,
) -> None:
    for tool, rule in action_rules.items():
        if isinstance(rule, dict):
            target.setdefault(tool, {})
            if not isinstance(target[tool], dict):
                raise PermissionError(f"conflicting rule shape for OpenCode tool {tool}")
            for pattern in rule:
                target[tool][pattern] = decision
        else:
            target[tool] = decision


def _contains_ask(value: Mapping[str, Any] | Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_ask(item) for item in value.values())
    return value == "ask"
