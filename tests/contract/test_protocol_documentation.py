from __future__ import annotations

import re
from pathlib import Path

from dispatcher.protocol import parse_supervisor_command

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_protocol_json_examples_validate_against_runtime_schema() -> None:
    document = (PROJECT_ROOT / "docs" / "protocol.md").read_text(encoding="utf-8")
    examples = re.findall(r"```json\n(.*?)\n```", document, flags=re.DOTALL)

    assert examples
    for example in examples:
        parse_supervisor_command(example)
