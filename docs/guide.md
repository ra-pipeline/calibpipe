# calibpipe Documentation

`calibpipe` is a lightweight helper package for launching single-run and batch ALMA Science Pipeline jobs outside the main `pipeline` Python package. It is intended for workstation and cluster workflows where operators need a stable command-line interface, reproducible environment selection, and backward-compatible wrappers around existing `calibPipeIF.py` and `runbatch.py` usage patterns.

---

## Overview

Use `calibpipe` when you need to:

- run a single MOUS from the command line without entering an interactive CASA session
- submit many MOUS runs to Slurm from a pipefile
- resolve CASA and pipeline environment variables from TOML configuration instead of ad hoc shell scripts
- preserve compatibility with operational scripts that still call `calibPipeIF.py`, `runbatch.py`, or `calibpipe_env.sh`

`calibpipe` does not replace the main `pipeline` package. The `pipeline` repository still provides the reduction framework, heuristics, task implementations, and weblog generation. `calibpipe` is the execution-oriented wrapper that selects a configured CASA + pipeline environment and launches a run with the requested inputs.

---

## Repository Layout

The project is organized as follows:

```text
calibpipe/
├── README.md
├── config.example.toml
├── docs/
│   ├── index.md
│   ├── guide.md
│   └── api/
├── scripts/
│   ├── calibPipeIF.py
│   ├── calibpipe_env.sh
│   └── runbatch.py
├── src/
│   └── calibpipe/
│       ├── cli.py
│       ├── config.py
│       ├── driver.py
│       ├── batch.py
│       └── steps/
└── tests/
```

Key implementation modules:

- `scripts/` contains backward-compatible runner shims (`calibPipeIF.py`, `runbatch.py`, `calibpipe_env.sh`).
- `src/calibpipe/cli.py` provides the unified command-line interface.
- `src/calibpipe/config.py` loads TOML configuration and builds the runtime environment.
- `src/calibpipe/driver.py` orchestrates a single pipeline run.
- `src/calibpipe/batch.py` generates and submits Slurm jobs.
- `src/calibpipe/steps/` houses modular execution phases (staging, PMR, PPR, CASA runner).

---

## Installation & Setup

### 1. Development Environment with `uv` (Recommended)

