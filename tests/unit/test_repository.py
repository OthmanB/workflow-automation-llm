from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from helpers import FixtureProject, config_values, create_fixture_project, write_config

from dispatcher.repository import (
    RepositorySnapshot,
    RepositoryValidationError,
    inspect_repository,
    validate_executor_snapshot,
    validate_pending_executor_changes,
    validate_review_snapshot,
    working_patch_sha256,
)
from dispatcher.results import ReviewTarget, parse_executor_result


@pytest.fixture
def project(tmp_path: Path) -> FixtureProject:
    project = create_fixture_project(tmp_path)
    _commit_initial_tree(project.repository)
    return project


def test_manifest_records_create_modify_delete_and_rename(project: FixtureProject) -> None:
    before = inspect_repository(project.config, "fixture-repo", require_clean=True)

    (project.evidence / "fixture.md").write_text("updated evidence\n", encoding="utf-8")
    (project.evidence / "obsolete.md").unlink()
    (project.evidence / "rename-source.md").rename(project.evidence / "rename-destination.md")
    (project.evidence / "created.md").write_text("created evidence\n", encoding="utf-8")
    _git(project.repository, "add", "-A")

    after = inspect_repository(project.config, "fixture-repo", require_clean=False)

    assert before.manifest_sha256 != after.manifest_sha256
    assert {change.change_type for change in after.changes} >= {
        "created",
        "modified",
        "deleted",
        "renamed",
    }
    assert {entry.relative_path for entry in after.evidence} == {
        "evidence/created.md",
        "evidence/fixture.md",
        "evidence/rename-destination.md",
    }


def test_external_symlink_evidence_is_never_accepted(project: FixtureProject) -> None:
    outside = project.root / "outside.md"
    outside.write_text("outside content\n", encoding="utf-8")
    (project.evidence / "escaped.md").symlink_to(outside)
    _git(project.repository, "add", "evidence/escaped.md")
    _git(project.repository, "commit", "-m", "add escaped evidence")
    snapshot = inspect_repository(project.config, "fixture-repo", require_clean=True)

    with pytest.raises(RepositoryValidationError, match="unsupported symlink"):
        validate_executor_snapshot(
            project.config,
            coordinate=snapshot.dispatch_coordinate(),
            before=snapshot,
            after=snapshot,
            result=_executor_result(snapshot),
        )


