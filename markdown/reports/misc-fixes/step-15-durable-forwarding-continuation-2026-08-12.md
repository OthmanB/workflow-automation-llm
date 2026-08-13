# Step 15 Durable Forwarding Continuation Report

Date: 2026-08-12

## Outcome

Production continuation now discovers every durable `FORWARDED` dispatch,
delivers its authoritative stored sanitized payload to a successful supervisor
turn, and only then records `ACKNOWLEDGED`. The Step 14 test-only
pre-acknowledgement workaround was removed. Failed-batch continuation now uses
this production path, retries only failed steps, preserves accepted siblings and
historical failed batches, and reaches completion without
`dispatch_in_flight`.

The final non-live suite passed with 391 tests, 25 more than the required
366-pass baseline, with the same 9 live tests deselected.

## 1. Exact Files Changed

This step changed exactly these files:

1. `src/dispatcher/execution.py`
2. `tests/unit/test_execution.py`
3. `tests/live/test_real_operation_disposable.py`
4. `docs/protocol.md`
5. `docs/operations.md`
6. `markdown/reports/misc-fixes/step-15-durable-forwarding-continuation-2026-08-12.md`

All pre-existing uncommitted Steps 1-14 work and unrelated changes were
preserved.

## 2. Production Root Cause

`SequentialExecutionCoordinator.run_to_completion()` previously initialized
`pending_acknowledgements` to an empty list on every invocation. The list was
populated only after the same invocation returned from `execute_worker()` or
`execute_batch()`. When a mixed batch persisted one successful child as
`FORWARDED` and another child caused `finalize_batch()` to move the run to
`WAITING_OPERATOR`, `run_to_completion()` returned before assigning the
successful IDs. A later invocation therefore had no in-memory knowledge of the
durable forwarding and could neither deliver nor acknowledge it.

The Step 14 `_acknowledge_leftover_forwardings()` helper masked this by directly
acknowledging persisted forwarding before supervisor receipt. Direct source and
test execution confirmed the defect; prior reports were not treated as proof.

## 3. Discovery And Ordering

`SequentialExecutionCoordinator._continuation_prompt()` at
`src/dispatcher/execution.py:340` runs after bootstrap rendering and
`workflow.activate()` returns the authoritative current record and generation.
It selects dispatches whose state is exactly `DispatchStatus.FORWARDED` and
sorts them by:

1. `dispatch.last_event.sequence`, which is the forwarding transition event
   while the dispatch remains `FORWARDED`;
2. `dispatch.dispatch_id` as a stable tie-breaker.

All other states are ignored, including malformed payloads attached to those
historical states. Duplicate pending dispatch identities fail closed. The
ordered IDs initialize the existing `pending_acknowledgements` list at
`src/dispatcher/execution.py:421`; dictionary insertion and worker completion
order are not used.

## 4. Resume Envelope

When pending forwarding exists, the first supervisor prompt is deterministic
JSON serialized with `sort_keys=True`:

```text
{
  "kind": "orchestration_resume",
  "bootstrap": "<existing complete rendered bootstrap>",
  "pending_forwardings": [
    {
      "dispatch_id": "dispatch-example",
      "payload": {
        "kind": "executor_result",
        "dispatch_id": "dispatch-example"
      }
    }
  ]
}
```

Entries contain parsed stored objects, not concatenated prose. Worker prompts,
stderr, credentials, and private environment are not included. With no pending
forwarding, `_continuation_prompt()` returns the exact original bootstrap string
and an empty acknowledgement list.

## 5. Persisted-Payload Validation

For every pending dispatch, the coordinator loads `DispatchPayload` through
`StateStore.load_dispatch_payload()` and requires a non-null, non-whitespace
`forwarding_payload`. The shared strict parser at
`src/dispatcher/execution.py:526` requires exactly one JSON object and rejects
malformed JSON, surrounding Markdown/prose, duplicate keys at any object depth,
and non-finite `NaN`/`Infinity` constants. It then requires:

- embedded `dispatch_id` exactly equal to the owning `DispatchRecord` ID;
- executor dispatch `kind` exactly `executor_result`;
- reviewer dispatch `kind` exactly `reviewer_result`.

State-store load failures are converted to `ExecutionCoordinatorError` without
exposing payload content. All payloads are validated while constructing the
side-effect-free envelope; if any one fails, no supervisor call or
acknowledgement occurs and all pending dispatches remain `FORWARDED`.

## 6. Redaction And Digest Decision

Current persistence computes `DispatchRecord.forwarding_digest` from the
pre-persistence forwarding string in `SequentialWorkflow`, then
`StateStore._write_dispatch_payload()` applies `redact_text()` to the stored
payload. Redaction can therefore change the stored bytes after digest creation.

This step deliberately does not compare `forwarding_digest` with the stored
payload. Continuation replays the authoritative stored sanitized representation
and validates its strict JSON shape, identity, and role kind. It neither
reconstructs nor exposes pre-redaction text, and it introduces no false
integrity failure when redaction changes content. Forwarding-digest semantics
and the state schema were not changed. A direct test persists a forwarding whose
Bearer value is redacted and proves the sanitized stored object is replayed
despite the pre-redaction digest.

## 7. Acknowledgement And Replay Semantics

The established loop ordering remains:

