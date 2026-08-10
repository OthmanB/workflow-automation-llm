"""Strict schema-v1 project configuration and environment validation."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class ConfigError(ValueError):
    """A schema or environment validation failure in project configuration."""


class ContractModel(BaseModel):
    """Base for immutable, strict, closed configuration contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")]
ModelName = Annotated[str, Field(pattern=r"^[^/\s]+/[^/\s]+$")]
PermissionDecision = Literal["allow", "ask", "deny"]


class ProjectDefinition(ContractModel):
    """Project identity independent of a repository working directory."""

    project_id: Identifier
    name: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(min_length=1, max_length=2000)]


class SourcesDefinition(ContractModel):
    """Authoritative project-level source directories and selected documents."""

    specifications_dir: str
    plans_dir: str
    plan_files: list[Annotated[str, Field(min_length=1)]]
    roles_files: list[Annotated[str, Field(min_length=1)]]

    @field_validator("plan_files", "roles_files")
    @classmethod
    def relative_source_files(cls, values: list[str]) -> list[str]:
        for value in values:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("source file paths must be relative and cannot contain '..'")
        return values


class StateDefinition(ContractModel):
    """Persistent state location, explicitly configured for every project."""

    directory: str
    lease_heartbeat_seconds: Annotated[int, Field(ge=1, le=86_400)]
    lease_stale_after_seconds: Annotated[int, Field(ge=2, le=604_800)]

    @model_validator(mode="after")
    def stale_lease_threshold_exceeds_heartbeat(self) -> Self:
        if self.lease_stale_after_seconds <= self.lease_heartbeat_seconds:
            raise ValueError("lease_stale_after_seconds must exceed lease_heartbeat_seconds")
        return self


class RemoteDefinition(ContractModel):
    """Exact Git remote expected for one registered repository."""

    name: Identifier
    url: Annotated[str, Field(min_length=1)]


class RepositoryDefinition(ContractModel):
    """One repository registered to a project under a stable identifier."""

    root: str
    expected_remote: RemoteDefinition
    default_branch: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,300}$")]
    evidence_roots: list[Annotated[str, Field(min_length=1)]]
    writable_roots: list[Annotated[str, Field(min_length=1)]]
    external_roots: list[Annotated[str, Field(min_length=1)]]
    commit_policy: Literal["required", "prohibited"]
    permission_policy: Identifier
    allow_shared_writable_roots: bool

    @field_validator("evidence_roots", "writable_roots")
    @classmethod
    def nonempty_relative_roots(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError("must contain at least one root")
        for value in values:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("roots must be relative and cannot contain '..'")
        return values


class RoleDefinition(ContractModel):
    """Configured model role and its named permission policy."""

    model: ModelName
    variant: Annotated[str, Field(min_length=1, max_length=100)]
    display: Annotated[str, Field(min_length=1, max_length=200)]
    permission_policy: Identifier


class RolesDefinition(ContractModel):
    """One supervisor and independently addressable executor/reviewer pools."""

    supervisor: dict[Identifier, RoleDefinition]
    executors: dict[Identifier, RoleDefinition]
    reviewers: dict[Identifier, RoleDefinition]

    @model_validator(mode="after")
    def validate_role_keys(self) -> Self:
        if len(self.supervisor) != 1:
            raise ValueError("roles.supervisor must define exactly one role")
        if not self.executors:
            raise ValueError("roles.executors must define at least one role")

        keys = [*self.supervisor, *self.executors, *self.reviewers]
        if len(keys) != len(set(keys)):
            raise ValueError("role keys must be unique across supervisor, executors, and reviewers")
        return self


class ProfileSelection(ContractModel):
    """One profile from an explicit, versioned profiles document."""

    profiles_file: str
    profile_id: Identifier


class ProfileDefinition(ContractModel):
    """Explicit review obligations selected for a run in Phase 6."""

    review_schedule: Literal["on_failure", "critical", "always"]
    multi_review: Literal["off", "on_critical_only", "on_every_review"]
    reviewer_role_keys: list[Identifier]
    required_acceptances: Annotated[int, Field(ge=1, le=20)]

    @model_validator(mode="after")
    def validate_reviewer_obligation(self) -> Self:
        if len(self.reviewer_role_keys) != len(set(self.reviewer_role_keys)):
            raise ValueError("reviewer_role_keys must not contain duplicates")
        if self.required_acceptances > len(self.reviewer_role_keys):
            raise ValueError("required_acceptances cannot exceed reviewer_role_keys")
        if self.multi_review == "off" and self.required_acceptances != 1:
            raise ValueError("multi_review off profiles require exactly one acceptance")
        if self.multi_review != "off" and self.required_acceptances < 2:
            raise ValueError("multi_review profiles require at least two acceptances")
        return self


class ProfilesDocument(ContractModel):
    """Schema-v1 document containing named review profiles."""

    schema_version: Literal[1]
    profiles: dict[Identifier, ProfileDefinition]
    default: Identifier

    @model_validator(mode="after")
    def selected_default_exists(self) -> Self:
        if self.default not in self.profiles:
            raise ValueError("profiles.default must reference profiles")
        return self


class ConcurrencyDefinition(ContractModel):
    """Explicit bounded-parallel controls for independently dispatchable work."""

    max_active_dispatches: Annotated[int, Field(ge=1, le=100)]
    max_batch_size: Annotated[int, Field(ge=1, le=100)]
    role_capacities: dict[Identifier, Annotated[int, Field(ge=1, le=100)]]
    failure_mode: Literal["wait_for_started"]
    same_repository_mode: Literal["serialized", "worktree_barrier"]
    worktree_root: Annotated[str, Field(min_length=1)]
    worktree_branch_prefix: Annotated[
        str,
        Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,80}$"),
    ]

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.max_batch_size > self.max_active_dispatches:
            raise ValueError("max_batch_size cannot exceed max_active_dispatches")
        if self.max_active_dispatches > sum(self.role_capacities.values()):
            raise ValueError("max_active_dispatches cannot exceed total role capacity")
        return self


