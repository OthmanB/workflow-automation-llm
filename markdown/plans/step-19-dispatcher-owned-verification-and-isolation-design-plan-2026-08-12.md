# Step 19: Dispatcher-Owned Verification and Isolation

**Date:** 2026-08-12  
**Status:** Architecture accepted; Phase A implemented, release gate remains blocked  
**Source:**
`markdown/reports/misc-fixes/step-16-permission-boundary-security-analysis-2026-08-12.md`

## Goal

Replace model-owned free-form verification commands with dispatcher-owned,
structured checks and define an execution isolation boundary sufficient to
make filesystem and network claims technically accurate.

This is the durable architecture. Steps 17-18 contain the observed reviewer
exploit but do not make OpenCode's UX permission system an OS security
boundary.

## Mandatory Design Gate

No implementation begins until an owner-approved architecture decision covers:

1. normalized-plan schema versioning;
2. structured check representation;
3. which process executes checks;
4. model tool permissions after migration;
5. filesystem isolation backend;
6. network/egress isolation backend;
7. provider connectivity versus tool-process connectivity;
8. supported development and production platforms;
9. evidence/transcript retention;
10. migration with no compatibility fallback.

Use a fresh Claude Sonnet 5 design session, followed by a fresh GPT-5.6 Sol
implementation only after owner approval.

## Proposed Structured Check Contract

Each acceptance criterion should bind a machine-readable check, not prose:

```json
{
  "criterion_id": "verify-real-output",
  "description": "Fixed disposable output check",
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

Requirements:

- argv array only; never `shell=True` or free-form shell text;
- relative, registered working-directory identity;
- bounded timeout/output;
- deterministic controlled environment;
- expected exit-code set;
- duplicate-free criterion/check IDs;
- network policy explicit and mandatory;
- no command supplied or rewritten by the model.

## Dispatcher Execution Flow

1. Executor may inspect and edit only authorized writable roots.
2. Model returns result/evidence metadata; it does not run acceptance checks.
3. Dispatcher inspects repository boundaries.
4. Dispatcher executes structured checks under the selected isolation backend.
5. Dispatcher records command argv, exit code, bounded redacted output,
   timestamp, and transcript SHA-256.
6. Dispatcher constructs authoritative `VerificationResult` values.
7. Model-supplied verification is removed from the trust boundary or required
   only as a non-authoritative self-report.
8. Dispatcher performs the commit itself after successful checks, or adopts a
   separately approved commit design; the model should not require Git Bash
   access.
9. Reviewer receives immutable dispatcher verification evidence and remains
   read-only with no Bash.

## Isolation Problem

OpenCode's parent process requires provider connectivity, while model-invoked
tool subprocesses must not have arbitrary network access. A process-wide
network deny on OpenCode would also block the provider.

The design must separate these authorities rather than relying on permission
prompts:

- parent OpenCode transport: provider allow-listed connectivity only;
- dispatcher-owned check subprocess: outbound network denied;
- model tool surface: no Bash/web fetch after dispatcher-owned checks exist;
- filesystem: repository writable roots and isolated state only; host home,
  credentials, unrelated repositories, and private state inaccessible.

## Backend Decision

The current macOS workstation has no supported per-process network namespace.
Deprecated `sandbox-exec` is not an acceptable release boundary by itself.

Choose one supported production path:

- dedicated Linux runner using bubblewrap/namespaces plus egress filtering;
- dedicated VM with filesystem mounts and network allow-list;
- another independently reviewed sandbox with equivalent guarantees.

Docker is not required, but an unsandboxed host process cannot satisfy the
technical no-network/no-external-service claim.

If the operator chooses to remain on unsandboxed macOS, the release record
must explicitly downgrade those properties from enforced guarantees to
operator-requested behavior; a strict GO review should not treat them as
proven.

## Implementation Phases

### Phase A: Schema and executor

- Version the normalized plan/check schema.
- Implement strict structured-check parsing.
- Implement dispatcher check runner with shell disabled, bounds, redaction,
  transcript hashing, and network-backend interface.
- Add fault/property tests.

### Phase B: Trust-boundary migration

- Remove model-owned verification authority.
- Remove reviewer Bash.
- Move commit operation into dispatcher-owned controlled execution.
- Bind check definitions/results into forwarding and review targets.
- Update completion obligations and evidence schemas.

### Phase C: Isolation backend and proof

- Implement selected filesystem/network backend.
- Prove provider connectivity remains functional while check/tool network is
  denied.
- Prove paths outside registered roots are inaccessible.
- Rerun all disposable scenarios plus adversarial network/filesystem tests.

## Required Tests

- strict argv schema, no shell strings;
- path traversal and unregistered cwd rejection;
- timeout/process-group cleanup;
- output bounds and redaction;
- exit-code enforcement;
- transcript hash/size integrity;
- network denial from check process;
- filesystem denial outside mounted roots;
- reviewer has no Bash and still reviews authoritative verification;
- executor cannot forge dispatcher verification;
- checks run against the exact inspected revision;
- commit occurs only after passing checks;
- crash recovery between check, commit, and forwarding;
- full non-live/property/fault-injection suites;
- complete live disposable matrix with an attempted outbound request proving
  denial.

## Release Gate

Step 19 is complete only when machine evidence proves:

- model tool processes cannot make arbitrary outbound connections;
- check processes cannot make outbound connections;
- unrelated filesystem paths cannot be read or written;
- provider transport still works through its explicit allow-list;
- all accepted verification results are dispatcher-generated;
- repository commits are produced only by controlled dispatcher operations.

## Evidence

Design decision:

`markdown/decisions/dispatcher-verification-isolation-decision-2026-08-12.md`

Implementation report:

`markdown/reports/dispatcher-owned-verification-and-isolation-report-2026-08-12.md`

Do not combine design approval and implementation in one model session.
