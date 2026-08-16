"""Production-only dispatcher adapters for the typed port-forward/TLS lifecycle."""

from __future__ import annotations

import hashlib
import os
import queue
import signal
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import psutil

from .cluster_operation_lifecycle import PortForwardOwnership
from .cluster_operation_runner import (
    PortForwardChild,
    PortForwardCleanupOutcome,
    PortForwardProcessAdapter,
    PortForwardReadiness,
    PortForwardSpawnRequest,
    TlsDc8ProbeAdapter,
    TlsDc8ProbeOutcome,
    TlsDc8ProbeRequest,
    TlsDc8ProbeResult,
)
from .config import Config

_MAX_PORT_FORWARD_OUTPUT_BYTES = 65_536
_STREAM_EVENT_QUEUE_SIZE = 4_096
_PROCESS_IDENTITY_TOLERANCE_SECONDS = 1.0
_CLEANUP_GRACE_SECONDS = 5
_EXPECTED_CLIENT_CERTIFICATE_REJECTIONS = frozenset(
    {
        "TLSV13_ALERT_CERTIFICATE_REQUIRED",
        "SSLV3_ALERT_CERTIFICATE_REQUIRED",
    }
)


class ProductionPortForwardAdapterError(RuntimeError):
    """A production port-forward request was not the exact typed dispatcher request."""


class _SpawnedProcess(Protocol):
    pid: int
    stdout: _ReadableBytes | None
    stderr: _ReadableBytes | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class _ProcessIdentity(Protocol):
    def create_time(self) -> float: ...

    def cmdline(self) -> list[str]: ...


class _ReadableBytes(Protocol):
    def read(self, size: int = -1) -> bytes: ...


@dataclass
class _TrackedPortForward:
    process: _SpawnedProcess
    action_id: str
    argv_sha256: str
    process_created_at: datetime
    deadline: float
    readiness_signal: bytes
    stream_events: queue.Queue[tuple[str, bytes | None]]