def test_write_outside_registered_writable_roots_halts_acceptance(project: FixtureProject) -> None:
    values = config_values(project)
    repository = values["repositories"]["fixture-repo"]
    repository["writable_roots"] = ["src", "evidence"]
    repository["commit_policy"] = "prohibited"
    config = write_config(project, values)
    before = inspect_repository(config, "fixture-repo", require_clean=True)
    (project.repository / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
    after = inspect_repository(config, "fixture-repo", require_clean=False)
    result = _executor_result(
        before,
        result_revision=None,
        patch_sha256=working_patch_sha256(project.repository),
    )

    with pytest.raises(RepositoryValidationError, match="outside configured writable roots"):
        validate_executor_snapshot(
            config,
            coordinate=before.dispatch_coordinate(),
            before=before,
            after=after,
            result=result,
        )


def test_moving_repository_head_invalidates_review_target(project: FixtureProject) -> None:
    before = inspect_repository(project.config, "fixture-repo", require_clean=True)
    (project.repository / "src" / "value.txt").write_text("value=2\n", encoding="utf-8")
    _git(project.repository, "add", "src/value.txt")
    _git(project.repository, "commit", "-m", "move repository head")
    after = inspect_repository(project.config, "fixture-repo", require_clean=True)
    target = ReviewTarget(
        executor_dispatch_id="executor-dispatch",
        executor_attempt=1,
        result_revision=before.revision,
        patch_sha256=None,
        artifact_hashes=[entry.sha256 for entry in before.evidence],
    )

    with pytest.raises(RepositoryValidationError, match="review target no longer matches"):
        validate_review_snapshot(
            project.config,
            coordinate=before.dispatch_coordinate(),
            before=before,
            after=after,
            review_target=target,
        )


def test_wrong_branch_and_unclean_baseline_fail_before_dispatch(project: FixtureProject) -> None:
    _git(project.repository, "checkout", "-b", "unexpected-branch")

    with pytest.raises(RepositoryValidationError, match="branch mismatch"):
        inspect_repository(project.config, "fixture-repo", require_clean=True)

    _git(project.repository, "checkout", "main")
    (project.repository / "src" / "value.txt").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(RepositoryValidationError, match="must be clean before dispatch"):
        inspect_repository(project.config, "fixture-repo", require_clean=True)


def test_registered_nested_repository_directory_is_rejected(project: FixtureProject) -> None:
    (project.repository / "src" / "evidence").mkdir()
    values = config_values(project)
    values["repositories"]["fixture-repo"]["root"] = str(project.repository / "src")
    config = write_config(project, values)

    with pytest.raises(RepositoryValidationError, match="not the registered Git worktree"):
        inspect_repository(config, "fixture-repo", require_clean=True)


def test_external_root_change_is_not_attributed_to_the_dispatch(project: FixtureProject) -> None:
    external = project.root / "external-watch"
    external.mkdir()
    external_file = external / "state.txt"
    external_file.write_text("before\n", encoding="utf-8")
    values = config_values(project)
    values["repositories"]["fixture-repo"]["external_roots"] = [str(external)]
    config = write_config(project, values)
    before = inspect_repository(config, "fixture-repo", require_clean=True)
    external_file.write_text("after\n", encoding="utf-8")
    after = inspect_repository(config, "fixture-repo", require_clean=True)

    with pytest.raises(RepositoryValidationError, match="configured external root changed"):
        validate_executor_snapshot(
            config,
            coordinate=before.dispatch_coordinate(),
            before=before,
            after=after,
            result=_executor_result(before),
        )


def test_concurrent_repository_write_halts_acceptance(project: FixtureProject) -> None:
    before = inspect_repository(project.config, "fixture-repo", require_clean=True)
    (project.repository / "src" / "concurrent.txt").write_text("unrelated writer\n", encoding="utf-8")
    after = inspect_repository(project.config, "fixture-repo", require_clean=False)

    with pytest.raises(RepositoryValidationError, match="rejects uncommitted executor changes"):
        validate_executor_snapshot(
            project.config,
            coordinate=before.dispatch_coordinate(),
            before=before,
            after=after,
            result=_executor_result(before),
        )


def test_external_diff_drivers_never_run_during_inspection_or_patch_hashing(
    project: FixtureProject,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "diff-external-ran"
    script = tmp_path / "evil-diff.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    script.chmod(0o700)
    _git(project.repository, "config", "diff.external", str(script))

    before = inspect_repository(project.config, "fixture-repo", require_clean=True)
    (project.repository / "src" / "value.txt").write_text("value=9\n", encoding="utf-8")
    after = inspect_repository(project.config, "fixture-repo", require_clean=False)
    patch_sha256 = working_patch_sha256(project.repository)

    assert not marker.exists()
    assert patch_sha256
    assert len(after.changes) == 1
    assert before.clean and not after.clean


def test_executor_git_config_mutation_is_rejected_before_checks(project: FixtureProject) -> None:
    before = inspect_repository(project.config, "fixture-repo", require_clean=True)
    _git(project.repository, "config", "filter.evil.clean", "/usr/bin/false")
    after = inspect_repository(project.config, "fixture-repo", require_clean=True)

    with pytest.raises(RepositoryValidationError, match="Git metadata changed"):
        validate_executor_snapshot(
            project.config,
            coordinate=before.dispatch_coordinate(),
            before=before,
            after=after,
            result=_executor_result(before),
        )


def test_executor_hook_mutation_is_rejected_before_checks(project: FixtureProject) -> None:
    before = inspect_repository(project.config, "fixture-repo", require_clean=True)
    (project.repository / ".git" / "hooks" / "pre-commit").write_text(
        "#!/bin/sh\necho executor-injected hook\n", encoding="utf-8"
    )
    after = inspect_repository(project.config, "fixture-repo", require_clean=True)

    with pytest.raises(RepositoryValidationError, match="Git metadata changed"):
        validate_pending_executor_changes(
            project.config,
            coordinate=before.dispatch_coordinate(),
            before=before,
            after=after,
            root=project.repository,
            writable_paths=("evidence/",),
            require_changes=False,
        )


def test_executor_refs_mutation_is_rejected_before_staging(project: FixtureProject) -> None:
    before = inspect_repository(project.config, "fixture-repo", require_clean=True)
    _git(project.repository, "branch", "executor-injected-branch")
    after = inspect_repository(project.config, "fixture-repo", require_clean=True)

    with pytest.raises(RepositoryValidationError, match="refs changed"):
        validate_pending_executor_changes(
            project.config,
            coordinate=before.dispatch_coordinate(),
            before=before,
            after=after,
            root=project.repository,
            writable_paths=("evidence/",),
            require_changes=False,
        )


def test_pre_existing_exclude_rules_cannot_hide_out_of_scope_writes(
    project: FixtureProject,
) -> None:
    (project.repository / "excludes").write_text("hidden-area/\n", encoding="utf-8")
    _git(project.repository, "add", "excludes")
    _git(project.repository, "commit", "-m", "add baseline excludes file")
    _git(project.repository, "config", "core.excludesFile", "excludes")
    before = inspect_repository(project.config, "fixture-repo", require_clean=True)
    (project.repository / "hidden-area").mkdir()
    (project.repository / "hidden-area" / "outside.txt").write_text("hidden write\n", encoding="utf-8")
    after = inspect_repository(project.config, "fixture-repo", require_clean=False)

    assert "hidden-area/outside.txt" in after.ignored
    with pytest.raises(RepositoryValidationError, match="created ignored paths outside step writable_paths"):
        validate_pending_executor_changes(
            project.config,
            coordinate=before.dispatch_coordinate(),
            before=before,
            after=after,
            root=project.repository,
            writable_paths=("evidence/",),
            require_changes=False,
        )


def test_in_scope_ignored_writes_remain_visible_and_authorized(project: FixtureProject) -> None:
    (project.repository / ".gitignore").write_text("cache/\n", encoding="utf-8")
    _git(project.repository, "add", ".gitignore")
    _git(project.repository, "commit", "-m", "add baseline gitignore")
    before = inspect_repository(project.config, "fixture-repo", require_clean=True)
    (project.repository / "cache").mkdir()
    (project.repository / "cache" / "note.txt").write_text("in-scope cache write\n", encoding="utf-8")
    after = inspect_repository(project.config, "fixture-repo", require_clean=False)

    paths = validate_pending_executor_changes(
        project.config,
        coordinate=before.dispatch_coordinate(),
        before=before,
        after=after,
        root=project.repository,
        writable_paths=("cache/",),
        require_changes=False,
    )

    assert any("cache/" in path for path in after.ignored)
    assert paths == ()


def test_dispatcher_commit_moves_refs_without_metadata_rejection(project: FixtureProject) -> None:
    before = inspect_repository(project.config, "fixture-repo", require_clean=True)
    (project.repository / "src" / "value.txt").write_text("value=3\n", encoding="utf-8")
    _git(project.repository, "add", "src/value.txt")
    _git(project.repository, "commit", "-m", "dispatcher-owned commit moves the branch ref")
    after = inspect_repository(project.config, "fixture-repo", require_clean=True)
    assert after.revision != before.revision

    # Metadata must be stable across a dispatcher-owned commit; only the ref
    # fingerprint may change, and executor-snapshot validation must not reject it.
    assert after.git_metadata_sha256 == before.git_metadata_sha256
    assert after.git_refs_sha256 != before.git_refs_sha256
    validate_executor_snapshot(
        project.config,
        coordinate=before.dispatch_coordinate(),
        before=before,
        after=after,
        result=_executor_result(after, result_revision=after.revision),
    )


def _executor_result(
    snapshot: RepositorySnapshot,
    *,
    result_revision: str | None = None,
    patch_sha256: str | None = None,
):
    artifact = next(entry for entry in snapshot.evidence if entry.relative_path == "evidence/fixture.md")
    return parse_executor_result(
        {
            "result_version": 1,
            "response_contract": "dispatcher.executor_result.v1",
            "dispatch_id": "dispatch-one",
            "attempt": 1,
            "step_id": "prepare-fixture",
            "repository": {
                "repo_id": snapshot.repo_id,
                "base_revision": snapshot.revision,
                "result_revision": snapshot.revision if result_revision is None else result_revision,
                "patch_sha256": patch_sha256,
            },
            "evidence": [
                {
                    "artifact_id": "fixture-evidence",
                    "relative_path": "fixture.md",
                    "sha256": artifact.sha256,
                    "media_type": "text/markdown",
                    "size_bytes": artifact.size_bytes,
                }
            ],
            "verification": [
                {"check_id": "fixture-check", "status": "passed", "summary": "passed"}
            ],
            "summary": "fixture executor result",
            "outcome": "completed",
        }
    )


def _commit_initial_tree(repository: Path) -> None:
    evidence = repository / "evidence"
    source = repository / "src"
    source.mkdir(exist_ok=True)
    (source / "value.txt").write_text("value=1\n", encoding="utf-8")
    (evidence / "fixture.md").write_text("fixture evidence\n", encoding="utf-8")
    (evidence / "obsolete.md").write_text("obsolete evidence\n", encoding="utf-8")
    (evidence / "rename-source.md").write_text("rename evidence\n", encoding="utf-8")
    _git(repository, "config", "user.name", "Fixture Initializer")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "branch", "-M", "main")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial fixture")


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()
