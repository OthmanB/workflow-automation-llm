from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from helpers import FixtureProject, config_values, create_fixture_project

from dispatcher import preflight
from dispatcher.mock_harness import MockRunner
from dispatcher.preflight import PreflightError
from dispatcher.sessions import SessionResult


@pytest.fixture
def project(tmp_path: Path) -> FixtureProject:
    return create_fixture_project(tmp_path)


def _mcp_project(tmp_path: Path, *, command: list[str] | None = None, passthrough: list[str] | None = None):
    from helpers import write_config

    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["mcp"] = {
        "environment_passthrough": passthrough if passthrough is not None else [],
        "servers": {
            "fixture": {
                "type": "local",
                "enabled": True,
                "command": command or ["/usr/bin/fixture-mcp"],
                "environment": {},
            }
        },
    }
    for pool in values["roles"].values():
        for role_key in pool:
            pool[role_key]["mcp_tools"] = ["fixture_echo"]
    return write_config(project, values)


def test_preflight_mcp_passes_when_servers_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_non_model_checks(monkeypatch)
    import sys

    config = _mcp_project(tmp_path, command=[sys.executable, "-V"])
    results = preflight.run_preflight(config, config.state_dir, skip_smoke=True)
    assert results["mcp"]["status"] == "passed"
    assert "1 enabled MCP server" in results["mcp"]["detail"]


def test_preflight_mcp_resolves_path_commands_via_PATH(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_non_model_checks(monkeypatch)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "fixture-mcp-bin").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "fixture-mcp-bin").chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    config = _mcp_project(tmp_path, command=["fixture-mcp-bin"])

    results = preflight.run_preflight(config, config.state_dir, skip_smoke=True)
    assert results["mcp"]["status"] == "passed"


def test_preflight_mcp_fails_on_missing_passthrough_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_non_model_checks(monkeypatch)
    monkeypatch.delenv("FIXTURE_MCP_TOKEN", raising=False)
    config = _mcp_project(tmp_path, passthrough=["FIXTURE_MCP_TOKEN"])

    with pytest.raises(PreflightError, match="mcp: .*passthrough environment variable"):
        preflight.run_preflight(config, config.state_dir, skip_smoke=True)


def test_preflight_mcp_fails_on_missing_local_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_non_model_checks(monkeypatch)
    config = _mcp_project(tmp_path, command=["/definitely/missing/fixture-mcp"])

    with pytest.raises(PreflightError, match="mcp: .*does not exist"):
        preflight.run_preflight(config, config.state_dir, skip_smoke=True)


def _patch_non_model_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "_check_paths", lambda _config: "ok")
    monkeypatch.setattr(preflight, "_check_git", lambda _config: "ok")
    monkeypatch.setattr(
        preflight,
        "_check_disk",
        lambda _config, _preflight_config: "ok",
    )
    monkeypatch.setattr(
        preflight,
        "_check_credentials",
        lambda _credentials: "ok",
    )


