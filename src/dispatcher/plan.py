"""Normalized schema-v2 plans and explicit YAML-sidecar import."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from .config import Config, ConfigError, ContractModel, Identifier
from .yaml_io import DuplicateYamlKeyError, load_unique_yaml


class PlanError(ValueError):
    """A normalized plan is invalid or incompatible with project configuration."""


Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
RelativePath = Annotated[str, Field(min_length=1, max_length=500)]


class PlanSource(ContractModel):
    """An immutable source document that informed normalized work."""

    source_id: Identifier
    root: Literal["plans", "specifications"]
    relative_path: RelativePath
    sha256: Sha256
    media_type: Literal["text/markdown", "application/yaml"]

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("relative_path must be relative and cannot contain '..'")
        return value


class ArtifactReference(ContractModel):
    """A required input or produced output identified independently of prose."""

    artifact_id: Identifier
    producer_step_id: Identifier | None
    description: Annotated[str, Field(min_length=1, max_length=1000)]


class ResourceLock(ContractModel):
    """A normalized exclusive resource lock held during one step."""

    resource_id: Identifier
    mode: Literal["read", "write"]


class StepAuthorization(ContractModel):
    """Structured authorization request constrained by repository policy."""

    authorized_actions: tuple[Identifier, ...]
    writable_paths: tuple[RelativePath, ...]

    @field_validator("authorized_actions", "writable_paths", mode="before")
    @classmethod
    def freeze_authorization_collections(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value
    requires_operator_approval: bool

    @field_validator("authorized_actions")
    @classmethod
    def nonempty_unique_actions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("authorized_actions must not be empty")
        if len(values) != len(set(values)):
            raise ValueError("authorized_actions must not contain duplicates")
        return values

    @field_validator("writable_paths")
    @classmethod
    def valid_writable_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[tuple[PurePosixPath, bool]] = []
        for value in values:
            path, directory = parse_writable_path(value)
            for existing_path, existing_directory in normalized:
                if path == existing_path:
                    raise ValueError("writable_paths must not contain duplicate paths")
                if existing_directory and _pure_path_is_within(path, existing_path):
                    raise ValueError("writable_paths must not contain overlapping paths")
                if directory and _pure_path_is_within(existing_path, path):
                    raise ValueError("writable_paths must not contain overlapping paths")
            normalized.append((path, directory))
        return values

    @model_validator(mode="after")
    def validate_action_scope(self) -> Self:
        actions = set(self.authorized_actions)
        if "modify" in actions and not self.writable_paths:
            raise ValueError("modify authorization requires nonempty writable_paths")
        if "commit" in actions and "modify" not in actions:
            raise ValueError("commit authorization requires modify authorization")
        if "modify" not in actions and self.writable_paths:
            raise ValueError("writable_paths require modify authorization")
        return self


class VerificationCheck(ContractModel):
    """Dispatcher-owned argv check executed without a shell."""

    argv: tuple[Annotated[str, Field(min_length=1, max_length=1000)], ...]
    working_directory: Literal["repository"]
    timeout_seconds: Annotated[int, Field(ge=1, le=3600)]
    max_output_bytes: Annotated[int, Field(ge=1024, le=10_000_000)]
    expected_exit_codes: tuple[Annotated[int, Field(ge=0, le=255)], ...]
    network_policy: Literal["deny"]

    @field_validator("argv", "expected_exit_codes", mode="before")
    @classmethod
    def freeze_check_collections(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_check(self) -> Self:
        if not self.argv:
            raise ValueError("verification check argv must not be empty")
        if not self.expected_exit_codes:
            raise ValueError("verification check expected_exit_codes must not be empty")
        if len(self.expected_exit_codes) != len(set(self.expected_exit_codes)):
            raise ValueError("verification check expected_exit_codes must be unique")
        return self


class AcceptanceCriterion(ContractModel):
    """A check that must pass before a step can be accepted."""

    criterion_id: Identifier
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    check: VerificationCheck


class EvidenceRequirement(ContractModel):
    """An evidence artifact required for step acceptance."""

    artifact_id: Identifier
    relative_path: RelativePath
    media_type: Literal["text/markdown", "application/json", "text/plain"]

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("relative_path must be relative and cannot contain '..'")
        return value


class ReviewObligation(ContractModel):
    """Typed review obligation, independent of the selected review profile."""

    required: bool
    reviewer_role_keys: tuple[Identifier, ...]
    required_acceptances: Annotated[int, Field(ge=0, le=20)]

    @field_validator("reviewer_role_keys", mode="before")
    @classmethod
    def freeze_reviewer_keys(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_requirement(self) -> Self:
        if self.required:
            if not self.reviewer_role_keys:
                raise ValueError("required review needs at least one reviewer_role_key")
            if self.required_acceptances < 1:
                raise ValueError("required review needs at least one required_acceptance")
        elif self.reviewer_role_keys or self.required_acceptances:
            raise ValueError("optional review must have no reviewers and zero required_acceptances")
        if len(self.reviewer_role_keys) != len(set(self.reviewer_role_keys)):
            raise ValueError("reviewer_role_keys must not contain duplicates")
        if self.required_acceptances > len(self.reviewer_role_keys):
            raise ValueError("required_acceptances cannot exceed reviewer_role_keys")
        return self


class RetryPolicy(ContractModel):
    """Step-local retry and escalation rules, with no runtime defaults."""

    max_executor_attempts: Annotated[int, Field(ge=1, le=20)]
    max_reviewer_attempts: Annotated[int, Field(ge=0, le=20)]
    on_failed: Literal["halt", "retry", "escalate"]
    on_blocked: Literal["halt", "retry", "escalate"]
    on_changes_requested: Literal["halt", "retry", "escalate"]
    escalation_role_key: Identifier | None

    @model_validator(mode="after")
    def escalation_requires_role(self) -> Self:
        needs_escalation = "escalate" in {
            self.on_failed,
            self.on_blocked,
            self.on_changes_requested,
        }
        if needs_escalation != (self.escalation_role_key is not None):
            raise ValueError(
                "escalation_role_key is required exactly when a retry policy escalates"
            )
        return self


class PlanStep(ContractModel):
    """A fully actionable step with dependencies and acceptance obligations."""

    ordinal: Annotated[int, Field(ge=1)]
    step_id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=500)]
    repo_id: Identifier
    depends_on: tuple[Identifier, ...]
    required_inputs: tuple[ArtifactReference, ...]
    produced_outputs: tuple[ArtifactReference, ...]
    resource_locks: tuple[ResourceLock, ...]
    risk_tags: tuple[Identifier, ...]
    authorization: StepAuthorization
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    evidence_requirements: tuple[EvidenceRequirement, ...]
    review: ReviewObligation
    retry: RetryPolicy

    @field_validator(
        "depends_on",
        "required_inputs",
        "produced_outputs",
        "resource_locks",
        "risk_tags",
        "acceptance_criteria",
        "evidence_requirements",
        mode="before",
    )
    @classmethod
    def freeze_collections(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_step_collections(self) -> Self:
        if self.step_id in self.depends_on:
            raise ValueError("depends_on cannot include the current step")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("depends_on must not contain duplicates")
        if len(self.resource_locks) != len(
            {(lock.resource_id, lock.mode) for lock in self.resource_locks}
        ):
            raise ValueError("resource_locks must not contain duplicates")
        if len(self.risk_tags) != len(set(self.risk_tags)):
            raise ValueError("risk_tags must not contain duplicates")
        if not self.acceptance_criteria:
            raise ValueError("acceptance_criteria must not be empty")
        if not self.evidence_requirements:
            raise ValueError("evidence_requirements must not be empty")
        if len({criterion.criterion_id for criterion in self.acceptance_criteria}) != len(
            self.acceptance_criteria
        ):
            raise ValueError("acceptance criterion IDs must be unique per step")
        if len({item.artifact_id for item in self.produced_outputs}) != len(
            self.produced_outputs
        ):
            raise ValueError("produced output artifact IDs must be unique per step")
        if len({item.artifact_id for item in self.evidence_requirements}) != len(
            self.evidence_requirements
        ):
            raise ValueError("evidence artifact IDs must be unique per step")
        return self


class NormalizedPlan(ContractModel):
    """Immutable execution plan, with semantic digest independent of source format."""

    schema_version: Literal[2]
    plan_id: Identifier
    sources: tuple[PlanSource, ...]
    steps: tuple[PlanStep, ...]

    @field_validator("sources", "steps", mode="before")
    @classmethod
    def freeze_collections(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        if not self.sources:
            raise ValueError("sources must not be empty")
        if not self.steps:
            raise ValueError("steps must not be empty")
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ValueError("source IDs must be unique")

        step_map = {step.step_id: step for step in self.steps}
        if len(step_map) != len(self.steps):
            raise ValueError("step IDs must be unique")
        ordinals = [step.ordinal for step in self.steps]
        if ordinals != list(range(1, len(self.steps) + 1)):
            raise ValueError("steps must be ordered with contiguous ordinals starting at 1")

        for step in self.steps:
            for dependency_id in step.depends_on:
                dependency = step_map.get(dependency_id)
                if dependency is None:
                    raise ValueError(
                        f"steps.{step.step_id}.depends_on references unknown step {dependency_id}"
                    )
                if dependency.ordinal >= step.ordinal:
                    raise ValueError("dependencies must have a lower ordinal than dependent steps")
            for artifact in step.required_inputs:
                if artifact.producer_step_id is not None:
                    if artifact.producer_step_id not in step_map:
                        raise ValueError(
                            "required input producer_step_id must reference a plan step"
                        )
                    if artifact.producer_step_id not in step.depends_on:
                        raise ValueError(
                            "required input producer_step_id must be listed in depends_on"
                        )

        produced_ids = [
            artifact.artifact_id for step in self.steps for artifact in step.produced_outputs
        ]
        if len(produced_ids) != len(set(produced_ids)):
            raise ValueError("produced output artifact IDs must be globally unique")
        self._validate_no_cycles(step_map)
        self._validate_unordered_write_locks(step_map)
        return self

    @property
    def plan_digest(self) -> str:
        """Digest semantic steps only, so equivalent adapters share a digest."""
        payload = {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "steps": [step.model_dump(mode="json") for step in self.steps],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def source_digest(self) -> str:
        """Digest source identities and hashes separately from semantic plan data."""
        payload = [source.model_dump(mode="json") for source in self.sources]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _validate_no_cycles(self, step_map: dict[str, PlanStep]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError(f"dependency cycle detected at step {step_id}")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in step_map[step_id].depends_on:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in step_map:
            visit(step_id)

    def _validate_unordered_write_locks(self, step_map: dict[str, PlanStep]) -> None:
        ancestors = {
            step_id: _ancestor_steps(step_id, step_map) for step_id in step_map
        }
        writers: dict[str, list[str]] = {}
        for step in self.steps:
            for lock in step.resource_locks:
                if lock.mode == "write":
                    writers.setdefault(lock.resource_id, []).append(step.step_id)
        for resource_id, writer_steps in writers.items():
            for index, first in enumerate(writer_steps):
                for second in writer_steps[index + 1 :]:
                    if first not in ancestors[second] and second not in ancestors[first]:
                        raise ValueError(
                            "unordered write lock conflict for resource "
                            f"{resource_id}: {first}, {second}"
                        )


class PlanApproval(ContractModel):
    """Operator approval binding immutable semantic and source plan identities."""

    plan_digest: Sha256
    source_digest: Sha256
    operator_decision_ref: Identifier
    approved_at: datetime


def approve_plan(plan: NormalizedPlan, operator_decision_ref: str) -> PlanApproval:
    """Create the explicit approval required before a normalized plan can run."""
    return PlanApproval(
        plan_digest=plan.plan_digest,
        source_digest=plan.source_digest,
        operator_decision_ref=operator_decision_ref,
        approved_at=datetime.now(UTC),
    )


def validate_plan_approval(plan: NormalizedPlan, approval: PlanApproval) -> None:
    """Reject an approval if semantic plan or approved source content changed."""
    if approval.plan_digest != plan.plan_digest:
        raise PlanError("plan approval does not match normalized plan digest")
    if approval.source_digest != plan.source_digest:
        raise PlanError("plan approval does not match normalized plan source digest")


def load_normalized_plan(path: str | Path, config: Config) -> NormalizedPlan:
    """Load a YAML sidecar without guessing missing fields from Markdown prose."""
    plan_path = Path(path).expanduser().resolve()
    if not plan_path.is_file():
        raise PlanError(f"normalized plan sidecar not found: {plan_path}")
    try:
        raw = load_unique_yaml(plan_path)
    except (DuplicateYamlKeyError, yaml.YAMLError) as exc:
        raise PlanError(f"invalid normalized plan YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PlanError("normalized plan sidecar must be a YAML mapping")
    try:
        plan = NormalizedPlan.model_validate(raw)
    except ValidationError as exc:
        raise PlanError(_format_plan_validation_error(exc)) from exc
    validate_plan_for_config(plan, config)
    verify_plan_sources(plan, config)
    return plan


def validate_plan_for_config(plan: NormalizedPlan, config: Config) -> None:
    """Validate plan repository, reviewer, escalation, and policy references."""
    for step in plan.steps:
        try:
            repository = config.repository(step.repo_id)
        except ConfigError as exc:
            raise PlanError(str(exc)) from exc
        repository_policy = config.model.permission_policies.policies[
            repository.permission_policy
        ]
        allowed_actions = {
            action for action, decision in repository_policy.actions.items() if decision == "allow"
        }
        unauthorized = set(step.authorization.authorized_actions) - allowed_actions
        if unauthorized:
            raise PlanError(
                f"step {step.step_id} authorization exceeds repository policy: "
                f"{sorted(unauthorized)}"
            )
        actions = set(step.authorization.authorized_actions)
        if "commit" in actions and repository.commit_policy != "required":
            raise PlanError(
                f"step {step.step_id} commit authorization requires repository commit_policy required"
            )
        if repository.commit_policy == "required" and "modify" in actions and "commit" not in actions:
            raise PlanError(
                f"step {step.step_id} modifies a required-commit repository without commit authorization"
            )
        for writable_path in step.authorization.writable_paths:
            logical_path, _directory = parse_writable_path(writable_path)
            if not any(
                _pure_path_is_within(logical_path, PurePosixPath(root))
                for root in repository.writable_roots
            ):
                raise PlanError(
                    f"step {step.step_id} writable path is outside repository writable_roots: "
                    f"{writable_path}"
                )
        if "modify" in actions:
            for requirement in step.evidence_requirements:
                candidates = [
                    PurePosixPath(root) / PurePosixPath(requirement.relative_path)
                    for root in repository.evidence_roots
                ]
                authorized = [
                    candidate
                    for candidate in candidates
                    if writable_path_allows(step.authorization.writable_paths, candidate.as_posix())
                ]
                if len(authorized) != 1:
                    raise PlanError(
                        f"step {step.step_id} evidence {requirement.artifact_id} must resolve "
                        "inside exactly one writable_paths scope"
                    )
        for reviewer_role_key in step.review.reviewer_role_keys:
            if config.role_kind(reviewer_role_key) != "reviewer":
                raise PlanError(
                    f"step {step.step_id} reviewer role is not a reviewer: {reviewer_role_key}"
                )
        escalation_role = step.retry.escalation_role_key
        if escalation_role is not None:
            config.role(escalation_role)


def verify_plan_sources(plan: NormalizedPlan, config: Config) -> None:
    """Verify every immutable normalized-plan source before bootstrap or dispatch."""
    roots = {
        "plans": Path(config.model.sources.plans_dir),
        "specifications": Path(config.model.sources.specifications_dir),
    }
    for source in plan.sources:
        path = roots[source.root] / source.relative_path
        if not path.is_file():
            raise PlanError(f"plan source does not exist: {path}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != source.sha256:
            raise PlanError(
                f"plan source hash mismatch for {source.source_id}: "
                f"expected {source.sha256}, found {actual}"
            )


def _ancestor_steps(step_id: str, step_map: dict[str, PlanStep]) -> set[str]:
    ancestors: set[str] = set()
    for dependency in step_map[step_id].depends_on:
        ancestors.add(dependency)
        ancestors.update(_ancestor_steps(dependency, step_map))
    return ancestors


def parse_writable_path(value: str) -> tuple[PurePosixPath, bool]:
    """Parse one canonical repository-relative file or trailing-slash directory scope."""
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("writable path must not contain control characters")
    if "\\" in value:
        raise ValueError("writable path must use POSIX separators")
    directory = value.endswith("/")
    logical = value[:-1] if directory else value
    if not logical or logical.startswith("/") or "//" in logical:
        raise ValueError("writable path must be a non-root repository-relative path")
    parts = logical.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("writable path must not contain empty, '.', or '..' segments")
    if ".git" in parts:
        raise ValueError("writable path must not include .git")
    return PurePosixPath(*parts), directory


def writable_path_allows(scopes: tuple[str, ...], value: str) -> bool:
    """Return whether one canonical repository path is authorized by exact scopes."""
    path = PurePosixPath(value)
    return any(
        path == scope_path or (directory and _pure_path_is_within(path, scope_path))
        for scope_path, directory in (parse_writable_path(scope) for scope in scopes)
    )


def _pure_path_is_within(path: PurePosixPath, root: PurePosixPath) -> bool:
    if root == PurePosixPath("."):
        return True
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _format_plan_validation_error(exc: ValidationError) -> str:
    errors = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        errors.append(f"{location}: {error['msg']}")
    return "invalid normalized plan: " + "; ".join(errors)
