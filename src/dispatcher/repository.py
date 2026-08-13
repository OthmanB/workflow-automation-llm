"""Repository identity, immutable coordinates, and evidence manifest validation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Annotated, Iterable, Literal

from pydantic import Field, field_validator, model_validator

from .config import Config, ContractModel, Identifier
from .plan import EvidenceRequirement, writable_path_allows
from .results import ArtifactRecord, ExecutorResult, ProposalEvidence, ReviewTarget
from .workflow import RepositoryCoordinate


class RepositoryValidationError(ValueError):
    """A repository does not match its registered dispatch boundary."""


SAFE_GIT_ARGS: tuple[str, ...] = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
)


def hardened_git_environment() -> dict[str, str]:
    """Return a Git environment immune to user system/global configuration.

    Repo-local configuration is additionally bounded by the metadata
    fingerprint and by explicit ``--no-ext-diff``/``--no-textconv`` on
    content-producing diffs; no dispatcher inspection command may execute an
    executor-controlled helper.
    """
    return {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/usr/bin/false",
        "GIT_EDITOR": "/usr/bin/true",
        "GIT_PAGER": "cat",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }


GitBranch = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,300}$")]


class EvidenceManifestEntry(ContractModel):
    """One content-addressed file observed below a registered evidence root."""

    root: str
    relative_path: str
    file_type: Literal["file", "symlink"]
    size_bytes: int = Field(ge=0)
    mode: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("root", "relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("manifest paths must be relative and cannot contain '..'")
        return value


class RepositoryChange(ContractModel):
    """One Git worktree change represented without relying on prose output."""

    change_type: Literal["created", "modified", "deleted", "renamed", "untracked"]
    paths: tuple[str, ...]
    index_status: Annotated[str, Field(min_length=1, max_length=1)] = " "
    worktree_status: Annotated[str, Field(min_length=1, max_length=1)] = " "
    mode_changed: bool = False

    @field_validator("paths")
    @classmethod
    def valid_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("repository change requires at least one path")
        for value in values:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("repository change paths must be relative and cannot contain '..'")
        return values


class RepositorySnapshot(ContractModel):
    """Immutable observation of one exact registered repository worktree."""

    repo_id: Identifier
    branch: GitBranch
    revision: str = Field(min_length=1, max_length=200)
    worktree_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    remote_name: Identifier
    remote_url: str = Field(min_length=1)
    clean: bool
    evidence: tuple[EvidenceManifestEntry, ...]
    external: tuple[EvidenceManifestEntry, ...]
    changes: tuple[RepositoryChange, ...]
    ignored: tuple[str, ...] = ()
    dirty_patch_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    git_metadata_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    git_refs_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def clean_snapshot_has_no_changes(self) -> "RepositorySnapshot":
        if self.clean and self.changes:
            raise ValueError("clean repository snapshot cannot contain changes")
        return self

    def dispatch_coordinate(self, *, base_branch: str | None = None) -> RepositoryCoordinate:
        """Return the durable pre-dispatch coordinate derived from this snapshot."""
        return RepositoryCoordinate(
            repo_id=self.repo_id,
            base_revision=self.revision,
            base_branch=base_branch or self.branch,
            working_branch=self.branch,
            worktree_id=self.worktree_id,
            remote_name=self.remote_name,
            remote_url=self.remote_url,
        )


def inspect_repository(
    config: Config,
    repo_id: str,
    *,
    require_clean: bool,
) -> RepositorySnapshot:
    """Inspect one configured root and reject any identity or baseline mismatch."""
    repository = config.repository(repo_id)
    return _inspect_repository_at(
        config,
        repo_id,
        root=config.repository_root(repo_id).resolve(),
        expected_branch=repository.default_branch,
        require_clean=require_clean,
    )


def inspect_workspace(
    config: Config,
    repo_id: str,
    *,
    root: Path,
    expected_branch: str,
    require_clean: bool,
) -> RepositorySnapshot:
    """Inspect one linked worktree without requiring the repository default branch."""
    return _inspect_repository_at(
        config,
        repo_id,
        root=root.resolve(),
        expected_branch=expected_branch,
        require_clean=require_clean,
    )


def _inspect_repository_at(
    config: Config,
    repo_id: str,
    *,
    root: Path,
    expected_branch: str,
    require_clean: bool,
) -> RepositorySnapshot:
    repository = config.repository(repo_id)
    top_level = _git(root, "rev-parse", "--show-toplevel")
    if Path(top_level).resolve() != root:
        raise RepositoryValidationError(
            f"repository {repo_id} root is not the registered Git worktree: {root}"
        )
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch != expected_branch:
        raise RepositoryValidationError(
            f"repository {repo_id} branch mismatch: expected {expected_branch!r}, found {branch!r}"
        )
    remote_url = _git(root, "remote", "get-url", repository.expected_remote.name)
    if remote_url != repository.expected_remote.url:
        raise RepositoryValidationError(
            f"repository {repo_id} remote mismatch: expected {repository.expected_remote.url!r}, "
            f"found {remote_url!r}"
        )
    revision = _git(root, "rev-parse", "HEAD")
    git_dir = _resolve_git_path(root, _git(root, "rev-parse", "--git-dir"))
    common_dir = _resolve_git_path(root, _git(root, "rev-parse", "--git-common-dir"))
    worktree_id = hashlib.sha256(f"{root}\0{git_dir}".encode("utf-8")).hexdigest()
    git_metadata_sha256, git_refs_sha256 = _git_metadata_fingerprint(git_dir, common_dir)
    changes, ignored = _git_changes(root)
    if require_clean and changes:
        raise RepositoryValidationError(
            f"repository {repo_id} must be clean before dispatch: {_change_paths(changes)}"
        )
    dirty_patch_sha256 = _patch_digest(root)
    evidence = tuple(_evidence_entries(root, repository.evidence_roots))
    external = tuple(_external_entries(config.repository_external_dirs(repo_id)))
    manifest_sha256 = _snapshot_digest(
        repo_id=repo_id,
        branch=branch,
        revision=revision,
        worktree_id=worktree_id,
        remote_name=repository.expected_remote.name,
        remote_url=remote_url,
        evidence=evidence,
        external=external,
        changes=changes,
        ignored=tuple(sorted(ignored)),
        dirty_patch_sha256=dirty_patch_sha256,
        git_metadata_sha256=git_metadata_sha256,
        git_refs_sha256=git_refs_sha256,
    )
    return RepositorySnapshot(
        repo_id=repo_id,
        branch=branch,
        revision=revision,
        worktree_id=worktree_id,
        remote_name=repository.expected_remote.name,
        remote_url=remote_url,
        clean=not changes,
        evidence=evidence,
        external=external,
        changes=tuple(changes),
        ignored=tuple(sorted(ignored)),
        dirty_patch_sha256=dirty_patch_sha256,
        git_metadata_sha256=git_metadata_sha256,
        git_refs_sha256=git_refs_sha256,
        manifest_sha256=manifest_sha256,
    )


def validate_executor_snapshot(
    config: Config,
    *,
    coordinate: RepositoryCoordinate,
    before: RepositorySnapshot,
    after: RepositorySnapshot,
    result: ExecutorResult,
) -> None:
    """Bind executor output, worktree state, and required evidence to one attempt."""
    _validate_snapshot_identity(coordinate, after)
    _validate_external_roots(before, after)
    _validate_git_metadata_unchanged(before, after)
    repository = config.repository(coordinate.repo_id)
    if repository.commit_policy == "required":
        if not after.clean:
            raise RepositoryValidationError(
                "committing repository policy rejects uncommitted executor changes: "
                f"{_change_paths(after.changes)}"
            )
        if result.repository.result_revision != after.revision or result.repository.patch_sha256 is not None:
            raise RepositoryValidationError("executor result must report the inspected committed revision only")
    else:
        patch_sha256 = working_patch_sha256(config.repository_root(coordinate.repo_id))
        if after.clean or result.repository.patch_sha256 != patch_sha256:
            raise RepositoryValidationError(
                "non-committing repository policy requires the exact inspected patch SHA-256"
            )
        if result.repository.result_revision is not None and result.repository.result_revision != after.revision:
            raise RepositoryValidationError("executor result revision does not match the inspected repository")
    _validate_changed_paths(config, coordinate.repo_id, after.changes)
    _validate_evidence(config, coordinate.repo_id, after, result)


def validate_pending_executor_changes(
    config: Config,
    *,
    coordinate: RepositoryCoordinate,
    before: RepositorySnapshot,
    after: RepositorySnapshot,
    root: Path,
    writable_paths: tuple[str, ...],
    require_changes: bool,
) -> tuple[str, ...]:
    """Validate exact dirty executor output before checks or dispatcher staging."""
    _validate_snapshot_identity(coordinate, after)
    _validate_external_roots(before, after)
    _validate_git_metadata_unchanged(before, after, include_refs=True)
    if after.revision != coordinate.base_revision or before.revision != coordinate.base_revision:
        raise RepositoryValidationError("repository HEAD moved after dispatch preparation")
    if require_changes and not after.changes:
        raise RepositoryValidationError("completed required-commit proposal produced no changes")
    if any(change.index_status not in {" ", "?"} for change in after.changes):
        raise RepositoryValidationError("executor must not stage repository changes")
    unsupported = [
        path
        for change in after.changes
        for path in change.paths
        if change.mode_changed or (root / path).is_symlink()
    ]
    if unsupported:
        raise RepositoryValidationError(
            "executor changes contain a mode change or symlink: "
            + ", ".join(sorted(set(unsupported)))
        )
    paths = tuple(sorted(set(_change_paths(after.changes))))
    unexpected = [path for path in paths if not writable_path_allows(writable_paths, path)]
    if unexpected:
        raise RepositoryValidationError(
            "executor changed paths outside step writable_paths: "
            + ", ".join(sorted(unexpected))
        )
    created_ignored = [path for path in after.ignored if path not in set(before.ignored)]
    unexpected_ignored = [
        path for path in created_ignored if not writable_path_allows(writable_paths, path)
    ]
    if unexpected_ignored:
        raise RepositoryValidationError(
            "executor created ignored paths outside step writable_paths: "
            + ", ".join(sorted(unexpected_ignored))
        )
    _validate_changed_paths(config, coordinate.repo_id, after.changes)
    _validate_changed_parent_paths(root, paths)
    return paths


def authoritative_evidence(
    config: Config,
    *,
    repo_id: str,
    snapshot: RepositorySnapshot,
    requirements: tuple[EvidenceRequirement, ...],
    declarations: list[ProposalEvidence],
) -> list[ArtifactRecord]:
    """Build authoritative artifact records from exact declared evidence locations."""
    expected = [
        (item.artifact_id, item.relative_path, item.media_type) for item in requirements
    ]
    actual = [(item.artifact_id, item.relative_path, item.media_type) for item in declarations]
    if actual != expected:
        raise RepositoryValidationError(
            "executor proposal evidence declarations do not exactly match plan requirements"
        )
    if any(entry.file_type != "file" for entry in snapshot.evidence):
        raise RepositoryValidationError("evidence manifest contains an unsupported symlink")
    manifest = {entry.relative_path: entry for entry in snapshot.evidence}
    records: list[ArtifactRecord] = []
    for declaration in declarations:
        candidates = [
            manifest[str(Path(root) / declaration.relative_path)]
            for root in config.repository(repo_id).evidence_roots
            if str(Path(root) / declaration.relative_path) in manifest
        ]
        if len(candidates) != 1:
            raise RepositoryValidationError(
                f"evidence artifact {declaration.artifact_id} must resolve to exactly one registered evidence path"
            )
        entry = candidates[0]
        records.append(
            ArtifactRecord(
                artifact_id=declaration.artifact_id,
                relative_path=declaration.relative_path,
                sha256=entry.sha256,
                media_type=declaration.media_type,
                size_bytes=entry.size_bytes,
            )
        )
    return records


def validate_review_snapshot(
    config: Config,
    *,
    coordinate: RepositoryCoordinate,
    before: RepositorySnapshot,
    after: RepositorySnapshot,
    review_target: ReviewTarget,
) -> None:
    """Require a reviewer to inspect the same durable revision or patch as the executor."""
    _validate_snapshot_identity(coordinate, after)
    _validate_external_roots(before, after)
    _validate_git_metadata_unchanged(before, after)
    repository = config.repository(coordinate.repo_id)
    if repository.commit_policy == "required":
        if not after.clean:
            raise RepositoryValidationError("review repository changed after executor result")
        if review_target.result_revision != after.revision or review_target.patch_sha256 is not None:
            raise RepositoryValidationError("review target no longer matches the inspected repository revision")
    else:
        patch_sha256 = working_patch_sha256(config.repository_root(coordinate.repo_id))
        if after.clean or review_target.patch_sha256 != patch_sha256:
            raise RepositoryValidationError("review target no longer matches the inspected repository patch")
    manifest_hashes = {entry.sha256 for entry in after.evidence if entry.file_type == "file"}
    if not set(review_target.artifact_hashes).issubset(manifest_hashes):
        raise RepositoryValidationError("review target evidence hashes no longer match the repository manifest")


def working_patch_sha256(root: Path) -> str:
    """Hash tracked and untracked worktree content for an explicit no-commit result."""
    return _patch_digest(root)


def _patch_digest(root: Path) -> str:
    """Hash committable dirty content: tracked diff plus untracked file bytes."""
    diff = _git_bytes(root, "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD")
    chunks = [b"tracked\0", diff, b"\0untracked\0"]
    for relative_path in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if not relative_path:
            continue
        path = root / relative_path
        mode = path.lstat().st_mode
        chunks.append(relative_path.encode("utf-8"))
        chunks.append(b"\0")
        if stat.S_ISLNK(mode):
            chunks.append(os.readlink(path).encode("utf-8"))
        elif stat.S_ISREG(mode):
            chunks.append(path.read_bytes())
        else:
            raise RepositoryValidationError(f"unsupported untracked path type: {relative_path}")
        chunks.append(b"\0")
    return hashlib.sha256(b"".join(chunks)).hexdigest()


def _validate_snapshot_identity(coordinate: RepositoryCoordinate, snapshot: RepositorySnapshot) -> None:
    mismatches = []
    for field, expected, actual in (
        ("repo_id", coordinate.repo_id, snapshot.repo_id),
        ("working_branch", coordinate.working_branch, snapshot.branch),
        ("worktree_id", coordinate.worktree_id, snapshot.worktree_id),
        ("remote_name", coordinate.remote_name, snapshot.remote_name),
        ("remote_url", coordinate.remote_url, snapshot.remote_url),
    ):
        if expected != actual:
            mismatches.append(f"{field} expected {expected!r}, found {actual!r}")
    if mismatches:
        raise RepositoryValidationError("repository identity changed after dispatch preparation: " + "; ".join(mismatches))


def _validate_git_metadata_unchanged(
    before: RepositorySnapshot,
    after: RepositorySnapshot,
    *,
    include_refs: bool = False,
) -> None:
    """Reject executor-attributable Git metadata mutation between observations.

    The metadata fingerprint covers config, info exclude/attributes, hooks,
    and the worktree HEAD. Refs are only compared while the dispatcher has
    not itself moved them (before commit capability execution).
    """
    if before.git_metadata_sha256 != after.git_metadata_sha256:
        raise RepositoryValidationError(
            "repository Git metadata changed during the dispatch; "
            "executor Git config, exclude, attributes, hooks, or HEAD mutation is rejected"
        )
    if include_refs and before.git_refs_sha256 != after.git_refs_sha256:
        raise RepositoryValidationError(
            "repository Git refs changed during the dispatch before structured staging"
        )


def _validate_external_roots(before: RepositorySnapshot, after: RepositorySnapshot) -> None:
    if before.external != after.external:
        raise RepositoryValidationError("configured external root changed without an isolated dispatch artifact")


def _validate_changed_paths(config: Config, repo_id: str, changes: tuple[RepositoryChange, ...]) -> None:
    repository = config.repository(repo_id)
    allowed_roots = tuple(Path(root) for root in repository.writable_roots)
    unexpected = [
        path
        for change in changes
        for path in change.paths
        if not any(_is_within(path, root) for root in allowed_roots)
    ]
    if unexpected:
        raise RepositoryValidationError(
            f"repository {repo_id} changed outside configured writable roots: {sorted(set(unexpected))}"
        )


def _validate_evidence(
    config: Config,
    repo_id: str,
    snapshot: RepositorySnapshot,
    result: ExecutorResult,
) -> None:
    if any(entry.file_type != "file" for entry in snapshot.evidence):
        raise RepositoryValidationError("evidence manifest contains an unsupported symlink")
    manifest = {entry.relative_path: entry for entry in snapshot.evidence}
    for artifact in result.evidence:
        candidates = [
            manifest[str(Path(root) / artifact.relative_path)]
            for root in config.repository(repo_id).evidence_roots
            if str(Path(root) / artifact.relative_path) in manifest
        ]
        if len(candidates) != 1:
            raise RepositoryValidationError(
                f"evidence artifact {artifact.artifact_id} must resolve to exactly one registered evidence path"
            )
        entry = candidates[0]
        if entry.sha256 != artifact.sha256 or entry.size_bytes != artifact.size_bytes:
            raise RepositoryValidationError(
                f"evidence artifact {artifact.artifact_id} does not match the inspected content manifest"
            )


def _is_within(value: str, root: Path) -> bool:
    if root == Path("."):
        return True
    path = Path(value)
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _evidence_entries(root: Path, evidence_roots: list[str]) -> list[EvidenceManifestEntry]:
    return _manifest_entries(
        [(configured_root, root / configured_root, root) for configured_root in evidence_roots]
    )


def _external_entries(external_roots: list[Path]) -> list[EvidenceManifestEntry]:
    return _manifest_entries(
        [(f"external-{index}", root, root) for index, root in enumerate(external_roots)]
    )


def _manifest_entries(
    roots: list[tuple[str, Path, Path]],
) -> list[EvidenceManifestEntry]:
    entries: list[EvidenceManifestEntry] = []
    for entry_root, directory_root, relative_root in roots:
        for directory, directory_names, file_names in os.walk(directory_root, followlinks=False):
            directory_path = Path(directory)
            symlink_directories = [name for name in directory_names if (directory_path / name).is_symlink()]
            directory_names[:] = [name for name in directory_names if name not in symlink_directories]
            for name in sorted([*symlink_directories, *file_names]):
                path = directory_path / name
                stat_result = path.lstat()
                relative_path = path.relative_to(relative_root).as_posix()
                if stat.S_ISREG(stat_result.st_mode):
                    file_type: Literal["file", "symlink"] = "file"
                    content = path.read_bytes()
                elif stat.S_ISLNK(stat_result.st_mode):
                    file_type = "symlink"
                    content = os.readlink(path).encode("utf-8")
                else:
                    raise RepositoryValidationError(f"unsupported evidence path type: {relative_path}")
                entries.append(
                    EvidenceManifestEntry(
                        root=entry_root,
                        relative_path=relative_path,
                        file_type=file_type,
                        size_bytes=stat_result.st_size,
                        mode=stat.S_IMODE(stat_result.st_mode),
                        mtime_ns=stat_result.st_mtime_ns,
                        sha256=hashlib.sha256(content).hexdigest(),
                    )
                )
    return sorted(entries, key=lambda entry: (entry.root, entry.relative_path))


def _git_changes(root: Path) -> tuple[list[RepositoryChange], list[str]]:
    raw = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored")
    mode_changes = _git_mode_changes(root)
    fields = raw.split("\0")
    changes: list[RepositoryChange] = []
    ignored: list[str] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        if len(field) < 4:
            raise RepositoryValidationError("Git status returned malformed porcelain output")
        status = field[:2]
        first_path = field[3:]
        if status == "!!":
            ignored.append(first_path)
            continue
        code = "".join(character for character in status if character != " ")
        paths: tuple[str, ...]
        if "C" in code:
            raise RepositoryValidationError("Git copy status is unsupported for executor changes")
        if "R" in code:
            if index >= len(fields) or not fields[index]:
                raise RepositoryValidationError("Git rename status omitted its paired path")
            paths = (first_path, fields[index])
            index += 1
            change_type: Literal["created", "modified", "deleted", "renamed", "untracked"] = "renamed"
        elif status == "??":
            paths = (first_path,)
            change_type = "untracked"
        elif "D" in code:
            paths = (first_path,)
            change_type = "deleted"
        elif "A" in code:
            paths = (first_path,)
            change_type = "created"
        else:
            paths = (first_path,)
            change_type = "modified"
        changes.append(
            RepositoryChange(
                change_type=change_type,
                paths=paths,
                index_status=status[0],
                worktree_status=status[1],
                mode_changed=any(path in mode_changes for path in paths),
            )
        )
    return changes, ignored


def _git_mode_changes(root: Path) -> set[str]:
    raw = _git(root, "diff", "--raw", "-z", "--no-renames", "HEAD", "--")
    fields = raw.split("\0")
    changed: set[str] = set()
    index = 0
    while index < len(fields):
        metadata = fields[index].lstrip("\n")
        index += 1
        if not metadata:
            continue
        if not metadata.startswith(":") or index >= len(fields):
            raise RepositoryValidationError("Git raw diff returned malformed output")
        parts = metadata[1:].split()
        if len(parts) != 5:
            raise RepositoryValidationError("Git raw diff metadata is malformed")
        path = fields[index]
        index += 1
        if parts[0] != parts[1]:
            changed.add(path)
    return changed


def _resolve_git_path(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _git_metadata_fingerprint(git_dir: Path, common_dir: Path) -> tuple[str, str]:
    """Hash executor-sensitive Git metadata without invoking a Git command.

    The metadata fingerprint covers repository-local configuration, info
    exclude/attributes, hooks, and the worktree HEAD: everything that could
    execute a helper or hide changes. The refs fingerprint covers packed and
    loose refs and is only compared before the dispatcher moves refs itself.
    """
    metadata_paths = [
        ("common/config", common_dir / "config"),
        ("common/info/exclude", common_dir / "info" / "exclude"),
        ("common/info/attributes", common_dir / "info" / "attributes"),
        *(
            (f"common/hooks/{path.name}", path)
            for path in sorted((common_dir / "hooks").glob("*"))
            if path.is_file()
        ),
        ("worktree/HEAD", git_dir / "HEAD"),
    ]
    refs_paths = [("common/packed-refs", common_dir / "packed-refs")]
    refs_root = common_dir / "refs"
    if refs_root.is_dir():
        refs_paths.extend(
            (f"common/refs/{path.relative_to(refs_root).as_posix()}", path)
            for path in sorted(refs_root.rglob("*"))
            if path.is_file()
        )
    return _hash_observed_files(metadata_paths), _hash_observed_files(refs_paths)


def _hash_observed_files(paths: Iterable[tuple[str, Path]]) -> str:
    entries: list[dict[str, str | None]] = []
    for label, path in paths:
        if path.is_file():
            entries.append({"label": label, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        else:
            entries.append({"label": label, "sha256": None})
    payload = {"entries": entries}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _snapshot_digest(
    *,
    repo_id: str,
    branch: str,
    revision: str,
    worktree_id: str,
    remote_name: str,
    remote_url: str,
    evidence: tuple[EvidenceManifestEntry, ...],
    external: tuple[EvidenceManifestEntry, ...],
    changes: list[RepositoryChange],
    ignored: tuple[str, ...],
    dirty_patch_sha256: str,
    git_metadata_sha256: str,
    git_refs_sha256: str,
) -> str:
    payload = {
        "repo_id": repo_id,
        "branch": branch,
        "revision": revision,
        "worktree_id": worktree_id,
        "remote_name": remote_name,
        "remote_url": remote_url,
        "evidence": [entry.model_dump(mode="json") for entry in evidence],
        "external": [entry.model_dump(mode="json") for entry in external],
        "changes": [change.model_dump(mode="json") for change in changes],
        "ignored": list(ignored),
        "dirty_patch_sha256": dirty_patch_sha256,
        "git_metadata_sha256": git_metadata_sha256,
        "git_refs_sha256": git_refs_sha256,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _change_paths(changes: list[RepositoryChange] | tuple[RepositoryChange, ...]) -> list[str]:
    return [path for change in changes for path in change.paths]


def _validate_changed_parent_paths(root: Path, paths: tuple[str, ...]) -> None:
    resolved_root = root.resolve()
    for value in paths:
        parent = (root / value).parent
        while parent != root and not parent.exists():
            parent = parent.parent
        try:
            parent.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise RepositoryValidationError(
                f"executor changed path traverses outside registered worktree: {value}"
            ) from exc


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *SAFE_GIT_ARGS, *args],
            cwd=root,
            env=hardened_git_environment(),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RepositoryValidationError(f"Git inspection failed in {root}: {exc}") from exc
    return result.stdout.rstrip("\n")


def _git_bytes(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *SAFE_GIT_ARGS, *args],
            cwd=root,
            env=hardened_git_environment(),
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RepositoryValidationError(f"Git inspection failed in {root}: {exc}") from exc
    return result.stdout
