# Step 6 - Chat Response Uses Final Answer Only

**Date:** 2026-08-11  
**Status:** Implemented, verified, uncommitted, for human review.

## Real Evidence And Fix

This change addresses a confirmed disposable live-run failure, not a hypothetical
case. That run emitted three OpenCode `text` events. The first two were interim
narration around tool work; the third event alone was a well-formed,
contract-compliant executor-result JSON object. The former decoder joined all
three events with newlines, so the worker path rejected the resulting prose-led
string with `worker response is not one strict JSON object: Expecting value:
line 1 column 1 (char 0)`.

`src/dispatcher/sessions.py:212-218` keeps the existing required-response
presence check exactly as `if require_response and not self._chat_parts`, then
returns `self._chat_parts[-1]` rather than joining every collected part.
`src/dispatcher/sessions.py:224-234` remains responsible for validating,
redacting, byte-accounting, and collecting every nonempty text event. Therefore
the output limit and required-response validation are unchanged; only the
derived `chat_response` selects the final text event.

Interim events are not discarded from the debug trail. `consume_line` invokes
`_append_metadata` after each validated event at
`src/dispatcher/sessions.py:189-198`; `_append_metadata` at
`src/dispatcher/sessions.py:272-288` retains bounded sanitized metadata,
including the `text` event type and part ID. The complete redacted JSONL event
line, including interim text, continues to be written to the private stdout
log at `src/dispatcher/sessions.py:679-715`. This report uses the precise
distinction: `SessionResult.raw` is bounded metadata rather than full event
bodies, while the private stdout JSONL log is the forensic record containing
the full redacted text. The new unit and contract tests assert that every
interim `text` part ID remains in `raw` while `chat_response` contains only
the final JSON text.

## Protocol Investigation

Reviewed the current local compatibility material before changing the decoder:

- `docs/compatibility.md:5-11` pins OpenCode compatibility to `1.18.11` and
  identifies the sanitized fixture contract, but does not define text-event
  continuation or final-answer boundaries.
- `docs/protocol.md:1-20` and `docs/protocol.md:129-144` define dispatcher
  machine-output requirements, not OpenCode event-fragment semantics.
- `tests/fixtures/opencode/1.18.11/README.md:8-18` identifies the tagged
  `v1.18.11` run-command source provenance, but specifies no continuation
  field or final-text marker.
- Every pre-existing artifact in `tests/fixtures/opencode/1.18.11/` was read.
  The JSONL streams had at most one text event each: new, resumed, forked,
  malformed-valid-tail, and duplicate-key each have one; tool and error streams
  have none. The timeout, nonzero-exit, session list/export/import, and README
  artifacts have no additional JSONL sequencing signal.

No reviewed local evidence documents a reliable way to distinguish a single
final answer split across several text events from distinct narration and final
answer events. `step_start`/`step_finish` boundaries and `messageID` values are
present in some events, but the local protocol does not assign either a
continuation meaning. The real failure had tool/step-finish activity between
each text event, which clearly identified narration there, but that observation
cannot safely define a general protocol rule.

The implemented rule is therefore intentionally simple: the last validated
`text` event is the session answer. Its limit is explicit: if a future OpenCode
protocol legitimately splits one final contract object over multiple text
events without a documented reliable delimiter, this decoder returns the final
fragment and the strict downstream parser fails closed. That is safer than
accepting narration mixed with an authorization or result contract; a protocol
upgrade should add an evidenced assembly rule and corresponding fixtures.

## Shared Consumers

Both response consumers benefit because they receive the same decoder field:

- The worker path calls `_strict_json_object(result.chat_response)` at
  `src/dispatcher/execution.py:211-221` before validating executor or reviewer
  contracts. The final JSON response is now presented without narration.
- The supervisor path writes and returns that same field at
  `src/dispatcher/execution.py:152-160`; the sequential driver subsequently
  passes it to `parse_supervisor_command`. A narrated supervisor response had
  the same prior risk of failing the strict command parser. The adapted
  sequential integration test emits narration before every supervisor,
  executor, and reviewer final response and completes successfully, proving
  both paths use the corrected value.
