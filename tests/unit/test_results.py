from __future__ import annotations

from typing import Any

import pytest

from dispatcher.results import (
    ResultError,
    ResultExpectation,
    parse_executor_result,
    parse_reviewer_result,
    validate_executor_result_context,
    validate_reviewer_result_context,
)

_SHA = "a" * 64
_PATCH = "b" * 64


def _executor_result(outcome: str = "completed") -> dict[str, Any]:
    result: dict[str, Any] = {
        "result_version": 1,
        "dispatch_id": "dispatch-one",
        "attempt": 1,
        "step_id": "prepare-fixture",
        "repository": {
            "repo_id": "fixture-repo",
            "base_revision": "base-sha",
            "result_revision": "result-sha",
            "patch_sha256": None,
        },
        "evidence": [
            {
                "artifact_id": "fixture-evidence",
                "relative_path": "fixture.md",
                "sha256": _SHA,
                "media_type": "text/markdown",
                "size_bytes": 10,
            }
        ],
        "verification": [
            {"check_id": "fixture-check", "status": "passed", "summary": "passed"}
        ],
        "summary": "fixture result",
        "transcript_ref": "transcript-one",
        "outcome": outcome,
    }
    if outcome == "blocked":
        result["blockers"] = ["fixture blocker"]
    if outcome == "failed":
        result["failure_code"] = "fixture-failure"
    return result


def _review_target() -> dict[str, Any]:
    return {
        "executor_dispatch_id": "executor-dispatch",
        "executor_attempt": 1,
        "result_revision": "result-sha",
        "patch_sha256": None,
        "artifact_hashes": [_SHA],
    }


def _review_result(verdict: str = "accepted") -> dict[str, Any]:
    result: dict[str, Any] = {
        "result_version": 1,
        "dispatch_id": "review-dispatch",
        "attempt": 1,
        "step_id": "prepare-fixture",
        "repo_id": "fixture-repo",
        "review_target": _review_target(),
        "findings": [],
        "verification": [
            {"check_id": "review-check", "status": "passed", "summary": "passed"}
        ],
        "required_remediation": [],
        "summary": "fixture review",
        "transcript_ref": "review-transcript",
        "verdict": verdict,
    }
    if verdict == "changes_requested":
        result["required_remediation"] = ["repair fixture"]
    if verdict == "blocked":
        result["blockers"] = ["cannot inspect fixture"]
    if verdict == "inconclusive":
        result["reason"] = "missing fixture context"
    return result


def _executor_expectation() -> ResultExpectation:
    return ResultExpectation(
        dispatch_id="dispatch-one",
        attempt=1,
        step_id="prepare-fixture",
        repo_id="fixture-repo",
        expected_review_target=None,
    )


def test_executor_outcome_union_and_context_validation() -> None:
    for outcome in ("completed", "blocked", "failed"):
        result = parse_executor_result(_executor_result(outcome))

        validate_executor_result_context(result, _executor_expectation())


def test_blocked_executor_result_requires_blockers() -> None:
    values = _executor_result("blocked")
    values["blockers"] = []

    with pytest.raises(ResultError, match="blocked executor result requires blockers"):
        parse_executor_result(values)


def test_executor_result_rejects_stale_attempt_and_wrong_repository() -> None:
    result = parse_executor_result(_executor_result())
    expectation = _executor_expectation().model_copy(update={"attempt": 2})

    with pytest.raises(ResultError, match="attempt expected 2, received 1"):
        validate_executor_result_context(result, expectation)

    values = _executor_result()
    values["repository"]["repo_id"] = "other-repo"
    with pytest.raises(ResultError, match="repo_id expected 'fixture-repo'"):
        validate_executor_result_context(parse_executor_result(values), _executor_expectation())


def test_reviewer_verdict_union_and_immutable_target_validation() -> None:
    target = _review_target()
    expectation = ResultExpectation(
        dispatch_id="review-dispatch",
        attempt=1,
        step_id="prepare-fixture",
        repo_id="fixture-repo",
        expected_review_target=target,
    )
    for verdict in ("accepted", "changes_requested", "blocked", "inconclusive"):
        result = parse_reviewer_result(_review_result(verdict))

        validate_reviewer_result_context(result, expectation)


def test_reviewer_cannot_accept_blocking_findings_or_remediation() -> None:
    values = _review_result("accepted")
    values["findings"] = [
        {"finding_id": "blocking", "severity": "blocking", "summary": "blocking"}
    ]

    with pytest.raises(ResultError, match="accepted review cannot contain blocking findings"):
        parse_reviewer_result(values)


def test_reviewer_result_rejects_wrong_immutable_target() -> None:
    values = _review_result()
    values["review_target"]["patch_sha256"] = _PATCH
    values["review_target"]["result_revision"] = None
    result = parse_reviewer_result(values)
    expectation = ResultExpectation(
        dispatch_id="review-dispatch",
        attempt=1,
        step_id="prepare-fixture",
        repo_id="fixture-repo",
        expected_review_target=_review_target(),
    )

    with pytest.raises(ResultError, match="target does not match"):
        validate_reviewer_result_context(result, expectation)
