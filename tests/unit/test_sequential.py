from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from helpers import FixtureProject, create_fixture_project, valid_plan_values

from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.protocol import parse_supervisor_command
from dispatcher.repository import EvidenceManifestEntry, RepositorySnapshot
from dispatcher.results import parse_executor_result, parse_reviewer_result
from dispatcher.sequential import (
    CompletionDecision,
    PreparedDispatch,
    SequentialWorkflow,
    SequentialWorkflowError,
)
from dispatcher.state_store import StateStore
from dispatcher.workflow import (
    RunRecord,
    StepStatus,
    TransitionEvent,
    UsageAmount,
    UsageLedger,
    new_run_record,
    transition_step,
)

_EVIDENCE_SHA = "a" * 64


@pytest.fixture
def project(tmp_path: Path) -> FixtureProject:
    return create_fixture_project(tmp_path)


def _event(sequence: int) -> TransitionEvent:
    return TransitionEvent(
        event_id=f"event-{sequence}",
        sequence=sequence,
        actor="dispatcher",
        reason="fixture workflow transition",
        correlation_id="fixture-correlation",
        occurred_at=datetime.now(UTC),
    )


def _store(project: FixtureProject) -> StateStore:
    return StateStore(
        project.state,
        heartbeat_seconds=project.config.lease_heartbeat_seconds,
        stale_after_seconds=project.config.lease_stale_after_seconds,
    )


def _record(
    project: FixtureProject,
    *,
    review_required: bool = False,
    max_reviewer_attempts: int | None = None,
    max_executor_attempts: int | None = None,
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
        values["steps"][0]["retry"]["on_changes_requested"] = "retry"
    if max_reviewer_attempts is not None:
        values["steps"][0]["retry"]["max_reviewer_attempts"] = max_reviewer_attempts
    if max_executor_attempts is not None:
        values["steps"][0]["retry"]["max_executor_attempts"] = max_executor_attempts
    plan = NormalizedPlan.model_validate(values)
    record = new_run_record(
        run_id="fixture-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-approve-plan"),
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


def _workflow(project: FixtureProject, store: StateStore) -> SequentialWorkflow:
    return SequentialWorkflow(
        project.config,
        store,
        owner_id="fixture-owner",
        repository_inspector=lambda _config, _repo_id, require_clean: _repository_snapshot(),
    )


def _repository_snapshot() -> RepositorySnapshot:
    return RepositorySnapshot(
        repo_id="fixture-repo",
        branch="main",
        revision="base-sha",
        worktree_id="b" * 64,
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
                sha256=_EVIDENCE_SHA,
            ),
        ),
        external=(),
        changes=(),
        manifest_sha256="c" * 64,
    )


def _dispatch_command(role: str = "terra", mode: str = "new") -> str:
    return json.dumps(
        {
            "protocol_version": 1,
            "action": "dispatch",
            "step_id": "prepare-fixture",
            "target_role": role,
            "session_mode": mode,
            "prompt": "Perform the approved fixture work.",
            "rationale": "fixture",
        }
    )


def _executor_result(prepared: PreparedDispatch, outcome: str = "completed") -> dict[str, Any]:
    result: dict[str, Any] = {
        "result_version": 1,
        "dispatch_id": prepared.dispatch.dispatch_id,
        "attempt": prepared.dispatch.attempt,
        "step_id": "prepare-fixture",
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
                "sha256": _EVIDENCE_SHA,
                "media_type": "text/markdown",
                "size_bytes": 10,
            }
        ],
        "verification": [{"check_id": "fixture-check", "status": "passed", "summary": "passed"}],
        "summary": "fixture executor result",
        "outcome": outcome,
    }
    if outcome == "failed":
        result["failure_code"] = "fixture-failure"
    if outcome == "blocked":
        result["blockers"] = ["fixture blocker"]
    return result


def _activate_ready_run(
    project: FixtureProject,
    *,
    review_required: bool = False,
    max_reviewer_attempts: int | None = None,
    max_executor_attempts: int | None = None,
) -> tuple[StateStore, SequentialWorkflow, RunRecord, int]:
    store = _store(project)
    record = _record(
        project,
        review_required=review_required,
        max_reviewer_attempts=max_reviewer_attempts,
        max_executor_attempts=max_executor_attempts,
    )
    generation = store.create_run(record)
    workflow = _workflow(project, store)
    active, generation = workflow.activate(record.run_id, expected_generation=generation)
    return store, workflow, active, generation


