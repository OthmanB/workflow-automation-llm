# Cluster Operation Manifest Schema V1

`schemas/cluster-operation-manifest-v1.json` and
`src/dispatcher/cluster_operations.py:ClusterOperationManifest` define a strict,
repository-owned, static contract for Phase 1. The public library API
`validate_cluster_operation_references_for_plan(config=..., plan=...)` admits
only normalized-plan references. The separate public post-commit API
`validate_cluster_operations_for_plan(config=..., plan=...)` validates every
manifest and declared file. Neither runs a cluster command, contacts a cluster,
reads referenced source/values/chart content, or reads or persists Secret
content. Phase 2 adds local, sanitized lifecycle state after this static
validation. Phase 3 adds a dispatcher-library fixed-argv runner only. Phase 4
adds a separate dispatcher-owned read-only snapshot collector. Phase 5 adds a
dispatcher-private production port-forward/TLS/DC8 lifecycle. `dispatcher
execute` creates those adapters only for a real-operation approval carrying a
cluster-operation envelope; neither boundary is exposed to workers.

## Required Shape

```yaml
schema_version: 1
operation_id: sample-app-deploy
context: integration-cluster
source_identity:
  repository_id: application
  revision: approval_snapshot
allowed_namespaces: [platform]
allowed_files:
  - path: deploy/manifests/sample-app.yaml
    sha256: approval_snapshot
  - path: deploy/charts/sample-app/Chart.lock
    sha256: approval_snapshot
  - path: deploy/values/sample-app.yaml
    sha256: approval_snapshot
secret_requirements:
  - namespace: platform
    name: sample-app-credentials
    keys: [username]
actions:
  - action: kubectl_server_dry_run
    namespace: platform
    timeout_seconds: 60
    expected_resources:
      - {api_version: apps/v1, kind: Deployment, namespace: platform, name: sample-app}
    readiness_probes:
      - probe: deployment_available
        resource: {api_version: apps/v1, kind: Deployment, namespace: platform, name: sample-app}
    manifest_files:
      - path: deploy/manifests/sample-app.yaml
        sha256: approval_snapshot
  - action: helm_upgrade_install
    namespace: platform
    timeout_seconds: 90
    expected_resources:
      - {api_version: apps/v1, kind: Deployment, namespace: platform, name: sample-app}
    readiness_probes:
      - probe: deployment_available
        resource: {api_version: apps/v1, kind: Deployment, namespace: platform, name: sample-app}
    release: sample-app
    chart_path: deploy/charts/sample-app
    chart_lock_file: {path: deploy/charts/sample-app/Chart.lock, sha256: approval_snapshot}
    values_files:
      - path: deploy/values/sample-app.yaml
        sha256: approval_snapshot
  - action: port_forward
    action_id: sample-app-forward
    namespace: platform
    expected_resources:
      - {api_version: v1, kind: Service, namespace: platform, name: sample-app}
    resource: {api_version: v1, kind: Service, namespace: platform, name: sample-app}
    local_port: 18080
    remote_port: 8443
    startup_timeout_seconds: 30
    probe_timeout_seconds: 30
    lifetime_timeout_seconds: 90
  - action: tls_dc8_no_client_certificate_rejection
    port_forward_action_id: sample-app-forward
rollback:
  automatic: true
  strategy: restore_approval_snapshot
```

`approval_snapshot` is an explicit placeholder, not a digest or source revision.
It prevents a static manifest from pinning today's source, chart, values, tool,
or server state. Phase 2 approval material must calculate and bind actual
SHA-256 values and the committed source revision together at operator approval
and retain them through mutation and rollback.

## Lifecycle

1. Plan/config admission calls
   `validate_cluster_operation_references_for_plan()`. It validates only the
   target, matching preflight binding, allowed repository, normalized manifest
   path, operation-manifest root, declared ordered action tuple, and automatic
   rollback requirement. The manifest and its files may be absent: the executor
   is expected to create them in its authorized repository scope.
2. Dispatcher structured Git commits that executor output. No cluster action is
   introduced by this commit.