`calibpipe` uses [`uv`](https://docs.astral.sh/uv/) as its primary package and environment manager.

From the repository root, sync the virtual environment with development and documentation extras:

```bash
cd /path/to/calibpipe
uv sync --extra dev --extra docs
```

You can then run any `calibpipe` command directly inside the managed environment using `uv run`:

```bash
# Inspect resolved configuration
uv run calibpipe config show --env=main

# Run a single MOUS reduction
uv run calibpipe run --mous=uid://A001/X128a/Xb9 --env=main

# Submit batch jobs to Slurm
uv run calibpipe batch quick.run --env=main

# Run tests
uv run --extra dev pytest tests/
```

### 2. Isolated Tool Installation via `uv tool` (Recommended on Clusters)

To make `calibpipe` directly accessible in your `$PATH` across the cluster without needing `uv run` or manual venv activation:

```bash
uv tool install --editable /path/to/calibpipe
```

Because `--editable` is passed, the CLI stays synchronized with your checkout. You can then invoke commands directly from any working directory:

```bash
calibpipe run --mous=uid://A001/X128a/Xb9 --env=main
```

### 3. Alternative Installation Methods

* **Standard Editable Install via `pip`:**
  ```bash
  pip install -e .
  ```

* **Isolated Install via `pipx`:**
  ```bash
  pipx install /path/to/calibpipe
  ```

### Tip: Relocating the `uv` Cache Off NFS Home Directories

On HPC clusters where `$HOME` is NFS-backed, the default `uv` cache directory (`~/.cache/uv`) can cause two problems:

1. **Disk quota exhaustion** — the cache grows unbounded (no automatic eviction) and counts against your home directory quota.
2. **Cross-filesystem hardlink failures** — `uv` installs packages by hardlinking files from its cache into virtual environments. Hardlinks cannot cross filesystem boundaries. When the cache is on NFS and the venv lives on a local scratch filesystem (or vice versa), `uv` falls back to full file copies, producing warnings like `Failed to hardlink files; falling back to full copy`.

#### Solution: Redirect the Cache to Local Scratch

Set `UV_CACHE_DIR` to a path on a fast local or parallel filesystem:

```bash
# Add to ~/.bashrc (or your site's shell profile)
export UV_CACHE_DIR="/scratch/$USER/.cache/uv"
```

Verify the active cache location at any time:

```bash
uv cache dir
```

#### Fix the Link-Mode Separately (if needed)

If your venv and the cache still reside on different filesystems (e.g. project workspace on NFS, cache on local scratch), set the install link mode to `copy` to suppress the cross-device fallback warning:

```bash
export UV_LINK_MODE=copy          # via environment variable
# or per-invocation:
uv sync --link-mode=copy
```

> [!NOTE] `copy` mode is slower and uses more disk space per venv than hardlinks, because each venv receives its own copy of every package file. Placing `UV_CACHE_DIR` on the same filesystem as your project avoids this trade-off entirely.

#### Cache Maintenance

`uv` does not automatically limit cache size. Prune stale entries periodically:

```bash
uv cache prune    # removes unused/outdated entries (safe, non-destructive)
uv cache clean    # wipes the entire cache (forces fresh downloads on next use)
```

---

> [!WARNING] Observatory Infrastructure & Cluster Dependency
>
> `calibpipe` is an execution driver designed for ALMA Science Pipeline operations on observatory HPC clusters (e.g., NAASC cluster) and specialized pipeline workstations. Running `calibpipe` with arbitrary custom configurations on a standard personal workstation will **not work out-of-the-box** unless you have access to valid ALMA pipeline installations, Slurm, ALMA datapacker, and `pipelineMakeRequest` (PMR).
>
> If you are working on an observatory cluster, refer to your site administrator or internal templates (e.g. `notes/config.internal.example.toml`).

---

## Configuration

`calibpipe` relies on a structured TOML configuration file to separate code from site-specific paths and CASA installations. Start from `config.example.toml` in the repository root and copy it to `config.toml`.

### 1. File Discovery Precedence

When any `calibpipe` command or script runs, the configuration file is resolved in the following strict order of precedence (first found wins):

1. **CLI Argument:** `--config=<path>`
2. **Environment Variable:** `$CALIBPIPE_CONFIG`
3. **Current Working Directory:** `./config.toml`
4. **User Config Directory:** `~/.config/calibpipe/config.toml`

```mermaid
flowchart LR
    A["1. --config CLI flag"] -->|if unset| B["2. $CALIBPIPE_CONFIG env var"]
    B -->|if unset| C["3. ./config.toml (cwd)"]
    C -->|if unset| D["4. ~/.config/calibpipe/config.toml"]
```

If none of these paths exist, `calibpipe` raises an actionable `ConfigError`.

---

### 2. Configuration Hierarchy & Structure

The TOML configuration is organized into three distinct tiers:

```
config.toml
├── default_env = "main"          <-- Default target environment
├── [paths]                       <-- Tier 1: Working directories & pipeline datasets
├── [envs.<name>]                 <-- Tier 2: Selectable CASA + Pipeline runtime targets
├── [site]                        <-- Tier 3: Observatory tooling & cluster overrides (Optional)
├── [batch]                       <-- Tier 4: Slurm cluster batch submission defaults (Optional)
└── [run]                         <-- Tier 5: Pipeline single-run driver defaults (Optional)
```

#### Tier 1: Workspace & Product Paths (`[paths]`)
Configures directories where runs, logs, and reference databases reside:
- `scipipe_rootdir`: Root directory where execution folders and data products are staged. Supports `{user}` interpolation.
- `scipipe_logdir`: Root directory for driver and subprocess execution logs. Supports `{user}` interpolation.
- `pickle_dir`: (Optional) Directory for post-run pickle logs. Supports `{user}` interpolation.
- `obscaldir`: (Optional) Path to static offline calibrator database (passed to the pipeline via `--staticobscal`).
- `aUdir`: (Optional) Directory for auxiliary `analysisUtils` scripts.
- `validation_dir`: (Optional) Directory containing baseline reference products.
- `heuristics_root`: (Optional) Base directory for multi-branch pipeline checkouts (`{heuristics_root}/{branch}`).

#### Tier 2: Runtime Targets (`[envs.<name>]`)
Each `[envs.<name>]` table represents a selectable runtime target (e.g. `[envs.main]`, `[envs.dev]`, `[envs.modular]`):
- **Monolithic CASA Installation:**
  - `casa_root`: Root path to the monolithic CASA installation containing `bin/casa` and `bin/mpicasa`.
- **Modular Pixi Environment:**
  - `pixi_dir`: Path to the Pixi project directory containing `pyproject.toml` or `pixi.toml`.
  - `pixi_env`: (Optional) Target Pixi environment name (default: `"default"`). In MPI mode, `calibpipe` sets `CASA_NPROCS` to the requested core count.
- **Common Options:**
  - `branch`: (Optional) Pipeline branch identifier (defaults to `<name>`).
  - `heuristics_dir`: (Optional) Path to pipeline heuristics checkout. Supports template substitutions `{casa_root}`, `{pixi_dir}`, and `{branch}` (e.g. `{pixi_dir}/pipeline`). If omitted in Pixi mode, defaults to `{pixi_dir}/pipeline`.
  - *Arbitrary extra keys:* Any additional key-value pairs in this table are exported as environment variables for that specific target.

#### Tier 3: Site & Cluster Overrides (`[site]`) (Optional)
Overrides the package's built-in defaults for observatory-specific infrastructure:
- `pixi_bin`: (Optional) Explicit path to the `pixi` executable (defaults to finding `pixi` on `$PATH`).
- `pmr_home`: Installation root for `pipelineMakeRequest` (default: `/opt/pipetools/latest`).
- `datapacker_home`: ALMA datapacker installation root (default: `/opt/datapacker/current`).
- `acsdata`: ACS data directory (default: `/opt/acsdata`).
- `java_home`: JVM runtime path (default: `$JAVA_HOME` or `/usr/lib/jvm/default-java`).
- `flux_service_url`: Primary ALMA flux service URL.
- `flux_service_url_backup`: Secondary ALMA flux service backup URL.
- `submit_host`: If set, `calibpipe batch` strictly refuses to submit Slurm jobs unless run on this specific hostname.
- `strict_paths`: If set to `true`, path validation aborts with an error instead of issuing warnings.
- `use_custom_rcdir`: If `true` (default), generates an isolated CASA runtime environment (`.casa/` with `config.py` and `startup.py`) inside the run tree, ensuring pipeline heuristics and `eppr` are properly initialized without relying on `~/.casa/`.

#### Tier 4: Slurm Batch Defaults (`[batch]`) (Optional)
Configures baseline defaults for `calibpipe batch` when CLI flags are not provided:
- `queue`: Slurm partition / queue name (default: `plwg`).
- `cores`: Number of tasks / CPU cores allocated per Slurm job `--ntasks` (default: `8`).
- `mem`: Total RAM in GB per job `--mem` (default: `248`; mutually exclusive with `mem_per_cpu`).
- `node`: Slurm node count string `--nodes` (default: `"1"`).
- `mail_type`: Slurm email notification policy `--mail-type` (default: `ALL`).
- `walltime`: Optional job runtime limit `--time` (e.g. `"24:00:00"`; omitted if unset).
- `nodelist`: Optional target host pinning `--nodelist` (e.g. `"cvpost01"`).
- `chdir`: Optional working directory override `--chdir`.
- `cpus_per_task`: Optional CPUs per MPI task `--cpus-per-task` for hybrid `mpicasa` execution.
- `mem_per_cpu`: Optional RAM per CPU `--mem-per-cpu` (e.g. `"30G"`; replaces `mem` if set).
- `hint`: Optional scheduler placement hint `--hint` (e.g. `"nomultithread"`).
- `ntasks_per_core`: Optional task limit per physical core `--ntasks-per-core` (e.g. `1` to disable hyperthreading).
- `distribution`: Optional task distribution policy `--distribution` (e.g. `"cyclic:cyclic"`).
- `no_requeue`: Prevent Slurm from requeuing jobs on node failure `--no-requeue` (default: `true`).

#### Tier 5: Pipeline Run Defaults (`[run]`) (Optional)
Configures single-run driver execution defaults for `calibpipe run`:
- `recipe`: Default pipeline reduction recipe (default: `calimage`).
- `ncores`: Default CPU core count passed to `mpicasa` (default: `8`).
- `loglevel`: Default pipeline log level (default: `debug`).
- `useresume`: Use breakpoint / resume execution instead of two sequential CASA contexts (default: `false`).
- `use_custom_rcdir`: Override site-level isolated CASA runtime setting per run (default: `true`).

---

### 3. Environment Variable Precedence & Construction

When `build_environment()` runs, environment variables are assembled and overlaid in the following order:

```mermaid
flowchart TD
    A["1. Host Shell Environment (os.environ: USER, HOME, PATH)"] --> B["2. Built-in Defaults (SITE_DEFAULTS)"]
    B --> C["3. [site] Overrides from config.toml"]
    C --> D["4. [paths] Directories (interpolating {user})"]
    D --> E["5. Target [envs.<name>] (CASA_ROOT / PIXI_DIR, heuristics, PATH prepends)"]
    E --> F["6. Path Reachability Validation (check_paths)"]
```

1. **Host Environment:** Reads baseline `os.environ` (inherits `USER`, `HOME`, base `PATH`).
2. **Site Defaults & Overrides:** Resolves `JAVA_HOME`, `ACSDATA`, `ACSROOT` (`pmr_home`), `DATAPACKER_HOME`, `JARSDIR`, `FLUX_SERVICE_URL`.
3. **Paths Resolution:** Resolves and creates `SCIPIPE_ROOTDIR`, `SCIPIPE_LOGDIR`, `PICKLE_DIR`, `OBSCALDIR`, `AUDIR`, `VALIDATION_DIR`.
4. **Environment Target Resolution:** Sets `CASA_ROOT` (or `PIXI_DIR`), prepends executable paths and `PMR/bin` to `PATH`, and sets `SCIPIPE_HEURISTICS`.
5. **Path Validation:** `check_paths()` checks disk accessibility for `CASA_ROOT` (or `pixi_dir`), `pmr_home`, `datapacker_home`, `acsdata`, and `java_home`. If any path is missing, actionable guidance is printed to `sys.stderr`.

---

## Command-Line Workflows

`calibpipe` provides one CLI with three primary workflows.

### 1. Run a Single MOUS

```bash
calibpipe run --mous=uid://A001/X128a/Xb9 --env=main --recipe=calimage
```

This resolves the selected environment, stages the run inputs, and launches the underlying pipeline execution.

The legacy script wrapper still works:

```bash
./scripts/calibPipeIF.py --mous=uid://A001/X128a/Xb9 --env=main
```

### 2. Submit a Batch to Slurm

```bash
calibpipe batch quick.run --env=main -c 8 -m 248 -p plwg
```

The `quick.run` file contains one MOUS per line, with an optional recipe column:

```text
# <mous_uid> [recipe]
uid://A001/X128a/Xb9 calimage
uid://A002/Xcff05c/Xd calimage
```

The legacy script wrapper also remains available:

```bash
./scripts/runbatch.py quick.run --env=main -c 8 -m 248 -p plwg
```

#### Multi-Job Concurrency and HPC Safety

When running large batches across Slurm nodes (`calibpipe batch ...`), `calibpipe` implements several
safeguards to guarantee conflict-free concurrent execution:

##### 1. Pixi Lockfile & Environment Concurrency (`--frozen`)

When running under a modular Pixi environment (`--env=<pixi_env>`):

* **Immutable Lockfile Enforcement:** `calibpipe` executes Pixi with `pixi run --frozen`. This guarantees Pixi
  treats `pixi.lock` as strictly read-only and never attempts to re-solve dependencies or update the lockfile
  concurrently across worker nodes.
* **Script-Based Execution (`casa_piperun.py`):** Rather than passing complex, multi-statement inline Python
  strings through nested shell layers (which can strip quotes and cause syntax errors in CASA), `calibpipe` writes
  a clean `casa_piperun.py` script directly into each MOUS's `working/` directory and executes it via
  `-c /path/to/working/casa_piperun.py`. Using an absolute path ensures CASA recognizes it as a script file
  regardless of the initial working directory of the Pixi task or MPI launcher.

> [!IMPORTANT] Pre-install Pixi Environments Before Large Batches
> Always run `pixi install` once in your Pixi workspace before submitting multi-job batches. Once initialized,
> worker nodes access shared conda/pip binaries strictly in **read-only mode** without filesystem lock
> contention. If multiple uninitialized jobs start concurrently, they will attempt to unpack packages
> simultaneously into `.pixi/envs/`, leading to race conditions.

##### 2. Isolated CASA Runtime State (`.casa`)

Monolithic and modular CASA default to writing user configuration, cache, and telemetry SQLite state into
`~/.casa` in your home directory. In a parallel cluster environment, concurrent jobs writing to `~/.casa` can
collide or corrupt databases.

`calibpipe` automatically isolates CASA state per MOUS:
* Creates a dedicated `.casa/` directory inside `<mous_path>/working/.casa/`.
* Renders private, customized `config.py` and `startup.py` scripts.
* Passes `--cachedir`, `--configfile`, and `--startupfile` pointing directly into that job's working directory.
* Jobs never touch or contend for a shared `~/.casa/` directory.

##### 3. Slurm Submission Safety

* **Unique Job Records:** Each job in a batch receives a unique timestamped name (`batch.<mous>_<timestr>`) with
  dedicated `.out`, `.err`, and `.sbatch` files.
* **Submission Staggering:** `calibpipe batch` pauses for 1 second between consecutive submissions to ensure unique
  timestamp resolution and prevent submission bursts.
* **Isolated Working Directories:** Each MOUS executes exclusively within its own
  `<rootdir>/<project_run>/SOUS_.../GOUS_.../MOUS_.../working/` hierarchy.

### 3. Resolve a Shell Environment

```bash
eval "$(calibpipe env --env=main)"
```

This prints shell exports that can be evaluated directly in `bash` or `zsh`.

The compatibility wrapper is:

```bash
source scripts/calibpipe_env.sh --env=main
```

Use `scripts/calibpipe_env.sh` when you want the resolved CASA and pipeline variables loaded into your current shell session and prefer the older `source ...` workflow. It must be sourced rather than executed, and it exists mainly as a compatibility wrapper around `calibpipe env`.

### 4. Inspect Resolved Configuration

Inspect active settings, paths, and environment defaults resolved from `config.toml` without executing anything:

```bash
calibpipe config show --env=main
```

Or inspect an alternate configuration file:

```bash
calibpipe config show --config=/path/to/custom_config.toml --env=dev
```

---

## Backward Compatibility

`calibpipe` preserves legacy runner shims in `scripts/` so existing user scripts and scheduled workflows do not need to change immediately:

- `scripts/calibPipeIF.py` forwards to `calibpipe run` / `calibpipe.driver`
- `scripts/runbatch.py` forwards to `calibpipe batch` / `calibpipe.batch`
- `scripts/calibpipe_env.sh` forwards to `calibpipe env` / `calibpipe.cli env`

This allows gradual migration to the unified `calibpipe` CLI without breaking older job wrappers or polluting the global `$PATH`.

---

## Testing

### Offline Unit Testing

Run the test suite from the repository root:

```bash
pytest tests/
# or with uv:
uv run --extra dev pytest tests/
```

The test suite runs 100% offline without requiring local CASA installations, Slurm daemons, or cluster filesystems. Subprocess invocations (`sbatch`, `casa`, `pipelineMakeRequest`) are mocked, and generated execution steps and Slurm batch scripts are validated against golden reference specifications under `tests/reference/`.

### Cluster Pre-Flight Verification

When developing locally while targeting a remote cluster (or before submitting long-running batch jobs), use these verification techniques:

1. **Verify Environment Resolution (`--print-env`):**
   Inspect resolved variables, CASA paths, and PMR directories without modifying the current shell:
   ```bash
   calibpipe env --env=main --print-env
   ```
   If configured paths (such as `casa_root` or `pmr_home`) do not exist or are unreachable on the current host, `calibpipe` emits warning diagnostics indicating the missing paths.

2. **Verify Single-Run Driver Environment:**
   You can verify driver environment initialization for a specific MOUS without triggering pipeline staging or launching CASA:
   ```bash
   calibpipe run --mous=uid://A001/X128a/Xb9 --env=main --print-env
   ```

3. **Isolated Cluster Configuration:**
   Maintain cluster-specific paths in an unversioned `config.toml` on the remote system (copied from `config.example.toml`). Because `config.toml` is gitignored, cluster-specific filesystem paths are kept local and never committed to version control.

---

## Building the Documentation

Install the documentation extras and build or serve the documentation locally:

```bash
# Live reloading server
uv run --extra docs zensical serve

# Or static HTML build
uv run --extra docs zensical build
```

The Zensical configuration lives in `zensical.toml`. API pages are generated through `mkdocstrings-python` by importing modules from `src/`.

---

## Related Files

- `README.md` (repository root): top-level package overview and quick start
- `config.example.toml`: example configuration template
- `quick.run`: sample batch input format
- [API Reference](api/index.md): generated API reference