def _prepare_executor(
    project: FixtureProject,
    *,
    review_required: bool = False,
    max_reviewer_attempts: int | None = None,
    max_executor_attempts: int | None = None,
) -> tuple[StateStore, SequentialWorkflow, PreparedDispatch]:
    _store_value, workflow, record, generation = _activate_ready_run(
        project,
        review_required=review_required,
        max_reviewer_attempts=max_reviewer_attempts,
        max_executor_attempts=max_executor_attempts,
    )
    prepared = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(),
    )
    assert isinstance(prepared, PreparedDispatch)
    running = workflow.mark_running(prepared, process_id=1234)
    identified = workflow.record_session_id(running, runtime_session_id="ses-executor")
    return _store_value, workflow, identified


def test_bootstrap_is_self_contained_and_persisted(project: FixtureProject) -> None:
    store, workflow, record, _generation = _activate_ready_run(project)

    bootstrap, path = workflow.render_bootstrap(record.run_id)

    assert record.plan_digest in bootstrap
    assert "specification.md" in bootstrap
    assert "plan.md" in bootstrap
    assert "prepare-fixture" in bootstrap
    assert path.is_file()
    examples = re.findall(r"```json\n(.*?)\n```", bootstrap, flags=re.DOTALL)
    assert len(examples) == 2
    assert all(parse_supervisor_command(example) for example in examples)


def test_executor_dispatch_transitions_only_its_ready_step_and_completion_is_guarded(
    project: FixtureProject,
) -> None:
    store, workflow, prepared = _prepare_executor(project)

    record, generation, forwarding = workflow.apply_executor_result(
        prepared,
        parse_executor_result(_executor_result(prepared)),
    )

    assert record.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert "executor_result" in forwarding
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=prepared.dispatch.dispatch_id,
    )
    decision = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text='{"protocol_version":1,"action":"request_completion"}',
    )

    assert isinstance(decision, CompletionDecision)
    assert decision.accepted
    assert decision.report_path is not None
    assert store.load_run(record.run_id)[0].state.value == "SUCCEEDED"


def test_stall_requeues_with_a_fresh_dispatch_and_preserves_the_stall_count(project: FixtureProject) -> None:
    store, workflow, prepared = _prepare_executor(project, max_executor_attempts=2)

    record, generation, retry_allowed = workflow.handle_stall(
        prepared,
        category="timeout",
        reason="synthetic timeout",
    )

    assert retry_allowed is True
    assert record.steps["prepare-fixture"].state is StepStatus.READY
    assert record.steps["prepare-fixture"].stalls == 1
    assert record.steps["prepare-fixture"].last_stall_category == "timeout"
    assert record.dispatches[prepared.dispatch.dispatch_id].state.value == "FAILED"

    retry = workflow.prepare_stall_retry(record, generation, prepared, category="timeout")

    assert retry.dispatch.dispatch_id != prepared.dispatch.dispatch_id
    assert retry.dispatch.attempt == 2
    assert json.loads(retry.prompt)["task"].startswith("Continue the current approved step")


def test_stall_retry_exhaustion_enters_operator_wait(project: FixtureProject) -> None:
    store, workflow, prepared = _prepare_executor(project, max_executor_attempts=4)
    current = prepared
    record = None
    generation = prepared.generation
    for attempt in range(3):
        record, generation, retry_allowed = workflow.handle_stall(
            current,
            category="connection",
            reason=f"synthetic connection interruption {attempt + 1}",
        )
        if retry_allowed:
            current = workflow.prepare_stall_retry(record, generation, current, category="connection")
            current = workflow.record_session_id(
                workflow.mark_running(current, process_id=5000 + attempt),
                runtime_session_id=f"session-stall-{attempt}",
            )

    assert record is not None
    assert record.state.value == "WAITING_OPERATOR"
    assert record.operator_request is not None
    assert record.operator_request.kind == "stall_recovery"
    assert record.steps["prepare-fixture"].stalls == 3
    assert record.steps["prepare-fixture"].state is StepStatus.BLOCKED


