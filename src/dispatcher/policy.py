"""Compile selected Phase 6 profile and plan policy into immutable run obligations."""

from __future__ import annotations

import hashlib
import json

from .config import Config
from .plan import NormalizedPlan, PlanStep
from .workflow import CompiledReviewObligation, RunPolicy


class PolicyError(ValueError):
    """A configured policy cannot produce a safe executable run obligation."""


def compile_run_policy(config: Config, plan: NormalizedPlan) -> RunPolicy:
    """Compile one selected profile and plan into immutable per-step review obligations."""
    obligations: dict[str, CompiledReviewObligation] = {}
    for step in plan.steps:
        obligations[step.step_id] = _compile_review_obligation(config, step)
    payload = {
        "profile_id": config.profile_id,
        "profile_digest": config.profile_digest,
        "underspec_mode": config.execution.underspec_mode,
        "review_obligations": {
            step_id: obligation.model_dump(mode="json") for step_id, obligation in obligations.items()
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return RunPolicy(
        profile_id=config.profile_id,
        profile_digest=config.profile_digest,
        review_obligations=obligations,
        underspec_mode=config.execution.underspec_mode,
        policy_digest=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


def _compile_review_obligation(config: Config, step: PlanStep) -> CompiledReviewObligation:
    profile = config.profiles.profiles[config.profile_id]
    review_policy = config.model.review_policy
    critical = bool(set(step.risk_tags) & set(review_policy.critical_risk_tags))
    profile_requires_review = profile.review_schedule == "always" or (
        profile.review_schedule == "critical" and critical
    )
    mandatory = step.review.required or review_policy.mandatory_review
    required = mandatory or profile_requires_review
    profile_multireview = profile.multi_review == "on_every_review" or (
        profile.multi_review == "on_critical_only" and critical
    )
    roles = list(step.review.reviewer_role_keys)
    if profile_requires_review or review_policy.mandatory_review or profile_multireview:
        for role_key in profile.reviewer_role_keys:
            if role_key not in roles:
                roles.append(role_key)
    required_acceptances = step.review.required_acceptances
    if profile_requires_review or review_policy.mandatory_review or profile_multireview:
        required_acceptances = max(required_acceptances, profile.required_acceptances)
    if not required:
        roles = []
        required_acceptances = 0
    if required and not roles:
        raise PolicyError(f"step {step.step_id} requires review but has no configured reviewers")
    if required_acceptances > len(roles):
        raise PolicyError(
            f"step {step.step_id} requires {required_acceptances} acceptances but has {len(roles)} reviewers"
        )
    if required_acceptances > step.retry.max_reviewer_attempts:
        raise PolicyError(
            f"step {step.step_id} retry.max_reviewer_attempts cannot satisfy compiled review obligation"
        )
    return CompiledReviewObligation(
        step_id=step.step_id,
        required=required,
        reviewer_role_keys=tuple(roles),
        required_acceptances=required_acceptances,
        independence="fresh_session",
        waivable=review_policy.allow_operator_waiver and not mandatory,
        source_policy_digest=config.profile_digest,
    )
