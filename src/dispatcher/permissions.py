"""Compile explicit dispatcher policy layers into OpenCode permission rules."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from .config import Config, PermissionPolicy
from .security import atomic_write_private_text

PermissionDecision = Literal["allow", "ask", "deny"]

# Each semantic action maps to the narrowest supported OpenCode permission pattern.
_ACTION_RULES: dict[str, dict[str, PermissionDecision | dict[str, PermissionDecision]]] = {
    "inspect": {"read": "allow", "glob": "allow", "grep": "allow"},
    "modify": {"edit": "allow", "write": "allow"},
    "verify": {
        "bash": {
            "pytest *": "allow",
            "ruff *": "allow",
            "mypy *": "allow",
        }
    },
    "commit": {"bash": {"git commit *": "allow"}},
    "push": {"bash": {"git push *": "allow"}},
    "force_push": {"bash": {"git push --force *": "allow"}},
    "create_branch": {"bash": {"git branch *": "allow"}},
}


class PermissionError(ValueError):
    """A policy layer or dispatch authorization was incomplete or invalid."""


def compile_effective_policy(
    config: Config,
    *,
    repo_id: str,
    role_key: str,
    dispatch_authorized_actions: Iterable[str],
) -> dict[str, Any]:
    """Compile global through concrete-role layers for one authorized dispatch.

    Layer precedence is global, project, repository, role class, concrete role,
    then dispatch authorization.  The final authorization can only tighten a
    policy: any undeclared semantic action compiles to an explicit denial.
    """
    layers = config.permission_policy_layers(repo_id=repo_id, role_key=role_key)
    return compile_policy_layers(layers, dispatch_authorized_actions)


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
        action_decisions.update(layer.actions)

    authorized = set(dispatch_authorized_actions)
    unknown_authorizations = authorized - set(_ACTION_RULES)
    if unknown_authorizations:
        unknown = ", ".join(sorted(unknown_authorizations))
        raise PermissionError(f"unknown dispatch authorization actions: {unknown}")

    rules: dict[str, Any] = {"*": effective_default}
    for action, decision in action_decisions.items():
        if action not in _ACTION_RULES:
            raise PermissionError(f"unknown configured permission action: {action}")
        if action not in authorized:
            decision = "deny"
        _merge_action_rules(rules, _ACTION_RULES[action], decision)
    return rules


def generate_opencode_config(permission_rules: Mapping[str, Any]) -> dict[str, Any]:
    """Generate the isolated inline OpenCode configuration for one child process."""
    if "*" not in permission_rules:
        raise PermissionError("compiled OpenCode permission rules must preserve the global default")
    return {"permission": dict(permission_rules)}


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