def test_enabled_preflight_uses_injected_smoke_runner(
    project: FixtureProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_non_model_checks(monkeypatch)
    calls: list[str] = []

    def runner(**kwargs: Any) -> SessionResult:
        calls.append(kwargs["model"])
        return SessionResult(
            session_id="ses_fixture",
            exit_code=0,
            chat_response="OK",
            evidence_written=[],
        )

    results = preflight.run_preflight(project.config, project.config.state_dir, run_session=runner)

    assert results["models"]["status"] == "passed"
    assert calls == [
        "fixture/executor",
        "fixture/reviewer",
        "fixture/reviewer-two",
        "fixture/supervisor",
    ]
    audit_event = json.loads(
        (Path(project.config.state_dir) / "audit.jsonl").read_text().splitlines()[-1]
    )
    assert audit_event["passed"] is True
    assert audit_event["checks"] == results


@pytest.mark.parametrize("response", ["NOT OK", "OK\nextra", "OK but more", ""])
def test_model_smoke_rejects_non_exact_ok_responses(
    project: FixtureProject,
    monkeypatch: pytest.MonkeyPatch,
    response: str,
) -> None:
    _patch_non_model_checks(monkeypatch)

    def runner(**_kwargs: Any) -> SessionResult:
        return SessionResult(
            session_id="ses_fixture",
            exit_code=0,
            chat_response=response,
            evidence_written=[],
        )

    with pytest.raises(PreflightError, match="exact 'OK' response"):
        preflight.run_preflight(project.config, project.config.state_dir, run_session=runner)


def test_absent_preflight_is_audited(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path, include_preflight=False)

    results = preflight.run_preflight(project.config, project.config.state_dir)

    assert results["preflight"]["status"] == "skipped"
    audit_event = json.loads(
        (Path(project.config.state_dir) / "audit.jsonl").read_text().splitlines()[-1]
    )
    assert audit_event["passed"] is True


def test_skip_smoke_does_not_call_runner(
    project: FixtureProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_non_model_checks(monkeypatch)

    def runner(**_kwargs: Any) -> SessionResult:
        raise AssertionError("smoke runner should not be called")

    results = preflight.run_preflight(
        project.config,
        project.config.state_dir,
        run_session=runner,
        skip_smoke=True,
    )

    assert "models" not in results


def test_mock_only_preflight_rejects_missing_runner(
    project: FixtureProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_non_model_checks(monkeypatch)

    with pytest.raises(PreflightError, match="mock-only preflight requires an injected session runner"):
        preflight.run_preflight(project.config, project.config.state_dir)


def test_unexpected_check_error_becomes_audited_failure(
    project: FixtureProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preflight,
        "_check_paths",
        lambda _config: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )
    monkeypatch.setattr(preflight, "_check_git", lambda _config: "ok")
    monkeypatch.setattr(
        preflight,
        "_check_disk",
        lambda _config, _preflight_config: "ok",
    )
    monkeypatch.setattr(
        preflight,
        "_check_credentials",
        lambda _credentials: "ok",
    )

    with pytest.raises(PreflightError, match="fs_paths: unexpected"):
        preflight.run_preflight(project.config, project.config.state_dir, skip_smoke=True)

    audit_event = json.loads(
        (Path(project.config.state_dir) / "audit.jsonl").read_text().splitlines()[-1]
    )
    assert audit_event["passed"] is False
    assert audit_event["checks"]["fs_paths"] == {
        "status": "failed",
        "detail": "unexpected",
        "error_type": "RuntimeError",
    }


def test_missing_required_path_fails(project: FixtureProject) -> None:
    project.evidence.rmdir()

    with pytest.raises(PreflightError, match="evidence path does not exist"):
        preflight._check_paths(project.config)


def test_git_failure_is_actionable(
    project: FixtureProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_git(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.CalledProcessError(128, ["git", "rev-parse"])

    monkeypatch.setattr(subprocess, "run", failed_git)

    with pytest.raises(PreflightError, match="git check failed for repository fixture-repo"):
        preflight._check_git(project.config)


def test_missing_credential_fails(project: FixtureProject, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPATCHER_FIXTURE_TOKEN", raising=False)

    with pytest.raises(
        PreflightError,
        match="missing environment variables: DISPATCHER_FIXTURE_TOKEN",
    ):
        preflight._check_credentials(["DISPATCHER_FIXTURE_TOKEN"])


def test_disk_check_covers_registered_repository_paths(
    project: FixtureProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[Path] = []

    def disk_usage(path: Path) -> SimpleNamespace:
        checked.append(path)
        return SimpleNamespace(free=500 * 1024 * 1024)

    monkeypatch.setattr(preflight.shutil, "disk_usage", disk_usage)

    assert project.config.preflight is not None
    result = preflight._check_disk(project.config, project.config.preflight)

    assert result == "ok (500 MB minimum across 3 paths)"
    assert set(checked) == {project.root, project.repository, project.evidence}


def test_disk_failure_identifies_low_target(
    project: FixtureProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def disk_usage(path: Path) -> SimpleNamespace:
        free_mb = 50 if path == project.evidence else 500
        return SimpleNamespace(free=free_mb * 1024 * 1024)

    monkeypatch.setattr(preflight.shutil, "disk_usage", disk_usage)

    assert project.config.preflight is not None
    with pytest.raises(
        PreflightError,
        match="disk free below required 100 MB: evidence:fixture-repo:1=50 MB",
    ):
        preflight._check_disk(
            project.config,
            project.config.preflight.model_copy(update={"disk_space_min_mb": 100}),
        )


def test_model_failure_is_reported(project: FixtureProject) -> None:
    def runner(**_kwargs: Any) -> SessionResult:
        return SessionResult(
            session_id="",
            exit_code=1,
            chat_response="model not found",
            evidence_written=[],
        )

    assert project.config.preflight is not None
    with pytest.raises(PreflightError, match="fixture/executor: exit code 1"):
        preflight._check_models(project.config, project.config.preflight, runner)


def test_mock_runner_answers_smoke_probe() -> None:
    result = MockRunner()(
        prompt="Reply with exactly: OK",
        model="fixture/model",
        title="smoke-test Fixture Model",
    )

    assert result.exit_code == 0
    assert result.chat_response == "OK"


def test_preflight_config_is_explicit(project: FixtureProject) -> None:
    values = config_values(project)

    assert values["preflight"]["require_git_remote"] is True