def test_process_and_session_identity_are_separate_atomic_generations(
    project: FixtureProject,
) -> None:
    store, workflow, record, generation = _activate_ready_run(project)
    prepared = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(),
    )
    assert isinstance(prepared, PreparedDispatch)

    running = workflow.mark_running(prepared, process_id=4321)

    assert running.generation == prepared.generation + 1
    assert running.dispatch.state.value == "RUNNING"
    assert running.dispatch.runtime_session_id is None
    assert store.sessions_for_run(record.run_id) == {}
    assert store.load_dispatch_payload(record.run_id, running.dispatch.dispatch_id).process_id == 4321

    identified = workflow.record_session_id(running, runtime_session_id="ses-atomic")

    assert identified.generation == running.generation + 1
    assert identified.dispatch.runtime_session_id == "ses-atomic"
    assert store.sessions_for_run(record.run_id)["executors"]["terra"]["session_id"] == (
        "ses-atomic"
    )
    with pytest.raises(SequentialWorkflowError, match="generation is stale"):
        workflow.record_session_id(running, runtime_session_id="ses-duplicate")


def test_unknown_step_and_missing_session_fail_before_dispatch_persistence(project: FixtureProject) -> None:
    store, workflow, record, generation = _activate_ready_run(project)
    unknown = json.loads(_dispatch_command())
    unknown["step_id"] = "missing-step"

    with pytest.raises(SequentialWorkflowError, match="unknown plan step"):
        workflow.prepare_from_supervisor(
            record.run_id,
            expected_generation=generation,
            supervisor_text=json.dumps(unknown),
        )
    resume = json.loads(_dispatch_command(mode="resume"))
    with pytest.raises(SequentialWorkflowError, match="no dispatcher-owned session"):
        workflow.prepare_from_supervisor(
            record.run_id,
            expected_generation=generation,
            supervisor_text=json.dumps(resume),
        )

    assert store.load_run(record.run_id)[0].dispatches == {}


def test_supervisor_repository_assertion_cannot_override_normalized_step(project: FixtureProject) -> None:
    store, workflow, record, generation = _activate_ready_run(project)
    command = json.loads(_dispatch_command())
    command["repo_id"] = "other-repo"

    with pytest.raises(SequentialWorkflowError, match="does not match step repository"):
        workflow.prepare_from_supervisor(
            record.run_id,
            expected_generation=generation,
            supervisor_text=json.dumps(command),
        )

    assert store.load_run(record.run_id)[0].dispatches == {}


def test_risk_gate_waits_durably_and_approve_resumes_the_exact_step(project: FixtureProject) -> None:
    values = valid_plan_values(project)
    values["steps"][0]["authorization"]["requires_operator_approval"] = True
    plan = NormalizedPlan.model_validate(values)
    record = new_run_record(
        run_id="risk-gate-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-risk-gate"),
        event=_event(1),
    )
    store = _store(project)
    generation = store.create_run(record)
    workflow = _workflow(project, store)
    active, generation = workflow.activate(record.run_id, expected_generation=generation)

    waiting = workflow.prepare_from_supervisor(
        active.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(),
    )

    assert isinstance(waiting, RunRecord)
    assert waiting.state.value == "WAITING_OPERATOR"
    assert waiting.operator_request is not None
    assert waiting.operator_request.kind == "risk_gate"
    store.close()
    store = _store(project)
    workflow = _workflow(project, store)
    resumed, generation = store.answer_operator_request(
        run_id=record.run_id,
        expected_generation=store.load_run(record.run_id)[1],
        request_id=waiting.operator_request.request_id,
        answer="approve",
        actor_id="operator",
    )
    resumed, generation = workflow.refresh_readiness(resumed, generation)

    assert resumed.state.value == "RUNNING"
    assert resumed.steps["prepare-fixture"].operator_gate_resolved
    assert resumed.steps["prepare-fixture"].state is StepStatus.READY


