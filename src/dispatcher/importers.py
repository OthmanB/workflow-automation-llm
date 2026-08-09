"""Optional source-format adapters that produce explicit normalized-plan sidecars."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .config import Config
from .plan import NormalizedPlan, PlanError, load_normalized_plan

_TIER2_STEP_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|.*\|$")
_BACKTICK_VALUE = re.compile(r"`([^`]+)`")


def import_tier2_markdown(
    markdown_path: str | Path,
    sidecar_path: str | Path,
    config: Config,
) -> NormalizedPlan:
    """Validate a Tier 2 table against an explicit sidecar without guessing work.

    Tier 2 terminology and table parsing are contained in this reference
    adapter. All authorization, evidence, retry, review, and dependency fields
    still come from the generic YAML sidecar and remain required.
    """
    markdown = Path(markdown_path).expanduser().resolve()
    if not markdown.is_file():
        raise PlanError(f"Tier 2 Markdown source not found: {markdown}")
    plan = load_normalized_plan(sidecar_path, config)
    rows = _tier2_step_rows(markdown, config)
    plan_steps = {step.step_id: step for step in plan.steps}
    if set(rows) != set(plan_steps):
        missing = sorted(set(rows) - set(plan_steps))
        extra = sorted(set(plan_steps) - set(rows))
        raise PlanError(
            f"Tier 2 sidecar steps differ from Markdown table: missing={missing}, extra={extra}"
        )
    for step_id, (title, repo_id) in rows.items():
        step = plan_steps[step_id]
        if step.title != title:
            raise PlanError(
                f"Tier 2 sidecar title mismatch for {step_id}: expected {title!r}, found {step.title!r}"
            )
        if step.repo_id != repo_id:
            raise PlanError(
                f"Tier 2 sidecar repository mismatch for {step_id}: "
                f"expected {repo_id!r}, found {step.repo_id!r}"
            )
    source_hash = hashlib.sha256(markdown.read_bytes()).hexdigest()
    markdown_sources = [
        source
        for source in plan.sources
        if source.root == "plans" and source.relative_path == markdown.name
    ]
    if len(markdown_sources) != 1 or markdown_sources[0].sha256 != source_hash:
        raise PlanError("Tier 2 sidecar must include the exact Markdown source hash")
    return plan


def _tier2_step_rows(markdown: Path, config: Config) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    in_step_table = False
    for line in markdown.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("| Step | Scope (repo) |"):
            in_step_table = True
            continue
        if not in_step_table:
            continue
        if not line.startswith("|"):
            break
        if line.replace("|", "").replace("-", "").strip() == "":
            continue
        match = _TIER2_STEP_ROW.match(line)
        if match is None:
            raise PlanError(f"invalid Tier 2 step-table row: {line}")
        step_id = match.group(1).strip()
        scope = match.group(2).strip()
        repository_values = _BACKTICK_VALUE.findall(scope)
        repo_id = next(
            (value for value in reversed(repository_values) if value in config.model.repositories),
            None,
        )
        if repo_id is None:
            raise PlanError(f"Tier 2 step {step_id} does not reference a registered repository")
        title = scope.split(" — `", maxsplit=1)[0].strip()
        if step_id in rows:
            raise PlanError(f"duplicate Tier 2 step-table row: {step_id}")
        rows[step_id] = (title, repo_id)
    if not rows:
        raise PlanError("Tier 2 Markdown has no Step | Scope (repo) table")
    return rows
