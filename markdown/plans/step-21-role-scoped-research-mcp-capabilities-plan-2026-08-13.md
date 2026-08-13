# Step 21-Lite: Restore Research MCP Tools

**Date:** 2026-08-13  
**Status:** Proposed implementation plan  
**Prerequisites:** Step 20 implementation complete  
**Operating model:** Trusted personal research environment

## Purpose

Make Context7, Repomix, and Semble available to dispatcher supervisors,
executors, and reviewers so the models can look up current library
documentation and understand codebases while doing real work.

Dispatcher workers currently use isolated HOME/XDG directories and an inline
OpenCode configuration that contains permissions but no MCP servers. Prompts
also advertise an empty MCP list. This makes the operator's existing global MCP
configuration unavailable during normal dispatcher execution.

Step 21-lite restores that utility with the smallest maintainable change:

- keep project configuration `schema_version: 2`;
- add MCP server definitions to the existing project config;
- add an exact MCP tool list to each role;
- compile both into the child OpenCode configuration;
- pass configured MCP environment variables to the child; and
- verify the pinned OpenCode version with one deterministic fixture server.

This step must not introduce a new workflow state, approval protocol, manifest
version, recovery path, sandbox, proxy, or migration subsystem.

## Direction

The project is a tool for one operator working on experimental and research
code. Development should now prioritize completing useful workflows over
expanding infrastructure.

Existing schema-v2 plans, typed worker contracts, writable-path validation,
dispatcher-owned checks, and dispatcher-owned commits remain in place because
they already exist and provide useful behavior. Step 21-lite does not revisit
or extend their machinery.

New capability work should follow these rules:

1. Prefer direct configuration and a short execution path.
2. Reuse existing models and persistence rather than versioning them again.
3. Test the behavior that could prevent real use; avoid broad combinatorial
   suites and prompt-string snapshots.
4. Stop after the tools work and run a real workflow before planning another
   infrastructure step.

## Scope

### Included

- Context7:
  - `context7_resolve-library-id`
  - `context7_query-docs`
- Repomix:
  - `repomix_pack_codebase`
  - `repomix_pack_remote_repository`
  - `repomix_attach_packed_output`
  - `repomix_read_repomix_output`
  - `repomix_grep_repomix_output`
  - `repomix_file_system_read_directory`
  - `repomix_file_system_read_file`
- Semble:
  - `semble_search`
  - `semble_find_related`
- supervisor, executor, and reviewer role assignments;
- local and remote OpenCode MCP server definitions;
- explicit environment-variable passthrough;
- one pinned-OpenCode compatibility fixture;
- concise documentation and a manual real-tool smoke record.

### Excluded

- GitHub and Playwright;
- `repomix_generate_skill`;
- MCP proxies or argument filters;
- method-level audit records;
- new approval or capability digests;
- project config schema v3;
- normalized-plan or workflow-state schema changes;
- MCP-specific recovery logic;
- a dedicated live-test gate or matrix;
- hostile-repository or hostile-MCP-server guarantees.

MCP results remain model context. They do not replace dispatcher-owned
verification, evidence calculation, review decisions, or Git operations.

## Configuration

Extend the current strict project configuration model in place while retaining:

```yaml
schema_version: 2
```

Add one required top-level `mcp` section and one required `mcp_tools` list to
each role. Existing project configs and test fixtures are updated directly; no
runtime compatibility inference is needed.

Conceptual shape:

```yaml
schema_version: 2

mcp:
  environment_passthrough: []
  servers:
    context7:
      type: remote
      url: https://mcp.context7.com/mcp
      enabled: true
      headers: {}
    repomix:
      type: local
      command: [<configured-repomix-command>]
      enabled: true
      environment: {}
    semble:
      type: local
      command: [<configured-semble-command>]
      enabled: true
      environment: {}

roles:
  supervisor:
    supervisor:
      model: provider/model
      variant: standard
      display: Supervisor
      permission_policy: supervisor
      mcp_tools:
        - context7_resolve-library-id
        - context7_query-docs
        - repomix_pack_codebase
        - repomix_pack_remote_repository
        - repomix_attach_packed_output
        - repomix_read_repomix_output
        - repomix_grep_repomix_output
        - repomix_file_system_read_directory
        - repomix_file_system_read_file
        - semble_search
        - semble_find_related
```

Use the actual working local commands in the operator's project YAML. Test
fixtures use a deterministic fixture MCP server and never contact live services.

### Minimal Validation

Validate only what is needed for clear failures:

- local servers have a nonempty argv list;
- remote servers have a nonempty HTTP(S) URL;
- server and role tool lists contain no duplicates;
- every role tool is explicitly listed in a configured tool catalog;
- `environment_passthrough` contains valid, unique environment names; and
- missing requested environment variables fail before OpenCode launch.

Do not add prefix-ownership rules, unused-server errors, data-scope policies, or
an MCP-specific policy language.

## OpenCode Compatibility Check

The first implementation task is a small compatibility probe against the pinned
OpenCode 1.18.11 version.

Create a deterministic local fixture MCP server with two methods and prove:

1. it loads from `OPENCODE_CONFIG_CONTENT` under the existing isolated child
   HOME/XDG environment;
2. the expected OpenCode tool names are exposed;
3. one configured method can be called; and
4. an unlisted method is denied by the generated permission map.

Capture one sanitized fixture and a short provenance note under the existing
`tests/fixtures/opencode/1.18.11` directory.

