from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from helpers import config_values, create_fixture_project, valid_plan_values, write_config

from dispatcher.git_commit import (
    StructuredGitError,
    adopt_structured_git_commit,
    execute_structured_git_commit,
    prepare_structured_git_intent,
)
from dispatcher.plan import NormalizedPlan
from dispatcher.repository import RepositoryValidationError, inspect_repository


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _structured_project(tmp_path: Path):
    project = create_fixture_project(tmp_path)
    (project.repository / "result.txt").write_text("before\n", encoding="utf-8")
    (project.evidence / "fixture.md").write_text("before evidence\n", encoding="utf-8")
    _git(project.repository, "config", "user.name", "Fixture Initializer")
    _git(project.repository, "config", "user.email", "fixture@example.invalid")
    _git(project.repository, "branch", "-M", "main")
    _git(project.repository, "add", ".")
    _git(project.repository, "commit", "-m", "initial fixture")
    values = config_values(project)
    values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    values["permission_policies"]["policies"]["executor-class"]["actions"]["commit"] = "allow"
    config = write_config(project, values)
    plan_values = valid_plan_values(project)
    plan_values["steps"][0]["authorization"] = {
        "authorized_actions": ["inspect", "modify", "commit"],
        "writable_paths": ["evidence/", "result.txt"],
        "requires_operator_approval": False,
    }
    return project, config, NormalizedPlan.model_validate(plan_values)


def test_structured_git_preparation_does_not_touch_real_index_and_commit_is_exact(
    tmp_path: Path,
) -> None:
    project, config, plan = _structured_project(tmp_path)
    before = inspect_repository(config, "fixture-repo", require_clean=True)
    (project.repository / "result.txt").write_text("after\n", encoding="utf-8")
    (project.evidence / "fixture.md").write_text("after evidence\n", encoding="utf-8")
    dirty = inspect_repository(config, "fixture-repo", require_clean=False)

    intent = prepare_structured_git_intent(
        config,
        step=plan.steps[0],
        attempt=1,
        worktree=project.repository,
        coordinate=before.dispatch_coordinate(),
        before=before,
        dirty=dirty,
    )

    assert _git(project.repository, "diff", "--cached", "--name-only") == ""
    assert intent.changed_paths == ("evidence/fixture.md", "result.txt")
    outcome = execute_structured_git_commit(config, worktree=project.repository, intent=intent)

    assert outcome.repository_after.clean
    assert outcome.result_revision == _git(project.repository, "rev-parse", "HEAD")
    assert _git(project.repository, "rev-parse", "HEAD^") == before.revision
    assert _git(project.repository, "show", "-s", "--format=%s") == (
        "dispatcher: prepare-fixture attempt 1"
    )
    assert _git(project.repository, "show", "-s", "--format=%an <%ae> / %cn <%ce>") == (
        "Dispatcher Executor <dispatcher-author@example.invalid> / "
        "Dispatcher Committer <dispatcher-committer@example.invalid>"
    )
    assert outcome.stage.argv == (
        "git",
        "add",
        "-A",
        "--",
        "evidence/fixture.md",
        "result.txt",
    )
    assert "--no-verify" in outcome.commit.argv
    assert "--no-gpg-sign" in outcome.commit.argv

    adoption, adopted_snapshot = adopt_structured_git_commit(
        config,
        worktree=project.repository,
        intent=intent,
    )
    assert adoption.result_revision == outcome.result_revision
    assert adoption.parent_revision == before.revision
    assert adoption.tree == intent.candidate_tree
    assert adoption.changed_paths == intent.changed_paths
    assert adoption.repository_manifest_sha256 == adopted_snapshot.manifest_sha256


def test_structured_git_adoption_rejects_a_commit_that_does_not_match_intent(
    tmp_path: Path,
) -> None:
    project, config, plan = _structured_project(tmp_path)
    before = inspect_repository(config, "fixture-repo", require_clean=True)
    (project.repository / "result.txt").write_text("after\n", encoding="utf-8")
    dirty = inspect_repository(config, "fixture-repo", require_clean=False)
    intent = prepare_structured_git_intent(
        config,
        step=plan.steps[0],
        attempt=1,
        worktree=project.repository,
        coordinate=before.dispatch_coordinate(),
        before=before,
        dirty=dirty,
    )
    execute_structured_git_commit(config, worktree=project.repository, intent=intent)
    _git(project.repository, "commit", "--amend", "-m", "tampered recovery commit")

    with pytest.raises(StructuredGitError, match="identity or message"):
        adopt_structured_git_commit(
            config,
            worktree=project.repository,
            intent=intent,
        )


