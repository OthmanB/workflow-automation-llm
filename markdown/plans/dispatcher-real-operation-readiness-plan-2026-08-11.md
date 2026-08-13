# Dispatcher Real Operation Readiness Plan

**Date:** 2026-08-11
**Status:** proposed implementation plan; not authorization to run real work

## Purpose

This plan closes the remaining gates before the dispatcher may run approved
project work through real OpenCode sessions.

The operating levels use these explicit names:

1. **Mock workflow test:** fake OpenCode responses; no real model call and no
   repository change.
2. **Live smoke test:** one real, harmless OpenCode call in an empty temporary
   directory; no repository change.
3. **Real operation:** real OpenCode sessions may edit, review, and commit files
   under an approved project plan.

The current T2 synthetic mock result is proof that the workflow can continue
from an approved baseline. It is not proof that T2.2a was implemented. A future
real-operation run must start from a fresh baseline-backed run in which T2.2a is
Pending.

## Required Order

1. Finalize the T2 repository-ownership map.
2. Add cancellation and bounded stall recovery.
3. Pass the real, harmless OpenCode smoke test.
4. Add an explicit real-operation mode and command.
5. Pass a real-operation test in disposable repositories.
6. Run one operator-observed, low-risk real T2.2a step and stop.
7. Review the evidence and explicitly approve broader real operation.

No later item may bypass an earlier item.

## 1. Finalize T2 Repository Ownership

### Current state

- The private baseline is approved locally.
- T2.1a through T2.1f have observed evidence and review proof.
- T2.2a onward are Pending.
- A permanent local detector T2 worktree exists at the reviewed branch and
  commit.
- Some Markdown table rows do not explicitly name a repository. The local draft
  currently assigns T2.7, T2.8a, and T2.8b to `agent-threat-detection`.

### Work

- Add a generic importer input for explicit repository ownership when a source
  table omits it. The project-specific map remains local and ignored by Git.
- Require one repository assignment for every step; never guess from prose.
- Compare the ownership map with the local normalized sidecar and reject any
  disagreement.
- Re-run source-hash, repository, evidence, review, dependency, permission, and
  retry validation.
- Record an owner approval for the completed ownership map.

### Completion proof

- The reference importer validates all 19 T2 rows.
- Every step has exactly one registered repository.
- No private path, repository detail, or evidence enters the public repository.
- T2.1a–T2.1f remain Accepted and T2.2a onward remain Pending.

## 2. Cancellation And Bounded Stall Recovery

### Plain-language stall definition

A stall means the child session did not produce a usable typed result because
it timed out, was cancelled, lost its connection, was interrupted by a temporary
provider problem, or returned incomplete output.

A normal implementation failure is not a stall. A provider account/quota stop
is not an internal project budget.

### Structured failure categories

Preserve OpenCode's structured error name and safe fields rather than reducing
everything to one message. Map exact pinned OpenCode errors into these groups:

| Group | Examples | Automatic action |
|---|---|---|
| Temporary interruption | timeout, connection loss, temporary rate limit | cancel, wait, bounded continuation retry |
| Context exhausted | context-overflow/output-length error | start a fresh bounded continuation session |
| Provider account stop | exhausted quota, billing/account restriction | stop and ask the operator; no automatic retry |
| Authentication or permission failure | invalid provider credential, denied access | stop and ask the operator |
| User cancellation | dispatcher/operator cancellation | record cancellation; retry only if explicitly allowed |
| Unknown failure | unrecognized structured error or inconsistent output | stop for reconciliation; never guess |

Provider limits must come from exact structured OpenCode/provider errors when
available. Message matching may be used only as a versioned, tested fallback and
must never turn an unknown error into an automatic retry.

The existing optional project cost/token budget remains separate. It must not be
used to represent provider quota, billing, or remote rate limits.

### Cancellation behavior

