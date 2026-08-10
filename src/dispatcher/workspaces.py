"""Safe temporary Git worktree ownership for same-repository barrier batches."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from .config import Config
from .repository import inspect_repository
from .state_store import StateStore
from .workflow import (
    RunRecord,
    TransitionEvent,
    WorkspaceChild,
    WorkspaceGroup,
    WorkspaceGroupStatus,
    transition_workspace_group,
)


class WorkspaceError(RuntimeError):
    """A temporary worktree group cannot be safely created, inspected, or removed."""


@dataclass(frozen=True)
class WorkspaceOutcome:
    """One durable workspace lifecycle update for a run."""

    record: RunRecord
    generation: int
    group: WorkspaceGroup


class WorktreeManager:
    """Creates only dispatcher-owned temporary branches below configured roots."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def plan_group(
        self,
        *,
        workspace_group_id: str,
        repo_id: str,
        step_ids: Iterable[str],
        event: TransitionEvent,
    ) -> WorkspaceGroup:
        """Build a durable PREPARED group before any Git side effect occurs."""
        concurrency = self.config.execution.concurrency
        if concurrency.same_repository_mode != "worktree_barrier":
            raise WorkspaceError("same-repository worktrees require worktree_barrier mode")
        repository = self.config.repository(repo_id)
        if repository.commit_policy != "required":
            raise WorkspaceError("same-repository worktrees require commit_policy required")
        snapshot = inspect_repository(self.config, repo_id, require_clean=True)
        root = Path(concurrency.worktree_root).resolve()
        group_root = root / workspace_group_id
        children = tuple(
            WorkspaceChild(
                step_id=step_id,
                branch=_branch_name(concurrency.worktree_branch_prefix, workspace_group_id, step_id),
                worktree_path=str((group_root / _safe_path_component(step_id)).resolve()),
                base_revision=snapshot.revision,
            )
            for step_id in step_ids
        )
        return WorkspaceGroup(
            workspace_group_id=workspace_group_id,
            repo_id=repo_id,
            base_revision=snapshot.revision,
            base_branch=snapshot.branch,
            integration_branch=_branch_name(concurrency.worktree_branch_prefix, workspace_group_id, "integration"),
            integration_worktree_path=str((group_root / "integration").resolve()),
            worktree_root=str(group_root),
            lease_owner_id=f"workspace-{workspace_group_id}",
            children=children,
            state=WorkspaceGroupStatus.PREPARED,
            last_event=event,
        )

    def provision(self, group: WorkspaceGroup, *, event: TransitionEvent) -> WorkspaceGroup:
        """Create linked worktrees from one clean revision and activate the group."""
        if group.state is not WorkspaceGroupStatus.PREPARED:
            raise WorkspaceError("only prepared workspace groups can be provisioned")
        self._validate_group(group)
        source = self.config.repository_root(group.repo_id)
        snapshot = inspect_repository(self.config, group.repo_id, require_clean=True)
        if snapshot.revision != group.base_revision or snapshot.branch != group.base_branch:
            raise WorkspaceError("repository moved after workspace group preparation")
        created: list[WorkspaceChild] = []
        try:
            for child in group.children:
                path = Path(child.worktree_path)
                if path.exists():
                    raise WorkspaceError(f"workspace path already exists: {path}")
                path.parent.mkdir(parents=True, exist_ok=True)
                self._git(
                    source,
                    "worktree",
                    "add",
                    "-b",
                    child.branch,
                    str(path),
                    group.base_revision,
                )
                created.append(child)
            return transition_workspace_group(group, WorkspaceGroupStatus.ACTIVE, event)
        except Exception as exc:
            self._rollback_created(source, created)
            if isinstance(exc, WorkspaceError):
                raise
            raise WorkspaceError(f"could not provision workspace group: {exc}") from exc

    def inspect(self, group: WorkspaceGroup) -> WorkspaceGroup:
        """Verify that every durable child still has its owned branch and base lineage."""
        self._validate_group(group)
        source = self.config.repository_root(group.repo_id)
        listed = _worktree_map(self._git(source, "worktree", "list", "--porcelain"))
        updated_children: list[WorkspaceChild] = []
        for child in group.children:
            path = Path(child.worktree_path).resolve()
            metadata = listed.get(path)
            if metadata is None:
                raise WorkspaceError(f"workspace is missing from Git metadata: {path}")
            branch = self._git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
            if branch != child.branch:
                raise WorkspaceError(f"workspace branch mismatch for {child.step_id}")
            head = self._git(path, "rev-parse", "HEAD")
            if not _is_descendant(path, group.base_revision, head):
                raise WorkspaceError(f"workspace history diverged before base revision for {child.step_id}")
            updated_children.append(child.model_copy(update={"head_revision": head}))
        return group.model_copy(update={"children": tuple(updated_children)})

    def begin_integration(self, group: WorkspaceGroup, *, event: TransitionEvent) -> WorkspaceGroup:
        """Persist the point after which the source branch may be promoted."""
        if group.state is not WorkspaceGroupStatus.ACTIVE:
            raise WorkspaceError("only active workspace groups can begin integration")
        return transition_workspace_group(group, WorkspaceGroupStatus.INTEGRATING, event)

    def integrate(self, group: WorkspaceGroup, *, event: TransitionEvent) -> WorkspaceGroup:
        """Merge child branches in order, then fast-forward the source default branch once."""
        if group.state is not WorkspaceGroupStatus.INTEGRATING:
            raise WorkspaceError("workspace integration intent must be recorded before merging")
        self._validate_group(group)
        source = self.config.repository_root(group.repo_id)
        snapshot = inspect_repository(self.config, group.repo_id, require_clean=True)
        if snapshot.revision != group.base_revision or snapshot.branch != group.base_branch:
            raise WorkspaceError("repository moved after workspace group preparation")
        integration_path = Path(group.integration_worktree_path)
        if integration_path.exists() or self._branch_exists(source, group.integration_branch):
            raise WorkspaceError("workspace integration branch or path already exists")
        integration_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._git(
                source,
                "worktree",
                "add",
                "-b",
                group.integration_branch,
                str(integration_path),
                group.base_revision,
            )
            for child in group.children:
                self._git(integration_path, "merge", "--no-ff", "--no-edit", child.branch)
            self._git(source, "merge", "--ff-only", group.integration_branch)
        except Exception as exc:
            if isinstance(exc, WorkspaceError):
                raise
            raise WorkspaceError(f"workspace integration failed: {exc}") from exc
        revision = self._git(source, "rev-parse", "HEAD")
        return group.model_copy(update={"integration_revision": revision, "last_event": event})

    def begin_cleanup(self, group: WorkspaceGroup, *, event: TransitionEvent) -> WorkspaceGroup:
        """Persist the intent to remove only this manager's temporary workspaces."""
        if group.state not in {WorkspaceGroupStatus.ACTIVE, WorkspaceGroupStatus.INTEGRATING, WorkspaceGroupStatus.FAILED}:
            raise WorkspaceError("workspace group is not eligible for cleanup")
        return transition_workspace_group(group, WorkspaceGroupStatus.CLEANUP_PENDING, event)

    def cleanup(self, group: WorkspaceGroup, *, event: TransitionEvent, force: bool = False) -> WorkspaceGroup:
        """Remove owned worktrees and merged branches, then prune stale Git metadata."""
        if group.state is not WorkspaceGroupStatus.CLEANUP_PENDING:
            raise WorkspaceError("workspace cleanup must be recorded before removal")
        self._validate_group(group)
        source = self.config.repository_root(group.repo_id)
        errors: list[str] = []
        for child in group.children:
            path = Path(child.worktree_path)
            if path.exists():
                try:
                    args = ("worktree", "remove", "--force", str(path)) if force else ("worktree", "remove", str(path))
                    self._git(source, *args)
                except WorkspaceError as exc:
                    errors.append(str(exc))
                    continue
            try:
                args = ("branch", "-D", child.branch) if force else ("branch", "-d", child.branch)
                self._git(source, *args)
            except WorkspaceError as exc:
                errors.append(str(exc))
        integration_path = Path(group.integration_worktree_path)
        if integration_path.exists():
            try:
                args = ("worktree", "remove", "--force", str(integration_path)) if force else (
                    "worktree",
                    "remove",
                    str(integration_path),
                )
                self._git(source, *args)
            except WorkspaceError as exc:
                errors.append(str(exc))
        if self._branch_exists(source, group.integration_branch):
            try:
                args = ("branch", "-D", group.integration_branch) if force else ("branch", "-d", group.integration_branch)
                self._git(source, *args)
            except WorkspaceError as exc:
                errors.append(str(exc))
        try:
            self._git(source, "worktree", "prune")
        except WorkspaceError as exc:
            errors.append(str(exc))
        if errors:
            raise WorkspaceError("workspace cleanup incomplete: " + "; ".join(errors))
        group_root = Path(group.worktree_root)
        if group_root.exists():
            shutil.rmtree(group_root)
        return transition_workspace_group(group, WorkspaceGroupStatus.CLEANED, event)

    def _validate_group(self, group: WorkspaceGroup) -> None:
        if group.repo_id not in self.config.model.repositories:
            raise WorkspaceError(f"unknown workspace repository: {group.repo_id}")
        configured_root = Path(self.config.execution.concurrency.worktree_root).resolve()
        group_root = Path(group.worktree_root).resolve()
        try:
            group_root.relative_to(configured_root)
        except ValueError as exc:
            raise WorkspaceError("workspace group root escapes configured worktree_root") from exc
        expected_prefix = f"{self.config.execution.concurrency.worktree_branch_prefix}/{group.workspace_group_id}/"
        if not group.integration_branch.startswith(expected_prefix):
            raise WorkspaceError("workspace integration branch does not use configured prefix")
        for child in group.children:
            if not child.branch.startswith(expected_prefix):
                raise WorkspaceError("workspace child branch does not use configured prefix")
            try:
                Path(child.worktree_path).resolve().relative_to(group_root)
            except ValueError as exc:
                raise WorkspaceError("workspace child path escapes group root") from exc
        try:
            Path(group.integration_worktree_path).resolve().relative_to(group_root)
        except ValueError as exc:
            raise WorkspaceError("workspace integration path escapes group root") from exc

    def _rollback_created(self, source: Path, children: Iterable[WorkspaceChild]) -> None:
        for child in reversed(tuple(children)):
            path = Path(child.worktree_path)
            if path.exists():
                try:
                    self._git(source, "worktree", "remove", "--force", str(path))
                except WorkspaceError:
                    pass
            try:
                self._git(source, "branch", "-D", child.branch)
            except WorkspaceError:
                pass
        try:
            self._git(source, "worktree", "prune")
        except WorkspaceError:
            pass

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                check=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
            raise WorkspaceError(f"git {' '.join(args)} failed: {detail}") from exc
        return result.stdout.strip()

    def _branch_exists(self, cwd: Path, branch: str) -> bool:
        try:
            self._git(cwd, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
        except WorkspaceError:
            return False
        return True


def _branch_name(prefix: str, group_id: str, name: str) -> str:
    return f"{prefix}/{group_id}/{_safe_path_component(name)}"


def _safe_path_component(value: str) -> str:
    return value.replace("/", "-")


def _worktree_map(output: str) -> dict[Path, dict[str, str]]:
    entries: dict[Path, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            if "worktree" in current:
                entries[Path(current["worktree"]).resolve()] = current
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if "worktree" in current:
        entries[Path(current["worktree"]).resolve()] = current
    return entries


def _is_descendant(cwd: Path, base_revision: str, head_revision: str) -> bool:
    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_revision, head_revision],
            cwd=cwd,
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


class WorkspaceCoordinator:
    """Persists temporary worktree ownership before and after Git side effects."""

    def __init__(self, config: Config, store: StateStore, manager: WorktreeManager | None = None) -> None:
        self.config = config
        self.store = store
        self.manager = manager or WorktreeManager(config)

    def prepare(
        self,
        *,
        run_id: str,
        expected_generation: int,
        repo_id: str,
        step_ids: Iterable[str],
    ) -> WorkspaceOutcome:
        """Persist intent, acquire the repository lease, then provision child worktrees."""
        record, generation = self.store.load_run(run_id)
        if generation != expected_generation:
            raise WorkspaceError("run generation changed before workspace preparation")
        group_id = f"workspace-{uuid.uuid4().hex}"
        prepared = self.manager.plan_group(
            workspace_group_id=group_id,
            repo_id=repo_id,
            step_ids=step_ids,
            event=self._event(record, group_id, "prepared same-repository workspace group"),
        )
        record, generation = self.store.save_workspace_group(
            record,
            expected_generation=generation,
            group=prepared,
        )
        try:
            self.store.acquire_resource_leases(
                run_id=run_id,
                owner_id=prepared.lease_owner_id,
                resource_keys=[f"repository:{repo_id}"],
            )
            active = self.manager.provision(
                prepared,
                event=self._event(record, group_id, "provisioned same-repository workspace group"),
            )
            record, generation = self.store.save_workspace_group(
                record,
                expected_generation=generation,
                group=active,
            )
            return WorkspaceOutcome(record, generation, active)
        except Exception as exc:
            self.store.release_leases(
                owner_id=prepared.lease_owner_id,
                resource_keys=[f"repository:{repo_id}"],
            )
            failed = transition_workspace_group(
                prepared,
                WorkspaceGroupStatus.FAILED,
                self._event(record, group_id, f"workspace provisioning failed: {type(exc).__name__}"),
            )
            record, _generation = self.store.save_workspace_group(
                record,
                expected_generation=generation,
                group=failed,
            )
            raise WorkspaceError(f"workspace provisioning failed: {exc}") from exc

    def cleanup(
        self,
        *,
        run_id: str,
        expected_generation: int,
        workspace_group_id: str,
        force: bool = False,
    ) -> WorkspaceOutcome:
        """Persist cleanup intent before removing temporary worktrees and branches."""
        record, generation = self.store.load_run(run_id)
        if generation != expected_generation:
            raise WorkspaceError("run generation changed before workspace cleanup")
        try:
            group = record.workspace_groups[workspace_group_id]
        except KeyError as exc:
            raise WorkspaceError(f"unknown workspace group: {workspace_group_id}") from exc
        pending = self.manager.begin_cleanup(
            group,
            event=self._event(record, workspace_group_id, "workspace cleanup requested"),
        )
        record, generation = self.store.save_workspace_group(
            record,
            expected_generation=generation,
            group=pending,
        )
        try:
            cleaned = self.manager.cleanup(
                pending,
                event=self._event(record, workspace_group_id, "workspace cleanup completed"),
                force=force,
            )
            self.store.release_leases(
                owner_id=cleaned.lease_owner_id,
                resource_keys=[f"repository:{cleaned.repo_id}"],
            )
            record, generation = self.store.save_workspace_group(
                record,
                expected_generation=generation,
                group=cleaned,
            )
            return WorkspaceOutcome(record, generation, cleaned)
        except Exception as exc:
            failed = transition_workspace_group(
                pending,
                WorkspaceGroupStatus.FAILED,
                self._event(record, workspace_group_id, f"workspace cleanup failed: {type(exc).__name__}"),
            )
            record, _generation = self.store.save_workspace_group(
                record,
                expected_generation=generation,
                group=failed,
            )
            raise WorkspaceError(f"workspace cleanup failed: {exc}") from exc

    def integrate(
        self,
        *,
        run_id: str,
        expected_generation: int,
        workspace_group_id: str,
    ) -> WorkspaceOutcome:
        """Persist integration intent, promote the source branch, then remove temporary Git state."""
        record, generation = self.store.load_run(run_id)
        if generation != expected_generation:
            raise WorkspaceError("run generation changed before workspace integration")
        try:
            group = record.workspace_groups[workspace_group_id]
        except KeyError as exc:
            raise WorkspaceError(f"unknown workspace group: {workspace_group_id}") from exc
        integrating = self.manager.begin_integration(
            group,
            event=self._event(record, workspace_group_id, "workspace integration requested"),
        )
        record, generation = self.store.save_workspace_group(
            record,
            expected_generation=generation,
            group=integrating,
        )
        try:
            integrated = self.manager.integrate(
                integrating,
                event=self._event(record, workspace_group_id, "workspace integration promoted"),
            )
            record, generation = self.store.save_workspace_group(
                record,
                expected_generation=generation,
                group=integrated,
            )
        except Exception as exc:
            failed = transition_workspace_group(
                integrating,
                WorkspaceGroupStatus.FAILED,
                self._event(record, workspace_group_id, f"workspace integration failed: {type(exc).__name__}"),
            )
            record, _generation = self.store.save_workspace_group(
                record,
                expected_generation=generation,
                group=failed,
            )
            raise WorkspaceError(f"workspace integration failed: {exc}") from exc
        return self.cleanup(
            run_id=run_id,
            expected_generation=generation,
            workspace_group_id=workspace_group_id,
        )

    @staticmethod
    def _event(record: RunRecord, correlation_id: str, reason: str) -> TransitionEvent:
        return TransitionEvent(
            event_id=f"event-{uuid.uuid4().hex}",
            sequence=record.sequence + 1,
            actor="dispatcher",
            reason=reason,
            correlation_id=correlation_id,
            occurred_at=datetime.now(UTC),
        )
