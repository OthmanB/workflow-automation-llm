"""Optional source-format adapters that produce explicit normalized-plan sidecars."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Mapping

import yaml
from pydantic import Field

from .config import Config, ContractModel, Identifier
from .plan import NormalizedPlan, PlanError, load_normalized_plan

_TIER2_STEP_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|.*\|$")
_BACKTICK_VALUE = re.compile(r"`([^`]+)`")


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate keys in the small project-local ownership document."""


def _construct_unique_mapping(loader: yaml.Loader, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate ownership-map key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class OwnershipMap(ContractModel):
    """Explicit project-local repository assignment for source rows without one."""

    schema_version: int = Field(strict=True, ge=1, le=1)
    project_id: Identifier
    step_repositories: dict[Identifier, Identifier]


def load_ownership_map(path: str | Path, config: Config) -> OwnershipMap:
    """Load a strict ownership map without applying any repository-side effects."""
    ownership_path = Path(path).expanduser().resolve()
    if not ownership_path.is_file():
        raise PlanError(f"ownership map not found: {ownership_path}")
    try:
        raw = yaml.load(ownership_path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
        ownership = OwnershipMap.model_validate(raw)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise PlanError(f"invalid ownership map: {exc}") from exc
    if ownership.project_id != config.project_id:
        raise PlanError("ownership map project_id does not match configured project")
    unknown_repositories = set(ownership.step_repositories.values()) - set(config.model.repositories)
    if unknown_repositories:
        raise PlanError(
            "ownership map references unregistered repositories: "
            + ", ".join(sorted(unknown_repositories))
        )
    return ownership


def import_tier2_markdown(
    markdown_path: str | Path,
    sidecar_path: str | Path,
    config: Config,
    ownership_map: OwnershipMap | Mapping[str, str] | None = None,
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
    rows = _tier2_step_rows(markdown, config, ownership_map)
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


def _tier2_step_rows(
    markdown: Path,
    config: Config,
    ownership_map: OwnershipMap | Mapping[str, str] | None,
) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str | None]] = {}
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
        title = _normalize_title(scope.split(" — `", maxsplit=1)[0].strip())
        if step_id in rows:
            raise PlanError(f"duplicate Tier 2 step-table row: {step_id}")
        rows[step_id] = (title, repo_id)
    if not rows:
        raise PlanError("Tier 2 Markdown has no Step | Scope (repo) table")
    mapped = (
        ownership_map.step_repositories
        if isinstance(ownership_map, OwnershipMap)
        else dict(ownership_map or {})
    )
    unknown_steps = set(mapped) - set(rows)
    if unknown_steps:
        raise PlanError("ownership map references unknown steps: " + ", ".join(sorted(unknown_steps)))
    resolved: dict[str, tuple[str, str]] = {}
    for step_id, (title, explicit_repo_id) in rows.items():
        mapped_repo_id = mapped.get(step_id)
        if explicit_repo_id is not None and mapped_repo_id is not None and explicit_repo_id != mapped_repo_id:
            raise PlanError(
                f"ownership map conflicts with Markdown repository for {step_id}: "
                f"{explicit_repo_id!r} != {mapped_repo_id!r}"
            )
        repo_id = explicit_repo_id or mapped_repo_id
        if repo_id is None:
            raise PlanError(
                f"Tier 2 step {step_id} does not reference a registered repository; "
                "add it to the explicit ownership map"
            )
        if repo_id not in config.model.repositories:
            raise PlanError(f"Tier 2 step {step_id} references unregistered repository {repo_id!r}")
        resolved[step_id] = (title, repo_id)
    return resolved


def _normalize_title(value: str) -> str:
    """Ignore Markdown inline-code presentation while preserving title text."""
    return re.sub(r"`([^`]*)`", r"\1", value)
