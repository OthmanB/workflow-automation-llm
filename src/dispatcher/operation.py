"""Fail-closed checks for the separately approved real-operation command."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping, Sequence

from pydantic import Field, model_validator

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


class RealOperationScopeManifest(ContractModel):
    """Ordered set of step permissions approved for one autonomous run segment."""

    scope_version: Literal[1]
    steps: tuple[RolePermissionManifest, ...]
    digest: Sha256


class RepositoryRevisionExpectation(ContractModel):
    """One repository revision the operator approved for an autonomous scope."""

    repo_id: Identifier
    revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")


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
    """Operator decision bound to an exact autonomous real-operation scope."""

    approval_ref: Identifier
    project_id: Identifier
    config_digest: Sha256
    plan_digest: Sha256
    run_id: Identifier
    repo_id: Identifier
    step_id: Identifier
    permission_manifest: RolePermissionManifest
    scope_manifest: RealOperationScopeManifest | None = None
    repository_revisions: tuple[RepositoryRevisionExpectation, ...] | None = None
    decided_at: datetime

    @model_validator(mode="after")
    def validate_repository_revisions(self) -> "RealOperationApproval":
        if self.repository_revisions is not None:
            repo_ids = [item.repo_id for item in self.repository_revisions]
            if not repo_ids or len(repo_ids) != len(set(repo_ids)):
                raise ValueError("repository_revisions must contain each repository exactly once")
        return self


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


def autonomous_execution_scope(
    plan: NormalizedPlan,
    record: RunRecord,
) -> tuple[PlanStep, ...]:
    """Return the contiguous pending or ready scope from the next executable step."""
    pending_step = first_pending_executable_step(plan, record)
    if pending_step is None:
        return ()
    return _contiguous_autonomous_scope(
        plan,
        record,
        start_step_id=pending_step.step_id,
        retain_completed=False,
    )


def _contiguous_autonomous_scope(
    plan: NormalizedPlan,
    record: RunRecord,
    *,
    start_step_id: str,
    retain_completed: bool,
) -> tuple[PlanStep, ...]:
    """Build a scope until the first unresolved step makes later work unreachable."""
    try:
        start_index = next(
            index for index, step in enumerate(plan.steps) if step.step_id == start_step_id
        )
    except StopIteration as exc:
        raise RealOperationError("real operation approval record references an unknown step") from exc
    completed = {
        step_id
        for step_id, step in record.steps.items()
        if step.state in {StepStatus.ACCEPTED, StepStatus.WAIVED}
    }
    executable = {StepStatus.PENDING, StepStatus.READY}
    if retain_completed:
        executable |= {StepStatus.REVIEW_REQUIRED, StepStatus.ACCEPTED, StepStatus.WAIVED}
    scoped: list[PlanStep] = []
    scoped_ids: set[str] = set()
    for step in plan.steps[start_index:]:
        current = record.steps[step.step_id]
        if current.state not in executable:
            break
        if not current.operator_gate_resolved:
            break
        if any(dependency_id not in completed | scoped_ids for dependency_id in step.depends_on):
            break
        if any(
            artifact.producer_step_id is not None
            and artifact.producer_step_id not in completed | scoped_ids
            for artifact in step.required_inputs
        ):
            break
        scoped.append(step)
        scoped_ids.add(step.step_id)
    return tuple(scoped)


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
    return _compile_role_permission_manifest_for_step(config=config, plan=plan, step=pending_step)


def _compile_role_permission_manifest_for_step(
    *,
    config: Config,
    plan: NormalizedPlan,
    step: PlanStep,
) -> RolePermissionManifest:
    """Compile the canonical role and structured-Git manifest for one plan step."""
    repo_id = step.repo_id
    obligation = compile_run_policy(config, plan).review_obligations[step.step_id]
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
                step.authorization.authorized_actions,
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
        for requirement in step.evidence_requirements
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
        "step_id": step.step_id,
        "commit_policy": repository.commit_policy,
        "commit_authorized": "commit" in step.authorization.authorized_actions,
        "writable_paths": list(step.authorization.writable_paths),
        "evidence_paths": list(evidence_paths),
        "message_format": "dispatcher: <step_id> attempt <n>",
        "identity_digest": identity_digest,
    }
    structured_git = StructuredGitCapability(
        capability_version=config.execution.structured_git.capability_version,
        safety_policy_version=1,
        repo_id=repo_id,
        step_id=step.step_id,
        commit_policy=repository.commit_policy,
        commit_authorized="commit" in step.authorization.authorized_actions,
        writable_paths=step.authorization.writable_paths,
        evidence_paths=evidence_paths,
        message_format="dispatcher: <step_id> attempt <n>",
        identity_digest=identity_digest,
        digest=digest_json(capability_payload),
    )
    return RolePermissionManifest(
        manifest_version=2,
        repo_id=repo_id,
        step_id=step.step_id,
        roles=entries,
        structured_git=structured_git,
    )


def compile_real_operation_scope_manifest(
    *,
    config: Config,
    plan: NormalizedPlan,
    record: RunRecord,
    repo_id: str,
) -> RealOperationScopeManifest:
    """Compile the complete ordered scope reachable from the current launch step."""
    pending_step = first_pending_executable_step(plan, record)
    if pending_step is None or pending_step.repo_id != repo_id:
        raise RealOperationError("requested repository is not the first pending executable step")
    scoped_steps = autonomous_execution_scope(plan, record)
    if not scoped_steps or scoped_steps[0].step_id != pending_step.step_id:
        raise RealOperationError(
            "the first pending executable step cannot run without an unresolved dependency or operator gate"
        )
    return _scope_manifest_for_steps(config=config, plan=plan, steps=scoped_steps)


def _scope_manifest_for_steps(
    *,
    config: Config,
    plan: NormalizedPlan,
    steps: tuple[PlanStep, ...],
) -> RealOperationScopeManifest:
    """Compile ordered immutable role and structured-Git permissions for known scope steps."""
    manifests = tuple(
        _compile_role_permission_manifest_for_step(config=config, plan=plan, step=step)
        for step in steps
    )
    payload = {
        "scope_version": 1,
        "steps": [manifest.model_dump(mode="json") for manifest in manifests],
    }
    return RealOperationScopeManifest(
        scope_version=1,
        steps=manifests,
        digest=digest_json(payload),
    )


def _compile_approved_real_operation_scope_manifest(
    *,
    config: Config,
    plan: NormalizedPlan,
    record: RunRecord,
    repo_id: str,
    start_step_id: str,
) -> RealOperationScopeManifest:
    """Rebuild the originally approved scope across a completed or recoverable prefix."""
    scoped_steps = _contiguous_autonomous_scope(
        plan,
        record,
        start_step_id=start_step_id,
        retain_completed=True,
    )
    if not scoped_steps or scoped_steps[0].step_id != start_step_id:
        raise RealOperationError(
            "the approved real-operation scope cannot resume without an unresolved dependency or operator gate"
        )
    if scoped_steps[0].repo_id != repo_id:
        raise RealOperationError("real operation approval record does not match the current repository")
    return _scope_manifest_for_steps(config=config, plan=plan, steps=scoped_steps)


def _first_remaining_scope_manifest(
    scope_manifest: RealOperationScopeManifest,
    record: RunRecord,
) -> RolePermissionManifest:
    """Return the first unfinished approved step without discarding the completed prefix."""
    for manifest in scope_manifest.steps:
        state = record.steps[manifest.step_id].state
        if state not in {StepStatus.ACCEPTED, StepStatus.WAIVED}:
            if state not in {StepStatus.PENDING, StepStatus.READY, StepStatus.REVIEW_REQUIRED}:
                raise RealOperationError(
                    f"approved real-operation scope cannot resume from step {manifest.step_id} "
                    f"in state {state.value}"
                )
            return manifest
    raise RealOperationError("approved real-operation scope has no unfinished step to execute")


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


def _validate_scope_manifest_digest(
    scope_manifest: RealOperationScopeManifest,
    supplied: str | None,
) -> None:
    if supplied is None:
        if len(scope_manifest.steps) > 1:
            raise RealOperationError(
                "multi-step real-operation approval requires --scope-manifest-digest from "
                "permission-manifest after reviewing every scoped step"
            )
        return
    if not re.fullmatch(r"[a-f0-9]{64}", supplied):
        raise RealOperationError("scope manifest digest is not a SHA-256 value")
    if supplied != scope_manifest.digest:
        raise RealOperationError(
            "scope manifest digest does not match the current full scope; rerun permission-manifest "
            "and review every scoped step"
        )


def parse_repository_revision_args(values: Sequence[str]) -> dict[str, str]:
    """Parse repeated REPOSITORY=REVISION arguments without accepting duplicates."""
    parsed: dict[str, str] = {}
    for value in values:
        repo_id, separator, revision = value.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,127}", repo_id):
            raise RealOperationError(
                f"invalid repository revision argument {value!r}; expected REPOSITORY=REVISION"
            )
        if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
            raise RealOperationError(
                f"repository revision for {repo_id} is not a full lowercase Git object ID"
            )
        if repo_id in parsed:
            raise RealOperationError(f"duplicate repository revision for {repo_id}")
        parsed[repo_id] = revision
    return parsed


def _scope_repository_ids(scope_manifest: RealOperationScopeManifest) -> tuple[str, ...]:
    """Return repositories once each in their first appearance in the ordered scope."""
    return tuple(dict.fromkeys(manifest.repo_id for manifest in scope_manifest.steps))


def _ordered_repository_revisions(
    scope_manifest: RealOperationScopeManifest,
    supplied: Mapping[str, str],
) -> tuple[RepositoryRevisionExpectation, ...]:
    expected_repo_ids = _scope_repository_ids(scope_manifest)
    supplied_repo_ids = set(supplied)
    if supplied_repo_ids != set(expected_repo_ids):
        missing = ", ".join(repo_id for repo_id in expected_repo_ids if repo_id not in supplied_repo_ids) or "none"
        extra = ", ".join(sorted(supplied_repo_ids - set(expected_repo_ids))) or "none"
        raise RealOperationError(
            f"repository revision expectations do not match approval scope; missing: {missing}; extra: {extra}"
        )
    try:
        return tuple(
            RepositoryRevisionExpectation(repo_id=repo_id, revision=supplied[repo_id])
            for repo_id in expected_repo_ids
        )
    except ValueError as exc:
        raise RealOperationError(f"invalid repository revision expectation: {exc}") from exc


def _inspect_expected_scope_repositories(
    config: Config,
    expectations: Sequence[RepositoryRevisionExpectation],
) -> dict[str, Any]:
    """Require every repository bound to an approval scope to remain clean and pinned."""
    snapshots: dict[str, Any] = {}
    for expectation in expectations:
        try:
            snapshot = inspect_repository(config, expectation.repo_id, require_clean=True)
        except Exception as exc:
            raise RealOperationError(
                f"repository {expectation.repo_id} is not clean and inspectable for the approved scope: {exc}"
            ) from exc
        if not snapshot.clean:
            raise RealOperationError(
                f"repository {expectation.repo_id} is not clean for the approved scope"
            )
        if snapshot.revision != expectation.revision:
            raise RealOperationError(
                f"repository is not at the expected revision: {expectation.repo_id}"
            )
        snapshots[expectation.repo_id] = snapshot
    return snapshots


def _approval_repository_revisions(
    *,
    config: Config,
    scope_manifest: RealOperationScopeManifest,
    supplied: Mapping[str, str] | None,
) -> tuple[RepositoryRevisionExpectation, ...] | None:
    """Bind supplied revisions while retaining the legacy single-repository command shape."""
    repo_ids = _scope_repository_ids(scope_manifest)
    if supplied:
        expectations = _ordered_repository_revisions(scope_manifest, supplied)
        _inspect_expected_scope_repositories(config, expectations)
        return expectations
    if len(repo_ids) != 1:
        raise RealOperationError(
            "multi-repository real-operation approval requires complete "
            "--expected-repository-revision input for every scoped repository"
        )
    return None


def _execution_repository_revisions(
    *,
    scope_manifest: RealOperationScopeManifest,
    approval: RealOperationApproval,
    expected_revision: str | None,
    supplied: Mapping[str, str] | None,
) -> tuple[RepositoryRevisionExpectation, ...]:
    """Resolve legacy single-repository input while failing closed for multi-repository scopes."""
    repo_ids = _scope_repository_ids(scope_manifest)
    supplied = supplied or {}
    if len(repo_ids) == 1:
        repo_id = repo_ids[0]
        if supplied:
            expectations = _ordered_repository_revisions(scope_manifest, supplied)
            if expected_revision is not None and expectations[0].revision != expected_revision:
                raise RealOperationError("legacy expected revision conflicts with repository revision expectation")
        elif expected_revision is not None:
            expectations = _ordered_repository_revisions(scope_manifest, {repo_id: expected_revision})
        else:
            raise RealOperationError("single-repository real operation requires --expected-revision")
    else:
        if expected_revision is not None or not supplied:
            raise RealOperationError(
                "multi-repository real operation requires complete --expected-repository-revision input"
            )
        expectations = _ordered_repository_revisions(scope_manifest, supplied)
    if approval.repository_revisions is None:
        if len(repo_ids) != 1:
            raise RealOperationError(
                "legacy real-operation approval cannot authorize a multi-repository scope"
            )
    elif approval.repository_revisions != expectations:
        raise RealOperationError(
            "repository revision expectations do not match the real-operation approval record"
        )
    return expectations


def approve_real_operation(
    *,
    config: Config,
    record: RunRecord,
    plan: NormalizedPlan,
    repo_id: str,
    approval_ref: str,
    permission_digests: Mapping[str, str],
    scope_manifest_digest: str | None = None,
    expected_repository_revisions: Mapping[str, str] | None = None,
) -> RealOperationApproval:
    """Bind an operator decision to every reachable autonomous plan step."""
    scope_manifest = compile_real_operation_scope_manifest(
        config=config,
        plan=plan,
        record=record,
        repo_id=repo_id,
    )
    permission_manifest = scope_manifest.steps[0]
    _validate_permission_digests(permission_manifest, permission_digests)
    _validate_scope_manifest_digest(scope_manifest, scope_manifest_digest)
    repository_revisions = _approval_repository_revisions(
        config=config,
        scope_manifest=scope_manifest,
        supplied=expected_repository_revisions,
    )
    return RealOperationApproval(
        approval_ref=approval_ref,
        project_id=config.project_id,
        config_digest=record.config_digest,
        plan_digest=record.plan_digest,
        run_id=record.run_id,
        repo_id=repo_id,
        step_id=permission_manifest.step_id,
        permission_manifest=permission_manifest,
        scope_manifest=scope_manifest,
        repository_revisions=repository_revisions,
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
    expected_revision: str | None,
    approval_record_path: str | Path,
    confirm: bool,
    expected_repository_revisions: Mapping[str, str] | None = None,
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
    if approval.step_id not in {step.step_id for step in current_plan.steps}:
        raise RealOperationError("real operation approval record does not match the current step")
    expected_scope_manifest = _compile_approved_real_operation_scope_manifest(
        config=config,
        plan=current_plan,
        record=record,
        repo_id=repo_id,
        start_step_id=approval.step_id,
    )
    if approval.permission_manifest.model_dump(mode="json") != expected_scope_manifest.steps[0].model_dump(
        mode="json"
    ):
        raise RealOperationError(
            "real operation approval record does not match the current role permission manifest"
        )
    if approval.scope_manifest is None:
        if len(expected_scope_manifest.steps) > 1:
            raise RealOperationError(
                "legacy single-step real operation approval cannot authorize the current multi-step "
                "scope; rerun permission-manifest and approve the full scope"
            )
    elif approval.scope_manifest.model_dump(mode="json") != expected_scope_manifest.model_dump(
        mode="json"
    ):
        raise RealOperationError(
            "real operation approval record does not match the current complete approval scope"
        )
    expected_permission_manifest = _first_remaining_scope_manifest(expected_scope_manifest, record)
    repository_revisions = _execution_repository_revisions(
        scope_manifest=expected_scope_manifest,
        approval=approval,
        expected_revision=expected_revision,
        supplied=expected_repository_revisions,
    )
    snapshots = _inspect_expected_scope_repositories(config, repository_revisions)
    snapshot = snapshots[repo_id]
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
        "step_id": expected_permission_manifest.step_id,
        "permission_manifest": expected_permission_manifest.model_dump(mode="json"),
        "scope_manifest": expected_scope_manifest.model_dump(mode="json"),
        "repository_revisions": [
            expectation.model_dump(mode="json") for expectation in repository_revisions
        ],
        "verification_backend": backend.name,
        "stall_policy_digest": expected_stall_digest,
        "approval": approval.model_dump(mode="json"),
    }
