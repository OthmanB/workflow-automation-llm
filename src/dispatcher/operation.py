"""Fail-closed checks for the separately approved real-operation command."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping, Sequence

from pydantic import Field

from .baseline import BaselineError, validate_approved_baseline
from .config import Config, ContractModel, Identifier, MCPToolName
from .mcp import compile_role_mcp_servers, resolve_role_mcp_tools
from .permissions import (
    PermissionError,
    compile_effective_policy,
    generate_opencode_config,
    role_scoped_authorized_actions,
)
from .plan import (
    NormalizedPlan,
    PlanError,
    PlanStep,
    Sha256,
    load_normalized_plan,
    validate_plan_approval,
)
from .policy import compile_run_policy
from .repository import inspect_repository
from .state_store import StateStore
from .verification import VerificationError, verification_backend
from .workflow import RunRecord, RunStatus, StepStatus


class RealOperationError(RuntimeError):
    """A real-operation prerequisite is absent or does not match exactly."""


class LiveSmokeProof(ContractModel):
    """Sanitized proof produced by the read-only real OpenCode smoke test."""

    proof_version: int = Field(strict=True, ge=1, le=1)
    config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    model: str = Field(min_length=1)
    opencode_version: str = Field(pattern=r"^1\.18\.18$")
    passed: bool
    session_id_present: bool
    workdir_clean: bool
    evidence_written: list[str]
    response: str = Field(min_length=1, max_length=200)
    completed_at: datetime


class RolePermissionEntry(ContractModel):
    """One exact compiled role permission approved for a real operation."""

    role_kind: Literal["supervisor", "executor", "reviewer"]
    authorized_actions: tuple[Identifier, ...]
    mcp_tools: tuple[MCPToolName, ...]
    digest: Sha256


class RolePermissionManifest(ContractModel):
    """Exact role set and permission digests for one executable step."""

    manifest_version: Literal[2]
    repo_id: Identifier
    step_id: Identifier
    roles: dict[Identifier, RolePermissionEntry]
    structured_git: "StructuredGitCapability"


class StructuredGitCapability(ContractModel):
    """Dispatcher-side commit authority bound independently of child permissions."""

    capability_version: Literal[1]
    safety_policy_version: Literal[1]
    repo_id: Identifier
    step_id: Identifier
    commit_policy: Literal["required", "prohibited"]
    commit_authorized: bool
    writable_paths: tuple[str, ...]
    evidence_paths: tuple[str, ...]
    message_format: Literal["dispatcher: <step_id> attempt <n>"]
    identity_digest: Sha256
    digest: Sha256


class RealOperationApproval(ContractModel):
    """Operator decision bound to one exact real-operation launch target."""

    approval_ref: Identifier
    project_id: Identifier
    config_digest: Sha256
    plan_digest: Sha256
    run_id: Identifier
    repo_id: Identifier
    step_id: Identifier
    permission_manifest: RolePermissionManifest
    decided_at: datetime


LIVE_SMOKE_PROOF_MAX_AGE_SECONDS = 1800


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


def load_real_operation_approval(path: str | Path) -> RealOperationApproval:
    """Load the operator's exact real-operation approval record."""
    approval_path = Path(path).expanduser().resolve()
    try:
        return RealOperationApproval.model_validate_json(approval_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RealOperationError(f"invalid real operation approval: {exc}") from exc


def first_pending_executable_step(
    plan: NormalizedPlan,
    record: RunRecord,
) -> PlanStep | None:
    """Return the first plan step whose durable state is pending or ready."""
    return next(
        (
            step
            for step in plan.steps
            if record.steps[step.step_id].state in {StepStatus.PENDING, StepStatus.READY}
        ),
        None,
    )


def compile_role_permission_manifest(
    *,
    config: Config,
    plan: NormalizedPlan,
    record: RunRecord,
    repo_id: str,
) -> RolePermissionManifest:
    """Compile every role permission that may participate in the next step."""
    pending_step = first_pending_executable_step(plan, record)
    if pending_step is None or pending_step.repo_id != repo_id:
        raise RealOperationError("requested repository is not the first pending executable step")
    obligation = compile_run_policy(config, plan).review_obligations[pending_step.step_id]
    role_keys = [
        *config.model.roles.supervisor,
        *config.model.roles.executors,
        *obligation.reviewer_role_keys,
    ]
    if len(role_keys) != len(set(role_keys)):
        raise RealOperationError("real-operation permission roles must be unique")
    entries: dict[str, RolePermissionEntry] = {}
    for role_key in sorted(role_keys, key=lambda key: (config.role_kind(key), key)):
        role_kind = config.role_kind(role_key)
        try:
            authorized_actions = role_scoped_authorized_actions(
                pending_step.authorization.authorized_actions,
                role_kind,
            )
            permission = generate_opencode_config(
                compile_effective_policy(
                    config,
                    repo_id=repo_id,
                    role_key=role_key,
                    dispatch_authorized_actions=authorized_actions,
                ),
                mcp_servers=compile_role_mcp_servers(config, role_key),
            )
        except (PermissionError, ValueError) as exc:
            raise RealOperationError(
                f"cannot compile real-operation permission for role {role_key}: {exc}"
            ) from exc
        entries[role_key] = RolePermissionEntry(
            role_kind=role_kind,
            authorized_actions=authorized_actions,
            mcp_tools=resolve_role_mcp_tools(config, role_key),
            digest=digest_json(permission),
        )
    repository = config.repository(repo_id)
    evidence_paths = tuple(
        (PurePosixPath(root) / PurePosixPath(requirement.relative_path)).as_posix()
        for requirement in pending_step.evidence_requirements
        for root in repository.evidence_roots
    )
    identity_digest = digest_json(
        {
            "author_name": config.execution.structured_git.author_name,
            "author_email": config.execution.structured_git.author_email,
            "committer_name": config.execution.structured_git.committer_name,
            "committer_email": config.execution.structured_git.committer_email,
        }
    )
    capability_payload = {
        "capability_version": config.execution.structured_git.capability_version,
        "safety_policy_version": 1,
        "repo_id": repo_id,
        "step_id": pending_step.step_id,
        "commit_policy": repository.commit_policy,
        "commit_authorized": "commit" in pending_step.authorization.authorized_actions,
        "writable_paths": list(pending_step.authorization.writable_paths),
        "evidence_paths": list(evidence_paths),
        "message_format": "dispatcher: <step_id> attempt <n>",
        "identity_digest": identity_digest,
    }
    structured_git = StructuredGitCapability(
        capability_version=config.execution.structured_git.capability_version,
        safety_policy_version=1,
        repo_id=repo_id,
        step_id=pending_step.step_id,
        commit_policy=repository.commit_policy,
        commit_authorized="commit" in pending_step.authorization.authorized_actions,
        writable_paths=pending_step.authorization.writable_paths,
        evidence_paths=evidence_paths,
        message_format="dispatcher: <step_id> attempt <n>",
        identity_digest=identity_digest,
        digest=digest_json(capability_payload),
    )
    return RolePermissionManifest(
        manifest_version=2,
        repo_id=repo_id,
        step_id=pending_step.step_id,
        roles=entries,
        structured_git=structured_git,
    )


def parse_permission_digest_args(values: Sequence[str]) -> dict[str, str]:
    """Parse repeated ROLE=SHA256 arguments without accepting duplicates."""
    parsed: dict[str, str] = {}
    for value in values:
        role_key, separator, digest = value.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,127}", role_key):
            raise RealOperationError(
                f"invalid permission digest argument {value!r}; expected ROLE=SHA256"
            )
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise RealOperationError(f"permission digest for role {role_key} is not a SHA-256 value")
        if role_key in parsed:
            raise RealOperationError(f"duplicate permission digest for role {role_key}")
        parsed[role_key] = digest
    if not parsed:
        raise RealOperationError("at least one role permission digest is required")
    return parsed


