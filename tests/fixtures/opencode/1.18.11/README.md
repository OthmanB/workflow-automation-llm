# OpenCode 1.18.11 Compatibility Fixtures

These sanitized fixtures define the external CLI shapes that the dispatcher
must support before real OpenCode execution is re-enabled.

## Provenance

- Installed version verified with `opencode --version`: `1.18.11`.
- JSONL event envelopes and payloads are derived from the exact tagged source:
  `https://github.com/anomalyco/opencode/blob/v1.18.11/packages/opencode/src/cli/cmd/run.ts`.
- Sanitized export structure is derived from:
  `https://github.com/anomalyco/opencode/blob/v1.18.11/packages/opencode/src/cli/cmd/export.ts`.
- Import output is derived from:
  `https://github.com/anomalyco/opencode/blob/v1.18.11/packages/opencode/src/cli/cmd/import.ts`.
- Session-list structure was captured with
  `opencode session list -n 1 --format json` and manually replaced with fixture
  IDs, timestamps, title, project ID, and directory.
- No model-backed command was run to create these fixtures.

## Sanitization

- Session, message, part, call, and project IDs use a `fixture` marker.
- Filesystem paths are under `/fixture`.
- Prompts, reasoning, tool input/output, and errors are synthetic.
- Timestamps are fixed synthetic values.
- No credentials, private prompts, real session IDs, or user paths are present.

## Files

- `run-new-session.jsonl`: successful new session event stream.
- `run-resumed-session.jsonl`: successful resumed-session event stream.
- `run-forked-session.jsonl`: successful forked-session event stream.
- `run-tool-events.jsonl`: completed tool and reasoning events.
- `run-narration-then-result.jsonl`: narrated multi-step session with a final executor result.
- `run-error.jsonl`: structured session error event.
- `run-malformed.jsonl`: one malformed line followed by one valid event.
- `run-nonzero-exit.json`: process-level nonzero-exit metadata.
- `run-timeout.json`: dispatcher-side timeout metadata; OpenCode does not emit a
  dedicated timeout JSONL event.
- `session-list.json`: structured session-list output.
- `session-export-sanitized.json`: minimal sanitized export shape.
- `session-import-output.txt`: successful import output shape.

## Refresh policy

When changing the supported OpenCode version:

1. Verify the local binary version.
2. Review the matching tagged `run.ts` and `export.ts` sources.
3. Capture `session list --format json` and sanitize every value.
4. Refresh all affected fixtures without using real prompts or IDs.
5. Run the fixture sanitation and decoder contract tests.
6. Update `[tool.dispatcher.opencode]` in `pyproject.toml` in the same change.
7. Do not enable real dispatch until the live harmless compatibility smoke test
   for the new version passes.
