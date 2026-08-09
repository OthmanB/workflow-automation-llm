# SQLite Authoritative State Store

**Date:** 2026-08-10 00:00 UTC
**Status:** Accepted for dispatcher Phase 3

## Decision

The dispatcher uses a private SQLite database at
`<state.directory>/dispatcher.sqlite3` as its authoritative runtime state
store. `state.json`, `sessions.json`, `run-record.json`, Markdown transcripts,
and JSONL audit output are derived compatibility or human-readable artifacts;
they are not recovery authorities.

## Rationale

SQLite is in the Python standard library, supplies atomic multi-table
transactions, durable schema migrations, foreign keys, and cross-process file
locking without introducing a service dependency. It captures runs, plans,
steps, dispatches, sessions, reviews, artifacts, operator decisions, leases,
and audit events under one transaction boundary.

## Durability Assumptions

- The state directory must be a local filesystem with atomic rename, `fsync`,
  and POSIX-style advisory locking semantics.
- Network filesystems, cloud-sync folders, and removable media are unsupported
  for active dispatcher state.
- The store enables SQLite WAL mode, `synchronous=FULL`, and foreign keys.
- Database, WAL, SHM, transcript, audit, and report files are owner-only.
- Database corruption or a newer schema version stops execution with recovery
  guidance; no automatic repair is attempted.

## Consequences

- A generation-checked snapshot updates run, plan, step, dispatch, and session
  rows together or not at all.
- Run and repository leases prevent concurrent dispatchers. A stale lease needs
  an explicit operator approval reference before it can be replaced.
- A `RUNNING` dispatch is never retried automatically because external work may
  have completed before process loss.
- Real OpenCode dispatch remains disabled until Phase 4 uses this authority for
  every launch and transition.
