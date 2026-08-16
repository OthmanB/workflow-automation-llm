from __future__ import annotations

import hashlib
import io
import signal
import socket
import ssl
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import dispatcher.cluster_operation_adapters as adapters
from dispatcher.cluster_operation_adapters import (
    ProductionPortForwardAdapterError,
    ProductionPortForwardProcessAdapter,
    ProductionTlsDc8ProbeAdapter,
)
from dispatcher.cluster_operation_lifecycle import PortForwardOwnership, PortForwardOwnershipState
from dispatcher.cluster_operation_runner import (
    PortForwardCleanupOutcome,
    PortForwardReadiness,
    PortForwardSpawnRequest,
    TlsDc8ProbeOutcome,
    TlsDc8ProbeRequest,
)
from dispatcher.cluster_operations import PortForwardAction
from dispatcher.config import ClusterMutationTargetDefinition, Config


@dataclass
class FakeProcessIdentity:
    created_at: float
    argv: tuple[str, ...]

    def create_time(self) -> float:
        return self.created_at

    def cmdline(self) -> list[str]:
        return list(self.argv)


class FakePopen:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", exit_on_wait: bool = False) -> None:
        self.pid = 4242
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode: int | None = None
        self.exit_on_wait = exit_on_wait
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.exit_on_wait:
            self.returncode = 0
            return 0
        raise subprocess.TimeoutExpired("kubectl", timeout)


class FakePopenFactory:
    def __init__(self, process: FakePopen) -> None:
        self.process = process
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> FakePopen:
        self.calls.append((args, kwargs))
        return self.process


class FakeSocket:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> FakeSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True


class FakeTlsSocket:
    def __enter__(self) -> FakeTlsSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class FakeTlsContext:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.check_hostname = True
        self.verify_mode = ssl.CERT_REQUIRED
        self.error = error
        self.wrap_calls: list[tuple[FakeSocket, str | None]] = []

    def wrap_socket(self, connection: FakeSocket, *, server_hostname: str | None) -> FakeTlsSocket:
        self.wrap_calls.append((connection, server_hostname))
        if self.error is not None:
            raise self.error
        return FakeTlsSocket()


def _request(tmp_path: Path) -> tuple[Config, PortForwardSpawnRequest]:
    kubectl = tmp_path / "kubectl"
    kubectl.write_bytes(b"checksum-bound-kubectl")
    kubectl.chmod(0o700)
    target = ClusterMutationTargetDefinition.model_validate(
        {
            "context": "fixture-context",
            "toolchain": {
                "kubectl": {
                    "path": str(kubectl),
                    "sha256": hashlib.sha256(kubectl.read_bytes()).hexdigest(),
                },
                "helm": {"path": str(tmp_path / "helm"), "sha256": "a" * 64},
            },
            "allowed_repository_ids": ["fixture-repo"],
            "operation_manifest_roots": ["deploy/operations"],
            "source_file_roots": ["deploy"],
            "max_snapshot_age_seconds": 900,
            "max_action_timeout_seconds": 120,
            "preflight_target_id": "fixture-preflight",
        }
    )
    action = PortForwardAction.model_validate(
        {
            "action": "port_forward",
            "action_id": "sample-forward",
            "namespace": "platform",
            "expected_resources": [
                {"api_version": "v1", "kind": "Service", "namespace": "platform", "name": "sample"}
            ],
            "resource": {"api_version": "v1", "kind": "Service", "namespace": "platform", "name": "sample"},
            "local_port": 18080,
            "remote_port": 8443,
            "startup_timeout_seconds": 10,
            "probe_timeout_seconds": 10,
            "lifetime_timeout_seconds": 30,
        }
    )
    argv = (
        str(kubectl),
        "--context",
        "fixture-context",
        "--namespace",
        "platform",
        "port-forward",
        "--address",
        "127.0.0.1",
        "service/sample",
        "18080:8443",
    )
    config = cast(Config, SimpleNamespace(cluster_mutation=SimpleNamespace(targets={"fixture": target})))
    return config, PortForwardSpawnRequest(argv=argv, target=target, action=action)


