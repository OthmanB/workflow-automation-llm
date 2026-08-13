"""CLI entry point for the dispatcher.

Usage:
    dispatcher run --config config/projects/<name>.yaml  [--resume]  [--mock]  [--skip-smoke]
    dispatcher preflight --config config/projects/<name>.yaml  [--skip-smoke]
    dispatcher smoke-proof --config <project.yaml> --model <model-id> --output <path>
    dispatcher status --config config/projects/<name>.yaml
    dispatcher start --config config/projects/<name>.yaml --run-record <record.json>
    dispatcher resume --config config/projects/<name>.yaml --run-id <run-id>
    dispatcher recover --config config/projects/<name>.yaml --run-id <run-id>
    dispatcher answer --config config/projects/<name>.yaml --run-id <run-id> --request-id <id> --answer <value>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .baseline import BaselineError
from .config import Config
from .observability import (
    configure_logging,
    export_support_bundle,
    log_event,
    prune_derived_artifacts,
    status_snapshot,
)
from .operation import LiveSmokeProof
from .security import atomic_write_private_text
from .sessions import SUPPORTED_OPENCODE_VERSION, SessionResult
from .sessions import run_session as real_run_session

logger = logging.getLogger("dispatcher.cli")

_LIVE_SMOKE_PROMPT = "Reply with exactly LIVE_SMOKE_OK. Do not use tools or inspect files."
_LIVE_SMOKE_PERMISSION_CONFIG = {
    "permission": {"*": "deny", "read": "allow", "glob": "allow", "grep": "allow"}
}
_LIVE_SMOKE_ENV_MESSAGE = "set DISPATCHER_LIVE_OPENCODE=1 to run the live OpenCode smoke suite"


def _setup_logging(level: str) -> None:
    configure_logging(level)
    # Keep opencode subprocess noise down.
    logging.getLogger("opencode").setLevel(logging.WARNING)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dispatcher",
        description="Automated supervisor → executor → reviewer loop over opencode sessions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    run_parser = sub.add_parser("run", help="start (or resume) a run")
    run_parser.add_argument(
        "--config", required=True,
        help="path to per-project YAML config file",
    )
    run_parser.add_argument(
        "--resume", action="store_true",
        help="resume from saved state",
    )
    run_parser.add_argument(
        "--mock", action="store_true",
        help="use the mock harness instead of real opencode calls",
    )
    run_parser.add_argument(
        "--mock-scenario", default="simple",
        help="mock scenario name (default: simple)",
    )
    run_parser.add_argument(
        "--skip-smoke", action="store_true",
        help="skip the model smoke test (useful after first run)",
    )
    run_parser.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    approval_parser = sub.add_parser(
        "approve-real-operation", help="write an approval record for one exact real-operation step"
    )
    approval_parser.add_argument("--config", required=True)
    approval_parser.add_argument("--run-id", required=True)
    approval_parser.add_argument("--plan", required=True)
    approval_parser.add_argument("--repo-id", required=True)
    approval_parser.add_argument("--approval-ref", required=True)
    approval_parser.add_argument(
        "--permission-digest",
        action="append",
        required=True,
        metavar="ROLE=SHA256",
    )
    approval_parser.add_argument("--output", required=True)

    manifest_parser = sub.add_parser(
        "permission-manifest", help="write the exact role permission manifest for one step"
    )
    manifest_parser.add_argument("--config", required=True)
    manifest_parser.add_argument("--run-id", required=True)
    manifest_parser.add_argument("--plan", required=True)
    manifest_parser.add_argument("--repo-id", required=True)
    manifest_parser.add_argument("--output", required=True)

    execute_parser = sub.add_parser("execute", help="run one explicitly approved real-operation run")
    execute_parser.add_argument("--config", required=True)
    execute_parser.add_argument("--run-id", required=True)
    execute_parser.add_argument("--plan", required=True)
    execute_parser.add_argument("--repo-id", required=True)
    execute_parser.add_argument("--smoke-proof", required=True)
    execute_parser.add_argument("--smoke-model", required=True)
    execute_parser.add_argument(
        "--permission-digest",
        action="append",
        required=True,
        metavar="ROLE=SHA256",
    )
    execute_parser.add_argument("--stall-policy-digest", required=True)
    execute_parser.add_argument("--expected-revision", required=True)
    execute_parser.add_argument("--approval-record", required=True)
    execute_parser.add_argument("--confirm-real-operation", action="store_true")
    execute_parser.add_argument("--max-turns", type=int, default=20)
    execute_parser.add_argument(
        "--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )

    # --- preflight ---
    pf_parser = sub.add_parser("preflight", help="run pre-flight checks only")
    pf_parser.add_argument("--config", required=True)
    pf_parser.add_argument("--skip-smoke", action="store_true")
    pf_parser.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    smoke_parser = sub.add_parser(
        "smoke-proof", help="run the live read-only OpenCode smoke test and write its proof"
    )
    smoke_parser.add_argument("--config", required=True)
    smoke_parser.add_argument("--model", required=True)
    smoke_parser.add_argument("--output", required=True)

    # --- status ---
    st_parser = sub.add_parser("status", help="show current run status")
    st_parser.add_argument("--config", required=True)
    st_parser.add_argument("--run-id")
    st_parser.add_argument("--format", choices=["text", "json"], default="text")
    st_parser.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    # --- recover ---
    start_parser = sub.add_parser("start", help="persist a new approved run without executing it")
    start_parser.add_argument("--config", required=True)
    start_parser.add_argument("--run-record", required=True)
    start_parser.add_argument("--use-approved-baseline", action="store_true")
    start_parser.add_argument(
        "--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )

    resume_parser = sub.add_parser("resume", help="validate a resumable durable run without executing it")
    resume_parser.add_argument("--config", required=True)
    resume_parser.add_argument("--run-id", required=True)
    resume_parser.add_argument(
        "--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )

    # --- recover ---
    recover_parser = sub.add_parser(
        "recover", help="adopt exact interrupted commits and inspect unresolved durable dispatches"
    )
    recover_parser.add_argument("--config", required=True)
    recover_parser.add_argument("--run-id", required=True)
    recover_parser.add_argument(
        "--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )

    cancel_parser = sub.add_parser("cancel", help="request and signal one active worker dispatch")
    cancel_parser.add_argument("--config", required=True)
    cancel_parser.add_argument("--run-id", required=True)
    cancel_parser.add_argument("--dispatch-id", required=True)
    cancel_parser.add_argument("--actor-id", required=True)
    cancel_parser.add_argument(
        "--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )

    # --- answer ---
    answer_parser = sub.add_parser("answer", help="persist an answer for a waiting run")
    answer_parser.add_argument("--config", required=True)
    answer_parser.add_argument("--run-id", required=True)
    answer_parser.add_argument("--request-id", required=True)
    answer_parser.add_argument("--answer", required=True)
    answer_parser.add_argument("--actor-id", required=True)
    answer_parser.add_argument(
        "--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )

    support_parser = sub.add_parser("support", help="export a sanitized derived support bundle")
    support_parser.add_argument("--config", required=True)
    support_parser.add_argument("--run-id", required=True)
    support_parser.add_argument(
        "--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )

    prune_parser = sub.add_parser("prune", help="apply derived-artifact retention")
    prune_parser.add_argument("--config", required=True)
    prune_parser.add_argument("--apply", action="store_true")
    prune_parser.add_argument(
        "--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )

    # --- baseline ---
    baseline_parser = sub.add_parser("baseline", help="inspect or approve historical project baseline")
    baseline_sub = baseline_parser.add_subparsers(dest="baseline_command", required=True)
    baseline_inspect = baseline_sub.add_parser("inspect", help="produce read-only historical observations")
    baseline_inspect.add_argument("--config", required=True)
    baseline_inspect.add_argument("--plan", required=True)
    baseline_inspect.add_argument("--source-markdown")
    baseline_inspect.add_argument("--ownership-map")
    baseline_inspect.add_argument("--output")
    baseline_inspect.add_argument(
        "--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    baseline_approve = baseline_sub.add_parser("approve", help="persist explicit approved baseline decisions")
    baseline_approve.add_argument("--config", required=True)
    baseline_approve.add_argument("--plan", required=True)
    baseline_approve.add_argument("--source-markdown")
    baseline_approve.add_argument("--ownership-map")
    baseline_approve.add_argument("--observation", required=True)
    baseline_approve.add_argument("--decisions", required=True)
    baseline_approve.add_argument("--approval-decision-ref", required=True)
    baseline_approve.add_argument(
        "--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )

    return parser.parse_args(argv)


def _cmd_run(args: argparse.Namespace) -> int:
    if not args.mock:
        logger.error(
            "real OpenCode execution is disabled during remediation Phase 2; "
            "use --mock for proof-of-concept validation"
        )
        return 2

    from .config import load_config
    from .loop import Orchestrator
    from .preflight import run_preflight

    cfg = load_config(args.config)
    _setup_logging(args.log_level or cfg.observability.log_level)

    logger.info("project: %s  profile: %s  root: %s",
                 cfg.project_name,
                 cfg.profile_id,
                 cfg.default_repository.root)

    # Pre-flight.
    from .mock_harness import MockRunner
    run_session = MockRunner(scenario=args.mock_scenario)
    logger.warning("=== MOCK MODE: no real opencode calls ===")

    run_preflight(cfg, cfg.state_dir, skip_smoke=args.skip_smoke,
                  run_session=run_session)

    orch = Orchestrator(cfg, run_session=run_session, resume=args.resume)
    return orch.run()


def _cmd_execute(args: argparse.Namespace) -> int:
    import os

    from . import state as state_mod
    from .config import load_config
    from .execution import SequentialExecutionCoordinator
    from .operation import (
        RealOperationError,
        parse_permission_digest_args,
        validate_real_operation_prerequisites,
    )
    from .preflight import PreflightError, run_preflight
    from .sequential import SequentialWorkflow
    from .sessions import run_session
    from .state_store import StateStoreError

    try:
        cfg = load_config(args.config)
        _setup_logging(args.log_level or cfg.observability.log_level)
        store = state_mod.open_state_store(cfg)
        record, generation = store.load_run(args.run_id)
        details = validate_real_operation_prerequisites(
            config=cfg,
            store=store,
            record=record,
            plan_path=args.plan,
            repo_id=args.repo_id,
            smoke_proof_path=args.smoke_proof,
            smoke_model=args.smoke_model,
            permission_digests=parse_permission_digest_args(args.permission_digest),
            stall_policy_digest=args.stall_policy_digest,
            expected_revision=args.expected_revision,
            approval_record_path=args.approval_record,
            confirm=args.confirm_real_operation,
        )
        if cfg.preflight is None or not cfg.preflight.enabled:
            raise RealOperationError("real operation requires enabled preflight configuration")
        run_preflight(cfg, cfg.state_dir, run_session=run_session, skip_smoke=False)
        approval = details["approval"]
        assert isinstance(approval, dict)
        store.append_audit_event(
            run_id=args.run_id,
            event_id=f"audit-real-operation-{approval['approval_ref']}",
            sequence=record.sequence + 1,
            kind="real_operation_approved",
            correlation_id=args.run_id,
            causation_id=None,
            payload=details,
        )
        workflow = SequentialWorkflow(cfg, store, owner_id=f"real-operation-{os.getpid()}")
        coordinator = SequentialExecutionCoordinator(
            cfg,
            store,
            workflow,
            owner_id=f"real-operation-{os.getpid()}",
        )
        outcome = coordinator.run_to_completion(
            args.run_id,
            expected_generation=generation,
            max_turns=args.max_turns,
        )
    except (RealOperationError, PreflightError, StateStoreError, OSError, ValueError) as exc:
        print(f"execute: FAILED - {exc}", file=sys.stderr)
        return 2
    print(f"execute: completed accepted={outcome.accepted} report={outcome.report_path}")
    return 0 if outcome.accepted else 1


def _cmd_approve_real_operation(args: argparse.Namespace) -> int:
    from . import state as state_mod
    from .config import load_config
    from .operation import RealOperationError, approve_real_operation, parse_permission_digest_args
    from .plan import load_normalized_plan
    from .state_store import StateStoreError

    try:
        config = load_config(args.config)
        _setup_logging(config.observability.log_level)
        store = state_mod.open_state_store(config)
        record, _generation = store.load_run(args.run_id)
        plan = load_normalized_plan(args.plan, config)
        approval = approve_real_operation(
            config=config,
            record=record,
            plan=plan,
            repo_id=args.repo_id,
            approval_ref=args.approval_ref,
            permission_digests=parse_permission_digest_args(args.permission_digest),
        )
        atomic_write_private_text(args.output, approval.model_dump_json(indent=2) + "\n")
    except (RealOperationError, StateStoreError, OSError, ValueError) as exc:
        print(f"approve-real-operation: FAILED - {exc}", file=sys.stderr)
        return 2
    print(
        "approve-real-operation: written "
        f"{args.output} run={approval.run_id} repo={approval.repo_id} step={approval.step_id}"
    )
    return 0


def _cmd_permission_manifest(args: argparse.Namespace) -> int:
    from . import state as state_mod
    from .config import load_config
    from .operation import RealOperationError, compile_role_permission_manifest
    from .plan import load_normalized_plan
    from .state_store import StateStoreError

    try:
        config = load_config(args.config)
        _setup_logging(config.observability.log_level)
        store = state_mod.open_state_store(config)
        record, _generation = store.load_run(args.run_id)
        plan = load_normalized_plan(args.plan, config)
        manifest = compile_role_permission_manifest(
            config=config,
            plan=plan,
            record=record,
            repo_id=args.repo_id,
        )
        atomic_write_private_text(args.output, manifest.model_dump_json(indent=2) + "\n")
    except (RealOperationError, StateStoreError, OSError, ValueError) as exc:
        print(f"permission-manifest: FAILED - {exc}", file=sys.stderr)
        return 2
    print(
        "permission-manifest: written "
        f"{args.output} repo={manifest.repo_id} step={manifest.step_id} "
        f"roles={','.join(manifest.roles)}"
    )
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    from .config import load_config
    from .mock_harness import MockRunner
    from .preflight import run_preflight

    cfg = load_config(args.config)
    _setup_logging(args.log_level or cfg.observability.log_level)

    try:
        run_preflight(
            cfg,
            cfg.state_dir,
            run_session=MockRunner(),
            skip_smoke=args.skip_smoke,
        )
        print("pre-flight: ALL CHECKS PASSED")
        return 0
    except Exception as exc:
        print(f"pre-flight: FAILED — {exc}", file=sys.stderr)
        return 1


def produce_live_smoke_proof(
    config: Config,
    *,
    model: str,
    output: str | Path,
    run_session: Callable[..., SessionResult] = real_run_session,
) -> LiveSmokeProof:
    """Run the isolated live smoke call and persist its sanitized proof."""
    if os.environ.get("DISPATCHER_LIVE_OPENCODE") != "1":
        raise RuntimeError(_LIVE_SMOKE_ENV_MESSAGE)

    with tempfile.TemporaryDirectory(prefix="dispatcher-live-smoke-workdir-") as workdir_name:
        with tempfile.TemporaryDirectory(prefix="dispatcher-live-smoke-state-") as state_dir_name:
            workdir = Path(workdir_name)
            result = run_session(
                prompt=_LIVE_SMOKE_PROMPT,
                model=model,
                variant="",
                session_id=None,
                mode="new",
                workdir=workdir,
                title="dispatcher-read-only-live-smoke",
                auto_approve=False,
                timeout_seconds=30,
                termination_grace_seconds=5,
                max_output_bytes=65_536,
                state_dir=Path(state_dir_name),
                permission_config=_LIVE_SMOKE_PERMISSION_CONFIG,
                snapshot_dirs=[str(workdir)],
            )

    response = result.chat_response.strip()
    proof = LiveSmokeProof(
        proof_version=1,
        config_digest=config.config_digest,
        model=model,
        opencode_version=result.opencode_version,
        passed=(result.exit_code == 0 and "LIVE_SMOKE_OK" in result.chat_response),
        session_id_present=bool(result.session_id),
        workdir_clean=(result.evidence_written == []),
        evidence_written=result.evidence_written,
        response=response,
        completed_at=datetime.now(UTC),
    )
    success = (
        proof.passed
        and proof.session_id_present
        and proof.workdir_clean
        and proof.response == "LIVE_SMOKE_OK"
        and proof.opencode_version == SUPPORTED_OPENCODE_VERSION
    )
    if not success:
        failed_proof = proof if not proof.passed else proof.model_copy(update={"passed": False})
        atomic_write_private_text(output, failed_proof.model_dump_json(indent=2) + "\n")
        raise RuntimeError(
            "live smoke result did not meet expectations: "
            f"exit_code={result.exit_code}, session_id_present={proof.session_id_present}, "
            f"workdir_clean={proof.workdir_clean}, response={proof.response!r}"
        )
    atomic_write_private_text(output, proof.model_dump_json(indent=2) + "\n")
    return proof


def _cmd_smoke_proof(
    args: argparse.Namespace,
    *,
    run_session: Callable[..., SessionResult] = real_run_session,
) -> int:
    if os.environ.get("DISPATCHER_LIVE_OPENCODE") != "1":
        print(f"smoke-proof: {_LIVE_SMOKE_ENV_MESSAGE}", file=sys.stderr)
        return 2

    from .config import load_config

    try:
        config = load_config(args.config)
        _setup_logging(config.observability.log_level)
        proof = produce_live_smoke_proof(
            config,
            model=args.model,
            output=args.output,
            run_session=run_session,
        )
    except Exception as exc:
        print(f"smoke-proof: FAILED - {exc}", file=sys.stderr)
        return 2
    print(f"smoke-proof: written {args.output} passed={proof.passed}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from . import state as state_mod
    from .config import load_config

    cfg = load_config(args.config)
    _setup_logging(args.log_level or cfg.observability.log_level)

    database_path = Path(cfg.state_dir) / "dispatcher.sqlite3"
    if database_path.exists():
        store = state_mod.open_state_store(cfg)
        snapshot = status_snapshot(cfg, store, args.run_id)
        if args.format == "json":
            print(json.dumps(snapshot, indent=2, sort_keys=True))
        else:
            _print_status_text(snapshot)
        run = snapshot["run"]
        log_event(
            logger,
            "status snapshot rendered",
            project_id=cfg.project_id,
            run_id=run["run_id"] if isinstance(run, dict) else None,
        )
        return 0

    s = state_mod.load_state(cfg.state_dir)
    sessions = state_mod.load_sessions(cfg.state_dir)

    print(f"Project: {s.get('project', '?')}")
    print(f"Current step: {s.get('current_step', '(not started)')}")
    print()

    for pool, label in [
        ("supervisor", "Supervisor"),
        ("executors", "Executors"),
        ("reviewers", "Reviewers"),
    ]:
        print(f"{label}:")
        pool_sessions = sessions.get(pool, {})
        for key, info in pool_sessions.items():
            sid = info.get("session_id", "?")
            model = info.get("model", "?")
            print(f"  {key}: {model}  session={sid}")
        if not pool_sessions:
            print("  (none)")
    return 0


def _print_status_text(snapshot: dict[str, object]) -> None:
    run = snapshot["run"]
    print(f"Project: {snapshot['project_id']}")
    if run is None:
        print("Run: (none)")
        return
    assert isinstance(run, dict)
    print(f"Run: {run['run_id']}  state={run['state']}  generation={run['generation']}")
    ready = snapshot["ready_steps"]
    print(f"Ready steps: {', '.join(ready) if isinstance(ready, list) and ready else '(none)'}")
    active = snapshot["active_dispatches"]
    print(f"Active dispatches: {len(active) if isinstance(active, list) else 0}")
    waiting = snapshot["waiting_operator"]
    if isinstance(waiting, dict):
        print(f"Waiting operator: {waiting['kind']} request={waiting['request_id']}")


def _cmd_recover(args: argparse.Namespace) -> int:
    from . import state as state_mod
    from .config import load_config
    from .sequential import SequentialWorkflow, SequentialWorkflowError
    from .state_store import StateStoreError

    cfg = load_config(args.config)
    _setup_logging(args.log_level or cfg.observability.log_level)
    try:
        store = state_mod.open_state_store(cfg)
        items = store.classify_recovery(args.run_id)
        workflow = SequentialWorkflow(cfg, store, owner_id=f"recovery-{os.getpid()}")
        adopted: list[str] = []
        recovered_forwardings: list[str] = []
        recovery_failures: list[str] = []
        candidates = [
            item for item in items if item.disposition == "structured_commit_adoption_required"
        ]
        completed = [item for item in items if item.disposition == "forwarding_required"]
        if candidates or completed:
            recovery_owner = f"recovery-{os.getpid()}"
            store.acquire_run_lease(
                project_id=cfg.project_id,
                run_id=args.run_id,
                owner_id=recovery_owner,
                recovery_approved_by="recover-command",
            )
            try:
                for item in candidates:
                    structured = store.load_structured_git_record(args.run_id, item.dispatch_id)
                    if structured.state != "STAGED":
                        continue
                    try:
                        workflow.adopt_interrupted_structured_commit(args.run_id, item.dispatch_id)
                    except SequentialWorkflowError:
                        continue
                    adopted.append(item.dispatch_id)
                for item in completed:
                    try:
                        workflow.recover_completed_dispatch(args.run_id, item.dispatch_id)
                    except (SequentialWorkflowError, StateStoreError) as exc:
                        recovery_failures.append(f"{item.dispatch_id}: {exc}")
                        continue
                    recovered_forwardings.append(item.dispatch_id)
            finally:
                store.release_leases(
                    owner_id=recovery_owner,
                    resource_keys=[f"run:{cfg.project_id}"],
                )
        items = store.classify_recovery(args.run_id)
        workspace_items = store.classify_workspace_recovery(args.run_id)
    except StateStoreError as exc:
        print(f"recover: FAILED - {exc}", file=sys.stderr)
        return 2
    for dispatch_id in adopted:
        print(f"{dispatch_id}: adopted exact interrupted structured Git commit")
    for dispatch_id in recovered_forwardings:
        print(f"{dispatch_id}: recovered durable forwarding from completed result")
    for failure in recovery_failures:
        print(f"recover: forwarding recovery failed - {failure}", file=sys.stderr)
    if not items and not workspace_items:
        print("recover: no unresolved dispatches or workspaces")
        return 0
    for item in items:
        print(f"{item.dispatch_id}: {item.state.value} -> {item.disposition}: {item.detail}")
    for workspace_item in workspace_items:
        print(
            f"workspace {workspace_item.workspace_group_id}: {workspace_item.state.value} "
            f"-> {workspace_item.disposition}: {workspace_item.detail}"
        )
    needs_reconciliation = any(item.disposition == "operator_reconciliation_required" for item in items)
    needs_reconciliation = needs_reconciliation or any(
        item.disposition == "structured_commit_adoption_required" for item in items
    )
    needs_reconciliation = needs_reconciliation or any(
        item.disposition == "forwarding_required" for item in items
    )
    needs_reconciliation = needs_reconciliation or any(
        item.disposition == "operator_reconciliation_required" for item in workspace_items
    )
    return 1 if needs_reconciliation else 0


def _cmd_support(args: argparse.Namespace) -> int:
    from . import state as state_mod
    from .config import ConfigError, load_config
    from .state_store import StateStoreError

    try:
        cfg = load_config(args.config)
        _setup_logging(args.log_level or cfg.observability.log_level)
        bundle = export_support_bundle(cfg, state_mod.open_state_store(cfg), args.run_id)
    except (ConfigError, StateStoreError, OSError, ValueError) as exc:
        print(f"support: FAILED - {exc}", file=sys.stderr)
        return 2
    log_event(logger, "support bundle exported", project_id=cfg.project_id, run_id=args.run_id)
    print(f"support: exported  {bundle}")
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    from . import state as state_mod
    from .config import ConfigError, load_config
    from .state_store import StateStoreError

    if not args.apply:
        print("prune: use --apply to modify derived artifacts", file=sys.stderr)
        return 2
    try:
        cfg = load_config(args.config)
        _setup_logging(args.log_level or cfg.observability.log_level)
        actions = prune_derived_artifacts(cfg, state_mod.open_state_store(cfg))
    except (ConfigError, StateStoreError, OSError, ValueError) as exc:
        print(f"prune: FAILED - {exc}", file=sys.stderr)
        return 2
    for action in actions:
        print(f"prune: {action.action}  {action.path}")
    log_event(logger, "derived artifact retention applied", project_id=cfg.project_id)
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    from . import state as state_mod
    from .baseline import BaselineError, hydrate_run_from_baseline, validate_approved_baseline
    from .config import load_config
    from .state_store import StateStoreError
    from .workflow import RunRecord, RunStatus

    cfg = load_config(args.config)
    _setup_logging(args.log_level or cfg.observability.log_level)
    try:
        record = RunRecord.model_validate_json(Path(args.run_record).read_text(encoding="utf-8"))
        if record.project_id != cfg.project_id or record.config_digest != cfg.config_digest:
            raise StateStoreError("run record project or config digest does not match --config")
        if record.state is not RunStatus.NEW:
            raise StateStoreError("start requires a NEW run record; use resume, recover, or explicit archive")
        store = state_mod.open_state_store(cfg)
        if args.use_approved_baseline:
            approval = validate_approved_baseline(plan=record.plan, config=cfg, store=store)
            record = hydrate_run_from_baseline(record, approval)
        generation = store.create_run(record)
    except (BaselineError, OSError, ValueError, StateStoreError) as exc:
        print(f"start: FAILED - {exc}", file=sys.stderr)
        return 2
    print(f"start: persisted  run={record.run_id} generation={generation}")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    from . import state as state_mod
    from .config import load_config
    from .state_store import StateStoreError

    cfg = load_config(args.config)
    _setup_logging(args.log_level or cfg.observability.log_level)
    try:
        record, generation = state_mod.open_state_store(cfg).resume_run(
            project_id=cfg.project_id,
            run_id=args.run_id,
        )
    except StateStoreError as exc:
        print(f"resume: FAILED - {exc}", file=sys.stderr)
        return 2
    print(f"resume: validated  run={record.run_id} state={record.state.value} generation={generation}")
    return 0


def _cmd_answer(args: argparse.Namespace) -> int:
    from . import state as state_mod
    from .config import load_config
    from .state_store import StateStoreError
    from .workspaces import WorkspaceCoordinator, WorkspaceError

    cfg = load_config(args.config)
    _setup_logging(args.log_level or cfg.observability.log_level)
    try:
        store = state_mod.open_state_store(cfg)
        record, generation = store.load_run(args.run_id)
        request = record.operator_request
        if (
            request is not None
            and request.request_id == args.request_id
            and request.kind == "workspace_reconciliation"
            and args.answer == "reconcile"
        ):
            cleanup = WorkspaceCoordinator(cfg, store).cleanup(
                run_id=args.run_id,
                expected_generation=generation,
                workspace_group_id=request.context_ref,
                force=False,
            )
            generation = cleanup.generation
        updated, new_generation = store.answer_operator_request(
            run_id=args.run_id,
            expected_generation=generation,
            request_id=args.request_id,
            answer=args.answer,
            actor_id=args.actor_id,
        )
    except (StateStoreError, WorkspaceError) as exc:
        print(f"answer: FAILED - {exc}", file=sys.stderr)
        return 2
    print(f"answer: recorded  run={updated.run_id} state={updated.state.value} generation={new_generation}")
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    from . import state as state_mod
    from .config import load_config
    from .sessions import OpenCodeAdapterError, cancel_process_group
    from .state_store import StateStoreError

    cfg = load_config(args.config)
    _setup_logging(args.log_level or cfg.observability.log_level)
    try:
        store = state_mod.open_state_store(cfg)
        record, generation = store.load_run(args.run_id)
        _updated, _generation, process_id, process_host, process_create_time = (
            store.request_dispatch_cancellation(
                run_id=args.run_id,
                expected_generation=generation,
                dispatch_id=args.dispatch_id,
                actor_id=args.actor_id,
            )
        )
        stopped = cancel_process_group(
            process_id,
            process_host,
            cfg.execution.termination_grace_seconds,
            process_create_time,
        )
    except (OpenCodeAdapterError, OSError, StateStoreError, ValueError) as exc:
        print(f"cancel: FAILED - {exc}", file=sys.stderr)
        return 2
    print(f"cancel: recorded  run={record.run_id} dispatch={args.dispatch_id} process_stopped={stopped}")
    return 0


def _reject_duplicate_decisions_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BaselineError(f"duplicate JSON key in decisions file: {key}")
        result[key] = value
    return result


def _cmd_baseline(args: argparse.Namespace) -> int:
    from . import state as state_mod
    from .baseline import (
        BaselineDecision,
        BaselineError,
        BaselineObservation,
        approve_baseline,
        inspect_baseline,
    )
    from .config import load_config
    from .importers import import_tier2_markdown, load_ownership_map
    from .plan import load_normalized_plan
    from .security import atomic_write_private_text
    from .state_store import StateStoreError

    cfg = load_config(args.config)
    _setup_logging(args.log_level or cfg.observability.log_level)
    try:
        if bool(args.source_markdown) != bool(args.ownership_map):
            raise BaselineError("--source-markdown and --ownership-map must be supplied together")
        if args.source_markdown:
            ownership = load_ownership_map(args.ownership_map, cfg)
            plan = import_tier2_markdown(args.source_markdown, args.plan, cfg, ownership)
        else:
            plan = load_normalized_plan(args.plan, cfg)
        if args.baseline_command == "inspect":
            observation = inspect_baseline(plan, cfg)
            payload = observation.model_dump_json(indent=2) + "\n"
            if args.output:
                atomic_write_private_text(args.output, payload)
                print(f"baseline: observation written to {args.output}")
            else:
                print(payload, end="")
            return 0
        observation = BaselineObservation.model_validate_json(Path(args.observation).read_text(encoding="utf-8"))
        raw_decisions = json.loads(
            Path(args.decisions).read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_decisions_keys,
        )
        if isinstance(raw_decisions, dict):
            raw_decisions = raw_decisions.get("decisions")
        if not isinstance(raw_decisions, list):
            raise BaselineError("baseline decisions file must be a JSON list or an object with decisions")
        approve_baseline(
            observation,
            decisions=tuple(BaselineDecision.model_validate(item) for item in raw_decisions),
            plan=plan,
            config=cfg,
            store=state_mod.open_state_store(cfg),
            approval_decision_ref=args.approval_decision_ref,
        )
        print(f"baseline: approved  project={cfg.project_id} plan={plan.plan_id}")
        return 0
    except (BaselineError, OSError, StateStoreError, ValueError) as exc:
        print(f"baseline: FAILED - {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    elif args.command == "approve-real-operation":
        return _cmd_approve_real_operation(args)
    elif args.command == "permission-manifest":
        return _cmd_permission_manifest(args)
    elif args.command == "execute":
        return _cmd_execute(args)
    elif args.command == "preflight":
        return _cmd_preflight(args)
    elif args.command == "smoke-proof":
        return _cmd_smoke_proof(args)
    elif args.command == "status":
        return _cmd_status(args)
    elif args.command == "start":
        return _cmd_start(args)
    elif args.command == "resume":
        return _cmd_resume(args)
    elif args.command == "recover":
        return _cmd_recover(args)
    elif args.command == "answer":
        return _cmd_answer(args)
    elif args.command == "cancel":
        return _cmd_cancel(args)
    elif args.command == "support":
        return _cmd_support(args)
    elif args.command == "prune":
        return _cmd_prune(args)
    elif args.command == "baseline":
        return _cmd_baseline(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
