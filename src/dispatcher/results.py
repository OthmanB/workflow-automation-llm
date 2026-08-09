"""Typed schema-v1 executor and reviewer result protocols."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from .config import ContractModel, Identifier
from .plan import Sha256


class ResultError(ValueError):
    """A worker result is malformed or does not match its active dispatch."""


class ArtifactRecord(ContractModel):
    """Immutable evidence artifact returned by an executor or reviewer."""

    artifact_id: Identifier
    relative_path: Annotated[str, Field(min_length=1, max_length=500)]
    sha256: Sha256
    media_type: Literal["text/markdown", "application/json", "text/plain"]
    size_bytes: Annotated[int, Field(ge=0, le=1_000_000_000)]


class VerificationResult(ContractModel):
    """Structured verification outcome; command transcript stays supplemental."""

    check_id: Identifier
    status: Literal["passed", "failed", "skipped"]
    summary: Annotated[str, Field(min_length=1, max_length=5000)]


class RepositoryCoordinate(ContractModel):
    """Executor work coordinates pinned before and after a dispatch."""

    repo_id: Identifier
    base_revision: Annotated[str, Field(min_length=1, max_length=200)]
    result_revision: Annotated[str, Field(min_length=1, max_length=200)] | None
    patch_sha256: Sha256 | None

    @model_validator(mode="after")
    def completed_coordinate_requires_result(self) -> "RepositoryCoordinate":
        if self.result_revision is None and self.patch_sha256 is None:
            raise ValueError("repository coordinate requires result_revision or patch_sha256")
        return self


class ExecutorResultBase(ContractModel):
    """Common fields for every executor result outcome."""

    result_version: Literal[1]
    dispatch_id: Identifier
    attempt: Annotated[int, Field(ge=1, le=100)]
    step_id: Identifier
    repository: RepositoryCoordinate
    evidence: list[ArtifactRecord]
    verification: list[VerificationResult]
    summary: Annotated[str, Field(min_length=1, max_length=10_000)]
    transcript_ref: Identifier | None = None


class ExecutorCompletedResult(ExecutorResultBase):
    """Executor reported completed work at a pinned revision or patch."""

    outcome: Literal["completed"]


class ExecutorBlockedResult(ExecutorResultBase):
    """Executor cannot continue and supplies one or more structured blockers."""

    outcome: Literal["blocked"]
    blockers: list[Annotated[str, Field(min_length=1, max_length=5000)]]

    @model_validator(mode="after")
    def requires_blockers(self) -> "ExecutorBlockedResult":
        if not self.blockers:
            raise ValueError("blocked executor result requires blockers")
        return self


class ExecutorFailedResult(ExecutorResultBase):
    """Executor failed with a stable classification code."""

    outcome: Literal["failed"]
    failure_code: Identifier


ExecutorResult: TypeAlias = Annotated[
    ExecutorCompletedResult | ExecutorBlockedResult | ExecutorFailedResult,
    Field(discriminator="outcome"),
]
_EXECUTOR_RESULT_ADAPTER: TypeAdapter[ExecutorResult] = TypeAdapter(ExecutorResult)


class ReviewTarget(ContractModel):
    """Immutable executor output and artifacts a reviewer was asked to inspect."""

    executor_dispatch_id: Identifier
    executor_attempt: Annotated[int, Field(ge=1, le=100)]
    result_revision: Annotated[str, Field(min_length=1, max_length=200)] | None
    patch_sha256: Sha256 | None
    artifact_hashes: list[Sha256]

    @model_validator(mode="after")
    def requires_revision_or_patch(self) -> "ReviewTarget":
        if self.result_revision is None and self.patch_sha256 is None:
            raise ValueError("review target requires result_revision or patch_sha256")
        if len(self.artifact_hashes) != len(set(self.artifact_hashes)):
            raise ValueError("review target artifact_hashes must not contain duplicates")
        return self


class ReviewFinding(ContractModel):
    """A review finding whose severity determines whether acceptance is possible."""

    finding_id: Identifier
    severity: Literal["info", "warning", "blocking"]
    summary: Annotated[str, Field(min_length=1, max_length=5000)]


class ReviewerResultBase(ContractModel):
    """Common reviewer result fields, all bound to one immutable review target."""

    result_version: Literal[1]
    dispatch_id: Identifier
    attempt: Annotated[int, Field(ge=1, le=100)]
    step_id: Identifier
    repo_id: Identifier
    review_target: ReviewTarget
    findings: list[ReviewFinding]
    verification: list[VerificationResult]
    required_remediation: list[Annotated[str, Field(min_length=1, max_length=5000)]]
    summary: Annotated[str, Field(min_length=1, max_length=10_000)]
    transcript_ref: Identifier | None = None


class ReviewerAcceptedResult(ReviewerResultBase):
    """Reviewer accepted the exact target without blocking remediation."""

    verdict: Literal["accepted"]

    @model_validator(mode="after")
    def accepted_has_no_blockers(self) -> "ReviewerAcceptedResult":
        if self.required_remediation:
            raise ValueError("accepted review cannot require remediation")
        if any(finding.severity == "blocking" for finding in self.findings):
            raise ValueError("accepted review cannot contain blocking findings")
        return self


class ReviewerChangesRequestedResult(ReviewerResultBase):
    """Reviewer requested deterministic rework for the immutable target."""

    verdict: Literal["changes_requested"]

    @model_validator(mode="after")
    def changes_requested_has_remediation(self) -> "ReviewerChangesRequestedResult":
        if not self.required_remediation:
            raise ValueError("changes_requested review requires remediation")
        return self


class ReviewerBlockedResult(ReviewerResultBase):
    """Reviewer could not determine a valid verdict and includes blockers."""

    verdict: Literal["blocked"]
    blockers: list[Annotated[str, Field(min_length=1, max_length=5000)]]

    @model_validator(mode="after")
    def blocked_has_blockers(self) -> "ReviewerBlockedResult":
        if not self.blockers:
            raise ValueError("blocked review requires blockers")
        return self


class ReviewerInconclusiveResult(ReviewerResultBase):
    """Reviewer lacks sufficient evidence and records a specific reason."""

    verdict: Literal["inconclusive"]
    reason: Annotated[str, Field(min_length=1, max_length=5000)]


ReviewerResult: TypeAlias = Annotated[
    ReviewerAcceptedResult
    | ReviewerChangesRequestedResult
    | ReviewerBlockedResult
    | ReviewerInconclusiveResult,
    Field(discriminator="verdict"),
]
_REVIEWER_RESULT_ADAPTER: TypeAdapter[ReviewerResult] = TypeAdapter(ReviewerResult)


class ResultExpectation(ContractModel):
    """Dispatcher-owned identity used before a result can change workflow state."""

    dispatch_id: Identifier
    attempt: Annotated[int, Field(ge=1, le=100)]
    step_id: Identifier
    repo_id: Identifier
    expected_review_target: ReviewTarget | None = None


def parse_executor_result(payload: object) -> ExecutorResult:
    """Validate an executor result object against the schema-v1 union."""
    try:
        return _EXECUTOR_RESULT_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise ResultError(_format_validation_error("executor", exc)) from exc


def parse_reviewer_result(payload: object) -> ReviewerResult:
    """Validate a reviewer result object against the schema-v1 union."""
    try:
        return _REVIEWER_RESULT_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise ResultError(_format_validation_error("reviewer", exc)) from exc


def validate_executor_result_context(
    result: ExecutorResult,
    expected: ResultExpectation,
) -> None:
    """Reject stale, wrong-step, wrong-attempt, or wrong-repository executor results."""
    _validate_result_identity(result.dispatch_id, result.attempt, result.step_id, result.repository.repo_id, expected)


def validate_reviewer_result_context(
    result: ReviewerResult,
    expected: ResultExpectation,
) -> None:
    """Reject a review result not bound to its dispatch and immutable review target."""
    _validate_result_identity(result.dispatch_id, result.attempt, result.step_id, result.repo_id, expected)
    if expected.expected_review_target is None:
        raise ResultError("review result received for a dispatch without an expected review target")
    if result.review_target != expected.expected_review_target:
        raise ResultError("review result target does not match the immutable dispatch target")


def _validate_result_identity(
    dispatch_id: str,
    attempt: int,
    step_id: str,
    repo_id: str,
    expected: ResultExpectation,
) -> None:
    mismatches = []
    for field, actual, expected_value in (
        ("dispatch_id", dispatch_id, expected.dispatch_id),
        ("attempt", attempt, expected.attempt),
        ("step_id", step_id, expected.step_id),
        ("repo_id", repo_id, expected.repo_id),
    ):
        if actual != expected_value:
            mismatches.append(f"{field} expected {expected_value!r}, received {actual!r}")
    if mismatches:
        raise ResultError("result does not match active dispatch: " + "; ".join(mismatches))


def _format_validation_error(kind: str, exc: ValidationError) -> str:
    errors = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        errors.append(f"{location}: {error['msg']}")
    return f"invalid {kind} result: " + "; ".join(errors)
