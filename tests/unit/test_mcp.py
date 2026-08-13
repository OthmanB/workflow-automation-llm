from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from helpers import config_values, create_fixture_project, write_config

from dispatcher.config import DEFAULT_INHERITED_MCP_TOOLS, ConfigError, load_config
from dispatcher.mcp import (
    collect_role_mcp_environment,
    compile_mcp_permissions,
    compile_role_mcp_servers,
    inherits_global_mcp_config,
    resolve_role_mcp_tools,
)

FIXTURE_SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp" / "fixture_mcp_server.py"


def _mcp_project(tmp_path: Path, *, tools: dict[str, list[str]] | None = None):
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["mcp"] = {
        "environment_passthrough": ["FIXTURE_MCP_TOKEN"],
        "servers": {
            "fixture": {
                "type": "local",
                "enabled": True,
                "command": [sys.executable, str(FIXTURE_SERVER)],
                "environment": {"MODE": "fixture"},
            },
            "context7": {
                "type": "remote",
                "enabled": True,
                "url": "https://example.invalid/mcp",
                "headers": {},
            },
        },
    }
    assigned = tools or {
        "supervisor": ["context7_resolve-library-id", "context7_query-docs"],
        "terra": ["context7_query-docs"],
        "reviewer": ["context7_query-docs"],
        "reviewer-two": [],
    }
    for entries in values["roles"].values():
        for role_key in entries:
            entries[role_key]["mcp_tools"] = assigned.get(role_key, [])
    return project, write_config(project, values)


def test_fixture_mcp_server_speaks_json_rpc() -> None:
    process = subprocess.Popen(
        [sys.executable, str(FIXTURE_SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None

    def call(method: str, params: dict[str, Any], identifier: int) -> dict[str, Any]:
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        return json.loads(process.stdout.readline())

    initialize = call("initialize", {}, 1)
    assert initialize["result"]["serverInfo"]["name"] == "fixture-mcp"
    listing = call("tools/list", {}, 2)
    assert [tool["name"] for tool in listing["result"]["tools"]] == ["echo", "probe"]
    echoed = call("tools/call", {"name": "echo", "arguments": {"text": "hello"}}, 3)
    assert echoed["result"]["content"][0]["text"] == "echo:hello"
    process.terminate()
    process.wait(timeout=10)


def test_role_mcp_tools_compile_to_exact_servers_and_permissions(tmp_path: Path) -> None:
    project, config = _mcp_project(tmp_path)

    assert resolve_role_mcp_tools(config, "terra") == ("context7_query-docs",)
    servers = compile_role_mcp_servers(config, "terra")
    assert servers == {
        "context7": {
            "type": "remote",
            "url": "https://example.invalid/mcp",
            "enabled": True,
        }
    }
    assert compile_mcp_permissions(config, "terra") == {"context7_query-docs": "allow"}
    assert collect_role_mcp_environment(config, "terra") == ("FIXTURE_MCP_TOKEN",)
    assert compile_role_mcp_servers(config, "reviewer-two") == {}
    assert compile_mcp_permissions(config, "reviewer-two") == {}


def test_changing_mcp_tools_changes_config_digest(tmp_path: Path) -> None:
    project, config = _mcp_project(tmp_path)
    original = config.config_digest

    values = config_values(project)
    values["roles"]["executors"]["terra"]["mcp_tools"] = [
        "context7_query-docs",
        "context7_resolve-library-id",
    ]
    changed = write_config(project, values)

    assert changed.config_digest != original


def test_changing_mcp_servers_changes_config_digest(tmp_path: Path) -> None:
    project, config = _mcp_project(tmp_path)
    original = config.config_digest

    values = config_values(project)
    values["mcp"]["servers"]["context7"]["url"] = "https://other.invalid/mcp"
    changed = write_config(project, values)

    assert changed.config_digest != original


def _invalid_values(tmp_path: Path) -> dict[str, Any]:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["mcp"] = {
        "environment_passthrough": [],
        "servers": {
            "fixture": {
                "type": "local",
                "enabled": True,
                "command": [sys.executable, str(FIXTURE_SERVER)],
                "environment": {},
            },
            "context7": {
                "type": "remote",
                "enabled": True,
                "url": "https://example.invalid/mcp",
                "headers": {},
            },
        },
    }
    for entries in values["roles"].values():
        for role_key in entries:
            entries[role_key]["mcp_tools"] = ["context7_query-docs"]
    return project, values


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda values: values["roles"]["executors"]["terra"].update(
                {"mcp_tools": ["context7_query-docs", "context7_query-docs"]}
            ),
            "must not contain duplicate tools",
        ),
        (
            lambda values: values["roles"]["executors"]["terra"].update(
                {"mcp_tools": ["context7_typo"]}
            ),
            "not in the configured MCP tool catalog",
        ),
        (
            lambda values: values["roles"]["executors"]["terra"].update(
                {"mcp_tools": ["repomix_pack_codebase"]}
            ),
            "does not reference a configured MCP server",
        ),
        (
            lambda values: values["roles"]["executors"]["terra"].update(
                {"mcp_tools": ["bash"]}
            ),
            "String should match pattern",
        ),
        (
            lambda values: values["mcp"]["servers"]["context7"].update({"enabled": False}),
            "references disabled MCP server",
        ),
        (
            lambda values: values["mcp"]["servers"]["context7"].update({"url": "not-a-url"}),
            "must be an http or https URL",
        ),
        (
            lambda values: values["mcp"]["servers"]["fixture"].update({"command": []}),
            "at least 1 item",
        ),
        (
            lambda values: values["mcp"].update(
                {"environment_passthrough": ["FIXTURE_MCP_TOKEN", "FIXTURE_MCP_TOKEN"]}
            ),
            "must be unique",
        ),
        (
            lambda values: values["mcp"].update({"environment_passthrough": ["BAD-NAME"]}),
            "valid environment variable names",
        ),
    ],
)
def test_invalid_mcp_configuration_rejects(tmp_path: Path, mutate, message: str) -> None:
    project, values = _invalid_values(tmp_path)
    mutate(values)
    import yaml

    project.config_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(project.config_path)


def test_passthrough_names_are_collected_for_every_role(tmp_path: Path) -> None:
    project, config = _mcp_project(tmp_path)
    assert collect_role_mcp_environment(config, "terra") == ("FIXTURE_MCP_TOKEN",)
    assert collect_role_mcp_environment(config, "reviewer-two") == ("FIXTURE_MCP_TOKEN",)


def test_omitted_mcp_inherits_global_servers_and_default_research_tools(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values.pop("mcp")
    for entries in values["roles"].values():
        for role in entries.values():
            role.pop("mcp_tools")
    config = write_config(project, values)

    assert inherits_global_mcp_config(config) is True
    assert compile_role_mcp_servers(config, "terra") == {}
    assert resolve_role_mcp_tools(config, "terra") == DEFAULT_INHERITED_MCP_TOOLS
    assert set(compile_mcp_permissions(config, "terra")) == set(
        resolve_role_mcp_tools(config, "terra")
    )


def test_explicit_mcp_registry_takes_precedence_over_global_config(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    config = write_config(project, config_values(project))

    assert inherits_global_mcp_config(config) is False
    assert resolve_role_mcp_tools(config, "terra") == ()
    assert compile_role_mcp_servers(config, "terra") == {}