def _ownership(request: PortForwardSpawnRequest, *, created_at: datetime) -> PortForwardOwnership:
    return PortForwardOwnership(
        action_id=request.action.action_id,
        context=request.target.context,
        resource=request.action.resource,
        bind_address="127.0.0.1",
        local_port=request.action.local_port,
        remote_port=request.action.remote_port,
        argv_sha256=hashlib.sha256("\0".join(request.argv).encode("utf-8")).hexdigest(),
        state=PortForwardOwnershipState.STARTED,
        intent_at=created_at,
        pid=4242,
        process_created_at=created_at,
        started_at=created_at,
    )


def _adapter(
    tmp_path: Path,
    process: FakePopen,
    *,
    monotonic=lambda: 0.0,
) -> tuple[ProductionPortForwardProcessAdapter, PortForwardSpawnRequest, FakePopenFactory, FakeProcessIdentity]:
    config, request = _request(tmp_path)
    factory = FakePopenFactory(process)
    identity = FakeProcessIdentity(1_786_272_000.0, request.argv)
    adapter = ProductionPortForwardProcessAdapter(
        config,
        popen_factory=factory,
        process_factory=lambda _pid: identity,
        monotonic=monotonic,
    )
    return adapter, request, factory, identity


def test_production_port_forward_uses_only_the_runner_fixed_tuple_and_separate_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakePopen()
    adapter, request, factory, _identity = _adapter(tmp_path, process)
    monkeypatch.setattr(adapters.os, "getpgid", lambda pid: pid)

    child = adapter.spawn(request)

    assert child.pid == 4242
    assert len(factory.calls) == 1
    args, kwargs = factory.calls[0]
    assert args == (request.argv,)
    assert kwargs == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "start_new_session": True,
    }
    with pytest.raises(ProductionPortForwardAdapterError):
        adapter.spawn(replace(request, argv=("/bin/sh", "-c", "unexpected")))
    assert len(factory.calls) == 1


def test_production_port_forward_rechecks_the_configured_kubectl_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakePopen()
    adapter, request, factory, _identity = _adapter(tmp_path, process)
    monkeypatch.setattr(adapters.os, "getpgid", lambda pid: pid)
    Path(request.argv[0]).write_bytes(b"changed-kubectl")

    with pytest.raises(ProductionPortForwardAdapterError, match="kubectl identity"):
        adapter.spawn(request)
    assert factory.calls == []


def test_production_port_forward_accepts_only_the_exact_readiness_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = b"Forwarding from 127.0.0.1:18080 -> 8443\n"
    process = FakePopen(stdout=ready)
    adapter, request, _factory, _identity = _adapter(tmp_path, process)
    monkeypatch.setattr(adapters.os, "getpgid", lambda pid: pid)
    child = adapter.spawn(request)

    assert adapter.wait_ready(
        _ownership(request, created_at=child.process_created_at), request.action.startup_timeout_seconds
    ) is PortForwardReadiness.READY


def test_production_port_forward_rejects_a_near_match_readiness_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakePopen(stdout=b"Forwarding from 127.0.0.1:18080 -> 8444\n")
    process.returncode = 1
    adapter, request, _factory, _identity = _adapter(tmp_path, process)
    monkeypatch.setattr(adapters.os, "getpgid", lambda pid: pid)
    child = adapter.spawn(request)

    assert adapter.wait_ready(
        _ownership(request, created_at=child.process_created_at), request.action.startup_timeout_seconds
    ) is PortForwardReadiness.FAILED


def test_production_port_forward_fails_closed_on_bounded_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakePopen(stdout=b"x" * (adapters._MAX_PORT_FORWARD_OUTPUT_BYTES + 1))
    adapter, request, _factory, _identity = _adapter(tmp_path, process)
    monkeypatch.setattr(adapters.os, "getpgid", lambda pid: pid)
    child = adapter.spawn(request)

    assert adapter.wait_ready(
        _ownership(request, created_at=child.process_created_at), request.action.startup_timeout_seconds
    ) is PortForwardReadiness.FAILED