3. Before any future snapshot, approval, or mutation, call
   `validate_cluster_operations_for_plan()` against that exact committed source
   revision. It performs the complete checks below and fails closed if the
   manifest or any declared file is missing or invalid, including when the
   manifest action types are omitted, added, changed, or reordered from the
   plan's preauthorized tuple. There is no fallback that skips this post-commit
   validation.

## Post-Commit Static Checks

- The manifest context and source repository match the selected target and plan
  step. Every declared file is normalized, exists below the repository's allowed
  source-file roots, and every action file is declared exactly once in
  `allowed_files`.
- Helm accepts only a local chart directory with a declared locked `Chart.lock`
  file; repositories and OCI URLs are not representable. Every action timeout
  is bounded by the selected target.
- Namespaces, expected resource identities, and readiness probes are finite and
  typed. Probe types are `deployment_available`, `statefulset_ready`, and
  `job_complete`, each tied to the matching exact resource identity.
- `rollback.automatic: true` with `restore_approval_snapshot` is mandatory.
- Secret requirements are metadata only: namespace, name, and required key
  names. Inline Secret objects, `data`, `stringData`, and values are not part of
  the schema.
- A `port_forward` names an exact core `v1` `Service`, has a unique action ID
  and local port, and exposes no bind address, host, request, shell, certificate,
  or client credential field. The implementation fixes its bind address to
  `127.0.0.1`. Its explicit startup, probe, and lifetime limits are each bounded
  by the selected target. Each forward is immediately followed by exactly one
  `tls_dc8_no_client_certificate_rejection` linked by that action ID.

All models forbid unknown fields. There is therefore no generic argv, shell,
field-manager, Helm `--set`, environment expansion, wildcard, absolute path,
traversal, Secret value, private key, token, or kubeconfig input surface.

## Approval-Bound Lifecycle

Phase 2 defines the strict local contracts in
`cluster-operation-approval-snapshot-v1.json`,
`cluster-operation-approval-v1.json`, and
`cluster-operation-lifecycle-v1.json`. A snapshot binds the exact committed
source revision, plan/config/validated-manifest/envelope/preflight/Tier-1 digests,
binary and toolchain identity digests, action digests, exact approved source-file
digests, normalized resource/release/image/Secret-metadata fingerprints, and a
rollback entry for each Helm release. A rollback entry proves the release was
new or records its exact pre-snapshot revision. It contains no manifests,
command output, kubeconfig, credential, certificate/key, or Secret value.

The SQLite journal is keyed by `(run_id, operation_id, source_revision)`. Its
immutable identity includes the plan/config/manifest/action/rollback bindings;
every update uses generation compare-and-swap and appends a digest-only audit
event. Phase 3 command evidence persists only bounded stdout/stderr SHA-256
values, duration, exit status, command kind, and static action identity. The
lifecycle vocabulary and allowed successor table are:

| State | Allowed next states |
|---|---|
| `DISCOVERED` | `STATIC_VALIDATED`, `FAILED` |
| `STATIC_VALIDATED` | `SNAPSHOT_CAPTURED`, `FAILED` |
| `SNAPSHOT_CAPTURED` | `APPROVED`, `FAILED` |
| `APPROVED` | `SERVER_DRY_RUN_PASSED`, `PORT_FORWARD_INTENT`, `FAILED` |
| `SERVER_DRY_RUN_PASSED` | `MUTATION_STARTED`, `PORT_FORWARD_INTENT`, `FAILED` |
| `PORT_FORWARD_INTENT` | `PORT_FORWARD_STARTED`, `RECONCILIATION_REQUIRED` |
| `PORT_FORWARD_STARTED` | `TLS_DC8_PROBING`, `ROLLBACK_STARTED`, `FAILED`, `RECONCILIATION_REQUIRED` |
| `TLS_DC8_PROBING` | `PORT_FORWARD_INTENT`, `SUCCEEDED`, `ROLLBACK_STARTED`, `FAILED`, `RECONCILIATION_REQUIRED` |
| `MUTATION_STARTED` | `MUTATED`, `ROLLBACK_STARTED`, `FAILED`, `RECONCILIATION_REQUIRED` |
| `MUTATED` | `PROBING`, `ROLLBACK_STARTED`, `FAILED`, `RECONCILIATION_REQUIRED` |
| `PROBING` | `PORT_FORWARD_INTENT`, `SUCCEEDED`, `ROLLBACK_STARTED`, `FAILED`, `RECONCILIATION_REQUIRED` |
| `ROLLBACK_STARTED` | `ROLLED_BACK`, `FAILED`, `RECONCILIATION_REQUIRED` |
| `FAILED` | none |
| `RECONCILIATION_REQUIRED` | `ROLLBACK_STARTED`, `FAILED` |
| `SUCCEEDED`, `ROLLED_BACK` | none |