- The preflight exact-OK gate remains `result.chat_response.strip() != "OK"`
  at `src/dispatcher/preflight.py:264-269`. Its legitimate one-text-event case
  is unchanged: `test_run_session_streams_validated_output_to_private_logs`
  retains its existing `FIXTURE_OK` assertion at
  `tests/unit/test_sessions.py:83-105`, and `_success_body` still emits one
  text event followed by `step_finish` at `tests/unit/test_sessions.py:57-63`.

## Fixture And Tests

Added
`tests/fixtures/opencode/1.18.11/run-narration-then-result.jsonl`, and listed
it in `tests/fixtures/opencode/1.18.11/README.md:30-35`. It models two
step-start/narration/tool-use/step-finish cycles, followed by a final step with
one complete schema-v1 executor result. Its exact content is:

```jsonl
{"type":"step_start","timestamp":1700000000600,"sessionID":"ses_fixture_narration_result","part":{"id":"prt_fixture_narration_start_one","sessionID":"ses_fixture_narration_result","messageID":"msg_fixture_narration_one","type":"step-start","snapshot":"sha_fixture_narration_base"}}
{"type":"text","timestamp":1700000000601,"sessionID":"ses_fixture_narration_result","part":{"id":"prt_fixture_narration_one","sessionID":"ses_fixture_narration_result","messageID":"msg_fixture_narration_one","type":"text","text":"I will inspect the synthetic fixture before producing the result.","time":{"start":1700000000600,"end":1700000000601}}}
{"type":"tool_use","timestamp":1700000000602,"sessionID":"ses_fixture_narration_result","part":{"id":"prt_fixture_narration_tool_one","sessionID":"ses_fixture_narration_result","messageID":"msg_fixture_narration_one","type":"tool","callID":"call_fixture_narration_read","tool":"read","state":{"status":"completed","input":{"filePath":"/fixture/project/README.md"},"output":"synthetic fixture content","title":"Read synthetic fixture file","metadata":{},"time":{"start":1700000000601,"end":1700000000602}}}}
{"type":"step_finish","timestamp":1700000000603,"sessionID":"ses_fixture_narration_result","part":{"id":"prt_fixture_narration_finish_one","sessionID":"ses_fixture_narration_result","messageID":"msg_fixture_narration_one","type":"step-finish","reason":"tool-calls","snapshot":"sha_fixture_narration_one","cost":0.001,"tokens":{"total":11,"input":7,"output":4,"reasoning":0,"cache":{"read":0,"write":0}}}}
{"type":"step_start","timestamp":1700000000604,"sessionID":"ses_fixture_narration_result","part":{"id":"prt_fixture_narration_start_two","sessionID":"ses_fixture_narration_result","messageID":"msg_fixture_narration_two","type":"step-start","snapshot":"sha_fixture_narration_one"}}
{"type":"text","timestamp":1700000000605,"sessionID":"ses_fixture_narration_result","part":{"id":"prt_fixture_narration_two","sessionID":"ses_fixture_narration_result","messageID":"msg_fixture_narration_two","type":"text","text":"The synthetic worktree is clean; I will now produce the contract result.","time":{"start":1700000000604,"end":1700000000605}}}
{"type":"tool_use","timestamp":1700000000606,"sessionID":"ses_fixture_narration_result","part":{"id":"prt_fixture_narration_tool_two","sessionID":"ses_fixture_narration_result","messageID":"msg_fixture_narration_two","type":"tool","callID":"call_fixture_narration_status","tool":"bash","state":{"status":"completed","input":{"command":"git status --porcelain"},"output":"","title":"Check synthetic worktree status","metadata":{},"time":{"start":1700000000605,"end":1700000000606}}}}
{"type":"step_finish","timestamp":1700000000607,"sessionID":"ses_fixture_narration_result","part":{"id":"prt_fixture_narration_finish_two","sessionID":"ses_fixture_narration_result","messageID":"msg_fixture_narration_two","type":"step-finish","reason":"tool-calls","snapshot":"sha_fixture_narration_two","cost":0.002,"tokens":{"total":22,"input":14,"output":8,"reasoning":0,"cache":{"read":1,"write":0}}}}
{"type":"step_start","timestamp":1700000000608,"sessionID":"ses_fixture_narration_result","part":{"id":"prt_fixture_final_start","sessionID":"ses_fixture_narration_result","messageID":"msg_fixture_final_result","type":"step-start","snapshot":"sha_fixture_narration_two"}}
{"type":"text","timestamp":1700000000609,"sessionID":"ses_fixture_narration_result","part":{"id":"prt_fixture_final_result","sessionID":"ses_fixture_narration_result","messageID":"msg_fixture_final_result","type":"text","text":"{\"result_version\":1,\"response_contract\":\"dispatcher.executor_result.v1\",\"dispatch_id\":\"dispatch_fixture\",\"attempt\":1,\"step_id\":\"prepare_fixture\",\"repository\":{\"repo_id\":\"fixture_repo\",\"base_revision\":\"base_fixture\",\"result_revision\":\"result_fixture\",\"patch_sha256\":null},\"evidence\":[],\"verification\":[{\"check_id\":\"fixture_check\",\"status\":\"passed\",\"summary\":\"Synthetic fixture verified.\"}],\"summary\":\"Synthetic fixture completed.\",\"transcript_ref\":null,\"outcome\":\"completed\"}","time":{"start":1700000000608,"end":1700000000609}}}
{"type":"step_finish","timestamp":1700000000610,"sessionID":"ses_fixture_narration_result","part":{"id":"prt_fixture_final_finish","sessionID":"ses_fixture_narration_result","messageID":"msg_fixture_final_result","type":"step-finish","reason":"stop","snapshot":"sha_fixture_narration_result","cost":0.003,"tokens":{"total":35,"input":22,"output":13,"reasoning":0,"cache":{"read":2,"write":0}}}}
```

