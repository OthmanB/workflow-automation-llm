"""Read-only historical baseline inspection and explicit operator approval."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import field_validator

from .config import Config, ContractModel, Identifier
from .plan import NormalizedPlan, Sha256, verify_plan_sources
from .state_store import StateStore

BASELINE_IMPORTER_VERSION: Literal["baseline-v1"] = "baseline-v1"


class BaselineError(ValueError):
    """Historical state cannot be safely inspected or approved."""


class BaselineEvidence(ContractModel):
    """One immutable evidence file observed during a read-only inspection."""

    relative_path: str
    sha256: Sha256
    size_bytes: int


class BaselineStep(ContractModel):
    """Independently justified historical proposal for a normalized plan step."""

    step_id: Identifier
    repo_id: Identifier
    proposed_state: Literal["PENDING", "ACCEPTED", "WAIVED"]
    repository_revision: str | None
    evidence: tuple[BaselineEvidence, ...]
    review_evidence: tuple[BaselineEvidence, ...]
    gaps: tuple[str, ...]

    @field_validator("evidence", "review_evidence", "gaps", mode="before")
    @classmethod
    def freeze_values(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class BaselineCandidate(ContractModel):
    """A non-authoritative inspection result that needs an operator approval."""

    importer_version: Literal["baseline-v1"]
    project_id: Identifier
    plan_digest: Sha256
    source_digest: Sha256
    inspected_at: datetime
    steps: tuple[BaselineStep, ...]

    @field_validator("steps", mode="before")
    @classmethod
    def freeze_steps(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @property
    def candidate_digest(self) -> str:
        payload = self.model_dump(mode="json")
        del payload["inspected_at"]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def inspect_baseline(plan: NormalizedPlan, config: Config) -> BaselineCandidate:
    """Inspect each step independently without mutating repositories or state."""
    try:
        verify_plan_sources(plan, config)
    except Exception as exc:
        raise BaselineError(f"cannot inspect baseline with stale plan sources: {exc}") from exc
    steps = []
    for step in plan.steps:
        repository = config.repository(step.repo_id)
        root = Path(repository.root)
        evidence, missing = _inspect_evidence(root, repository.evidence_roots, step)
        revision = _git_revision(root)
        gaps = [
            "historical acceptance is not inferred without an operator-approved baseline decision",
            "independent review evidence is not available through the generic baseline importer",
        ]
        gaps.extend(missing)
        steps.append(
            BaselineStep(
                step_id=step.step_id,
                repo_id=step.repo_id,
                proposed_state="PENDING",
                repository_revision=revision,
                evidence=evidence,
                review_evidence=(),
                gaps=tuple(gaps),
            )
        )
    return BaselineCandidate(
        importer_version=BASELINE_IMPORTER_VERSION,
        project_id=config.project_id,
        plan_digest=plan.plan_digest,
        source_digest=plan.source_digest,
        inspected_at=datetime.now(UTC),
        steps=tuple(steps),
    )


def approve_baseline(
    candidate: BaselineCandidate,
    *,
    plan: NormalizedPlan,
    config: Config,
    store: StateStore,
    operator_decision_ref: str,
) -> None:
    """Persist a baseline only when it exactly matches the active approved plan sources."""
    if candidate.project_id != config.project_id:
        raise BaselineError("baseline candidate project does not match configured project")
    if candidate.plan_digest != plan.plan_digest or candidate.source_digest != plan.source_digest:
        raise BaselineError("baseline candidate no longer matches the normalized plan or source digests")
    try:
        verify_plan_sources(plan, config)
    except Exception as exc:
        raise BaselineError(f"cannot approve baseline with stale plan sources: {exc}") from exc
    store.save_baseline(
        project_id=config.project_id,
        plan_digest=plan.plan_digest,
        source_digest=plan.source_digest,
        candidate=candidate.model_dump(mode="json"),
        operator_decision_ref=operator_decision_ref,
    )


def validate_approved_baseline(
    *,
    plan: NormalizedPlan,
    config: Config,
    store: StateStore,
) -> BaselineCandidate:
    """Reject a stored baseline if current plan sources or historical evidence changed."""
    stored = store.load_baseline(project_id=config.project_id, plan_digest=plan.plan_digest)
    if stored is None:
        raise BaselineError("no approved baseline exists for the active normalized plan")
    if stored["source_digest"] != plan.source_digest:
        raise BaselineError("approved baseline source digest no longer matches the active plan")
    candidate = BaselineCandidate.model_validate_json(json.dumps(stored["candidate"]))
    current = inspect_baseline(plan, config)
    if candidate.candidate_digest != current.candidate_digest:
        raise BaselineError("historical evidence changed since baseline approval; inspect and approve again")
    return candidate


def _inspect_evidence(root: Path, evidence_roots: list[str], step: object) -> tuple[tuple[BaselineEvidence, ...], list[str]]:
    from .plan import PlanStep

    if not isinstance(step, PlanStep):
        raise TypeError("step must be a PlanStep")
    observed: list[BaselineEvidence] = []
    missing: list[str] = []
    for requirement in step.evidence_requirements:
        matches = [root / evidence_root / requirement.relative_path for evidence_root in evidence_roots]
        existing = next((path for path in matches if path.is_file()), None)
        if existing is None:
            missing.append(f"missing required evidence {requirement.artifact_id}")
            continue
        observed.append(
            BaselineEvidence(
                relative_path=str(existing.relative_to(root)),
                sha256=hashlib.sha256(existing.read_bytes()).hexdigest(),
                size_bytes=existing.stat().st_size,
            )
        )
    return tuple(observed), missing


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