def test_underspecification_auto_mode_never_silently_approves_an_ask(project: FixtureProject) -> None:
    from helpers import config_values, write_config

    values = config_values(project)
    values["execution"]["underspec_mode"] = "auto"
    configured = replace(project, config=write_config(project, values))
    store, workflow, record, generation = _activate_ready_run(configured)

    with pytest.raises(SequentialWorkflowError, match="underspecification requests are denied"):
        workflow.prepare_from_supervisor(
            record.run_id,
            expected_generation=generation,
            supervisor_text=(
                '{"protocol_version":1,"action":"ask_operator","question":"Need a decision"}'
            ),
        )

    assert store.load_run(record.run_id)[0].state.value == "RUNNING"


def test_budget_usage_is_persisted_and_halts_after_an_over_limit_result(project: FixtureProject) -> None:
    from helpers import config_values, write_config

    values = config_values(project)
    values["budget"].update(
        {
            "enabled": True,
            "max_run_cost_usd": 0.001,
            "max_step_cost_usd": 0.001,
            "max_context_tokens": 100,
            "on_limit": "halt",
        }
    )
    configured = replace(project, config=write_config(project, values))
    _store_value, workflow, prepared = _prepare_executor(configured)

    record, _generation, forwarding = workflow.apply_executor_result(
        prepared,
        parse_executor_result(_executor_result(prepared)),
        usage={
            "cost_usd": 0.002,
            "tokens_total": 10,
            "tokens_input": 6,
            "tokens_output": 4,
            "tokens_reasoning": 0,
        },
    )

    assert record.state.value == "HALTED"
    assert record.usage.run.cost_usd == 0.002
    assert record.usage.by_step["prepare-fixture"].tokens_total == 10
    assert record.usage.by_role["terra"].tokens_input == 6
    assert record.usage.by_session["ses-executor"].tokens_output == 4
    assert json.loads(forwarding)["usage"]["cost_usd"] == 0.002


def test_enabled_budget_rejects_missing_measured_usage(project: FixtureProject) -> None:
    from helpers import config_values, write_config

    values = config_values(project)
    values["budget"]["enabled"] = True
    configured = replace(project, config=write_config(project, values))
    _store_value, workflow, prepared = _prepare_executor(configured)

    with pytest.raises(SequentialWorkflowError, match="measured OpenCode usage is required"):
        workflow.apply_executor_result(prepared, parse_executor_result(_executor_result(prepared)))


def test_budget_blocks_a_resumed_session_at_the_context_threshold(project: FixtureProject) -> None:
    from helpers import config_values, write_config

    values = config_values(project)
    values["budget"].update(
        {
            "enabled": True,
            "max_run_cost_usd": 1.0,
            "max_step_cost_usd": 1.0,
            "max_context_tokens": 10,
            "on_limit": "halt",
        }
    )
    configured = replace(project, config=write_config(project, values))
    store = _store(configured)
    record = _record(configured).model_copy(
        update={"usage": UsageLedger(by_session={"ses-executor": UsageAmount(tokens_total=10)})}
    )
    generation = store.create_run(record, sessions={"executors": {"terra": {"session_id": "ses-executor"}}})
    workflow = _workflow(configured, store)
    active, generation = workflow.activate(record.run_id, expected_generation=generation)

    with pytest.raises(SequentialWorkflowError, match="context token limit is exhausted"):
        workflow.prepare_from_supervisor(
            active.run_id,
            expected_generation=generation,
            supervisor_text=_dispatch_command(mode="resume"),
        )


def test_operator_can_waive_only_a_non_mandatory_compiled_review(project: FixtureProject) -> None:
    from helpers import config_values, write_config

    profiles = yaml.safe_load(project.profiles_path.read_text(encoding="utf-8"))
    profiles["profiles"]["economy"] = {
        "review_schedule": "always",
        "multi_review": "off",
        "reviewer_role_keys": ["reviewer"],
        "required_acceptances": 1,
    }
    project.profiles_path.write_text(yaml.safe_dump(profiles, sort_keys=False), encoding="utf-8")
    values = config_values(project)
    values["profile"]["profile_id"] = "economy"
    values["review_policy"]["allow_operator_waiver"] = True
    configured = replace(project, config=write_config(project, values))
    _store_value, workflow, executor = _prepare_executor(configured, max_reviewer_attempts=1)
    record, generation, _forwarding = workflow.apply_executor_result(
        executor,
        parse_executor_result(_executor_result(executor)),
    )
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=executor.dispatch.dispatch_id,
    )

    waiting = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=(
            '{"protocol_version":1,"action":"request_review_waiver",'
            '"step_id":"prepare-fixture","rationale":"operator accepts the evidence risk"}'
        ),
    )

    assert isinstance(waiting, RunRecord)
    assert waiting.operator_request is not None
    assert waiting.operator_request.kind == "review_waiver"
    resumed, _generation = _store_value.answer_operator_request(
        run_id=waiting.run_id,
        expected_generation=_store_value.load_run(waiting.run_id)[1],
        request_id=waiting.operator_request.request_id,
        answer="waive",
        actor_id="operator",
    )
    waived_review = resumed.steps["prepare-fixture"]

    assert resumed.state.value == "RUNNING"
    assert waived_review.state is StepStatus.ACCEPTED
    assert waived_review.review_waiver_decision_ref is not None
    assert waived_review.accepted_artifact_ids == ["fixture-evidence"]


