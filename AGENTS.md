# AGENTS.md — calibpipe

> Working guidance for AI coding agents and human contributors in the `calibpipe` repository.

## 1. Scope and Canonical Paths
- Repository root: `calibpipe/`. Source: `src/calibpipe/`. Tests: `tests/`. Docs: `docs/`.
- Do not route new work through `/calibpip` (deprecated).
- Keep user-facing package documentation in `docs/` under this repository, not in `pipeline/docs/`.

## 2. Environment and Tooling
- Use `uv` as the default environment and command runner (`uv.lock` is source of truth):
  ```bash
  uv sync --extra dev --extra docs
  uv run --extra dev pytest tests/
  uv run --extra docs zensical build
  ```
- Preserve canonical console script `calibpipe` and backward-compatible runner shims in `scripts/`.

## 3. Validation Expectations
- After code edits, run the narrowest relevant validation first (e.g. `uv run pytest tests/test_batch.py -k <pattern>`).
- Keep tests and docs 100% offline; do not introduce live network dependencies.
- Prefer actionable error messages over silent failures or unhandled stack traces.

## 4. Safety, Privacy & Review
- Human-in-the-loop: Never `git add`, `git commit`, or `git push` without explicit user request.
- No destructive commands (`rm -rf`, branch resets) without permission.
- Never inspect or leak secrets, `.env*`, SSH keys, or shell environment dumps (`printenv`, `env`).
- Always use synthetic generic placeholders for test fixtures, usernames, paths, and hostnames.

## 5. Agent Efficiency Directives (Token Conservation)
- **Compact File Inspection**: Always view files using narrow line ranges (`StartLine`/`EndLine` <= 60 lines). Never read full files exceeding 100 lines unless creating a whole-file diff.
- **Targeted Test Execution**: Always run the single relevant test module or filter with `-k <test_name>` (e.g. `uv run pytest tests/test_batch.py -k test_submit`). Avoid running the full test suite during intermediate iterations.
- **Concise Reporting**: Keep tool explanations to 1–2 sentences; avoid summarizing large diffs or repeating file contents in conversational responses.

