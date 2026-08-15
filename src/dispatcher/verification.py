"""Dispatcher-owned structured verification with bounded isolation backends."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import select
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from .config import Config, ContractModel, Identifier
from .plan import PlanStep, Sha256, VerificationCheck
from .security import redact_text


class VerificationError(RuntimeError):
    """A structured check could not be executed or did not satisfy policy."""


class AuthoritativeVerification(ContractModel):
    """One dispatcher-generated check result bound to an immutable transcript."""

    check_id: Identifier
    status: Literal["passed", "failed"]
    argv: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    output_truncated: bool
    stdout_sha256: Sha256
    stderr_sha256: Sha256
    transcript_sha256: Sha256
    duration_ms: int = Field(ge=0)
    backend: Identifier
    summary: str = Field(min_length=1, max_length=2000)


class IsolationBackend(Protocol):
    """Wrap one argv command in a platform isolation boundary."""

    name: str
    production_ready: bool

    def command(self, argv: tuple[str, ...], workspace: Path, home: Path) -> list[str]: ...


class DirectTestBackend:
    """Environment-neutral test backend for dispatcher-owned check semantics."""

    name = "direct-test-v1"
    production_ready = False

    def command(self, argv: tuple[str, ...], _workspace: Path, _home: Path) -> list[str]:
        return list(argv)


class DarwinSeatbeltBackend:
    """macOS local verification containment for dispatcher-owned checks."""

    name = "darwin-seatbelt-v1"
    production_ready = True

    def __init__(self) -> None:
        if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            raise VerificationError("darwin_seatbelt_v1 verification backend is unavailable")

    def command(self, argv: tuple[str, ...], workspace: Path, home: Path) -> list[str]:
        profile = (
            '(version 1)\n'
            '(allow default)\n'
            '(deny network*)\n'
            '(deny file-write*)\n'
            f'(allow file-write* (subpath "{_seatbelt_escape(workspace)}") '
            f'(subpath "{_seatbelt_escape(home.parent)}") (literal "/dev/null"))\n'
        )
        return ["/usr/bin/sandbox-exec", "-p", profile, *argv]


class LinuxBubblewrapBackend:
    """Production Linux filesystem namespace with no network namespace."""

    name = "linux-bwrap-v1"
    production_ready = True

    def __init__(self) -> None:
        executable = shutil.which("bwrap")
        if platform.system() != "Linux" or executable is None:
            raise VerificationError("linux_bwrap_v1 verification backend is unavailable")
        self._executable = executable

    def command(self, argv: tuple[str, ...], workspace: Path, home: Path) -> list[str]:
        command = [
            self._executable,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--unshare-net",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        for system_path in ("/bin", "/usr", "/usr/local", "/lib", "/lib64", "/etc"):
            if Path(system_path).exists():
                command.extend(("--ro-bind", system_path, system_path))
        command.extend(
            (
                "--bind",
                str(workspace),
                "/workspace",
                "--dir",
                "/tmp/home",
                "--chdir",
                "/workspace",
                "--setenv",
                "HOME",
                "/tmp/home",
                "--setenv",
                "PATH",
                "/usr/local/bin:/usr/bin:/bin",
                "--",
                *argv,
            )
        )
        return command


def verification_backend(config: Config) -> IsolationBackend:
    """Create the explicitly configured backend or fail closed."""
    name = config.execution.verification_backend
    if name == "direct_test_v1":
        return DirectTestBackend()
    if name == "darwin_seatbelt_v1":
        return DarwinSeatbeltBackend()
    if name == "linux_bwrap_v1":
        return LinuxBubblewrapBackend()
    raise VerificationError(f"unknown verification backend: {name}")


class VerificationRunner:
    """Execute plan-owned checks in disposable repository copies."""

    def __init__(self, backend: IsolationBackend) -> None:
        self.backend = backend

    @classmethod
    def from_config(cls, config: Config) -> "VerificationRunner":
        return cls(verification_backend(config))

    def run(self, step: PlanStep, repository: Path) -> tuple[AuthoritativeVerification, ...]:
        return tuple(
            self._run_check(criterion.criterion_id, criterion.check, repository)
            for criterion in step.acceptance_criteria
        )

    def _run_check(
        self,
        check_id: str,
        check: VerificationCheck,
        repository: Path,
    ) -> AuthoritativeVerification:
        with tempfile.TemporaryDirectory(prefix="dispatcher-verification-") as temporary:
            root = Path(temporary).resolve()
            workspace = root / "workspace"
            home = root / "home"
            home.mkdir(mode=0o700)
            shutil.copytree(repository, workspace, symlinks=True)
            environment = {
                "HOME": str(home),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTEST_ADDOPTS": "-p no:cacheprovider",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "TMPDIR": str(root),
            }
            command = self.backend.command(check.argv, workspace, home)
            started = time.monotonic()
            exit_code, timed_out, stdout, stderr, output_truncated = _run_bounded(
                command,
                cwd=workspace,
                environment=environment,
                timeout_seconds=check.timeout_seconds,
                max_output_bytes=check.max_output_bytes,
            )
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            status: Literal["passed", "failed"] = (
                "passed"
                if not timed_out
                and not output_truncated
                and exit_code in check.expected_exit_codes
                else "failed"
            )
            stdout_sha256 = hashlib.sha256(stdout).hexdigest()
            stderr_sha256 = hashlib.sha256(stderr).hexdigest()
            transcript = {
                "argv": list(check.argv),
                "backend": self.backend.name,
                "check_id": check_id,
                "duration_ms": duration_ms,
                "exit_code": exit_code,
                "output_truncated": output_truncated,
                "status": status,
                "stderr_sha256": stderr_sha256,
                "stdout_sha256": stdout_sha256,
                "timed_out": timed_out,
            }
            transcript_sha256 = hashlib.sha256(
                json.dumps(transcript, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if timed_out:
                summary = "dispatcher verification timed out"
            elif output_truncated:
                summary = "dispatcher verification exceeded its output bound"
            elif status == "failed":
                excerpt = redact_text((stderr or stdout).decode("utf-8", errors="replace"))[-1800:]
                summary = f"dispatcher verification exited with status {exit_code}: {excerpt or '[no output]'}"
            else:
                summary = redact_text(
                    f"dispatcher verification exited with status {exit_code}"
                )
            return AuthoritativeVerification(
                check_id=check_id,
                status=status,
                argv=check.argv,
                exit_code=exit_code,
                timed_out=timed_out,
                output_truncated=output_truncated,
                stdout_sha256=stdout_sha256,
                stderr_sha256=stderr_sha256,
                transcript_sha256=transcript_sha256,
                duration_ms=duration_ms,
                backend=self.backend.name,
                summary=summary,
            )


def _run_bounded(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    max_output_bytes: int,
) -> tuple[int | None, bool, bytes, bytes, bool]:
    """Run one check with bounded stdout/stderr without limiting its own files.

    The output bound applies only to captured stdout/stderr. Files the check
    legitimately writes inside the disposable workspace are unrestricted.
    Non-blocking reads via ``select.poll`` keep this safe under threaded batch
    execution: no ``preexec_fn`` and no process-global resource limits.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    try:
        stdout = bytearray()
        stderr = bytearray()
        timed_out = False
        deadline = time.monotonic() + timeout_seconds
        bound = max_output_bytes + 1
        poller = select.poll()
        stdout_fd = process.stdout.fileno()
        stderr_fd = process.stderr.fileno()
        os.set_blocking(stdout_fd, False)
        os.set_blocking(stderr_fd, False)
        poller.register(stdout_fd, select.POLLIN)
        poller.register(stderr_fd, select.POLLIN)
        remaining_stdout = bound
        remaining_stderr = bound
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_check_process(process)
                break
            if remaining_stdout <= 0 or remaining_stderr <= 0:
                # One stream already exceeded the output bound; further reads
                # are unnecessary and could block the producer on a full pipe.
                _kill_check_process(process)
                break
            try:
                events = poller.poll(remaining * 1000)
            except OSError:
                break
            if not events:
                continue
            for fd, _event in events:
                if fd == stdout_fd and remaining_stdout > 0:
                    chunk = os.read(stdout_fd, min(65536, remaining_stdout))
                    if chunk:
                        stdout.extend(chunk)
                        remaining_stdout -= len(chunk)
                elif fd == stderr_fd and remaining_stderr > 0:
                    chunk = os.read(stderr_fd, min(65536, remaining_stderr))
                    if chunk:
                        stderr.extend(chunk)
                        remaining_stderr -= len(chunk)
            if process.poll() is not None:
                for fd, remaining in (
                    (stdout_fd, remaining_stdout),
                    (stderr_fd, remaining_stderr),
                ):
                    while remaining > 0:
                        try:
                            chunk = os.read(fd, min(65536, remaining))
                        except (BlockingIOError, OSError):
                            break
                        if not chunk:
                            break
                        if fd == stdout_fd:
                            stdout.extend(chunk)
                        else:
                            stderr.extend(chunk)
                        remaining -= len(chunk)
                break
    finally:
        process.wait(timeout=10)
    output_truncated = len(stdout) > max_output_bytes or len(stderr) > max_output_bytes
    return (
        process.returncode,
        timed_out,
        bytes(stdout[:max_output_bytes]),
        bytes(stderr[:max_output_bytes]),
        output_truncated,
    )


def _kill_check_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _seatbelt_escape(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')
