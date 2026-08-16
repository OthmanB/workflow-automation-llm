from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import yaml
from helpers import (
    FixtureProject,
    config_values,
    create_fixture_project,
    valid_plan_values,
    write_config,
)

from dispatcher.cluster_operation_lifecycle import (
    ClusterOperationStatus,
    create_auto_approved_cluster_operation_approval,
    new_cluster_operation_lifecycle_record,
)
from dispatcher.cluster_operation_runner import ClusterOperationCommandResult
from dispatcher.cluster_operation_snapshot import (
    ClusterOperationSnapshotCommandResult,
    capture_cluster_operation_snapshot,
)
from dispatcher.cluster_operations import validate_cluster_operations_for_plan
from dispatcher.execution import RealOperationExecutionContext, SequentialExecutionCoordinator
from dispatcher.operation import (
    ClusterOperationEnvelope,
    RealOperationApproval,
    approve_real_operation,
    compile_real_operation_scope_manifest,
    digest_json,
)
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.sequential import SequentialWorkflow
from dispatcher.sessions import SessionResult
from dispatcher.state_store import StateStore
from dispatcher.verification import AuthoritativeVerification
from dispatcher.workflow import (
    DispatchStatus,
    RunRecord,
    RunStatus,
    StepStatus,
    TransitionEvent,
    new_run_record,
)

_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
_TIER1_DIGEST = "1" * 64


@dataclass(frozen=True)
class DispatcherFixture:
    project: FixtureProject
    config: Any
    store: StateStore
    record: RunRecord
    approval: RealOperationApproval
    kubectl: Path
    helm: Path


class FakeSnapshotRunner:
    def __init__(self, fixture: DispatcherFixture, trace: list[str]) -> None:
        self.fixture = fixture
        self.trace = trace
        self.calls: list[tuple[tuple[str, ...], int]] = []
        self.results = [
            ClusterOperationSnapshotCommandResult(0, b"fixture-context\n"),
            ClusterOperationSnapshotCommandResult(
                0,
                b'{"clientVersion":{"gitVersion":"v1.28.0"},'
                b'"serverVersion":{"gitVersion":"v1.27.3"}}',
            ),
            ClusterOperationSnapshotCommandResult(
                0, b"apps/v1\tDeployment\tplatform\tsample-app\tresource-uid\t42\n"
            ),
            ClusterOperationSnapshotCommandResult(
                0,
                b'{"name":"sample-app","namespace":"platform","version":7,'
                b'"info":{"status":"deployed"},'
                b'"chart":{"metadata":{"version":"1.2.3","appVersion":"2.3.4"}}}',
            ),
            ClusterOperationSnapshotCommandResult(
                0,
                b'[{"revision":7,"status":"deployed","chart":"sample-app-1.2.3",'
                b'"app_version":"2.3.4"}]',
            ),
        ]

    def __call__(self, argv: tuple[str, ...], timeout_seconds: int) -> ClusterOperationSnapshotCommandResult:
        _assert_review_not_accepted(self.fixture)
        assert "apply" not in argv
        assert "upgrade" not in argv
        assert "port-forward" not in argv
        self.trace.append("snapshot")
        self.calls.append((argv, timeout_seconds))
        return self.results.pop(0)


class FakeOperationRunner:
    def __init__(
        self,
        fixture: DispatcherFixture,
        trace: list[str],
        *,
        fail_dry_run: bool = False,
    ) -> None:
        self.fixture = fixture
        self.trace = trace
        self.fail_dry_run = fail_dry_run
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def __call__(self, argv: tuple[str, ...], timeout_seconds: int) -> ClusterOperationCommandResult:
        _assert_review_not_accepted(self.fixture)
        self.trace.append("operation")
        self.calls.append((argv, timeout_seconds))
        if self.fail_dry_run:
            return ClusterOperationCommandResult(returncode=1, stdout=b"", stderr=b"failed")
        return ClusterOperationCommandResult(returncode=0, stdout=b"ok")


