"""Strict schema-v1 project configuration and environment validation."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import unicodedata
import urllib.parse
from pathlib import Path, PurePosixPath
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

from .yaml_io import DuplicateYamlKeyError, load_unique_yaml


class ConfigError(ValueError):
    """A schema or environment validation failure in project configuration."""


class ContractModel(BaseModel):
    """Base for immutable, strict, closed configuration contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")]
ModelName = Annotated[str, Field(pattern=r"^[^/\s]+/[^/\s]+$")]
PermissionDecision = Literal["allow", "ask", "deny"]
PermissionAction = Literal[
    "inspect",
    "modify",
    "verify",
    "commit",
    "push",
    "force_push",
    "create_branch",
]
PERMISSION_ACTIONS: tuple[PermissionAction, ...] = (
    "inspect",
    "modify",
    "verify",
    "commit",
    "push",
    "force_push",
    "create_branch",
)


def validate_normalized_relative_path(value: str, field: str) -> str:
    """Reject paths that could expand, escape, or select an unbounded file set."""
    if not value:
        raise ValueError(f"{field} must not be empty")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"{field} must not contain control characters")
    if "\\" in value:
        raise ValueError(f"{field} must use POSIX separators")
    if value.startswith("/") or value.endswith("/") or "//" in value:
        raise ValueError(f"{field} must be a normalized repository-relative path")
    if any(character in value for character in "$%{}*?[]~"):
        raise ValueError(f"{field} must not contain expansion or wildcard characters")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field} must not contain empty, '.', or '..' segments")
    if ".git" in parts:
        raise ValueError(f"{field} must not include .git")
    if any(part.startswith("-") or ":" in part for part in parts):
        raise ValueError(f"{field} must not contain command options or URLs")
    PurePosixPath(*parts)
    return value


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


MCPToolName = Annotated[
    str,
    Field(pattern=r"^[a-z0-9][a-z0-9-]*_[a-z0-9][a-z0-9_-]*$"),
]
_MCP_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"

# The explicit Step 21-lite tool catalog. Every role tool must be listed here;
# each entry maps a sanitized OpenCode tool name to its configured server key.
MCP_TOOL_CATALOG: dict[str, str] = {
    "context7_resolve-library-id": "context7",
    "context7_query-docs": "context7",
    "repomix_pack_codebase": "repomix",
    "repomix_pack_remote_repository": "repomix",
    "repomix_attach_packed_output": "repomix",
    "repomix_read_repomix_output": "repomix",
    "repomix_grep_repomix_output": "repomix",
    "repomix_file_system_read_directory": "repomix",
    "repomix_file_system_read_file": "repomix",
    "semble_search": "semble",
    "semble_find_related": "semble",
}
DEFAULT_INHERITED_MCP_TOOLS = tuple(MCP_TOOL_CATALOG)


def _validate_mcp_environment_names(values: tuple[str, ...], location: str) -> None:
    for name in values:
        if not re.match(_MCP_ENV_NAME, name):
            raise ValueError(f"{location} names must be valid environment variable names")
    if len(values) != len(set(values)):
        raise ValueError(f"{location} names must be unique")


class MCPRemoteServer(ContractModel):
    """One remote HTTP MCP server definition."""

    type: Literal["remote"]
    enabled: bool
    url: Annotated[str, Field(min_length=1, max_length=2000)]
    headers: dict[str, str] = {}

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("remote MCP server url must be an http or https URL")
        return value


