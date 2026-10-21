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

Pending changes on `feat/direct-execution-modes` and `feat/cascading-config`:

- **Configuration Hierarchy Simplification & Flattening:**
  - Consolidated tool installation roots (`pmr_home`, `datapacker_home`, `acsdata`, `java_home`, `pixi_bin`) directly under `[paths]` alongside workspace and storage directories, with bidirectional inheritance maintaining full backward compatibility with `[site]`.
  - Reserved `[site]` specifically for cluster operational policies and runtime constraints (`submit_host`, `strict_paths`, fallback tool paths, fallback execution behavior, and flux service endpoints).
  - Standardized execution option cascading across `CLI > [profiles] > [run] > [site] > built-in defaults`: moved execution-behavior controls (`use_custom_rcdir`, `casa_enable_telemetry`, `log2term`) to first-class fields on `[run]` and `[profiles.<name>]`, while maintaining `[site]` as facility-level defaults.
  - Decoupled MPI execution process counts (`[run].nprocs`) from batch scheduler CPU allocations (`[batch].cores` / `--ntasks` / `request_cpus`), eliminating misleading aliases and cross-coupling across scheduler and MPI configurations.
  - Standardized environment defaulting via `default_env` at configuration root, enabling clean global defaults across subcommands while keeping command-line execution safe and explicit (bare `calibpipe run` displays help; `calibpipe submit` requires an explicit target and strictly blocks submitting interactive profiles).
- **CASA Logging Configuration (`nologger` & `logfile`):** Added `nologger` (disables GUI logger window via `casaconfig` `--nologger`) and `logfile` (custom logfile path via `casaconfig` `--logfile`) support across `[run]`, `[profiles.<name>]`, and CLI flags (`--nologger` / `--no-nologger`, `--logfile <path>`), with graceful alias fallback for `noglogger`.
- **Job Name Prefix Parsing Fix:** Fixed bug in `batch.job_name()` where profile names containing `"mous"` (such as `e2e_mous_calimage`) were truncated by the hardcoded 11-character UID prefix offset (`uid___A001_`) due to loose substring matching; updated to strict `--mous=` prefix token checking.
- **Direct Execution Modes:** Added first-class support for offline reduction targets without querying archive metadata:
  - Direct recipe reductions on local datasets via `pipeline.recipereducer.reduce` (`--vis` + `--procedure` / `--recipe`).
  - Standalone Pipeline Processing Requests (`--PPR` / `--vla`).
  - Standalone Python/CASA scripts (`--script`).
  - Inline CASA interpreter commands (`--cmd`), enabling in-CASA test execution.
  - Interactive CASA sessions (`-i` / `--interactive`) with preloaded environment variables and optional IPython autoreload.
- **Unified Profile & Batch Submission:** Enhanced `calibpipe submit` (alias: `calibpipe batch`):
  - Single-job profile submission: submit self-contained execution profiles or ad-hoc targets directly to Slurm/HTCondor without requiring a temporary pipefile (`calibpipe submit -p <profile>`).
  - Multi-job pipefile queues: process batches containing both MOUS UIDs and direct dataset paths (`calibpipe submit pipefile.txt -p <profile>`).
- **Named Execution & Batch Profiles:** Configurable `[profiles.<name>]` supporting modular subtables (`.env`, `.run`, `.batch`) as well as flat keys, target overrides, and workspace search path integration.
- **Path Candidate Lists & 3-Pass Resolution:** Configurable candidate lists for `scipipe_rootdir`, `scipipe_logdir`, and `casarundata`. Evaluates candidates via 3-pass resolution (direct path $\to$ parent directory $\to$ index 0 fallback) with `{user}` and `~` expansion.
- **CASA Measures Path Consolidation:** Unified measures path settings under `casarundata` (with backward-compatible aliases `casadata` and `rundata`), populating `.casa/config.py` and exporting both `CASADATA` and `CASARUNDATA`.
- **Pipeline Heuristics Aliases:** Added `pipe_ver` alias for heuristics directory configuration alongside `pl_ver`, `pipe_dir`, and `heuristics_dir`.
- **MPI SQLite History Locking Prevention:** Automatically generates `.casa/ipython/profile_default/ipython_config.py` with `c.HistoryManager.enabled = False` for batch runs, eliminating 32-second Lustre lock contention and SQLite locking errors across multi-rank MPI runs while preserving command history for interactive sessions (`-i`).
- **HPC Hardware Tuning & Diagnostics:** Automatically injects OpenMPI diagnostic flags (`--display-allocation`, `--display-map`) for multi-process runs, unified process counts under `nprocs` (aliasing `ncores`), and added subprocess thread controls (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`).
- **Ancillary Staging & Backup Rotation:** Extended input staging (`--cont-dat`, `--jyperk-csv`, `--parameter-list`, `--ancillary`) and safe non-destructive backup directory rotation (`--backup`).
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
