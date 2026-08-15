from __future__ import annotations

import hashlib
import platform
import sys
import uuid
from pathlib import Path

import pytest
from helpers import create_fixture_project, valid_plan_values
from pydantic import ValidationError

from dispatcher.execution import SequentialExecutionCoordinator
from dispatcher.plan import NormalizedPlan, VerificationCheck
from dispatcher.sequential import SequentialWorkflow
from dispatcher.state_store import StateStore
from dispatcher.verification import (
    DarwinSeatbeltBackend,
    DirectTestBackend,
    LinuxBubblewrapBackend,
    VerificationError,
    VerificationRunner,
)


def _step(tmp_path: Path, *, argv: list[str], timeout: int = 10, maximum: int = 65536):
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = create_fixture_project(tmp_path)
    values = valid_plan_values(project)
    values["steps"][0]["acceptance_criteria"][0]["check"].update(
        {
            "argv": argv,
            "timeout_seconds": timeout,
            "max_output_bytes": maximum,
        }
    )
    return project, NormalizedPlan.model_validate(values).steps[0]


def test_shared_fixture_coordinator_uses_direct_test_backend_without_seatbelt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dispatcher.verification.platform.system", lambda: "Linux")
    project, step = _step(tmp_path, argv=[sys.executable, "-c", "print('verified')"])
    store = StateStore(project.state, heartbeat_seconds=30, stale_after_seconds=120)
    workflow = SequentialWorkflow(project.config, store, owner_id="test-backend-owner")
    coordinator = SequentialExecutionCoordinator(
        project.config,
        store,
        workflow,
        owner_id="test-backend-owner",
    )

    runner = coordinator._verification_runner.__self__

    assert isinstance(runner, VerificationRunner)
    assert isinstance(runner.backend, DirectTestBackend)
    assert coordinator._verification_runner(step, project.repository)[0].status == "passed"


def test_verification_check_requires_argv_array_and_deny_network() -> None:
    with pytest.raises(ValidationError):
        VerificationCheck.model_validate(
            {
                "argv": "pytest -q",
                "working_directory": "repository",
                "timeout_seconds": 10,
                "max_output_bytes": 65536,
                "expected_exit_codes": [0],
                "network_policy": "deny",
            }
        )
    with pytest.raises(ValidationError):
        VerificationCheck.model_validate(
            {
                "argv": ["pytest", "-q"],
                "working_directory": "repository",
                "timeout_seconds": 10,
                "max_output_bytes": 65536,
                "expected_exit_codes": [0],
                "network_policy": "inherit",
            }
        )


def test_dispatcher_verification_records_passing_transcript(tmp_path: Path) -> None:
    project, step = _step(tmp_path, argv=[sys.executable, "-c", "print('verified')"])

    result = VerificationRunner(DirectTestBackend()).run(step, project.repository)[0]

    assert result.status == "passed"
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.output_truncated is False
    assert len(result.transcript_sha256) == 64


def test_dispatcher_verification_fails_on_exit_timeout_and_output_bound(tmp_path: Path) -> None:
    project, failed_step = _step(tmp_path / "failed", argv=[sys.executable, "-c", "raise SystemExit(3)"])
    failed = VerificationRunner(DirectTestBackend()).run(failed_step, project.repository)[0]
    project, timeout_step = _step(
        tmp_path / "timeout",
        argv=[sys.executable, "-c", "import time; time.sleep(5)"],
        timeout=1,
    )
    timed_out = VerificationRunner(DirectTestBackend()).run(timeout_step, project.repository)[0]
    project, output_step = _step(
        tmp_path / "output",
        argv=[sys.executable, "-c", "print('x' * 100000)"],
        maximum=1024,
    )
    oversized = VerificationRunner(DirectTestBackend()).run(output_step, project.repository)[0]

    assert failed.status == "failed" and failed.exit_code == 3
    assert timed_out.status == "failed" and timed_out.timed_out is True
    assert oversized.status == "failed" and oversized.output_truncated is True


