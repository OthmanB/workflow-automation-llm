"""Pinned OpenCode 1.18.11 adapter with bounded streaming process management."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import psutil

from .security import ensure_private_directory, open_private_text, redact_text, redact_value

logger = logging.getLogger(__name__)

OPENCODE_BIN = "opencode"
SUPPORTED_OPENCODE_VERSION = "1.18.11"
FailureCategory = Literal[
    "timeout",
    "interrupted",
    "connection",
    "rate_limit",
    "context_overflow",
    "quota",
    "authentication",
    "permission",
    "protocol",
    "unknown",
]
_SUPPORTED_EVENT_TYPES = frozenset(
    {"error", "reasoning", "step_finish", "step_start", "text", "tool_use"}
)
_VERSION_PATTERN = re.compile(r"\b(\d+\.\d+\.\d+)\b")


class OpenCodeAdapterError(RuntimeError):
    """Base failure raised before a session result can affect workflow state."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        stderr: str = "",
        stdout_log_path: str = "",
        stderr_log_path: str = "",
        category: FailureCategory = "unknown",
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr
        self.stdout_log_path = stdout_log_path
        self.stderr_log_path = stderr_log_path
        self.category = category


class OpenCodeVersionError(OpenCodeAdapterError):
    """The installed OpenCode binary is unavailable or unsupported."""


class OpenCodeProtocolError(OpenCodeAdapterError):
    """The supported OpenCode JSONL contract was malformed or incomplete."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, category="protocol", **kwargs)


class OpenCodeProcessError(OpenCodeAdapterError):
    """OpenCode returned a nonzero exit or a structured error event."""


class OpenCodeProcessIdentityError(OpenCodeProcessError):
    """A process ID no longer identifies the process the dispatcher started."""


class OpenCodeTimeoutError(OpenCodeAdapterError):
    """The OpenCode process group did not complete by its configured deadline."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, category="timeout", **kwargs)


class OpenCodeSessionError(OpenCodeAdapterError):
    """A requested persisted OpenCode session was missing, foreign, or stale."""


