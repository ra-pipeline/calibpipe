---
trigger: glob
globs: "**/*.md,docs/**"
---
# Documentation Standards

## Build & Tooling
- Docs stack: Zensical plus `mkdocstrings-python` (`zensical.toml`).
- Build verification: `uv run --extra docs zensical build`.
- Local live preview: `uv run --extra docs zensical serve`.

## Structure & Terminology
- Terminology: Always refer to the pipeline as the **ALMA pipeline** (or ALMA Science Pipeline), never as "CASA pipeline".
- Keep `README.md`, `docs/guide.md`, `docs/index.md`, and `docs/api/index.md` aligned with code behavior.
- Document setup and workflows with `uv` command examples.
- Use standard ` ```mermaid ` fences for diagrams.