@pytest.mark.parametrize(
    ("intent_update", "expected_mismatch"),
    [
        ({"base_revision": "0" * 40}, "parent"),
        ({"candidate_tree": "0" * 40}, "tree"),
        ({"changed_paths": ("evidence/fixture.md",)}, "changed paths"),
        ({"message": "dispatcher: wrong step attempt 1"}, "identity or message"),
    ],
)
def test_structured_git_adoption_rejects_each_durable_fingerprint_mismatch(
    tmp_path: Path,
    intent_update: dict[str, object],
    expected_mismatch: str,
) -> None:
    project, config, plan = _structured_project(tmp_path)
    before = inspect_repository(config, "fixture-repo", require_clean=True)
    (project.repository / "result.txt").write_text("after\n", encoding="utf-8")
    (project.evidence / "fixture.md").write_text("after evidence\n", encoding="utf-8")
    dirty = inspect_repository(config, "fixture-repo", require_clean=False)
    intent = prepare_structured_git_intent(
        config,
        step=plan.steps[0],
        attempt=1,
        worktree=project.repository,
        coordinate=before.dispatch_coordinate(),
        before=before,
        dirty=dirty,
    )
    execute_structured_git_commit(config, worktree=project.repository, intent=intent)

    with pytest.raises(StructuredGitError, match=expected_mismatch):
        adopt_structured_git_commit(
            config,
            worktree=project.repository,
            intent=intent.model_copy(update=intent_update),
        )


def test_structured_git_adoption_rejects_dirty_post_commit_state(tmp_path: Path) -> None:
    project, config, plan = _structured_project(tmp_path)
    before = inspect_repository(config, "fixture-repo", require_clean=True)
    (project.repository / "result.txt").write_text("after\n", encoding="utf-8")
    dirty = inspect_repository(config, "fixture-repo", require_clean=False)
    intent = prepare_structured_git_intent(
        config,
        step=plan.steps[0],
        attempt=1,
        worktree=project.repository,
        coordinate=before.dispatch_coordinate(),
        before=before,
        dirty=dirty,
    )
    execute_structured_git_commit(config, worktree=project.repository, intent=intent)
    (project.repository / "result.txt").write_text("uncommitted tampering\n", encoding="utf-8")

    with pytest.raises(RepositoryValidationError, match="must be clean"):
        adopt_structured_git_commit(
            config,
            worktree=project.repository,
            intent=intent,
        )


def test_structured_git_rejects_out_of_scope_staged_mode_and_symlink_changes(
    tmp_path: Path,
) -> None:
    project, config, plan = _structured_project(tmp_path)
    before = inspect_repository(config, "fixture-repo", require_clean=True)

    (project.repository / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
    dirty = inspect_repository(config, "fixture-repo", require_clean=False)
    with pytest.raises(RepositoryValidationError, match="outside step writable_paths"):
        prepare_structured_git_intent(
            config,
            step=plan.steps[0],
            attempt=1,
            worktree=project.repository,
            coordinate=before.dispatch_coordinate(),
            before=before,
            dirty=dirty,
        )
    (project.repository / "unexpected.txt").unlink()

    (project.repository / "result.txt").write_text("staged\n", encoding="utf-8")
    _git(project.repository, "add", "result.txt")
    dirty = inspect_repository(config, "fixture-repo", require_clean=False)
    with pytest.raises(RepositoryValidationError, match="must not stage"):
        prepare_structured_git_intent(
            config,
            step=plan.steps[0],
            attempt=1,
            worktree=project.repository,
            coordinate=before.dispatch_coordinate(),
            before=before,
            dirty=dirty,
        )


def test_structured_git_rejects_dangerous_local_config(tmp_path: Path) -> None:
    project, config, plan = _structured_project(tmp_path)
    _git(project.repository, "config", "filter.fixture.clean", "/usr/bin/false")
    before = inspect_repository(config, "fixture-repo", require_clean=True)
    (project.repository / "result.txt").write_text("after\n", encoding="utf-8")
    dirty = inspect_repository(config, "fixture-repo", require_clean=False)

    with pytest.raises(StructuredGitError, match="unsupported command execution"):
        prepare_structured_git_intent(
            config,
            step=plan.steps[0],
            attempt=1,
            worktree=project.repository,
            coordinate=before.dispatch_coordinate(),
            before=before,
            dirty=dirty,
        )


def test_structured_git_rejects_metadata_mutation_after_dispatch_inspection(
    tmp_path: Path,
) -> None:
    project, config, plan = _structured_project(tmp_path)
    before = inspect_repository(config, "fixture-repo", require_clean=True)
    (project.repository / "result.txt").write_text("after\n", encoding="utf-8")
    _git(project.repository, "config", "filter.fixture.clean", "/usr/bin/false")
    dirty = inspect_repository(config, "fixture-repo", require_clean=False)

    with pytest.raises(RepositoryValidationError, match="Git metadata changed"):
        prepare_structured_git_intent(
            config,
            step=plan.steps[0],
            attempt=1,
            worktree=project.repository,
            coordinate=before.dispatch_coordinate(),
            before=before,
            dirty=dirty,
        )
