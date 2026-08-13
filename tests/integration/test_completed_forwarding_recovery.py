from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import (
    FixtureProject,
    config_values,
    create_fixture_project,
    valid_plan_values,
    write_config,
)

from dispatcher.config import Config
from dispatcher.execution import (
    ExecutionCoordinatorError,
    SequentialExecutionCoordinator,
    SupervisorOutcome,
)
from dispatcher.git_commit import execute_structured_git_commit, prepare_structured_git_intent
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.repository import (
    EvidenceManifestEntry,
    RepositorySnapshot,
    authoritative_evidence,
    inspect_repository,
)
from dispatcher.results import parse_executor_proposal, parse_executor_result, parse_reviewer_result
from dispatcher.sequential import (
    PreparedDispatch,
    SequentialWorkflow,
    SequentialWorkflowError,
    WorkerResultValidationError,
)
from dispatcher.state_store import StateStore
from dispatcher.verification import AuthoritativeVerification
from dispatcher.workflow import (
    DispatchStatus,
    RunRecord,
    RunStatus,
    StepStatus,
    TransitionEvent,
    new_run_record,
    transition_dispatch,
    transition_step,
)


@dataclass(frozen=True)
class LegacyCompletedFixture:
    project: FixtureProject
    config: Config
    store: StateStore
    workflow: SequentialWorkflow
    run_id: str
    dispatch_id: str
    result_revision: str | None = None


def _event(sequence: int) -> TransitionEvent:
    return TransitionEvent(
        event_id=f"event-completed-recovery-{sequence}",
        sequence=sequence,
        actor="dispatcher",
        reason="completed forwarding recovery fixture",
        correlation_id="completed-recovery",
        occurred_at=datetime.now(UTC),
    )


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fake_snapshot() -> RepositorySnapshot:
    return RepositorySnapshot(
        repo_id="fixture-repo",
        branch="main",
        revision="base-sha",
        worktree_id="a" * 64,
        remote_name="origin",
        remote_url="https://example.invalid/fixture.git",
        clean=True,
        evidence=(
            EvidenceManifestEntry(
                root="evidence",
                relative_path="evidence/fixture.md",
                file_type="file",
                size_bytes=10,
                mode=0o644,
                mtime_ns=1,
                sha256="b" * 64,
            ),
        ),
        external=(),
        changes=(),
        ignored=(),
        dirty_patch_sha256="a" * 64,
        git_metadata_sha256="c" * 64,
        git_refs_sha256="d" * 64,
        manifest_sha256="e" * 64,
    )


def _fake_workflow(project: FixtureProject, store: StateStore) -> SequentialWorkflow:
    return SequentialWorkflow(
        project.config,
        store,
        owner_id="completed-recovery-owner",
        repository_inspector=lambda _config, _repo_id, require_clean: _fake_snapshot(),
    )


def _ready_record(
    project: FixtureProject,
    *,
    review_required: bool = False,
) -> RunRecord:
    values = valid_plan_values(project)
    if review_required:
        values["steps"][0]["review"] = {
            "required": True,
            "reviewer_role_keys": ["reviewer"],
            "required_acceptances": 1,
        }
        values["steps"][0]["retry"]["max_reviewer_attempts"] = 2
        values["steps"][0]["retry"]["max_executor_attempts"] = 2
    plan = NormalizedPlan.model_validate(values)
    record = new_run_record(
        run_id="completed-recovery-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-completed-recovery"),
        event=_event(1),
    )
    pending = record.steps["prepare-fixture"]
    ready = transition_step(pending, StepStatus.READY, _event(2))
    return record.model_copy(
        update={
            "steps": {"prepare-fixture": ready},
            "sequence": ready.last_event.sequence,
            "updated_at": ready.last_event.occurred_at,
        }
    )


def _dispatch_command(role: str = "terra") -> str:
    return json.dumps(
        {
            "protocol_version": 1,
            "action": "dispatch",
            "step_id": "prepare-fixture",
            "target_role": role,
            "session_mode": "new",
            "prompt": "Perform the approved fixture work.",
        }
    )


