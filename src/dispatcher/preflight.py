"""Pre-flight safety checks — run before the first dispatch."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import audit
from .config import Config, PreflightDefinition
from .sessions import SessionResult

logger = logging.getLogger(__name__)


class PreflightError(Exception):
    """A pre-flight check failed — the run cannot start."""


RunSessionFn = Callable[..., SessionResult]
CheckFn = Callable[[], str]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_preflight(
    config: Config,
    state_dir: str,
    run_session: RunSessionFn | None = None,
    skip_smoke: bool = False,
) -> dict[str, Any]:
    """Run all enabled pre-flight checks.

    Returns a dict of check results.  Raises PreflightError if *any* check
    fails — pre-flight is a hard gate.
    """
    preflight_cfg = config.preflight
    results: dict[str, Any] = {}
    if preflight_cfg is None or not preflight_cfg.enabled:
        logger.info("pre-flight disabled in config — skipping")
        results["preflight"] = {
            "status": "skipped",
            "detail": "disabled by configuration",
        }
        audit.preflight_result(state_dir, True, results)
        return results

    errors: list[str] = []

    def _run(label: str, fn: CheckFn) -> None:
        try:
            detail = fn()
            results[label] = {"status": "passed", "detail": detail}
            logger.info("pre-flight: %-30s  PASS", label)
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            results[label] = {
                "status": "failed",
                "detail": detail,
                "error_type": type(exc).__name__,
            }
            errors.append(f"{label}: {detail}")
            if isinstance(exc, PreflightError):
                logger.error("pre-flight: %-30s  FAIL  %s", label, detail)
            else:
                logger.exception(
                    "pre-flight: %-30s  ERROR  %s", label, detail
                )

    _run("fs_paths", lambda: _check_paths(config))
    _run("git_auth", lambda: _check_git(config))
    _run("disk_space", lambda: _check_disk(config, preflight_cfg))
    _run("credentials", lambda: _check_credentials(preflight_cfg.credentials))

    if not skip_smoke and preflight_cfg.models_smoke_test:
        if run_session is None:
            _run(
                "models",
                lambda: _raise_preflight_error(
                    "mock-only preflight requires an injected session runner"
                ),
            )
        else:
            _run(
                "models",
                lambda: _check_models(config, preflight_cfg, run_session),
            )

    passed = len(errors) == 0
    audit.preflight_result(state_dir, passed, results)

    if errors:
        raise PreflightError(
            "pre-flight failed:\n  " + "\n  ".join(errors)
        )

    logger.info("pre-flight: all checks passed (%d)", len(results))
    return results


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_paths(config: Config) -> str:
    for key, path_string in (
        ("specifications", config.model.sources.specifications_dir),
        ("plans", config.model.sources.plans_dir),
    ):
        p = Path(path_string)
        if not p.exists():
            raise PreflightError(f"path {key}={p} does not exist")
    for repo_id in config.model.repositories:
        for evidence_path in config.repository_evidence_dirs(repo_id):
            if not evidence_path.exists():
                raise PreflightError(
                    f"repository {repo_id} evidence path does not exist: {evidence_path}"
                )
    # Ensure state dir is writable.
    sd = Path(config.state_dir)
    sd.mkdir(parents=True, exist_ok=True)
    if not os.access(sd, os.W_OK):
        raise PreflightError(f"state dir not writable: {sd}")
    return "ok"


def _check_git(config: Config) -> str:
    checked_repositories = []
    for repo_id, repository in config.model.repositories.items():
        root = config.repository_root(repo_id)
        try:
            subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=root,
                capture_output=True,
                timeout=10,
                check=True,
            )
            if config.preflight is not None and config.preflight.require_git_remote:
                remote = subprocess.run(
                    ["git", "remote", "get-url", repository.expected_remote.name],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
                if remote.stdout.strip() != repository.expected_remote.url:
                    raise PreflightError(
                        f"repository {repo_id} remote mismatch: expected "
                        f"{repository.expected_remote.url!r}, found {remote.stdout.strip()!r}"
                    )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as exc:
            raise PreflightError(f"git check failed for repository {repo_id}: {exc}") from exc
        checked_repositories.append(repo_id)

    return f"ok ({len(checked_repositories)} repositories)"


def _check_disk(config: Config, preflight_cfg: PreflightDefinition) -> str:
    min_mb = preflight_cfg.disk_space_min_mb
    if min_mb <= 0:
        return "skipped"

    targets = {"state": Path(config.state_dir)}
    for repo_id in config.model.repositories:
        targets[f"repository:{repo_id}"] = config.repository_root(repo_id)
        for index, evidence_path in enumerate(config.repository_evidence_dirs(repo_id), start=1):
            targets[f"evidence:{repo_id}:{index}"] = evidence_path

    free_by_target: dict[str, int] = {}
    for label, target in targets.items():
        probe = _nearest_existing_path(target)
        usage = shutil.disk_usage(probe)
        free_by_target[label] = usage.free // (1024 * 1024)

    failures = {
        label: free_mb
        for label, free_mb in free_by_target.items()
        if free_mb < min_mb
    }
    if failures:
        details = ", ".join(
            f"{label}={free_mb} MB" for label, free_mb in failures.items()
        )
        raise PreflightError(
            f"disk free below required {min_mb} MB: {details}"
        )

    minimum_free = min(free_by_target.values())
    return f"ok ({minimum_free} MB minimum across {len(targets)} paths)"


def _nearest_existing_path(path: Path) -> Path:
    probe = path.resolve(strict=False)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.exists():
        raise PreflightError(f"no existing parent for disk check path: {path}")
    return probe


def _check_credentials(credentials: list[str]) -> str:
    missing = []
    for var in credentials:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        raise PreflightError(
            f"missing environment variables: {', '.join(missing)}"
        )
    return f"ok ({len(credentials)} vars set)"


def _check_models(
    config: Config,
    preflight_cfg: PreflightDefinition,
    run_session: RunSessionFn,
) -> str:
    """Smoke-test every model the config references.

    Runs a trivial opencode command per model and verifies exit 0 + "OK"
    marker.  This catches dead endpoints and typos before the real run.
    """
    prompt = preflight_cfg.smoke_prompt
    models: set[tuple[str, str, str]] = set()  # (model, variant, display)

    # Collect all unique models from all role pools.
    for role in config.model.all_roles().values():
        models.add((role.model, role.variant, role.display))

    failures: list[str] = []
    for model, variant, display in sorted(models):
        logger.info("smoke-testing model %s (variant=%s) ...", model, variant)
        try:
            result = run_session(
                prompt=prompt,
                model=model,
                variant=variant,
                session_id=None,
                mode="new",
                workdir=config.repository_root(config.default_repository_id),
                title=f"smoke-test {display}",
                auto_approve=False,
                timeout_seconds=config.execution.timeout_seconds,
                termination_grace_seconds=config.execution.termination_grace_seconds,
                max_output_bytes=config.execution.max_output_bytes,
                state_dir=config.state_dir,
            )
        except Exception as exc:
            failures.append(f"{model}: {exc}")
            continue

        if result.exit_code != 0:
            failures.append(f"{model}: exit code {result.exit_code}")
        elif "OK" not in result.chat_response:
            failures.append(f"{model}: did not return 'OK' marker")
        else:
            logger.info("smoke-test %-30s  PASS", display)

    if failures:
        raise PreflightError(
            "model smoke test failures:\n  " + "\n  ".join(failures)
        )
    return f"ok ({len(models)} models)"


def _raise_preflight_error(message: str) -> str:
    raise PreflightError(message)