def _validate_permission_digests(
    manifest: RolePermissionManifest,
    supplied: Mapping[str, str],
) -> None:
    expected = {role_key: entry.digest for role_key, entry in manifest.roles.items()}
    if set(supplied) != set(expected):
        missing = ", ".join(sorted(set(expected) - set(supplied))) or "none"
        extra = ", ".join(sorted(set(supplied) - set(expected))) or "none"
        raise RealOperationError(
            f"permission digest role set does not match; missing: {missing}; extra: {extra}"
        )
    mismatched = sorted(role_key for role_key in expected if supplied[role_key] != expected[role_key])
    if mismatched:
        raise RealOperationError(
            "permission digest does not match compiled role permission: " + ", ".join(mismatched)
        )


def approve_real_operation(
    *,
    config: Config,
    record: RunRecord,
    plan: NormalizedPlan,
    repo_id: str,
    approval_ref: str,
    permission_digests: Mapping[str, str],
) -> RealOperationApproval:
    """Bind an operator decision to the current run's first executable step."""
    pending_step = first_pending_executable_step(plan, record)
    if pending_step is None or pending_step.repo_id != repo_id:
        raise RealOperationError("requested repository is not the first pending executable step")
    permission_manifest = compile_role_permission_manifest(
        config=config,
        plan=plan,
        record=record,
        repo_id=repo_id,
    )
    _validate_permission_digests(permission_manifest, permission_digests)
    return RealOperationApproval(
        approval_ref=approval_ref,
        project_id=config.project_id,
        config_digest=record.config_digest,
        plan_digest=record.plan_digest,
        run_id=record.run_id,
        repo_id=repo_id,
        step_id=pending_step.step_id,
        permission_manifest=permission_manifest,
        decided_at=datetime.now(UTC),
    )