def _activate_and_prepare(
    tmp_path: Path,
    *,
    review_required: bool = False,
) -> tuple[StateStore, SequentialWorkflow, RunRecord, int, PreparedDispatch]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = create_fixture_project(tmp_path)
    store = StateStore(
        project.config.state_dir,
        heartbeat_seconds=project.config.lease_heartbeat_seconds,
        stale_after_seconds=project.config.lease_stale_after_seconds,
    )
    generation = store.create_run(_ready_record(project, review_required=review_required))
    workflow = _fake_workflow(project, store)
    record, generation = workflow.activate("completed-recovery-run", expected_generation=generation)
    prepared = workflow.prepare_from_supervisor(
        "completed-recovery-run",
        expected_generation=generation,
        supervisor_text=_dispatch_command(),
    )
    assert isinstance(prepared, PreparedDispatch)
    running = workflow.mark_running(prepared, process_id=999_999_999, process_create_time=1.0)
    running = workflow.record_session_id(running, runtime_session_id="session-recovery")
    return store, workflow, record, generation, running


def _executor_result(prepared: PreparedDispatch) -> dict:
    return {
        "result_version": 1,
        "response_contract": "dispatcher.executor_result.v1",
        "dispatch_id": prepared.dispatch.dispatch_id,
        "attempt": prepared.dispatch.attempt,
        "step_id": prepared.dispatch.step_id,
        "repository": {
            "repo_id": "fixture-repo",
            "base_revision": "base-sha",
            "result_revision": "base-sha",
            "patch_sha256": None,
        },
        "evidence": [
            {
                "artifact_id": "fixture-evidence",
                "relative_path": "fixture.md",
                "sha256": "b" * 64,
                "media_type": "text/markdown",
                "size_bytes": 10,
            }
        ],
        "verification": [
            {"check_id": "fixture-check", "status": "passed", "summary": "passed"}
        ],
        "summary": "completed recovery fixture result",
        "outcome": "completed",
    }


def _reviewer_result(prepared: PreparedDispatch) -> dict:
    assert prepared.review_target is not None
    return {
        "result_version": 1,
        "response_contract": "dispatcher.reviewer_result.v1",
        "dispatch_id": prepared.dispatch.dispatch_id,
        "attempt": prepared.dispatch.attempt,
        "step_id": prepared.dispatch.step_id,
        "repo_id": "fixture-repo",
        "review_target": prepared.review_target.model_dump(mode="json"),
        "findings": [],
        "verification": [
            {"check_id": "fixture-check", "status": "passed", "summary": "passed"}
        ],
        "required_remediation": [],
        "summary": "completed recovery fixture review",
        "verdict": "accepted",
    }


def _legacy_complete(
    store: StateStore,
    record: RunRecord,
    generation: int,
    dispatch: PreparedDispatch,
    result: dict,
    *,
    authoritative_verification: list[dict] | None = None,
    structured_git_final: dict | None = None,
) -> None:
    event = _event(record.sequence + 1)
    store.commit_dispatch_transition(
        record,
        expected_generation=generation,
        dispatch_id=dispatch.dispatch.dispatch_id,
        target=DispatchStatus.COMPLETED,
        event=event,
        result_digest=_sha256_json(result),
        result=result,
        authoritative_verification=(
            authoritative_verification
            if authoritative_verification is not None
            else [
                {
                    "check_id": "fixture-check",
                    "status": "passed",
                    "argv": ["python", "-c", "print('fixture check')"],
                    "exit_code": 0,
                    "timed_out": False,
                    "output_truncated": False,
                    "stdout_sha256": "a" * 64,
                    "stderr_sha256": "b" * 64,
                    "transcript_sha256": "c" * 64,
                    "duration_ms": 1,
                    "backend": "fixture-recovery",
                    "summary": "fixture authoritative check passed",
                }
            ]
        ),
        repository_after=_fake_snapshot().model_dump(mode="json"),
        structured_git_final=structured_git_final,
    )


