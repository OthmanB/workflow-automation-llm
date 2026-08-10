# Installation and Compatibility

## Supported Environment

- Python `>=3.11`.
- OpenCode event compatibility is pinned to `1.18.11`.
- The pinned decoder contract is represented by sanitized fixtures under
  `tests/fixtures/opencode/1.18.11` and tested by
  `tests/contract/test_opencode_compatibility.py`.

Install the development environment with:

```bash
python -m pip install -e ".[dev]"
```

Build and validate a distribution with:

```bash
python -m build
twine check --strict dist/*
```

## Execution Boundary

The public configuration requires `execution.mode: mock_only`. Real OpenCode
dispatch and repository mutation are disabled. Fake OpenCode integration is
the supported execution proof for normal development and CI.

An optional read-only compatibility smoke exists for a separately approved
environment. It is skipped unless both environment variables are set:

```bash
DISPATCHER_LIVE_OPENCODE=1 DISPATCHER_LIVE_MODEL=<provider/model> \
  PYTHONPATH=src python -m pytest -m live_opencode
```

The smoke prompt only requests `LIVE_SMOKE_OK`, disables auto approval, and
uses a deny-by-default permission payload. It must not be used as authorization
for repository-mutating work.

## Dependency and Secret Gates

CI runs `pip-audit --strict .`, strict Twine metadata validation, a clean-wheel
install with `pip check`, and Gitleaks. GitHub Advanced Security is not enabled
for this repository, so the CI Gitleaks job is the repository-level secret
scanner.
