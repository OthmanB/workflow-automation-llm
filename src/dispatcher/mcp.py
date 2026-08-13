"""Compile exact per-role MCP capabilities for dispatcher child processes."""

from __future__ import annotations

from typing import Any

from .config import MCP_TOOL_CATALOG, Config, MCPServerDefinition

__all__ = [
    "MCPCompileError",
    "collect_role_mcp_environment",
    "compile_mcp_permissions",
    "compile_role_mcp_servers",
    "resolve_role_mcp_tools",
]


class MCPCompileError(ValueError):
    """A role MCP assignment cannot compile to an exact OpenCode configuration."""


def resolve_role_mcp_tools(config: Config, role_key: str) -> tuple[str, ...]:
    """Return the exact ordered MCP tool list configured for one role."""
    return config.role(role_key).mcp_tools


def _selected_servers(
    config: Config,
    tools: tuple[str, ...],
    *,
    role_key: str,
) -> dict[str, MCPServerDefinition]:
    servers: dict[str, MCPServerDefinition] = {}
    for tool in tools:
        server_key = MCP_TOOL_CATALOG.get(tool)
        if server_key is None:
            raise MCPCompileError(
                f"role {role_key} tool {tool!r} does not reference an enabled MCP server"
            )
        server = config.model.mcp.servers.get(server_key)
        if server is None or not server.enabled:
            raise MCPCompileError(
                f"role {role_key} tool {tool!r} does not reference an enabled MCP server"
            )
        servers[server_key] = server
    return servers


def compile_role_mcp_servers(config: Config, role_key: str) -> dict[str, Any]:
    """Compile the OpenCode ``mcp`` object containing only servers the role uses."""
    tools = resolve_role_mcp_tools(config, role_key)
    servers = _selected_servers(config, tools, role_key=role_key)
    compiled: dict[str, Any] = {}
    for name in sorted(servers):
        server = servers[name]
        if server.type == "local":
            compiled[name] = {
                "type": "local",
                "command": list(server.command),
                "environment": dict(server.environment),
                "enabled": True,
            }
        else:
            entry: dict[str, Any] = {
                "type": "remote",
                "url": server.url,
                "enabled": True,
            }
            if server.headers:
                entry["headers"] = dict(server.headers)
            compiled[name] = entry
    return compiled


def compile_mcp_permissions(config: Config, role_key: str) -> dict[str, str]:
    """Compile exact OpenCode permission allow entries for one role's MCP tools."""
    return {tool: "allow" for tool in resolve_role_mcp_tools(config, role_key)}


def collect_role_mcp_environment(config: Config, role_key: str) -> tuple[str, ...]:
    """Return the configured passthrough environment variable names."""
    return config.model.mcp.environment_passthrough

