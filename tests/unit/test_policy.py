from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from helpers import config_values, create_fixture_project, valid_plan_values, write_config

from dispatcher.config import ConfigError
from dispatcher.plan import NormalizedPlan
from dispatcher.policy import PolicyError, compile_run_policy


def test_mandatory_review_cannot_be_weakened_by_the_selected_profile(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["review_policy"]["mandatory_review"] = True
    plan_values = valid_plan_values(project)
    plan_values["steps"][0]["retry"]["max_reviewer_attempts"] = 2
    config = write_config(project, values)

    policy = compile_run_policy(config, NormalizedPlan.model_validate(plan_values))
    obligation = policy.review_obligations["prepare-fixture"]

    assert obligation.required
    assert obligation.required_acceptances == 2
    assert obligation.reviewer_role_keys == ("reviewer", "reviewer-two")
    assert not obligation.waivable


def test_thorough_profile_compiles_two_fresh_reviews_for_every_step(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    profiles = yaml.safe_load(project.profiles_path.read_text(encoding="utf-8"))
    profiles["profiles"]["thorough"] = {
        "review_schedule": "always",
        "multi_review": "on_every_review",
        "reviewer_role_keys": ["reviewer", "reviewer-two"],
        "required_acceptances": 2,
    }
    project.profiles_path.write_text(yaml.safe_dump(profiles, sort_keys=False), encoding="utf-8")
    values = config_values(project)
    values["profile"]["profile_id"] = "thorough"
    plan_values = valid_plan_values(project)
    plan_values["steps"][0]["retry"]["max_reviewer_attempts"] = 2
    config = write_config(project, values)

    obligation = compile_run_policy(config, NormalizedPlan.model_validate(plan_values)).review_obligations[
        "prepare-fixture"
    ]

    assert obligation.required
    assert obligation.required_acceptances == 2
    assert obligation.independence == "fresh_session"


def test_profile_requirement_exceeding_plan_reviewer_attempts_fails_closed(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    profiles = yaml.safe_load(project.profiles_path.read_text(encoding="utf-8"))
    profiles["profiles"]["thorough"] = {
        "review_schedule": "always",
        "multi_review": "on_every_review",
        "reviewer_role_keys": ["reviewer", "reviewer-two"],
        "required_acceptances": 2,
    }
    project.profiles_path.write_text(yaml.safe_dump(profiles, sort_keys=False), encoding="utf-8")
    values = config_values(project)
    values["profile"]["profile_id"] = "thorough"
    config = write_config(project, values)

    with pytest.raises(PolicyError, match="max_reviewer_attempts"):
        compile_run_policy(config, NormalizedPlan.model_validate(valid_plan_values(project)))


def test_economy_and_balanced_profiles_apply_only_their_declared_review_scope(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    profiles = yaml.safe_load(project.profiles_path.read_text(encoding="utf-8"))
    profiles["profiles"]["economy"] = {
        "review_schedule": "on_failure",
        "multi_review": "off",
        "reviewer_role_keys": ["reviewer"],
        "required_acceptances": 1,
    }
    project.profiles_path.write_text(yaml.safe_dump(profiles, sort_keys=False), encoding="utf-8")
    plan_values = valid_plan_values(project)
    plan_values["steps"][0]["retry"]["max_reviewer_attempts"] = 2
    plan_values["steps"][0]["risk_tags"] = ["critical"]
    plan = NormalizedPlan.model_validate(plan_values)

    balanced_values = config_values(project)
    balanced = write_config(project, balanced_values)
    balanced_obligation = compile_run_policy(balanced, plan).review_obligations["prepare-fixture"]

    economy_values = config_values(project)
    economy_values["profile"]["profile_id"] = "economy"
    economy = write_config(project, economy_values)
    economy_obligation = compile_run_policy(economy, plan).review_obligations["prepare-fixture"]

    assert balanced_obligation.required_acceptances == 2
    assert economy_obligation.required is False
    assert economy_obligation.required_acceptances == 0


def test_multi_review_off_rejects_multiple_required_acceptances_at_config_load(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    profiles = yaml.safe_load(project.profiles_path.read_text(encoding="utf-8"))
    profiles["profiles"]["balanced"]["multi_review"] = "off"

    project.profiles_path.write_text(yaml.safe_dump(profiles, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigError, match="multi_review off"):
        write_config(project, config_values(project))
