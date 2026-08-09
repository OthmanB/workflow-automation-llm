"""Main orchestration loop.

The loop drives one supervisor-driven message cycle:
  supervisor → parse envelope → dispatch to slave → capture response →
  forward to supervisor → repeat until done/halt/ask.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Callable

from . import audit, forward
from . import state as state_mod
from .config import Config
from .dispatch import Route, RouteError, route_from_command
from .protocol import ProtocolError, diagnostic_command_hint, parse_supervisor_command
from .sessions import OpenCodeSessionError, SessionResult, validate_session_reference
from .sessions import run_session as _real_run_session

logger = logging.getLogger(__name__)

RunSessionFn = Callable[..., SessionResult]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """Owns the state and drives the loop."""

    def __init__(
        self,
        config: Config,
        *,
        run_session: RunSessionFn | None = None,
        resume: bool = False,
    ) -> None:
        self.config = config
        self._run_session: RunSessionFn = run_session or _real_run_session
        self.state_dir = config.state_dir
        self._state = state_mod.load_state(self.state_dir)
        self._sessions = state_mod.load_sessions(self.state_dir)

        if resume:
            logger.info("resuming run from %s", self.state_dir)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Run the loop until done, halt, or an unrecoverable error.

        Returns 0 on clean completion, 1 on halt, 2 on unrecoverable error.
        """
        try:
            self._bootstrap_or_resume()
            result = self._loop()
            return result
        except KeyboardInterrupt:
            logger.warning("interrupted — state saved, resume with --resume")
            self._persist()
            return 1
        except Exception as exc:
            logger.exception("unrecoverable error: %s", exc)
            audit.halt(self.state_dir, str(exc))
            self._persist()
            return 2

    # ------------------------------------------------------------------
    # Bootstrap / resume
    # ------------------------------------------------------------------

    def _bootstrap_or_resume(self) -> None:
        sup_sessions = self._sessions.get("supervisor", {})
        sup_key = self.config.supervisor_key
        sup_info = sup_sessions.get(sup_key, {})
        sup_id = sup_info.get("session_id", "")

        if sup_id:
            if self._run_session is _real_run_session:
                validate_session_reference(
                    session_id=sup_id,
                    registry_entry=sup_info,
                    workdir=self.config.default_repository.root,
                )

        if not sup_id:
            self._bootstrap()
        else:
            self._resume(sup_id)

    def _bootstrap(self) -> None:
        """Build the supervisor bootstrap message and set it as the
        pending forward so the first _supervisor_turn uses it."""
        logger.info("bootstrapping supervisor ...")
        prompt = self._render_bootstrap()
        self._pending_forward = prompt
        self._state["project"] = self.config.project_name
        self._state["current_step"] = ""
        self._persist()

    def _resume(self, sup_id: str) -> None:
        """Build the resume message and set it as the pending forward."""
        logger.info("resuming supervisor session %s ...", sup_id)
        prompt = self._render_resume()
        self._pending_forward = prompt
        self._persist()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self) -> int:
        max_rounds = self.config.execution.max_rounds_per_step
        round_count = 0

        while round_count < max_rounds:
            round_count += 1
            logger.info("=== loop round %d ===", round_count)

            # --- A. Get supervisor decision ---
            route = self._supervisor_turn()
            if route is None:
                # Parse failure or ambiguous — halt.
                self._halt("could not parse supervisor decision")
                return 1

            logger.info("route: kind=%s target=%s mode=%s step=%s",
                        route.kind, route.target, route.mode, route.step)

            # --- B. Handle terminal routes ---
            if route.kind == "completion":
                self._halt(
                    "completion requests remain unavailable until the Phase 4 "
                    "dispatcher-owned completion guard is wired to durable state"
                )
                return 1

            if route.kind == "halt":
                reason = route.prompt_body[:200]
                logger.warning("supervisor says halt: %s", reason)
                audit.halt(self.state_dir, reason)
                self._persist()
                return 1

            if route.kind == "ask":
                question = route.prompt_body
                logger.info("supervisor asks: %s", question[:200])
                if self.config.execution.underspec_mode != "ask":
                    logger.warning(
                        "supervisor asked a question but underspec_mode is %s "
                        "— treating as halt",
                        self.config.execution.underspec_mode,
                    )
                    self._halt(f"question in non-ask mode: {question[:200]}")
                    return 1
                self._persist()
                print(f"\n{'='*60}")
                print(f"Supervisor asks:\n{question}")
                print(f"{'='*60}")
                answer = input("Your answer (or 'halt' to stop): ").strip()
                if answer.lower() == "halt":
                    self._halt("operator halted on question")
                    return 1
                audit.operator_decision(self.state_dir, question, answer)
                # Feed the answer back to the supervisor in the context message.
                route = self._supervisor_turn(
                    extra_context=f"Operator answer:\n{answer}"
                )
                if route is None:
                    self._halt("could not parse supervisor response to answer")
                    return 1
                # Continue processing the new route below.
                if route.kind in ("done", "halt"):
                    # Recurse: handle the terminal and return.
                    return self._loop()  # will re-enter loop with new route

            # --- C. Dispatch to slave ---
            role_pool = "executors" if route.kind == "executor" else "reviewers"
            model = self._model_for(role_pool, route.target)
            variant = self._variant_for(role_pool, route.target)

            session_id = ""
            mode = route.mode
            if mode in ("resume", "fork"):
                # Look up stored session for this role.
                stored = state_mod.session_registry_get(
                    self._sessions, role_pool, route.target
                )
                session_id = stored.get("session_id", "")
                if not session_id:
                    raise OpenCodeSessionError(
                        f"mode={mode} requires a persisted session for {role_pool}.{route.target}"
                    )
                if self._run_session is _real_run_session:
                    validate_session_reference(
                        session_id=session_id,
                        registry_entry=stored,
                        workdir=self.config.default_repository.root,
                    )

            title = (
                f"{route.target} · {route.step} review"
                if route.kind == "reviewer"
                else f"{route.target} · {route.step}"
            )

            snapshot_dirs = [str(path) for path in self.config.evidence_dirs]

            logger.info("dispatching to %s/%s  mode=%s  step=%s",
                         role_pool, route.target, route.mode, route.step)

            # Phase 1 keeps permission enforcement unavailable and real execution blocked.
            auto = False

            dispatch_hash = hashlib.sha1(
                route.prompt_body.encode()
            ).hexdigest()[:12]

            audit.dispatch_sent(
                self.state_dir, route.kind, route.target,
                route.step, mode,
                session_id or "<new>", dispatch_hash,
            )

            result = self._run_session(
                prompt=route.prompt_body,
                model=model,
                variant=variant,
                session_id=session_id if mode in ("resume", "fork") else None,
                mode=mode,
                workdir=self.config.default_repository.root,
                title=title,
                auto_approve=auto,
                timeout_seconds=self.config.execution.timeout_seconds,
                termination_grace_seconds=self.config.execution.termination_grace_seconds,
                max_output_bytes=self.config.execution.max_output_bytes,
                state_dir=self.state_dir,
                snapshot_dirs=snapshot_dirs,
            )

            audit.response_received(
                self.state_dir, route.kind, route.target,
                route.step, result.session_id,
                result.evidence_written, result.usage,
                result.exit_code,
            )

            # Record session for future resume.
            state_mod.session_registry_set(
                self._sessions, role_pool, route.target,
                session_id=result.session_id,
                model=model, variant=variant,
                working_directory=str(self.config.default_repository.root),
                opencode_version=result.opencode_version,
                parent_session_id=result.parent_session_id,
            )
            self._state["current_step"] = route.step

            # Save transcript.
            self._save_transcript(
                f"dispatch-{route.kind}-{dispatch_hash}",
                f"supervisor -> {route.target}\n\n{route.prompt_body}",
            )
            self._save_transcript(
                f"response-{route.kind}-{dispatch_hash}",
                f"{route.target} -> supervisor\n\n{result.chat_response}",
            )

            self._persist()

            # --- D. Forward to supervisor ---
            fwd = forward.render_forwarding_message(
                template=self.config.execution.response_template,
                role_display=route.kind.capitalize(),
                model_display=self.config.role_display(route.target),
                step=route.step,
                session_id=result.session_id,
                chat_response=result.chat_response,
                evidence=result.evidence_written,
                context_pct=result.usage.get("context_pct", 0.0),
                tokens_used=result.usage.get("total_tokens", 0),
            )

            # The forward is done by feeding it into the NEXT supervisor_turn
            # at the top of the loop — the last forward message becomes the
            # supervisor's input on the next iteration.

            # Store the forwarding message as the pending inbox.
            self._pending_forward = fwd

        logger.error("max rounds (%d) exhausted", max_rounds)
        self._halt("max rounds exhausted")
        return 1

    # ------------------------------------------------------------------
    # Supervisor turn helper
    # ------------------------------------------------------------------

    def _supervisor_turn(self, extra_context: str = "") -> Route | None:
        """Run one supervisor turn and parse its dispatch envelope.

        The forwarding message from the previous slave response (or the
        initial bootstrap context) is sent as the prompt.
        """
        sup_key = self.config.supervisor_key
        sup_id = self._sessions.get("supervisor", {}).get(sup_key, {}).get(
            "session_id", ""
        )

        # Build the prompt from the pending forward or extra context.
        prompt = ""
        if hasattr(self, "_pending_forward") and self._pending_forward:
            prompt = self._pending_forward
            del self._pending_forward
        if extra_context:
            prompt = (prompt + "\n\n" + extra_context).strip()

        if not prompt:
            # First turn — no forward yet; dispatch an empty prompt to get
            # the supervisor's initial decision.
            prompt = "Reply with your first dispatch decision."

        result = self._run_session(
            prompt=prompt,
            model=self.config.supervisor_model(),
            variant=self.config.supervisor_variant(),
            session_id=sup_id if sup_id else None,
            mode="resume" if sup_id else "new",
            workdir=self.config.default_repository.root,
            title=f"supervisor · {self._state.get('current_step', 'init')}",
            auto_approve=False,
            timeout_seconds=self.config.execution.timeout_seconds,
            termination_grace_seconds=self.config.execution.termination_grace_seconds,
            max_output_bytes=self.config.execution.max_output_bytes,
            state_dir=self.state_dir,
        )

        self._record_supervisor(result)
        self._save_transcript(
            "supervisor-turn",
            f"supervisor -> dispatcher\n\n{result.chat_response}",
        )

        # Parse exactly one schema-v1 JSON command. Natural language is diagnostic-only.
        text = result.chat_response
        try:
            command = parse_supervisor_command(text)
            return route_from_command(command, self.config)
        except (ProtocolError, RouteError) as exc:
            logger.error("could not parse supervisor command: %s", exc)
            print(f"\n{'!'*60}")
            print(diagnostic_command_hint(text))
            print(f"\n{'!'*60}")
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _record_supervisor(self, result: SessionResult) -> None:
        sup_key = self.config.supervisor_key
        state_mod.session_registry_set(
            self._sessions, "supervisor", sup_key,
            session_id=result.session_id,
            model=self.config.supervisor_model(),
            variant=self.config.supervisor_variant(),
            working_directory=str(self.config.default_repository.root),
            opencode_version=result.opencode_version,
            parent_session_id=result.parent_session_id,
        )

    def _model_for(self, pool: str, key: str) -> str:
        expected_kind = "executor" if pool == "executors" else "reviewer"
        if self.config.role_kind(key) != expected_kind:
            raise ValueError(f"role {key} is not configured as a {expected_kind}")
        return self.config.role(key).model

    def _variant_for(self, pool: str, key: str) -> str:
        expected_kind = "executor" if pool == "executors" else "reviewer"
        if self.config.role_kind(key) != expected_kind:
            raise ValueError(f"role {key} is not configured as a {expected_kind}")
        return self.config.role(key).variant

    def _save_transcript(self, label: str, content: str) -> None:
        try:
            path = state_mod.save_transcript(self.state_dir, label, content)
            logger.debug("transcript saved: %s", path)
        except Exception:
            logger.warning("failed to save transcript", exc_info=True)

    def _halt(self, reason: str) -> None:
        logger.warning("HALT: %s", reason)
        audit.halt(self.state_dir, reason)
        self._persist()

    def _persist(self) -> None:
        try:
            state_mod.save_state(self.state_dir, self._state)
            state_mod.save_sessions(self.state_dir, self._sessions)
        except Exception:
            logger.warning("failed to persist state", exc_info=True)

    def _render_bootstrap(self) -> str:
        """Render the supervisor bootstrap message from the template."""
        try:
            template_path = (
                Path(__file__).resolve().parent.parent.parent
                / "templates" / "bootstrap_supervisor.md"
            )
            if template_path.exists():
                template = template_path.read_text(encoding="utf-8")
            else:
                template = _FALLBACK_BOOTSTRAP
        except Exception:
            template = _FALLBACK_BOOTSTRAP

        cfg = self.config
        return template.format(
            project_name=cfg.project_name,
            repositories_summary=self._repositories_summary(),
            specifications_dir=cfg.model.sources.specifications_dir,
            plans_dir=cfg.model.sources.plans_dir,
            evidence_dir=cfg.evidence_dirs[0],
            profile_mode=cfg.profile_id,
            roles_summary=self._roles_summary(),
        )

    def _render_resume(self) -> str:
        """Render the supervisor resume message."""
        s = self._state
        return (
            f"Resuming {self.config.project_name} run at step {s.get('current_step', '?')}.\n"
            f"Last decision: {s.get('last_decision_hash', '?')}\n"
            f"Last response: {s.get('last_response_hash', '?')}\n"
            f"\nContinue from where the plan left off. Reply with a dispatch envelope."
        )

    def _roles_summary(self) -> str:
        lines = ["| Role | Key | Model |"]
        lines.append("|---|---|---|")
        for pool_name, label in [
            ("supervisor", "Supervisor"),
            ("executors", "Executor"),
            ("reviewers", "Reviewer"),
        ]:
            pool = getattr(self.config.model.roles, pool_name)
            for key, defn in pool.items():
                lines.append(
                    f"| {label} | {key} | {defn.model} ({defn.variant}) |"
                )
        return "\n".join(lines)

    def _repositories_summary(self) -> str:
        lines = []
        for repo_id, repository in self.config.model.repositories.items():
            lines.append(f"- `{repo_id}`: `{repository.root}` ({repository.default_branch})")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fallback bootstrap template (used when no template file is present)
# ---------------------------------------------------------------------------

_FALLBACK_BOOTSTRAP = """\
You are the **supervisor** for project "{project_name}".

Registered repositories:
{repositories_summary}
Specifications: {specifications_dir}
Plan directory: {plans_dir}
Evidence directory: {evidence_dir}
Profile: {profile_mode}

Available roles:
{roles_summary}

## Your task

Read the specifications and plan, then direct one executor or reviewer at a
time by replying with exactly one schema-v1 JSON command:

```json
{{"protocol_version":1,"action":"dispatch","step_id":"<step-id>","target_role":"<role-key>","session_mode":"new","prompt":"<task prompt>"}}
```

To request completion evaluation, reply with:

```json
{{"protocol_version":1,"action":"request_completion"}}
```

**Evidence convention:** supervisor decisions go to
`{evidence_dir}/<step>-supervisor-go.md`; executor handoffs to
`{evidence_dir}/<step>-<agent>-handoff.md`; reviews to
`{evidence_dir}/<step>-<reviewer>-review.md`.

**Discipline:** keep each turn short; details live in the .md files, so your
session does not bloat. Every reply must be one JSON command object.

Reply with your first dispatch decision.
"""
