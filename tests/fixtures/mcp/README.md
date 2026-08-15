# Deterministic MCP Fixture Server

`fixture_mcp_server.py` is a minimal stdio JSON-RPC MCP server used by the
Step 21-lite compatibility and compiler tests. It exposes exactly two methods:
`echo` (returns the provided text) and `probe` (returns a fixed marker).

## Capture provenance

- The pinned OpenCode `1.18.18` compatibility fixture preserves the expected
  MCP tool naming convention: tools are exposed as
  `<server>_<method>` after name sanitization (for example a server named
  `fixture` exposes `fixture_echo` and `fixture_probe`).
- Dispatcher code emits inline OpenCode configuration through
  `OPENCODE_CONFIG_CONTENT` with a top-level `mcp` object (local servers carry
  `type: local`, an argv `command`, optional `environment`, and `enabled`)
  and exact per-tool permission allow keys. `{env:NAME}` placeholders are
  passed verbatim and are never redacted before launch.
- A full live tool-event capture through a real model backend remains gated
  behind the same live environment gates as the other real-operation proofs;
  no credentials or server locations are embedded in the repository.

The unit test `tests/unit/test_mcp.py::test_fixture_mcp_server_speaks_json_rpc`
exercises the fixture's initialize/tools/list/tools/call exchange directly.
