# Code Discipline Compliance Review

**Review date:** 2026-08-13  
**Rules reviewed:** `.github/copilot-instructions.md`  
**Scope:** Current working-tree implementation, configuration, documentation, and CI definitions. This is a static compliance review; the test suite was not rerun because it was reported passing.

## Verdict

The project substantially follows the strict-configuration and secret-handling disciplines, but it does not fully comply with the documented code discipline. No critical or high-severity departure was identified. Three medium-severity and two low-severity departures require remediation for full compliance.

Severity definitions used by this review:

- **Critical:** creates an immediate, credible secret exposure, data-loss, or unsafe execution path.
- **High:** bypasses a mandatory safety control in the active real-operation path.
- **Medium:** violates an explicit mandatory discipline and can cause divergent behavior, operational ambiguity, or sustained maintenance cost.
- **Low:** violates an explicit convention with bounded impact or creates a maintainability/observability weakness.

## Findings

### Medium: Project documentation and runbooks are outside the mandated locations

**Rules:** 5.1, 5.2, 5.3

The rules require documentation under `markdown/`, operational instructions under dated `instructions/YYYY-MM-DD/` directories, and a date-and-time header in every instruction file. The current repository instead keeps material in several disallowed locations:

- Root-level documentation: `README.md:1`.
- General documentation: `docs/config-schema.md:1`, `docs/operations.md:1`, `docs/protocol.md:1`, `docs/compatibility.md:1`, and the other Markdown files under `docs/`.
- Operational runbook: `docs/operations.md:1-7`; it is neither in `instructions/YYYY-MM-DD/` nor dated with a time.
- Additional documentation: `config/projects/README.md:1`, `schemas/README.md:1`, `templates/README.md:1`, and `docs/diagrams/README.md:1`.

**Impact:** The mandated documentation taxonomy cannot be relied upon. Operators and agents must search multiple roots for authoritative material, and instruction provenance/versioning is absent.

**Recommendation:** Move end-user documentation into `markdown/`; move procedures and runbooks to a dated `instructions/2026-08-13/` (or applicable historical date) directory with a date-and-time header. Keep only a minimal root entry point if the root README is intentionally exempted by an amended rule.

### Medium: Tunable runtime limits remain hardcoded rather than YAML-authoritative

**Rules:** 2.1, 2.2, 2.5, 2.6

The primary dispatch limits are configured in `execution`, but multiple runtime behaviors use fixed numeric limits that cannot be selected or validated from YAML:

- Git inspection timeouts: `src/dispatcher/config.py:744-751`, `src/dispatcher/preflight.py:138-152`, `src/dispatcher/repository.py:467-487`, and `src/dispatcher/baseline.py:390-396` use fixed five- or ten-second limits.
- Workspace Git commands use a fixed 30-second timeout in `src/dispatcher/workspaces.py:296-303` and `src/dispatcher/workspaces.py:343-350`.
- The live-smoke path fixes timeout, termination grace period, and output bound in `src/dispatcher/cli.py:447-462`.
- Supervisor-forwarding truncation is an implicit function default in `src/dispatcher/forward.py:10-22` (`max_chars=4000`).
- The callable mock runtime has implicit timeout, grace-period, and output-size defaults in `src/dispatcher/mock_harness.py:139-155`.

**Impact:** Operators cannot tune or review all active time and output bounds from the project configuration. The values can diverge across command paths and cannot be constrained by the YAML schema.

**Recommendation:** Add an explicitly named, validated configuration section for command, Git, smoke, and transcript/forwarding limits. Pass those values through every relevant call site. For test-only mock values, remove defaults or keep them strictly within test fixtures so production-importable runtime code has no policy defaults.

### Medium: Missing bootstrap resources silently select a fallback instruction set

**Rules:** 2.2, 2.5, 2.6

`Orchestrator._render_bootstrap()` silently replaces a missing or unreadable bootstrap template with `_FALLBACK_BOOTSTRAP` (`src/dispatcher/loop.py:409-421`, fallback at `src/dispatcher/loop.py:470-506`). The fallback itself embeds protocol version, output format, evidence conventions, and workflow behavior.