New regression tests:

- `test_decoder_uses_only_the_last_text_event_as_chat_response` at
  `tests/unit/test_sessions.py:327-390` feeds a fake JSONL stream with two
  narration events and a final JSON event. It asserts the response is exactly
  the final event and that all text-event IDs remain in bounded raw metadata.
- `test_decoder_uses_only_final_text_from_narration_then_result_fixture` at
  `tests/contract/test_opencode_compatibility.py:157-179` decodes the new
  fixture, asserts exact final JSON text, loads it with `json.loads`, and
  validates it through `parse_executor_result`.
- `test_fake_opencode_executes_narrated_rework_review_and_completion_in_disposable_git`
  at `tests/integration/test_sequential_git_e2e.py:21-129` is the adapted
  integration proof. Its local fake CLI now emits two narrated tool steps
  before every final response through `_emit_narrated_response` at
  `tests/fixtures/opencode/fake_cli.py:114-148`. The completed sequential run
  exercises narrated supervisor commands and narrated worker contracts end to
  end.

Verification completed after all code, fixture, and test changes:

```text
.venv/bin/python -m pytest tests/unit/test_sessions.py tests/contract/test_opencode_compatibility.py tests/integration/test_sequential_git_e2e.py tests/integration/test_execute_command_disposable.py -q -m "not live_opencode"
43 passed in 9.39s

.venv/bin/ruff check src/dispatcher/sessions.py tests/unit/test_sessions.py tests/contract/test_opencode_compatibility.py tests/integration/test_sequential_git_e2e.py tests/fixtures/opencode/fake_cli.py
All checks passed!

.venv/bin/python -m pytest tests -q -m "not live_opencode"
261 passed, 5 deselected in 36.29s
```

The final pytest summary is strictly greater than the prior Step 5 total of
259 passed tests.

## Scope Confirmation

No commit, push, or branch creation was performed. All repository changes,
including this report, remain uncommitted for human review. The fake CLI is a
local executable copied to pytest temporary directories; its disposable Git
commits occur only inside those test repositories, not in this repository.

`config/projects/local/`, `config/state/`, and `state/` were neither read nor
modified. No live OpenCode invocation, network or HTTP call, credential use,
or external service call was made. Verification explicitly used the local fake
OpenCode executable and `-m "not live_opencode"`.
