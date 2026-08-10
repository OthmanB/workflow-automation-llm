"""Fail-closed checks for the separately approved real-operation command."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from .baseline import BaselineError, validate_approved_baseline
from .config import Config, ContractModel
from .permissions import compile_effective_policy, generate_opencode_config
from .plan import PlanError, load_normalized_plan, validate_plan_approval
from .repository import inspect_repository
from .state_store import StateStore
from .workflow import RunRecord, RunStatus, StepStatus


class RealOperationError(RuntimeError):
    """A real-operation prerequisite is absent or does not match exactly."""


class LiveSmokeProof(ContractModel):
    """Sanitized proof produced by the read-only real OpenCode smoke test."""

    proof_version: int = Field(strict=True, ge=1, le=1)
    config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    model: str = Field(min_length=1)
    opencode_version: str = Field(pattern=r"^1\.18\.11$")
    passed: bool
    session_id_present: bool
    workdir_clean: bool
    evidence_written: list[str]
    response: str = Field(min_length=1, max_length=200)
    completed_at: datetime


def digest_json(value: object) -> str:
    """Hash a canonical JSON value for exact command approval matching."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_live_smoke_proof(path: str | Path) -> LiveSmokeProof:
    """Load sanitized smoke evidence without accepting raw logs or credentials."""
    proof_path = Path(path).expanduser().resolve()
    try:
        return LiveSmokeProof.model_validate_json(proof_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RealOperationError(f"invalid live smoke proof: {exc}") from exc


def validate_real_operation_prerequisites(
    *,
    config: Config,
    store: StateStore,
    record: RunRecord,
    plan_path: str | Path,
    repo_id: str,
    smoke_proof_path: str | Path,
    smoke_model: str,
    permission_digest: str,
    stall_policy_digest: str,
    approval_ref: str,
    confirm: bool,
) -> dict[str, Any]:
    """Perform every pre-launch real-operation check without starting OpenCode."""
    if config.model.schema_version != 2 or config.execution.mode != "real_operation":
        raise RealOperationError("config must be schema v2 with execution.mode real_operation")
    if not confirm:
        raise RealOperationError("--confirm-real-operation is required")
    if not approval_ref:
        raise RealOperationError("operator approval reference is required")
    try:
        current_plan = load_normalized_plan(plan_path, config)
        validate_plan_approval(current_plan, record.plan_approval)
    except (PlanError, OSError, ValueError) as exc:
        raise RealOperationError(f"plan approval does not match current plan: {exc}") from exc
    if record.plan_digest != current_plan.plan_digest or record.config_digest != config.config_digest:
        raise RealOperationError("run does not match the exact current plan and config digests")
    if record.state not in {RunStatus.NEW, RunStatus.READY, RunStatus.RUNNING}:
        raise RealOperationError(f"run is not executable from state {record.state.value}")
    if repo_id not in config.model.repositories:
        raise RealOperationError(f"repository is not registered: {repo_id}")
    if store.classify_recovery(record.run_id) or store.classify_workspace_recovery(record.run_id):
        raise RealOperationError("run has unresolved recovery work")
    try:
        validate_approved_baseline(plan=current_plan, config=config, store=store)
    except BaselineError as exc:
        raise RealOperationError(f"approved baseline is not current: {exc}") from exc
    snapshot = inspect_repository(config, repo_id, require_clean=True)
    pending_step = next(
        (
            step for step in current_plan.steps
            if record.steps[step.step_id].state in {StepStatus.PENDING, StepStatus.READY}
        ),
        None,
    )
    if pending_step is None or pending_step.repo_id != repo_id:
        raise RealOperationError("requested repository is not the first pending executable step")
    smoke = load_live_smoke_proof(smoke_proof_path)
    if not smoke.passed or not smoke.session_id_present or not smoke.workdir_clean or smoke.evidence_written:
        raise RealOperationError("live smoke proof does not prove a clean read-only run")
    if smoke.config_digest != config.config_digest or smoke.model != smoke_model:
        raise RealOperationError("live smoke proof does not match the exact config or model")
    if smoke.response != "LIVE_SMOKE_OK":
        raise RealOperationError("live smoke proof response is not the required fixed response")
    role_key = next(iter(config.model.roles.executors))
    permission = generate_opencode_config(
        compile_effective_policy(
            config,
            repo_id=repo_id,
            role_key=role_key,
            dispatch_authorized_actions=pending_step.authorization.authorized_actions,
        )
    )
    expected_permission_digest = digest_json(permission)
    if permission_digest != expected_permission_digest:
        raise RealOperationError("permission digest does not match the first executable step")
    expected_stall_digest = digest_json(config.execution.stall_policy.model_dump(mode="json"))
    if stall_policy_digest != expected_stall_digest:
        raise RealOperationError("stall-policy digest does not match current YAML")
    return {
        "repo_id": repo_id,
        "revision": snapshot.revision,
        "branch": snapshot.branch,
        "step_id": pending_step.step_id,
        "permission_digest": expected_permission_digest,
        "stall_policy_digest": expected_stall_digest,
    }