def test_thorough_profile_requires_two_distinct_fresh_reviewer_acceptances(project: FixtureProject) -> None:
    from helpers import config_values, write_config

    profiles = yaml.safe_load(project.profiles_path.read_text(encoding="utf-8"))
    profiles["profiles"]["thorough"] = {
        "review_schedule": "always",
        "multi_review": "on_every_review",
        "reviewer_role_keys": ["reviewer", "reviewer-two"],
        "required_acceptances": 2,
    }
    project.profiles_path.write_text(yaml.safe_dump(profiles, sort_keys=False), encoding="utf-8")
    values = config_values(project)
    values["profile"]["profile_id"] = "thorough"
    configured = replace(project, config=write_config(project, values))
    store, workflow, executor = _prepare_executor(configured, review_required=True)
    record, generation, _forwarding = workflow.apply_executor_result(
        executor,
        parse_executor_result(_executor_result(executor)),
    )
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=executor.dispatch.dispatch_id,
    )

    first = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(role="reviewer"),
    )
    assert isinstance(first, PreparedDispatch)
    first = workflow.record_session_id(workflow.mark_running(first, process_id=1235), runtime_session_id="ses-one")
    first_result = parse_reviewer_result(
        {
            "result_version": 1,
            "dispatch_id": first.dispatch.dispatch_id,
            "attempt": first.dispatch.attempt,
            "step_id": "prepare-fixture",
            "repo_id": "fixture-repo",
            "review_target": first.review_target.model_dump(mode="json"),
            "findings": [],
            "verification": [{"check_id": "review-check", "status": "passed", "summary": "passed"}],
            "required_remediation": [],
            "summary": "first review",
            "verdict": "accepted",
        }
    )
    record, generation, _forwarding = workflow.apply_reviewer_result(first, first_result)
    assert record.steps["prepare-fixture"].state is StepStatus.REVIEW_REQUIRED
    assert record.steps["prepare-fixture"].accepted_reviewer_role_keys == ["reviewer"]
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=first.dispatch.dispatch_id,
    )

    second = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(role="reviewer-two"),
    )
    assert isinstance(second, PreparedDispatch)
    second = workflow.record_session_id(workflow.mark_running(second, process_id=1236), runtime_session_id="ses-two")
    second_result = first_result.model_copy(
        update={
            "dispatch_id": second.dispatch.dispatch_id,
            "attempt": second.dispatch.attempt,
            "review_target": second.review_target,
            "summary": "second review",
        }
    )
    record, _generation, _forwarding = workflow.apply_reviewer_result(second, second_result)

    assert record.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert record.steps["prepare-fixture"].review_acceptances == 2
    assert record.steps["prepare-fixture"].accepted_reviewer_role_keys == ["reviewer", "reviewer-two"]