def test_recover_completed_executor_dispatch_forwards_exactly_once(tmp_path: Path) -> None:
    store, workflow, _record, _generation, running = _activate_and_prepare(tmp_path)
    record, generation = store.load_run("completed-recovery-run")
    _legacy_complete(store, record, generation, running, _executor_result(running))

    recovered, _generation, forwarding = workflow.recover_completed_dispatch(
        "completed-recovery-run",
        running.dispatch.dispatch_id,
    )

    dispatch = recovered.dispatches[running.dispatch.dispatch_id]
    assert dispatch.state is DispatchStatus.FORWARDED
    assert recovered.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    payload = store.load_dispatch_payload("completed-recovery-run", running.dispatch.dispatch_id)
    assert payload.result is not None
    assert payload.forwarding_payload is not None
    assert json.loads(payload.forwarding_payload)["kind"] == "executor_result"
    assert "completed recovery fixture result" in forwarding
    assert store.classify_recovery("completed-recovery-run")[0].disposition == (
        "acknowledgement_required"
    )

    with pytest.raises(SequentialWorkflowError, match="requires a COMPLETED dispatch"):
        workflow.recover_completed_dispatch("completed-recovery-run", running.dispatch.dispatch_id)


def test_recover_completed_reviewer_dispatch_persists_review_and_acceptance_once(
    tmp_path: Path,
) -> None:
    store, workflow, _record, _generation, executor = _activate_and_prepare(
        tmp_path,
        review_required=True,
    )
    record, generation, _forwarding = workflow.apply_executor_result(
        executor,
        parse_executor_result(_executor_result(executor)),
    )
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=executor.dispatch.dispatch_id,
    )
    reviewer = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(role="reviewer"),
    )
    assert isinstance(reviewer, PreparedDispatch)
    reviewer = workflow.mark_running(reviewer, process_id=999_999_998, process_create_time=1.0)
    reviewer = workflow.record_session_id(reviewer, runtime_session_id="session-review-recovery")
    record, generation = store.load_run("completed-recovery-run")
    _legacy_complete(store, record, generation, reviewer, _reviewer_result(reviewer))

    recovered, _generation, _forwarding = workflow.recover_completed_dispatch(
        "completed-recovery-run",
        reviewer.dispatch.dispatch_id,
    )

    dispatch = recovered.dispatches[reviewer.dispatch.dispatch_id]
    assert dispatch.state is DispatchStatus.FORWARDED
    assert recovered.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert recovered.steps["prepare-fixture"].review_acceptances == 1
    assert store.review_for_dispatch("completed-recovery-run", reviewer.dispatch.dispatch_id)
    with sqlite3.connect(store.database_path) as connection:
        review_count = connection.execute(
            "SELECT COUNT(*) FROM reviews WHERE run_id = ? AND dispatch_id = ?",
            ("completed-recovery-run", reviewer.dispatch.dispatch_id),
        ).fetchone()[0]
    assert review_count == 1

    with pytest.raises(SequentialWorkflowError, match="requires a COMPLETED dispatch"):
        workflow.recover_completed_dispatch("completed-recovery-run", reviewer.dispatch.dispatch_id)


