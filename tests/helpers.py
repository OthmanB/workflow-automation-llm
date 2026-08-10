from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from dispatcher.config import Config, load_config


@dataclass(frozen=True)
class FixtureProject:
    root: Path
    config_path: Path
    repository: Path
    specifications: Path
    plans: Path
    evidence: Path
    state: Path
    profiles_path: Path
    config: Config


def create_fixture_project(
    tmp_path: Path,
    *,
    include_preflight: bool = True,
    config_relative_paths: bool = False,
) -> FixtureProject:
    root = tmp_path / "project"
    root.mkdir()
    repository = root / "repository"
    specifications = root / "specifications"
    plans = root / "plans"
    evidence = repository / "evidence"
    state = root / "state"
    for path in (repository, specifications, plans, evidence):
        path.mkdir(parents=True, exist_ok=True)
    (specifications / "specification.md").write_text("fixture specification\n", encoding="utf-8")
    (plans / "plan.md").write_text("fixture plan\n", encoding="utf-8")
    (plans / "roles.md").write_text("fixture roles\n", encoding="utf-8")
    _initialize_git_repository(repository, "https://example.invalid/fixture.git")

    profiles_path = root / "profiles.yaml"
    profiles_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "profiles": {
                    "balanced": {
                        "review_schedule": "critical",
                        "multi_review": "on_critical_only",
                        "reviewer_role_keys": ["reviewer", "reviewer-two"],
                        "required_acceptances": 2,
                    }
                },
                "default": "balanced",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config_path = root / "project.yaml"
    values: dict[str, Any] = {
        "schema_version": 1,
        "project": {
            "project_id": "fixture-project",
            "name": "Fixture Project",
            "description": "Schema-v1 fixture project",
        },
        "sources": {
            "specifications_dir": str(specifications),
            "plans_dir": str(plans),
            "plan_files": ["plan.md"],
            "roles_files": ["roles.md"],
        },
        "state": {
            "directory": str(state),
            "lease_heartbeat_seconds": 30,
            "lease_stale_after_seconds": 120,
        },
        "repositories": {
            "fixture-repo": {
                "root": str(repository),
                "expected_remote": {
                    "name": "origin",
                    "url": "https://example.invalid/fixture.git",
                },
                "default_branch": "main",
                "evidence_roots": ["evidence"],
                "writable_roots": ["."],
                "external_roots": [],
                "commit_policy": "required",
                "permission_policy": "repository",
                "allow_shared_writable_roots": False,
            }
        },
        "roles": {
            "supervisor": {
                "supervisor": {
                    "model": "fixture/supervisor",
                    "variant": "high",
                    "display": "Fixture Supervisor",
                    "permission_policy": "supervisor",
                }
            },
            "executors": {
                "terra": {
                    "model": "fixture/executor",
                    "variant": "high",
                    "display": "Fixture Executor",
                    "permission_policy": "executor",
                }
            },
            "reviewers": {
                "reviewer": {
                    "model": "fixture/reviewer",
                    "variant": "high",
                    "display": "Fixture Reviewer",
                    "permission_policy": "reviewer",
                },
                "reviewer-two": {
                    "model": "fixture/reviewer-two",
                    "variant": "high",
                    "display": "Fixture Second Reviewer",
                    "permission_policy": "reviewer",
                }
            },
        },
        "profile": {
            "profiles_file": str(profiles_path),
            "profile_id": "balanced",
        },
        "execution": {
            "mode": "mock_only",
            "protocol_version": 1,
            "scheduling": "sequential",
            "concurrency": {
                "max_active_dispatches": 1,
                "max_batch_size": 1,
                "role_capacities": {"terra": 1, "reviewer": 1, "reviewer-two": 1},
                "failure_mode": "wait_for_started",
            },
            "default_repo_id": "fixture-repo",
            "timeout_seconds": 60,
            "termination_grace_seconds": 5,
            "max_output_bytes": 65536,
            "max_rounds_per_step": 4,
            "halt_mode": "ask_on_ambiguity",
            "underspec_mode": "ask",
            "response_template": "[response_content_chat]",
        },
        "review_policy": {
            "mandatory_review": False,
            "critical_risk_tags": ["critical"],
            "allow_operator_waiver": False,
        },
        "budget": {
            "enabled": False,
            "max_run_cost_usd": 10.0,
            "max_step_cost_usd": 5.0,
            "max_context_tokens": 100000,
            "on_limit": "halt",
        },
        "observability": {
            "log_format": "json",
            "log_level": "INFO",
            "retention": {
                "mode": "archive",
                "archive_directory": str(root / "archive"),
                "max_transcripts_per_run": 100,
                "max_reports": 100,
                "max_audit_exports": 100,
                "max_support_bundles": 50,
                "max_archived_artifacts": 1000,
            },
        },
        "permission_policies": {
            "global_policy": "global",
            "project_policy": "project",
            "role_class_policies": {
                "supervisor": "supervisor-class",
                "executor": "executor-class",
                "reviewer": "reviewer-class",
            },
            "policies": {
                "global": {"default": "deny", "actions": {}},
                "project": {"default": "deny", "actions": {}},
                "repository": {
                    "default": "deny",
                    "actions": {"inspect": "allow", "modify": "allow", "verify": "allow"},
                },
                "supervisor-class": {"default": "deny", "actions": {"inspect": "allow"}},
                "executor-class": {
                    "default": "deny",
                    "actions": {"inspect": "allow", "modify": "allow", "verify": "allow"},
                },
                "reviewer-class": {
                    "default": "deny",
                    "actions": {"inspect": "allow", "modify": "deny", "verify": "allow"},
                },
                "supervisor": {"default": "deny", "actions": {}},
                "executor": {"default": "deny", "actions": {}},
                "reviewer": {"default": "deny", "actions": {}},
            },
        },
        "evidence": {
            "hash_algorithm": "sha256",
            "require_content_hashes": True,
            "immutable": True,
            "allow_unexpected_writes": False,
        },
    }
    if include_preflight:
        values["preflight"] = {
            "enabled": True,
            "models_smoke_test": True,
            "smoke_prompt": "Reply with exactly: OK",
            "credentials": [],
            "require_git_remote": True,
            "disk_space_min_mb": 1,
        }
    if config_relative_paths:
        values["sources"]["specifications_dir"] = "specifications"
        values["sources"]["plans_dir"] = "plans"
        values["state"]["directory"] = "state"
        values["profile"]["profiles_file"] = "profiles.yaml"
        values["repositories"]["fixture-repo"]["root"] = "repository"

    config_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    return FixtureProject(
        root=root,
        config_path=config_path,
        repository=repository,
        specifications=specifications,
        plans=plans,
        evidence=evidence,
        state=state,
        profiles_path=profiles_path,
        config=load_config(config_path),
    )