def test_production_port_forward_startup_timeout_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock_values = iter((0.0, 0.0, 2.0))
    process = FakePopen()
    adapter, request, _factory, _identity = _adapter(
        tmp_path, process, monotonic=lambda: next(clock_values)
    )
    monkeypatch.setattr(adapters.os, "getpgid", lambda pid: pid)
    child = adapter.spawn(request)

    assert adapter.wait_ready(_ownership(request, created_at=child.process_created_at), 1) is PortForwardReadiness.TIMEOUT


def test_production_port_forward_refuses_pid_reuse_without_signalling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakePopen()
    adapter, request, _factory, identity = _adapter(tmp_path, process)
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(adapters.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(adapters.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    child = adapter.spawn(request)
    identity.created_at += 2

    assert adapter.close_owned(
        _ownership(request, created_at=child.process_created_at), request.action.lifetime_timeout_seconds
    ) is PortForwardCleanupOutcome.PID_REUSED
    assert signals == []


def test_production_port_forward_cleanup_signals_only_the_verified_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakePopen(exit_on_wait=True)
    adapter, request, _factory, _identity = _adapter(tmp_path, process)
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(adapters.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(adapters.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    child = adapter.spawn(request)

    assert adapter.close_owned(
        _ownership(request, created_at=child.process_created_at), request.action.lifetime_timeout_seconds
    ) is PortForwardCleanupOutcome.CLOSED
    assert signals == [(4242, signal.SIGTERM)]


def test_production_port_forward_unknown_child_is_not_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakePopen()
    adapter, request, _factory, _identity = _adapter(tmp_path, process)
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(adapters.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(adapters.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    child = adapter.spawn(request)
    adapter._children.clear()

    assert adapter.close_owned(
        _ownership(request, created_at=child.process_created_at), request.action.lifetime_timeout_seconds
    ) is PortForwardCleanupOutcome.UNOWNED
    assert signals == []


def _probe_request() -> TlsDc8ProbeRequest:
    return TlsDc8ProbeRequest(
        port_forward_action_id="sample-forward",
        bind_address="127.0.0.1",
        local_port=18080,
        timeout_seconds=10,
    )


def test_tls_dc8_accepts_only_the_expected_client_certificate_rejection() -> None:
    connection = FakeSocket()
    context = FakeTlsContext(error=_client_certificate_required_error())
    calls: list[tuple[tuple[str, int], int]] = []
    adapter = ProductionTlsDc8ProbeAdapter(
        socket_create_connection=lambda address, timeout: calls.append((address, timeout)) or connection,
        tls_context_factory=lambda: context,
    )

    result = adapter.probe_no_client_certificate(_probe_request())

    assert result.outcome is TlsDc8ProbeOutcome.CLIENT_CERTIFICATE_REQUIRED
    assert calls == [(("127.0.0.1", 18080), 10)]
    assert context.wrap_calls == [(connection, None)]


def test_tls_dc8_rejects_an_unexpected_successful_handshake_without_application_data() -> None:
    connection = FakeSocket()
    context = FakeTlsContext()
    adapter = ProductionTlsDc8ProbeAdapter(
        socket_create_connection=lambda _address, timeout: connection,
        tls_context_factory=lambda: context,
    )

    result = adapter.probe_no_client_certificate(_probe_request())

    assert result.outcome is TlsDc8ProbeOutcome.UNAUTHENTICATED_HANDSHAKE_SUCCEEDED
    assert context.wrap_calls == [(connection, None)]


def test_tls_dc8_returns_timeout_without_a_network_fallback() -> None:
    adapter = ProductionTlsDc8ProbeAdapter(
        socket_create_connection=lambda _address, timeout: (_ for _ in ()).throw(socket.timeout())
    )

    result = adapter.probe_no_client_certificate(_probe_request())

    assert result.outcome is TlsDc8ProbeOutcome.TIMEOUT


def _client_certificate_required_error() -> ssl.SSLError:
    error = ssl.SSLError(1, "TLSV13_ALERT_CERTIFICATE_REQUIRED")
    error.reason = "TLSV13_ALERT_CERTIFICATE_REQUIRED"
    return error