def refresh_opencode_credentials(
    state_dir: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Atomically snapshot the operator's active OpenCode credentials for one project."""
    parent = os.environ if environment is None else environment
    data_home = Path(
        parent.get(
            "XDG_DATA_HOME",
            str(Path(parent.get("HOME", str(Path.home()))) / ".local" / "share"),
        )
    )
    source = data_home / "opencode" / "auth.json"
    if not source.is_file():
        raise OpenCodeSessionError(f"active OpenCode credential store is unavailable: {source}")
    target_dir = ensure_private_directory(
        Path(state_dir) / "opencode-child" / "home" / ".local" / "share" / "opencode"
    )
    target = target_dir / "auth.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".auth.", suffix=".tmp", dir=target_dir)
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as source_file, os.fdopen(descriptor, "wb") as target_file:
            shutil.copyfileobj(source_file, target_file)
            target_file.flush()
            os.fsync(target_file.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, target)
        target.chmod(0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


@dataclass(frozen=True)
class SessionDescriptor:
    """Structured session metadata returned by ``opencode session list``."""

    session_id: str
    title: str
    updated: int
    directory: str


@dataclass(frozen=True)
class SessionLifecycleCallbacks:
    """Synchronous durable-state hooks around the external process lifecycle."""

    on_process_started: Callable[[int, float], None]
    on_session_identified: Callable[[str], None]


@dataclass
class SessionResult:
    """The validated outcome of one OpenCode command."""

    session_id: str
    exit_code: int
    chat_response: str
    evidence_written: list[str]
    usage: dict[str, Any] = field(default_factory=dict)
    cost: float | None = None
    raw: list[dict[str, Any]] = field(default_factory=list)
    elapsed_s: float = 0.0
    stdout_log_path: str = ""
    stderr_log_path: str = ""
    parent_session_id: str = ""
    opencode_version: str = ""


class OpenCodeJsonlDecoder:
    """Incrementally decode the exact JSONL events emitted by OpenCode 1.18.11."""

    def __init__(
        self,
        *,
        max_output_bytes: int,
        on_session_identified: Callable[[str], None] | None = None,
    ) -> None:
        self._max_output_bytes = max_output_bytes
        self._raw: list[dict[str, Any]] = []
        self._raw_bytes = 0
        self._chat_parts: list[str] = []
        self._chat_bytes = 0
        self._usage: dict[str, Any] = {}
        self._cost: float | None = None
        self._session_id = ""
        self._structured_error = ""
        self._structured_error_name = ""
        self._structured_error_message = ""
        self._saw_step_finish = False
        self._on_session_identified = on_session_identified

    def consume_line(self, line: str, *, line_number: int) -> None:
        """Decode one complete JSONL line and retain only bounded safe metadata."""
        try:
            event = json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
        except json.JSONDecodeError as exc:
            raise OpenCodeProtocolError(f"malformed OpenCode JSONL at line {line_number}") from exc
        if not isinstance(event, dict):
            raise OpenCodeProtocolError(f"OpenCode JSONL line {line_number} must be an object")

        event_type = event.get("type")
        if event_type not in _SUPPORTED_EVENT_TYPES:
            raise OpenCodeProtocolError(
                f"unsupported OpenCode 1.18.11 event type at line {line_number}: {event_type!r}"
            )

        session_id = event.get("sessionID")
        if not isinstance(session_id, str) or not session_id:
            raise OpenCodeProtocolError(f"OpenCode event at line {line_number} is missing sessionID")
        if self._session_id and self._session_id != session_id:
            raise OpenCodeProtocolError("OpenCode event stream contains multiple session IDs")
        if not self._session_id:
            self._session_id = session_id
            if self._on_session_identified is not None:
                self._on_session_identified(session_id)

        part = event.get("part")
        if event_type != "error" and not isinstance(part, dict):
            raise OpenCodeProtocolError(
                f"OpenCode {event_type!r} event at line {line_number} is missing part"
            )

        if event_type == "text":
            assert isinstance(part, dict)
            self._consume_text(part, line_number)
        elif event_type == "step_finish":
            assert isinstance(part, dict)
            self._consume_step_finish(part, line_number)
        elif event_type == "error":
            self._consume_error(event, line_number)

        self._append_metadata(event)

    def finish(self, *, require_response: bool) -> tuple[list[dict[str, Any]], str, dict[str, Any], str, float | None]:
        """Return the validated result or fail before the caller persists state."""
        if self._structured_error:
            raise OpenCodeProcessError(
                f"OpenCode structured error: {self._structured_error}",
                category=classify_provider_failure(
                    self._structured_error_name,
                    self._structured_error_message,
                ),
            )
        if not self._session_id:
            raise OpenCodeProtocolError("OpenCode output did not include a sessionID")
        if require_response and not self._chat_parts:
            raise OpenCodeProtocolError("OpenCode output did not include final assistant text")
        if require_response and not self._saw_step_finish:
            raise OpenCodeProtocolError("OpenCode output did not include step_finish")
        return (
            self._raw,
            self._chat_parts[-1] if self._chat_parts else "",
            self._usage,
            self._session_id,
            self._cost,
        )

    def _consume_text(self, part: dict[str, Any], line_number: int) -> None:
        if part.get("type") != "text":
            raise OpenCodeProtocolError(f"OpenCode text event at line {line_number} has invalid part.type")
        text = part.get("text")
        if not isinstance(text, str) or not text.strip():
            raise OpenCodeProtocolError(f"OpenCode text event at line {line_number} is missing text")
        text = redact_text(text.strip())
        self._chat_bytes += len(text.encode("utf-8"))
        if self._chat_bytes > self._max_output_bytes:
            raise OpenCodeProtocolError("OpenCode assistant text exceeded configured output limit")
        self._chat_parts.append(text)

    def _consume_step_finish(self, part: dict[str, Any], line_number: int) -> None:
        if part.get("type") != "step-finish":
            raise OpenCodeProtocolError(
                f"OpenCode step_finish event at line {line_number} has invalid part.type"
            )
        tokens = part.get("tokens")
        if not isinstance(tokens, dict):
            raise OpenCodeProtocolError(
                f"OpenCode step_finish event at line {line_number} is missing tokens"
            )
        if not all(isinstance(tokens.get(key), int) for key in ("total", "input", "output", "reasoning")):
            raise OpenCodeProtocolError(
                f"OpenCode step_finish event at line {line_number} has invalid tokens"
            )
        cost = part.get("cost")
        if not isinstance(cost, (int, float)):
            raise OpenCodeProtocolError(
                f"OpenCode step_finish event at line {line_number} has invalid cost"
            )
        self._usage = redact_value(tokens)
        self._cost = float(cost)
        self._saw_step_finish = True

    def _consume_error(self, event: dict[str, Any], line_number: int) -> None:
        error = event.get("error")
        if not isinstance(error, dict):
            raise OpenCodeProtocolError(f"OpenCode error event at line {line_number} is missing error")
        data = error.get("data")
        message = data.get("message") if isinstance(data, dict) else None
        name = error.get("name")
        if not isinstance(name, str) or not isinstance(message, str):
            raise OpenCodeProtocolError(f"OpenCode error event at line {line_number} is malformed")
        self._structured_error = f"{name}: {redact_text(message)}"
        self._structured_error_name = name
        self._structured_error_message = redact_text(message)

    def _append_metadata(self, event: dict[str, Any]) -> None:
        part = event.get("part")
        metadata: dict[str, Any] = {
            "type": event["type"],
            "timestamp": event.get("timestamp"),
            "sessionID": event["sessionID"],
        }
        if isinstance(part, dict):
            metadata["part"] = {
                key: part[key]
                for key in ("id", "messageID", "sessionID", "type", "reason", "snapshot")
                if key in part
            }
        encoded_size = len(json.dumps(metadata, separators=(",", ":")).encode("utf-8"))
        if self._raw_bytes + encoded_size <= self._max_output_bytes:
            self._raw.append(metadata)
            self._raw_bytes += encoded_size


def _check_binary() -> str:
    """Verify that the installed CLI exactly matches the pinned adapter version."""
    try:
        result = subprocess.run(
            [OPENCODE_BIN, "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OpenCodeVersionError(f"cannot execute {OPENCODE_BIN} --version: {exc}") from exc
    if result.returncode != 0:
        raise OpenCodeVersionError(
            f"{OPENCODE_BIN} --version failed with exit {result.returncode}: {redact_text(result.stderr)}"
        )
    match = _VERSION_PATTERN.search(result.stdout)
    if not match:
        raise OpenCodeVersionError(f"could not parse {OPENCODE_BIN} version output")
    version = match.group(1)
    if version != SUPPORTED_OPENCODE_VERSION:
        raise OpenCodeVersionError(
            f"unsupported OpenCode version {version}; expected {SUPPORTED_OPENCODE_VERSION}"
        )
    return version


def run_session(
    *,
    prompt: str,
    model: str,
    variant: str,
    session_id: str | None,
    mode: Literal["new", "resume", "fork"],
    workdir: str | Path,
    title: str,
    auto_approve: bool,
    timeout_seconds: int,
    termination_grace_seconds: int,
    max_output_bytes: int,
    state_dir: str | Path,
    credential_state_dir: str | Path | None = None,
    permission_config: Mapping[str, Any] | None = None,
    environment_passthrough: Iterable[str] = (),
    inherit_opencode_config: bool = False,
    require_response: bool = True,
    snapshot_dirs: list[str] | None = None,
    lifecycle: SessionLifecycleCallbacks | None = None,
) -> SessionResult:
    """Run one OpenCode command with a private child environment and bounded output."""
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be positive")
    if termination_grace_seconds < 1:
        raise ValueError("termination_grace_seconds must be positive")
    if max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    _validate_session_request(mode, session_id)
    _check_binary()

    command = _build_command(
        model=model,
        variant=variant,
        session_id=session_id,
        mode=mode,
        workdir=workdir,
        title=title,
        auto_approve=auto_approve,
    )
    snapshots_before = {directory: _snapshot_dir(directory) for directory in snapshot_dirs or []}
    logs_dir = ensure_private_directory(Path(state_dir) / "opencode-events")
    run_token = uuid.uuid4().hex
    stdout_log = logs_dir / f"{run_token}.stdout.jsonl"
    stderr_log = logs_dir / f"{run_token}.stderr.log"
    child_env = build_child_environment(
        os.environ,
        state_dir=state_dir,
        credential_state_dir=credential_state_dir,
        permission_config=permission_config,
        environment_passthrough=environment_passthrough,
        inherit_opencode_config=inherit_opencode_config,
    )

    logger.info("opencode run model=%s variant=%s session_mode=%s", model, variant, mode)
    started = time.monotonic()

    def session_identified(returned_id: str) -> None:
        if mode == "resume" and returned_id != session_id:
            raise OpenCodeSessionError(
                f"resume returned session {returned_id!r}, expected {session_id!r}"
            )
        if mode == "fork" and returned_id == session_id:
            raise OpenCodeSessionError("fork returned the parent session ID")
        if lifecycle is not None:
            lifecycle.on_session_identified(returned_id)

    try:
        exit_code, decoder, stderr = _run_streaming_process(
            command=command,
            prompt=prompt,
            environment=child_env,
            timeout_seconds=timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            max_output_bytes=max_output_bytes,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            on_process_started=(lifecycle.on_process_started if lifecycle is not None else None),
            on_session_identified=session_identified,
        )
        if exit_code != 0:
            category: FailureCategory = (
                "interrupted"
                if exit_code in {-signal.SIGINT, -signal.SIGTERM}
                else classify_provider_failure("", stderr)
            )
            raise OpenCodeProcessError(
                f"OpenCode exited with status {exit_code}: {stderr or '[no stderr]'}",
                exit_code=exit_code,
                stderr=stderr,
                category=category,
                stdout_log_path=str(stdout_log),
                stderr_log_path=str(stderr_log),
            )
        raw, chat, usage, returned_session_id, cost = decoder.finish(require_response=require_response)
    except OpenCodeAdapterError as exc:
        if "exit_code" in locals() and exc.exit_code is None:
            exc.exit_code = exit_code
        exc.stdout_log_path = exc.stdout_log_path or str(stdout_log)
        exc.stderr_log_path = exc.stderr_log_path or str(stderr_log)
        raise
    elapsed = time.monotonic() - started
    if mode == "resume" and returned_session_id != session_id:
        raise OpenCodeSessionError(
            f"resume returned session {returned_session_id!r}, expected {session_id!r}"
        )
    if mode == "fork" and returned_session_id == session_id:
        raise OpenCodeSessionError("fork returned the parent session ID")

    evidence = _new_evidence(snapshot_dirs or [], snapshots_before)
    return SessionResult(
        session_id=returned_session_id,
        exit_code=exit_code,
        chat_response=chat,
        evidence_written=evidence,
        usage=usage,
        cost=cost,
        raw=raw,
        elapsed_s=elapsed,
        stdout_log_path=str(stdout_log),
        stderr_log_path=str(stderr_log),
        parent_session_id=session_id if mode == "fork" and session_id else "",
        opencode_version=SUPPORTED_OPENCODE_VERSION,
    )


def classify_provider_failure(name: str, message: str) -> FailureCategory:
    """Map known OpenCode/provider errors to safe retry categories."""
    text = f"{name} {message}".lower()
    if "contextoverflow" in text or "context overflow" in text or "outputlength" in text:
        return "context_overflow"
    if "ratelimit" in text or "rate limit" in text or "too many requests" in text:
        return "rate_limit"
    if "quota" in text or "billing" in text or "insufficient credit" in text:
        return "quota"
    if "auth" in text or "unauthorized" in text or "invalid api key" in text:
        return "authentication"
    if "permission" in text or "forbidden" in text or "access denied" in text:
        return "permission"
    if "connection" in text or "network" in text or "econn" in text or "timed out" in text:
        return "connection"
    if "abort" in text or "interrupt" in text or "cancel" in text:
        return "interrupted"
    return "unknown"


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous duplicate fields in an external OpenCode JSONL event."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OpenCodeProtocolError(f"duplicate JSON key in OpenCode event: {key}")
        result[key] = value
    return result


def list_sessions(*, limit: int = 1000) -> list[SessionDescriptor]:
    """Return an exact, structured session list from the pinned OpenCode CLI."""
    if limit < 1:
        raise ValueError("limit must be positive")
    _check_binary()
    try:
        result = subprocess.run(
            [OPENCODE_BIN, "session", "list", "-n", str(limit), "--format", "json"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OpenCodeSessionError(f"could not list OpenCode sessions: {exc}") from exc
    if result.returncode != 0:
        raise OpenCodeSessionError(
            f"OpenCode session list failed with exit {result.returncode}: {redact_text(result.stderr)}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenCodeSessionError("OpenCode session list returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise OpenCodeSessionError("OpenCode session list JSON must be an array")

    sessions: list[SessionDescriptor] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise OpenCodeSessionError(f"OpenCode session list item {index} must be an object")
        session_id = item.get("id")
        title = item.get("title")
        updated = item.get("updated")
        directory = item.get("directory")
        if not isinstance(session_id, str) or not isinstance(title, str):
            raise OpenCodeSessionError(f"OpenCode session list item {index} has invalid identity")
        if not isinstance(updated, int) or not isinstance(directory, str):
            raise OpenCodeSessionError(f"OpenCode session list item {index} has invalid metadata")
        sessions.append(SessionDescriptor(session_id, title, updated, directory))
    return sessions


def _session_exists(session_id: str) -> bool:
    """Return whether a session ID exists exactly once in structured CLI output."""
    return sum(item.session_id == session_id for item in list_sessions()) == 1


def validate_session_reference(
    *,
    session_id: str,
    registry_entry: Mapping[str, Any],
    workdir: str | Path,
) -> SessionDescriptor:
    """Require a registry-owned session to exist in the active project directory."""
    if registry_entry.get("session_id") != session_id:
        raise OpenCodeSessionError("session ID is not owned by the dispatcher registry entry")
    expected_directory = str(Path(workdir).resolve())
    if registry_entry.get("working_directory") != expected_directory:
        raise OpenCodeSessionError("session registry working directory does not match active project")
    matches = [item for item in list_sessions() if item.session_id == session_id]
    if len(matches) != 1:
        raise OpenCodeSessionError(f"session {session_id!r} was not found exactly once")
    if str(Path(matches[0].directory).resolve()) != expected_directory:
        raise OpenCodeSessionError("OpenCode session directory does not match active project")
    return matches[0]


def build_child_environment(
    parent_environment: Mapping[str, str],
    *,
    state_dir: str | Path,
    credential_state_dir: str | Path | None = None,
    permission_config: Mapping[str, Any] | None,
    environment_passthrough: Iterable[str] = (),
    inherit_opencode_config: bool = False,
) -> dict[str, str]:
    """Build an isolated environment without inherited credentials or OpenCode state."""
    missing_required = [name for name in environment_passthrough if name not in parent_environment]
    if missing_required:
        raise OpenCodeSessionError(
            "required MCP environment variable missing: " + ", ".join(sorted(missing_required))
        )
    runtime_dir = ensure_private_directory(Path(state_dir) / "opencode-child")
    home = ensure_private_directory(runtime_dir / "home")
    config_home = ensure_private_directory(home / ".config")
    cache_home = ensure_private_directory(home / ".cache")
    data_home = ensure_private_directory(home / ".local" / "share")
    if credential_state_dir is not None:
        credential_auth = (
            Path(credential_state_dir)
            / "opencode-child"
            / "home"
            / ".local"
            / "share"
            / "opencode"
            / "auth.json"
        )
        if credential_auth.is_file():
            target_auth_dir = ensure_private_directory(data_home / "opencode")
            target_auth = target_auth_dir / "auth.json"
            if credential_auth.resolve() != target_auth.resolve():
                shutil.copy2(credential_auth, target_auth)
            target_auth.chmod(0o600)
    state_home = ensure_private_directory(home / ".local" / "state")

    environment = (
        dict(parent_environment)
        if inherit_opencode_config
        else {
            key: parent_environment[key]
            for key in ("LANG", "LC_ALL", "LC_CTYPE", "PATH", "TERM", "TMPDIR")
            if key in parent_environment
        }
    )
    for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR"):
        environment.pop(key, None)
    environment.update(
        {
            name: parent_environment[name]
            for name in environment_passthrough
            if name in parent_environment
        }
    )
    environment.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_CACHE_HOME": str(cache_home),
            "XDG_DATA_HOME": str(data_home),
            "XDG_STATE_HOME": str(state_home),
        }
    )
    if inherit_opencode_config:
        configured_dir = parent_environment.get("OPENCODE_CONFIG_DIR")
        if configured_dir:
            opencode_config_dir = Path(configured_dir)
        else:
            config_home = Path(
                parent_environment.get(
                    "XDG_CONFIG_HOME",
                    str(Path(parent_environment.get("HOME", str(Path.home()))) / ".config"),
                )
            )
            opencode_config_dir = config_home / "opencode"
        if opencode_config_dir.is_dir():
            environment["OPENCODE_CONFIG_DIR"] = str(opencode_config_dir)
    if permission_config is not None:
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            dict(permission_config),
            separators=(",", ":"),
        )
    return environment


def _build_command(
    *,
    model: str,
    variant: str,
    session_id: str | None,
    mode: Literal["new", "resume", "fork"],
    workdir: str | Path,
    title: str,
    auto_approve: bool,
) -> list[str]:
    command = [OPENCODE_BIN, "run", "--format", "json", "-m", model, "--dir", str(workdir)]
    if variant:
        command.extend(["--variant", variant])
    if title:
        command.extend(["--title", title])
    if session_id:
        command.extend(["-s", session_id])
    if mode == "fork":
        command.append("--fork")
    if auto_approve:
        command.append("--auto")
    return command


def _run_streaming_process(
    *,
    command: list[str],
    prompt: str,
    environment: Mapping[str, str],
    timeout_seconds: int,
    termination_grace_seconds: int,
    max_output_bytes: int,
    stdout_log: Path,
    stderr_log: Path,
    on_process_started: Callable[[int, float], None] | None,
    on_session_identified: Callable[[str], None] | None,
) -> tuple[int, OpenCodeJsonlDecoder, str]:
    """Stream stdout/stderr without retaining unbounded process output in memory."""
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
            start_new_session=True,
        )
    except OSError as exc:
        raise OpenCodeProcessError(f"could not start OpenCode: {exc}") from exc

    try:
        process_create_time = psutil.Process(process.pid).create_time()
    except psutil.Error as exc:
        _terminate_process_group(process, termination_grace_seconds)
        raise OpenCodeProcessError(
            "could not capture the OS process creation time for the started OpenCode worker"
        ) from exc

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    if on_process_started is not None:
        try:
            on_process_started(process.pid, process_create_time)
        except Exception:
            _terminate_process_group(process, termination_grace_seconds)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
            raise
    streams: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
    _start_stream_reader("stdout", process.stdout, streams)
    _start_stream_reader("stderr", process.stderr, streams)
    _start_stdin_writer(process.stdin, prompt.encode("utf-8"))

    decoder = OpenCodeJsonlDecoder(
        max_output_bytes=max_output_bytes,
        on_session_identified=on_session_identified,
    )
    stderr_collector = _BoundedTextCollector(max_output_bytes)
    stdout_buffer = b""
    stderr_buffer = b""
    stdout_closed = False
    stderr_closed = False
    line_number = 0
    failure: Exception | None = None
    deadline = time.monotonic() + timeout_seconds

    with open_private_text(stdout_log, append=False) as stdout_handle, open_private_text(
        stderr_log, append=False
    ) as stderr_handle:
        while not (stdout_closed and stderr_closed and process.poll() is not None):
            remaining = deadline - time.monotonic()
            if remaining <= 0 and failure is None:
                failure = OpenCodeTimeoutError(
                    f"OpenCode timed out after {timeout_seconds} seconds",
                    stdout_log_path=str(stdout_log),
                    stderr_log_path=str(stderr_log),
                )
                _terminate_process_group(process, termination_grace_seconds)
            try:
                stream_name, chunk = streams.get(timeout=max(0.01, min(remaining, 0.1)))
            except queue.Empty:
                continue
            if chunk is None:
                if stream_name == "stdout":
                    stdout_closed = True
                else:
                    stderr_closed = True
                continue
            if stream_name == "stdout":
                stdout_buffer, lines = _split_complete_lines(stdout_buffer + chunk)
                if len(stdout_buffer) > max_output_bytes and failure is None:
                    failure = OpenCodeProtocolError(
                        "OpenCode emitted an unterminated JSONL line above the configured output limit"
                    )
                    _terminate_process_group(process, termination_grace_seconds)
                for raw_line in lines:
                    line_number += 1
                    line = raw_line.decode("utf-8", errors="replace")
                    stdout_handle.write(redact_text(line) + "\n")
                    if failure is None:
                        try:
                            decoder.consume_line(line, line_number=line_number)
                        except Exception as exc:
                            failure = exc
                            _terminate_process_group(process, termination_grace_seconds)
            else:
                stderr_buffer, lines = _split_complete_lines(stderr_buffer + chunk)
                if len(stderr_buffer) > max_output_bytes:
                    stderr_handle.write(redact_text(stderr_buffer.decode("utf-8", errors="replace")))
                    stderr_collector.add(stderr_buffer.decode("utf-8", errors="replace"))
                    stderr_buffer = b""
                for raw_line in lines:
                    line = redact_text(raw_line.decode("utf-8", errors="replace"))
                    stderr_handle.write(line + "\n")
                    stderr_collector.add(line + "\n")

        if stdout_buffer:
            line_number += 1
            line = stdout_buffer.decode("utf-8", errors="replace")
            stdout_handle.write(redact_text(line) + "\n")
            if failure is None:
                try:
                    decoder.consume_line(line, line_number=line_number)
                except Exception as exc:
                    failure = exc
        if stderr_buffer:
            line = redact_text(stderr_buffer.decode("utf-8", errors="replace"))
            stderr_handle.write(line + "\n")
            stderr_collector.add(line + "\n")

    exit_code = process.wait(timeout=termination_grace_seconds)
    if failure is not None:
        raise failure
    return exit_code, decoder, stderr_collector.value


def _start_stream_reader(
    name: str,
    stream: Any,
    output: queue.Queue[tuple[str, bytes | None]],
) -> None:
    def read_chunks() -> None:
        try:
            while chunk := stream.read(4096):
                output.put((name, chunk))
        finally:
            output.put((name, None))

    threading.Thread(target=read_chunks, name=f"opencode-{name}", daemon=True).start()


def _start_stdin_writer(stream: Any, prompt: bytes) -> None:
    def write_prompt() -> None:
        try:
            stream.write(prompt)
            stream.flush()
        except BrokenPipeError:
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    threading.Thread(target=write_prompt, name="opencode-stdin", daemon=True).start()


def _terminate_process_group(process: subprocess.Popen[bytes], grace_seconds: int) -> None:
    """Terminate the dedicated process group, then kill survivors after grace."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        if process.poll() is not None:
            return
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        if process.poll() is not None:
            return
        process.kill()
    process.wait(timeout=grace_seconds)


def _process_identity_matches(process_id: int, expected_create_time: float) -> bool:
    """Return whether the PID is gone or still identifies the recorded process."""
    try:
        current_create_time = psutil.Process(process_id).create_time()
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied as exc:
        raise OpenCodeProcessError("refusing cancellation without process ownership") from exc
    except psutil.Error as exc:
        raise OpenCodeProcessError("could not verify process identity before cancellation") from exc
    if abs(current_create_time - expected_create_time) > 1:
        raise OpenCodeProcessIdentityError(
            "process identity does not match the recorded start time; cancellation is refused"
        )
    return True


def cancel_process_group(
    process_id: int,
    expected_host: str,
    grace_seconds: int,
    expected_create_time: float,
) -> bool:
    """Interrupt one dispatcher-owned process group after host/identity checks."""
    if expected_host != socket.gethostname():
        raise OpenCodeProcessError("refusing cancellation for a process on another host")
    if not _process_identity_matches(process_id, expected_create_time):
        return False
    try:
        os.killpg(process_id, signal.SIGINT)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    if not _process_identity_matches(process_id, expected_create_time):
        return True
    try:
        os.killpg(process_id, signal.SIGTERM)
    except ProcessLookupError:
        return True
    time.sleep(min(0.1, max(0.01, grace_seconds / 10)))
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return True
    if not _process_identity_matches(process_id, expected_create_time):
        return True
    try:
        os.killpg(process_id, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _split_complete_lines(buffer: bytes) -> tuple[bytes, list[bytes]]:
    parts = buffer.split(b"\n")
    return parts.pop(), parts


def _validate_session_request(mode: str, session_id: str | None) -> None:
    if mode == "new" and session_id is not None:
        raise OpenCodeSessionError("new sessions must not include a session ID")
    if mode in {"resume", "fork"} and not session_id:
        raise OpenCodeSessionError(f"{mode} sessions require a persisted session ID")
    if mode not in {"new", "resume", "fork"}:
        raise OpenCodeSessionError(f"unsupported session mode: {mode!r}")


def _snapshot_dir(path: str) -> set[str]:
    directory = Path(path)
    if not directory.is_dir():
        return set()
    return {str(item.relative_to(directory)) for item in directory.rglob("*") if item.is_file()}


def _new_evidence(snapshot_dirs: list[str], before: Mapping[str, set[str]]) -> list[str]:
    evidence: list[str] = []
    for directory in snapshot_dirs:
        for relative_path in sorted(_snapshot_dir(directory) - before[directory]):
            evidence.append(str(Path(directory) / relative_path))
    return evidence


def _parse_json_output(stdout: str) -> tuple[list[dict[str, Any]], str, dict[str, Any], str]:
    """Compatibility wrapper used by pinned fixture tests and direct callers."""
    decoder = OpenCodeJsonlDecoder(max_output_bytes=max(1, len(stdout.encode("utf-8"))))
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if line:
            decoder.consume_line(line, line_number=line_number)
    raw, chat, usage, session_id, _cost = decoder.finish(require_response=True)
    return raw, chat, usage, session_id


@dataclass
class _BoundedTextCollector:
    limit: int
    value: str = ""

    def add(self, text: str) -> None:
        remaining = self.limit - len(self.value.encode("utf-8"))
        if remaining > 0:
            self.value += text.encode("utf-8")[:remaining].decode("utf-8", errors="ignore")
