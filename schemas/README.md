# Generated Schema V1 Documents

The JSON files in this directory are generated from the Pydantic models under
`src/dispatcher/`. They are reviewed artifacts, not hand-maintained schemas.

Regenerate after changing a contract model:

```bash
python -c "from dispatcher.schema_export import write_schema_documents; write_schema_documents('schemas')"
```

Run the schema-export contract test afterward. A changed generated document is a
versioned contract change and must include matching model, test, documentation,
and migration updates.