class ScriptedSessionRunner:
    def __init__(
        self,
        fixture: DispatcherFixture,
        trace: list[str],
        *,
        before_reviewer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.fixture = fixture
        self.trace = trace
        self.before_reviewer = before_reviewer
        self.supervisor_turns = 0

    def __call__(self, **kwargs: Any) -> SessionResult:
        title = str(kwargs["title"])
        if title.startswith("supervisor"):
            response = self._supervisor_response()
            return SessionResult(
                session_id=f"supervisor-{self.supervisor_turns}",
                exit_code=0,
                chat_response=response,
                evidence_written=[],
            )

        prompt = json.loads(kwargs["prompt"])
        assert self.fixture.approval.approval_ref not in kwargs["prompt"]
        assert "cluster_operation_envelopes" not in kwargs["prompt"]
        assert "kubectl" not in kwargs["prompt"]
        assert "port_forward" not in kwargs["prompt"]
        assert "tls_dc8" not in kwargs["prompt"]
        lifecycle = kwargs["lifecycle"]
        lifecycle.on_process_started(os.getpid(), 1.0)
        role_kind = title.split(" - ", maxsplit=1)[0]
        session_id = f"{role_kind}-{prompt['dispatch_id']}"
        lifecycle.on_session_identified(session_id)
        if title.startswith("executor"):
            self.trace.append("executor")
            _write_executor_operation(Path(kwargs["workdir"]))
            return SessionResult(
                session_id=session_id,
                exit_code=0,
                chat_response=json.dumps(
                    {
                        "proposal_version": 2,
                        "response_contract": "dispatcher.executor_proposal.v2",
                        "dispatch_id": prompt["dispatch_id"],
                        "attempt": prompt["attempt"],
                        "step_id": prompt["step_id"],
                        "repository": {
                            "repo_id": prompt["repo_id"],
                            "base_revision": prompt["base_revision"],
                        },
                        "evidence": [
                            {
                                "artifact_id": "fixture-evidence",
                                "relative_path": "fixture.md",
                                "media_type": "text/markdown",
                            }
                        ],
                        "criterion_self_reports": [
                            {
                                "check_id": criterion["criterion_id"],
                                "status": "not_run",
                                "summary": "dispatcher owns verification",
                            }
                            for criterion in prompt["acceptance_criteria"]
                        ],
                        "summary": "cluster operation files prepared",
                        "outcome": "completed",
                    }
                ),
                evidence_written=[],
            )

        assert title.startswith("reviewer")
        if self.before_reviewer is not None:
            self.before_reviewer(prompt)
        self.trace.append("reviewer_result")
        return SessionResult(
            session_id=session_id,
            exit_code=0,
            chat_response=json.dumps(
                {
                    "result_version": 1,
                    "response_contract": "dispatcher.reviewer_result.v1",
                    "dispatch_id": prompt["dispatch_id"],
                    "attempt": prompt["attempt"],
                    "step_id": prompt["step_id"],
                    "repo_id": prompt["repo_id"],
                    "review_target": prompt["review_target"],
                    "findings": [],
                    "verification": [
                        {
                            "check_id": criterion["criterion_id"],
                            "status": "passed",
                            "summary": "reviewed",
                        }
                        for criterion in prompt["acceptance_criteria"]
                    ],
                    "required_remediation": [],
                    "summary": "accepted after review",
                    "verdict": "accepted",
                }
            ),
            evidence_written=[],
        )

    def _supervisor_response(self) -> str:
        self.supervisor_turns += 1
        if self.supervisor_turns == 1:
            role = "terra"
        elif self.supervisor_turns == 2:
            role = "reviewer"
        else:
            return json.dumps({"protocol_version": 1, "action": "request_completion"})
        return json.dumps(
            {
                "protocol_version": 1,
                "action": "dispatch",
                "step_id": "prepare-fixture",
                "target_role": role,
                "session_mode": "new",
                "prompt": f"run {role}",
            }
        )


def test_cluster_operation_runs_only_before_post_operation_reviewer_acceptance(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    trace: list[str] = []
    snapshots = FakeSnapshotRunner(fixture, trace)
    operations = FakeOperationRunner(fixture, trace)
    runner = ScriptedSessionRunner(fixture, trace)
    coordinator = _coordinator(fixture, runner, snapshots, operations)

    decision = coordinator.run_to_completion(fixture.record.run_id, expected_generation=1)

    final, _generation = fixture.store.load_run(fixture.record.run_id)
    operation = validate_cluster_operations_for_plan(config=fixture.config, plan=fixture.record.plan)[
        "prepare-fixture"
    ]
    journal = fixture.store.load_cluster_operation(
        run_id=fixture.record.run_id,
        operation_id=operation.manifest.operation_id,
        source_revision=_git(fixture.project.repository, "rev-parse", "HEAD"),
    )
    assert final.operator_request is None
    assert decision.accepted is True
    assert final.state is RunStatus.SUCCEEDED
    assert final.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert journal.status is ClusterOperationStatus.SUCCEEDED
    assert len(snapshots.calls) == 5
    assert [argv[0] for argv, _timeout in operations.calls] == [
        str(fixture.kubectl),
        str(fixture.helm),
        str(fixture.kubectl),
    ]
    assert trace.index("reviewer_result") < trace.index("snapshot") < trace.index("operation")


def test_cluster_operation_failure_blocks_review_acceptance_and_never_forwards(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    trace: list[str] = []
    snapshots = FakeSnapshotRunner(fixture, trace)
    operations = FakeOperationRunner(fixture, trace, fail_dry_run=True)
    coordinator = _coordinator(
        fixture,
        ScriptedSessionRunner(fixture, trace),
        snapshots,
        operations,
    )

    decision = coordinator.run_to_completion(fixture.record.run_id, expected_generation=1)

    final, generation = fixture.store.load_run(fixture.record.run_id)
    reviewer = next(item for item in final.dispatches.values() if item.role_kind == "reviewer")
    operation = validate_cluster_operations_for_plan(config=fixture.config, plan=fixture.record.plan)[
        "prepare-fixture"
    ]
    journal = fixture.store.load_cluster_operation(
        run_id=fixture.record.run_id,
        operation_id=operation.manifest.operation_id,
        source_revision=_git(fixture.project.repository, "rev-parse", "HEAD"),
    )
    assert decision.accepted is False
    assert final.state is RunStatus.WAITING_OPERATOR
    assert final.steps["prepare-fixture"].state is StepStatus.BLOCKED
    assert final.steps["prepare-fixture"].review_acceptances == 0
    assert reviewer.state is DispatchStatus.FAILED
    assert reviewer.failure_category == "cluster_operation"
    assert fixture.store.review_for_dispatch(fixture.record.run_id, reviewer.dispatch_id) is False
    assert fixture.store.load_dispatch_payload(fixture.record.run_id, reviewer.dispatch_id).forwarding_payload is None
    assert journal.status is ClusterOperationStatus.FAILED
    assert len(operations.calls) == 1

    resumed = coordinator.run_to_completion(fixture.record.run_id, expected_generation=generation)

    assert resumed.accepted is False
    assert len(operations.calls) == 1


def test_stale_envelope_rejects_before_fake_snapshot_or_operation_command(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    stale_approval = _stale_envelope_approval(fixture.approval)
    trace: list[str] = []
    snapshots = FakeSnapshotRunner(fixture, trace)
    operations = FakeOperationRunner(fixture, trace)
    coordinator = _coordinator(
        fixture,
        ScriptedSessionRunner(fixture, trace),
        snapshots,
        operations,
        approval=stale_approval,
    )

    decision = coordinator.run_to_completion(fixture.record.run_id, expected_generation=1)

    final, _generation = fixture.store.load_run(fixture.record.run_id)
    operation = validate_cluster_operations_for_plan(config=fixture.config, plan=fixture.record.plan)[
        "prepare-fixture"
    ]
    journal = fixture.store.load_cluster_operation(
        run_id=fixture.record.run_id,
        operation_id=operation.manifest.operation_id,
        source_revision=_git(fixture.project.repository, "rev-parse", "HEAD"),
    )
    assert decision.accepted is False
    assert final.steps["prepare-fixture"].review_acceptances == 0
    assert snapshots.calls == []
    assert operations.calls == []
    assert journal.status is ClusterOperationStatus.FAILED


def test_ambiguous_lifecycle_record_never_auto_reapplies(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    trace: list[str] = []
    snapshots = FakeSnapshotRunner(fixture, trace)
    operations = FakeOperationRunner(fixture, trace)

    def start_ambiguous_lifecycle(prompt: dict[str, Any]) -> None:
        operation = validate_cluster_operations_for_plan(config=fixture.config, plan=fixture.record.plan)[
            "prepare-fixture"
        ]
        source_revision = prompt["review_target"]["result_revision"]
        record = new_cluster_operation_lifecycle_record(
            operation,
            run_id=fixture.record.run_id,
            source_revision=source_revision,
            plan_digest=fixture.record.plan_digest,
            config_digest=fixture.config.config_digest,
            max_snapshot_age_seconds=900,
            now=_NOW,
        )
        fixture.store.create_cluster_operation(record)
        record = fixture.store.transition_cluster_operation(
            run_id=record.run_id,
            operation_id=record.operation_id,
            source_revision=record.source_revision,
            expected_generation=record.generation,
            target=ClusterOperationStatus.STATIC_VALIDATED,
            now=_NOW,
        )
        snapshot = capture_cluster_operation_snapshot(
            config=fixture.config,
            operation=operation,
            source_revision=source_revision,
            real_operation_approval=fixture.approval,
            tier1_invariant_snapshot_digest=_TIER1_DIGEST,
            command_runner=FakeSnapshotRunner(fixture, []),
            now=_NOW,
        )
        record = fixture.store.attach_cluster_operation_snapshot(
            run_id=record.run_id,
            operation_id=record.operation_id,
            source_revision=record.source_revision,
            expected_generation=record.generation,
            snapshot=snapshot,
            now=_NOW,
        )
        approval = create_auto_approved_cluster_operation_approval(
            operation,
            source_revision,
            fixture.approval,
            now=_NOW,
        )
        record = fixture.store.attach_cluster_operation_approval(
            run_id=record.run_id,
            operation_id=record.operation_id,
            source_revision=record.source_revision,
            expected_generation=record.generation,
            approval=approval,
            now=_NOW,
        )
        record = fixture.store.transition_cluster_operation(
            run_id=record.run_id,
            operation_id=record.operation_id,
            source_revision=record.source_revision,
            expected_generation=record.generation,
            target=ClusterOperationStatus.SERVER_DRY_RUN_PASSED,
            now=_NOW,
        )
        fixture.store.transition_cluster_operation(
            run_id=record.run_id,
            operation_id=record.operation_id,
            source_revision=record.source_revision,
            expected_generation=record.generation,
            target=ClusterOperationStatus.MUTATION_STARTED,
            now=_NOW,
        )

    coordinator = _coordinator(
        fixture,
        ScriptedSessionRunner(fixture, trace, before_reviewer=start_ambiguous_lifecycle),
        snapshots,
        operations,
    )

    decision = coordinator.run_to_completion(fixture.record.run_id, expected_generation=1)

    operation = validate_cluster_operations_for_plan(config=fixture.config, plan=fixture.record.plan)[
        "prepare-fixture"
    ]
    journal = fixture.store.load_cluster_operation(
        run_id=fixture.record.run_id,
        operation_id=operation.manifest.operation_id,
        source_revision=_git(fixture.project.repository, "rev-parse", "HEAD"),
    )
    assert decision.accepted is False
    assert operations.calls == []
    assert journal.status is ClusterOperationStatus.RECONCILIATION_REQUIRED


def test_normal_step_remains_unaffected_without_real_operation_context(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, with_cluster_operation=False)
    trace: list[str] = []
    snapshots = FakeSnapshotRunner(fixture, trace)
    operations = FakeOperationRunner(fixture, trace)
    coordinator = SequentialExecutionCoordinator(
        fixture.config,
        fixture.store,
        SequentialWorkflow(fixture.config, fixture.store, owner_id="normal-step-owner"),
        owner_id="normal-step-owner",
        session_runner=ScriptedSessionRunner(fixture, trace),
        verification_runner=_passing_verification,
    )

    decision = coordinator.run_to_completion(fixture.record.run_id, expected_generation=1)

    final, _generation = fixture.store.load_run(fixture.record.run_id)
    assert decision.accepted is True
    assert final.state is RunStatus.SUCCEEDED
    assert snapshots.calls == []
    assert operations.calls == []


def _fixture(tmp_path: Path, *, with_cluster_operation: bool = True) -> DispatcherFixture:
    project = create_fixture_project(tmp_path)
    tools = project.root / "mutation-tools"
    tools.mkdir()
    kubectl = tools / "kubectl"
    helm = tools / "helm"
    kubectl.write_bytes(b"fake-kubectl")
    helm.write_bytes(b"fake-helm")
    kubectl.chmod(0o700)
    helm.chmod(0o700)

    values = config_values(project)
    values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    values["permission_policies"]["policies"]["executor-class"]["actions"]["commit"] = "allow"
    values["cluster_preflight"] = {
        "capability_version": 1,
        "target_id": "fixture-readiness",
        "context": "fixture-context",
        "minimum_client_version": "v1.27.0",
        "minimum_server_version": "v1.27.0",
        "request_timeout_seconds": 10,
        "required_namespaces": ["platform"],
        "required_helm_releases": [
            {
                "release": "sample-app",
                "namespace": "platform",
                "chart": "sample-app",
                "minimum_chart_version": "1.0.0",
            }
        ],
        "required_api_resources": [{"resource": "deployments.apps"}],
        "auth_checks": [{"verb": "get", "resource": "deployments.apps", "namespace": "platform"}],
    }
    values["cluster_mutation"] = {
        "capability_version": 1,
        "targets": {
            "fixture-target": {
                "context": "fixture-context",
                "toolchain": {
                    "kubectl": {"path": str(kubectl), "sha256": _digest(kubectl)},
                    "helm": {"path": str(helm), "sha256": _digest(helm)},
                },
                "allowed_repository_ids": ["fixture-repo"],
                "operation_manifest_roots": ["deploy/operations"],
                "source_file_roots": ["deploy"],
                "max_snapshot_age_seconds": 900,
                "max_action_timeout_seconds": 120,
                "preflight_target_id": "fixture-readiness",
            }
        },
    }
    config = write_config(project, values)
    (project.repository / "evidence" / "fixture.md").write_text("initial evidence\n", encoding="utf-8")
    _git(project.repository, "config", "user.name", "Fixture Initializer")
    _git(project.repository, "config", "user.email", "fixture@example.invalid")
    _git(project.repository, "add", ".")
    _git(project.repository, "commit", "-m", "initial fixture")
    _git(project.repository, "branch", "-M", "main")

    plan_values = valid_plan_values(project)
    step = plan_values["steps"][0]
    step["authorization"] = {
        "authorized_actions": ["inspect", "modify", "verify", "commit"],
        "writable_paths": ["deploy/", "evidence/"],
        "requires_operator_approval": False,
    }
    step["review"] = {
        "required": True,
        "reviewer_role_keys": ["reviewer"],
        "required_acceptances": 1,
    }
    step["retry"] = {
        "max_executor_attempts": 1,
        "max_reviewer_attempts": 1,
        "on_failed": "halt",
        "on_blocked": "halt",
        "on_changes_requested": "halt",
        "escalation_role_key": None,
    }
    if with_cluster_operation:
        step["cluster_operation"] = {
            "target_name": "fixture-target",
            "operation_manifest_path": "deploy/operations/sample-app.yaml",
            "requires_cluster_approval": True,
            "preauthorized_actions": ["kubectl_server_dry_run", "helm_upgrade_install"],
            "requires_automatic_rollback": True,
        }
    plan = NormalizedPlan.model_validate(plan_values)
    record = new_run_record(
        run_id="cluster-dispatch-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-cluster-dispatch"),
        event=TransitionEvent(
            event_id="event-cluster-dispatch",
            sequence=1,
            actor="operator",
            reason="cluster dispatcher integration fixture",
            correlation_id="cluster-dispatch-run",
            occurred_at=_NOW,
        ),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    assert store.create_run(record) == 1
    scope = compile_real_operation_scope_manifest(
        config=config,
        record=record,
        plan=plan,
        repo_id="fixture-repo",
    )
    approval = approve_real_operation(
        config=config,
        record=record,
        plan=plan,
        repo_id="fixture-repo",
        approval_ref="decision-cluster-dispatch",
        permission_digests={role_key: item.digest for role_key, item in scope.steps[0].roles.items()},
        scope_manifest_digest=scope.digest if with_cluster_operation else None,
    )
    return DispatcherFixture(project, config, store, record, approval, kubectl, helm)


def _coordinator(
    fixture: DispatcherFixture,
    session_runner: ScriptedSessionRunner,
    snapshots: FakeSnapshotRunner,
    operations: FakeOperationRunner,
    *,
    approval: RealOperationApproval | None = None,
) -> SequentialExecutionCoordinator:
    real_approval = approval or fixture.approval
    return SequentialExecutionCoordinator(
        fixture.config,
        fixture.store,
        SequentialWorkflow(fixture.config, fixture.store, owner_id="cluster-dispatch-owner"),
        owner_id="cluster-dispatch-owner",
        session_runner=session_runner,
        verification_runner=_passing_verification,
        real_operation_context=RealOperationExecutionContext(
            approval=real_approval,
            cluster_operation_envelopes=real_approval.cluster_operation_envelopes,
            tier1_invariant_snapshot_digest=_TIER1_DIGEST,
            snapshot_command_runner=snapshots,
            operation_command_runner=operations,
            now=lambda: _NOW,
        ),
    )


def _passing_verification(step: Any, _workdir: Path) -> tuple[AuthoritativeVerification, ...]:
    return tuple(
        AuthoritativeVerification(
            check_id=criterion.criterion_id,
            status="passed",
            argv=("fixture-check", criterion.criterion_id),
            exit_code=0,
            timed_out=False,
            output_truncated=False,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
            transcript_sha256="c" * 64,
            duration_ms=0,
            backend="fixture",
            summary="passed",
        )
        for criterion in step.acceptance_criteria
    )


def _assert_review_not_accepted(fixture: DispatcherFixture) -> None:
    record, _generation = fixture.store.load_run(fixture.record.run_id)
    reviewer = next(item for item in record.dispatches.values() if item.role_kind == "reviewer")
    assert record.steps["prepare-fixture"].review_acceptances == 0
    assert reviewer.state is DispatchStatus.RUNNING
    assert fixture.store.review_for_dispatch(fixture.record.run_id, reviewer.dispatch_id) is False
    assert fixture.store.load_dispatch_payload(
        fixture.record.run_id, reviewer.dispatch_id
    ).forwarding_payload is None


def _write_executor_operation(repository: Path) -> None:
    manifest_file = repository / "deploy/manifests/sample-app.yaml"
    chart_path = repository / "deploy/charts/sample-app"
    values_file = repository / "deploy/values/sample-app.yaml"
    operation_path = repository / "deploy/operations/sample-app.yaml"
    for path in (manifest_file.parent, chart_path, values_file.parent, operation_path.parent):
        path.mkdir(parents=True, exist_ok=True)
    manifest_file.write_text("apiVersion: apps/v1\nkind: Deployment\n", encoding="utf-8")
    (chart_path / "Chart.lock").write_text("dependencies: []\n", encoding="utf-8")
    values_file.write_text("replicaCount: 1\n", encoding="utf-8")
    (repository / "evidence/fixture.md").write_text("cluster evidence\n", encoding="utf-8")
    deployment = {
        "api_version": "apps/v1",
        "kind": "Deployment",
        "namespace": "platform",
        "name": "sample-app",
    }
    manifest = {
        "schema_version": 1,
        "operation_id": "sample-app-deploy",
        "context": "fixture-context",
        "source_identity": {"repository_id": "fixture-repo", "revision": "approval_snapshot"},
        "allowed_namespaces": ["platform"],
        "allowed_files": [
            {"path": "deploy/charts/sample-app/Chart.lock", "sha256": "approval_snapshot"},
            {"path": "deploy/manifests/sample-app.yaml", "sha256": "approval_snapshot"},
            {"path": "deploy/values/sample-app.yaml", "sha256": "approval_snapshot"},
        ],
        "secret_requirements": [],
        "actions": [
            {
                "action": "kubectl_server_dry_run",
                "namespace": "platform",
                "timeout_seconds": 30,
                "expected_resources": [deployment],
                "readiness_probes": [{"probe": "deployment_available", "resource": deployment}],
                "manifest_files": [
                    {"path": "deploy/manifests/sample-app.yaml", "sha256": "approval_snapshot"}
                ],
            },
            {
                "action": "helm_upgrade_install",
                "namespace": "platform",
                "timeout_seconds": 60,
                "expected_resources": [deployment],
                "readiness_probes": [{"probe": "deployment_available", "resource": deployment}],
                "release": "sample-app",
                "chart_path": "deploy/charts/sample-app",
                "chart_lock_file": {
                    "path": "deploy/charts/sample-app/Chart.lock",
                    "sha256": "approval_snapshot",
                },
                "values_files": [
                    {"path": "deploy/values/sample-app.yaml", "sha256": "approval_snapshot"}
                ],
            },
        ],
        "rollback": {"automatic": True, "strategy": "restore_approval_snapshot"},
    }
    operation_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")


def _stale_envelope_approval(approval: RealOperationApproval) -> RealOperationApproval:
    envelope_payload = approval.cluster_operation_envelopes[0].model_dump()
    envelope_payload["target_name"] = "stale-target"
    envelope_payload["digest"] = digest_json(
        {key: value for key, value in envelope_payload.items() if key != "digest"}
    )
    stale_envelope = ClusterOperationEnvelope.model_validate(envelope_payload)
    assert approval.scope_manifest is not None
    scope_payload = approval.scope_manifest.model_dump()
    scope_payload["cluster_operation_envelopes"] = [stale_envelope.model_dump(mode="json")]
    scope_payload["digest"] = digest_json(
        {key: value for key, value in scope_payload.items() if key != "digest"}
    )
    stale_scope = approval.scope_manifest.model_copy(
        update={"cluster_operation_envelopes": (stale_envelope,), "digest": scope_payload["digest"]}
    )
    return approval.model_copy(
        update={
            "scope_manifest": stale_scope,
            "cluster_operation_envelopes": (stale_envelope,),
        }
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()