- Add `dispatcher cancel` for one active dispatch.
- Persist the cancellation request before sending a signal.
- Confirm the process belongs to the same run, host, and recorded launch before
  signalling it.
- Send an interrupt first, equivalent to stopping the active terminal task.
- Wait for the configured grace period, then terminate and finally kill the
  remaining managed process group only if necessary.
- Record whether cancellation completed, timed out, or needs reconciliation.
- Never signal an unknown or reused process ID.

### Retry policy

Add explicit YAML values:

```yaml
stall_policy:
  maximum_retries_per_step: 2
  cooldown_seconds: 180
  on_exhausted: ask
```

The continuation instruction is dispatcher-owned and fixed, for example:

> Continue the current approved step from its last durable result. Do not repeat
> completed side effects. Return the required typed result only.

Rules:

- Every stalled attempt increments the durable per-step stall count.
- Wait for the cooldown before retrying.
- Reuse or replace the session according to the exact failure category.
- Never repeat an uncertain external side effect automatically.
- At the configured limit, ask the operator or halt according to YAML.
- A successful typed result ends the stall sequence but does not erase its audit
  history.

### Tests

- Timeout with child and grandchild cleanup.
- Operator cancellation before and after session identification.
- Temporary connection failure followed by successful continuation.
- Provider rate-limit error with and without a retry delay.
- Context overflow requiring a fresh continuation session.
- Quota/authentication/unknown errors that never retry automatically.
- Process crash during cancellation, cooldown, and retry preparation.
- Exact stall-limit exhaustion and operator wait behavior.

## 3. Real, Harmless OpenCode Smoke Test

### Purpose

Prove that the installed pinned OpenCode version, credentials, event decoder,
session identity, permissions, and process control work with a real model before
allowing repository changes.

### Procedure

- Verify the installed OpenCode version. If it differs from the supported pin,
  capture sanitized fixtures and update the adapter before proceeding.
- Use an empty temporary directory outside every project repository.
- Deny every tool action; the model needs no file, shell, network, or Git tool to
  answer the fixed phrase.
- Disable automatic approval.
- Ask only for `LIVE_SMOKE_OK`.
- Verify the exact session ID, final text, clean temporary directory, zero
  evidence writes, and bounded logs.
- Run a second harmless cancellation smoke to prove managed interruption without
  repository access.
- Store only sanitized version, model, result, duration, and correlation data.

### Completion proof

- The opt-in live suite passes with the intended model and pinned OpenCode.
- No file or repository changed.
- Cancellation leaves no child process running.
- A repeat run also passes.

## 4. Explicit Real-Operation Mode

### Naming and configuration

Use the explicit project modes:

```yaml
execution:
  mode: mock_workflow_test  # normal development and CI
```

or, only in a separately approved private configuration:

```yaml
execution:
  mode: real_operation
```

The live smoke test remains a test command, not a project execution mode.

Because the current public schema used `mock_only`, introduce this terminology
through a versioned configuration migration rather than silently changing the
meaning of schema v1.

### Command

Add a separate command instead of overloading the legacy mock command:

```text
dispatcher execute --config <private-project.yaml> --run-id <id> \
  --approval-ref <operator-decision> --confirm-real-operation
```

The command must fail unless all conditions hold:

- configuration says `real_operation`;
- plan and baseline approvals match exact current hashes;
- run is non-terminal and has no unresolved recovery item;
- every registered repository is clean, on its expected branch, and at the
  expected revision;
- preflight and the required live smoke have passed recently;
- cancellation/stall recovery tests are enabled and configured;
- permission policy is deny-by-default;
- operator approval is bound to the exact project, config, plan, run, and first
  step;
- another dispatcher does not own the run or repository;
- the acknowledgement flag is present.

The command records the approval before launching OpenCode. It pauses normally
at operator gates and never treats a waiting state as failure.

### Tests