@pytest.mark.skipif(platform.system() != "Darwin", reason="requires macOS Seatbelt")
def test_darwin_development_backend_denies_network_and_external_writes(tmp_path: Path) -> None:
    network_script = (
        "import socket; s=socket.socket(); "
        "\ntry: s.bind(('127.0.0.1', 0))"
        "\nexcept PermissionError: raise SystemExit(0)"
        "\nraise SystemExit(7)"
    )
    project, network_step = _step(
        tmp_path / "network",
        argv=[sys.executable, "-c", network_script],
    )
    network = VerificationRunner(DarwinSeatbeltBackend()).run(
        network_step, project.repository
    )[0]
    forbidden = Path("/var/tmp") / f"dispatcher-step19-{uuid.uuid4().hex}"
    write_script = (
        "from pathlib import Path; p=Path(" + repr(str(forbidden)) + ")"
        "\ntry: p.write_text('forbidden')"
        "\nexcept PermissionError: raise SystemExit(0)"
        "\nraise SystemExit(7)"
    )
    project, write_step = _step(
        tmp_path / "write",
        argv=[sys.executable, "-c", write_script],
    )
    write = VerificationRunner(DarwinSeatbeltBackend()).run(
        write_step, project.repository
    )[0]

    assert network.status == "passed"
    assert write.status == "passed"
    assert not forbidden.exists()


@pytest.mark.skipif(platform.system() == "Linux", reason="tests unavailable backend")
def test_linux_production_backend_fails_closed_when_unavailable() -> None:
    with pytest.raises(VerificationError, match="unavailable"):
        LinuxBubblewrapBackend()


def test_linux_production_backend_command_unshares_network_and_mounts_only_runtime_roots(
    tmp_path: Path,
) -> None:
    backend = object.__new__(LinuxBubblewrapBackend)
    backend._executable = "/usr/bin/bwrap"  # type: ignore[attr-defined]
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()

    command = backend.command(("python", "-c", "print('ok')"), workspace, home)

    assert "--unshare-all" in command
    assert "--unshare-net" in command
    assert "--bind" in command
    assert str(workspace) in command
    assert "/workspace" in command
    assert "--ro-bind" in command
    assert "/Users" not in command
    assert str(Path.home()) not in command


def test_check_can_write_large_workspace_artifacts_beyond_output_bound(tmp_path: Path) -> None:
    script = (
        "from pathlib import Path; p=Path('big-artifact.bin')"
        "\np.write_bytes(b'x' * (1024 * 1024))"
        "\nassert p.stat().st_size == 1024 * 1024"
        "\nprint('verified')"
    )
    project, step = _step(
        tmp_path,
        argv=[sys.executable, "-c", script],
        maximum=1024,
    )

    result = VerificationRunner(DirectTestBackend()).run(step, project.repository)[0]

    assert result.status == "passed"
    assert result.exit_code == 0
    assert result.output_truncated is False
    assert result.stdout_sha256


def test_check_excess_stdout_and_stderr_remain_bounded(tmp_path: Path) -> None:
    project, stdout_step = _step(
        tmp_path / "stdout",
        argv=[sys.executable, "-c", "import sys; sys.stdout.write('o' * 100000)"],
        maximum=1024,
    )
    stdout_result = VerificationRunner(DirectTestBackend()).run(stdout_step, project.repository)[0]
    project, stderr_step = _step(
        tmp_path / "stderr",
        argv=[sys.executable, "-c", "import sys; sys.stderr.write('e' * 100000)"],
        maximum=1024,
    )
    stderr_result = VerificationRunner(DirectTestBackend()).run(stderr_step, project.repository)[0]

    assert stdout_result.status == "failed"
    assert stdout_result.output_truncated is True
    assert stdout_result.stdout_sha256 == hashlib.sha256(b"o" * 1024).hexdigest()
    assert stderr_result.status == "failed"
    assert stderr_result.output_truncated is True
    assert stderr_result.stderr_sha256 == hashlib.sha256(b"e" * 1024).hexdigest()


def test_parallel_verification_is_safe_and_deterministic(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    def run_echo(root: Path, tag: str) -> str:
        project, step = _step(
            root / tag,
            argv=[sys.executable, "-c", f"print('echo-{tag}')"],
        )
        return VerificationRunner(DirectTestBackend()).run(step, project.repository)[0].stdout_sha256

    with ThreadPoolExecutor(max_workers=8) as executor:
        first = list(
            executor.map(
                lambda tag: run_echo(tmp_path / "first", tag),
                (f"tag-{index}" for index in range(16)),
            )
        )
    with ThreadPoolExecutor(max_workers=8) as executor:
        second = list(
            executor.map(
                lambda tag: run_echo(tmp_path / "second", tag),
                (f"tag-{index}" for index in range(16)),
            )
        )

    expected = [hashlib.sha256(f"echo-tag-{index}\n".encode("utf-8")).hexdigest() for index in range(16)]
    assert len(set(first)) == 16
    assert first == expected
    assert first == second