def test_adopt_failed_reviewer_result_retains_attempt_and_completes_step(
    tmp_path: Path,
) -> None:
    store, workflow, _record, _generation, executor = _activate_and_prepare(
        tmp_path,
        review_required=True,
    )
    record, generation, _forwarding = workflow.apply_executor_result(
        executor,
        parse_executor_result(_executor_result(executor)),
    )
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=executor.dispatch.dispatch_id,
    )
    reviewer = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(role="reviewer"),
    )
    assert isinstance(reviewer, PreparedDispatch)
    reviewer = workflow.mark_running(reviewer, process_id=999_999_997, process_create_time=1.0)
    reviewer = workflow.record_session_id(reviewer, runtime_session_id="session-failed-review")
    workflow.fail_dispatch(
        reviewer,
        reason="worker response was not strict JSON",
        failure_category="result_validation",
        failure_detail="worker response was not strict JSON",
    )
    failed, _generation = store.load_run(reviewer.run_id)
    assert failed.state is RunStatus.WAITING_OPERATOR

    adopted, generation = workflow.adopt_failed_reviewer_result(
        reviewer.run_id,
        reviewer.dispatch.dispatch_id,
        parse_reviewer_result(_reviewer_result(reviewer)),
        runtime_session_id="session-failed-review",
        authoritative_verification=(
            AuthoritativeVerification(
                check_id="fixture-check",
                status="passed",
                argv=("python", "-c", "print('fixture check')"),
                exit_code=0,
                timed_out=False,
                output_truncated=False,
                stdout_sha256="a" * 64,
                stderr_sha256="b" * 64,
                transcript_sha256="c" * 64,
                duration_ms=1,
                backend="fixture-recovery",
                summary="fixture authoritative check passed",
            ),
        ),
        usage=None,
        actor_id="fixture-recovery",
    )

    assert adopted.state is RunStatus.RUNNING
    assert adopted.steps[reviewer.dispatch.step_id].state is StepStatus.ACCEPTED
    assert adopted.steps[reviewer.dispatch.step_id].reviewer_attempts == 1
    assert adopted.dispatches[reviewer.dispatch.dispatch_id].state is DispatchStatus.FAILED
    assert store.review_for_dispatch(reviewer.run_id, reviewer.dispatch.dispatch_id)
    assert store.load_dispatch_payload(reviewer.run_id, reviewer.dispatch.dispatch_id).result is not None
    completion = workflow.evaluate_completion(adopted, generation)
    assert completion.accepted

    recovered, generation = workflow.record_adopted_failed_review_usage(
        reviewer.run_id,
        reviewer.dispatch.dispatch_id,
        parse_reviewer_result(_reviewer_result(reviewer)),
        runtime_session_id="session-failed-review",
        usage={
            "cost_usd": 0.25,
            "tokens_total": 100,
            "tokens_input": 70,
            "tokens_output": 20,
            "tokens_reasoning": 10,
        },
    )

    assert recovered.state is RunStatus.SUCCEEDED
    assert recovered.usage.by_role["reviewer"].tokens_total == 100
    assert store.load_run(reviewer.run_id)[1] == generation
    with pytest.raises(SequentialWorkflowError, match="already recorded"):
        workflow.record_adopted_failed_review_usage(
            reviewer.run_id,
            reviewer.dispatch.dispatch_id,
            parse_reviewer_result(_reviewer_result(reviewer)),
            runtime_session_id="session-failed-review",
            usage={
                "cost_usd": 0.25,
                "tokens_total": 100,
                "tokens_input": 70,
                "tokens_output": 20,
                "tokens_reasoning": 10,
            },
        )


def test_legacy_completed_structured_commit_is_forwarded_without_a_new_commit(
    tmp_path: Path,
) -> None:
    fixture = _legacy_committed_fixture(tmp_path)
    store = fixture.store
    workflow = fixture.workflow

    recovered, _generation, forwarding = workflow.recover_completed_dispatch(
        fixture.run_id,
        fixture.dispatch_id,
    )

    dispatch = recovered.dispatches[fixture.dispatch_id]
    assert dispatch.state is DispatchStatus.FORWARDED
    assert recovered.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    structured = store.load_structured_git_record(fixture.run_id, fixture.dispatch_id)
    assert structured.state == "COMMITTED"
    payload = store.load_dispatch_payload(fixture.run_id, fixture.dispatch_id)
    assert payload.result is not None
    assert payload.result["repository"]["result_revision"] == fixture.result_revision
    assert fixture.result_revision is not None
    assert json.loads(payload.forwarding_payload)["kind"] == "executor_result"
    assert "legacy committed fixture" in forwarding
    assert _git(fixture.project.repository, "rev-list", "--count", "HEAD") == "2"
    assert _git(fixture.project.repository, "status", "--porcelain") == ""