class MCPLocalServer(ContractModel):
    """One local stdio MCP server definition with an exact argv list."""

    type: Literal["local"]
    enabled: bool
    command: tuple[Annotated[str, Field(min_length=1)], ...] = Field(min_length=1)
    environment: dict[str, str] = {}

    @field_validator("command", mode="before")
    @classmethod
    def freeze_command(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


MCPServerDefinition = Annotated[
    MCPLocalServer | MCPRemoteServer,
    Field(discriminator="type"),
]


class MCPRegistry(ContractModel):
    """Explicit project MCP server registry and passthrough environment names."""

    environment_passthrough: tuple[str, ...] = ()
    servers: dict[Identifier, MCPServerDefinition]

    @field_validator("environment_passthrough", mode="before")
    @classmethod
    def freeze_passthrough(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("environment_passthrough")
    @classmethod
    def validate_passthrough(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        _validate_mcp_environment_names(values, "mcp.environment_passthrough")
        return values


class RoleDefinition(ContractModel):
    """Configured model role and its named permission policy."""

    model: ModelName
    variant: Annotated[str, Field(min_length=1, max_length=100)]
    display: Annotated[str, Field(min_length=1, max_length=200)]
    permission_policy: Identifier
    mcp_tools: tuple[MCPToolName, ...] = ()

    @field_validator("mcp_tools", mode="before")
    @classmethod
    def freeze_tool_list(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


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


class StructuredGitDefinition(ContractModel):
    """Explicit dispatcher-owned Git commit identity and process bounds."""

    capability_version: Literal[1]
    author_name: Annotated[str, Field(min_length=1, max_length=200)]
    author_email: Annotated[str, Field(min_length=3, max_length=320)]
    committer_name: Annotated[str, Field(min_length=1, max_length=200)]
    committer_email: Annotated[str, Field(min_length=3, max_length=320)]
    timeout_seconds: Annotated[int, Field(ge=1, le=300)]
    max_output_bytes: Annotated[int, Field(ge=1024, le=10_000_000)]

    @field_validator("author_name", "author_email", "committer_name", "committer_email")
    @classmethod
    def safe_identity_value(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Git identity values must not have leading or trailing whitespace")
        if any(unicodedata.category(character) == "Cc" for character in value):
            raise ValueError("Git identity values must not contain control characters")
        return value

    @field_validator("author_email", "committer_email")
    @classmethod
    def valid_email_shape(cls, value: str) -> str:
        local, separator, domain = value.partition("@")
        if not separator or not local or not domain or "@" in domain:
            raise ValueError("Git identity email must contain one non-edge '@'")
        return value


class ExecutionDefinition(ContractModel):
    """All active execution controls for the mock-only Phase 7 runtime."""

    mode: Literal["mock_workflow_test", "real_operation"]
    protocol_version: Literal[1]
    verification_backend: Literal["darwin_seatbelt_v1", "linux_bwrap_v1", "direct_test_v1"]
    structured_git: StructuredGitDefinition
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

    @model_validator(mode="after")
    def test_backend_is_limited_to_mock_workflows(self) -> Self:
        if self.mode == "real_operation" and self.verification_backend == "direct_test_v1":
            raise ValueError(
                "direct_test_v1 verification backend is only allowed in mock_workflow_test mode"
            )
        return self


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
    on_exhausted: Literal["ask", "halt", "fail"]


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
    actions: dict[PermissionAction, PermissionDecision]

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


KubernetesContext = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,253}$"),
]
KubernetesName = Annotated[
    str,
    Field(pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$"),
]
Sha256Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
KubernetesDiscoveryResource = Annotated[
    str,
    Field(pattern=r"^[a-z][a-z0-9-]{0,62}(?:\.[a-z0-9][a-z0-9.-]{0,251})?$"),
]
KubernetesAuthResource = Annotated[
    str,
    Field(
        pattern=(
            r"^[a-z][a-z0-9-]{0,62}(?:\.[a-z0-9][a-z0-9.-]{0,251})?"
            r"(?:/[a-z][a-z0-9-]{0,62})?$"
        )
    ),
]
SemanticVersion = Annotated[
    str,
    Field(
        pattern=(
            r"^v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
            r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
            r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
        )
    ),
]
KubectlAuthVerb = Literal["get", "list", "watch", "create", "patch", "update", "delete"]


class HelmReleaseRequirement(ContractModel):
    """One deployed Helm release with a chart-version capability floor."""

    release: KubernetesName
    namespace: KubernetesName
    chart: KubernetesName
    minimum_chart_version: SemanticVersion


class ApiResourceRequirement(ContractModel):
    """One exact Kubernetes API resource advertised by discovery."""

    resource: KubernetesDiscoveryResource


class KubectlAuthRequirement(ContractModel):
    """One bounded authorization capability required by a future typed operation."""

    verb: KubectlAuthVerb
    resource: KubernetesAuthResource
    namespace: KubernetesName


class ClusterPreflightDefinition(ContractModel):
    """Read-only Kubernetes readiness contract for a named integration cluster."""

    capability_version: Literal[1]
    target_id: Identifier = "cluster-preflight"
    kubectl_path: str | None = None
    context: KubernetesContext
    minimum_client_version: SemanticVersion
    minimum_server_version: SemanticVersion
    request_timeout_seconds: Annotated[int, Field(ge=1, le=30)]
    required_namespaces: list[KubernetesName] = Field(min_length=1, max_length=20)
    required_helm_releases: list[HelmReleaseRequirement] = Field(min_length=1, max_length=20)
    required_api_resources: list[ApiResourceRequirement] = Field(min_length=1, max_length=50)
    auth_checks: list[KubectlAuthRequirement] = Field(min_length=1, max_length=50)

    @field_validator("kubectl_path")
    @classmethod
    def kubectl_path_is_absolute_and_normalized(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not Path(value).is_absolute():
            raise ValueError("kubectl_path must be an absolute path")
        if value != os.path.normpath(value):
            raise ValueError("kubectl_path must be normalized")
        return value

    @field_validator("required_namespaces")
    @classmethod
    def required_namespaces_are_unique(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("required_namespaces must not contain duplicates")
        return values

    @model_validator(mode="after")
    def validate_exact_requirements(self) -> Self:
        release_keys = [(item.namespace, item.release) for item in self.required_helm_releases]
        if len(release_keys) != len(set(release_keys)):
            raise ValueError("required_helm_releases must not contain duplicate namespace/release pairs")

        resources = [item.resource for item in self.required_api_resources]
        if len(resources) != len(set(resources)):
            raise ValueError("required_api_resources must not contain duplicates")

        namespaces = set(self.required_namespaces)
        required_resources = set(resources)
        auth_keys = [(item.verb, item.resource, item.namespace) for item in self.auth_checks]
        if len(auth_keys) != len(set(auth_keys)):
            raise ValueError("auth_checks must not contain duplicate verb/resource/namespace checks")
        for item in self.auth_checks:
            if item.namespace not in namespaces:
                raise ValueError("auth_checks namespaces must appear in required_namespaces")
            parent_resource, _separator, _subresource = item.resource.partition("/")
            if parent_resource not in required_resources:
                raise ValueError(
                    "auth_checks resources or subresource parents must appear in required_api_resources"
                )
        return self


class ClusterPreflightConfig(ContractModel):
    """Schema-validated input and identity for one read-only cluster preflight."""

    project_id: Identifier
    config_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    definition: ClusterPreflightDefinition


class ClusterMutationToolDefinition(ContractModel):
    """One exact dispatcher-owned executable pinned for mutation-time verification."""

    path: str
    sha256: Sha256Digest

    @field_validator("path")
    @classmethod
    def executable_path_is_absolute_and_normalized(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("mutation tool path must be an absolute path")
        if value != os.path.normpath(value):
            raise ValueError("mutation tool path must be normalized")
        return value


class ClusterMutationToolchainDefinition(ContractModel):
    """The two binaries whose content is re-verified before every mutation launch."""

    kubectl: ClusterMutationToolDefinition
    helm: ClusterMutationToolDefinition

    @model_validator(mode="after")
    def tool_paths_are_distinct(self) -> Self:
        if self.kubectl.path == self.helm.path:
            raise ValueError("cluster mutation kubectl and helm paths must be distinct")
        return self


class ClusterMutationTargetDefinition(ContractModel):
    """Static allowlist for a future dispatcher-owned cluster operation target."""

    context: KubernetesContext
    toolchain: ClusterMutationToolchainDefinition
    allowed_repository_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=20)
    operation_manifest_roots: tuple[str, ...] = Field(min_length=1, max_length=20)
    source_file_roots: tuple[str, ...] = Field(min_length=1, max_length=20)
    max_snapshot_age_seconds: Annotated[int, Field(ge=1, le=86_400)]
    max_action_timeout_seconds: Annotated[int, Field(ge=1, le=3_600)]
    preflight_target_id: Identifier

    @field_validator(
        "allowed_repository_ids",
        "operation_manifest_roots",
        "source_file_roots",
        mode="before",
    )
    @classmethod
    def freeze_collections(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("operation_manifest_roots", "source_file_roots")
    @classmethod
    def validate_roots(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            validate_normalized_relative_path(value, "cluster mutation root")
        return values

    @model_validator(mode="after")
    def validate_unique_allowlists(self) -> Self:
        for field_name, values in (
            ("allowed_repository_ids", self.allowed_repository_ids),
            ("operation_manifest_roots", self.operation_manifest_roots),
            ("source_file_roots", self.source_file_roots),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must not contain duplicates")
        return self


class ClusterMutationDefinition(ContractModel):
    """Schema-only cluster mutation capability; it grants no execution authority."""

    capability_version: Literal[1]
    targets: dict[Identifier, ClusterMutationTargetDefinition]

    @model_validator(mode="after")
    def require_named_targets(self) -> Self:
        if not self.targets:
            raise ValueError("cluster_mutation.targets must define at least one target")
        return self


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
    cluster_preflight: ClusterPreflightDefinition | None = None
    cluster_mutation: ClusterMutationDefinition | None = None
    mcp: MCPRegistry | None = None

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
        required_actions = set(PERMISSION_ACTIONS)
        for role_kind, policy_id in self.permission_policies.role_class_policies.model_dump().items():
            actions = set(self.permission_policies.policies[policy_id].actions)
            if actions != required_actions:
                missing = ", ".join(sorted(required_actions - actions)) or "none"
                extra = ", ".join(sorted(actions - required_actions)) or "none"
                raise ValueError(
                    "permission_policies.role_class_policies."
                    f"{role_kind} policy {policy_id!r} must define every permission action "
                    f"exactly once; missing: {missing}; extra: {extra}"
                )
        self._validate_cluster_mutation_targets()
        self._validate_mcp_registry()
        return self

    def _validate_cluster_mutation_targets(self) -> None:
        """Bind future mutation targets to this config's one read-only preflight target."""
        if self.cluster_mutation is None:
            return
        if self.cluster_preflight is None:
            raise ValueError("cluster_mutation requires cluster_preflight")
        preflight = self.cluster_preflight
        for target_name, target in self.cluster_mutation.targets.items():
            if target.preflight_target_id != preflight.target_id:
                raise ValueError(
                    "cluster_mutation.targets."
                    f"{target_name}.preflight_target_id must reference cluster_preflight.target_id"
                )
            if target.context != preflight.context:
                raise ValueError(
                    f"cluster_mutation.targets.{target_name}.context must match cluster_preflight.context"
                )
            for repository_id in target.allowed_repository_ids:
                if repository_id not in self.repositories:
                    raise ValueError(
                        "cluster_mutation.targets."
                        f"{target_name}.allowed_repository_ids must reference repositories"
                    )
            validate_cluster_mutation_toolchain(target.toolchain)

    def _validate_mcp_registry(self) -> None:
        """Reject MCP assignments that cannot compile to exact OpenCode rules."""
        for role_key, role in self.all_roles().items():
            if len(role.mcp_tools) != len(set(role.mcp_tools)):
                raise ValueError(f"roles.{role_key}.mcp_tools must not contain duplicate tools")
            for tool in role.mcp_tools:
                server_key = MCP_TOOL_CATALOG.get(tool)
                if server_key is None:
                    raise ValueError(
                        f"roles.{role_key}.mcp_tools tool {tool!r} is not in the configured MCP tool catalog"
                    )
                if self.mcp is None:
                    continue
                server = self.mcp.servers.get(server_key)
                if server is None:
                    raise ValueError(
                        f"roles.{role_key}.mcp_tools tool {tool!r} does not reference a configured MCP server"
                    )
                if not server.enabled:
                    raise ValueError(
                        f"roles.{role_key}.mcp_tools tool {tool!r} references disabled MCP server {server_key!r}"
                    )

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
        self.model = _load_project_config_model(self.config_path)
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
    def cluster_preflight(self) -> ClusterPreflightDefinition | None:
        return self.model.cluster_preflight

    @property
    def cluster_mutation(self) -> ClusterMutationDefinition | None:
        return self.model.cluster_mutation

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


def load_cluster_preflight_config(config_path: str | Path) -> ClusterPreflightConfig:
    """Load only the strict project schema required for a read-only cluster check.

    This intentionally does not construct ``Config``: cluster preflight must not
    create state or run repository/environment probes before its bounded cluster
    commands start.
    """
    path = Path(config_path).expanduser().resolve()
    model = _load_project_config_model(path)
    if model.cluster_preflight is None:
        raise ConfigError("cluster_preflight is required for dispatcher cluster-preflight")
    validate_cluster_preflight_kubectl_path(model.cluster_preflight)
    return ClusterPreflightConfig(
        project_id=model.project.project_id,
        config_digest=_sha256_json(model.model_dump(mode="json")),
        definition=model.cluster_preflight,
    )


def validate_cluster_preflight_kubectl_path(definition: ClusterPreflightDefinition) -> None:
    """Reject configured kubectl clients that cannot be safely executed directly."""
    if definition.kubectl_path is None:
        return
    path = Path(definition.kubectl_path)
    if not path.is_file():
        raise ConfigError("cluster_preflight.kubectl_path must be an existing regular file")
    if not os.access(path, os.X_OK):
        raise ConfigError("cluster_preflight.kubectl_path must be executable")


def validate_cluster_mutation_toolchain(definition: ClusterMutationToolchainDefinition) -> None:
    """Require configured mutation binaries to be executable before they can be launched.

    This validates only filesystem shape. The dispatcher runner recalculates both
    configured SHA-256 values at each operation launch before issuing any command.
    """
    for name, tool in (("kubectl", definition.kubectl), ("helm", definition.helm)):
        path = Path(tool.path)
        if not path.is_file():
            raise ConfigError(f"cluster_mutation.toolchain.{name}.path must be an existing regular file")
        if not os.access(path, os.X_OK):
            raise ConfigError(f"cluster_mutation.toolchain.{name}.path must be executable")


def _load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"{label} file not found: {path}")
    try:
        data = load_unique_yaml(path)
    except (DuplicateYamlKeyError, yaml.YAMLError) as exc:
        raise ConfigError(f"invalid {label} YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{label} must be a YAML mapping")
    return data


def _load_project_config_model(config_path: Path) -> ProjectConfigModel:
    """Parse and strictly validate a project config without environment side effects."""
    raw = _load_yaml_mapping(config_path, "project config")
    resolved = _resolve_config_paths(raw, config_path.parent)
    try:
        return ProjectConfigModel.model_validate(resolved)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error("project config", exc)) from exc


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
