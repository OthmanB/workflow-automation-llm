# Step 21-Lite: Restore Research MCP Tools — Implementation Record

**Date:** 2026-08-13
**Operating model:** Trusted personal research environment

## Status

Implementation code-complete on schema-v2 project configuration. The operator's
installed Context7, Repomix, and Semble servers are inherited by default; three
manual model-tool smoke calls remain pending.

## Configuration

- Omitted `mcp` inherits the operator's OpenCode configuration directory and
  process environment while retaining dispatcher-owned session/data paths.
- An explicit `mcp` section takes precedence and provides
  `environment_passthrough` plus a server registry; an empty registry disables
  MCP.
- Local servers: `type: local`, non-empty argv `command` (no shell strings),
  optional `environment`, `enabled`. Remote servers: `type: remote`,
  http/https `url`, optional `headers`, `enabled`.
- Empty or omitted role `mcp_tools` receives the default catalog; a nonempty
  list narrows it. Every tool must be in the explicit dispatcher catalog:

  - Context7: `context7_resolve-library-id`, `context7_query-docs`
  - Repomix: pack codebase/remote, attach/read/grep packed output, file-system
    read directory/file (no `repomix_generate_skill`)
  - Semble: `semble_search`, `semble_find_related`

- Validation rejects duplicate role tools, unknown tools, tools whose catalog
  server is missing or disabled, empty local commands, invalid remote URLs,
  and duplicate/invalid passthrough names. No prefix-ownership or
  unused-server rules were added.

## Compilation

- `src/dispatcher/mcp.py` emits, per role, the OpenCode `mcp` object containing
  only that role's selected servers, exact per-tool permission allow entries,
  and the passthrough variable names.
- Permission compilation merges MCP allows after immutable reviewer/supervisor
  ceilings. Roles with MCP tools require a deny-default compiled policy so
  unlisted MCP methods stay denied. MCP tool names must match the
  `<server>_<method>` shape, so a name can never override a native OpenCode
  permission key such as `bash`, `edit`, `write`, or `read`.
- The executable `OPENCODE_CONFIG_CONTENT` is never redacted; `{env:NAME}`
  placeholders reach the child verbatim (resolved values stay process-local).
- Real-operation permission manifests expose each role's `mcp_tools`; the
  existing per-role digest hashes the complete generated child configuration
  (no manifest version bump).
- Preflight gained a static `mcp` check: passthrough variables present, local
  commands resolvable (absolute paths or PATH lookup), role configs
  serializable.

## Pinned OpenCode Compatibility

A deterministic stdio JSON-RPC fixture server (`tests/fixtures/mcp/`, methods
`echo` and `probe`) proves the emitted configuration shape, `initialize`/
`tools/list`/`tools/call` protocol exchange, and the `<server>_<method>`
naming convention. The reviewer independently confirmed that pinned OpenCode
1.18.11 loads the fixture through isolated `OPENCODE_CONFIG_CONTENT`. A full
model-driven invocation capture (calling `fixture_echo` while `fixture_probe`
is denied) remains gated behind the existing live environment gates.

Pinned OpenCode 1.18.11 was also launched with dispatcher-style isolated
HOME/XDG paths plus inherited `OPENCODE_CONFIG_DIR`. `opencode mcp list`
connected the operator's Context7, Repomix, Semble, GitHub, and Playwright
servers. The dispatcher inline permission map contained only the default
Context7, Repomix, and Semble method catalog.

## Manual Smoke — pending

Three model-driven calls remain to be recorded through a dispatcher workflow:

1. Context7 resolves and queries one library.
2. Repomix packs or searches this repository.
3. Semble searches this repository.

The public example inherits the operator's existing OpenCode servers. On the
verified operator machine, OpenCode reports Context7, Repomix, Semble, GitHub,
and Playwright connected; dispatcher permissions expose only the default
Context7, Repomix, and Semble catalog.

## Verification

- Full non-live suite: `533 passed, 10 deselected`.
- Ruff passed. MyPy passed for 34 source files.
- `pip check`, strict `pip-audit`, `git diff --check`, wheel/sdist build, and
  strict Twine checks passed.
- Schema artifacts regenerated in place; project configuration remains
  `schema_version: 2`.

## Limitations

- `repomix_file_system_read_file` accepts absolute paths, widening model read
  access beyond the worktree; accepted under the trusted-personal operating
  model.
- MCP failures are ordinary worker failures; they cannot fall back to GitHub,
  Playwright, or Bash and cannot change acceptance criteria.
- No live model environment variables were present; the ten live tests remain
  unexecuted.

## Next Action

Run the three manual smoke calls, then move to a real useful workflow.