def test_conflicting_reviews_require_a_fresh_tie_break_on_the_same_artifact(project: FixtureProject) -> None:
    from helpers import config_values, write_config

    profiles = yaml.safe_load(project.profiles_path.read_text(encoding="utf-8"))
    profiles["profiles"]["thorough"] = {
        "review_schedule": "always",
        "multi_review": "on_every_review",
        "reviewer_role_keys": ["reviewer", "reviewer-two", "reviewer-three"],
        "required_acceptances": 2,
    }
    project.profiles_path.write_text(yaml.safe_dump(profiles, sort_keys=False), encoding="utf-8")
    values = config_values(project)
    values["profile"]["profile_id"] = "thorough"
    values["roles"]["reviewers"]["reviewer-three"] = {
        **values["roles"]["reviewers"]["reviewer-two"],
        "model": "fixture/reviewer-three",
        "display": "Fixture Tie Break Reviewer",
    }
    values["execution"]["concurrency"]["role_capacities"]["reviewer-three"] = 1
    configured = replace(project, config=write_config(project, values))
    _store_value, workflow, executor = _prepare_executor(configured, max_reviewer_attempts=3)
    record, generation, _forwarding = workflow.apply_executor_result(
        executor,
        parse_executor_result(_executor_result(executor)),
    )
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=executor.dispatch.dispatch_id,
    )

    first = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(role="reviewer"),
    )
    assert isinstance(first, PreparedDispatch)
    first = workflow.record_session_id(workflow.mark_running(first, process_id=1235), runtime_session_id="ses-one")
    accepted = parse_reviewer_result(
        {
            "result_version": 1,
            "dispatch_id": first.dispatch.dispatch_id,
            "attempt": first.dispatch.attempt,
            "step_id": "prepare-fixture",
            "repo_id": "fixture-repo",
            "review_target": first.review_target.model_dump(mode="json"),
            "findings": [],
            "verification": [{"check_id": "review-check", "status": "passed", "summary": "passed"}],
            "required_remediation": [],
            "summary": "first review accepts",
            "verdict": "accepted",
        }
    )
    record, generation, _forwarding = workflow.apply_reviewer_result(first, accepted)
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=first.dispatch.dispatch_id,
    )

    second = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(role="reviewer-two"),
    )
    assert isinstance(second, PreparedDispatch)
    second = workflow.record_session_id(
        workflow.mark_running(second, process_id=1236),
        runtime_session_id="ses-two",
    )
    changes_requested = parse_reviewer_result(
        {
            "result_version": 1,
            "dispatch_id": second.dispatch.dispatch_id,
            "attempt": second.dispatch.attempt,
            "step_id": "prepare-fixture",
            "repo_id": "fixture-repo",
            "review_target": second.review_target.model_dump(mode="json"),
            "findings": [],
            "verification": [{"check_id": "review-check", "status": "passed", "summary": "passed"}],
            "required_remediation": ["resolve tie"],
            "summary": "second review disagrees",
            "verdict": "changes_requested",
        }
    )
    record, generation, _forwarding = workflow.apply_reviewer_result(second, changes_requested)
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=second.dispatch.dispatch_id,
    )

    tie_break = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(role="reviewer-three"),
    )

    assert isinstance(tie_break, PreparedDispatch)
    assert record.steps["prepare-fixture"].state is StepStatus.REVIEW_REQUIRED
    assert tie_break.review_target == first.review_target == second.review_target