def validate_real_operation_prerequisites(
    *,
    config: Config,
    store: StateStore,
    record: RunRecord,
    plan_path: str | Path,
    repo_id: str,
    smoke_proof_path: str | Path,
    smoke_model: str,
    permission_digests: Mapping[str, str],
    stall_policy_digest: str,
    expected_revision: str,
    approval_record_path: str | Path,
    confirm: bool,
) -> dict[str, Any]:
    """Perform every pre-launch real-operation check without starting OpenCode."""
    if config.model.schema_version != 2 or config.execution.mode != "real_operation":
        raise RealOperationError("config must be schema v2 with execution.mode real_operation")
    if not confirm:
        raise RealOperationError("--confirm-real-operation is required")
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
    if snapshot.revision != expected_revision:
        raise RealOperationError("repository is not at the expected revision")
    pending_step = first_pending_executable_step(current_plan, record)
    if pending_step is None or pending_step.repo_id != repo_id:
        raise RealOperationError("requested repository is not the first pending executable step")
    approval = load_real_operation_approval(approval_record_path)
    if approval.project_id != config.project_id:
        raise RealOperationError("real operation approval record does not match the current project")
    if approval.config_digest != config.config_digest:
        raise RealOperationError("real operation approval record does not match the current config")
    if approval.plan_digest != current_plan.plan_digest:
        raise RealOperationError("real operation approval record does not match the current plan")
    if approval.run_id != record.run_id:
        raise RealOperationError("real operation approval record does not match the current run")
    if approval.repo_id != repo_id:
        raise RealOperationError("real operation approval record does not match the current repository")
    if approval.step_id != pending_step.step_id:
        raise RealOperationError("real operation approval record does not match the current step")
    expected_permission_manifest = compile_role_permission_manifest(
        config=config,
        plan=current_plan,
        record=record,
        repo_id=repo_id,
    )
    if approval.permission_manifest.model_dump(mode="json") != expected_permission_manifest.model_dump(
        mode="json"
    ):
        raise RealOperationError(
            "real operation approval record does not match the current role permission manifest"
        )
    smoke = load_live_smoke_proof(smoke_proof_path)
    if not smoke.passed or not smoke.session_id_present or not smoke.workdir_clean or smoke.evidence_written:
        raise RealOperationError("live smoke proof does not prove a clean read-only run")
    if smoke.config_digest != config.config_digest or smoke.model != smoke_model:
        raise RealOperationError("live smoke proof does not match the exact config or model")
    if smoke.response != "LIVE_SMOKE_OK":
        raise RealOperationError("live smoke proof response is not the required fixed response")
    if smoke.completed_at.tzinfo is None or smoke.completed_at.utcoffset() is None:
        raise RealOperationError("live smoke proof completed_at must be timezone-aware")
    if (datetime.now(UTC) - smoke.completed_at).total_seconds() > LIVE_SMOKE_PROOF_MAX_AGE_SECONDS:
        raise RealOperationError("live smoke proof is stale; rerun the live smoke test")
    _validate_permission_digests(expected_permission_manifest, permission_digests)
    expected_stall_digest = digest_json(config.execution.stall_policy.model_dump(mode="json"))
    if stall_policy_digest != expected_stall_digest:
        raise RealOperationError("stall-policy digest does not match current YAML")
    try:
        backend = verification_backend(config)
    except VerificationError as exc:
        raise RealOperationError(f"production verification isolation is unavailable: {exc}") from exc
    if not backend.production_ready:
        raise RealOperationError(
            f"verification backend {backend.name} is development-only and cannot launch a real operation"
        )
    return {
        "repo_id": repo_id,
        "revision": snapshot.revision,
        "branch": snapshot.branch,
        "step_id": pending_step.step_id,
        "permission_manifest": expected_permission_manifest.model_dump(mode="json"),
        "verification_backend": backend.name,
        "stall_policy_digest": expected_stall_digest,
        "approval": approval.model_dump(mode="json"),
    }
