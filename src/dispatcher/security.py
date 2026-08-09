"""Redaction and owner-only filesystem helpers for dispatcher runtime data."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

OWNER_DIRECTORY_MODE = 0o700
OWNER_FILE_MODE = 0o600

_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "cookie",
        "credential",
        "password",
        "secret",
        "token",
    }
)
_ASSIGNMENT_PATTERN = re.compile(
    r"(?ix)\b(?:api[_-]?key|authorization|cookie|credential|password|secret|token)\b"
    r"\s*[:=]\s*(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_URL_CREDENTIAL_PATTERN = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^\s/@:]+):([^\s/@]+)@")


def ensure_private_directory(path: str | Path) -> Path:
    """Create ``path`` and enforce owner-only directory permissions."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True, mode=OWNER_DIRECTORY_MODE)
    directory.chmod(OWNER_DIRECTORY_MODE)
    return directory


def redact_text(value: str) -> str:
    """Redact common credential forms before persistence or logging."""
    value = _URL_CREDENTIAL_PATTERN.sub(r"\1[REDACTED]@", value)
    value = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    return _ASSIGNMENT_PATTERN.sub(lambda match: _redact_assignment(match.group(0)), value)


def redact_value(value: Any) -> Any:
    """Recursively redact values whose keys or text look credential-bearing."""
    if isinstance(value, dict):
        return {
            key: _redact_mapping_item(str(key), item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def write_private_text(path: str | Path, content: str, *, append: bool = False) -> None:
    """Write UTF-8 content to an owner-only file."""
    target = Path(path)
    ensure_private_directory(target.parent)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    descriptor = os.open(target, flags, OWNER_FILE_MODE)
    os.chmod(target, OWNER_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def write_private_text_exclusive(path: str | Path, content: str) -> None:
    """Create an owner-only UTF-8 file without ever replacing an existing artifact."""
    target = Path(path)
    ensure_private_directory(target.parent)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, OWNER_FILE_MODE)
    os.chmod(target, OWNER_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def open_private_text(path: str | Path, *, append: bool = True) -> TextIO:
    """Open an owner-only UTF-8 file for streaming writes."""
    target = Path(path)
    ensure_private_directory(target.parent)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    descriptor = os.open(target, flags, OWNER_FILE_MODE)
    os.chmod(target, OWNER_FILE_MODE)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def atomic_write_private_text(path: str | Path, content: str) -> None:
    """Atomically replace an owner-only UTF-8 runtime artifact."""
    target = Path(path)
    directory = ensure_private_directory(target.parent)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=directory,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    try:
        temporary.chmod(OWNER_FILE_MODE)
        temporary.replace(target)
        target.chmod(OWNER_FILE_MODE)
    finally:
        if temporary.exists():
            temporary.unlink()


def private_json_text(value: Mapping[str, Any] | list[Any]) -> str:
    """Serialize a redacted JSON value for a private runtime artifact."""
    return json.dumps(redact_value(value), ensure_ascii=False, indent=2)


def _redact_assignment(value: str) -> str:
    separator = ":" if ":" in value and "=" not in value else "="
    return f"{value.split(separator, 1)[0]}{separator}[REDACTED]"


def _redact_mapping_item(key: str, value: Any) -> Any:
    lowered = key.lower()
    if lowered in _SECRET_FIELD_NAMES:
        return "[REDACTED]"
    if lowered == "authorization" and isinstance(value, str):
        return "[REDACTED]"
    return redact_value(value)