def test_persist_forwarded_dispatch_is_all_or_nothing(tmp_path: Path) -> None:
    store, _workflow, _record, _generation, running = _activate_and_prepare(tmp_path)
    record, generation = store.load_run("completed-recovery-run")
    dispatch = record.dispatches[running.dispatch.dispatch_id]
    result = _executor_result(running)
    completion_event = _event(record.sequence + 1)
    completed = transition_dispatch(
        dispatch,
        DispatchStatus.COMPLETED,
        completion_event,
        result_digest=_sha256_json(result),
    )
    forwarding = json.dumps(
        {"kind": "executor_result", "dispatch_id": dispatch.dispatch_id},
        sort_keys=True,
    )
    forward_event = _event(record.sequence + 2)
    forwarded = transition_dispatch(
        completed,
        DispatchStatus.FORWARDED,
        forward_event,
        forwarding_digest=_sha256_text(forwarding),
    )
    record = record.model_copy(
        update={
            "dispatches": {dispatch.dispatch_id: forwarded},
            "sequence": forward_event.sequence,
            "updated_at": forward_event.occurred_at,
        }
    )

    class InjectedFault(RuntimeError):
        pass

    def fault() -> None:
        raise InjectedFault("injected persistence failure")

    with pytest.raises(InjectedFault):
        store.persist_forwarded_dispatch(
            record,
            expected_generation=generation,
            dispatch_id=dispatch.dispatch_id,
            result=result,
            authoritative_verification=[
                {
                    "check_id": "fixture-check",
                    "status": "passed",
                    "argv": ["python", "-c", "print('fixture check')"],
                    "exit_code": 0,
                    "timed_out": False,
                    "output_truncated": False,
                    "stdout_sha256": "a" * 64,
                    "stderr_sha256": "b" * 64,
                    "transcript_sha256": "c" * 64,
                    "duration_ms": 1,
                    "backend": "fixture-recovery",
                    "summary": "fixture authoritative check passed",
                }
            ],
            repository_after=_fake_snapshot().model_dump(mode="json"),
            forwarding_payload=forwarding,
            fault_hook=fault,
        )

    stored, stored_generation = store.load_run("completed-recovery-run")
    assert stored_generation == generation
    assert stored.dispatches[dispatch.dispatch_id].state is DispatchStatus.RUNNING
    payload = store.load_dispatch_payload("completed-recovery-run", dispatch.dispatch_id)
    assert payload.result is None
    assert payload.forwarding_payload is None
    assert payload.authoritative_verification is None


def test_run_to_completion_recovers_completed_dispatch_before_supervisor_turn(
    tmp_path: Path,
) -> None:
    store, workflow, _record, generation, running = _activate_and_prepare(tmp_path)
    current, current_generation = store.load_run("completed-recovery-run")
    _legacy_complete(store, current, current_generation, running, _executor_result(running))
    _completed, generation = store.load_run("completed-recovery-run")
    coordinator = SequentialExecutionCoordinator(
        workflow.config,
        store,
        workflow,
        owner_id="completed-recovery-owner",
        session_runner=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no worker run expected")),
    )
    prompts: list[str] = []

    def supervisor_turn(_run_id: str, **kwargs) -> SupervisorOutcome:
        prompts.append(kwargs["prompt"])
        envelope = json.loads(kwargs["prompt"])
        forwardings = envelope["pending_forwardings"]
        assert [item["dispatch_id"] for item in forwardings] == [running.dispatch.dispatch_id]
        assert forwardings[0]["payload"]["kind"] == "executor_result"
        return SupervisorOutcome(
            json.dumps(
                {"protocol_version": 1, "action": "halt", "reason": "recovery proof complete"}
            ),
            "supervisor-recovery",
            kwargs["expected_generation"],
        )

    coordinator.run_supervisor_turn = supervisor_turn  # type: ignore[method-assign]

    with pytest.raises(ExecutionCoordinatorError, match="HALTED"):
        coordinator.run_to_completion(
            "completed-recovery-run",
            expected_generation=generation,
        )

    persisted, _persisted_generation = store.load_run("completed-recovery-run")
    assert persisted.dispatches[running.dispatch.dispatch_id].state is DispatchStatus.ACKNOWLEDGED
    assert persisted.state is RunStatus.HALTED
    assert len(prompts) == 1


