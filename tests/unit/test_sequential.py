from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from helpers import (
    FixtureProject,
    config_values,
    create_fixture_project,
    valid_plan_values,
    write_config,
)

from dispatcher.permissions import (
    READ_ONLY_DIAGNOSTIC_COMMANDS,
    READ_ONLY_NATIVE_TOOLS,
    read_only_diagnostic_bash_rules,
)
from dispatcher.plan import NormalizedPlan, approve_plan
from dispatcher.protocol import parse_supervisor_command
from dispatcher.repository import EvidenceManifestEntry, RepositoryChange, RepositorySnapshot
from dispatcher.results import (
    EXECUTOR_OUTCOME_OPTIONS,
    EXECUTOR_PROPOSAL_OUTCOME_OPTIONS,
    REVIEWER_VERDICT_OPTIONS,
    ExecutorResult,
    ReviewerResult,
    parse_executor_proposal,
    parse_executor_result,
    parse_reviewer_result,
)
from dispatcher.schema_export import schema_documents
from dispatcher.sequential import (
    CompletionDecision,
    PreparedBatch,
    PreparedDispatch,
    SequentialWorkflow,
    SequentialWorkflowError,
    _validate_result_verification,
)
from dispatcher.state_store import DispatchPayload, StateStore
from dispatcher.verification import AuthoritativeVerification
from dispatcher.workflow import (
    DispatchStatus,
    OperatorRequest,
    RunRecord,
    RunStatus,
    StepStatus,
    TransitionEvent,
    UsageAmount,
    UsageLedger,
    new_run_record,
    transition_run,
    transition_step,
)

_EVIDENCE_SHA = "a" * 64
_ALL_ACTIONS = (
    "inspect",
    "modify",
    "verify",
    "commit",
    "push",
    "force_push",
    "create_branch",
)


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
    criterion_ids: tuple[str, ...] = ("fixture-check",),
    authorized_actions: tuple[str, ...] = ("inspect",),
    sources: list[dict[str, str]] | None = None,
) -> RunRecord:
    values = valid_plan_values(project)
    if sources is not None:
        values["sources"] = sources
    values["steps"][0]["authorization"]["authorized_actions"] = list(authorized_actions)
    values["steps"][0]["authorization"]["writable_paths"] = (
        ["evidence/"] if "modify" in authorized_actions else []
    )
    values["steps"][0]["acceptance_criteria"] = [
        {
            "criterion_id": criterion_id,
            "description": f"Verify {criterion_id}.",
            "check": {
                "argv": ["python", "-c", f"print('{criterion_id}')"],
                "working_directory": "repository",
                "timeout_seconds": 30,
                "max_output_bytes": 65536,
                "expected_exit_codes": [0],
                "network_policy": "deny",
            },
        }
        for criterion_id in criterion_ids
    ]
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
        ignored=(),
        dirty_patch_sha256="a" * 64,
        git_metadata_sha256="d" * 64,
        git_refs_sha256="e" * 64,
        manifest_sha256="c" * 64,
    )


def _dirty_snapshot(clean: RepositorySnapshot, *, marker: str) -> RepositorySnapshot:
    return clean.model_copy(
        update={
            "clean": False,
            "changes": (
                RepositoryChange(
                    change_type="modified",
                    paths=("evidence/fixture.md",),
                    index_status=" ",
                    worktree_status="M",
                ),
            ),
            "dirty_patch_sha256": marker * 64,
            "manifest_sha256": marker * 64,
        }
    )


def _failed_authoritative_verification(
    *,
    summary: str = "fixture assertion failed",
    transcript_marker: str = "c",
) -> tuple[AuthoritativeVerification, ...]:
    return (
        AuthoritativeVerification(
            check_id="fixture-check",
            status="failed",
            argv=("python", "-c", "raise SystemExit(1)"),
            exit_code=1,
            timed_out=False,
            output_truncated=False,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
            transcript_sha256=transcript_marker * 64,
            duration_ms=5,
            backend="fixture-isolation",
            summary=summary,
        ),
    )


