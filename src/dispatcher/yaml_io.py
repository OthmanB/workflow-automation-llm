"""Strict YAML loading for dispatcher-controlled configuration documents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class DuplicateYamlKeyError(ValueError):
    """A YAML mapping repeats a key that would otherwise be silently overwritten."""


class UniqueKeyLoader(yaml.SafeLoader):
    """PyYAML loader that rejects duplicate mapping keys at every nesting level."""


def _construct_unique_mapping(
    loader: yaml.Loader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DuplicateYamlKeyError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_unique_yaml(path: str | Path) -> Any:
    """Load YAML while preserving duplicate-key errors for callers."""
    return yaml.load(Path(path).read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