def test_review_escalation_waits_for_reassignment_without_resetting_attempts(project: FixtureProject) -> None:
    values = valid_plan_values(project)
    values["steps"][0]["review"] = {
        "required": True,
        "reviewer_role_keys": ["reviewer"],
        "required_acceptances": 1,
    }
    values["steps"][0]["retry"] = {
        "max_executor_attempts": 2,
        "max_reviewer_attempts": 1,
        "on_failed": "halt",
        "on_blocked": "halt",
        "on_changes_requested": "escalate",
        "escalation_role_key": "terra",
    }
    plan = NormalizedPlan.model_validate(values)
    record = new_run_record(
        run_id="escalation-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-escalation"),
        event=_event(1),
    )
    store = _store(project)
    generation = store.create_run(record)
    workflow = _workflow(project, store)
    active, generation = workflow.activate(record.run_id, expected_generation=generation)
    executor = workflow.prepare_from_supervisor(
        active.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(),
    )
    assert isinstance(executor, PreparedDispatch)
    executor = workflow.record_session_id(
        workflow.mark_running(executor, process_id=1234),
        runtime_session_id="ses-executor",
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
    reviewer = workflow.record_session_id(
        workflow.mark_running(reviewer, process_id=1235),
        runtime_session_id="ses-reviewer",
    )
    changes = parse_reviewer_result(
        {
            "result_version": 1,
            "dispatch_id": reviewer.dispatch.dispatch_id,
            "attempt": reviewer.dispatch.attempt,
            "step_id": "prepare-fixture",
            "repo_id": "fixture-repo",
            "review_target": reviewer.review_target.model_dump(mode="json"),
            "findings": [],
            "verification": [{"check_id": "review-check", "status": "passed", "summary": "passed"}],
            "required_remediation": ["reassign executor"],
            "summary": "review requires escalation",
            "verdict": "changes_requested",
        }
    )
    waiting, generation, _forwarding = workflow.apply_reviewer_result(reviewer, changes)

    assert waiting.state.value == "WAITING_OPERATOR"
    assert waiting.steps["prepare-fixture"].state is StepStatus.BLOCKED
    assert waiting.steps["prepare-fixture"].rework_rounds == 1
    assert waiting.operator_request is not None
    waiting, generation = workflow.acknowledge_forwarding(
        waiting.run_id,
        expected_generation=generation,
        dispatch_id=reviewer.dispatch.dispatch_id,
    )
    resumed, generation = store.answer_operator_request(
        run_id=waiting.run_id,
        expected_generation=generation,
        request_id=waiting.operator_request.request_id,
        answer="reassign",
        actor_id="operator",
    )
    assert resumed.steps["prepare-fixture"].reassignment_role_key == "terra"
    reassigned = workflow.prepare_from_supervisor(
        resumed.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(mode="resume"),
    )

    assert isinstance(reassigned, PreparedDispatch)
    assert reassigned.dispatch.attempt == 2


def test_attempt_exhaustion_is_step_local(project: FixtureProject) -> None:
    values = valid_plan_values(project)
    second = json.loads(json.dumps(values["steps"][0]))
    second.update(
        {
            "ordinal": 2,
            "step_id": "second-fixture",
            "title": "Second fixture",
            "produced_outputs": [
                {
                    "artifact_id": "second-output",
                    "producer_step_id": None,
                    "description": "Second output",
                }
            ],
            "resource_locks": [{"resource_id": "second-resource", "mode": "write"}],
            "evidence_requirements": [
                {
                    "artifact_id": "second-evidence",
                    "relative_path": "second.md",
                    "media_type": "text/markdown",
                }
            ],
        }
    )
    values["steps"].append(second)
    plan = NormalizedPlan.model_validate(values)
    event = _event(1)
    record = new_run_record(
        run_id="two-step-run",
        project_id=project.config.project_id,
        config_digest=project.config.config_digest,
        plan=plan,
        plan_approval=approve_plan(plan, "decision-two-step-plan"),
        event=event,
    )
    first = transition_step(record.steps["prepare-fixture"], StepStatus.READY, _event(2))
    first = first.model_copy(update={"executor_attempts": 1})
    second_record = transition_step(record.steps["second-fixture"], StepStatus.READY, _event(3))
    record = record.model_copy(
        update={
            "steps": {
                "prepare-fixture": first,
                "second-fixture": second_record,
            },
            "sequence": 3,
            "updated_at": second_record.last_event.occurred_at,
        }
    )
    store = _store(project)
    generation = store.create_run(record)
    workflow = _workflow(project, store)
    active, generation = workflow.activate(record.run_id, expected_generation=generation)
    exhausted = json.loads(_dispatch_command())
    second_command = dict(exhausted)
    second_command["step_id"] = "second-fixture"

    with pytest.raises(SequentialWorkflowError, match="exhausted executor attempts"):
        workflow.prepare_from_supervisor(
            active.run_id,
            expected_generation=generation,
            supervisor_text=json.dumps(exhausted),
        )
    prepared = workflow.prepare_from_supervisor(
        active.run_id,
        expected_generation=generation,
        supervisor_text=json.dumps(second_command),
    )

    assert isinstance(prepared, PreparedDispatch)
    assert prepared.dispatch.step_id == "second-fixture"


def test_supervisor_completion_request_cannot_bypass_unmet_obligations(project: FixtureProject) -> None:
    store, workflow, record, generation = _activate_ready_run(project)

    decision = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text='{"protocol_version":1,"action":"request_completion","rationale":"premature"}',
    )

    assert isinstance(decision, CompletionDecision)
    assert not decision.accepted
    assert any("step_not_accepted" in obligation for obligation in decision.obligations)
    assert store.load_run(record.run_id)[0].state.value == "RUNNING"