def _legacy_committed_fixture(tmp_path: Path) -> LegacyCompletedFixture:
    committed_root = tmp_path / "committed"
    committed_root.mkdir(parents=True, exist_ok=True)
    project = create_fixture_project(committed_root)
    result_path = project.repository / "result.txt"
    result_path.write_text("before\n", encoding="utf-8")
    (project.evidence / "fixture.md").write_text("before evidence\n", encoding="utf-8")
    _git(project.repository, "config", "user.name", "Fixture Initializer")
    _git(project.repository, "config", "user.email", "fixture@example.invalid")
    _git(project.repository, "branch", "-M", "main")
    _git(project.repository, "add", ".")
    _git(project.repository, "commit", "-m", "initial fixture")
    values = config_values(project)
    values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    values["permission_policies"]["policies"]["executor-class"]["actions"]["commit"] = "allow"
    config = write_config(project, values)
    plan_values = valid_plan_values(project)
    plan_values["steps"][0]["authorization"] = {
        "authorized_actions": ["inspect", "modify", "verify", "commit"],
        "writable_paths": ["evidence/fixture.md", "result.txt"],
        "requires_operator_approval": False,
    }
    plan = NormalizedPlan.model_validate(plan_values)
    run_id = "committed-recovery-run"
    record = new_run_record(
        run_id=run_id,
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-committed-recovery"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(config, store, owner_id="committed-recovery-owner")
    active, generation = workflow.activate(run_id, expected_generation=generation)
    prepared = workflow.prepare_from_supervisor(
        run_id,
        expected_generation=generation,
        supervisor_text=json.dumps(
            {
                "protocol_version": 1,
                "action": "dispatch",
                "step_id": "prepare-fixture",
                "target_role": "terra",
                "session_mode": "new",
                "prompt": "write the exact fixture files",
            }
        ),
    )
    assert isinstance(prepared, PreparedDispatch)
    running = workflow.mark_running(prepared, process_id=999_999_999, process_create_time=1.0)
    running = workflow.record_session_id(running, runtime_session_id="session-committed-recovery")
    result_path.write_text("after\n", encoding="utf-8")
    (project.evidence / "fixture.md").write_text("after evidence\n", encoding="utf-8")
    prompt = json.loads(running.prompt)
    proposal = parse_executor_proposal(
        {
            "proposal_version": 2,
            "response_contract": "dispatcher.executor_proposal.v2",
            "dispatch_id": running.dispatch.dispatch_id,
            "attempt": running.dispatch.attempt,
            "step_id": running.dispatch.step_id,
            "repository": {
                "repo_id": running.dispatch.intent.repository.repo_id,
                "base_revision": running.dispatch.intent.repository.base_revision,
            },
            "evidence": [
                {
                    "artifact_id": item["artifact_id"],
                    "relative_path": item["relative_path"],
                    "media_type": item["media_type"],
                }
                for item in prompt["evidence_requirements"]
            ],
            "criterion_self_reports": [
                {
                    "check_id": item["criterion_id"],
                    "status": "not_run",
                    "summary": "dispatcher owns this check",
                }
                for item in prompt["acceptance_criteria"]
            ],
            "summary": "legacy committed fixture",
            "outcome": "completed",
        }
    )
    workflow.record_executor_proposal(running, proposal)
    dirty = inspect_repository(config, "fixture-repo", require_clean=False)
    verification = tuple(
        AuthoritativeVerification(
            check_id=criterion.criterion_id,
            status="passed",
            argv=criterion.check.argv,
            exit_code=0,
            timed_out=False,
            output_truncated=False,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
            transcript_sha256="c" * 64,
            duration_ms=1,
            backend="fixture-committed-recovery",
            summary="fixture check passed before the commit",
        )
        for criterion in plan.steps[0].acceptance_criteria
    )
    artifacts = authoritative_evidence(
        config,
        repo_id="fixture-repo",
        snapshot=dirty,
        requirements=plan.steps[0].evidence_requirements,
        declarations=proposal.evidence,
    )
    intent = prepare_structured_git_intent(
        config,
        step=plan.steps[0],
        attempt=running.dispatch.attempt,
        worktree=project.repository,
        coordinate=running.dispatch.intent.repository,
        before=running.repository_before,
        dirty=dirty,
    )
    store.record_structured_git_checked(
        run_id=run_id,
        dispatch_id=running.dispatch.dispatch_id,
        checked={
            "authoritative_verification": [item.model_dump(mode="json") for item in verification],
            "evidence": [item.model_dump(mode="json") for item in artifacts],
            "repository_before": running.repository_before.model_dump(mode="json"),
            "repository_dirty": dirty.model_dump(mode="json"),
            "usage": None,
        },
        intent=intent.model_dump(mode="json"),
    )

    def record_staged(stage) -> None:
        store.record_structured_git_staged(
            run_id=run_id,
            dispatch_id=running.dispatch.dispatch_id,
            stage=stage.model_dump(mode="json"),
        )

    outcome = execute_structured_git_commit(
        config,
        worktree=project.repository,
        intent=intent,
        on_staged=record_staged,
    )
    committed_artifacts = authoritative_evidence(
        config,
        repo_id="fixture-repo",
        snapshot=outcome.repository_after,
        requirements=plan.steps[0].evidence_requirements,
        declarations=proposal.evidence,
    )
    result = {
        "result_version": 1,
        "response_contract": "dispatcher.executor_result.v1",
        "dispatch_id": running.dispatch.dispatch_id,
        "attempt": running.dispatch.attempt,
        "step_id": running.dispatch.step_id,
        "repository": {
            "repo_id": "fixture-repo",
            "base_revision": running.dispatch.intent.repository.base_revision,
            "result_revision": outcome.result_revision,
            "patch_sha256": None,
        },
        "evidence": [item.model_dump(mode="json") for item in committed_artifacts],
        "verification": [
            {"check_id": item.criterion_id, "status": "passed", "summary": "passed"}
            for item in plan.steps[0].acceptance_criteria
        ],
        "summary": "legacy committed fixture",
        "outcome": "completed",
    }
    durable, durable_generation = store.load_run(run_id)
    _legacy_complete(
        store,
        durable,
        durable_generation,
        running,
        result,
        authoritative_verification=[item.model_dump(mode="json") for item in verification],
        structured_git_final={
            "state": "COMMITTED",
            "commit": outcome.commit.model_dump(mode="json"),
            "result_revision": outcome.result_revision,
            "repository_after": outcome.repository_after.model_dump(mode="json"),
        },
    )
    store.close()
    reopened = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    return LegacyCompletedFixture(
        project=project,
        config=config,
        store=reopened,
        workflow=SequentialWorkflow(config, reopened, owner_id="committed-recovery-owner"),
        run_id=run_id,
        dispatch_id=running.dispatch.dispatch_id,
        result_revision=outcome.result_revision,
    )


def test_materialization_rejects_mutation_between_proposal_and_verification(
    tmp_path: Path,
) -> None:
    binding_root = tmp_path / "binding"
    binding_root.mkdir(parents=True, exist_ok=True)
    project = create_fixture_project(binding_root)
    result_path = project.repository / "result.txt"
    result_path.write_text("before\n", encoding="utf-8")
    (project.evidence / "fixture.md").write_text("before evidence\n", encoding="utf-8")
    _git(project.repository, "config", "user.name", "Fixture Initializer")
    _git(project.repository, "config", "user.email", "fixture@example.invalid")
    _git(project.repository, "branch", "-M", "main")
    _git(project.repository, "add", ".")
    _git(project.repository, "commit", "-m", "initial fixture")
    values = config_values(project)
    values["permission_policies"]["policies"]["repository"]["actions"]["commit"] = "allow"
    values["permission_policies"]["policies"]["executor-class"]["actions"]["commit"] = "allow"
    config = write_config(project, values)
    plan_values = valid_plan_values(project)
    plan_values["steps"][0]["authorization"] = {
        "authorized_actions": ["inspect", "modify", "verify", "commit"],
        "writable_paths": ["evidence/fixture.md", "result.txt"],
        "requires_operator_approval": False,
    }
    plan = NormalizedPlan.model_validate(plan_values)
    run_id = "binding-run"
    record = new_run_record(
        run_id=run_id,
        project_id=config.project_id,
        config_digest=config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-binding"),
        event=_event(1),
    )
    store = StateStore(
        config.state_dir,
        heartbeat_seconds=config.lease_heartbeat_seconds,
        stale_after_seconds=config.lease_stale_after_seconds,
    )
    generation = store.create_run(record)
    workflow = SequentialWorkflow(config, store, owner_id="binding-owner")
    _active, generation = workflow.activate(run_id, expected_generation=generation)
    prepared = workflow.prepare_from_supervisor(
        run_id,
        expected_generation=generation,
        supervisor_text=json.dumps(
            {
                "protocol_version": 1,
                "action": "dispatch",
                "step_id": "prepare-fixture",
                "target_role": "terra",
                "session_mode": "new",
                "prompt": "write the exact fixture files",
            }
        ),
    )
    assert isinstance(prepared, PreparedDispatch)
    running = workflow.mark_running(prepared, process_id=999_999_999, process_create_time=1.0)
    running = workflow.record_session_id(running, runtime_session_id="session-binding")
    result_path.write_text("proposal-time content\n", encoding="utf-8")
    (project.evidence / "fixture.md").write_text("proposal evidence\n", encoding="utf-8")
    proposal = parse_executor_proposal(
        {
            "proposal_version": 2,
            "response_contract": "dispatcher.executor_proposal.v2",
            "dispatch_id": running.dispatch.dispatch_id,
            "attempt": running.dispatch.attempt,
            "step_id": running.dispatch.step_id,
            "repository": {
                "repo_id": "fixture-repo",
                "base_revision": running.dispatch.intent.repository.base_revision,
            },
            "evidence": [
                {
                    "artifact_id": requirement.artifact_id,
                    "relative_path": requirement.relative_path,
                    "media_type": requirement.media_type,
                }
                for requirement in plan.steps[0].evidence_requirements
            ],
            "criterion_self_reports": [
                {"check_id": criterion.criterion_id, "status": "not_run", "summary": "not run"}
                for criterion in plan.steps[0].acceptance_criteria
            ],
            "summary": "mutation binding fixture",
            "outcome": "completed",
        }
    )
    proposal_snapshot = workflow.record_executor_proposal(running, proposal)
    verification = tuple(
        AuthoritativeVerification(
            check_id=criterion.criterion_id,
            status="passed",
            argv=criterion.check.argv,
            exit_code=0,
            timed_out=False,
            output_truncated=False,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
            transcript_sha256="c" * 64,
            duration_ms=1,
            backend="fixture-binding",
            summary="fixture check passed",
        )
        for criterion in plan.steps[0].acceptance_criteria
    )
    result_path.write_text("mutated after verification\n", encoding="utf-8")

    with pytest.raises(WorkerResultValidationError, match="changed between proposal inspection"):
        workflow.materialize_executor_proposal(
            running,
            proposal,
            authoritative_verification=verification,
            usage=None,
            verified_snapshot=proposal_snapshot,
        )


def test_recover_command_recovers_a_durable_completed_forwarding(
    tmp_path: Path,
    capsys,
) -> None:
    store, workflow, _record, _generation, running = _activate_and_prepare(tmp_path)
    record, generation = store.load_run("completed-recovery-run")
    _legacy_complete(store, record, generation, running, _executor_result(running))

    from dispatcher.cli import main

    assert (
        main(
            [
                "recover",
                "--config",
                str(workflow.config.config_path),
                "--run-id",
                "completed-recovery-run",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "recovered durable forwarding from completed result" in output
    stored = StateStore(
        workflow.config.state_dir,
        heartbeat_seconds=workflow.config.lease_heartbeat_seconds,
        stale_after_seconds=workflow.config.lease_stale_after_seconds,
    )
    persisted, _persisted_generation = stored.load_run("completed-recovery-run")
    assert persisted.dispatches[running.dispatch.dispatch_id].state is DispatchStatus.FORWARDED
    assert stored.classify_recovery("completed-recovery-run")[0].disposition == (
        "acknowledgement_required"
    )


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