class ProductionPortForwardProcessAdapter(PortForwardProcessAdapter):
    """Spawn and close only a verified, fixed-argv dispatcher port-forward process."""

    def __init__(
        self,
        config: Config,
        *,
        popen_factory: Callable[..., _SpawnedProcess] | None = None,
        process_factory: Callable[[int], _ProcessIdentity] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._popen_factory = cast(
            Callable[..., _SpawnedProcess], popen_factory or subprocess.Popen
        )
        self._process_factory = process_factory or psutil.Process
        self._monotonic = monotonic
        self._children: dict[int, _TrackedPortForward] = {}

    def spawn(self, request: PortForwardSpawnRequest) -> PortForwardChild:
        """Start the independently reconstructed fixed tuple in a separate process group."""
        self._validate_spawn_request(request)
        try:
            process = self._popen_factory(
                request.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise ProductionPortForwardAdapterError("could not start typed port-forward") from exc
        if process.stdout is None or process.stderr is None or process.pid < 1:
            raise ProductionPortForwardAdapterError("typed port-forward has no usable child identity")

        try:
            identity = self._process_factory(process.pid)
            created_at = datetime.fromtimestamp(identity.create_time(), tz=UTC)
            observed_argv_sha256 = _argv_sha256(tuple(identity.cmdline()))
        except psutil.Error as exc:
            raise ProductionPortForwardAdapterError(
                "could not capture typed port-forward process identity"
            ) from exc
        expected_argv_sha256 = _argv_sha256(request.argv)
        if observed_argv_sha256 != expected_argv_sha256:
            raise ProductionPortForwardAdapterError("typed port-forward argv identity does not match")
        try:
            if os.getpgid(process.pid) != process.pid:
                raise ProductionPortForwardAdapterError("typed port-forward lacks its own process group")
        except OSError as exc:
            raise ProductionPortForwardAdapterError("could not verify typed port-forward process group") from exc

        events: queue.Queue[tuple[str, bytes | None]] = queue.Queue(
            maxsize=_STREAM_EVENT_QUEUE_SIZE
        )
        _start_bounded_stream_reader("stdout", process.stdout, events)
        _start_bounded_stream_reader("stderr", process.stderr, events)
        self._children[process.pid] = _TrackedPortForward(
            process=process,
            action_id=request.action.action_id,
            argv_sha256=expected_argv_sha256,
            process_created_at=created_at,
            deadline=self._monotonic() + request.action.lifetime_timeout_seconds,
            readiness_signal=(
                f"Forwarding from 127.0.0.1:{request.action.local_port} -> "
                f"{request.action.remote_port}"
            ).encode("ascii"),
            stream_events=events,
        )
        return PortForwardChild(
            pid=process.pid,
            process_created_at=created_at,
            argv_sha256=expected_argv_sha256,
        )

    def wait_ready(
        self, ownership: PortForwardOwnership, timeout_seconds: int
    ) -> PortForwardReadiness:
        """Accept only the exact fixed loopback readiness line before the lifetime deadline."""
        tracked = self._owned_child(ownership)
        if tracked is None:
            return PortForwardReadiness.AMBIGUOUS
        if not _valid_timeout(timeout_seconds):
            return PortForwardReadiness.TIMEOUT
        deadline = min(self._monotonic() + timeout_seconds, tracked.deadline)
        totals = {"stdout": 0, "stderr": 0}
        stdout_line = bytearray()
        stdout_line_overlong = False
        streams_closed: set[str] = set()
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return PortForwardReadiness.TIMEOUT
            try:
                stream_name, chunk = tracked.stream_events.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                if tracked.process.poll() is not None and len(streams_closed) == 2:
                    return PortForwardReadiness.FAILED
                continue
            if chunk is None:
                streams_closed.add(stream_name)
                if tracked.process.poll() is not None and len(streams_closed) == 2:
                    return PortForwardReadiness.FAILED
                continue
            totals[stream_name] += len(chunk)
            if totals[stream_name] > _MAX_PORT_FORWARD_OUTPUT_BYTES:
                return PortForwardReadiness.FAILED
            if stream_name != "stdout":
                continue
            if chunk == b"\n":
                if not stdout_line_overlong and bytes(stdout_line) == tracked.readiness_signal:
                    return PortForwardReadiness.READY
                stdout_line.clear()
                stdout_line_overlong = False
            elif len(stdout_line) < len(tracked.readiness_signal):
                stdout_line.extend(chunk)
            else:
                stdout_line_overlong = True

    def close_owned(
        self, ownership: PortForwardOwnership, timeout_seconds: int
    ) -> PortForwardCleanupOutcome:
        """Signal an owned group only after every durable process identity still matches."""
        tracked = self._owned_child(ownership)
        if tracked is None:
            return PortForwardCleanupOutcome.UNOWNED
        assert ownership.pid is not None
        if tracked.process.poll() is not None:
            self._children.pop(ownership.pid, None)
            return PortForwardCleanupOutcome.CLOSED
        if not self._process_identity_matches(ownership):
            return PortForwardCleanupOutcome.PID_REUSED
        try:
            if os.getpgid(ownership.pid) != ownership.pid:
                return PortForwardCleanupOutcome.UNOWNED
            os.killpg(ownership.pid, signal.SIGTERM)
        except ProcessLookupError:
            return self._closed_or_unowned(ownership, tracked)
        except OSError:
            return PortForwardCleanupOutcome.FAILED

        grace_seconds = min(
            _CLEANUP_GRACE_SECONDS,
            max(0, min(timeout_seconds, int(max(0, tracked.deadline - self._monotonic())))),
        )
        try:
            tracked.process.wait(timeout=grace_seconds)
            self._children.pop(ownership.pid, None)
            return PortForwardCleanupOutcome.CLOSED
        except subprocess.TimeoutExpired:
            pass
        if not self._process_identity_matches(ownership):
            return PortForwardCleanupOutcome.PID_REUSED
        try:
            if os.getpgid(ownership.pid) != ownership.pid:
                return PortForwardCleanupOutcome.UNOWNED
            os.killpg(ownership.pid, signal.SIGKILL)
        except ProcessLookupError:
            return self._closed_or_unowned(ownership, tracked)
        except OSError:
            return PortForwardCleanupOutcome.FAILED
        try:
            tracked.process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            return PortForwardCleanupOutcome.TIMEOUT
        self._children.pop(ownership.pid, None)
        return PortForwardCleanupOutcome.CLOSED

    def _validate_spawn_request(self, request: PortForwardSpawnRequest) -> None:
        if not isinstance(request, PortForwardSpawnRequest) or (
            not isinstance(request.argv, tuple)
            or any(not isinstance(argument, str) for argument in request.argv)
        ):
            raise ProductionPortForwardAdapterError("port-forward request is not a typed argv tuple")
        cluster_mutation = self._config.cluster_mutation
        if (
            cluster_mutation is None
            or request.target not in cluster_mutation.targets.values()
            or request.argv != _port_forward_argv(request)
        ):
            raise ProductionPortForwardAdapterError("port-forward request is not an approved typed tuple")
        kubectl = Path(request.target.toolchain.kubectl.path)
        if (
            not kubectl.is_file()
            or not os.access(kubectl, os.X_OK)
            or _file_sha256(kubectl) != request.target.toolchain.kubectl.sha256
        ):
            raise ProductionPortForwardAdapterError("configured kubectl identity is unavailable")

    def _owned_child(self, ownership: PortForwardOwnership) -> _TrackedPortForward | None:
        if ownership.pid is None or ownership.process_created_at is None:
            return None
        tracked = self._children.get(ownership.pid)
        if (
            tracked is None
            or tracked.action_id != ownership.action_id
            or tracked.argv_sha256 != ownership.argv_sha256
            or tracked.process_created_at != ownership.process_created_at
        ):
            return None
        return tracked

    def _process_identity_matches(self, ownership: PortForwardOwnership) -> bool:
        assert ownership.pid is not None
        assert ownership.process_created_at is not None
        try:
            process = self._process_factory(ownership.pid)
            if abs(process.create_time() - ownership.process_created_at.timestamp()) > (
                _PROCESS_IDENTITY_TOLERANCE_SECONDS
            ):
                return False
            return _argv_sha256(tuple(process.cmdline())) == ownership.argv_sha256
        except psutil.Error:
            return False

    def _closed_or_unowned(
        self, ownership: PortForwardOwnership, tracked: _TrackedPortForward
    ) -> PortForwardCleanupOutcome:
        assert ownership.pid is not None
        if tracked.process.poll() is not None:
            self._children.pop(ownership.pid, None)
            return PortForwardCleanupOutcome.CLOSED
        return PortForwardCleanupOutcome.UNOWNED


class ProductionTlsDc8ProbeAdapter(TlsDc8ProbeAdapter):
    """Perform one no-client-certificate TLS handshake against a typed loopback port."""

    def __init__(
        self,
        *,
        socket_create_connection: Callable[..., socket.socket] = socket.create_connection,
        tls_context_factory: Callable[[], ssl.SSLContext] | None = None,
    ) -> None:
        self._socket_create_connection = socket_create_connection
        self._tls_context_factory = tls_context_factory or _no_client_certificate_context

    def probe_no_client_certificate(self, request: TlsDc8ProbeRequest) -> TlsDc8ProbeResult:
        """Connect only to literal loopback and send no application bytes after TLS setup."""
        if (
            request.bind_address != "127.0.0.1"
            or not _valid_timeout(request.timeout_seconds)
            or not 1024 <= request.local_port <= 65_535
        ):
            return _tls_result(TlsDc8ProbeOutcome.UNEXPECTED_LISTENER_BEHAVIOR)
        try:
            with self._socket_create_connection(
                ("127.0.0.1", request.local_port), timeout=request.timeout_seconds
            ) as connection:
                context = self._tls_context_factory()
                with context.wrap_socket(connection, server_hostname=None):
                    pass
        except (socket.timeout, TimeoutError):
            return _tls_result(TlsDc8ProbeOutcome.TIMEOUT)
        except ssl.SSLError as exc:
            if getattr(exc, "reason", None) in _EXPECTED_CLIENT_CERTIFICATE_REJECTIONS:
                return _tls_result(TlsDc8ProbeOutcome.CLIENT_CERTIFICATE_REQUIRED)
            return _tls_result(TlsDc8ProbeOutcome.UNEXPECTED_LISTENER_BEHAVIOR)
        except OSError:
            return _tls_result(TlsDc8ProbeOutcome.UNEXPECTED_LISTENER_BEHAVIOR)
        return _tls_result(TlsDc8ProbeOutcome.UNAUTHENTICATED_HANDSHAKE_SUCCEEDED)


def _port_forward_argv(request: PortForwardSpawnRequest) -> tuple[str, ...]:
    """Independently reconstruct the one tuple production code permits to execute."""
    action = request.action
    target = request.target
    return (
        target.toolchain.kubectl.path,
        "--context",
        target.context,
        "--namespace",
        action.resource.namespace,
        "port-forward",
        "--address",
        "127.0.0.1",
        f"service/{action.resource.name}",
        f"{action.local_port}:{action.remote_port}",
    )


def _start_bounded_stream_reader(
    name: str, stream: _ReadableBytes, events: queue.Queue[tuple[str, bytes | None]]
) -> None:
    def read_bytes() -> None:
        try:
            while byte := stream.read(1):
                events.put((name, byte))
        finally:
            events.put((name, None))

    threading.Thread(target=read_bytes, name=f"port-forward-{name}", daemon=True).start()


def _no_client_certificate_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Certificate verification is disabled only to observe this unauthenticated handshake.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _tls_result(outcome: TlsDc8ProbeOutcome) -> TlsDc8ProbeResult:
    return TlsDc8ProbeResult(
        outcome=outcome,
        evidence_sha256=hashlib.sha256(outcome.value.encode("ascii")).hexdigest(),
    )


def _valid_timeout(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _argv_sha256(argv: tuple[str, ...]) -> str:
    return hashlib.sha256("\0".join(argv).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()
