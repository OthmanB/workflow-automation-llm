"""Read-only historical observations, explicit decisions, and baseline-backed run hydration."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .config import Config, ContractModel, Identifier
from .plan import NormalizedPlan, Sha256, verify_plan_sources
from .policy import compile_run_policy
from .state_store import StateStore
from .workflow import RunRecord, StepStatus, TransitionEvent, transition_step

BASELINE_IMPORTER_VERSION: Literal["baseline-v2"] = "baseline-v2"


class BaselineError(ValueError):
    """Historical state cannot be safely inspected, decided, or hydrated."""


class BaselineEvidence(ContractModel):
    """One immutable artifact observed during a read-only historical inspection."""

    artifact_id: Identifier
    relative_path: str
    sha256: Sha256
    size_bytes: int


class BaselineStepObservation(ContractModel):
    """Observed repository/evidence facts; never an inferred completion decision."""

    step_id: Identifier
    repo_id: Identifier
    repository_revision: str | None
    evidence: tuple[BaselineEvidence, ...]
    review_evidence: tuple[BaselineEvidence, ...]
    gaps: tuple[str, ...]

    @field_validator("evidence", "review_evidence", "gaps", mode="before")
    @classmethod
    def freeze_values(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class BaselineObservation(ContractModel):
    """A non-authoritative observation set that must be explicitly decided."""

    importer_version: Literal["baseline-v2"]
    project_id: Identifier
    plan_digest: Sha256
    source_digest: Sha256
    inspected_at: datetime
    steps: tuple[BaselineStepObservation, ...]

    @field_validator("steps", mode="before")
    @classmethod
    def freeze_steps(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @property
    def observation_digest(self) -> str:
        payload = self.model_dump(mode="json")
        del payload["inspected_at"]
        return _digest(payload)


class BaselineDecision(ContractModel):
    """One operator-owned historical disposition for exactly one plan step."""

    step_id: Identifier
    state: Literal["PENDING", "ACCEPTED", "WAIVED"]
    reason: str = Field(min_length=1, max_length=5000)
    operator_decision_ref: Identifier
    accepted_reviewer_role_keys: tuple[Identifier, ...] = ()

    @field_validator("accepted_reviewer_role_keys", mode="before")
    @classmethod
    def freeze_reviewers(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_state_details(self) -> "BaselineDecision":
        if len(self.accepted_reviewer_role_keys) != len(set(self.accepted_reviewer_role_keys)):
            raise ValueError("accepted_reviewer_role_keys must not contain duplicates")
        if self.state != "ACCEPTED" and self.accepted_reviewer_role_keys:
            raise ValueError("only accepted baseline steps may name accepted reviewers")
        return self


class BaselineApproval(ContractModel):
    """Immutable observation plus every explicit decision approved for one plan digest."""

    observation: BaselineObservation
    decisions: tuple[BaselineDecision, ...]
    approval_decision_ref: Identifier
    approved_at: datetime

    @field_validator("decisions", mode="before")
    @classmethod
    def freeze_decisions(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_coverage(self) -> "BaselineApproval":
        observed = {step.step_id for step in self.observation.steps}
        decided = [decision.step_id for decision in self.decisions]
        if set(decided) != observed or len(decided) != len(set(decided)):
            raise ValueError("baseline approval requires exactly one decision for every observed step")
        return self

    @property
    def approval_digest(self) -> str:
        payload = self.model_dump(mode="json")
        del payload["approved_at"]
        return _digest(payload)


def inspect_baseline(plan: NormalizedPlan, config: Config) -> BaselineObservation:
    """Observe every historical step without inferring its acceptance state."""
    _verify_sources(plan, config)
    steps = []
    for step in plan.steps:
        repository = config.repository(step.repo_id)
        root = Path(repository.root)
        evidence, missing = _inspect_evidence(root, repository.evidence_roots, step, review=False)
        review_evidence, missing_review = _inspect_evidence(root, repository.evidence_roots, step, review=True)
        gaps = [
            "operator must explicitly decide PENDING, ACCEPTED, or WAIVED",
            *missing,
            *missing_review,
        ]
        steps.append(
            BaselineStepObservation(
                step_id=step.step_id,
                repo_id=step.repo_id,
                repository_revision=_git_revision(root),
                evidence=evidence,
                review_evidence=review_evidence,
                gaps=tuple(gaps),
            )
        )
    return BaselineObservation(
        importer_version=BASELINE_IMPORTER_VERSION,
        project_id=config.project_id,
        plan_digest=plan.plan_digest,
        source_digest=plan.source_digest,
        inspected_at=datetime.now(UTC),
        steps=tuple(steps),
    )


def approve_baseline(
    observation: BaselineObservation,
    *,
    decisions: tuple[BaselineDecision, ...],
    plan: NormalizedPlan,
    config: Config,
    store: StateStore,
    approval_decision_ref: str,
) -> BaselineApproval:
    """Validate every historical decision against evidence, review, policy, and source hashes."""
    _validate_observation(observation, plan, config)
    approval = BaselineApproval(
        observation=observation,
        decisions=decisions,
        approval_decision_ref=approval_decision_ref,
        approved_at=datetime.now(UTC),
    )
    obligations = compile_run_policy(config, plan).review_obligations
    observations = {item.step_id: item for item in observation.steps}
    plan_steps = {step.step_id: step for step in plan.steps}
    for decision in approval.decisions:
        step = plan_steps[decision.step_id]
        observed = observations[decision.step_id]
        if decision.state != "ACCEPTED":
            continue
        required_artifacts = {item.artifact_id for item in step.evidence_requirements}
        observed_artifacts = {item.artifact_id for item in observed.evidence}
        if not required_artifacts.issubset(observed_artifacts):
            raise BaselineError(f"accepted baseline step {step.step_id} lacks required evidence")
        obligation = obligations[step.step_id]
        if obligation.required:
            if not observed.review_evidence:
                raise BaselineError(f"accepted baseline step {step.step_id} lacks required review proof")
            accepted_roles = set(decision.accepted_reviewer_role_keys)
            if not accepted_roles.issubset(set(obligation.reviewer_role_keys)):
                raise BaselineError(f"accepted baseline step {step.step_id} names an unconfigured reviewer")
            if len(accepted_roles) < obligation.required_acceptances:
                raise BaselineError(f"accepted baseline step {step.step_id} lacks required reviewer decisions")
        elif decision.accepted_reviewer_role_keys:
            raise BaselineError(f"baseline step {step.step_id} has reviewers but no compiled review obligation")
    store.save_baseline(
        project_id=config.project_id,
        plan_digest=plan.plan_digest,
        source_digest=plan.source_digest,
        candidate=approval.model_dump(mode="json"),
        operator_decision_ref=approval_decision_ref,
    )
    return approval


def validate_approved_baseline(
    *,
    plan: NormalizedPlan,
    config: Config,
    store: StateStore,
) -> BaselineApproval:
    """Reject an approved baseline if its source, repositories, or evidence changed."""
    stored = store.load_baseline(project_id=config.project_id, plan_digest=plan.plan_digest)
    if stored is None:
        raise BaselineError("no approved baseline exists for the active normalized plan")
    if stored["source_digest"] != plan.source_digest:
        raise BaselineError("approved baseline source digest no longer matches the active plan")
    try:
        approval = BaselineApproval.model_validate_json(json.dumps(stored["candidate"]))
    except ValueError as exc:
        raise BaselineError("stored baseline uses an unsupported historical approval format; inspect and approve again") from exc
    _validate_observation(approval.observation, plan, config)
    current = inspect_baseline(plan, config)
    if approval.observation.observation_digest != current.observation_digest:
        raise BaselineError("historical evidence changed since baseline approval; inspect and approve again")
    return approval


def hydrate_run_from_baseline(record: RunRecord, approval: BaselineApproval) -> RunRecord:
    """Apply approved historical decisions before run activation without inventing new evidence."""
    if record.project_id != approval.observation.project_id or record.plan_digest != approval.observation.plan_digest:
        raise BaselineError("baseline approval does not match the new run")
    observations = {item.step_id: item for item in approval.observation.steps}
    decisions = {item.step_id: item for item in approval.decisions}
    steps = dict(record.steps)
    sequence = record.sequence
    updated_at = record.updated_at
    for step in record.plan.steps:
        decision = decisions[step.step_id]
        observed = observations[step.step_id]
        sequence += 1
        event = TransitionEvent(
            event_id=f"baseline-{step.step_id}-{sequence}",
            sequence=sequence,
            actor="operator",
            reason=f"approved historical baseline decision: {decision.state}",
            correlation_id=decision.operator_decision_ref,
            occurred_at=datetime.now(UTC),
        )
        current = steps[step.step_id]
        if decision.state == "PENDING":
            steps[step.step_id] = current.model_copy(update={"last_event": event})
        elif decision.state == "WAIVED":
            steps[step.step_id] = transition_step(
                current,
                StepStatus.WAIVED,
                event,
                waiver_decision_ref=decision.operator_decision_ref,
            )
        else:
            steps[step.step_id] = current.model_copy(
                update={
                    "state": StepStatus.ACCEPTED,
                    "accepted_artifact_ids": [item.artifact_id for item in observed.evidence],
                    "review_acceptances": len(decision.accepted_reviewer_role_keys),
                    "accepted_reviewer_role_keys": list(decision.accepted_reviewer_role_keys),
                    "operator_gate_resolved": True,
                    "last_event": event,
                }
            )
        updated_at = event.occurred_at
    return record.model_copy(update={"steps": steps, "sequence": sequence, "updated_at": updated_at})


def _validate_observation(observation: BaselineObservation, plan: NormalizedPlan, config: Config) -> None:
    if observation.project_id != config.project_id:
        raise BaselineError("baseline observation project does not match configured project")
    if observation.plan_digest != plan.plan_digest or observation.source_digest != plan.source_digest:
        raise BaselineError("baseline observation no longer matches normalized plan or source digests")
    if {step.step_id for step in observation.steps} != {step.step_id for step in plan.steps}:
        raise BaselineError("baseline observation must cover every normalized plan step")
    _verify_sources(plan, config)


def _verify_sources(plan: NormalizedPlan, config: Config) -> None:
    try:
        verify_plan_sources(plan, config)
    except Exception as exc:
        raise BaselineError(f"cannot inspect baseline with stale plan sources: {exc}") from exc


def _inspect_evidence(
    root: Path,
    evidence_roots: list[str],
    step: object,
    *,
    review: bool,
) -> tuple[tuple[BaselineEvidence, ...], list[str]]:
    from .plan import PlanStep

    if not isinstance(step, PlanStep):
        raise TypeError("step must be a PlanStep")
    if review:
        return _inspect_review_proof(root, evidence_roots, step.step_id)
    else:
        requirements = step.evidence_requirements
        label = "required evidence"
    observed: list[BaselineEvidence] = []
    missing: list[str] = []
    for requirement in requirements:
        matches = [root / evidence_root / requirement.relative_path for evidence_root in evidence_roots]
        existing = next((path for path in matches if path.is_file()), None)
        if existing is None:
            missing.append(f"missing {label} {requirement.artifact_id}")
            continue
        observed.append(
            BaselineEvidence(
                artifact_id=requirement.artifact_id,
                relative_path=str(existing.relative_to(root)),
                sha256=hashlib.sha256(existing.read_bytes()).hexdigest(),
                size_bytes=existing.stat().st_size,
            )
        )
    return tuple(observed), missing


def _inspect_review_proof(
    root: Path,
    evidence_roots: list[str],
    step_id: str,
) -> tuple[tuple[BaselineEvidence, ...], list[str]]:
    observed: list[BaselineEvidence] = []
    for evidence_root in evidence_roots:
        review_root = root / evidence_root / "reviews"
        if not review_root.is_dir():
            continue
        for index, path in enumerate(sorted(review_root.glob(f"{step_id}.*")), start=1):
            if not path.is_file():
                continue
            observed.append(
                BaselineEvidence(
                    artifact_id=f"review-proof-{step_id}-{index}",
                    relative_path=str(path.relative_to(root)),
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    size_bytes=path.stat().st_size,
                )
            )
    if observed:
        return tuple(observed), []
    return (), [f"missing review proof for {step_id}"]


def _git_revision(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
