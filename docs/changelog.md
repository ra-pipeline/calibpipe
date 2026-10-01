# Changelog

All notable changes to `calibpipe` are documented in this file, following [Keep a Changelog](https://keepachangelog.com/).

---

## Prototype vs. Library

`calibpipe` evolved from loose operational scripts in `scripts/` into a standalone, typed Python package:

- **Packaging:** Converted from unversioned scripts into a standard Python package (`pyproject.toml`, Python $\ge 3.10$, `uv`).
- **Unified CLI:** Replaced scattered scripts with the single `calibpipe` CLI (`run`, `batch`, `env`) while maintaining backward-compatible shims.
- **Typed Configuration:** Migrated from untyped dictionary parsing to typed dataclasses with cascading multi-layer merging.
- **Modern Runtimes:** Added first-class support for modular CASA 6 environments via Pixi alongside traditional tarballs.
- **Runtime Isolation:** Added dedicated per-run `.casa/` runtime directories to prevent `~/.casa` user state corruption.
- **HPC Batching:** Upgraded Slurm submission with persistent script logs, embedded `#SBATCH` directives, and queue monitoring.
- **Operator Convenience:** Added automatic relative symlinks (`working`, `products`, `rawdata`, `weblog`) in project root.

---

## [Unreleased]

Pending changes on `feat/cascading-config`:

- **Cascading Configuration:** Multi-layer deep config merging (Site `/etc/calibpipe` $\to$ User `~/.config/calibpipe` $\to$ Workspace `./config.toml` $\to$ CLI), additive `[envs]` tables, and `--no-site-config` isolation flag.
- **Convenience Symlink Shortcuts:** Automatic relative symlinks in project root (`working`, `products`, `rawdata`, `weblog`) to bypass nested OUS structures (`[run].symlink_shortcuts = true`).
- **Dynamic Weblog Linking:** Automatically locates and symlinks the latest generated pipeline HTML report post-run.
- **Execution Internals Documentation:** Added `docs/internals.md` covering PMR metadata resolution, project naming, and OUS directory layouts.

---

## [0.1.0] - 2026-10-01

- **Typed Configuration:** Strongly-typed dataclass configuration models with schema drift enforcement against `config.example.toml`.
- **Modular Pixi Runtime:** Support for modular CASA with Pixi (`pixi_dir`, `pixi_bin`, `pixi_env`), `CASA_NPROCS` core mapping, and virtualenv isolation.
- **Isolated CASA Runtime:** Automatic `.casa/` rcdir generation in working directory with telemetry suppression.
- **HPC Batch Hardening:** Template-rendered batch scripts with embedded `#SBATCH` directives, persistent job script records, CASA timestamped names, and `squeue` feedback.

---

## [0.0.9] - 2026-09-30

- **Standalone Package Scaffold:** Initialized standalone Python package with unified CLI (`calibpipe run`, `batch`, `env`).
- **Backward-Compatible Shims:** Preserved legacy entrypoints in `scripts/` (`calibPipeIF.py`, `runbatch.py`, `calibpipe_env.sh`).
- **Pre-flight Validation:** Path checking for CASA, PMR, and Java dependencies (`check_paths()`).
- **Native Shell Resolution:** Replaced custom bash TOML parser with `calibpipe env --eval`.
- **Documentation & Tests:** Initial Zensical documentation site and offline unit test suite.
