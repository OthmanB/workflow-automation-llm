# Dispatcher Phase 5 Execution Report

**Execution date:** 2026-08-10
**Plan:**
[`markdown/plans/dispatcher-remediation-plan-2026-08-09.md`](../plans/dispatcher-remediation-plan-2026-08-09.md)
**Scope:** Sequential multi-repository routing, immutable repository coordinates,
and content-addressed evidence manifests.

## Status

Phase 5 is implemented only through the validated sequential coordinator and
deterministic fake OpenCode subprocesses. Real model-backed OpenCode execution
remains disabled.

## Implemented

- The normalized plan owns `repo_id`. An optional supervisor `repo_id` is only
  checked as an equality assertion and cannot select a path or policy.
- Every dispatch re-inspects its registered Git worktree before launch: exact
  top-level root, configured branch, remote name and URL, clean baseline, head
  SHA, and worktree identity.
- `DispatchIntent` and the private dispatch payload persist base branch/SHA,
  working-tree identity, expected remote, and complete before/after snapshots.
- `commit_policy: required` accepts only a clean committed head; `prohibited`
  requires a dispatcher-computed patch SHA-256 that includes tracked and
  untracked content.
- Review verdicts are accepted only while the inspected repository still matches
  the exact executor revision or patch and artifact hashes in the review target.
- Repository snapshots contain evidence-root entries with relative path, type,
  size, mode, mtime, and SHA-256. Required evidence is checked against the
  inspected manifest, not worker-supplied hashes alone.
- Modified, created, deleted, renamed, untracked, symlink, external-root, and
  out-of-writable-root changes are detected. Symlinks and unexpected writes halt
  acceptance.
- `external_roots` are explicit config-relative watch directories outside all
  registered repositories. A changed external root cannot be attributed to the
  dispatch.
- The final run report lists durable repository coordinates and inspected
  manifest hashes plus content hashes for every reported artifact.
- SQLite schema migration 3 adds durable before/after repository snapshot
  columns without changing historical Phase 4 payload rows.

## Verification

The Phase 5 tests use only temporary repositories and the pinned fake OpenCode
CLI. They cover exact two-repository routing, wrong repository assertions,
repository branch/head movement, uncommitted work, revision-bound review,
create/modify/delete/rename manifests, symlink escape, unexpected writes,
external-root writes, concurrent writes, and state migration.

```text
PYTHONPATH=src pytest
159 passed

ruff check src tests
All checks passed!

mypy src
Success: no issues found in 25 source files

.venv/bin/python -m build
Successfully built dispatcher-0.1.0.tar.gz and dispatcher-0.1.0-py3-none-any.whl

clean wheel
dispatcher --help passed outside the checkout
packaged bootstrap template matched the source byte-for-byte
```

## Remaining Boundaries

- Live OpenCode allow/ask/deny enforcement remains a Phase 2 open item.
- Model-backed execution remains disabled despite the deterministic Phase 5
  fake-CLI gate.
- Repository worktrees are single-writer sequential roots guarded by durable
  repository leases; bounded parallel worktree scheduling remains Phase 7.
- The private historical baseline remains deferred.