def write_config(project: FixtureProject, values: dict[str, Any]) -> Config:
    project.config_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    return load_config(project.config_path)


def config_values(project: FixtureProject) -> dict[str, Any]:
    return yaml.safe_load(project.config_path.read_text(encoding="utf-8"))


def valid_plan_values(project: FixtureProject) -> dict[str, Any]:
    source_hash = hashlib.sha256((project.plans / "plan.md").read_bytes()).hexdigest()
    return {
        "schema_version": 1,
        "plan_id": "fixture-plan",
        "sources": [
            {
                "source_id": "fixture-plan-source",
                "root": "plans",
                "relative_path": "plan.md",
                "sha256": source_hash,
                "media_type": "text/markdown",
            }
        ],
        "steps": [
            {
                "ordinal": 1,
                "step_id": "prepare-fixture",
                "title": "Prepare fixture",
                "repo_id": "fixture-repo",
                "depends_on": [],
                "required_inputs": [],
                "produced_outputs": [
                    {
                        "artifact_id": "fixture-output",
                        "producer_step_id": None,
                        "description": "Fixture output",
                    }
                ],
                "resource_locks": [{"resource_id": "fixture-resource", "mode": "write"}],
                "risk_tags": [],
                "authorization": {
                    "authorized_actions": ["inspect"],
                    "requires_operator_approval": False,
                },
                "acceptance_criteria": [
                    {"criterion_id": "fixture-check", "description": "Fixture check passes"}
                ],
                "evidence_requirements": [
                    {
                        "artifact_id": "fixture-evidence",
                        "relative_path": "fixture.md",
                        "media_type": "text/markdown",
                    }
                ],
                "review": {
                    "required": False,
                    "reviewer_role_keys": [],
                    "required_acceptances": 0,
                },
                "retry": {
                    "max_executor_attempts": 1,
                    "max_reviewer_attempts": 0,
                    "on_failed": "halt",
                    "on_blocked": "halt",
                    "on_changes_requested": "halt",
                    "escalation_role_key": None,
                },
            }
        ],
    }


def _initialize_git_repository(path: Path, remote_url: str) -> None:
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", remote_url],
        check=True,
        capture_output=True,
        text=True,
    )