class ExecutionDefinition(ContractModel):
    """All active execution controls for the mock-only Phase 7 runtime."""

    mode: Literal["mock_workflow_test", "real_operation"]
    protocol_version: Literal[1]
    scheduling: Literal["sequential", "bounded_parallel"]
    concurrency: ConcurrencyDefinition
    default_repo_id: Identifier
    timeout_seconds: Annotated[int, Field(ge=1, le=86_400)]
    termination_grace_seconds: Annotated[int, Field(ge=1, le=3_600)]
    max_output_bytes: Annotated[int, Field(ge=1_024, le=104_857_600)]
    max_rounds_per_step: Annotated[int, Field(ge=1, le=100)]
    stall_policy: StallPolicyDefinition
    halt_mode: Literal["ask_on_ambiguity", "full_auto"]
    underspec_mode: Literal["ask", "auto"]
    response_template: Annotated[str, Field(min_length=1, max_length=20_000)]


class ReviewPolicyDefinition(ContractModel):
    """Project-wide review constraints combined with selected profile policy."""

    mandatory_review: bool
    critical_risk_tags: list[Identifier]
    allow_operator_waiver: bool

    @field_validator("critical_risk_tags")
    @classmethod
    def critical_risk_tags_are_unique(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("critical_risk_tags must not contain duplicates")
        return values


class BudgetDefinition(ContractModel):
    """Measured resource limits enforced by the sequential coordinator."""

    enabled: bool
    max_run_cost_usd: Annotated[float, Field(ge=0)]
    max_step_cost_usd: Annotated[float, Field(ge=0)]
    max_context_tokens: Annotated[int, Field(ge=1)]
    on_limit: Literal["halt", "ask"]


class StallPolicyDefinition(ContractModel):
    """Provider/process interruption retry policy, separate from project budgets."""

    maximum_retries_per_step: Annotated[int, Field(ge=0, le=20)]
    cooldown_seconds: Annotated[int, Field(ge=0, le=86_400)]
    on_exhausted: Literal["ask", "halt"]


class RetentionDefinition(ContractModel):
    """Bounded retention for derived, non-authoritative observability artifacts."""

    mode: Literal["archive", "delete"]
    archive_directory: Annotated[str, Field(min_length=1)]
    max_transcripts_per_run: Annotated[int, Field(ge=0, le=100_000)]
    max_reports: Annotated[int, Field(ge=0, le=100_000)]
    max_audit_exports: Annotated[int, Field(ge=0, le=100_000)]
    max_support_bundles: Annotated[int, Field(ge=0, le=100_000)]
    max_archived_artifacts: Annotated[int, Field(ge=0, le=1_000_000)]


class ObservabilityDefinition(ContractModel):
    """Explicit structured logging and derived-artifact retention controls."""

    log_format: Literal["json"]
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    retention: RetentionDefinition


class PermissionPolicy(ContractModel):
    """Named semantic permission rules compiled to exact OpenCode patterns."""

    default: PermissionDecision
    actions: dict[Identifier, PermissionDecision]

    @field_validator("actions")
    @classmethod
    def actions_cannot_be_empty_keys(cls, values: dict[str, PermissionDecision]) -> dict[str, PermissionDecision]:
        if any(not action for action in values):
            raise ValueError("actions cannot contain empty keys")
        return values


class RoleClassPolicies(ContractModel):
    """Required policy layer for each role class."""

    supervisor: Identifier
    executor: Identifier
    reviewer: Identifier


class PermissionPoliciesDefinition(ContractModel):
    """Explicit policy layers and named rules with no implicit fallback."""

    global_policy: Identifier
    project_policy: Identifier
    role_class_policies: RoleClassPolicies
    policies: dict[Identifier, PermissionPolicy]


class EvidencePolicy(ContractModel):
    """Explicit evidence integrity requirements used by normalized plans."""

    hash_algorithm: Literal["sha256"]
    require_content_hashes: bool
    immutable: bool
    allow_unexpected_writes: bool


class PreflightDefinition(ContractModel):
    """Optional preflight policy; absence means preflight is disabled."""

    enabled: bool
    models_smoke_test: bool
    smoke_prompt: Annotated[str, Field(min_length=1, max_length=1000)]
    credentials: list[Identifier]
    require_git_remote: bool
    disk_space_min_mb: Annotated[int, Field(ge=0, le=1_000_000)]


class ProjectConfigModel(ContractModel):
    """Versioned, project-neutral configuration schema for Phase 1."""

    schema_version: Literal[2]
    project: ProjectDefinition
    sources: SourcesDefinition
    state: StateDefinition
    repositories: dict[Identifier, RepositoryDefinition]
    roles: RolesDefinition
    profile: ProfileSelection
    execution: ExecutionDefinition
    review_policy: ReviewPolicyDefinition
    budget: BudgetDefinition
    observability: ObservabilityDefinition
    permission_policies: PermissionPoliciesDefinition
    evidence: EvidencePolicy
    preflight: PreflightDefinition | None = None

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if not self.repositories:
            raise ValueError("repositories must define at least one repository")
        if self.execution.default_repo_id not in self.repositories:
            raise ValueError("execution.default_repo_id must reference repositories")

        policy_keys = set(self.permission_policies.policies)
        if not policy_keys:
            raise ValueError("permission_policies must define at least one policy")
        for repo_id, repository in self.repositories.items():
            if repository.permission_policy not in policy_keys:
                raise ValueError(
                    f"repositories.{repo_id}.permission_policy must reference permission_policies"
                )
        for role_key, role in self.all_roles().items():
            if role.permission_policy not in policy_keys:
                raise ValueError(
                    f"roles.{role_key}.permission_policy must reference permission_policies"
                )
        worker_roles = set(self.roles.executors) | set(self.roles.reviewers)
        capacities = self.execution.concurrency.role_capacities
        if set(capacities) != worker_roles:
            raise ValueError("execution.concurrency.role_capacities must exactly match worker roles")
        if self.execution.scheduling == "sequential" and self.execution.concurrency.max_active_dispatches != 1:
            raise ValueError("sequential scheduling requires max_active_dispatches to be 1")
        for layer_name, policy_id in {
            "global_policy": self.permission_policies.global_policy,
            "project_policy": self.permission_policies.project_policy,
            **self.permission_policies.role_class_policies.model_dump(),
        }.items():
            if policy_id not in policy_keys:
                raise ValueError(
                    f"permission_policies.{layer_name} must reference permission_policies.policies"
                )
        return self

    def all_roles(self) -> dict[str, RoleDefinition]:
        """Return all role definitions keyed by their globally unique role key."""
        return {
            **self.roles.supervisor,
            **self.roles.executors,
            **self.roles.reviewers,
        }


class Config:
    """Loaded project configuration with resolved paths and verified references."""

    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        raw = _load_yaml_mapping(self.config_path, "project config")
        resolved = _resolve_config_paths(raw, self.config_path.parent)
        try:
            self.model = ProjectConfigModel.model_validate(resolved)
        except ValidationError as exc:
            raise ConfigError(_format_validation_error("project config", exc)) from exc
        self.profiles = self._load_profiles()
        self._validate_environment()

    @property
    def project_id(self) -> str:
        return self.model.project.project_id

    @property
    def project_name(self) -> str:
        return self.model.project.name

    @property
    def state_dir(self) -> str:
        return self.model.state.directory

    @property
    def lease_heartbeat_seconds(self) -> int:
        return self.model.state.lease_heartbeat_seconds

    @property
    def lease_stale_after_seconds(self) -> int:
        return self.model.state.lease_stale_after_seconds

    @property
    def execution(self) -> ExecutionDefinition:
        return self.model.execution

    @property
    def observability(self) -> ObservabilityDefinition:
        return self.model.observability

    @property
    def profile_id(self) -> str:
        return self.model.profile.profile_id

    @property
    def preflight(self) -> PreflightDefinition | None:
        return self.model.preflight

    @property
    def supervisor_key(self) -> str:
        return next(iter(self.model.roles.supervisor))

    @property
    def supervisor(self) -> RoleDefinition:
        return self.model.roles.supervisor[self.supervisor_key]

    @property
    def default_repository_id(self) -> str:
        return self.model.execution.default_repo_id

    @property
    def default_repository(self) -> RepositoryDefinition:
        return self.repository(self.default_repository_id)

    @property
    def evidence_dirs(self) -> list[Path]:
        return self.repository_evidence_dirs(self.default_repository_id)

    @property
    def config_digest(self) -> str:
        payload = self.model.model_dump(mode="json")
        return _sha256_json(payload)

    def repository(self, repo_id: str) -> RepositoryDefinition:
        try:
            return self.model.repositories[repo_id]
        except KeyError as exc:
            raise ConfigError(f"unknown repository id: {repo_id}") from exc

    def repository_root(self, repo_id: str) -> Path:
        return Path(self.repository(repo_id).root)

    def repository_evidence_dirs(self, repo_id: str) -> list[Path]:
        repository = self.repository(repo_id)
        root = Path(repository.root)
        return [root / relative_root for relative_root in repository.evidence_roots]

    def repository_external_dirs(self, repo_id: str) -> list[Path]:
        """Return resolved directories watched for writes outside the repository."""
        return [Path(path) for path in self.repository(repo_id).external_roots]

    def role(self, role_key: str) -> RoleDefinition:
        try:
            return self.model.all_roles()[role_key]
        except KeyError as exc:
            raise ConfigError(f"unknown role key: {role_key}") from exc

    def role_kind(self, role_key: str) -> Literal["supervisor", "executor", "reviewer"]:
        if role_key in self.model.roles.supervisor:
            return "supervisor"
        if role_key in self.model.roles.executors:
            return "executor"
        if role_key in self.model.roles.reviewers:
            return "reviewer"
        raise ConfigError(f"unknown role key: {role_key}")

    def role_display(self, role_key: str) -> str:
        return self.role(role_key).display

    def supervisor_model(self) -> str:
        return self.supervisor.model

    def supervisor_variant(self) -> str:
        return self.supervisor.variant

    def timeout_minutes(self) -> int:
        return max(1, (self.execution.timeout_seconds + 59) // 60)

    def permission_policy_layers(
        self,
        *,
        repo_id: str,
        role_key: str,
    ) -> tuple[PermissionPolicy, ...]:
        """Return policy layers in exact precedence order for one dispatch."""
        policy_set = self.model.permission_policies
        repository = self.repository(repo_id)
        role = self.role(role_key)
        role_class_policy = getattr(policy_set.role_class_policies, self.role_kind(role_key))
        policy_ids = (
            policy_set.global_policy,
            policy_set.project_policy,
            repository.permission_policy,
            role_class_policy,
            role.permission_policy,
        )
        return tuple(policy_set.policies[policy_id] for policy_id in policy_ids)

    def model_json_schema(self) -> dict[str, Any]:
        """Return the machine-readable schema generated from the runtime model."""
        return ProjectConfigModel.model_json_schema()

    def _load_profiles(self) -> ProfilesDocument:
        path = Path(self.model.profile.profiles_file)
        raw = _load_yaml_mapping(path, "profiles config")
        try:
            profiles = ProfilesDocument.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(_format_validation_error("profiles config", exc)) from exc
        if self.model.profile.profile_id not in profiles.profiles:
            raise ConfigError(
                "profile.profile_id must reference profiles config profiles: "
                f"{self.model.profile.profile_id}"
            )
        for profile_id, profile in profiles.profiles.items():
            for role_key in profile.reviewer_role_keys:
                if self.role_kind(role_key) != "reviewer":
                    raise ConfigError(f"profiles.{profile_id}.reviewer_role_keys must reference reviewers")
        return profiles

    @property
    def profile_digest(self) -> str:
        """Return the immutable selected-profile policy hash for a run record."""
        payload = {
            "profile_id": self.profile_id,
            "profile": self.profiles.profiles[self.profile_id].model_dump(mode="json"),
            "review_policy": self.model.review_policy.model_dump(mode="json"),
        }
        return _sha256_json(payload)

    def _validate_environment(self) -> None:
        _require_directory(Path(self.model.sources.specifications_dir), "sources.specifications_dir")
        plans_dir = Path(self.model.sources.plans_dir)
        _require_directory(plans_dir, "sources.plans_dir")
        for relative_path in [*self.model.sources.plan_files, *self.model.sources.roles_files]:
            path = plans_dir / relative_path
            if not path.is_file():
                raise ConfigError(f"sources file does not exist: {path}")

        _require_file(Path(self.model.profile.profiles_file), "profile.profiles_file")
        _require_writable_parent(Path(self.model.state.directory), "state.directory")
        _require_writable_parent(
            Path(self.model.observability.retention.archive_directory),
            "observability.retention.archive_directory",
        )
        _require_writable_parent(
            Path(self.model.execution.concurrency.worktree_root),
            "execution.concurrency.worktree_root",
        )

        resolved_roots: dict[str, Path] = {}
        writable_roots: list[tuple[str, Path, bool]] = []
        external_roots: list[tuple[str, Path]] = []
        for repo_id, repository in self.model.repositories.items():
            root = Path(repository.root)
            _require_directory(root, f"repositories.{repo_id}.root")
            resolved_roots[repo_id] = root
            _validate_git_remote(repo_id, root, repository.expected_remote)

            for field_name, relative_roots in (
                ("evidence_roots", repository.evidence_roots),
                ("writable_roots", repository.writable_roots),
            ):
                for relative_root in relative_roots:
                    resolved = _resolve_within_root(root, relative_root)
                    _require_directory(resolved, f"repositories.{repo_id}.{field_name}")
                    if field_name == "writable_roots":
                        writable_roots.append(
                            (repo_id, resolved, repository.allow_shared_writable_roots)
                        )
            for external_root in repository.external_roots:
                resolved_external = Path(external_root)
                _require_directory(
                    resolved_external,
                    f"repositories.{repo_id}.external_roots",
                )
                external_roots.append((repo_id, resolved_external))

        if len(set(resolved_roots.values())) != len(resolved_roots):
            raise ConfigError("repository roots must be unique after path resolution")
        _validate_writable_root_overlaps(writable_roots)
        _validate_external_root_overlaps(resolved_roots, external_roots)


def load_config(config_path: str | Path) -> Config:
    """Load schema-v1 configuration or raise ``ConfigError`` before preflight."""
    return Config(config_path)


def _load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"{label} file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid {label} YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{label} must be a YAML mapping")
    return data


def _resolve_config_paths(raw: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Resolve only declared config-relative path fields before strict validation."""
    resolved = copy.deepcopy(raw)

    def resolve_at(mapping: dict[str, Any], key: str) -> None:
        value = mapping.get(key)
        if isinstance(value, str):
            path = Path(value).expanduser()
            mapping[key] = str((path if path.is_absolute() else base_dir / path).resolve())

    if isinstance(resolved.get("sources"), dict):
        sources = resolved["sources"]
        resolve_at(sources, "specifications_dir")
        resolve_at(sources, "plans_dir")
    if isinstance(resolved.get("state"), dict):
        resolve_at(resolved["state"], "directory")
    if isinstance(resolved.get("observability"), dict):
        retention = resolved["observability"].get("retention")
        if isinstance(retention, dict):
            resolve_at(retention, "archive_directory")
    if isinstance(resolved.get("execution"), dict):
        concurrency = resolved["execution"].get("concurrency")
        if isinstance(concurrency, dict):
            resolve_at(concurrency, "worktree_root")
    if isinstance(resolved.get("profile"), dict):
        resolve_at(resolved["profile"], "profiles_file")
    if isinstance(resolved.get("repositories"), dict):
        for repository in resolved["repositories"].values():
            if isinstance(repository, dict):
                resolve_at(repository, "root")
                external_roots = repository.get("external_roots")
                if isinstance(external_roots, list):
                    repository["external_roots"] = [
                        str((Path(value) if Path(value).is_absolute() else base_dir / value).resolve())
                        if isinstance(value, str)
                        else value
                        for value in external_roots
                    ]
    return resolved


def _format_validation_error(label: str, exc: ValidationError) -> str:
    errors = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        errors.append(f"{location}: {error['msg']}")
    return f"invalid {label}: " + "; ".join(errors)


def _require_directory(path: Path, field: str) -> None:
    if not path.is_dir():
        raise ConfigError(f"{field} must be an existing directory: {path}")


def _require_file(path: Path, field: str) -> None:
    if not path.is_file():
        raise ConfigError(f"{field} must be an existing file: {path}")


def _require_writable_parent(path: Path, field: str) -> None:
    probe = path if path.exists() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.exists() or not os.access(probe, os.W_OK):
        raise ConfigError(f"{field} parent is not writable: {path}")


def _resolve_within_root(root: Path, relative_root: str) -> Path:
    resolved = (root / relative_root).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ConfigError(f"path escapes repository root: {relative_root}") from exc
    return resolved


def _validate_git_remote(repo_id: str, root: Path, expected: RemoteDefinition) -> None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", expected.name],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(
            f"repositories.{repo_id}.expected_remote could not resolve {expected.name}: {exc}"
        ) from exc
    actual = result.stdout.strip()
    if actual != expected.url:
        raise ConfigError(
            f"repositories.{repo_id}.expected_remote.url mismatch: expected {expected.url!r}, "
            f"found {actual!r}"
        )


def _validate_writable_root_overlaps(entries: list[tuple[str, Path, bool]]) -> None:
    for index, (left_id, left_path, left_allowed) in enumerate(entries):
        for right_id, right_path, right_allowed in entries[index + 1 :]:
            if left_id == right_id:
                continue
            overlaps = left_path.is_relative_to(right_path) or right_path.is_relative_to(left_path)
            if overlaps and not (left_allowed and right_allowed):
                raise ConfigError(
                    "repository writable roots overlap without explicit permission: "
                    f"{left_id}={left_path}, {right_id}={right_path}"
                )


def _validate_external_root_overlaps(
    repository_roots: dict[str, Path],
    external_roots: list[tuple[str, Path]],
) -> None:
    seen: set[Path] = set()
    for repo_id, external_root in external_roots:
        if external_root in seen:
            raise ConfigError(f"external roots must be unique after path resolution: {external_root}")
        seen.add(external_root)
        for registered_id, repository_root in repository_roots.items():
            if external_root.is_relative_to(repository_root) or repository_root.is_relative_to(external_root):
                raise ConfigError(
                    f"repositories.{repo_id}.external_roots overlaps registered repository "
                    f"{registered_id}: {external_root}"
                )


def _sha256_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