If pinned OpenCode uses a different MCP configuration or tool naming shape,
adjust the compiler to the observed behavior. Do not build a compatibility
abstraction for other OpenCode versions.

## Implementation

### 1. Configuration models

Update `src/dispatcher/config.py` with small local/remote MCP server models, a
top-level MCP definition, and `RoleDefinition.mcp_tools`.

Keep `ProjectConfigModel.schema_version` at 2 and regenerate the existing
project-config schema artifact. No migration command or alternate loader is
added.

### 2. OpenCode config compilation

Add a focused module such as `src/dispatcher/mcp.py` with pure helpers to:

- select the configured servers needed by a role;
- emit the OpenCode `mcp` configuration;
- add exact allow entries for the role's configured MCP tools; and
- return the environment-variable names to pass to the child.

MCP methods remain separate from repository semantic actions. Do not add them
to `PERMISSION_ACTIONS` and do not change executor writable paths, reviewer
native write ceilings, or dispatcher Git authority.

Extend the existing generated OpenCode configuration rather than creating a
second permission system. Unlisted tools retain the existing default deny.

### 3. Child environment

Update `build_child_environment()` to copy only the names in
`mcp.environment_passthrough` from the parent environment in addition to its
existing minimal environment.

MCP server config may use OpenCode `{env:NAME}` references. Environment values
must not be inserted into prompts or generated config text.

No secret manager, per-server credential model, or durable MCP credential record
is needed.

### 4. Worker prompts

Replace hard-coded `mcp: []` and `MCP tools: none` values with the role's actual
tool list:

- supervisor bootstrap lists available research tools;
- executor prompt includes the list while retaining its existing write/check/Git
  instructions; and
- reviewer `observation_tools.mcp` lists the configured methods while retaining
  its native read-only behavior.

Test parsed prompt fields, not complete rendered prompt strings.

### 5. Existing approval and state behavior

Do not bump `RolePermissionManifest` or alter SQLite schemas.

The existing project config digest already changes when MCP configuration
changes. The existing role permission digest should hash the complete generated
OpenCode child config after MCP is added. That is sufficient for the current
real-operation approval flow without a new manifest protocol.

No MCP data is added to run state beyond the generated child config and prompt
already stored for a dispatch.

### 6. Preflight

Keep preflight small:

- confirm each configured local MCP executable can be resolved;
- confirm every requested passthrough environment variable is present; and
- confirm role MCP config can be compiled.

Do not add automated live MCP calls to preflight. Real calls are checked once
manually after implementation.

## Expected Files

- `src/dispatcher/config.py`
- `src/dispatcher/mcp.py` (new)
- `src/dispatcher/permissions.py`
- `src/dispatcher/sessions.py`
- `src/dispatcher/execution.py`
- `src/dispatcher/sequential.py`
- `src/dispatcher/preflight.py`
- `src/dispatcher/operation.py` only if needed to hash the complete existing
  generated child config
- `config/projects/example.yaml`
- `tests/helpers.py`
- focused unit and compatibility tests
- existing generated project-config schema
- `docs/config-schema.md`
- `docs/operations.md`
- `docs/protocol.md`
- `docs/compatibility.md`

## Focused Tests

Keep the new suite near fifteen behavioral tests rather than building another
matrix.

Required coverage:

1. local and remote MCP config parse successfully;
2. malformed local command and remote URL reject;
3. duplicate and unknown role tools reject;
4. role config contains only its selected servers and exact tool allows;
5. an unlisted fixture MCP method is denied;
6. required environment variables reach the child;
7. unrelated environment variables remain omitted;
8. a missing passthrough variable fails before launch;
9. supervisor prompt contains its configured tool list;
10. executor prompt contains its configured tool list;
11. reviewer prompt contains its configured tool list;
12. empty role tool lists remain supported;
13. changing MCP config changes the existing config and role permission digests;
14. pinned OpenCode loads and calls the deterministic fixture server; and
15. existing sequential executor/reviewer integration remains green.

Do not add prompt snapshot tests, MCP recovery fault injection, state migration
tests, or a role/server Cartesian-product suite.

## Manual Smoke

After focused and full non-live tests pass, run exactly one real call through
each configured service in the personal environment:

1. Context7 resolves and queries one library;
2. Repomix packs or searches this repository; and
3. Semble searches this repository.

Record the commands/tool names and concise successful outputs in the Step 21
report. Do not build a permanent live-test harness before doing these calls.

## Verification

```sh
.venv/bin/pytest -q <focused Step 21 tests>
.venv/bin/pytest -q -m "not live_opencode"
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/pip check
git diff --check
```

Package build checks should be rerun if dependencies or packaged files change.

## Completion Criteria

Step 21-lite is complete when:

1. Context7, Repomix, and Semble are available to configured dispatcher roles;
2. project config remains schema version 2;
3. no manifest, approval, state, recovery, or normalized-plan version was added;
4. generated child config and prompts agree on each role's tool list;
5. the focused tests and existing non-live suite pass;
6. one manual call to each of the three services succeeds; and
7. the next action is a real useful workflow, not another infrastructure step.

## Evidence

Write a concise implementation record to:

```text
markdown/reports/step-21-lite-research-mcp-tools-report-2026-08-13.md
```

The report should contain the final configured tool lists, the pinned OpenCode
fixture result, test totals, the three manual smoke results, and any practical
limitation encountered. It should not restate the full plan.
