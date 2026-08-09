from __future__ import annotations

import json
from pathlib import Path

from dispatcher.schema_export import schema_documents

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_checked_schema_documents_match_executable_models() -> None:
    for filename, generated in schema_documents().items():
        published = json.loads((PROJECT_ROOT / "schemas" / filename).read_text(encoding="utf-8"))

        assert published == generated
