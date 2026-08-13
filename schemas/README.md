# Generated Contract Schemas

The JSON files in this directory are generated from the Pydantic models under
`src/dispatcher/`. They are reviewed artifacts, not hand-maintained schemas.

Regenerate after changing a contract model:

```bash
python -c "from dispatcher.schema_export import write_schema_documents; write_schema_documents('schemas')"
```

Run the schema-export contract test afterward. A changed generated document is a
versioned contract change and must include matching model, test, documentation,
and migration updates.

Current worker contracts include model-authored `executor-proposal-v2.json`,
dispatcher-authored `executor-result-v1.json`, and model-authored
`reviewer-result-v1.json`. Normalized plans use `normalized-plan-v2.json`.
The schema-v2 project configuration requires a top-level `mcp.servers`
registry and a `mcp_tools` list on every role definition.