- Every missing condition rejects before process launch.
- Mock mode can never reach the real command path.
- Real mode cannot use a public example configuration.
- Changed config, plan, baseline, branch, or repository blocks execution.
- Permission, review, evidence, cancellation, recovery, and barrier behavior use
  the same dispatcher-owned checks as mock workflow tests.
- The real model must return exactly one JSON object containing the exact
  `response_contract` field and every required non-empty field. JSON parsing is
  only the first check; full schema validation follows.
- Prose-wrapped JSON, valid JSON with missing or renamed fields, null required
  fields, empty required strings, incorrect evidence hashes, and incorrect
  evidence sizes must stop the run before workflow state advances.
- The disposable test harness must never repair, extract, or reshape a model
  response into a typed result. It may inspect disposable files for assertions,
  but production-style result parsing must receive the model response directly.

## 5. Disposable Real-Operation Test

Before touching T2, create temporary Git repositories containing synthetic
files and an approved synthetic plan.

Run real OpenCode through the dispatcher to:

- edit one harmless text file;
- produce typed evidence;
- receive an independent reviewer decision;
- commit and integrate through the normal worktree path;
- prove cancellation and recovery on a separate disposable attempt;
- clean all temporary branches, worktrees, sessions, and processes.

The typed result must be emitted directly by the real worker model. A harness or
dispatcher-side repair of natural-language output does not count as proof.

Repeat for:

- one sequential step;
- one cross-repository barrier;
- one same-repository worktree barrier.

No production or T2 repository is used in this gate.

## 6. First T2 Real Operation

### Fresh run required

Do not reuse `tier2-mock-baseline-20260810`. Its T2.2a acceptance is synthetic.

Create a fresh run from the approved historical baseline:

- T2.1a–T2.1f Accepted from observed evidence;
- T2.2a–T2.8b Pending;
- T2.2a is the first executable step.

### Initial scope

Run **T2.2a only** under direct operator observation.

The existing T2 instructions already make this a suitable first real step:

- fixture mode only;
- no live HTTP request;
- no cluster, gateway, Prometheus, analyser, detector, ledger, Ollama, or
  port-forward process;
- no Docker build, deployment, push, PR, or T2.2b;
- bounded authorized files;
- required tests, handoff, evidence, review, and commit.

Use a dispatcher-owned worktree. After executor and reviewer acceptance, merge
through the normal integration barrier and remove temporary branches/worktrees.

Stop when T2.2a is Accepted. T2.2b remains behind its operator gate and must not
start automatically.

### Completion proof

- T2.2a evidence and review are durable and tied to exact commit hashes.
- No prohibited process or external system was used.
- The source repository has only the approved integrated result.
- Temporary Git and OpenCode resources are gone.
- Recovery and support reports contain no credentials or raw model content.
- The operator explicitly decides whether broader T2 real operation may begin.

## Blockers And Mitigations

| Blocker | Plain-language meaning | Mitigation |
|---|---|---|
| Provider errors differ | OpenCode/providers may report limits differently | Support only captured, pinned structured errors; unknowns stop for review |
| Cancellation from another process | A process ID could be stale or reused | Store host/start identity and signal only a verified managed process group |
| T2 ownership gaps | Some Markdown rows omit repository names | Require a local explicit ownership map and owner approval |
| Local-only T2.1f evidence | Two final files are not remote yet | Keep the permanent local evidence worktree; publish later through a separate docs PR |
| Synthetic T2.2a mock result | The workflow test did not implement project files | Use a fresh baseline-backed run with T2.2a Pending for real operation |
| Real model behavior varies | A real model may return malformed commands/results | Strict parser rejects them; bounded retry or operator review, never guessed progress |

## Final Enablement Decision

Completing this plan does not silently enable real operation. After all evidence
passes, the owner must record one final decision approving:

- the explicit `real_operation` configuration;
- the repositories and branches allowed to change;
- the initial run and step;
- the cancellation/stall policy;
- the model/provider roles;
- the stop conditions.

Until that decision exists, the dispatcher remains in mock workflow test mode.
