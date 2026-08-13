"""Dispatcher-owned exact-path Git commit capability."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import Field

from .config import Config, ContractModel
from .plan import PlanStep, Sha256
from .repository import (
    RepositorySnapshot,
    hardened_git_environment,
    inspect_workspace,
    validate_pending_executor_changes,
)
from .security import redact_text
from .workflow import RepositoryCoordinate


class StructuredGitError(RuntimeError):
    """A structured Git operation cannot be prepared, executed, or adopted safely."""


class GitCommandRecord(ContractModel):
    """Bounded dispatcher-generated Git command result."""

    argv: tuple[str, ...]
    exit_code: int
    stdout_sha256: Sha256
    stderr_sha256: Sha256
    transcript_sha256: Sha256
    output_truncated: bool
    duration_ms: int = Field(ge=0)
    summary: str = Field(min_length=1, max_length=2000)


class StructuredGitIntent(ContractModel):
    """Immutable commit input that must be persisted before real-index staging."""

    capability_version: Literal[1]
    safety_policy_version: Literal[1]
    repo_id: str
    step_id: str
    attempt: int = Field(ge=1, le=100)
    working_branch: str = Field(min_length=1, max_length=301)
    worktree_id: Sha256
    base_revision: str = Field(min_length=1, max_length=200)
    pre_commit_snapshot_sha256: Sha256
    changed_paths: tuple[str, ...]
    candidate_tree: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    message: str = Field(min_length=1, max_length=500)
    identity_digest: Sha256


class StructuredGitOutcome(ContractModel):
    """Authoritative post-commit observation returned by the capability."""

    intent: StructuredGitIntent
    stage: GitCommandRecord
    commit: GitCommandRecord
    result_revision: str = Field(min_length=1, max_length=200)
    repository_after: RepositorySnapshot


class StructuredGitAdoption(ContractModel):
    """Exact post-crash commit observation derived without invoking Git mutation."""

    recovery_kind: Literal["exact_head_adoption"]
    result_revision: str = Field(min_length=1, max_length=200)
    parent_revision: str = Field(min_length=1, max_length=200)
    tree: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    changed_paths: tuple[str, ...]
    message: str = Field(min_length=1, max_length=500)
    identity_digest: Sha256
    repository_manifest_sha256: Sha256


def prepare_structured_git_intent(
    config: Config,
    *,
    step: PlanStep,
    attempt: int,
    worktree: Path,
    coordinate: RepositoryCoordinate,
    before: RepositorySnapshot,
    dirty: RepositorySnapshot,
) -> StructuredGitIntent:
    """Validate dirty work and calculate its tree without changing the real index."""
    if config.repository(step.repo_id).commit_policy != "required":
        raise StructuredGitError("structured commit requires repository commit_policy required")
    if "commit" not in step.authorization.authorized_actions:
        raise StructuredGitError("structured commit requires plan commit authorization")
    if coordinate.working_branch is None:
        raise StructuredGitError("structured commit requires a pinned working branch")
    paths = validate_pending_executor_changes(
        config,
        coordinate=coordinate,
        before=before,
        after=dirty,
        root=worktree,
        writable_paths=step.authorization.writable_paths,
        require_changes=True,
    )
    _reject_dangerous_local_config(config, worktree)
    candidate_tree = _candidate_tree(config, worktree, paths)
    return StructuredGitIntent(
        capability_version=config.execution.structured_git.capability_version,
        safety_policy_version=1,
        repo_id=step.repo_id,
        step_id=step.step_id,
        attempt=attempt,
        working_branch=coordinate.working_branch,
        worktree_id=dirty.worktree_id,
        base_revision=coordinate.base_revision,
        pre_commit_snapshot_sha256=dirty.manifest_sha256,
        changed_paths=paths,
        candidate_tree=candidate_tree,
        message=f"dispatcher: {step.step_id} attempt {attempt}",
        identity_digest=_identity_digest(config),
    )


def execute_structured_git_commit(
    config: Config,
    *,
    worktree: Path,
    intent: StructuredGitIntent,
    on_staged: Callable[[GitCommandRecord], None] | None = None,
) -> StructuredGitOutcome:
    """Stage exact paths and create one commit after durable intent exists."""
    current = inspect_workspace(
        config,
        intent.repo_id,
        root=worktree,
        expected_branch=intent.working_branch,
        require_clean=False,
    )
    if current.revision != intent.base_revision or current.worktree_id != intent.worktree_id:
        raise StructuredGitError("repository identity or HEAD changed before structured staging")
    if current.manifest_sha256 != intent.pre_commit_snapshot_sha256:
        raise StructuredGitError("repository changed after structured commit intent was prepared")
    _reject_dangerous_local_config(config, worktree)

    stage = _run_mutating(config, worktree, ("add", "-A", "--", *intent.changed_paths))
    staged_paths = tuple(
        sorted(
            filter(
                None,
                _git_text(config, worktree, "diff", "--cached", "--name-only", "-z").split("\0"),
            )
        )
    )
    if staged_paths != intent.changed_paths:
        raise StructuredGitError("real Git index paths do not match structured commit intent")
    staged_tree = _git_text(config, worktree, "write-tree")
    if staged_tree != intent.candidate_tree:
        raise StructuredGitError("real Git index tree does not match structured commit intent")
    if on_staged is not None:
        on_staged(stage)

    git_config = config.execution.structured_git
    commit = _run_mutating(
        config,
        worktree,
        (
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            intent.message,
        ),
        identity=True,
    )
    result_revision = _git_text(config, worktree, "rev-parse", "HEAD")
    parent = _git_text(config, worktree, "rev-parse", "HEAD^")
    tree = _git_text(config, worktree, "rev-parse", "HEAD^{tree}")
    metadata = _git_text(
        config,
        worktree,
        "show",
        "-s",
        "--format=%an%x00%ae%x00%cn%x00%ce%x00%s",
        "HEAD",
    ).split("\0")
    expected_metadata = [
        git_config.author_name,
        git_config.author_email,
        git_config.committer_name,
        git_config.committer_email,
        intent.message,
    ]
    if parent != intent.base_revision or tree != intent.candidate_tree or metadata != expected_metadata:
        raise StructuredGitError("created commit does not match structured commit intent")
    after = inspect_workspace(
        config,
        intent.repo_id,
        root=worktree,
        expected_branch=intent.working_branch,
        require_clean=True,
    )
    if after.revision != result_revision:
        raise StructuredGitError("post-commit repository inspection revision mismatch")
    if after.git_metadata_sha256 != current.git_metadata_sha256:
        raise StructuredGitError("repository Git metadata changed during structured commit")
    return StructuredGitOutcome(
        intent=intent,
        stage=stage,
        commit=commit,
        result_revision=result_revision,
        repository_after=after,
    )


def adopt_structured_git_commit(
    config: Config,
    *,
    worktree: Path,
    intent: StructuredGitIntent,
) -> tuple[StructuredGitAdoption, RepositorySnapshot]:
    """Adopt only a clean HEAD whose complete fingerprint matches durable intent."""
    if intent.capability_version != config.execution.structured_git.capability_version:
        raise StructuredGitError("structured commit capability version changed before recovery")
    if intent.identity_digest != _identity_digest(config):
        raise StructuredGitError("structured commit identity changed before recovery")
    _reject_dangerous_local_config(config, worktree)
    after = inspect_workspace(
        config,
        intent.repo_id,
        root=worktree,
        expected_branch=intent.working_branch,
        require_clean=True,
    )
    if after.worktree_id != intent.worktree_id:
        raise StructuredGitError("structured commit recovery worktree identity mismatch")
    result_revision = _git_text(config, worktree, "rev-parse", "HEAD")
    parent = _git_text(config, worktree, "rev-parse", "HEAD^")
    tree = _git_text(config, worktree, "rev-parse", "HEAD^{tree}")
    metadata = _git_text(
        config,
        worktree,
        "show",
        "-s",
        "--format=%an%x00%ae%x00%cn%x00%ce%x00%s",
        "HEAD",
    ).split("\0")
    git_config = config.execution.structured_git
    expected_metadata = [
        git_config.author_name,
        git_config.author_email,
        git_config.committer_name,
        git_config.committer_email,
        intent.message,
    ]
    changed_paths = tuple(
        sorted(
            filter(
                None,
                _git_text(
                    config,
                    worktree,
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "-z",
                    "HEAD",
                ).split("\0"),
            )
        )
    )
    mismatches = []
    if parent != intent.base_revision:
        mismatches.append("parent")
    if tree != intent.candidate_tree:
        mismatches.append("tree")
    if metadata != expected_metadata:
        mismatches.append("identity or message")
    if changed_paths != intent.changed_paths:
        mismatches.append("changed paths")
    if after.revision != result_revision:
        mismatches.append("inspected revision")
    if mismatches:
        raise StructuredGitError(
            "committed HEAD does not match structured commit intent: " + ", ".join(mismatches)
        )
    return (
        StructuredGitAdoption(
            recovery_kind="exact_head_adoption",
            result_revision=result_revision,
            parent_revision=parent,
            tree=tree,
            changed_paths=changed_paths,
            message=intent.message,
            identity_digest=intent.identity_digest,
            repository_manifest_sha256=after.manifest_sha256,
        ),
        after,
    )


def _candidate_tree(config: Config, worktree: Path, paths: tuple[str, ...]) -> str:
    with tempfile.TemporaryDirectory(prefix="dispatcher-git-index-") as temporary:
        temporary_root = Path(temporary)
        environment = _git_environment(config, temporary_home=temporary_root)
        environment["GIT_INDEX_FILE"] = str(temporary_root / "index")
        _run_git(config, worktree, ("read-tree", "HEAD"), environment=environment)
        _run_git(config, worktree, ("add", "-A", "--", *paths), environment=environment)
        return _run_git(
            config,
            worktree,
            ("write-tree",),
            environment=environment,
        )[0].decode().strip()


def _reject_dangerous_local_config(config: Config, worktree: Path) -> None:
    names = _git_text(
        config,
        worktree,
        "config",
        "--local",
        "--no-includes",
        "--name-only",
        "--null",
        "--list",
    ).split("\0")
    dangerous = []
    for raw_name in names:
        name = raw_name.lower()
        if (
            name.startswith("include.")
            or name.startswith("includeif.")
            or name.startswith("filter.")
            or name == "diff.external"
            or (name.startswith("diff.") and name.endswith(".command"))
            or name in {
                "core.hookspath",
                "core.fsmonitor",
                "core.sshcommand",
                "core.excludesfile",
                "core.attributesfile",
            }
        ):
            dangerous.append(raw_name)
    if dangerous:
        raise StructuredGitError(
            "repository local Git configuration enables unsupported command execution: "
            + ", ".join(sorted(dangerous))
        )


def _identity_digest(config: Config) -> str:
    git_config = config.execution.structured_git
    payload = {
        "author_name": git_config.author_name,
        "author_email": git_config.author_email,
        "committer_name": git_config.committer_name,
        "committer_email": git_config.committer_email,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _git_environment(
    config: Config,
    *,
    temporary_home: Path | None = None,
    identity: bool = False,
) -> dict[str, str]:
    home = temporary_home or Path(config.state_dir)
    environment = hardened_git_environment()
    environment.update(
        {
            "HOME": str(home),
        }
    )
    if identity:
        git_config = config.execution.structured_git
        environment.update(
            {
                "GIT_AUTHOR_NAME": git_config.author_name,
                "GIT_AUTHOR_EMAIL": git_config.author_email,
                "GIT_COMMITTER_NAME": git_config.committer_name,
                "GIT_COMMITTER_EMAIL": git_config.committer_email,
            }
        )
    return environment


def _run_mutating(
    config: Config,
    worktree: Path,
    args: tuple[str, ...],
    *,
    identity: bool = False,
) -> GitCommandRecord:
    started = time.monotonic()
    stdout, stderr, exit_code, output_truncated = _run_git(
        config,
        worktree,
        args,
        environment=_git_environment(config, identity=identity),
    )
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    argv = ("git", *args)
    stdout_sha256 = hashlib.sha256(stdout).hexdigest()
    stderr_sha256 = hashlib.sha256(stderr).hexdigest()
    transcript = {
        "argv": list(argv),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "output_truncated": output_truncated,
        "stderr_sha256": stderr_sha256,
        "stdout_sha256": stdout_sha256,
    }
    return GitCommandRecord(
        argv=argv,
        exit_code=exit_code,
        stdout_sha256=stdout_sha256,
        stderr_sha256=stderr_sha256,
        transcript_sha256=hashlib.sha256(
            json.dumps(transcript, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        output_truncated=output_truncated,
        duration_ms=duration_ms,
        summary=redact_text(f"dispatcher Git command exited with status {exit_code}"),
    )


def _git_text(config: Config, worktree: Path, *args: str) -> str:
    stdout, _stderr, _exit_code, _truncated = _run_git(
        config,
        worktree,
        tuple(args),
        environment=_git_environment(config),
    )
    return stdout.decode("utf-8", errors="strict").rstrip("\n")


def _run_git(
    config: Config,
    worktree: Path,
    args: tuple[str, ...],
    *,
    environment: dict[str, str],
) -> tuple[bytes, bytes, int, bool]:
    bound = config.execution.structured_git.max_output_bytes
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                ["git", *args],
                cwd=worktree,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            try:
                process.wait(timeout=config.execution.structured_git.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
                raise StructuredGitError("dispatcher Git command timed out") from exc
        except OSError as exc:
            raise StructuredGitError(
                f"dispatcher Git command could not start: {type(exc).__name__}"
            ) from exc
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(bound + 1)
        stderr = stderr_file.read(bound + 1)
    truncated = len(stdout) > bound or len(stderr) > bound
    stdout = stdout[:bound]
    stderr = stderr[:bound]
    if process.returncode != 0 or truncated:
        detail = redact_text(stderr.decode("utf-8", errors="replace"))[-1000:]
        reason = "exceeded output bound" if truncated else f"failed with status {process.returncode}"
        raise StructuredGitError(f"dispatcher Git command {reason}: {detail or '[no stderr]'}")
    return stdout, stderr, process.returncode, truncated
