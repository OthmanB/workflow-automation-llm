# Dispatcher Phase 2 Execution Report

**Execution date:** 2026-08-09
**Plan:**
[`markdown/plans/dispatcher-remediation-plan-2026-08-09.md`](../plans/dispatcher-remediation-plan-2026-08-09.md)
**Scope:** DISP-200 through DISP-204 boundary controls.

## Status

The pinned OpenCode adapter, session identity controls, process lifecycle, policy
compiler, child environment, and private local artifacts are implemented and
covered by deterministic tests. The CLI still rejects every real `dispatcher
run` before configuration loading unless `--mock` is supplied. This work does
not enable model-backed execution, repository mutation, deployment, or hosted
CI.

## Implemented Controls

- The adapter supports OpenCode `1.18.11` only. It checks the runtime binary
  version, decodes the supported JSONL event types incrementally, captures
  nested text/tokens/cost, preserves bounded safe metadata, and fails closed on
  malformed, unknown, incomplete, structured-error, or mixed-session streams.
- OpenCode uses streaming `Popen` I/O in a dedicated process group. Prompts are
  sent through stdin without the unsupported positional `-`; timeouts terminate
  the process tree after a required YAML grace period. Sanitized stdout and
  stderr go to private event logs while memory remains bounded by required YAML
  `max_output_bytes`.
- Session listing is structured JSON with exact ID comparisons. Resume and fork
  require dispatcher registry ownership and matching project directory; missing
  or stale references fail rather than silently creating a session. Registry
  entries retain working directory, OpenCode version, and fork parent ID.
- Permission policy is now an explicit layered configuration: global, project,
  repository, role class, concrete role, and structured dispatch authorization.
  Semantic actions compile to narrow OpenCode rules and the generated payload,
  including the global default, is supplied only through
  `OPENCODE_CONFIG_CONTENT`. `--auto` remains disabled whenever any rule asks.
- Child processes receive a minimal allowlisted environment and private
  HOME/XDG directories; no parent credentials or user OpenCode state are
  inherited. State, sessions, transcripts, audits, policy snapshots, and event
  logs use owner-only modes. Persisted/logged values redact common credentials,
  bearer headers, and credential-bearing URLs.

## Verification

```text
ruff check src tests
All checks passed!

mypy src
Success: no issues found in 20 source files

pytest
112 passed

python -m build
Successfully built dispatcher-0.1.0.tar.gz and dispatcher-0.1.0-py3-none-any.whl
```

The test suite includes the complete sanitized OpenCode `1.18.11` fixture
corpus, malformed/error/version-drift tests, a fake CLI process tree with a
grandchild timeout, high-volume output, exact-session/fork validation, compiled
allow/ask/deny policies, child-environment isolation, and private/redacted
runtime artifacts.

## Remaining Boundary

The plan item for a short-lived credential broker or scoped credential source is
not implemented because real execution remains deliberately blocked and no
credential is passed to child processes. It must be resolved before any future
change permits real OpenCode dispatch. Hosted GitHub Actions also remains
unverified because this workspace is not a Git repository.