1. call `run_supervisor_turn()` with the resume envelope;
2. after successful return, acknowledge each ordered dispatch ID;
3. refresh readiness;
4. parse and process the returned supervisor command.

If the supervisor call raises, no acknowledgement is attempted and a later
invocation receives the same deterministic envelope. If receipt succeeds but
command parsing fails, delivery remains acknowledged, matching the prior
meaning of acknowledgement. Once acknowledged, the dispatch is excluded from
future envelopes.

A crash after supervisor receipt but before the durable acknowledgement may
cause replay. Delivery is therefore at least once across process crashes, with
`dispatch_id` as the idempotency identity; no unsafe exactly-once guarantee was
introduced.

## 8. Step 14 Workaround Removal

`_acknowledge_leftover_forwardings()` and its call from
`_run_bounded_orchestration()` were deleted from
`tests/live/test_real_operation_disposable.py`. The harness now reloads only the
generation and calls production `run_to_completion()`.

The reactive fake supervisor records every received prompt. The batch
reconciliation full-loop test asserts that the first post-reconciliation prompt
is the production `orchestration_resume` envelope containing the successful
sibling's exact stored forwarding. It then proves that sibling is acknowledged,
only the failed child is retried, the first batch remains `FAILED`, the sibling
attempt count stays one, the replacement batch joins, the run succeeds, no
`dispatch_in_flight` obligation remains, and all leases are released. Removing
production discovery makes this test fail.

## 9. Tests And Focused Results

New focused tests in `tests/unit/test_execution.py`:

- `test_continuation_without_pending_forwardings_preserves_bootstrap`
- `test_continuation_replays_one_authoritative_sanitized_executor_forwarding`
- `test_continuation_accepts_reviewer_forwarding_kind`
- `test_continuation_order_is_stable_across_dispatch_insertion_orders`
- `test_continuation_excludes_acknowledged_dispatches`
- `test_continuation_ignores_every_non_forwarded_dispatch_state`, parameterized
  over `PREPARED`, `RUNNING`, `COMPLETED`, `ACKNOWLEDGED`, `FAILED`, and
  `ABANDONED`
- `test_continuation_rejects_corrupt_stored_forwarding`, parameterized over
  missing, empty, malformed, Markdown, surrounding prose, duplicate-key,
  non-finite-number, wrong-identity, and wrong-kind payloads
- `test_invalid_pending_payload_prevents_all_acknowledgements_and_supervisor_delivery`
- `test_successful_supervisor_receipt_acknowledges_then_refreshes_and_processes_command`
- `test_supervisor_failure_leaves_forwarding_for_at_least_once_replay`
- `test_successful_supervisor_receipt_acknowledges_all_pending_forwardings`
- `test_invalid_supervisor_command_after_receipt_keeps_delivery_acknowledgement`

Extended production full-loop proof:

- `test_batch_reconciliation_full_loop_with_fake_runner`

The required Step 14 fake full-loop tests all remain green:

- `test_batch_reconciliation_full_loop_with_fake_runner`
- `test_solo_reconciliation_full_loop_with_fake_runner`
- `test_review_rework_resume_full_loop_with_fake_runner`
- `test_halt_full_loop_with_fake_runner`

Final focused commands and results:

```text
.venv/bin/python -m pytest tests/unit/test_execution.py tests/contract/test_protocol_documentation.py -q
40 passed in 2.69s

.venv/bin/python -m pytest tests/unit/test_execution.py tests/integration/test_batch_execution.py tests/live/test_real_operation_disposable.py -q -m "not live_opencode"
63 passed, 8 deselected in 8.65s
```

## 10. Full Non-Live Result

```text
.venv/bin/python -m pytest tests -q -m "not live_opencode"
391 passed, 9 deselected in 54.69s
```

This is greater than the required `366 passed, 9 deselected` baseline.

## 11. Live Collect-Only Result

```text
.venv/bin/python -m pytest tests/live/test_real_operation_disposable.py --collect-only -q
29 tests collected in 0.21s
```

All eight `live_opencode` scenarios and 21 unmarked harness tests collect. No
live scenario was executed.

## 12. Static Checks

```text
.venv/bin/ruff check src/dispatcher/execution.py tests/unit/test_execution.py tests/live/test_real_operation_disposable.py
All checks passed!

git diff --check
no output (pass)
```

## 13. Constraints Confirmed

- No commit was created, amended, or pushed, and no branch was created.
- No live OpenCode invocation or live-marked test was executed.
- No network or HTTP call was made.
- No credentials were used or accessed.
- No `config/projects/local/`, `config/state/`, repository-root `state/`,
  private T2 state, or other prohibited path was inspected or modified.
- Result validation, verification enforcement, dispatch identity, evidence,
  repository cleanliness, operator recovery, cancellation, process identity,
  and historical batch semantics were not weakened.
- Old evidence reports were not edited to rewrite history.
- All Step 15 changes and this report remain uncommitted.

## 14. Deviations

There were no deviations from the requested production behavior. The resume
envelope example in `docs/protocol.md` uses a `text` fence rather than the
document's supervisor-command `json` fences because the existing documentation
contract test intentionally parses every `json` fence as an executable
supervisor command. The example content remains exact JSON. The shared strict
parser was also tightened to reject non-finite numeric constants after static
review identified Python's permissive default; this directly enforces the
requested strict-JSON requirement.