def _passed_authoritative_verification() -> tuple[AuthoritativeVerification, ...]:
    failed = _failed_authoritative_verification()[0]
    return (
        failed.model_copy(
            update={
                "status": "passed",
                "exit_code": 0,
                "summary": "fixture assertion passed",
            }
        ),
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


def _executor_result(
    prepared: PreparedDispatch,
    outcome: str = "completed",
    *,
    verification: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if verification is None:
        verification = [
            {"check_id": item["criterion_id"], "status": "passed", "summary": "passed"}
            for item in json.loads(prepared.prompt)["acceptance_criteria"]
        ]
    result: dict[str, Any] = {
        "result_version": 1,
        "response_contract": "dispatcher.executor_result.v1",
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
        "verification": verification,
        "summary": "fixture executor result",
        "outcome": outcome,
    }
    if outcome == "failed":
        result["failure_code"] = "fixture-failure"
    if outcome == "blocked":
        result["blockers"] = ["fixture blocker"]
    return result


def _reviewer_result(
    prepared: PreparedDispatch,
    verdict: str = "accepted",
    *,
    verification: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if verification is None:
        verification = [
            {"check_id": item["criterion_id"], "status": "passed", "summary": "passed"}
            for item in json.loads(prepared.prompt)["acceptance_criteria"]
        ]
    result: dict[str, Any] = {
        "result_version": 1,
        "response_contract": "dispatcher.reviewer_result.v1",
        "dispatch_id": prepared.dispatch.dispatch_id,
        "attempt": prepared.dispatch.attempt,
        "step_id": prepared.dispatch.step_id,
        "repo_id": prepared.dispatch.intent.repository.repo_id,
        "review_target": prepared.review_target.model_dump(mode="json"),
        "findings": [],
        "verification": verification,
        "required_remediation": [],
        "summary": f"fixture review {verdict}",
        "verdict": verdict,
    }
    if verdict == "changes_requested":
        result["required_remediation"] = ["repair fixture"]
    elif verdict == "blocked":
        result["blockers"] = ["fixture review blocker"]
    elif verdict == "inconclusive":
        result["reason"] = "fixture evidence is inconclusive"
    return result


def _activate_ready_run(
    project: FixtureProject,
    *,
    review_required: bool = False,
    max_reviewer_attempts: int | None = None,
    max_executor_attempts: int | None = None,
    criterion_ids: tuple[str, ...] = ("fixture-check",),
    authorized_actions: tuple[str, ...] = ("inspect",),
    sources: list[dict[str, str]] | None = None,
) -> tuple[StateStore, SequentialWorkflow, RunRecord, int]:
    store = _store(project)
    record = _record(
        project,
        review_required=review_required,
        max_reviewer_attempts=max_reviewer_attempts,
        max_executor_attempts=max_executor_attempts,
        criterion_ids=criterion_ids,
        authorized_actions=authorized_actions,
        sources=sources,
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
    criterion_ids: tuple[str, ...] = ("fixture-check",),
    authorized_actions: tuple[str, ...] = ("inspect",),
    sources: list[dict[str, str]] | None = None,
) -> tuple[StateStore, SequentialWorkflow, PreparedDispatch]:
    _store_value, workflow, record, generation = _activate_ready_run(
        project,
        review_required=review_required,
        max_reviewer_attempts=max_reviewer_attempts,
        max_executor_attempts=max_executor_attempts,
        criterion_ids=criterion_ids,
        authorized_actions=authorized_actions,
        sources=sources,
    )
    prepared = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(),
    )
    assert isinstance(prepared, PreparedDispatch)
    running = workflow.mark_running(prepared, process_id=1234, process_create_time=1234.0)
    identified = workflow.record_session_id(running, runtime_session_id="ses-executor")
    return _store_value, workflow, identified


def _worker_contexts(
    project: FixtureProject,
    *,
    criterion_ids: tuple[str, ...] = ("fixture-check",),
    sources: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _store_value, workflow, executor = _prepare_executor(
        project,
        review_required=True,
        criterion_ids=criterion_ids,
        sources=sources,
    )
    executor_context = json.loads(executor.prompt)
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
    return executor_context, json.loads(reviewer.prompt)


def _prepare_reviewer(
    project: FixtureProject,
    *,
    max_reviewer_attempts: int | None = None,
    max_executor_attempts: int | None = None,
) -> tuple[StateStore, SequentialWorkflow, PreparedDispatch]:
    store, workflow, executor = _prepare_executor(
        project,
        review_required=True,
        max_reviewer_attempts=max_reviewer_attempts,
        max_executor_attempts=max_executor_attempts,
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
    prepared = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(role="reviewer"),
    )
    assert isinstance(prepared, PreparedDispatch)
    reviewer = workflow.record_session_id(
        workflow.mark_running(prepared, process_id=1235, process_create_time=1235.0),
        runtime_session_id="ses-reviewer",
    )
    return store, workflow, reviewer


def _prepare_verification_feedback_executor(
    project: FixtureProject,
    *,
    max_executor_attempts: int,
    authorized_actions: tuple[str, ...] = ("inspect", "modify", "commit"),
) -> tuple[
    StateStore,
    SequentialWorkflow,
    PreparedDispatch,
    RepositorySnapshot,
    dict[str, RepositorySnapshot],
]:
    store = _store(project)
    record = _record(
        project,
        review_required=True,
        max_executor_attempts=max_executor_attempts,
        authorized_actions=authorized_actions,
    )
    generation = store.create_run(record)
    clean = _repository_snapshot()
    current = {"snapshot": _dirty_snapshot(clean, marker="f")}

    def inspect(_config, _repo_id, *, require_clean):
        return clean if require_clean else current["snapshot"]

    workflow = SequentialWorkflow(
        project.config,
        store,
        owner_id="verification-feedback-fixture-owner",
        repository_inspector=inspect,
    )
    active, generation = workflow.activate(record.run_id, expected_generation=generation)
    prepared = workflow.prepare_from_supervisor(
        active.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(),
    )
    assert isinstance(prepared, PreparedDispatch)
    prepared = workflow.record_session_id(
        workflow.mark_running(prepared, process_id=1234, process_create_time=1234.0),
        runtime_session_id="ses-verification-feedback",
    )
    return store, workflow, prepared, clean, current


def _role_scope_project(tmp_path: Path, *, scheduling: str) -> FixtureProject:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["execution"]["scheduling"] = scheduling
    values["permission_policies"]["policies"]["repository"]["actions"] = {
        action: "allow" for action in _ALL_ACTIONS
    }
    values["permission_policies"]["policies"]["executor-class"]["actions"] = {
        action: (
            "deny" if action in {"push", "force_push", "create_branch"} else "allow"
        )
        for action in _ALL_ACTIONS
    }
    values["permission_policies"]["policies"]["reviewer-class"]["actions"] = {
        action: "allow" for action in _ALL_ACTIONS
    }
    values["permission_policies"]["policies"]["reviewer"]["actions"] = {
        action: "allow" for action in _ALL_ACTIONS
    }
    return replace(project, config=write_config(project, values))


def _review_ready(
    project: FixtureProject,
) -> tuple[StateStore, SequentialWorkflow, PreparedDispatch, RunRecord, int]:
    store, workflow, executor = _prepare_executor(
        project,
        review_required=True,
        authorized_actions=_ALL_ACTIONS,
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
    return store, workflow, executor, record, generation


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
    dispatch = parse_supervisor_command(examples[0])
    assert dispatch.action == "dispatch"
    assert "dispatcher.executor_proposal.v2" in dispatch.prompt
    assert "schema-v1 executor result" not in dispatch.prompt
    assert "## Observation Capabilities" in bootstrap
    assert all(f"`{tool}`" in bootstrap for tool in READ_ONLY_NATIVE_TOOLS)
    assert all(f"`{command}`" in bootstrap for command in READ_ONLY_DIAGNOSTIC_COMMANDS)
    assert "MCP tools: none" in bootstrap
    assert "Do not create, edit, stage, commit, delete" in bootstrap
    assert "Reply with exactly one schema-v1 JSON command object" in bootstrap
    assert "name the exact bounded" in bootstrap
    assert "dispatcher adds the approved normative source ledger" in bootstrap


def test_worker_prompts_bind_ordered_authoritative_sources_without_task_mentions(
    project: FixtureProject,
) -> None:
    sources = [
        {
            "source_id": "fixture-specification-source",
            "root": "specifications",
            "relative_path": "specification.md",
            "sha256": hashlib.sha256(
                (project.specifications / "specification.md").read_bytes()
            ).hexdigest(),
            "media_type": "text/markdown",
        },
        {
            "source_id": "fixture-plan-source",
            "root": "plans",
            "relative_path": "plan.md",
            "sha256": hashlib.sha256((project.plans / "plan.md").read_bytes()).hexdigest(),
            "media_type": "text/markdown",
        },
    ]
    executor_context, reviewer_context = _worker_contexts(project, sources=sources)
    source_roots = {
        "plans": Path(project.config.model.sources.plans_dir),
        "specifications": Path(project.config.model.sources.specifications_dir),
    }
    expected_ledger = [
        {
            "source_id": source["source_id"],
            "root": source["root"],
            "relative_path": source["relative_path"],
            "sha256": source["sha256"],
            "path": str((source_roots[source["root"]] / source["relative_path"]).resolve()),
        }
        for source in sources
    ]

    for context in (executor_context, reviewer_context):
        assert context["task"] == "Perform the approved fixture work."
        assert "plan.md" not in context["task"]
        assert "specification.md" not in context["task"]
        assert context["authoritative_sources"] == expected_ledger
        assert "exact listed path entries as normative" in context["authoritative_sources_rule"]
        assert "must not substitute similarly named files" in context["authoritative_sources_rule"]
        assert "dispatcher source verification is authoritative" in context[
            "authoritative_sources_rule"
        ]


@pytest.mark.parametrize("batch", [False, True], ids=["ordinary", "batch"])
def test_dispatch_preparation_rechecks_immutable_sources_before_persisting(
    tmp_path: Path,
    batch: bool,
) -> None:
    project = (
        _role_scope_project(tmp_path, scheduling="bounded_parallel")
        if batch
        else create_fixture_project(tmp_path)
    )
    store = _store(project)
    record = _record(project)
    generation = store.create_run(record)
    workflow = _workflow(project, store)
    active, generation = workflow.activate(record.run_id, expected_generation=generation)
    workflow.render_bootstrap(active.run_id)

    (project.plans / "plan.md").unlink()
    supervisor_text = (
        json.dumps(
            {
                "protocol_version": 2,
                "action": "dispatch_batch",
                "children": [
                    {
                        "step_id": "prepare-fixture",
                        "target_role": "terra",
                        "session_mode": "new",
                        "prompt": "Perform the approved fixture work.",
                    }
                ],
            }
        )
        if batch
        else _dispatch_command()
    )

    with pytest.raises(SequentialWorkflowError, match="plan source does not exist"):
        workflow.prepare_from_supervisor(
            active.run_id,
            expected_generation=generation,
            supervisor_text=supervisor_text,
        )

    persisted, _generation = store.load_run(active.run_id)
    assert persisted.dispatches == {}
    assert store.opencode_invocations_for_run(active.run_id) == ()


def test_worker_prompt_lists_exact_schema_discriminator_options(project: FixtureProject) -> None:
    executor_context, reviewer_context = _worker_contexts(project)

    assert executor_context["outcome_options"] == ["completed", "blocked", "failed"]
    assert "verdict_options" not in executor_context
    assert "outcome field MUST be exactly one of: completed, blocked, failed" in executor_context[
        "final_response_check"
    ]
    assert "no other word, synonym, or variation is acceptable" in executor_context["final_response_check"]

    assert reviewer_context["verdict_options"] == [
        "accepted",
        "changes_requested",
        "blocked",
        "inconclusive",
    ]
    assert "outcome_options" not in reviewer_context
    assert (
        "verdict field MUST be exactly one of: accepted, changes_requested, blocked, inconclusive"
        in reviewer_context["final_response_check"]
    )
    assert "no other word, synonym, or variation is acceptable" in reviewer_context["final_response_check"]


def test_worker_prompt_embeds_authoritative_result_schemas(project: FixtureProject) -> None:
    executor_context, reviewer_context = _worker_contexts(project)
    documents = schema_documents()

    assert executor_context["response_json_schema"] == documents["executor-proposal-v2.json"]
    assert reviewer_context["response_json_schema"] == documents["reviewer-result-v1.json"]
    for context in (executor_context, reviewer_context):
        assert "Conform to response_json_schema exactly" in context["final_response_check"]
        assert "no extra fields, no missing required fields" in context["final_response_check"]
        assert "no values outside any defined enum" in context["final_response_check"]


def test_worker_prompt_uses_exact_plan_criterion_ids_for_every_example(
    project: FixtureProject,
) -> None:
    criterion_ids = ("content-check", "repository-check")
    executor_context, reviewer_context = _worker_contexts(
        project,
        criterion_ids=criterion_ids,
    )

    assert executor_context["required_verification_check_ids"] == list(criterion_ids)
    assert [
        item["check_id"]
        for item in executor_context["response_template"]["criterion_self_reports"]
    ] == list(criterion_ids)
    assert {
        item["status"]
        for item in executor_context["response_template"]["criterion_self_reports"]
    } == {"not_run"}
    assert "exactly one not_run entry" in executor_context["verification_contract"]
    assert "every status must be not_run" in executor_context["final_response_check"]
    assert executor_context["response_json_schema"] == schema_documents()[
        "executor-proposal-v2.json"
    ]

    assert reviewer_context["required_verification_check_ids"] == list(criterion_ids)
    assert [
        item["check_id"] for item in reviewer_context["response_template"]["verification"]
    ] == list(criterion_ids)
    assert {item["status"] for item in reviewer_context["response_template"]["verification"]} == {
        "passed"
    }
    assert reviewer_context["response_json_schema"] == schema_documents()[
        "reviewer-result-v1.json"
    ]


def test_reviewer_prompt_is_inspect_only_and_directs_remediation_to_executor(
    project: FixtureProject,
) -> None:
    _executor_context, reviewer_context = _worker_contexts(project)

    assert reviewer_context["authorized_actions"] == ["inspect"]
    assert reviewer_context["role_instruction"] == (
        "You are a reviewer. Use native read, glob, and grep to inspect file contents and locate "
        "files. Use exact diagnostic shell commands only for current directory, branch, revision, "
        "status, and diff metadata. Do not add shell arguments, redirection, chaining, pipes, "
        "substitutions, or other shell syntax. Do not run tests. Do not create, edit, stage, commit, "
        "delete, or otherwise modify files or Git state. If remediation is required, describe it "
        "in required_remediation for the executor; do not perform it."
    )
    assert reviewer_context["observation_tools"] == {
        "native": list(READ_ONLY_NATIVE_TOOLS),
        "diagnostic_commands": list(READ_ONLY_DIAGNOSTIC_COMMANDS),
        "mcp": [],
    }
    assert not any(
        runner in command
        for command in reviewer_context["observation_tools"]["diagnostic_commands"]
        for runner in ("pytest", "python", "ruff", "mypy", "git commit", "git add")
    )
    assert "diagnostic_commands" not in reviewer_context
    assert not set(reviewer_context["authorized_actions"]) & {
        "modify",
        "verify",
        "commit",
        "push",
        "force_push",
        "create_branch",
    }
    assert reviewer_context["response_contract"] == "dispatcher.reviewer_result.v1"
    assert reviewer_context["required_verification_check_ids"] == ["fixture-check"]
    assert reviewer_context["review_target"]["executor_dispatch_id"]
    assert reviewer_context["evidence_requirements"]


def test_single_and_batch_dispatches_apply_identical_reviewer_role_ceiling(
    tmp_path: Path,
) -> None:
    single_project = _role_scope_project(tmp_path / "single", scheduling="sequential")
    single_store, single_workflow, single_executor, single_record, single_generation = (
        _review_ready(single_project)
    )
    single_reviewer = single_workflow.prepare_from_supervisor(
        single_record.run_id,
        expected_generation=single_generation,
        supervisor_text=_dispatch_command(role="reviewer"),
    )
    assert isinstance(single_reviewer, PreparedDispatch)

    batch_project = _role_scope_project(tmp_path / "batch", scheduling="bounded_parallel")
    batch_store, batch_workflow, batch_executor, batch_record, batch_generation = _review_ready(
        batch_project
    )
    batch = batch_workflow.prepare_from_supervisor(
        batch_record.run_id,
        expected_generation=batch_generation,
        supervisor_text=json.dumps(
            {
                "protocol_version": 2,
                "action": "dispatch_batch",
                "children": [
                    {
                        "step_id": "prepare-fixture",
                        "target_role": "reviewer",
                        "session_mode": "new",
                        "prompt": "Review the exact executor result.",
                    }
                ],
            }
        ),
    )
    assert isinstance(batch, PreparedBatch)
    batch_reviewer = batch.dispatches[0]

    def expected_sources(fixture: FixtureProject) -> list[dict[str, str]]:
        roots = {
            "plans": Path(fixture.config.model.sources.plans_dir),
            "specifications": Path(fixture.config.model.sources.specifications_dir),
        }
        return [
            {
                "source_id": source["source_id"],
                "root": source["root"],
                "relative_path": source["relative_path"],
                "sha256": source["sha256"],
                "path": str((roots[source["root"]] / source["relative_path"]).resolve()),
            }
            for source in valid_plan_values(fixture)["sources"]
        ]

    single_ledger = expected_sources(single_project)
    batch_ledger = expected_sources(batch_project)

    for executor, ledger in ((single_executor, single_ledger), (batch_executor, batch_ledger)):
        executor_context = json.loads(executor.prompt)
        executor_permission = executor.permission_config["permission"]
        assert executor_context["authoritative_sources"] == ledger
        assert executor_context["authorized_actions"] == [
            action for action in _ALL_ACTIONS if action in {"inspect", "modify"}
        ]
        assert "Do not run acceptance tests" in executor_context["role_instruction"]
        assert executor_context["writable_paths"] == ["evidence/"]
        assert "evidence_diagnostic_commands" not in executor_context
        assert "diagnostic_commands" not in executor_context
        assert "observation_tools" not in executor_context
        assert executor_permission["edit"] == "allow"
        assert executor_permission["write"] == "allow"
        assert executor_permission["bash"]["pytest *"] == "deny"
        assert executor_permission["bash"]["git commit *"] == "deny"
        assert executor_permission["bash"]["git push *"] == "deny"
        assert executor_permission["bash"]["git push --force *"] == "deny"
        assert executor_permission["bash"]["git branch *"] == "deny"

    assert single_reviewer.permission_config == batch_reviewer.permission_config
    assert single_reviewer.permission_config["permission"]["bash"] == (
        read_only_diagnostic_bash_rules()
    )
    for store, reviewer, ledger in (
        (single_store, single_reviewer, single_ledger),
        (batch_store, batch_reviewer, batch_ledger),
    ):
        context = json.loads(reviewer.prompt)
        persisted = store.load_dispatch_payload(reviewer.run_id, reviewer.dispatch.dispatch_id)
        assert context["authoritative_sources"] == ledger
        assert context["authorized_actions"] == ["inspect"]
        assert context["observation_tools"] == {
            "native": list(READ_ONLY_NATIVE_TOOLS),
            "diagnostic_commands": list(READ_ONLY_DIAGNOSTIC_COMMANDS),
            "mcp": [],
        }
        assert "Do not create, edit, stage, commit" in context["role_instruction"]
        assert "Do not add shell arguments, redirection, chaining, pipes" in context[
            "role_instruction"
        ]
        assert persisted.prompt == reviewer.prompt
        assert persisted.policy == reviewer.permission_config
        assert persisted.policy["permission"]["bash"] == read_only_diagnostic_bash_rules()


def test_reviewer_dispatch_without_step_inspect_fails_before_persistence(
    project: FixtureProject,
) -> None:
    store, workflow, executor = _prepare_executor(
        project,
        review_required=True,
        authorized_actions=("modify",),
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

    with pytest.raises(
        ValueError,
        match="reviewer dispatch requires step authorization to include inspect",
    ):
        workflow.prepare_from_supervisor(
            record.run_id,
            expected_generation=generation,
            supervisor_text=_dispatch_command(role="reviewer"),
        )

    persisted, _generation = store.load_run(record.run_id)
    assert len(persisted.dispatches) == 1
    assert all(dispatch.role_kind == "executor" for dispatch in persisted.dispatches.values())


def test_worker_attention_examples_are_valid_result_instances(project: FixtureProject) -> None:
    executor_context, reviewer_context = _worker_contexts(project)

    executor_example = parse_executor_proposal(
        executor_context["response_requires_attention_template"]
    )
    reviewer_example = parse_reviewer_result(
        reviewer_context["response_requires_attention_template"]
    )

    assert executor_example.outcome == "blocked"
    assert executor_example.blockers == ["A concrete blocker prevents completion."]
    assert reviewer_example.verdict == "changes_requested"
    assert reviewer_example.findings[0].model_dump() == {
        "finding_id": "required-change",
        "severity": "blocking",
        "summary": "The reviewed result requires a concrete correction.",
    }


def test_failed_authoritative_verification_resumes_same_executor_with_feedback(
    project: FixtureProject,
) -> None:
    store = _store(project)
    record = _record(
        project,
        review_required=True,
        max_executor_attempts=2,
        authorized_actions=("inspect", "modify", "commit"),
    )
    generation = store.create_run(record)
    clean = _repository_snapshot()
    dirty = clean.model_copy(
        update={
            "clean": False,
            "changes": (
                RepositoryChange(
                    change_type="modified",
                    paths=("evidence/fixture.md",),
                    index_status=" ",
                    worktree_status="M",
                ),
            ),
            "dirty_patch_sha256": "f" * 64,
            "manifest_sha256": "1" * 64,
        }
    )

    def inspect(_config, _repo_id, *, require_clean):
        return clean if require_clean else dirty

    workflow = SequentialWorkflow(
        project.config,
        store,
        owner_id="verification-feedback-owner",
        repository_inspector=inspect,
    )
    active, generation = workflow.activate(record.run_id, expected_generation=generation)
    prepared = workflow.prepare_from_supervisor(
        active.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(),
    )
    assert isinstance(prepared, PreparedDispatch)
    prepared = workflow.mark_running(prepared, process_id=1234, process_create_time=1234.0)
    prepared = workflow.record_session_id(prepared, runtime_session_id="ses-verification-feedback")
    proposal = parse_executor_proposal(json.loads(prepared.prompt)["response_template"])
    assert proposal.outcome == "completed"
    proposal_snapshot = workflow.record_executor_proposal(prepared, proposal)
    verification = (
        AuthoritativeVerification(
            check_id="fixture-check",
            status="failed",
            argv=("python", "-c", "raise SystemExit(1)"),
            exit_code=1,
            timed_out=False,
            output_truncated=False,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
            transcript_sha256="c" * 64,
            duration_ms=5,
            backend="fixture-isolation",
            summary="fixture assertion failed",
        ),
    )

    failed, generation = workflow.record_executor_verification_failure(
        prepared,
        proposal,
        authoritative_verification=verification,
        usage=None,
        verified_snapshot=proposal_snapshot,
    )

    failed_dispatch = failed.dispatches[prepared.dispatch.dispatch_id]
    assert failed_dispatch.state.value == "FAILED"
    assert failed_dispatch.failure_category == "authoritative_verification"
    assert failed.steps[prepared.dispatch.step_id].state is StepStatus.READY
    assert store.load_dispatch_payload(
        failed.run_id, prepared.dispatch.dispatch_id
    ).authoritative_verification == tuple(item.model_dump(mode="json") for item in verification)
    assert store.load_dispatch_payload(
        failed.run_id, prepared.dispatch.dispatch_id
    ).repository_after == dirty.model_dump(mode="json")
    assert not store.leases_for_run(failed.run_id)
    store.close()

    recovered_store = _store(project)
    persisted, persisted_generation = recovered_store.load_run(failed.run_id)
    recovered_workflow = SequentialWorkflow(
        project.config,
        recovered_store,
        owner_id="verification-feedback-recovery-owner",
        repository_inspector=inspect,
    )
    retry = recovered_workflow.prepare_pending_verification_retry(
        persisted,
        persisted_generation,
    )
    assert retry is not None
    retry_context = json.loads(retry.prompt)
    assert retry.session_mode == "resume"
    assert retry.session_id == "ses-verification-feedback"
    assert retry.dispatch.attempt == 2
    assert retry.repository_before == clean
    assert retry_context["verification_feedback"] == [
        item.model_dump(mode="json") for item in verification
    ]
    assert retry_context["authoritative_sources"] == [
        {
            "source_id": "fixture-plan-source",
            "root": "plans",
            "relative_path": "plan.md",
            "sha256": hashlib.sha256((project.plans / "plan.md").read_bytes()).hexdigest(),
            "path": str(
                (Path(project.config.model.sources.plans_dir) / "plan.md").resolve()
            ),
        }
    ]
    assert "dispatcher.executor_proposal.v2" in retry_context["task"]


def test_durable_verification_retry_rechecks_sources_before_preparing_prompt(
    project: FixtureProject,
) -> None:
    store, workflow, prepared, _clean, _current = _prepare_verification_feedback_executor(
        project,
        max_executor_attempts=2,
    )
    proposal = parse_executor_proposal(json.loads(prepared.prompt)["response_template"])
    snapshot = workflow.record_executor_proposal(prepared, proposal)
    failed, generation = workflow.record_executor_verification_failure(
        prepared,
        proposal,
        authoritative_verification=_failed_authoritative_verification(),
        usage=None,
        verified_snapshot=snapshot,
    )
    original_prompt = store.load_dispatch_payload(
        failed.run_id, prepared.dispatch.dispatch_id
    ).prompt
    (project.plans / "plan.md").write_text("changed after activation\n", encoding="utf-8")

    retry = workflow.prepare_pending_verification_retry(failed, generation)

    assert isinstance(retry, tuple)
    waiting, _waiting_generation = retry
    assert waiting.state is RunStatus.WAITING_OPERATOR
    assert waiting.operator_request is not None
    assert "plan source hash mismatch" in waiting.operator_request.question
    assert len(waiting.dispatches) == 1
    assert store.load_dispatch_payload(
        failed.run_id, prepared.dispatch.dispatch_id
    ).prompt == original_prompt


def test_batch_verification_failure_persists_authoritative_evidence_without_rework(
    project: FixtureProject,
) -> None:
    store = _store(project)
    record = _record(
        project,
        max_executor_attempts=2,
        authorized_actions=("inspect", "modify", "commit"),
    )
    generation = store.create_run(record)
    clean = _repository_snapshot()
    dirty = _dirty_snapshot(clean, marker="f")

    def inspect(_config, _repo_id, *, require_clean):
        return clean if require_clean else dirty

    workflow = SequentialWorkflow(
        project.config,
        store,
        owner_id="batch-verification-feedback-owner",
        repository_inspector=inspect,
    )
    active, generation = workflow.activate(record.run_id, expected_generation=generation)
    prepared = workflow.prepare_from_supervisor(
        active.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(),
    )
    assert isinstance(prepared, PreparedDispatch)
    prepared = workflow.record_session_id(
        workflow.mark_running(prepared, process_id=1234, process_create_time=1234.0),
        runtime_session_id="ses-batch-verification-feedback",
    )
    stored, generation = store.load_run(prepared.run_id)
    batched_dispatch = stored.dispatches[prepared.dispatch.dispatch_id].model_copy(
        update={"batch_id": "batch-verification-feedback"}
    )
    stored = stored.model_copy(
        update={"dispatches": {**stored.dispatches, batched_dispatch.dispatch_id: batched_dispatch}}
    )
    generation = store.save_run(stored, expected_generation=generation)
    prepared = replace(prepared, generation=generation, dispatch=batched_dispatch)
    proposal = parse_executor_proposal(json.loads(prepared.prompt)["response_template"])
    assert proposal.outcome == "completed"
    snapshot = workflow.record_executor_proposal(prepared, proposal)
    verification = _failed_authoritative_verification()

    failed, generation = workflow.record_executor_verification_failure(
        prepared,
        proposal,
        authoritative_verification=verification,
        usage=None,
        verified_snapshot=snapshot,
    )

    dispatch = failed.dispatches[prepared.dispatch.dispatch_id]
    payload = store.load_dispatch_payload(failed.run_id, dispatch.dispatch_id)
    assert dispatch.failure_category == "authoritative_verification"
    assert dispatch.failure_detail is not None
    assert "backend=fixture-isolation" in dispatch.failure_detail
    assert "transcript=" + "c" * 64 in dispatch.failure_detail
    assert failed.steps[dispatch.step_id].state is StepStatus.BLOCKED
    assert failed.state is RunStatus.RUNNING
    assert payload.result == proposal.model_dump(mode="json")
    assert payload.repository_after == dirty.model_dump(mode="json")
    assert payload.authoritative_verification == tuple(
        item.model_dump(mode="json") for item in verification
    )
    assert workflow.prepare_pending_verification_retry(failed, generation) is None


def test_batch_verification_failure_reconciliation_never_enters_rework_loop(
    project: FixtureProject,
) -> None:
    store = _store(project)
    record = _record(
        project,
        max_executor_attempts=2,
        authorized_actions=("inspect", "modify", "commit"),
    )
    generation = store.create_run(record)
    clean = _repository_snapshot()
    dirty = _dirty_snapshot(clean, marker="f")

    def inspect(_config, _repo_id, *, require_clean):
        return clean if require_clean else dirty

    workflow = SequentialWorkflow(
        project.config,
        store,
        owner_id="batch-reconcile-feedback-owner",
        repository_inspector=inspect,
    )
    active, generation = workflow.activate(record.run_id, expected_generation=generation)
    prepared = workflow.prepare_from_supervisor(
        active.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(),
    )
    assert isinstance(prepared, PreparedDispatch)
    prepared = workflow.record_session_id(
        workflow.mark_running(prepared, process_id=1234, process_create_time=1234.0),
        runtime_session_id="ses-batch-reconcile-feedback",
    )
    stored, generation = store.load_run(prepared.run_id)
    batched_dispatch = stored.dispatches[prepared.dispatch.dispatch_id].model_copy(
        update={"batch_id": "batch-reconcile-feedback"}
    )
    stored = stored.model_copy(
        update={"dispatches": {**stored.dispatches, batched_dispatch.dispatch_id: batched_dispatch}}
    )
    generation = store.save_run(stored, expected_generation=generation)
    prepared = replace(prepared, generation=generation, dispatch=batched_dispatch)
    proposal = parse_executor_proposal(json.loads(prepared.prompt)["response_template"])
    snapshot = workflow.record_executor_proposal(prepared, proposal)
    failed, generation = workflow.record_executor_verification_failure(
        prepared,
        proposal,
        authoritative_verification=_failed_authoritative_verification(),
        usage=None,
        verified_snapshot=snapshot,
    )
    dispatch = failed.dispatches[prepared.dispatch.dispatch_id]
    assert failed.steps[dispatch.step_id].state is StepStatus.BLOCKED
    assert workflow.prepare_pending_verification_retry(failed, generation) is None

    reconciled_event = workflow._event(
        failed,
        "operator",
        "operator reconciled the failed batch dispatch",
        dispatch.dispatch_id,
    )
    reconciled_step = transition_step(
        failed.steps[dispatch.step_id],
        StepStatus.READY,
        reconciled_event,
    )
    steps = dict(failed.steps)
    steps[dispatch.step_id] = reconciled_step
    reconciled = failed.model_copy(
        update={
            "steps": steps,
            "sequence": reconciled_event.sequence,
            "updated_at": reconciled_event.occurred_at,
        }
    )
    generation = store.save_run(reconciled, expected_generation=generation)

    assert workflow.prepare_pending_verification_retry(reconciled, generation) is None
    persisted, _generation = store.load_run(failed.run_id)
    assert persisted.state is RunStatus.RUNNING
    assert persisted.operator_request is None
    assert persisted.steps[dispatch.step_id].state is StepStatus.READY


def test_verification_evidence_persists_even_when_usage_validation_fails(
    project: FixtureProject,
) -> None:
    store, workflow, prepared, clean, current = _prepare_verification_feedback_executor(
        project,
        max_executor_attempts=2,
    )
    store.begin_opencode_invocation(
        invocation_id="dispatch-invocation-usage-mismatch",
        run_id=prepared.run_id,
        dispatch_id=prepared.dispatch.dispatch_id,
        role_kind="executor",
        role_key="terra",
        step_id=prepared.dispatch.step_id,
        session_mode="new",
        requested_session_id=None,
    )
    _record, generation = store.finish_opencode_invocation(
        invocation_id="dispatch-invocation-usage-mismatch",
        runtime_session_id="ses-usage-mismatch",
        usage={
            "cost_usd": 0.1,
            "tokens_total": 10,
            "tokens_input": 6,
            "tokens_output": 4,
            "tokens_reasoning": 0,
        },
    )
    prepared = replace(prepared, generation=generation)
    proposal = parse_executor_proposal(json.loads(prepared.prompt)["response_template"])
    snapshot = workflow.record_executor_proposal(prepared, proposal)
    verification = _failed_authoritative_verification()

    failed, generation = workflow.record_executor_verification_failure(
        prepared,
        proposal,
        authoritative_verification=verification,
        usage={
            "cost_usd": 0.2,
            "tokens_total": 20,
            "tokens_input": 12,
            "tokens_output": 8,
            "tokens_reasoning": 0,
        },
        verified_snapshot=snapshot,
    )

    persisted, _generation = store.load_run(prepared.run_id)
    dispatch = persisted.dispatches[prepared.dispatch.dispatch_id]
    payload = store.load_dispatch_payload(prepared.run_id, prepared.dispatch.dispatch_id)
    assert dispatch.failure_category == "authoritative_verification"
    assert payload.authoritative_verification == tuple(
        item.model_dump(mode="json") for item in verification
    )
    assert payload.result == proposal.model_dump(mode="json")
    assert failed.state is RunStatus.WAITING_OPERATOR
    assert failed.steps[dispatch.step_id].state is StepStatus.BLOCKED
    assert failed.operator_request is not None
    assert failed.operator_request.kind == "reconciliation"
    assert workflow.prepare_pending_verification_retry(failed, generation) is None


def test_verification_retry_ignores_a_failure_from_an_older_executor_attempt(
    project: FixtureProject,
) -> None:
    store, workflow, prepared, _clean, _current = _prepare_verification_feedback_executor(
        project,
        max_executor_attempts=3,
    )
    proposal = parse_executor_proposal(json.loads(prepared.prompt)["response_template"])
    snapshot = workflow.record_executor_proposal(prepared, proposal)
    failed, generation = workflow.record_executor_verification_failure(
        prepared,
        proposal,
        authoritative_verification=_failed_authoritative_verification(),
        usage=None,
        verified_snapshot=snapshot,
    )

    step = failed.steps[prepared.dispatch.step_id].model_copy(update={"executor_attempts": 2})
    stale = failed.model_copy(update={"steps": {**failed.steps, step.step_id: step}})
    generation = store.save_run(stale, expected_generation=generation)

    assert workflow.prepare_pending_verification_retry(stale, generation) is None
    persisted, _generation = store.load_run(stale.run_id)
    assert persisted.state is RunStatus.RUNNING
    assert persisted.steps[step.step_id].state is StepStatus.READY


def test_verification_retry_ignores_stale_failure_after_reviewer_changes_requested(
    project: FixtureProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = config_values(project)
    values["repositories"]["fixture-repo"]["commit_policy"] = "prohibited"
    configured = replace(project, config=write_config(project, values))
    store, workflow, prepared, _clean, _current = _prepare_verification_feedback_executor(
        configured,
        max_executor_attempts=3,
        authorized_actions=("inspect", "modify"),
    )
    proposal = parse_executor_proposal(json.loads(prepared.prompt)["response_template"])
    first_snapshot = workflow.record_executor_proposal(prepared, proposal)
    failed, generation = workflow.record_executor_verification_failure(
        prepared,
        proposal,
        authoritative_verification=_failed_authoritative_verification(),
        usage=None,
        verified_snapshot=first_snapshot,
    )
    retry = workflow.prepare_pending_verification_retry(failed, generation)
    assert isinstance(retry, PreparedDispatch)
    retry = workflow.record_session_id(
        workflow.mark_running(retry, process_id=1235, process_create_time=1235.0),
        runtime_session_id="ses-verification-retry",
    )
    monkeypatch.setattr("dispatcher.sequential.working_patch_sha256", lambda _root: "a" * 64)
    monkeypatch.setattr("dispatcher.repository.working_patch_sha256", lambda _root: "a" * 64)
    second_proposal = parse_executor_proposal(json.loads(retry.prompt)["response_template"])
    second_snapshot = workflow.record_executor_proposal(retry, second_proposal)
    completed, generation, _forwarding = workflow.materialize_executor_proposal(
        retry,
        second_proposal,
        authoritative_verification=_passed_authoritative_verification(),
        usage=None,
        verified_snapshot=second_snapshot,
    )
    completed, generation = workflow.acknowledge_forwarding(
        completed.run_id,
        expected_generation=generation,
        dispatch_id=retry.dispatch.dispatch_id,
    )
    reviewer = workflow.prepare_from_supervisor(
        completed.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(role="reviewer"),
    )
    assert isinstance(reviewer, PreparedDispatch)
    reviewer = workflow.record_session_id(
        workflow.mark_running(reviewer, process_id=1236, process_create_time=1236.0),
        runtime_session_id="ses-verification-retry-reviewer",
    )
    changes_requested = parse_reviewer_result(
        _reviewer_result(reviewer, verdict="changes_requested"),
    )
    changed, generation, _forwarding = workflow.apply_reviewer_result(
        reviewer,
        changes_requested,
    )

    assert changed.steps[reviewer.dispatch.step_id].state is StepStatus.READY
    assert changed.dispatches[reviewer.dispatch.dispatch_id].state is DispatchStatus.FORWARDED
    assert workflow.prepare_pending_verification_retry(changed, generation) is None

    acknowledged, generation = workflow.acknowledge_forwarding(
        changed.run_id,
        expected_generation=generation,
        dispatch_id=reviewer.dispatch.dispatch_id,
    )
    assert acknowledged.dispatches[reviewer.dispatch.dispatch_id].state is DispatchStatus.ACKNOWLEDGED
    assert workflow.prepare_pending_verification_retry(acknowledged, generation) is None


def test_budgeted_verification_without_usage_requires_halt(project: FixtureProject) -> None:
    from helpers import config_values, write_config

    values = config_values(project)
    values["budget"].update(
        {
            "enabled": True,
            "max_run_cost_usd": 1.0,
            "max_step_cost_usd": 1.0,
            "max_context_tokens": 100,
            "on_limit": "halt",
        }
    )
    configured = replace(project, config=write_config(project, values))
    store, workflow, prepared, _clean, _current = _prepare_verification_feedback_executor(
        configured,
        max_executor_attempts=2,
    )
    proposal = parse_executor_proposal(json.loads(prepared.prompt)["response_template"])
    snapshot = workflow.record_executor_proposal(prepared, proposal)

    failed, generation = workflow.record_executor_verification_failure(
        prepared,
        proposal,
        authoritative_verification=_failed_authoritative_verification(),
        usage=None,
        verified_snapshot=snapshot,
    )

    assert failed.state is RunStatus.WAITING_OPERATOR
    assert failed.steps[prepared.dispatch.step_id].state is StepStatus.BLOCKED
    assert failed.operator_request is not None
    assert failed.operator_request.kind == "budget"
    assert failed.operator_request.allowed_answers == ["halt"]
    assert workflow.prepare_pending_verification_retry(failed, generation) is None



def test_batch_post_review_verification_failure_persists_authoritative_evidence_without_rework(
    project: FixtureProject,
) -> None:
    store, workflow, reviewer = _prepare_reviewer(project, max_executor_attempts=2)
    stored, generation = store.load_run(reviewer.run_id)
    batched_dispatch = stored.dispatches[reviewer.dispatch.dispatch_id].model_copy(
        update={"batch_id": "batch-review-verification-feedback"}
    )
    stored = stored.model_copy(
        update={"dispatches": {**stored.dispatches, batched_dispatch.dispatch_id: batched_dispatch}}
    )
    generation = store.save_run(stored, expected_generation=generation)
    reviewer = replace(reviewer, generation=generation, dispatch=batched_dispatch)
    result = parse_reviewer_result(_reviewer_result(reviewer))
    assert result.verdict == "accepted"
    snapshot = workflow.inspect_reviewer_result(reviewer, result)
    verification = _failed_authoritative_verification(
        summary="fresh acceptance assertion failed",
    )

    failed, generation = workflow.record_reviewer_verification_failure(
        reviewer,
        result,
        authoritative_verification=verification,
        usage=None,
        verified_snapshot=snapshot,
    )

    dispatch = failed.dispatches[reviewer.dispatch.dispatch_id]
    payload = store.load_dispatch_payload(failed.run_id, dispatch.dispatch_id)
    assert dispatch.failure_category == "acceptance_verification"
    assert dispatch.failure_detail is not None
    assert "fresh acceptance assertion failed" in dispatch.failure_detail
    assert failed.steps[dispatch.step_id].state is StepStatus.BLOCKED
    assert failed.state is RunStatus.RUNNING
    assert store.review_for_dispatch(failed.run_id, dispatch.dispatch_id) is False
    assert payload.result == result.model_dump(mode="json")
    assert payload.authoritative_verification == tuple(
        item.model_dump(mode="json") for item in verification
    )
    assert workflow.prepare_pending_verification_retry(failed, generation) is None


def test_verification_retry_recovery_uses_only_the_newest_durable_dirty_failure(
    project: FixtureProject,
) -> None:
    store, workflow, prepared, clean, current = _prepare_verification_feedback_executor(
        project,
        max_executor_attempts=3,
    )
    first_proposal = parse_executor_proposal(json.loads(prepared.prompt)["response_template"])
    first_snapshot = workflow.record_executor_proposal(prepared, first_proposal)
    first_verification = _failed_authoritative_verification(
        summary="first verification failure",
        transcript_marker="c",
    )
    first_failed, generation = workflow.record_executor_verification_failure(
        prepared,
        first_proposal,
        authoritative_verification=first_verification,
        usage=None,
        verified_snapshot=first_snapshot,
    )
    first_payload = store.load_dispatch_payload(
        first_failed.run_id,
        prepared.dispatch.dispatch_id,
    )
    assert first_payload.repository_after == current["snapshot"].model_dump(mode="json")

    retry = workflow.prepare_pending_verification_retry(first_failed, generation)
    assert isinstance(retry, PreparedDispatch)
    retry = workflow.record_session_id(
        workflow.mark_running(retry, process_id=1235, process_create_time=1235.0),
        runtime_session_id="ses-verification-feedback",
    )
    current["snapshot"] = _dirty_snapshot(clean, marker="d")
    second_proposal = parse_executor_proposal(json.loads(retry.prompt)["response_template"])
    second_snapshot = workflow.record_executor_proposal(retry, second_proposal)
    second_verification = _failed_authoritative_verification(
        summary="newest verification failure",
        transcript_marker="e",
    )
    second_failed, generation = workflow.record_executor_verification_failure(
        retry,
        second_proposal,
        authoritative_verification=second_verification,
        usage=None,
        verified_snapshot=second_snapshot,
    )
    second_payload = store.load_dispatch_payload(
        second_failed.run_id,
        retry.dispatch.dispatch_id,
    )
    assert second_payload.repository_after == current["snapshot"].model_dump(mode="json")
    assert not store.leases_for_run(second_failed.run_id)
    store.close()

    recovered_store = _store(project)
    recovered_record, recovered_generation = recovered_store.load_run(second_failed.run_id)
    recovered_workflow = SequentialWorkflow(
        project.config,
        recovered_store,
        owner_id="verification-feedback-newest-recovery-owner",
        repository_inspector=lambda _config, _repo_id, require_clean: (
            clean if require_clean else current["snapshot"]
        ),
    )
    recovered_retry = recovered_workflow.prepare_pending_verification_retry(
        recovered_record,
        recovered_generation,
    )

    assert isinstance(recovered_retry, PreparedDispatch)
    assert recovered_retry.dispatch.attempt == 3
    assert recovered_retry.session_id == "ses-verification-feedback"
    assert recovered_retry.repository_before == clean
    assert json.loads(recovered_retry.prompt)["verification_feedback"] == [
        item.model_dump(mode="json") for item in second_verification
    ]


def test_verification_retry_preparation_failure_waits_for_operator(
    project: FixtureProject,
) -> None:
    store, workflow, prepared, clean, current = _prepare_verification_feedback_executor(
        project,
        max_executor_attempts=2,
    )
    proposal = parse_executor_proposal(json.loads(prepared.prompt)["response_template"])
    snapshot = workflow.record_executor_proposal(prepared, proposal)
    verification = _failed_authoritative_verification()
    failed, generation = workflow.record_executor_verification_failure(
        prepared,
        proposal,
        authoritative_verification=verification,
        usage=None,
        verified_snapshot=snapshot,
    )
    current["snapshot"] = _dirty_snapshot(clean, marker="d")

    waiting = workflow.prepare_pending_verification_retry(failed, generation)

    assert isinstance(waiting, tuple)
    waiting_record, _waiting_generation = waiting
    payload = store.load_dispatch_payload(failed.run_id, prepared.dispatch.dispatch_id)
    assert waiting_record.state is RunStatus.WAITING_OPERATOR
    assert waiting_record.steps[prepared.dispatch.step_id].state is StepStatus.BLOCKED
    assert waiting_record.operator_request is not None
    assert waiting_record.operator_request.kind == "reconciliation"
    assert waiting_record.operator_request.context_ref == prepared.dispatch.dispatch_id
    assert waiting_record.dispatches[prepared.dispatch.dispatch_id].failure_category == (
        "authoritative_verification"
    )
    assert payload.repository_after == _dirty_snapshot(clean, marker="f").model_dump(mode="json")
    assert payload.authoritative_verification == tuple(
        item.model_dump(mode="json") for item in verification
    )


def test_failed_post_review_verification_routes_to_prior_executor_session(
    project: FixtureProject,
) -> None:
    store, workflow, reviewer = _prepare_reviewer(
        project,
        max_executor_attempts=2,
    )
    result = parse_reviewer_result(_reviewer_result(reviewer))
    assert result.verdict == "accepted"
    review_snapshot = workflow.inspect_reviewer_result(reviewer, result)
    verification = (
        AuthoritativeVerification(
            check_id="fixture-check",
            status="failed",
            argv=("python", "-c", "raise SystemExit(1)"),
            exit_code=1,
            timed_out=False,
            output_truncated=False,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
            transcript_sha256="c" * 64,
            duration_ms=5,
            backend="fixture-isolation",
            summary="fresh acceptance assertion failed",
        ),
    )

    failed, generation = workflow.record_reviewer_verification_failure(
        reviewer,
        result,
        authoritative_verification=verification,
        usage=None,
        verified_snapshot=review_snapshot,
    )

    failed_dispatch = failed.dispatches[reviewer.dispatch.dispatch_id]
    assert failed_dispatch.state.value == "FAILED"
    assert failed_dispatch.failure_category == "acceptance_verification"
    assert failed.steps[reviewer.dispatch.step_id].state is StepStatus.READY
    assert store.review_for_dispatch(failed.run_id, reviewer.dispatch.dispatch_id) is False
    assert store.load_dispatch_payload(
        failed.run_id,
        reviewer.dispatch.dispatch_id,
    ).authoritative_verification == tuple(
        item.model_dump(mode="json") for item in verification
    )

    retry = workflow.prepare_pending_verification_retry(
        failed,
        generation,
    )
    assert isinstance(retry, PreparedDispatch)
    retry_context = json.loads(retry.prompt)
    assert retry.dispatch.role_kind == "executor"
    assert retry.dispatch.role_key == "terra"
    assert retry.session_mode == "resume"
    assert retry.session_id == "ses-executor"
    assert retry.dispatch.attempt == 2
    assert retry.repository_before == RepositorySnapshot.model_validate_json(
        json.dumps(
            store.load_dispatch_payload(
                failed.run_id,
                reviewer.dispatch.dispatch_id,
            ).repository_before
        )
    )
    assert retry_context["verification_feedback"] == [
        item.model_dump(mode="json") for item in verification
    ]


def test_post_review_verification_usage_mismatch_waits_for_reconciliation(
    project: FixtureProject,
) -> None:
    store, workflow, reviewer = _prepare_reviewer(project, max_executor_attempts=2)
    store.begin_opencode_invocation(
        invocation_id="reviewer-invocation-usage-mismatch",
        run_id=reviewer.run_id,
        dispatch_id=reviewer.dispatch.dispatch_id,
        role_kind="reviewer",
        role_key="reviewer",
        step_id=reviewer.dispatch.step_id,
        session_mode="new",
        requested_session_id=None,
    )
    _record, generation = store.finish_opencode_invocation(
        invocation_id="reviewer-invocation-usage-mismatch",
        runtime_session_id="ses-reviewer",
        usage={
            "cost_usd": 0.1,
            "tokens_total": 10,
            "tokens_input": 6,
            "tokens_output": 4,
            "tokens_reasoning": 0,
        },
    )
    reviewer = replace(reviewer, generation=generation)
    result = parse_reviewer_result(_reviewer_result(reviewer))
    snapshot = workflow.inspect_reviewer_result(reviewer, result)
    verification = _failed_authoritative_verification(
        summary="fresh acceptance assertion failed",
    )

    failed, generation = workflow.record_reviewer_verification_failure(
        reviewer,
        result,
        authoritative_verification=verification,
        usage={
            "cost_usd": 0.2,
            "tokens_total": 20,
            "tokens_input": 12,
            "tokens_output": 8,
            "tokens_reasoning": 0,
        },
        verified_snapshot=snapshot,
    )

    dispatch = failed.dispatches[reviewer.dispatch.dispatch_id]
    assert dispatch.failure_category == "acceptance_verification"
    assert failed.state is RunStatus.WAITING_OPERATOR
    assert failed.steps[dispatch.step_id].state is StepStatus.BLOCKED
    assert failed.operator_request is not None
    assert failed.operator_request.kind == "reconciliation"
    assert workflow.prepare_pending_verification_retry(failed, generation) is None


def test_acceptance_verification_failure_with_exhausted_reviewer_budget_fails_step(
    project: FixtureProject,
) -> None:
    store, workflow, reviewer = _prepare_reviewer(
        project,
        max_executor_attempts=2,
        max_reviewer_attempts=1,
    )
    result = parse_reviewer_result(_reviewer_result(reviewer))
    assert result.verdict == "accepted"
    review_snapshot = workflow.inspect_reviewer_result(reviewer, result)
    verification = (
        AuthoritativeVerification(
            check_id="fixture-check",
            status="failed",
            argv=("python", "-c", "raise SystemExit(1)"),
            exit_code=1,
            timed_out=False,
            output_truncated=False,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
            transcript_sha256="c" * 64,
            duration_ms=5,
            backend="fixture-isolation",
            summary="fresh acceptance assertion failed",
        ),
    )

    failed, generation = workflow.record_reviewer_verification_failure(
        reviewer,
        result,
        authoritative_verification=verification,
        usage=None,
        verified_snapshot=review_snapshot,
    )

    dispatch = failed.dispatches[reviewer.dispatch.dispatch_id]
    assert dispatch.failure_category == "acceptance_verification"
    assert failed.steps[reviewer.dispatch.step_id].state is StepStatus.FAILED
    assert failed.state is RunStatus.FAILED
    assert store.load_dispatch_payload(
        failed.run_id,
        reviewer.dispatch.dispatch_id,
    ).authoritative_verification == tuple(item.model_dump(mode="json") for item in verification)
    assert workflow.prepare_pending_verification_retry(failed, generation) is None


def test_discriminator_option_constants_track_result_union_literals() -> None:
    assert EXECUTOR_OUTCOME_OPTIONS == _result_union_discriminator_options(ExecutorResult, "outcome")
    assert EXECUTOR_PROPOSAL_OUTCOME_OPTIONS == ("completed", "blocked", "failed")
    assert REVIEWER_VERDICT_OPTIONS == _result_union_discriminator_options(ReviewerResult, "verdict")


def _result_union_discriminator_options(result_union: object, field_name: str) -> tuple[str, ...]:
    variants = get_args(get_args(result_union)[0])
    return tuple(
        value
        for variant in variants
        for value in get_args(variant.model_fields[field_name].annotation)
    )


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


def test_recover_interrupted_dispatch_waits_for_reconciliation_after_usage_finalization(
    project: FixtureProject,
) -> None:
    store, workflow, prepared = _prepare_executor(project)
    store.begin_opencode_invocation(
        invocation_id="dispatch-interrupted-after-finish",
        run_id=prepared.run_id,
        dispatch_id=prepared.dispatch.dispatch_id,
        role_kind="executor",
        role_key="terra",
        step_id=prepared.dispatch.step_id,
        session_mode="new",
        requested_session_id=None,
    )
    store.finish_opencode_invocation(
        invocation_id="dispatch-interrupted-after-finish",
        runtime_session_id="ses-executor",
        usage=None,
    )

    recovered, generation = workflow.recover_interrupted_dispatch(
        prepared.run_id,
        prepared.dispatch.dispatch_id,
    )

    dispatch = recovered.dispatches[prepared.dispatch.dispatch_id]
    assert dispatch.state is DispatchStatus.FAILED
    assert dispatch.failure_category == "interrupted"
    assert recovered.state is RunStatus.WAITING_OPERATOR
    assert recovered.steps[dispatch.step_id].state is StepStatus.BLOCKED
    assert recovered.operator_request is not None
    assert recovered.operator_request.kind == "reconciliation"
    assert recovered.operator_request.context_ref == dispatch.dispatch_id
    resumed, _generation = store.answer_operator_request(
        run_id=recovered.run_id,
        expected_generation=generation,
        request_id=recovered.operator_request.request_id,
        answer="reconcile",
        actor_id="operator",
    )
    assert resumed.state is RunStatus.RUNNING
    assert resumed.steps[dispatch.step_id].state is StepStatus.READY


def test_completed_executor_with_exact_passed_coverage_requires_review(
    project: FixtureProject,
) -> None:
    _store_value, workflow, prepared = _prepare_executor(project, review_required=True)

    record, _generation, _forwarding = workflow.apply_executor_result(
        prepared,
        parse_executor_result(_executor_result(prepared)),
    )

    assert record.steps["prepare-fixture"].state is StepStatus.REVIEW_REQUIRED


@pytest.mark.parametrize(
    ("verification", "message"),
    [
        ([], "missing criterion IDs=['fixture-check']"),
        (
            [
                {"check_id": "fixture-check", "status": "passed", "summary": "passed"},
                {"check_id": "extra-check", "status": "passed", "summary": "extra"},
            ],
            "unknown check IDs=['extra-check']",
        ),
        (
            [
                {"check_id": "fixture-check", "status": "passed", "summary": "first"},
                {"check_id": "fixture-check", "status": "passed", "summary": "duplicate"},
            ],
            "duplicate check IDs=['fixture-check']",
        ),
        (
            [{"check_id": "renamed-check", "status": "passed", "summary": "renamed"}],
            "missing criterion IDs=['fixture-check']; unknown check IDs=['renamed-check']",
        ),
    ],
)
def test_executor_verification_requires_exact_criterion_coverage(
    project: FixtureProject,
    verification: list[dict[str, str]],
    message: str,
) -> None:
    store, workflow, prepared = _prepare_executor(project)

    with pytest.raises(SequentialWorkflowError, match=re.escape(message)):
        workflow.apply_executor_result(
            prepared,
            parse_executor_result(_executor_result(prepared, verification=verification)),
        )

    persisted, _generation = store.load_run(prepared.run_id)
    assert persisted.dispatches[prepared.dispatch.dispatch_id].state.value == "RUNNING"
    assert persisted.steps["prepare-fixture"].state is StepStatus.EXECUTING


@pytest.mark.parametrize("review_required", [False, True])
@pytest.mark.parametrize("status", ["failed", "skipped"])
def test_completed_executor_rejects_non_passing_verification_without_reshaping(
    project: FixtureProject,
    review_required: bool,
    status: str,
) -> None:
    store, workflow, prepared = _prepare_executor(project, review_required=review_required)
    result = parse_executor_result(
        _executor_result(
            prepared,
            verification=[
                {"check_id": "fixture-check", "status": status, "summary": "not passed"}
            ],
        )
    )

    with pytest.raises(
        SequentialWorkflowError,
        match=rf"executor completed.*non-passing checks=\['fixture-check={status}'\]",
    ):
        workflow.apply_executor_result(prepared, result)

    persisted, _generation = store.load_run(prepared.run_id)
    assert result.outcome == "completed"
    assert persisted.steps["prepare-fixture"].state is StepStatus.EXECUTING
    assert persisted.dispatches[prepared.dispatch.dispatch_id].state.value == "RUNNING"


@pytest.mark.parametrize(("outcome", "status"), [("blocked", "skipped"), ("failed", "failed")])
def test_executor_non_success_variants_allow_non_passing_exact_coverage(
    project: FixtureProject,
    outcome: str,
    status: str,
) -> None:
    _store_value, workflow, prepared = _prepare_executor(project)

    record, _generation, _forwarding = workflow.apply_executor_result(
        prepared,
        parse_executor_result(
            _executor_result(
                prepared,
                outcome,
                verification=[
                    {"check_id": "fixture-check", "status": status, "summary": "attention"}
                ],
            )
        ),
    )

    assert record.steps["prepare-fixture"].state in {StepStatus.BLOCKED, StepStatus.FAILED}


def test_executor_non_success_variant_still_requires_exact_coverage(
    project: FixtureProject,
) -> None:
    _store_value, workflow, prepared = _prepare_executor(project)

    with pytest.raises(SequentialWorkflowError, match="missing criterion IDs"):
        workflow.apply_executor_result(
            prepared,
            parse_executor_result(
                _executor_result(prepared, "blocked", verification=[])
            ),
        )


@pytest.mark.parametrize("role_kind", ["executor", "reviewer"])
def test_empty_acceptance_criteria_require_empty_verification(
    project: FixtureProject,
    role_kind: str,
) -> None:
    _store_value, workflow, executor = _prepare_executor(project, review_required=True)
    step = _record(project).plan.steps[0].model_copy(update={"acceptance_criteria": ()})
    if role_kind == "executor":
        empty_result: ExecutorResult | ReviewerResult = parse_executor_result(
            _executor_result(executor, verification=[])
        )
    else:
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
        empty_result = parse_reviewer_result(_reviewer_result(reviewer, verification=[]))

    _validate_result_verification(step, empty_result)


@pytest.mark.parametrize("role_kind", ["executor", "reviewer"])
def test_empty_acceptance_criteria_reject_nonempty_verification(
    project: FixtureProject,
    role_kind: str,
) -> None:
    _store_value, workflow, executor = _prepare_executor(project, review_required=True)
    step = _record(project).plan.steps[0].model_copy(update={"acceptance_criteria": ()})
    if role_kind == "executor":
        result: ExecutorResult | ReviewerResult = parse_executor_result(
            _executor_result(executor)
        )
    else:
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
        result = parse_reviewer_result(_reviewer_result(reviewer))

    with pytest.raises(SequentialWorkflowError, match="unknown check IDs"):
        _validate_result_verification(step, result)


@pytest.mark.parametrize(
    ("verification", "message"),
    [
        ([], "missing criterion IDs=['fixture-check']"),
        (
            [
                {"check_id": "fixture-check", "status": "passed", "summary": "passed"},
                {"check_id": "extra-check", "status": "passed", "summary": "extra"},
            ],
            "unknown check IDs=['extra-check']",
        ),
        (
            [
                {"check_id": "fixture-check", "status": "passed", "summary": "first"},
                {"check_id": "fixture-check", "status": "passed", "summary": "duplicate"},
            ],
            "duplicate check IDs=['fixture-check']",
        ),
        (
            [{"check_id": "renamed-check", "status": "passed", "summary": "renamed"}],
            "missing criterion IDs=['fixture-check']; unknown check IDs=['renamed-check']",
        ),
    ],
)
def test_reviewer_verification_requires_exact_criterion_coverage(
    project: FixtureProject,
    verification: list[dict[str, str]],
    message: str,
) -> None:
    store, workflow, reviewer = _prepare_reviewer(project)

    with pytest.raises(SequentialWorkflowError, match=re.escape(message)):
        workflow.apply_reviewer_result(
            reviewer,
            parse_reviewer_result(_reviewer_result(reviewer, verification=verification)),
        )

    persisted, _generation = store.load_run(reviewer.run_id)
    assert persisted.dispatches[reviewer.dispatch.dispatch_id].state.value == "RUNNING"
    assert persisted.steps["prepare-fixture"].state is StepStatus.REVIEWING


@pytest.mark.parametrize("status", ["failed", "skipped"])
def test_accepted_reviewer_rejects_non_passing_verification_before_review_persistence(
    project: FixtureProject,
    status: str,
) -> None:
    store, workflow, reviewer = _prepare_reviewer(project)
    result = parse_reviewer_result(
        _reviewer_result(
            reviewer,
            verification=[
                {"check_id": "fixture-check", "status": status, "summary": "not passed"}
            ],
        )
    )

    with pytest.raises(
        SequentialWorkflowError,
        match=rf"reviewer accepted.*non-passing checks=\['fixture-check={status}'\]",
    ):
        workflow.apply_reviewer_result(reviewer, result)

    persisted, _generation = store.load_run(reviewer.run_id)
    assert result.verdict == "accepted"
    assert persisted.steps["prepare-fixture"].review_acceptances == 0
    assert persisted.dispatches[reviewer.dispatch.dispatch_id].state.value == "RUNNING"
    with sqlite3.connect(store.database_path) as connection:
        review_count = connection.execute(
            "SELECT COUNT(*) FROM reviews WHERE dispatch_id = ?",
            (reviewer.dispatch.dispatch_id,),
        ).fetchone()[0]
    assert review_count == 0


@pytest.mark.parametrize(
    ("verdict", "status"),
    [
        ("changes_requested", "failed"),
        ("blocked", "skipped"),
        ("inconclusive", "failed"),
    ],
)
def test_reviewer_non_success_variants_allow_non_passing_exact_coverage(
    project: FixtureProject,
    verdict: str,
    status: str,
) -> None:
    _store_value, workflow, reviewer = _prepare_reviewer(project)

    record, _generation, _forwarding = workflow.apply_reviewer_result(
        reviewer,
        parse_reviewer_result(
            _reviewer_result(
                reviewer,
                verdict,
                verification=[
                    {"check_id": "fixture-check", "status": status, "summary": "attention"}
                ],
            )
        ),
    )

    assert record.dispatches[reviewer.dispatch.dispatch_id].state.value == "FORWARDED"


def test_executor_terminal_failure_makes_run_failed(project: FixtureProject) -> None:
    store, workflow, executor = _prepare_executor(project, max_executor_attempts=1)

    record, generation, _forwarding = workflow.apply_executor_result(
        executor,
        parse_executor_result(_executor_result(executor, "failed")),
    )
    persisted, persisted_generation = store.load_run(record.run_id)

    assert record.state is RunStatus.FAILED
    assert record.steps["prepare-fixture"].state is StepStatus.FAILED
    assert record.operator_request is None
    assert persisted == record
    assert persisted_generation == generation


def test_executor_blocked_policy_exhaustion_makes_run_failed(project: FixtureProject) -> None:
    _store_value, workflow, executor = _prepare_executor(project, max_executor_attempts=1)

    record, _generation, _forwarding = workflow.apply_executor_result(
        executor,
        parse_executor_result(_executor_result(executor, "blocked")),
    )

    assert record.state is RunStatus.FAILED
    assert record.steps["prepare-fixture"].state is StepStatus.FAILED


def test_reviewer_changes_requested_retry_exhaustion_makes_run_failed(
    project: FixtureProject,
) -> None:
    _store_value, workflow, reviewer = _prepare_reviewer(
        project,
        max_executor_attempts=1,
    )

    record, _generation, _forwarding = workflow.apply_reviewer_result(
        reviewer,
        parse_reviewer_result(_reviewer_result(reviewer, "changes_requested")),
    )

    assert record.state is RunStatus.FAILED
    assert record.steps["prepare-fixture"].state is StepStatus.FAILED


def test_reviewer_blocked_retry_exhaustion_makes_run_failed(project: FixtureProject) -> None:
    _store_value, workflow, reviewer = _prepare_reviewer(
        project,
        max_reviewer_attempts=1,
    )

    record, _generation, _forwarding = workflow.apply_reviewer_result(
        reviewer,
        parse_reviewer_result(_reviewer_result(reviewer, "blocked")),
    )

    assert record.state is RunStatus.FAILED
    assert record.steps["prepare-fixture"].state is StepStatus.FAILED


def test_reviewer_inconclusive_result_makes_run_failed(project: FixtureProject) -> None:
    _store_value, workflow, reviewer = _prepare_reviewer(
        project,
        max_reviewer_attempts=1,
    )

    record, _generation, _forwarding = workflow.apply_reviewer_result(
        reviewer,
        parse_reviewer_result(_reviewer_result(reviewer, "inconclusive")),
    )

    assert record.state is RunStatus.FAILED
    assert record.steps["prepare-fixture"].state is StepStatus.FAILED


def test_failed_step_and_run_persist_atomically_with_monotonic_sequence(
    project: FixtureProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, workflow, active, generation = _activate_ready_run(project)
    request_event = _event(active.sequence + 1)
    request = OperatorRequest(
        request_id="request-terminal-failure",
        question="Resolve the fixture gate?",
        allowed_answers=["answer", "halt"],
        context_ref="prepare-fixture",
        resume_to=RunStatus.RUNNING,
        expires_at=None,
        required_role=None,
        kind="underspecification",
        step_id="prepare-fixture",
    )
    waiting = transition_run(
        active,
        RunStatus.WAITING_OPERATOR,
        request_event,
        operator_request=request,
    )
    generation = store.save_run(waiting, expected_generation=generation)
    failed_event = _event(waiting.sequence + 1)
    failed_step = transition_step(
        waiting.steps["prepare-fixture"],
        StepStatus.FAILED,
        failed_event,
    )
    saved_records: list[RunRecord] = []
    original_save = store.save_run

    def capture_save(record: RunRecord, **kwargs: Any) -> int:
        saved_records.append(record)
        return original_save(record, **kwargs)

    monkeypatch.setattr(store, "save_run", capture_save)

    failed, next_generation = workflow._replace_step(waiting, generation, failed_step)

    assert saved_records == [failed]
    assert failed.state is RunStatus.FAILED
    assert failed.steps["prepare-fixture"].state is StepStatus.FAILED
    assert failed.operator_request is None
    assert failed.steps["prepare-fixture"].last_event.sequence == waiting.sequence + 1
    assert failed.sequence == failed.steps["prepare-fixture"].last_event.sequence + 1
    assert next_generation == generation + 1
    assert store.load_run(failed.run_id) == (failed, next_generation)


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


def test_durable_malformed_reviewer_retry_does_not_infer_reconciliation_approval(
    project: FixtureProject,
) -> None:
    store, workflow, reviewer = _prepare_reviewer(project, max_reviewer_attempts=2)
    failed, generation = workflow.fail_dispatch(
        reviewer,
        reason="reviewer response was malformed",
        failure_category="result_validation",
        failure_detail="accepted review cannot require remediation",
    )
    failed_dispatch = failed.dispatches[reviewer.dispatch.dispatch_id]
    failed_payload = store.load_dispatch_payload(failed.run_id, failed_dispatch.dispatch_id)

    retry = workflow.prepare_pending_reviewer_result_validation_retry(failed, generation)

    assert retry == (failed, generation)
    persisted, persisted_generation = store.load_run(failed.run_id)
    repeated = workflow.prepare_pending_reviewer_result_validation_retry(persisted, persisted_generation)

    assert repeated == (persisted, persisted_generation)
    assert persisted.state is RunStatus.WAITING_OPERATOR
    assert persisted.operator_request is not None
    assert persisted.operator_request.kind == "reconciliation"
    assert persisted.dispatches[failed_dispatch.dispatch_id] == failed_dispatch
    assert store.load_dispatch_payload(failed.run_id, failed_dispatch.dispatch_id) == failed_payload
    assert len(persisted.dispatches) == 2


def test_abandoned_reviewer_result_validation_attempt_recovers_after_prelaunch_crash(
    project: FixtureProject,
) -> None:
    store, workflow, _executor, record, generation = _review_ready(project)
    reviewer = workflow.prepare_from_supervisor(
        record.run_id,
        expected_generation=generation,
        supervisor_text=_dispatch_command(role="reviewer"),
    )
    assert isinstance(reviewer, PreparedDispatch)

    failed, generation = workflow.fail_dispatch(
        reviewer,
        reason="reviewer response validation interrupted before launch",
        failure_category="result_validation",
        failure_detail="crash before the reviewer session launched",
    )
    failed_dispatch = failed.dispatches[reviewer.dispatch.dispatch_id]

    retry = workflow.prepare_pending_reviewer_result_validation_retry(failed, generation)

    assert failed.state is RunStatus.RUNNING
    assert failed_dispatch.state is DispatchStatus.ABANDONED
    assert isinstance(retry, PreparedDispatch)
    assert retry.dispatch.dispatch_id != failed_dispatch.dispatch_id
    assert retry.dispatch.attempt == failed_dispatch.attempt + 1
    assert store.load_dispatch_payload(failed.run_id, failed_dispatch.dispatch_id).result is None


@pytest.mark.parametrize(
    ("on_exhausted", "expected_run_state", "expected_step_state", "request_kind"),
    [
        ("ask", RunStatus.WAITING_OPERATOR, StepStatus.REVIEW_REQUIRED, "stall_recovery"),
        ("halt", RunStatus.HALTED, StepStatus.REVIEW_REQUIRED, None),
        ("fail", RunStatus.FAILED, StepStatus.FAILED, None),
    ],
)
@pytest.mark.parametrize("exhaustion", ["reviewer_attempts", "stall_limit"])
def test_safe_reviewer_result_validation_exhaustion_honors_stall_policy(
    project: FixtureProject,
    on_exhausted: str,
    expected_run_state: RunStatus,
    expected_step_state: StepStatus,
    request_kind: str | None,
    exhaustion: str,
) -> None:
    values = config_values(project)
    values["execution"]["stall_policy"]["on_exhausted"] = on_exhausted
    if exhaustion == "stall_limit":
        values["execution"]["stall_policy"]["maximum_retries_per_step"] = 0
    configured = replace(project, config=write_config(project, values))
    store, workflow, reviewer = _prepare_reviewer(
        configured,
        max_reviewer_attempts=1 if exhaustion == "reviewer_attempts" else 2,
    )
    failed, generation = workflow.record_reviewer_result_validation_failure(
        reviewer,
        reason="reviewer response was malformed",
        failure_detail="accepted review cannot require remediation",
    )

    exhausted = workflow.prepare_pending_reviewer_result_validation_retry(failed, generation)

    assert isinstance(exhausted, tuple)
    persisted, _generation = store.load_run(failed.run_id)
    assert persisted.state is expected_run_state
    assert persisted.steps[reviewer.dispatch.step_id].state is expected_step_state
    assert len(persisted.dispatches) == 2
    if request_kind is None:
        assert persisted.operator_request is None
    else:
        assert persisted.operator_request is not None
        assert persisted.operator_request.kind == request_kind


@pytest.mark.parametrize("invalid_field", ["review_target", "repository_before", "source_ledger"])
def test_durable_malformed_reviewer_retry_rejects_invalid_immutable_context(
    project: FixtureProject,
    invalid_field: str,
) -> None:
    store, workflow, reviewer = _prepare_reviewer(project, max_reviewer_attempts=2)
    failed, generation = workflow.fail_dispatch(
        reviewer,
        reason="reviewer response was malformed",
        failure_category="result_validation",
        failure_detail="accepted review cannot require remediation",
    )
    payload = store.load_dispatch_payload(failed.run_id, reviewer.dispatch.dispatch_id)
    metadata = dict(payload.session_metadata or {})
    repository_before = payload.repository_before
    prompt = payload.prompt
    dispatch = failed.dispatches[reviewer.dispatch.dispatch_id]
    if invalid_field == "review_target":
        review_target = dict(metadata["review_target"])
        review_target["executor_attempt"] = 99
        metadata["review_target"] = review_target
    elif invalid_field == "repository_before":
        repository_before = dict(repository_before or {})
        repository_before["manifest_sha256"] = "f" * 64
    else:
        prompt_context = json.loads(prompt)
        prompt_context["authoritative_sources"] = []
        prompt = json.dumps(prompt_context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        dispatch = dispatch.model_copy(
            update={
                "intent": dispatch.intent.model_copy(
                    update={"prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
                )
            }
        )
        failed = failed.model_copy(
            update={
                "dispatches": {**failed.dispatches, dispatch.dispatch_id: dispatch},
            }
        )
    generation = store.save_run(
        failed,
        expected_generation=generation,
        dispatch_payloads={
            reviewer.dispatch.dispatch_id: DispatchPayload(
                prompt=prompt,
                policy=payload.policy,
                result=payload.result,
                authoritative_verification=payload.authoritative_verification,
                forwarding_payload=payload.forwarding_payload,
                process_id=payload.process_id,
                session_metadata=metadata,
                repository_before=repository_before,
                repository_after=payload.repository_after,
            )
        },
    )
    request = failed.operator_request
    assert request is not None
    reconciled, generation = store.answer_operator_request(
        run_id=failed.run_id,
        expected_generation=generation,
        request_id=request.request_id,
        answer="reconcile",
        actor_id="fixture-operator",
    )

    waiting = workflow.prepare_pending_reviewer_result_validation_retry(reconciled, generation)

    assert isinstance(waiting, tuple)
    persisted, _generation = store.load_run(failed.run_id)
    assert persisted.state is RunStatus.WAITING_OPERATOR
    assert persisted.steps[reviewer.dispatch.step_id].state is StepStatus.REVIEW_REQUIRED
    assert len(persisted.dispatches) == 2


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
                workflow.mark_running(
                    current,
                    process_id=5000 + attempt,
                    process_create_time=float(5000 + attempt),
                ),
                runtime_session_id=f"session-stall-{attempt}",
            )

    assert record is not None
    assert record.state.value == "WAITING_OPERATOR"
    assert record.operator_request is not None
    assert record.operator_request.kind == "stall_recovery"
    assert record.steps["prepare-fixture"].stalls == 3
    assert record.steps["prepare-fixture"].state is StepStatus.BLOCKED


def test_stall_exhaustion_configured_to_fail_makes_run_failed(
    project: FixtureProject,
) -> None:
    values = config_values(project)
    values["execution"]["stall_policy"]["maximum_retries_per_step"] = 0
    values["execution"]["stall_policy"]["on_exhausted"] = "fail"
    configured = replace(project, config=write_config(project, values))
    _store_value, workflow, prepared = _prepare_executor(
        configured,
        max_executor_attempts=2,
    )

    record, _generation, retry_allowed = workflow.handle_stall(
        prepared,
        category="timeout",
        reason="terminal synthetic timeout",
    )

    assert retry_allowed is False
    assert record.state is RunStatus.FAILED
    assert record.steps["prepare-fixture"].state is StepStatus.FAILED
    assert record.operator_request is None


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

    running = workflow.mark_running(prepared, process_id=4321, process_create_time=4321.0)

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


def test_worker_and_bootstrap_prompts_list_exact_role_mcp_tools(tmp_path: Path) -> None:
    project = create_fixture_project(tmp_path)
    values = config_values(project)
    values["mcp"] = {
        "environment_passthrough": [],
        "servers": {
            "fixture": {
                "type": "local",
                "enabled": True,
                "command": ["/usr/bin/fixture-mcp"],
                "environment": {},
            }
        },
    }
    values["roles"]["executors"]["terra"]["mcp_tools"] = ["fixture_echo"]
    values["roles"]["reviewers"]["reviewer"]["mcp_tools"] = ["fixture_echo", "fixture_probe"]
    values["roles"]["supervisor"]["supervisor"]["mcp_tools"] = ["fixture_echo"]
    config = write_config(project, values)
    object.__setattr__(project, "config", config)

    store, workflow, executor = _prepare_executor(project, review_required=True)
    executor_prompt = json.loads(executor.prompt)
    assert executor_prompt["research_tools"]["mcp"] == ["fixture_echo"]

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
    reviewer_prompt = json.loads(reviewer.prompt)
    assert reviewer_prompt["observation_tools"]["mcp"] == ["fixture_echo", "fixture_probe"]

    bootstrap, _path = workflow.render_bootstrap(record.run_id)
    assert "`fixture_echo`" in bootstrap
    assert "MCP tools: none." not in bootstrap


def test_empty_role_mcp_tools_render_an_explicit_none_statement(tmp_path: Path) -> None:
    _store, workflow, _record, _generation = _activate_ready_run(
        create_fixture_project(tmp_path)
    )
    bootstrap, _path = workflow.render_bootstrap("fixture-run")
    assert "MCP tools: none." in bootstrap


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
    first = workflow.record_session_id(
        workflow.mark_running(first, process_id=1235, process_create_time=1235.0),
        runtime_session_id="ses-one",
    )
    first_result = parse_reviewer_result(
        {
            "result_version": 1,
            "response_contract": "dispatcher.reviewer_result.v1",
            "dispatch_id": first.dispatch.dispatch_id,
            "attempt": first.dispatch.attempt,
            "step_id": "prepare-fixture",
            "repo_id": "fixture-repo",
            "review_target": first.review_target.model_dump(mode="json"),
            "findings": [],
            "verification": [{"check_id": "fixture-check", "status": "passed", "summary": "passed"}],
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
    second = workflow.record_session_id(
        workflow.mark_running(second, process_id=1236, process_create_time=1236.0),
        runtime_session_id="ses-two",
    )
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
    first = workflow.record_session_id(
        workflow.mark_running(first, process_id=1235, process_create_time=1235.0),
        runtime_session_id="ses-one",
    )
    accepted = parse_reviewer_result(
        {
            "result_version": 1,
            "response_contract": "dispatcher.reviewer_result.v1",
            "dispatch_id": first.dispatch.dispatch_id,
            "attempt": first.dispatch.attempt,
            "step_id": "prepare-fixture",
            "repo_id": "fixture-repo",
            "review_target": first.review_target.model_dump(mode="json"),
            "findings": [],
            "verification": [{"check_id": "fixture-check", "status": "passed", "summary": "passed"}],
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
        workflow.mark_running(second, process_id=1236, process_create_time=1236.0),
        runtime_session_id="ses-two",
    )
    changes_requested = parse_reviewer_result(
        {
            "result_version": 1,
            "response_contract": "dispatcher.reviewer_result.v1",
            "dispatch_id": second.dispatch.dispatch_id,
            "attempt": second.dispatch.attempt,
            "step_id": "prepare-fixture",
            "repo_id": "fixture-repo",
            "review_target": second.review_target.model_dump(mode="json"),
            "findings": [],
            "verification": [{"check_id": "fixture-check", "status": "passed", "summary": "passed"}],
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
        workflow.mark_running(executor, process_id=1234, process_create_time=1234.0),
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
        workflow.mark_running(reviewer, process_id=1235, process_create_time=1235.0),
        runtime_session_id="ses-reviewer",
    )
    changes = parse_reviewer_result(
        {
            "result_version": 1,
            "response_contract": "dispatcher.reviewer_result.v1",
            "dispatch_id": reviewer.dispatch.dispatch_id,
            "attempt": reviewer.dispatch.attempt,
            "step_id": "prepare-fixture",
            "repo_id": "fixture-repo",
            "review_target": reviewer.review_target.model_dump(mode="json"),
            "findings": [],
            "verification": [{"check_id": "fixture-check", "status": "passed", "summary": "passed"}],
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
    running = workflow.mark_running(prepared, process_id=1235, process_create_time=1235.0)
    reviewer = workflow.record_session_id(running, runtime_session_id="ses-reviewer")
    review_result = parse_reviewer_result(
        {
            "result_version": 1,
            "response_contract": "dispatcher.reviewer_result.v1",
            "dispatch_id": reviewer.dispatch.dispatch_id,
            "attempt": reviewer.dispatch.attempt,
            "step_id": "prepare-fixture",
            "repo_id": "fixture-repo",
            "review_target": reviewer.review_target.model_dump(mode="json"),
            "findings": [],
            "verification": [{"check_id": "fixture-check", "status": "passed", "summary": "passed"}],
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
    running = workflow.mark_running(prepared, process_id=1235, process_create_time=1235.0)
    reviewer = workflow.record_session_id(running, runtime_session_id="ses-reviewer")
    review_result = parse_reviewer_result(
        {
            "result_version": 1,
            "response_contract": "dispatcher.reviewer_result.v1",
            "dispatch_id": reviewer.dispatch.dispatch_id,
            "attempt": reviewer.dispatch.attempt,
            "step_id": "prepare-fixture",
            "repo_id": "fixture-repo",
            "review_target": reviewer.review_target.model_dump(mode="json"),
            "findings": [],
            "verification": [{"check_id": "fixture-check", "status": "passed", "summary": "passed"}],
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
        workflow.mark_running(rework, process_id=1236, process_create_time=1236.0),
        runtime_session_id="ses-executor-rework",
    )
    record, _generation, _forwarding = workflow.apply_executor_result(
        rework,
        parse_executor_result(_executor_result(rework)),
    )

    assert record.steps["prepare-fixture"].state is StepStatus.REVIEW_REQUIRED
    assert record.steps["prepare-fixture"].review_acceptances == 0
    assert record.steps["prepare-fixture"].accepted_reviewer_role_keys == []
