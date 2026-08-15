from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from helpers import config_values, create_fixture_project, valid_plan_values, write_config

from dispatcher.execution import SequentialExecutionCoordinator
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.sequential import PreparedBatch, SequentialWorkflow
from dispatcher.sessions import OpenCodeProcessError, SessionResult
from dispatcher.state_store import StateStore
from dispatcher.workflow import RunStatus, StepStatus, TransitionEvent, new_run_record


def test_batch_preparation_is_all_or_none_and_failed_children_join_durably(
    tmp_path: Path,
    caplog,
) -> None:
    project = create_fixture_project(tmp_path)
    sibling = _initialize_sibling_repository(project.root / "sibling")
    config = _parallel_two_repository_config(project, sibling)
    plan = _two_repository_plan(project)
    record = new_run_record(
        run_id="batch-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-batch"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(
        config,
        store,
        owner_id="batch-owner",
    )
    coordinator = SequentialExecutionCoordinator(
        config,
        store,
        workflow,
        owner_id="batch-owner",
        session_runner=_failing_session_runner,
    )
    coordinator.acquire_run(record.run_id)
    try:
        active, generation = workflow.activate(record.run_id, expected_generation=generation)
        invalid = json.dumps(
            {
                "protocol_version": 2,
                "action": "dispatch_batch",
                "children": [
                    {
                        "step_id": "prepare-fixture",
                        "target_role": "terra",
                        "session_mode": "new",
                        "prompt": "first fixture",
                    },
                    {
                        "step_id": "missing-step",
                        "target_role": "terra",
                        "session_mode": "new",
                        "prompt": "invalid fixture",
                    },
                ],
            }
        )
        try:
            workflow.prepare_from_supervisor(
                active.run_id,
                expected_generation=generation,
                supervisor_text=invalid,
            )
        except ValueError as exc:
            assert "unknown step missing-step" in str(exc)
        else:
            raise AssertionError("invalid batch was accepted")
        assert store.load_run(record.run_id)[0].dispatches == {}
        prepared = workflow.prepare_from_supervisor(
            active.run_id,
            expected_generation=generation,
            supervisor_text=json.dumps(
                {
                    "protocol_version": 2,
                    "action": "dispatch_batch",
                    "children": [
                        {
                            "step_id": "prepare-fixture",
                            "target_role": "terra",
                            "session_mode": "new",
                            "repo_id": "fixture-repo",
                            "prompt": "first fixture",
                        },
                        {
                            "step_id": "prepare-sibling",
                            "target_role": "terra",
                            "session_mode": "new",
                            "repo_id": "sibling-repo",
                            "prompt": "second fixture",
                        },
                    ],
                }
            ),
        )
        assert isinstance(prepared, PreparedBatch)
        persisted, _generation = store.load_run(record.run_id)
        assert len(persisted.dispatches) == 2
        assert persisted.batches[prepared.batch_id].state.value == "PREPARED"

        with caplog.at_level(logging.WARNING, logger="dispatcher.execution"):
            outcome = coordinator.execute_batch(prepared)
    finally:
        coordinator.release_run()

    assert outcome.record.state is RunStatus.WAITING_OPERATOR
    assert outcome.record.operator_request is not None
    assert outcome.record.operator_request.kind == "batch_reconciliation"
    assert outcome.record.batches[outcome.batch_id].state.value == "FAILED"
    assert len(outcome.record.batches[outcome.batch_id].failed_dispatch_ids) == 2
    assert {dispatch.state.value for dispatch in outcome.record.dispatches.values()} == {"FAILED"}
    for dispatch in outcome.record.dispatches.values():
        assert dispatch.failure_category == "unknown"
        assert dispatch.failure_detail == "deterministic batch child failure token=[REDACTED]"
        assert "batch-secret" not in dispatch.last_event.reason
    warnings = [
        record
        for record in caplog.records
        if record.name == "dispatcher.execution" and "batch child dispatch" in record.getMessage()
    ]
    assert len(warnings) == 2
    assert all(
        any(dispatch_id in record.getMessage() for record in warnings)
        for dispatch_id in outcome.record.batches[outcome.batch_id].failed_dispatch_ids
    )
    assert all("[unknown]" in record.getMessage() for record in warnings)
    assert all("token=[REDACTED]" in record.getMessage() for record in warnings)
    assert "batch-secret" not in caplog.text
    assert store.classify_recovery(record.run_id) == []
    reconciled, _generation = store.answer_operator_request(
        run_id=record.run_id,
        expected_generation=outcome.generation,
        request_id=outcome.record.operator_request.request_id,
        answer="reconcile",
        actor_id="operator",
    )
    assert reconciled.state is RunStatus.RUNNING
    assert {step.state.value for step in reconciled.steps.values()} == {"READY"}
    assert reconciled.batches[outcome.batch_id].state.value == "FAILED"


def test_successful_batch_forwards_and_acknowledges_every_child(
    tmp_path: Path,
    caplog,
) -> None:
    project = create_fixture_project(tmp_path)
    sibling = _initialize_sibling_repository(project.root / "sibling")
    config = _parallel_two_repository_config(project, sibling)
    plan = _two_repository_plan(project)
    record = new_run_record(
        run_id="successful-batch-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-successful-batch"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(
        config,
        store,
        owner_id="successful-batch-owner",
    )
    coordinator = SequentialExecutionCoordinator(
        config,
        store,
        workflow,
        owner_id="successful-batch-owner",
        session_runner=_usage_successful_session_runner,
    )
    coordinator.acquire_run(record.run_id)
    try:
        active, generation = workflow.activate(record.run_id, expected_generation=generation)
        prepared = workflow.prepare_from_supervisor(
            active.run_id,
            expected_generation=generation,
            supervisor_text=json.dumps(
                {
                    "protocol_version": 2,
                    "action": "dispatch_batch",
                    "children": [
                        {
                            "step_id": "prepare-fixture",
                            "target_role": "terra",
                            "session_mode": "new",
                            "prompt": "first fixture",
                        },
                        {
                            "step_id": "prepare-sibling",
                            "target_role": "terra",
                            "session_mode": "new",
                            "prompt": "second fixture",
                        },
                    ],
                }
            ),
        )
        assert isinstance(prepared, PreparedBatch)
        with caplog.at_level(logging.WARNING, logger="dispatcher.execution"):
            outcome = coordinator.execute_batch(prepared)
        assert outcome.record.state is RunStatus.RUNNING, [
            dispatch.last_event.reason for dispatch in outcome.record.dispatches.values()
        ]
        assert outcome.record.batches[outcome.batch_id].state.value == "JOINED"
        assert len(outcome.forwarded_dispatch_ids) == 2
        invocations = store.opencode_invocations_for_run(record.run_id)
        assert len(invocations) == 2
        assert all(invocation["usage_status"] == "COMPLETE" for invocation in invocations)
        assert outcome.record.usage.run.tokens_total == 20
        assert outcome.record.usage.by_role["terra"].tokens_total == 20
        assert {
            step_id: usage.tokens_total
            for step_id, usage in outcome.record.usage.by_step.items()
        } == {"prepare-fixture": 10, "prepare-sibling": 10}
        acknowledged = outcome.record
        generation = outcome.generation
        for dispatch_id in outcome.forwarded_dispatch_ids:
            acknowledged, generation = workflow.acknowledge_forwarding(
                acknowledged.run_id,
                expected_generation=generation,
                dispatch_id=dispatch_id,
            )
    finally:
        coordinator.release_run()

    assert {dispatch.state.value for dispatch in acknowledged.dispatches.values()} == {"ACKNOWLEDGED"}
    assert {step.state.value for step in acknowledged.steps.values()} == {"ACCEPTED"}
    assert not [
        record
        for record in caplog.records
        if record.name == "dispatcher.execution" and "batch child dispatch" in record.getMessage()
    ]


def test_parallel_verification_failures_persist_authoritative_evidence_before_reconciliation(
    tmp_path: Path,
) -> None:
    project = create_fixture_project(tmp_path)
    sibling = _initialize_sibling_repository(project.root / "sibling")
    config = _parallel_two_repository_config(project, sibling)
    plan = _two_repository_plan(project, failing_criteria=True)
    record = new_run_record(
        run_id="verification-failure-batch-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-verification-failure-batch"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(config, store, owner_id="verification-failure-batch-owner")
    coordinator = SequentialExecutionCoordinator(
        config,
        store,
        workflow,
        owner_id="verification-failure-batch-owner",
        session_runner=_successful_session_runner,
    )
    coordinator.acquire_run(record.run_id)
    try:
        active, generation = workflow.activate(record.run_id, expected_generation=generation)
        prepared = workflow.prepare_from_supervisor(
            active.run_id,
            expected_generation=generation,
            supervisor_text=json.dumps(
                {
                    "protocol_version": 2,
                    "action": "dispatch_batch",
                    "children": [
                        {
                            "step_id": "prepare-fixture",
                            "target_role": "terra",
                            "session_mode": "new",
                            "prompt": "first fixture",
                        },
                        {
                            "step_id": "prepare-sibling",
                            "target_role": "terra",
                            "session_mode": "new",
                            "prompt": "second fixture",
                        },
                    ],
                }
            ),
        )
        assert isinstance(prepared, PreparedBatch)
        outcome = coordinator.execute_batch(prepared)
    finally:
        coordinator.release_run()

    assert outcome.record.state is RunStatus.WAITING_OPERATOR
    assert outcome.record.operator_request is not None
    assert outcome.record.operator_request.kind == "batch_reconciliation"
    failed_ids = outcome.record.batches[outcome.batch_id].failed_dispatch_ids
    assert len(failed_ids) == 2
    for dispatch_id in failed_ids:
        dispatch = outcome.record.dispatches[dispatch_id]
        payload = store.load_dispatch_payload(outcome.record.run_id, dispatch_id)
        assert dispatch.failure_category == "authoritative_verification"
        assert dispatch.failure_detail is not None
        assert "transcript=" in dispatch.failure_detail
        assert outcome.record.steps[dispatch.step_id].state.value == "BLOCKED"
        assert payload.result is not None
        assert payload.authoritative_verification is not None
        assert payload.authoritative_verification[0]["status"] == "failed"

    reconciled, reconciled_generation = store.answer_operator_request(
        run_id=record.run_id,
        expected_generation=outcome.generation,
        request_id=outcome.record.operator_request.request_id,
        answer="reconcile",
        actor_id="verification-failure-batch-operator",
    )
    assert reconciled.state is RunStatus.RUNNING
    for dispatch_id in failed_ids:
        assert reconciled.steps[outcome.record.dispatches[dispatch_id].step_id].state is (
            StepStatus.READY
        )
    assert workflow.prepare_pending_verification_retry(reconciled, reconciled_generation) is None
    persisted, _generation = store.load_run(record.run_id)
    assert persisted.state is RunStatus.RUNNING
    assert persisted.operator_request is None
    for dispatch_id in failed_ids:
        assert persisted.steps[outcome.record.dispatches[dispatch_id].step_id].state is (
            StepStatus.READY
        )


def test_failed_batch_child_warns_once_and_preserves_successful_sibling(
    tmp_path: Path,
    caplog,
) -> None:
    project = create_fixture_project(tmp_path)
    sibling = _initialize_sibling_repository(project.root / "sibling")
    config = _parallel_two_repository_config(project, sibling)
    plan = _two_repository_plan(project)
    record = new_run_record(
        run_id="mixed-batch-run",
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-mixed-batch"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(
        config,
        store,
        owner_id="mixed-batch-owner",
    )
    coordinator = SequentialExecutionCoordinator(
        config,
        store,
        workflow,
        owner_id="mixed-batch-owner",
        session_runner=_mixed_session_runner,
    )
    coordinator.acquire_run(record.run_id)
    try:
        active, generation = workflow.activate(record.run_id, expected_generation=generation)
        prepared = workflow.prepare_from_supervisor(
            active.run_id,
            expected_generation=generation,
            supervisor_text=json.dumps(
                {
                    "protocol_version": 2,
                    "action": "dispatch_batch",
                    "children": [
                        {
                            "step_id": "prepare-fixture",
                            "target_role": "terra",
                            "session_mode": "new",
                            "prompt": "first fixture",
                        },
                        {
                            "step_id": "prepare-sibling",
                            "target_role": "terra",
                            "session_mode": "new",
                            "prompt": "second fixture",
                        },
                    ],
                }
            ),
        )
        assert isinstance(prepared, PreparedBatch)
        with caplog.at_level(logging.WARNING, logger="dispatcher.execution"):
            outcome = coordinator.execute_batch(prepared)
    finally:
        coordinator.release_run()

    failed_ids = outcome.record.batches[outcome.batch_id].failed_dispatch_ids
    assert len(failed_ids) == 1
    failed_id = failed_ids[0]
    successful_id = next(
        dispatch_id
        for dispatch_id in outcome.record.batches[outcome.batch_id].dispatch_ids
        if dispatch_id != failed_id
    )
    assert outcome.record.dispatches[failed_id].step_id == "prepare-sibling"
    assert outcome.record.dispatches[failed_id].state.value == "FAILED"
    assert outcome.record.dispatches[successful_id].state.value == "FORWARDED"
    assert outcome.record.steps["prepare-fixture"].state.value == "ACCEPTED"
    assert outcome.record.steps["prepare-sibling"].state.value == "BLOCKED"
    warnings = [
        record
        for record in caplog.records
        if record.name == "dispatcher.execution" and "batch child dispatch" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert failed_id in warnings[0].getMessage()
    assert successful_id not in warnings[0].getMessage()

    reconciled, _generation = store.answer_operator_request(
        run_id=outcome.record.run_id,
        expected_generation=outcome.generation,
        request_id=outcome.record.operator_request.request_id,
        answer="reconcile",
        actor_id="operator",
    )
    assert reconciled.steps["prepare-fixture"].state.value == "ACCEPTED"
    assert reconciled.steps["prepare-sibling"].state.value == "READY"


def _parallel_two_repository_config(project, sibling: Path):
    _commit_initial_repository(project.repository, "fixture.md")
    values = config_values(project)
    values["execution"].update(
        {
            "scheduling": "bounded_parallel",
            "concurrency": {
                "max_active_dispatches": 2,
                "max_batch_size": 2,
                "role_capacities": {"terra": 2, "reviewer": 1, "reviewer-two": 1},
                "failure_mode": "wait_for_started",
                "same_repository_mode": "serialized",
                "worktree_root": str(project.root / "worktrees"),
                "worktree_branch_prefix": "dispatcher/workspace",
            },
        }
    )
    values["permission_policies"]["policies"]["sibling-repository"] = {
        "default": "deny",
        "actions": {
            "inspect": "allow",
            "modify": "allow",
            "verify": "allow",
            "commit": "allow",
        },
    }
    values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    values["permission_policies"]["policies"]["executor-class"]["actions"]["commit"] = "allow"
    values["repositories"]["sibling-repo"] = {
        "root": str(sibling),
        "expected_remote": {"name": "origin", "url": "https://example.invalid/sibling.git"},
        "default_branch": "main",
        "evidence_roots": ["evidence"],
        "writable_roots": ["."],
        "external_roots": [],
        "commit_policy": "required",
        "permission_policy": "sibling-repository",
        "allow_shared_writable_roots": False,
    }
    return write_config(project, values)


def _two_repository_plan(project, *, failing_criteria: bool = False) -> NormalizedPlan:
    values = valid_plan_values(project)
    values["steps"][0]["authorization"] = {
        "authorized_actions": ["inspect", "modify", "verify", "commit"],
        "writable_paths": ["evidence/fixture.md"],
        "requires_operator_approval": False,
    }
    second = json.loads(json.dumps(values["steps"][0]))
    second.update(
        {
            "ordinal": 2,
            "step_id": "prepare-sibling",
            "title": "Prepare sibling",
            "repo_id": "sibling-repo",
            "produced_outputs": [
                {
                    "artifact_id": "sibling-output",
                    "producer_step_id": None,
                    "description": "Sibling output",
                }
            ],
            "resource_locks": [{"resource_id": "sibling-resource", "mode": "write"}],
            "evidence_requirements": [
                {
                    "artifact_id": "sibling-evidence",
                    "relative_path": "sibling.md",
                    "media_type": "text/markdown",
                }
            ],
            "authorization": {
                "authorized_actions": ["inspect", "modify", "verify", "commit"],
                "writable_paths": ["evidence/sibling.md"],
                "requires_operator_approval": False,
            },
        }
    )
    values["steps"].append(second)
    if failing_criteria:
        for step in values["steps"]:
            step["acceptance_criteria"][0]["check"]["argv"] = [
                "python",
                "-c",
                "raise SystemExit(1)",
            ]
    return NormalizedPlan.model_validate(values)


def _failing_session_runner(**kwargs: Any):
    lifecycle = kwargs["lifecycle"]
    lifecycle.on_process_started(1000, 1000.0)
    lifecycle.on_session_identified(f"session-{json.loads(kwargs['prompt'])['dispatch_id']}")
    raise OpenCodeProcessError("deterministic batch child failure token=batch-secret")


def _mixed_session_runner(**kwargs: Any) -> SessionResult:
    if json.loads(kwargs["prompt"])["step_id"] == "prepare-sibling":
        return _failing_session_runner(**kwargs)
    return _successful_session_runner(**kwargs)


def _successful_session_runner(**kwargs: Any) -> SessionResult:
    lifecycle = kwargs["lifecycle"]
    prompt = json.loads(kwargs["prompt"])
    session_id = f"session-{prompt['dispatch_id']}"
    lifecycle.on_process_started(1000, 1000.0)
    lifecycle.on_session_identified(session_id)
    sibling = prompt["step_id"] == "prepare-sibling"
    evidence_path = Path(kwargs["workdir"]) / "evidence" / (
        "sibling.md" if sibling else "fixture.md"
    )
    evidence_path.write_text("successful batch evidence\n", encoding="utf-8")
    return SessionResult(
        session_id=session_id,
        exit_code=0,
        evidence_written=[],
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
                        "artifact_id": "sibling-evidence" if sibling else "fixture-evidence",
                        "relative_path": "sibling.md" if sibling else "fixture.md",
                        "media_type": "text/markdown",
                    }
                ],
                "criterion_self_reports": [
                    {
                        "check_id": criterion["criterion_id"],
                        "status": "not_run",
                        "summary": "dispatcher owns this check",
                    }
                    for criterion in prompt["acceptance_criteria"]
                ],
                "summary": "successful batch child",
                "outcome": "completed",
            }
        ),
    )


def _usage_successful_session_runner(**kwargs: Any) -> SessionResult:
    result = _successful_session_runner(**kwargs)
    result.usage = {"total": 10, "input": 6, "output": 4, "reasoning": 0}
    result.cost = 0.01
    return result


def _initialize_sibling_repository(path: Path) -> Path:
    (path / "evidence").mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(path)], check=True, capture_output=True, text=True, timeout=10)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/sibling.git"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    _commit_initial_repository(path, "sibling.md")
    return path


def _commit_initial_repository(path: Path, evidence_name: str) -> None:
    evidence = path / "evidence" / evidence_name
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("initial evidence\n", encoding="utf-8")
    subprocess.run(["git", "config", "user.name", "Fixture Initializer"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"], cwd=path, check=True
    )
    subprocess.run(["git", "branch", "-M", "main"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial fixture"], cwd=path, check=True)


def _event(sequence: int) -> TransitionEvent:
    return TransitionEvent(
        event_id=f"event-{sequence}",
        sequence=sequence,
        actor="dispatcher",
        reason="batch fixture",
        correlation_id="batch-fixture",
        occurred_at=datetime.now(UTC),
    )
