from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_readme_links_current_normative_guides() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for document in (
        "docs/compatibility.md",
        "docs/config-schema.md",
        "docs/normalized-plan-schema.md",
        "docs/protocol.md",
        "docs/workflow-state-schema.md",
        "docs/operations.md",
        "docs/migration.md",
    ):
        assert (PROJECT_ROOT / document).is_file()
        assert document in readme


def test_operations_document_every_supported_cli_command_and_unsupported_lifecycle() -> None:
    operations = (PROJECT_ROOT / "docs" / "operations.md").read_text(encoding="utf-8")

    for command in (
        "dispatcher run",
        "dispatcher preflight",
        "dispatcher start",
        "dispatcher status",
        "dispatcher resume",
        "dispatcher recover",
        "dispatcher answer",
        "dispatcher support",
        "dispatcher prune",
        "dispatcher baseline",
    ):
        assert command in operations
    assert "`cancel` records the request before signalling" in operations
    assert "no authoritative-state `archive` command" in operations
    assert "Never edit or delete the database manually" in operations


def test_current_docs_preserve_mock_only_and_private_migration_boundaries() -> None:
    combined = "\n".join(
        (PROJECT_ROOT / path).read_text(encoding="utf-8")
        for path in ("README.md", "docs/compatibility.md", "docs/migration.md", "docs/operations.md")
    )

    assert "real OpenCode" in combined
    assert "repository-mutating execution" in combined
    assert "Private reference migration" in combined
    assert "separately authorized" in combined
    assert "dispatcher.sqlite3` is authoritative" in combined


def test_legacy_templates_and_diagrams_are_explicitly_non_operational() -> None:
    root = PROJECT_ROOT

    assert "Legacy mock-loop template" in (root / "templates" / "bootstrap_supervisor.md").read_text(
        encoding="utf-8"
    )
    assert "Historical, unused legacy template" in (root / "templates" / "resume_context.md").read_text(
        encoding="utf-8"
    )
    assert "Historical diagram" in (root / "docs" / "diagrams" / "architecture.mmd").read_text(
        encoding="utf-8"
    )
    assert "Historical diagram" in (root / "docs" / "diagrams" / "loop.mmd").read_text(encoding="utf-8")
    assert "sanitized public schema-v1 reference" in (root / "config" / "projects" / "README.md").read_text(
        encoding="utf-8"
    )