Snapshots attach only from `STATIC_VALIDATED`; approvals attach only from
`SNAPSHOT_CAPTURED`. Both must match the immutable identity exactly, and an
approval cannot outlast its snapshot. Expiry is evaluated against an explicit
UTC time at each future transition. An interrupted `MUTATION_STARTED` record is
never advanced automatically: a future runner must require explicit operator
reconciliation.

## Real-Operation Envelopes

`schemas/real-operation-approval-v1.json` publishes the preauthorization record
used by `dispatcher permission-manifest` and `dispatcher approve-real-operation`.
Before executor work, every cluster-operation step in the ordered autonomous
scope receives one immutable `ClusterOperationEnvelope`. It binds the run, step,
repository, target and context, normalized operation-manifest path, exact ordered
action types, mandatory automatic rollback intent, operation/source roots,
snapshot-age ceiling, plan/config digests, and its own canonical digest. The
permission-manifest JSON contains every full envelope for owner review; a scope
with any envelope requires its exact `--scope-manifest-digest` at real-operation
approval, even if the scope has one step.

After dispatcher structured Git has committed executor output, the public
`create_auto_approved_cluster_operation_approval()` function accepts only a
post-commit `ValidatedClusterOperation`, source revision, and real-operation
approval. It refuses a missing, extra, changed, or action-reordered envelope and
returns a snapshot-pending lifecycle approval derived from that envelope. It does
not collect a snapshot or run a tool. Attaching the result still requires a fresh
sanitized snapshot with the exact source, manifest, action, toolchain, and expiry
bindings; no second human approval is created at that point.

`dispatcher execute` integrates this lifecycle only at the dispatcher acceptance
boundary: after the executor's structured-Git source revision and an accepted,
freshly checked reviewer result, but before reviewer acceptance or supervisor
forwarding. It still requires a valid T2.5 operation manifest, exact approved
envelope, real-operation approval, and caller-supplied Tier-1 invariant digest.
The production adapters are invoked only after that snapshot and lifecycle
approval validation; they are not created for an approval without a cluster
envelope. This project has not performed a real T2.5 deployment; non-live tests
use injected fake command runners and mocked process/socket boundaries only.

`dispatcher cluster-operation status` only reads an existing local journal.
`dispatcher cluster-operation approve` accepts a pre-created sanitized snapshot
JSON and writes its matching owner approval after static validation. The
local-only `dispatcher cluster-operation snapshot` command requires an existing
`STATIC_VALIDATED` journal record, a revalidated post-commit plan/operation,
the exact real-operation approval, and a caller-supplied Tier-1 digest. It
writes a private sanitized snapshot JSON but does not attach it, approve it, or
mutate the journal. `status` and `approve` never invoke Kubernetes, Helm,
port-forwarding, OpenCode, or a network call; `snapshot` can issue only the
read-only fixed argv described below. There is no standalone cluster-mutation
command; mutation is available only through the guarded `dispatcher execute`
acceptance integration described above.

## Phase 4 Snapshot Boundary

`capture_cluster_operation_snapshot()` is a dispatcher-library API separate
from `ClusterOperationRunner.execute`. It requires `Config`, the static
`ValidatedClusterOperation`, the exact committed source revision,
`RealOperationApproval`, a caller-supplied Tier-1 invariant digest, and an
injected fixed-argv command runner. It first re-reads and fully validates the
manifest, validates the exact preauthorized envelope, and verifies every
declared source/chart/value file plus both target-pinned tool binaries. It
rechecks files and binaries after collection, so a changed input fails closed.

