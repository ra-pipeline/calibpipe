---
trigger: glob
globs: "**/*.py"
---
# Python Standards & Conventions

## Runtime & Syntax
- Package targets Python `>=3.10`.
- Use `from __future__ import annotations` in all modules.
- Use PEP 585/604 built-in generics (`list[str]`, `dict[str, Any]`) and pipe unions (`str | None`).
- Do not introduce PEP 695 syntax (`type Alias = ...`, `def func[T](...)`).

## Style & Documentation
- Google-style docstrings; do not duplicate type annotations in docstrings.
- Single quotes preferred; 4-space indentation; 120-column line limit.
- Lint and format via `uv run --extra dev ruff check <path>` and `uv run --extra dev ruff format <path>`.

## Configuration Lockstep
- Whenever dataclasses or schema fields in `src/calibpipe/config.py` are modified, always update `config.example.toml` and documentation in lockstep (enforced by `TestConfigExampleSchemaDrift`).
