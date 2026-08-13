# Installation and Compatibility

## Supported Environment

- Python `>=3.11`.
- OpenCode event compatibility is pinned to `1.18.11`.
- The pinned decoder contract is represented by sanitized fixtures under
  `tests/fixtures/opencode/1.18.11` and tested by
  `tests/contract/test_opencode_compatibility.py`.
- OpenCode JSONL events reject duplicate object keys before any session,
  text, usage, cost, or error field is interpreted.
- Normalized execution plans use schema version 2. Schema-v1 normalized plans
  are intentionally rejected because no real operation shipped before the
  dispatcher-owned structured verification migration.
- macOS local checks require `/usr/bin/sandbox-exec` for `darwin_seatbelt_v1`.
  The dispatcher fails closed if the configured backend is unavailable.
- Step 21 adds a deterministic local MCP fixture server under
  `tests/fixtures/mcp/` that speaks plain stdio JSON-RPC. Dispatcher
  compilation emits pinned OpenCode inline MCP server definitions and exact
  per-tool permission keys (`<server>_<method>` after name sanitization).
  Live tool-name capture against the real pinned binary remains gated behind
  the same live environment gates as other real-operation proofs; the project
  supports only the observed 1.18.11 inline MCP configuration shape and will
  not add a multi-version MCP adapter.

Install the development environment with:

```bash
python -m pip install -e ".[dev]"
```

Build and validate a distribution with:

```bash
python -m build
twine check --strict dist/*
```

## Execution Boundary

The public configuration requires `execution.mode: mock_workflow_test`. Real OpenCode
dispatch and repository mutation are disabled. Fake OpenCode integration is
the supported execution proof for normal development and CI.

An optional read-only compatibility smoke exists for a separately approved
environment. It is skipped unless both environment variables are set:

```bash
DISPATCHER_LIVE_OPENCODE=1 DISPATCHER_LIVE_MODEL=<provider/model> \
  PYTHONPATH=src python -m pytest -m live_opencode
```

The smoke prompt only requests `LIVE_SMOKE_OK`, disables auto approval, and
uses a deny-by-default permission payload. It must not be used as authorization
for repository-mutating work.

Schema v2 also defines `execution.mode: real_operation`, but that mode is
private and can only be used through the guarded `dispatcher execute` command.

## Dependency and Secret Gates

CI runs `pip-audit --strict .`, strict Twine metadata validation, a clean-wheel
install with `pip check`, and Gitleaks. GitHub Advanced Security is not enabled
for this repository, so the CI Gitleaks job is the repository-level secret
scanner.