def test_failed_executor_result_never_advances_the_step(project: FixtureProject) -> None:
    _store_value, workflow, prepared = _prepare_executor(project)

    record, _generation, _forwarding = workflow.apply_executor_result(
        prepared,
        parse_executor_result(_executor_result(prepared, "failed")),
    )

    assert record.steps["prepare-fixture"].state is StepStatus.FAILED


def test_reviewer_acceptance_is_fresh_policy_bound_and_revision_bound(project: FixtureProject) -> None:
    _store_value, workflow, executor = _prepare_executor(project, review_required=True)
    record, generation, _forwarding = workflow.apply_executor_result(
        executor,
        parse_executor_result(_executor_result(executor)),
    )
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=executor.dispatch.dispatch_id,
    )
    prepared = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(role="reviewer"),
    )
    assert isinstance(prepared, PreparedDispatch)
    assert prepared.review_target is not None
    assert prepared.permission_config["permission"]["edit"] == "deny"
    running = workflow.mark_running(prepared, process_id=1235)
    reviewer = workflow.record_session_id(running, runtime_session_id="ses-reviewer")
    review_result = parse_reviewer_result(
        {
            "result_version": 1,
            "dispatch_id": reviewer.dispatch.dispatch_id,
            "attempt": reviewer.dispatch.attempt,
            "step_id": "prepare-fixture",
            "repo_id": "fixture-repo",
            "review_target": reviewer.review_target.model_dump(mode="json"),
            "findings": [],
            "verification": [{"check_id": "review-check", "status": "passed", "summary": "passed"}],
            "required_remediation": [],
            "summary": "fixture review",
            "verdict": "accepted",
        }
    )

    record, _generation, _forwarding = workflow.apply_reviewer_result(reviewer, review_result)

    assert record.steps["prepare-fixture"].state is StepStatus.ACCEPTED
    assert record.steps["prepare-fixture"].review_acceptances == 1


def test_reviewer_changes_requested_returns_a_deterministic_rework_state(project: FixtureProject) -> None:
    _store_value, workflow, executor = _prepare_executor(project, review_required=True)
    record, generation, _forwarding = workflow.apply_executor_result(
        executor,
        parse_executor_result(_executor_result(executor)),
    )
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=executor.dispatch.dispatch_id,
    )
    prepared = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(role="reviewer"),
    )
    assert isinstance(prepared, PreparedDispatch)
    running = workflow.mark_running(prepared, process_id=1235)
    reviewer = workflow.record_session_id(running, runtime_session_id="ses-reviewer")
    review_result = parse_reviewer_result(
        {
            "result_version": 1,
            "dispatch_id": reviewer.dispatch.dispatch_id,
            "attempt": reviewer.dispatch.attempt,
            "step_id": "prepare-fixture",
            "repo_id": "fixture-repo",
            "review_target": reviewer.review_target.model_dump(mode="json"),
            "findings": [],
            "verification": [{"check_id": "review-check", "status": "passed", "summary": "passed"}],
            "required_remediation": ["repair fixture"],
            "summary": "fixture review",
            "verdict": "changes_requested",
        }
    )

    record, generation, _forwarding = workflow.apply_reviewer_result(reviewer, review_result)

    assert record.steps["prepare-fixture"].state is StepStatus.READY
    record, generation = workflow.acknowledge_forwarding(
        record.run_id,
        expected_generation=generation,
        dispatch_id=reviewer.dispatch.dispatch_id,
    )
    rework = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(mode="resume"),
    )
    assert isinstance(rework, PreparedDispatch)
    rework = workflow.record_session_id(
        workflow.mark_running(rework, process_id=1236),
        runtime_session_id="ses-executor-rework",
    )
    record, _generation, _forwarding = workflow.apply_executor_result(
        rework,
        parse_executor_result(_executor_result(rework)),
    )

    assert record.steps["prepare-fixture"].state is StepStatus.REVIEW_REQUIRED
    assert record.steps["prepare-fixture"].review_acceptances == 0
    assert record.steps["prepare-fixture"].accepted_reviewer_role_keys == []
