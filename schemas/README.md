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

`cluster-operation-manifest-v1.json` is the static repository contract for
dispatcher-owned cluster operations. Phase 2 adds generated sanitized contracts
for `cluster-operation-approval-snapshot-v1.json`,
`cluster-operation-approval-v1.json`, and
`cluster-operation-lifecycle-v1.json`, plus a local SQLite journal. These
contracts do not provide a production snapshot collector, cluster connection,
command output storage, Kubernetes/Helm execution, or a generic port-forward or
network probe capability. The manifest/lifecycle schemas include a
dispatcher-only Service loopback forward and fixed TLS/DC8 no-client-certificate
rejection record, exercised only through injected fakes in this release.

`real-operation-approval-v1.json` publishes the autonomous-scope approval record,
including every reviewed cluster-operation envelope. The envelope is still only a
contract boundary: it does not wire the cluster runner into execution or grant a
worker Kubernetes, Helm, or port-forward permission.