**Impact:** A packaging or filesystem defect changes supervisor instructions instead of failing before work begins. The fallback can drift from the packaged template without YAML review or schema validation.

**Recommendation:** Treat the packaged template as a required runtime resource and raise a clear startup error if it is absent or unreadable. If an emergency fallback is required, make selection an explicit, validated configuration choice and test both variants for equivalence.

### Low: Interactive diagnostics use `print` instead of the structured logging path

**Rules:** 4.1, 4.3

`src/dispatcher/loop.py:163-165` prints a supervisor question and `src/dispatcher/loop.py:357-359` prints an invalid-command diagnostic. Both events have already entered an operational control path, but the displayed content has no JSON timestamp, level, module/function field, or redaction through `JsonFormatter`.

**Impact:** These events are inconsistent with configured structured logs and may expose unredacted model text on the terminal. The risk is bounded because the output is interactive and the diagnostic hint truncates the displayed prefix.

**Recommendation:** Preserve the interactive prompt behavior, but emit a redacted structured event before rendering the user-facing text. Route non-interactive diagnostics through the CLI presentation layer rather than direct `print` calls in the orchestration module.

### Low: Logging handler and color policy are not configurable or implemented

**Rules:** 4.2, 4.4

`ObservabilityDefinition` exposes `log_format` and `log_level` (`src/dispatcher/config.py:289-294`), but `configure_logging()` always installs one `StreamHandler` with one JSON formatter (`src/dispatcher/observability.py:38-46`). The YAML `log_format` is restricted to `json` and is not consulted by the logger setup; handlers have no configuration; colorized levels and parameter/value distinction are not implemented.

**Impact:** Logging level is configurable, but format and handler topology are not, and the explicit terminal-readability requirement is unmet.

**Recommendation:** Either extend YAML with validated handler/format/color settings and honor them in `configure_logging()`, or revise the discipline rule to explicitly adopt structured JSON-only logging without color for this service.

## Compliant Areas

- **Strict YAML validation:** `src/dispatcher/config.py:423-435` loads duplicate-key-safe YAML and validates a frozen, `extra="forbid"`, strict Pydantic model. It resolves paths then validates required keys, types, ranges, references, directories, writable parents, and Git remotes before use. `src/dispatcher/yaml_io.py:19-41` rejects duplicate mapping keys.
- **Configuration authority:** Active execution controls such as timeout, output bound, retries, concurrency, budgets, policy composition, retention, and log level are explicit fields in `src/dispatcher/config.py:202-364` and `config/projects/example.yaml:67-196`.
- **Secret handling:** No credential values were found in the public example configuration. Credentials are checked by environment-variable name only in `src/dispatcher/preflight.py:213-222`; runtime values are redacted and private artifacts use owner-only permissions in `src/dispatcher/security.py:16-136`. CI also runs Gitleaks in `.github/workflows/ci.yml:29-32`.
- **Structured logging baseline:** `src/dispatcher/observability.py:22-51` includes timestamp, level, module, function, and correlation fields, then redacts values. The CLI loads the YAML log level before normal commands, for example `src/dispatcher/cli.py:260-261` and `src/dispatcher/cli.py:297-299`.
- **Command bounds:** Reviewed `subprocess.run` calls provide timeouts, and long-lived OpenCode and verification processes are bounded by configured timeouts and output limits in `src/dispatcher/sessions.py:320-438` and `src/dispatcher/verification.py:160-273`.
- **Explicit limits and lifecycle claims:** `README.md:9-27` and `docs/operations.md:20-41` clearly describe guarded and unsupported paths; no claim that the system is production-ready was found.

## Assessment Limits

- Operating-principle requirements about an agent's step-by-step work method, self-check loop, and pre-edit codemap cannot be verified from the repository snapshot alone. They require review of agent transcripts or change records.
- The governance requirement for explicit approval before agent edits likewise requires interaction history. It is not assessed as a code finding.
- No test, linter, type-check, or secret-scanner command was run as part of this report. The reported passing test suite was accepted as an input; the findings above are based on source and configuration inspection.
