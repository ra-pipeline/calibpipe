# AGENTS.md — calibpipe

> Working guidance for AI coding agents and human contributors in the `calibpipe` repository.

## 1. Scope and Canonical Paths

- Treat `/calibpipe` as the canonical repository root.
- Do not route new work through `/calibpip`; that duplicate name is deprecated.
- Keep user-facing package documentation in `docs/` under this repository, not in the sibling `pipeline/docs/` tree.
- Main source code lives in `src/calibpipe/`; tests live in `tests/`.

## 2. Environment and Tooling

- Use `uv` as the default environment and command runner for local development.
- Preferred setup command:

```bash
uv sync --extra dev --extra docs
```

- Preferred execution pattern:

```bash
uv run --extra dev pytest tests/
uv run --extra docs zensical serve
uv run --extra docs zensical build
```

- Avoid introducing ad hoc `venv` or `pip install` workflow changes when `uv` can do the job.
- `uv.lock` should remain the source of truth for resolved dependency state.

## 3. Packaging and Runtime Constraints

- Respect `pyproject.toml` as the packaging authority.
- The package currently targets Python `>=3.10`; do not introduce syntax that requires a newer minimum runtime.
- Preserve the canonical console script and backward-compatible runner shims:
  - `calibpipe` (unified CLI in `[project.scripts]`)
  - backward-compatible runner scripts in `scripts/` (`scripts/calibPipeIF.py`, `scripts/runbatch.py`, and `scripts/calibpipe_env.sh`)

## 4. Python Style

- Use `from __future__ import annotations` in Python modules.
- Use built-in generics such as `list[str]` and `dict[str, str]`.
- Use pipe unions such as `str | None`; do not introduce `Optional` or `Union`.
- Add type hints to function arguments and return values.
- Use 4-space indentation and keep lines within 120 columns.
- Prefer single quotes unless the surrounding file clearly uses a different convention.
- Use Google-style docstrings, but do not duplicate type information already present in annotations.

### Python Version Caveat

The preferred typing style is modern, but this repository still supports Python 3.10. That means:

- use PEP 585 and PEP 604 features freely
- do not introduce PEP 695 runtime syntax such as `type Alias = ...` or `def func[T](...)` until `requires-python` is raised accordingly

## 5. Documentation Standards

- The docs stack is Zensical plus `mkdocstrings-python`.
- `zensical.toml` is the canonical docs configuration.
- Keep these pages aligned when behavior changes:
  - `README.md`
  - `docs/guide.md`
  - `docs/index.md`
  - `docs/api/index.md` (and `docs/api/*.md`)
- Prefer documenting setup and docs workflows with `uv` examples first.
- API reference pages should use `mkdocstrings` directives instead of hand-maintained signature dumps.
- **Config Template Lockstep:** Whenever schema fields or dataclasses in `src/calibpipe/config.py` are modified, always update `config.example.toml` and documentation in lockstep (enforced by `TestConfigExampleSchemaDrift` in `tests/test_config.py`).
- **Terminology:** Always refer to the pipeline as the **ALMA pipeline** (or ALMA Science Pipeline), never as "CASA pipeline". CASA is the underlying data processing dependency; the pipeline itself is the ALMA or CASA-based pipeline.

## 6. Validation Expectations

- After code edits, run the narrowest relevant validation first.
- Preferred checks:

```bash
uv run --extra dev pytest tests/
uv run --extra docs zensical build
```

- When Python files are touched, prefer running lint and formatting checks from the managed environment as well:

```bash
uv run --extra dev ruff check <path>
uv run --extra dev ruff format <path>
```

- If only documentation or markdown changes were made, validate those files directly and avoid broad unrelated changes.
- If terminal execution is unavailable, use file diagnostics and report the limitation explicitly.

## 7. Safety and Git Hygiene

- Never inspect or expose secrets, tokens, cookies, private credentials, or shell environment dumps.
- Do not read `.env*`, private SSH material, cloud credential files, or similar secret-bearing files unless the user explicitly asks and the task requires it.
- Never hardcode secrets into code, tests, docs, examples, or logs.
- Use synthetic placeholders in examples and fixtures rather than real internal identities, hostnames, or credentials.
- **Privacy and Identity Sanitization:** Always perform a privacy check before finalizing or reviewing changes.
  Ensure local usernames, user home paths (e.g., `/users/<name>` or `/home/<name>`), personal initials, internal email
  addresses, and cluster node hostnames are never included in code, tests, docs, or commit messages; always sanitize
  them to generic synthetic placeholders (e.g., `user`, `/home/user`, `cluster01`).
- Never stage, commit, or push changes unless explicitly asked.
- Never use destructive git or filesystem commands without explicit user approval.
- Do not revert user changes you did not make.

## 8. Change Strategy

- Prefer small, local edits over broad rewrites.
- Fix root causes instead of adding compatibility hacks when feasible.
- Preserve backward compatibility for existing calibration and batch workflows unless the user explicitly asks to break it.
- Keep tests and documentation fully offline; do not introduce live service dependencies into routine validation.
- Prefer actionable error messages over silent failures or raw, uncontextualized tracebacks when changing user-facing behavior.
