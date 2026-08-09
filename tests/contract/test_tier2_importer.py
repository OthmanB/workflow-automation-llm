from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml
from helpers import create_fixture_project, valid_plan_values

from dispatcher.importers import import_tier2_markdown
from dispatcher.plan import PlanError


def test_tier2_adapter_requires_an_explicit_complete_sidecar(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    markdown = project.plans / "tier-2-reference.md"
    markdown.write_text(
        """\
# Tier 2 Reference

## Step-By-Step Plan

| Step | Scope (repo) | Spec source(s) | Multi-review? | Exit evidence |
|---|---|---|---|---|
| prepare-fixture | Fixture task — `fixture-repo` | fixture | No | fixture evidence |
""",
        encoding="utf-8",
    )
    values = valid_plan_values(project)
    values["sources"][0].update(
        {
            "source_id": "tier2-reference",
            "relative_path": markdown.name,
            "sha256": hashlib.sha256(markdown.read_bytes()).hexdigest(),
        }
    )
    values["steps"][0]["title"] = "Fixture task"
    sidecar = project.root / "tier2-sidecar.yaml"
    sidecar.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")

    plan = import_tier2_markdown(markdown, sidecar, project.config)

    assert plan.steps[0].step_id == "prepare-fixture"
    assert plan.steps[0].repo_id == "fixture-repo"


def test_tier2_adapter_rejects_sidecar_drift_from_table(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    markdown = project.plans / "tier-2-reference.md"
    markdown.write_text(
        """\
## Step-By-Step Plan

| Step | Scope (repo) | Spec source(s) | Multi-review? | Exit evidence |
|---|---|---|---|---|
| prepare-fixture | Fixture task — `fixture-repo` | fixture | No | fixture evidence |
""",
        encoding="utf-8",
    )
    values = valid_plan_values(project)
    values["sources"][0].update(
        {
            "relative_path": markdown.name,
            "sha256": hashlib.sha256(markdown.read_bytes()).hexdigest(),
        }
    )
    values["steps"][0]["title"] = "Different title"
    sidecar = project.root / "tier2-sidecar.yaml"
    sidecar.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")

    with pytest.raises(PlanError, match="title mismatch"):
        import_tier2_markdown(markdown, sidecar, project.config)
