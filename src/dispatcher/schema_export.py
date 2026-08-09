"""Generate checked JSON Schema documents from schema-v1 contract models."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from .config import ProjectConfigModel
from .plan import NormalizedPlan
from .protocol import SupervisorCommand
from .results import ExecutorResult, ReviewerResult
from .workflow import RunRecord


def schema_documents() -> dict[str, dict[str, Any]]:
    """Return every published schema-v1 document from its executable model."""
    return {
        "project-config-v1.json": ProjectConfigModel.model_json_schema(),
        "normalized-plan-v1.json": NormalizedPlan.model_json_schema(),
        "supervisor-command-v1.json": TypeAdapter(SupervisorCommand).json_schema(),
        "executor-result-v1.json": TypeAdapter(ExecutorResult).json_schema(),
        "reviewer-result-v1.json": TypeAdapter(ReviewerResult).json_schema(),
        "workflow-state-v1.json": RunRecord.model_json_schema(),
    }


def write_schema_documents(output_dir: str | Path) -> list[Path]:
    """Write deterministic schemas for review, tooling, and CI comparison."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, schema in schema_documents().items():
        path = directory / name
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        paths.append(path)
    return paths
