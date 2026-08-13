# Step 21-Lite: Restore Research MCP Tools — Implementation Record

**Date:** 2026-08-13
**Operating model:** Trusted personal research environment

## Status

Implementation code-complete on schema-v2 project configuration; the three
manual real-tool smoke calls remain pending (see Manual Smoke below).

## Configuration

- Required top-level `mcp` section: `environment_passthrough` (environment
  variable names copied into the isolated child environment; a missing name
  fails before OpenCode launch) and a `servers` registry.
- Local servers: `type: local`, non-empty argv `command` (no shell strings),
  optional `environment`, `enabled`. Remote servers: `type: remote`,
  http/https `url`, optional `headers`, `enabled`.
- Required `mcp_tools` list on every role; every tool must be in the explicit
  dispatcher catalog:

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

## Manual Smoke — pending

Three real calls remain to be recorded by the operator:

1. Context7 resolves and queries one library.
2. Repomix packs or searches this repository.
3. Semble searches this repository.

The public example enables Context7 (public URL); Repomix and Semble are
disabled placeholders pending the operator's installed commands in the real
project YAML. No credentials or server locations are embedded in the
repository.

## Verification

- Full non-live suite: `529 passed, 10 deselected`.
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