The collector has no generic argv, shell, subprocess fallback, raw-output
storage, or execution authority. Its fixed read-only command set is limited to:

- target-pinned `kubectl config current-context` and `kubectl version --output=json`;
- target-pinned `kubectl get` jsonpath metadata for each declared expected
  resource; the selected fields are identity, UID, and resource version only;
- target-pinned `kubectl get secret` metadata templating for each declared
  Secret requirement; it emits only UID, resource version, type, and key names,
  never values or `stringData`;
- target-pinned `helm status --output=json` and `helm history --output=json`
  for every Helm action.

For a Helm release, both reads prove either an explicitly absent/new release or
an existing deployed release with matching revision, chart version, app version,
and status. Only a digest of that selected normalized metadata and the rollback
new/existing state are persisted. Any unknown release state, output containing
secret-like material, malformed or oversized output, source/manifest/envelope
mismatch, tool mismatch, context mismatch, or unavailable command fails closed.
The collector never issues apply, rollout, port-forward, Helm upgrade, Helm repo,
or a generic network command. It is tested only with injected fake runners in
this release; no live collection is authorized by this documentation.

## Phase 3 Library Boundary

`dispatcher.cluster_operation_runner.ClusterOperationRunner` accepts only a
post-commit `ValidatedClusterOperation`, an approved lifecycle record, target
configuration, and an injected tuple-argv command runner. It has no subprocess
fallback, shell-string input, worker exposure, or standalone CLI entry point.
`dispatcher execute` supplies its fixed local adapter only after the dispatcher
has completed the lifecycle bindings above; workers never receive that adapter,
the approval, or its envelopes. The runner hashes both configured executables
before any command.

It permits only these generated argv forms:

- `kubectl apply --server-side --dry-run=server` with the fixed context,
  namespace, field manager, and individually approved manifest files.
- Helm v4 `upgrade --install` for the local chart and approved values, with the
  fixed release/namespace/context, `--wait`, `--rollback-on-failure`, and a
  bounded timeout. Every regular file below the chart directory must also be a
  declared approved source-file digest.
- `kubectl rollout status` for the declared Deployment, StatefulSet, or Job
  readiness probe. It cannot issue `get`, jsonpath, or arbitrary probe commands.

For each approved forward, the runner first persists `PORT_FORWARD_INTENT`, then
uses only `kubectl --context <context> --namespace <namespace> port-forward
--address 127.0.0.1 service/<name> <local>:<remote>` through the
dispatcher-private process adapter. That adapter independently reconstructs the
tuple from the typed action and configured target, rechecks the checksum-bound
kubectl, uses no shell, starts a separate process group, bounds both streams,
and accepts only the exact loopback readiness line without persisting raw output.
It immediately persists the exact action/context/Service/loopback-port identity,
argv SHA-256, PID, process create time, and start time before waiting. It invokes
the dispatcher-private TLS adapter only for a direct loopback handshake without a
client certificate or application data and records only its typed outcome and
evidence digest. `client_certificate_required` or
`unauthenticated_listener_rejected` passes DC8; an unauthenticated handshake,
unexpected listener, or timeout fails. Cleanup re-verifies PID/create-time/argv
and the owned process group before signalling. A mismatch, missing identity,
adapter ambiguity, crash recovery, or cleanup failure enters
`RECONCILIATION_REQUIRED`; it never adopts, reconnects, or signals an unknown
process and never re-applies the operation. Definite failures roll back prior
mutations only through the approved rollback entry; no-mutation forwards fail.

This code has not deployed T2.5 and is not authorized for a real cluster. Actual
invocation remains gated by committed T2.5 operation manifests and every declared
source/chart-lock/values file at the exact source revision; a fresh sanitized
snapshot with their digests, fixed tool hashes, preflight and Tier-1 digests,
resource fingerprints, and per-release rollback state; matching unexpired owner
approval; and an operator-selected `dispatcher execute` real-operation gate.
Workers remain without Kubernetes, Helm, kubeconfig, port-forward, or mutation
authority.
