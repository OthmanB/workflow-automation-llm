"""Shared test environment registration."""

from __future__ import annotations

from dispatcher import config as config_mod

# Register the deterministic fixture MCP server methods in the tool catalog so
# test configurations can assign them to roles without contacting live services.
config_mod.MCP_TOOL_CATALOG.setdefault("fixture_echo", "fixture")
config_mod.MCP_TOOL_CATALOG.setdefault("fixture_probe", "fixture")
