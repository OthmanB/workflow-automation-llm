# Dispatcher Verification And Isolation Decision

**Date:** 2026-08-12  
**Status:** Accepted for implementation  
**Supersedes:** model-owned acceptance verification and model-owned commits

## Decision

The dispatcher, not an LLM worker, owns acceptance-check execution, check
results, and repository commits.

OpenCode permissions remain defense-in-depth UX controls. They are not used as
the operating-system security boundary.

## Trust Domains

### OpenCode parent

- may connect to the configured model provider;
- receives isolated HOME/XDG/auth state;
- may use native repository `read`, `glob`, and path-scoped edit/write tools
  according to role;
- may not run verification commands, Git commits, pushes, browsers, or MCP
  mutation methods;
- returns a typed proposal/result but does not author authoritative check
  statuses or repository coordinates.

### Dispatcher verification process

- receives an argv array from the normalized plan, never model text;
- uses `shell=False`;
- runs in a disposable copy of the exact inspected repository tree;
- has bounded time, output, process group, and environment;
- has outbound network denied by the selected isolation backend;
- produces the authoritative verification status and transcript hash.

### Dispatcher Git process

- stages only registered writable roots and expected evidence paths;
- refuses unexpected changes;
- commits only after all required checks pass;
- uses argv execution with hooks disabled and a controlled environment;
- records the exact resulting revision in authoritative state.

### Reviewer and supervisor

- remain read-only against immutable repository state;
- consume dispatcher-generated verification evidence;
- have no test-runner, interpreter, commit, push, browser, or MCP mutation
  authority;
- may use exact read-only diagnostics and explicitly approved read-only MCP
  methods when the project external-access policy permits them.

## Plan Contract

Normalized plans move to schema version 2. Every acceptance criterion contains
one required structured check:

```json
{
  "criterion_id": "verify-real-output",
  "description": "Fixed output verification",
  "check": {
    "argv": ["python", "-m", "pytest", "-q", "test_real_output.py"],
    "working_directory": "repository",
    "timeout_seconds": 120,
    "max_output_bytes": 65536,
    "expected_exit_codes": [0],
    "network_policy": "deny"
  }
}
```

Free-form shell strings, implicit working directories, inherited network, and
model-supplied command substitutions are invalid.

## Result Authority

Worker result contracts continue to report a verification list for
transparency, but those values are non-authoritative. The dispatcher replaces
them only in a separately stored authoritative verification record; it never
repairs the model response itself.

State advancement uses only dispatcher check records. Model verification must
use exact criterion IDs, but disagreement with dispatcher results fails the
worker boundary.

Review targets bind dispatcher verification transcript digests in addition to
repository/evidence hashes.

## Isolation Backends

### macOS local Tier 2

`darwin_seatbelt_v1` is the supported local macOS backend for dispatcher-owned
verification checks:

- check-process network operations denied;
- writes denied outside the disposable verification workspace/root;
- isolated HOME and TMPDIR; and
- bounded subprocess and output handling.

It is suitable for the current macOS-confined Tier 2 implementation workflow.
It does not claim to sandbox the provider-connected OpenCode parent process or
replace repository/worktree validation around executor writes.

### Future Linux

`linux_bwrap_v1` remains an optional future backend:

- unshared network namespace;
- disposable writable workspace copy;
- read-only runtime/system mounts;
- no host HOME, project state, credentials, or unrelated repositories;
- explicit executable/runtime mounts;
- process-group termination and bounded output.

Real-operation preflight and execute fail closed when a selected backend is
unavailable.

### Mock

An injected fake backend is allowed only in non-live tests. No production
configuration can select it.

## MCP Policy

MCP access is exact-method allowlisting, never whole-server allowlisting.

- Context7: exact documentation read/query methods may be allowed only when
  external documentation access is approved.
- GitHub: exact read/list/search methods, repository-scoped; all issue/PR/file/
  branch/merge write methods denied.
- Playwright: denied for no-external-service operation because navigation and
  interaction are not intrinsically read-only.
- Repomix: exact pack/read/grep methods may be allowed; skill generation and
  file-writing methods denied.
- Semble: exact search/related methods may be allowed subject to code-data
  egress approval.

The dispatcher does not copy the operator's personal MCP configuration into
worker HOME/XDG state.

## Migration

There is no schema-v1 compatibility path for real operations. No real operation
has shipped. Public fixtures, normalized plans, baseline hashes, approvals, and
schemas migrate atomically to version 2.

## Release Gate

The first real T2.2a operation remains NO-GO until:

1. schema-v2 structured checks are authoritative;
2. model verification/commit authority is removed;
3. a production isolation backend is configured and proven available;
4. outbound requests from check/tool processes are denied in machine evidence;
5. paths outside registered roots are inaccessible in machine evidence; and
6. the full disposable suite passes on that backend from a committed revision.
