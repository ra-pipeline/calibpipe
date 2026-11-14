# calibpipe Documentation

`calibpipe` is a lightweight helper package for launching single-run and batch ALMA Science Pipeline jobs outside the main `pipeline` Python package. It is intended for validation, testing, and development workflows where operators and developers need a stable command-line interface, reproducible environment selection, and backward-compatible wrappers around existing `calibPipeIF.py` and `runbatch.py` usage patterns.

---

## Overview

Use `calibpipe` when you need to:

- run a single MOUS from the command line without entering an interactive CASA session
- submit many MOUS runs to Slurm from a pipefile
- resolve CASA and pipeline environment variables from TOML configuration instead of ad hoc shell scripts
- preserve compatibility with existing scripts that still call `calibPipeIF.py`, `runbatch.py`, or `calibpipe_env.sh`

`calibpipe` does not replace the main `pipeline` package: `pipeline` still provides the reduction framework, heuristics, tasks, and weblog generation. `calibpipe` simply selects a configured CASA + pipeline environment and launches a run or batch job with the requested inputs.

---

## Repository Layout

```text
calibpipe/
├── README.md
├── config.example.toml
├── docs/
│   ├── index.md
│   ├── guide.md
│   ├── internals.md
│   ├── changelog.md
│   └── api/
├── scripts/
│   ├── calibPipeIF.py
│   ├── calibpipe_env.sh
│   ├── runbatch.py
│   └── update_regression_reference.py
├── src/
│   └── calibpipe/
│       ├── cli.py
│       ├── config.py
│       ├── runner.py
│       ├── batch.py
│       ├── collect.py
│       ├── coverage.py
│       ├── report.py
│       ├── update_ref.py
│       ├── pmr.py
│       ├── workspace.py
│       ├── steps/
│       └── templates/
└── tests/
```

Key modules:

- `scripts/` holds backward-compatible shims (`calibPipeIF.py`, `runbatch.py`, `calibpipe_env.sh`, `update_regression_reference.py`).
- `src/calibpipe/cli.py` defines the unified CLI and subcommand routing.
- `src/calibpipe/config.py` loads TOML configuration and builds the runtime environment.
- `src/calibpipe/runner.py` runs a single pipeline execution.
- `src/calibpipe/batch.py` generates and submits Slurm/HTCondor jobs.
- `src/calibpipe/collect.py` discovers pytest nodes for regression test arrays.
- `src/calibpipe/coverage.py` combines distributed `.coverage.*` files and remaps source paths.
- `src/calibpipe/report.py` summarizes JUnit XML test results and triggers coverage combining.
- `src/calibpipe/update_ref.py` merges `*.NEW.results.txt` outputs into `pipeline-testdata` references.
- `src/calibpipe/pmr.py` wraps ALMA MakeProcessingRequests for directory staging and data fetching.
- `src/calibpipe/workspace.py` manages project convenience symlink shortcuts (`working`, `products`, `rawdata`, `weblog`).
- `src/calibpipe/steps/` holds modular execution steps (such as input staging).
- `src/calibpipe/templates/` stores Jinja/string templates for CASA runtime files and Slurm/HTCondor scripts.

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
uv run calibpipe submit quick.run --env=main

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
calibpipe run --mous=uid://A001/X128a/Xb9 --env=main --recipe=calimage
```

### 3. Alternative Installation Methods

- **Standard Editable Install via `pip`:**

  ```bash
  pip install -e .
  ```

- **Isolated Install via `pipx`:**

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
> `calibpipe` is an execution driver designed for ALMA Science Pipeline operations on observatory HPC clusters (e.g., NAASC cluster) and specialized pipeline workstations. Running `calibpipe` on a personal workstation requires access to valid ALMA pipeline installations, Slurm (for batch execution), and ALMA datapacker / `pipelineMakeRequest` (PMR) when running in MOUS mode.
>
> If you are working on an observatory cluster, refer to your site administrator, `/etc/calibpipe/config.toml` / `config.site.toml`, or `config.example.toml`.

---

## Configuration

`calibpipe` uses a structured TOML configuration model designed for both standalone developer workstations and multi-user observatory HPC clusters (e.g., NAASC cluster).

### 1. Cascading Configuration Architecture

Rather than requiring users to duplicate cluster-wide tooling and CASA paths into private configuration files, `calibpipe` discovers and **deep-merges** configuration across a multi-layer hierarchy:

```mermaid
flowchart TD
    Site["1. Site / Cluster Config<br/><i>/etc/calibpipe/config.toml</i><br/>or <i>$CALIBPIPE_SITE_CONFIG</i><br/>or <i>&lt;repo_root&gt;/config.site.toml</i>"]
    User["2. User Config<br/><i>~/.config/calibpipe/config.toml</i><br/><i>~/.calibpipe/config.toml</i>"]
    CWD["3. Workspace / CWD Config<br/><i>./config.toml</i>"]
    CLI["4. CLI / Env Override<br/><i>--config=&lt;path&gt;</i> or <i>$CALIBPIPE_CONFIG</i>"]
    Merged["Resolved CalibpipeConfig<br/>(Deep-Merged Hierarchy)"]

    Site -->|Base Defaults &amp; Tools| Merged
    User -->|Personal Overrides| Merged
    CWD -->|Directory Overrides| Merged
    CLI -->|Highest Priority| Merged
```

#### Discovery Precedence (Lowest to Highest Priority)

1. **Site / System Configuration (Base Defaults):**
   - `$CALIBPIPE_SITE_CONFIG` environment variable (if set, must exist)
   - `/etc/calibpipe/config.toml` (standard Linux system configuration)
   - `config.site.toml` in repository or package root
   - *Maintained by cluster admins*: provides shared cluster tools (`pmr_home`, `datapacker_home`, `java_home`, `acsdata`), cluster Slurm queues (`[batch]`), and canonical production CASA installations (`[envs.main]`, `[envs.modular]`).
2. **User Configuration (`~/.config/calibpipe/config.toml` & `~/.calibpipe/config.toml`):**
   - XDG location: `~/.config/calibpipe/config.toml` (or `$XDG_CONFIG_HOME/calibpipe/config.toml`)
   - Home dot-directory: `~/.calibpipe/config.toml`
   - *User-specific overrides*: personal scratch paths (`scipipe_rootdir = "/lustre/.../{user}"`), personal Slurm email, or developer feature environments (`[envs.my_branch]`). If both user files exist, `~/.calibpipe/config.toml` overlays on top of XDG settings.
3. **Workspace Configuration (`./config.toml` or `./.calibpipe.toml`):**
   - Project- or directory-specific settings when running from a dedicated workspace.
4. **Explicit CLI / Runtime Override (`--config=<path>` or `$CALIBPIPE_CONFIG`):**
   - Replaces the user (layer 2) and workspace (layer 3) configuration files with the explicit file.
   - Still layers on top of the site configuration (layer 1) unless `--no-site-config` or `CALIBPIPE_NO_SITE_CONFIG=1` is set (on `calibpipe run`, pass `--config-only` to load only the `--config` file without merging any other layer).

#### Deep-Merging Rules

- **`[paths]` & `[site]`:** Keys are merged recursively. User-specified values override site values, while all unmentioned site settings (`obscaldir`, `pmr_home`, `acsdata`, etc.) are seamlessly inherited.
- **`[batch]`:** Slurm partition and resource defaults are inherited from the site configuration. A user only needs to specify what they wish to customize (e.g. `mail_type = "FAIL"`).
- **`[envs]` (Additive & Overriding):** Environment tables are additive. If the site defines `[envs.main]` and `[envs.modular]`, and a user config defines `[envs.custom_pixi]`, **all three** environments are available to the user. If an environment name collides, the user's table overlays the site's definition.

#### Running in Isolated Mode (`--no-site-config`)

For isolated test suites or benchmarking runs where site defaults should be ignored completely:

```bash
# Via CLI flag
calibpipe run --mous=uid://A001/X1/X1 --config=isolated.toml --no-site-config
calibpipe submit myjob.txt --config=isolated.toml --no-site-config

# Or via environment variable
export CALIBPIPE_NO_SITE_CONFIG=1
```

---

### 2. Cascading Parameter Resolution Order

When `calibpipe run` or `calibpipe submit` prepares an execution or batch submission, configuration parameters are resolved across a strict precedence cascade:

```mermaid
flowchart TD
    CLI["1. CLI Flags (Highest Priority)<br/><code>--config, -p/--profile, --env, --workdir, --recipe, --mous/--vis/--script/--cmd/--ppr, -v, --dry-run</code>"]
    Prof["2. Profile Overrides<br/>Flat keys under <code>[profiles.&lt;name&gt;]</code>"]
    RunBatch["3. Section Tables<br/><code>[run]</code>, <code>[batch]</code>, <code>[paths]</code>, <code>[envs.&lt;name&gt;]</code>"]
    Site["4. Site Config<br/><code>[site]</code> (submit_host, PMR/tool paths, flux URLs, strict_paths)"]
    Builtin["5. Built-in Defaults (Lowest Priority)<br/>Typed dataclass defaults"]
    Resolved["Resolved Execution Options<br/>(<code>ResolvedRunOptions</code>, <code>ResolvedBatchOptions</code>, <code>EnvSpec</code>)"]

    CLI -->|Overrides Everything| Resolved
    Prof -->|Overrides Sections &amp; Site| Resolved
    RunBatch -->|Overrides Built-in Defaults| Resolved
    Site -->|Cluster Tooling &amp; Policies| Resolved
    Builtin -->|Base Fallback| Resolved
```

1. **CLI Flags (Highest Priority):** Explicit command-line arguments on `run`/`submit` (such as `-p/--profile`, `--env`, `--workdir`, `--recipe`, `--dry-run`, `-v/--verbose`, and run targets `--mous`, `--vis`, `--script`, `--cmd`, `--ppr`, `-i/--interactive`, `--config-only`) override profile and file-based settings. (Execution tuning and scheduler resource options live in TOML rather than CLI flags; see [Migrating to 0.2](migration-0.2.md).)
2. **Profile Overrides:** Settings defined with **flat keys** directly inside `[profiles.<name>]`:
   - Environment overrides: `env`, `casa_root`, `heuristics_dir`, `pixi_dir`, `pixi_env`, `branch`
   - Path overrides: `workspaces`, `casarundata`, `scipipe_rootdir`, `scipipe_logdir`, `pickle_dir`, `obscaldir`, `aUdir`, `validation_dir`, `datapath`, `rundata`, `workdir`
   - Run & target options: `mous`, `vis`, `script`, `cmd`, `ppr`, `vla`, `interactive`, `recipe`, `nprocs`, `loglevel`, `useresume`, `symlink_shortcuts`, `log2term`, `verbose`, `use_custom_rcdir`, `casa_enable_telemetry`, `omp_num_threads`, `openblas_num_threads`, `omp_max_threads`, `mem_frac`, etc.
   - Batch & test-array options: `partition`, `cores`, `mem`, `mem_per_cpu`, `cpus_per_task`, `scheduler`, `logdir`, `outfile`, `errfile`, `save_runfile`, `max_concurrent`, `collect`, `test_target`, `test_marker`, `test_filter`, `test_opts`, `with_report`, etc.
3. **Section Table Defaults:** Top-level defaults defined in `[run]`, `[batch]`, `[paths]`, and `[envs.<name>]`. Note that `pixi_bin` belongs under `[paths]`, while `use_custom_rcdir`, `casa_enable_telemetry`, and `log2term` belong under `[run]` (or `[profiles.<name>]`).
4. **Site Configuration:** Facility-wide tooling locations and policies from `[site]` (`submit_host`, `pmr_home`, `datapacker_home`, `acsdata`, `java_home`, `flux_service_url`, `flux_service_url_backup`, `strict_paths`).
5. **Built-in System Defaults (Lowest Priority):** Dataclass field defaults on `PathsConfig`, `SiteConfig`, `BatchConfig`, and `RunConfig`.

#### Flat Profile Keys

Profiles require flat keys directly under `[profiles.<name>]` (nested `.env`, `.paths`, `.run`, and `.batch` subtables and `[batches.<name>]` are rejected by validation):

```toml
[profiles."vlass/7x3"]
casa_root = "casa-6.7.7-15-py3.12.el8"
heuristics_dir = "PROJ-101/pipeline"
nprocs = 8
partition = "queue"
cpus_per_task = 3
use_custom_rcdir = true
casa_enable_telemetry = false
mem_per_cpu = "5G"
```

#### Environment Resolution (`resolve_env`) Cascading

When resolving the runtime CASA or Pixi environment (`EnvSpec`):
1. **Base Environment Selection:** Identified by `--env`, `profile.env`, `default_env`, or (if `default_env` is omitted) `default_profile`'s environment.
2. **Target Overrides:** `casa_root`, `heuristics_dir`, `pixi_dir`, `pixi_env`, or `branch` defined in `[profiles.<name>]` (or passed to `collect` / `report` / `coverage` where supported) override the base `[envs.<name>]` table.
3. **Standalone Checkouts:** Allows executing against custom CASA installations or pipeline git branches from a profile without adding a permanent `[envs.*]` entry.

#### Dynamic Workspace Search Path Resolution

Relative paths specified for datasets (`vis`), procedure XML recipes (`recipe`), custom scripts (`script`), standalone PPRs (`ppr`), working directories (`workdir`), runtime roots (`casa_root`, `heuristics_dir`, `pixi_dir`, `casarundata`), and ancillary files (`cont_dat`, `jyperk_csv`, `parameter_list`, `ancillary`) are resolved against workspace search roots:
1. **Direct Path Check:** If a path is absolute or exists relative to the current working directory, it is returned immediately.
2. **Workspace Root Search:** Searches directories listed in `profile.workspaces` (if specified) or `paths.workspaces` in order.
3. **Filesystem Performance Caching:** Directory checks are cached via `_cached_is_dir` to reduce repeated `stat` calls across network filesystems (NFS, Lustre).

---

### 3. Configuration Hierarchy & Structure

`calibpipe` organizes configuration around a clean, flattened mental model consisting of **four core tables** and **two auxiliary tables**:

```
config.toml
├── default_env = "main"          <-- Default CASA & pipeline runtime environment
├── [paths]                       <-- Core 1: Workspace directories, pipeline datasets & tool paths
├── [envs.<name>]                 <-- Core 2: Selectable CASA + Pipeline runtime targets
├── [batch]                       <-- Core 3: Batch scheduler submission defaults
├── [run]                         <-- Core 4: Pipeline single-run driver defaults
├── [profiles.<name>]             <-- Presets: Named execution presets & batch resource bundles
└── [site]                        <-- Policies: Observatory cluster operational policies (Optional)
```

> [!TIP]
> **`default_env` vs. Profiles (`[profiles.<name>]`):**
> `default_env` defines the baseline CASA and pipeline environment across all commands (e.g. `calibpipe run -i` drops directly into an interactive CASA session under this environment without needing `--env`).
> Profiles under `[profiles.<name>]` provide named task presets activated on demand via `-p / --profile <name>`. Batch submissions require an explicit target (`pipefile` or `-p <profile>`) and strictly forbid interactive profiles.

#### Core 1: Workspace, Product & Tool Paths (`[paths]`)

Configures directories where runs, logs, reference databases, and cluster tools reside:

- **Storage & Workspaces:**
  - `scipipe_rootdir`: Root directory where execution folders and data products are staged. Supports a single path string or candidate list (automatically picking the first existing path) and `{user}` interpolation.
  - `scipipe_logdir`: Root directory for driver and subprocess execution logs. Supports a single path string or candidate list (automatically picking the first existing path) and `{user}` interpolation.
  - `pickle_dir`: (Optional) Directory for post-run pickle logs. Supports `{user}` interpolation.
  - `workspaces`: (Optional) Sequence of workspace root directories searched when resolving relative dataset paths (`vis`), scripts (`script`), procedure XML recipes (`recipe`), runtime paths (`casa_root`, `pixi_dir`, `heuristics_dir`), standalone PPRs (`ppr`), working directories (`workdir`), and ancillary staging files (`cont_dat`, `jyperk_csv`, `parameter_list`, `ancillary`).
- **Measures & Datasets:**
  - `casarundata`: (Optional) Explicit directory or candidate list containing CASA runtime measures data (`geodetic/`, `ephemerides/`). Automatically picks the first existing directory and populates `measurespath` and `rundata` in `.casa/config.py` as well as `CASADATA`/`CASARUNDATA` in the environment.
  - `rundata`: (Optional) Candidate measures directories for runs. Can also be set to `"none"` or `[]` to explicitly disable runtime measures path configuration.
  - `datapath`: (Optional) Sequence of directories configured for CASA's `datapath` in `.casa/config.py`.
  - `obscaldir`: (Optional) Path to static offline calibrator database (staged when `staticobscal = true` or `oldcont = true`).
  - `aUdir`: (Optional) Directory for auxiliary `analysisUtils` scripts.
  - `validation_dir`: (Optional) Directory containing baseline reference products.
  - `heuristics_root`: (Optional) Base directory for multi-branch pipeline checkouts (`{heuristics_root}/{branch}`).
  - `pixi_bin`: (Optional) Explicit path to the `pixi` executable (defaults to finding `pixi` on `$PATH`).

##### Candidate Path List Resolution (3-Pass Algorithm)

For storage roots (`scipipe_rootdir`, `scipipe_logdir`) and runtime measures (`casarundata`, `rundata`), configuration can specify a list of path candidates rather than a single fixed directory. This allows a single configuration file to work seamlessly across heterogeneous cluster filesystems (e.g. primary vs. secondary storage arrays):

1. **Pass 1 (Direct Path Check):** The first candidate path that exists directly on disk is selected.
2. **Pass 2 (Parent Directory Check):** If no candidate exists directly, the first candidate whose immediate parent directory exists on disk is selected (allowing the target directory to be created dynamically on reachable storage).
3. **Pass 3 (Fallback):** If no candidate matches Pass 1 or Pass 2, `calibpipe` falls back to the first candidate path in the list (index 0).

Candidate entries expand user home directories (`~`) and interpolate `{user}` with the current username:

```toml
[paths]
scipipe_rootdir = [
    "/lustre/cluster-a/users/{user}/scipipe",
    "/lustre/cluster-b/users/{user}/scipipe",
    "~/scipipe",
]
scipipe_logdir = [
    "/lustre/cluster-a/users/{user}/scipipe/logs",
    "/lustre/cluster-b/users/{user}/scipipe/logs",
    "~/scipipe/logs",
]
casarundata = [
    "/opt/casa/data",
    "/data/casa/data",
]
```

When `casarundata` (or `rundata`) is resolved, `calibpipe` automatically sets `measurespath` and `rundata` in `.casa/config.py` and exports both `CASADATA` and `CASARUNDATA` in the runtime environment. Setting `rundata = "none"` or `rundata = []` explicitly disables measures path population.

#### Core 2: Runtime Targets (`[envs.<name>]`)

Each `[envs.<name>]` table represents a selectable runtime target (e.g. `[envs.main]`, `[envs.dev]`, `[envs.modular]`):

- **Monolithic CASA Installation:**
  - `casa_root`: Root path or relative directory name for the monolithic CASA installation containing `bin/casa` and `bin/mpicasa` (resolved against `paths.workspaces` if relative).
- **Modular Pixi Environment:**
  - `pixi_dir`: Path or relative directory name for the Pixi project directory containing `pyproject.toml` or `pixi.toml` (resolved against `paths.workspaces` if relative).
  - `pixi_env`: (Optional) Target Pixi environment name (default: `"default"`). In MPI mode, `calibpipe` sets `CASA_NPROCS` to the requested core count.
- **Common Options:**
  - `branch`: (Optional) Pipeline branch identifier (defaults to `<name>`).
  - `heuristics_dir`: (Optional) Path to pipeline heuristics checkout (resolved against `paths.workspaces` if relative). Supports template substitutions `{casa_root}`, `{pixi_dir}`, and `{branch}`. If omitted, defaults to `{heuristics_root}/{branch}` when `[paths].heuristics_root` is set, or `{pixi_dir}` in Pixi mode (package root otherwise).
  - `extra_vars`: (Optional) Table of custom string environment variables exported for that target (`[envs.<name>.extra_vars]`).

#### Core 3: Batch Scheduler Defaults (`[batch]`)

Configures baseline defaults for `calibpipe submit` when profile overrides are not provided.

##### Direct Scheduler Key Mapping

`calibpipe` provides direct, explicit key mappings to underlying Slurm directives and HTCondor submit attributes:

| Configuration Key | Supported Scheduler | Slurm Directive (`#SBATCH`) | HTCondor Attribute | Description |
| :--- | :--- | :--- | :--- | :--- |
| `cores` | **Both** | `--ntasks=<n>` | `request_cpus = <n>` | Cluster CPU / task allocation (default: `8`). |
| `cpus_per_task` | **Slurm only** | `--cpus-per-task=<c>` | *(Used only as OpenMP fallback)* | Logical CPUs / threads per task for OpenMP (optional; omitted by default). |
| `partition` | **Both** | `--partition=<p>` | `+partition = "<p>"` | Partition or queue name (default: `plwg`). |
| `mem` | **Both** | `--mem=<m>G` | `request_memory = <m>G` | Total memory in GB (default: `248`; mutually exclusive with `mem_per_cpu`). |
| `mem_per_cpu` | **Both** | `--mem-per-cpu=<m>` | `request_memory = cores × mem_per_cpu` | Memory per CPU (e.g. `"5G"`; scaled to total RAM in HTCondor). |
| `mail_type` | **Both** | `--mail-type=<t>` | `notification = <keyword>` | Notification event policy (default: `ALL`). |
| `chdir` | **Both** | `--chdir=<dir>` | `initialdir = <dir>` | Initial working directory on execution node (defaults to project root derived from `profile.workdir` if omitted). |
| `nodelist` | **Both** | `--nodelist=<host>` | `TARGET.Machine == "<host>"` | Pin execution to a specific host machine. |
| `request_gpus` | **Both** | `--gpus=<n>` | `request_gpus = <n>` | GPU count requested per job (default: `0`; omitted when `0` on Slurm). |
| `scheduler` | **Both** | *(Driver selector)* | *(Driver selector)* | Batch backend: `"slurm"` (default) or `"htcondor"`. |
| `dry_run` | **Both** | *(Driver mode)* | *(Driver mode)* | Preview submit scripts without submitting (default: `false`). |
| `logdir` | **Both** | *(Output path)* | *(Output path)* | Output directory for `batch.<ts>.toml`, script records, and scheduler logs. |
| `outfile`, `errfile` | **Both** | `--output`, `--error` | `output`, `error` | Custom scheduler stdout/stderr filename or path (default: `batch.<job>.out`/`.err`). |
| `save_runfile` | **Both** | *(Driver output)* | *(Driver output)* | Optional path to write collected test node IDs when `collect = true`. |
| `omp_num_threads`, `openblas_num_threads` | **Both** | *(Wrapper exports)* | *(Wrapper exports)* | Optional `OMP_NUM_THREADS` / `OPENBLAS_NUM_THREADS` exports in wrapper scripts. |
| `max_concurrent` | **Slurm only** | `--dependency=afterany:...` | *(Not supported)* | Chained Slurm dependency slot throttle for multi-job arrays. |
| `node` | **Slurm only** | `--nodes=<n>` | *(N/A)* | Slurm node count string (default: `"1"`). |
| `walltime` | **Slurm only** | `--time=<t>` | *(N/A)* | Slurm walltime limit (e.g. `"24:00:00"`; omitted if unset). |
| `ntasks_per_core` | **Slurm only** | `--ntasks-per-core=<c>` | *(N/A)* | Tasks per physical core (`1` prevents hyperthread sharing). |
| `hint` | **Slurm only** | `--hint=<h>` | *(N/A)* | Placement hint (e.g. `"nomultithread"`). |
| `distribution` | **Slurm only** | `--distribution=<d>` | *(N/A)* | Task distribution policy (e.g. `"cyclic:cyclic"`). |
| `no_requeue` | **Slurm only** | `--no-requeue` | *(N/A)* | Prevent job requeuing on node failure (default: `true`). |
| `requirements` | **HTCondor only** | *(N/A)* | `requirements = ( <expr> )` | Custom ClassAd requirements override (`"none"`, `"default"`, or expression). |
| `pool`, `schedd`, `spool` | **HTCondor only** | *(N/A)* | `condor_submit` flags | Central manager (`-pool`), schedd daemon (`-name`), and file spooling (`-spool`). |
| `should_transfer_files`, `when_to_transfer_output` | **HTCondor only** | *(N/A)* | File transfer attributes | Sandbox file transfer policy (`YES`/`IF_NEEDED`/`NO`, `ON_EXIT`/`ON_EXIT_OR_EVICT`). |

> [!NOTE]
> **Cluster Allocation (`cores`) vs. MPI Process Count (`nprocs`)**:
> - **Cluster Resource Allocation (`[batch].cores`)**: Configures the hardware slots allocated by the workload manager (`#SBATCH --ntasks` in Slurm, or `request_cpus` in HTCondor).
> - **MPI Process Count (`[run].nprocs`)**: Configures the number of MPI worker processes launched inside CASA via `mpicasa -n <nprocs>`.
> - **Total Allocated CPUs in Slurm**: Slurm computes the total logical CPUs allocated as `cores × cpus_per_task`. For example, setting `cores = 7` and `cpus_per_task = 3` requests 7 × 3 = 21 logical CPUs, which is the number reported in `squeue` under `CP` or `CPUS`.
>
> **Working Directory Resolution (`chdir`)**:
> - When `chdir` is omitted but `workdir` is defined in a profile, `calibpipe` automatically derives `chdir` as the project root (the parent directory if `workdir` ends in `working/`, preventing locks on `working/` when directory rotation `backup = true` is enabled).
> - During job dispatch, `calibpipe` pre-creates the resolved `chdir` directory on the filesystem if it does not yet exist, preventing Slurm startup aborts (`slurmstepd: error: couldn't chdir`).

#### Core 4: Pipeline Run Defaults (`[run]`)


Configures single-run driver execution defaults for `calibpipe run`:

- `recipe`: Default pipeline reduction recipe (default: `calimage`).
- `nprocs`: Default worker/MPI process count passed to `mpicasa` `-n` / `CASA_NPROCS` (default: `8`).
- `loglevel`: Default pipeline log level (default: `debug`).
- `useresume`: Use breakpoint / resume execution instead of two sequential CASA contexts (default: `false`).
- `symlink_shortcuts`: Automatically create convenience symlinks (`working`, `products`, `rawdata`, and `weblog` when pipeline HTML exists) in the project run root (default: `true`).
- `log2term`: Mirror CASA log messages to stdout / terminal in real time (default: `false`).
- `verbose`: Stream full subprocess output line-by-line to terminal and execution logs (default: `false`; override via `-v` / `--verbose`).
- `omp_num_threads`: `OMP_NUM_THREADS` environment variable override (default: `1` when `nprocs > 1`).
- `openblas_num_threads`: `OPENBLAS_NUM_THREADS` environment variable override (default: `1` when `nprocs > 1`).
- `omp_max_threads`: Restrict maximum OpenMP thread count via `casalog.ompSetNumThreads`.
- `mem_frac`: CASA memory fraction limit via `casalog.setMemoryFraction` (e.g. `0.8`).
- `oversubscribe`: OpenMPI `--oversubscribe` flag for `mpicasa` (default: `true`).
- `bind_to`: OpenMPI process binding policy `--bind-to` (e.g. `"core"`, `"socket"`, `"none"`).
- `map_by`: OpenMPI process mapping policy `--map-by` (e.g. `"core"`, `"socket"`, `"node"`).
- `psrecord`: System resource consumption profiling via `psrecord` CLI (default: `false`).
- `memstats`: Pipeline memory statistics tracking via `pipeline.infrastructure.utils.enable_memstats()` (default: `false`).
- `pl_psrecord`: Pipeline internal telemetry tracking via `pipeline.infrastructure.utils.enable_psrecord()` (default: `false`).
- `backup`: Rotate existing non-empty working directories to timestamped `_backup_<timestamp>` directories before execution (default: `false`).
- `staticobscal`: Stage prior `flux.csv`, `antennapos.csv`, and `*flag*template.txt` files for the MOUS/EBs from `[paths].obscaldir` (default: `false`).
- `oldcont`: Stage prior `cont.dat` for the MOUS from `[paths].obscaldir` (default: `false`).
- `cont_dat`: File path to `cont.dat` staged into the working execution directory.
- `jyperk_csv`: File path to `jyperk.csv` staged into the working execution directory.
- `parameter_list`: File path to parameter list override staged into the working execution directory (automatically prefixed with `SEIP_` or `QLIP_` for matching recipes).
- `ancillary`: List of additional paths (files or directories) or `['source', 'target']` pairs staged into the working execution directory.
- `datapath`: Default list of directories populated in CASA's `datapath`.
- `rundata`: Default list of candidate measures directories for runs.
- `dry_run`: If `true`, formats and displays generated execution scripts, resolved working directories, and exact commands without launching CASA / MPI subprocesses (default: `false`; override via `--dry-run`).
- `autoreload`: For interactive CASA sessions (`--interactive`), whether to configure IPython with `%load_ext autoreload` and `%autoreload 2` (default: `true`).
- `nologger`: Disable the CASA GUI logger window (`casaconfig` `--nologger` flag; default: `false`).
- `logfile`: Custom logfile path or filename passed to CASA (`casaconfig` `--logfile` flag; default: auto timestamped `casa-<timestamp>.log`).
- `use_custom_rcdir`: Generate an isolated CASA runtime environment (`.casa/` with `config.py`, `startup.py`, and `ipython/profile_default/ipython_config.py`) in the run tree (default: `true`). Can also be configured in `[profiles.<name>]`.
- `casa_enable_telemetry`: Control CASA telemetry collection (default: `false`). Can also be configured in `[profiles.<name>]`.
- `workdir`, `rawdata`, `ppr`, `vla`, `flag_dir`, `subdir`, `onlysemipass`: Execution directory, rawdata symlink root, PPR override, VLA mode toggle (`executevlappr`), prior flag/WVR directory, `SCIPIPE_ROOTDIR` subdirectory, and comma-separated semipass ASDM list for PMR (all optional; also configurable per profile).

#### Presets: Unified Named Profiles (`[profiles.<name>]`)

`[profiles.<name>]` serves as the single canonical preset mechanism across `calibpipe`. A profile can encapsulate:
- **Batch resource bundles:** partition, core count (`cores`), thread allocation (`cpus_per_task`), memory, walltime, and scheduler backend.
- **Self-contained execution targets:** MOUS UIDs, MeasurementSets (`vis`), procedures, standalone PPRs, custom scripts, inline commands, or interactive sessions.
- **Hardware & runtime tuning:** process counts, OpenMP threads, memory limits, and subprocess environments.

Profiles can be selected across both `calibpipe run` and `calibpipe submit` via `-p <name>` or `--profile=<name>`.

##### 1. Batch Resource Presets

Define reusable queue and resource configurations:

```toml
[profiles.debug]
desc = "Interactive debug queue with quick turnaround"
partition = "debug"
cores = 4
mem = 32
walltime = "01:00:00"

[profiles.heavy]
desc = "High-memory multi-node batch queue preset"
partition = "batch2"
cores = 16
cpus_per_task = 2
mem = 500
```

##### 2. Self-Contained Execution Targets

Profiles can encapsulate execution targets directly, enabling single-command regression runs or benchmark sessions:

```toml
[profiles."regression/pipeline_fast"]
desc = "Fast interferometry calibration regression test"
env = "dev"
vis = "regression/fast_vis.ms"
recipe = "procedure_hifa_calimage.xml"
datapath = ["casa-data", "pipeline-testdata"]
rundata = ["casa-data"]
nprocs = 8
workdir = "runs/regression_fast"
dry_run = false

[profiles.unit_tests]
desc = "Run unit test suite inside CASA interpreter"
cmd = "import pytest; pytest.main(['-vv', 'tests/unit'])"
nprocs = 1

[profiles.interactive_dev]
desc = "Interactive CASA debugging session with autoreload"
interactive = true
autoreload = true
nprocs = 4
```

##### Complete Profile Attributes Reference

Profiles support all execution target, driver tuning, cluster submission, environment, and path override fields:

| Category | Parameter | Description |
| :--- | :--- | :--- |
| **Execution Targets** | `mous` | ALMA MOUS UID string (e.g. `uid://A001/X1/X1`). |
| | `vis` | MeasurementSet path string or list of paths. |
| | `recipe` | Pipeline recipe shorthand (`calimage`, `cal`, `image`, etc.) or procedure XML filename/path. |
| | `script` | Custom Python script path. |
| | `cmd` | Inline Python snippet executed via CASA `-c`. |
| | `ppr` | Standalone PPR XML path. |
| | `interactive` | Launch interactive CASA session (`true`/`false`). |
| | `vla` | Flag indicating VLA pipeline execution (`true`/`false`). |
| **Environment Overrides** | `env` | Base environment name from `[envs.*]` (defaults to `default_env`). |
| | `casa_root` | Monolithic CASA installation directory. |
| | `heuristics_dir` | Pipeline heuristics checkout directory. |
| | `pixi_dir` | Pixi project directory for modular CASA environments. |
| | `pixi_env` | Target Pixi environment name (default: `"default"`). |
| | `branch` | Pipeline branch identifier. |
| **Path Overrides** | `workspaces` | Search roots for relative datasets, scripts, and runtime trees. |
| | `casarundata` | CASA runtime measures directory (`geodetic/`). |
| | `scipipe_rootdir` | Root directory where execution folders are staged. |
| | `scipipe_logdir` | Output directory for scheduler and execution logs. |
| | `pickle_dir`, `obscaldir`, `aUdir`, `validation_dir` | Overrides for `[paths]` support directories. |
| | `datapath` | Sequence of directories populated for CASA `datapath`. |
| | `rundata` | Candidate measures directories (or `"none"`). |
| | `workdir`, `rawdata` | Working execution directory and rawdata ASDM root override. |
| **Run & Telemetry** | `recipe` | Pipeline reduction recipe (`calimage`, `procedure_vlassCCIP.xml`, etc.). |
| | `nprocs` | MPI process count passed to `mpicasa -n` (default: `8`). |
| | `loglevel` | Pipeline log level (`debug`, `info`). |
| | `useresume` | Use breakpoint / resume execution (`true`/`false`). |
| | `symlink_shortcuts` | Create convenience symlinks in project root (`true`/`false`). |
| | `log2term` | Mirror CASA log messages to stdout / terminal in real time. |
| | `verbose` | Stream full subprocess output to terminal and execution logs. |
| | `omp_num_threads` | Sets `OMP_NUM_THREADS` in subprocess environment. |
| | `openblas_num_threads` | Sets `OPENBLAS_NUM_THREADS` in subprocess environment. |
| | `omp_max_threads` | Upper bound for `casalog.ompSetNumThreads`. |
| | `mem_frac` | CASA memory fraction limit via `casalog.setMemoryFraction`. |
| | `oversubscribe` | OpenMPI `--oversubscribe` flag. |
| | `bind_to` | OpenMPI process binding policy (e.g. `core`, `socket`). |
| | `map_by` | OpenMPI process mapping policy (e.g. `core`, `node`). |
| | `psrecord` | Profile CPU/memory with `psrecord` CLI (`true`/`false`). |
| | `memstats` | Enable pipeline memory statistics tracking. |
| | `pl_psrecord` | Enable pipeline internal telemetry tracking. |
| | `backup` | Rotate existing run directory to timestamped `_backup_<timestamp>`. |
| | `staticobscal` | Stage prior `flux.csv`, `antennapos.csv`, and flag templates from `obscaldir` (`true`/`false`). |
| | `oldcont` | Stage prior `cont.dat` from `obscaldir` (`true`/`false`). |
| | `cont_dat` | File path to `cont.dat` staged into working directory. |
| | `jyperk_csv` | File path to `jyperk.csv` staged into working directory. |
| | `parameter_list` | Parameter list override staged into working directory. |
| | `ancillary` | Additional files or `[source, target]` pairs to stage. |
| | `flag_dir`, `subdir`, `onlysemipass` | Prior flag/WVR directory, `SCIPIPE_ROOTDIR` subdirectory, and semipass ASDM list for PMR. |
| | `dry_run` | Preview execution without subprocess calls (`true`/`false`). |
| | `autoreload` | Configure IPython autoreload extensions in interactive mode. |
| | `nologger` | Disable CASA GUI logger window (`--nologger`). |
| | `logfile` | Custom logfile path or name (`--logfile`). |
| | `use_custom_rcdir` | Isolated `.casa/` runtime generation in run tree (`true`/`false`). |
| | `casa_enable_telemetry` | CASA telemetry reporting (`true`/`false`). |
| **Test Array & Discovery** | `collect` | Submit an array of individual test jobs collected from `test_target` (`true`/`false`). |
| | `test_target` | Pytest test directory or file to collect from (e.g. `tests/regression/fast`). |
| | `test_marker` | Pytest marker expression filter for test collection (e.g. `"regression"`). |
| | `test_filter` | Pytest keyword expression filter for test collection (e.g. `"uid___A002"`). |
| | `test_opts` | Extra pytest CLI options appended to test execution (e.g. `"--cov=pipeline --cov-branch --cov-report="`). |
| | `with_report` | Automatically submit a dependent Slurm wrap-up job to combine coverage and synthesize test results (`true`/`false`). |
| | `save_runfile` | Persist collected test node IDs to a runfile during `calibpipe submit`. |
| **Batch Directives** | `max_concurrent` | Maximum concurrent batch jobs running simultaneously via chained Slurm dependency slots (`#SBATCH --dependency=afterany:<job_{i-K}>`). |
| | `cores` | Allocated CPU / task count (Slurm `--ntasks` / HTCondor `request_cpus`). |
| | `cpus_per_task` | Logical CPUs / threads per task for OpenMP multithreading. |
| | `partition` | Slurm partition / HTCondor queue name. |
| | `mem` | Total memory in GB (mutually exclusive with `mem_per_cpu`). |
| | `mem_per_cpu` | Memory per CPU (e.g. `"5G"`). |
| | `node` | Slurm node count string (default: `"1"`). |
| | `walltime` | Slurm walltime limit (e.g. `"14-00:00:00"`). |
| | `nodelist` | Pin execution to a specific host machine. |
| | `chdir` | Initial working directory on execution node (defaults to project root derived from `workdir`). |
| | `ntasks_per_core` | Tasks per physical core (`1` prevents SMT sharing). |
| | `hint` | Slurm placement hint (e.g. `"nomultithread"`). |
| | `distribution` | Slurm task distribution policy across nodes/sockets. |
| | `no_requeue` | Prevent job requeuing on node failure. |
| | `request_gpus` | Requested GPU count (`--gpus` on Slurm, `request_gpus` on HTCondor). |
| | `scheduler` | Batch backend (`"slurm"` or `"htcondor"`). |
| | `requirements` | Custom HTCondor ClassAd expression. |
| | `pool`, `schedd`, `spool` | HTCondor `condor_submit` routing (`-pool`, `-name`, `-spool`). |
| | `should_transfer_files`, `when_to_transfer_output` | HTCondor sandbox file transfer directives. |
| | `logdir`, `outfile`, `errfile` | Batch log directory and custom scheduler stdout/stderr paths. |
| | `mail_type` | Slurm / HTCondor notification policy. |

Precedence order when resolving parameters is strictly hierarchical:
`CLI flag > -p / --profile=<name> > [run] / [batch] / [paths] / [site] > built-in defaults`.

When a CLI target (such as `--mous` or `--vis`) is provided alongside `-p <name>`, the CLI target takes precedence and overrides the profile's embedded target.

#### Policies: Site, Cluster & PMR Tooling (`[site]`) (Optional)

`[site]` defines operational constraints, ALMA archive/PMR tooling installation paths, and remote service endpoints across an observatory cluster:

- `submit_host`: If set, `calibpipe submit` strictly refuses to dispatch batch jobs unless invoked on this specific submit host.
- `pmr_home`: Installation root for `pipelineMakeRequest` (default: `/opt/pipetools/latest`).
- `datapacker_home`: ALMA datapacker installation root (default: `/opt/datapacker/current`).
- `acsdata`: ACS data directory (default: `/opt/acsdata`).
- `java_home`: JVM runtime path for PMR and datapacker (default: `$JAVA_HOME` or `/usr/lib/jvm/default-java`).
- `flux_service_url`: Primary ALMA flux service URL.
- `flux_service_url_backup`: Secondary ALMA flux service backup URL.
- `strict_paths`: If set to `true`, path reachability validation aborts execution immediately with an error rather than issuing non-fatal warnings.


---

### 4. Environment Variable Precedence & Construction

When `build_environment()` runs, environment variables are assembled and overlaid in the following order:

```mermaid
flowchart TD
    A["1. Host Shell Environment (os.environ: USER, HOME, PATH)"] --> B["2. Built-in &amp; [site] Settings (JAVA_HOME, ACSDATA, PMR/tool paths, FLUX_SERVICE_URL*)"]
    B --> C["3. [paths] &amp; [run] Settings (SCIPIPE_ROOTDIR, SCIPIPE_LOGDIR, CASADATA, CASA_ENABLE_TELEMETRY)"]
    C --> D["4. [envs.&lt;name&gt;] (CASA_ROOT / PIXI_DIR, heuristics, PATH prepends, extra_vars)"]
    D --> E["5. Path Reachability Validation (check_paths)"]
```

1. **Host Environment:** Reads baseline `os.environ` (inherits `USER`, `HOME`, base `PATH`).
2. **Site Tooling & Endpoints:** Resolves `JAVA_HOME`, `ACSDATA`, `ACSROOT` (`pmr_home`), `DATAPACKER_HOME`, `JARSDIR`, `FLUX_SERVICE_URL`, and `FLUX_SERVICE_URL_BACKUP`.
3. **Paths & Run Behavior:** Resolves and creates `SCIPIPE_ROOTDIR` and `SCIPIPE_LOGDIR` (interpolating `{user}` and optional `subdir`), resolves `CASADATA`/`CASARUNDATA`, and sets `CASA_ENABLE_TELEMETRY` from `[run]`/`[profiles.<name>]`.
4. **Environment Target Resolution:** Sets `CASA_ROOT` (or `PIXI_DIR`), prepends `pmr_home/bin` and `casa_root/bin` (or `[paths].pixi_bin` parent dir) to `PATH`, sets `SCIPIPE_HEURISTICS`/`SCIPIPE_SCRIPTDIR`, sets `MATPLOTLIBRC`, and overlays `[envs.<name>.extra_vars]`.
5. **Path Validation:** `check_paths()` verifies `CASA_ROOT` (or `pixi_dir` and `pixi_bin`), plus `pmr_home`, `datapacker_home`, `acsdata`, and `java_home` when running MOUS/PMR workflows. Missing paths print actionable warnings to `sys.stderr` (or raise `ConfigError` when `strict_paths = true`).

---

## Command-Line Workflows

`calibpipe` provides one CLI with three primary workflows.

### 1. Execute a Pipeline Run (`calibpipe run`)

`calibpipe run` orchestrates single-run executions across multiple operational modes—standard ALMA MOUS reductions, direct recipe reductions, standalone PPR processing, custom script runs, inline test commands, and interactive developer sessions:

```mermaid
flowchart TD
    CLI["CLI: calibpipe run -p &lt;profile&gt; [--dry-run]"] --> Config["Layered TOML Configs (Site → User → CWD)"]
    Config --> ProfileLookup["Profile Resolution ([profiles.*])"]
    ProfileLookup --> TargetResolve{"Target Source?"}
    TargetResolve -- CLI Flag Present --> UseCLI["CLI Target (--cmd, --vis, --mous, etc.)"]
    TargetResolve -- Profile Specified --> UseProfile["Profile Target (cmd, script, vis, ppr)"]
    UseCLI --> WorkspaceSearch["Resolve Paths via paths.workspaces"]
    UseProfile --> WorkspaceSearch
    WorkspaceSearch --> Staging["Ancillary Staging & Timestamped Backup Rotation"]
    Staging --> RCDir["Render .casa/ (config.py, startup.py, autoreload)"]
    RCDir --> DryRun{"--dry-run?"}
    DryRun -- Yes --> PrintPlan["Print Command, Scripts, & Env (Exit 0)"]
    DryRun -- No --> Exec["Launch CASA / mpicasa Subprocess"]
```

#### A. Standard ALMA MOUS (PMR Mode)

```bash
calibpipe run --mous=uid://A001/X128a/Xb9 --env=main --recipe=calimage
```

Resolves metadata via `pipelineMakeRequest` (PMR), stages flags/WVR, and executes `executeppr` in an isolated project tree.

The legacy script wrapper also remains supported:

```bash
./scripts/calibPipeIF.py --mous=uid://A001/X128a/Xb9 --env=main
```

#### B. Direct Recipe Reduction (Offline Dataset Mode)

Run pipeline recipes directly against local MeasurementSets or ASDMs without querying external metadata services or PMR:

```bash
calibpipe run --vis /data/test.ms --env=main
```

Invokes `pipeline.recipereducer.reduce(vis=[...], procedure=...)` directly inside the specified `--workdir` (or current directory), using `recipe` configured in `[run]` or `-p <profile>` (accepting either a shorthand like `calimage` or a procedure XML filename/path).

#### C. Standalone PPR Execution (ALMA or VLA)

Execute pre-generated pipeline processing requests directly:

```bash
# ALMA PPR
calibpipe run --ppr /data/PPR.xml --env=main

# VLA PPR (with vla = true configured in profile)
calibpipe run --ppr /data/PPR.xml -p vla_ppr --env=main
```

Invokes `executeppr` (ALMA) or `executevlappr` (VLA) directly without requiring the full PMR directory layout.

#### D. Custom Script Execution

Execute a standalone Python/CASA script inside the managed CASA environment and isolated `.casa` runtime directory:

```bash
calibpipe run --script scripts/benchmark.py --env=main
```

#### E. Inline Command & In-CASA Testing

Execute Python code directly inside CASA (ideal for running pytest suites within the CASA interpreter):

```bash
calibpipe run --cmd "import pytest; pytest.main(['-vv', 'tests/regression/test_fast.py'])" --env=main
```

#### F. Interactive CASA Session

Launch an interactive CASA shell with all environment variables, paths, and custom configuration pre-loaded:

```bash
calibpipe run -i --env=main
```

##### IPython Dynamic Autoreload
When launching interactive sessions with `-i` / `--interactive`, `calibpipe` generates an isolated IPython profile configuration (`.casa/ipython/profile_default/ipython_config.py`) that loads IPython's autoreload extension:
- `c.InteractiveShellApp.exec_lines = ['%load_ext autoreload', '%autoreload 2']`

This enables automatic live reloading of modified pipeline Python modules without restarting CASA. Autoreload is active by default and can be configured via `[run].autoreload = true / false` (or in a profile).

#### G. Hardware Tuning & Telemetry Profiling

Fine-tune CPU threading, OpenMPI bindings, memory caps, and resource profiling in `[run]` or `[profiles.<name>]`:

```toml
[profiles.tuned_mpi]
nprocs = 16
omp_num_threads = 4
openblas_num_threads = 2
mem_frac = 0.8
oversubscribe = true
bind_to = "core"
map_by = "socket"
psrecord = true
memstats = true
pl_psrecord = true
```

```bash
calibpipe run --vis /data/test.ms -p tuned_mpi --env=main
```

- `nprocs`: Worker/MPI process count passed to `mpicasa` `-n` / `CASA_NPROCS`.
- `casa_root`: Override CASA installation root directory.
- `heuristics_dir`: Override pipeline heuristics checkout directory.
- `pixi_dir`: Override Pixi workspace directory for modular CASA environments.
- `pixi_env`: Override Pixi environment name (default: `default`).
- `branch`: Override pipeline branch / checkout identifier.
- `casarundata`: Override CASA runtime measures directory (`geodetic/`).
- `omp_num_threads`: Sets `OMP_NUM_THREADS` in subprocess environment (defaults to `1` when `nprocs > 1`).
- `openblas_num_threads`: Sets `OPENBLAS_NUM_THREADS` in subprocess environment (defaults to `1` when `nprocs > 1`).
- `omp_max_threads`: Calls `casalog.ompSetNumThreads(...)` if current thread count exceeds the limit.
- `mem_frac`: Calls `casalog.setMemoryFraction(...)` to set CASA memory fraction (e.g. `0.8`).
- `oversubscribe`: Passes `--oversubscribe` to OpenMPI `mpicasa` execution.
- `bind_to`: Passes `--bind-to <type>` to OpenMPI `mpicasa` (e.g. `core`, `socket`, `none`).
- `map_by`: Passes `--map-by <type>` to OpenMPI `mpicasa` (e.g. `core`, `socket`, `node`).
- `psrecord`: Wraps session with `psrecord` CLI, generating `.rec` and `.rec.png` time-series telemetry plots.
- `memstats`: Injects `pipeline.infrastructure.utils.enable_memstats()` into the execution preamble.
- `pl_psrecord`: Injects `pipeline.infrastructure.utils.enable_psrecord()` into the execution preamble.

> [!NOTE] OpenMPI Diagnostics
> For all multi-process executions (`nprocs > 1`), `calibpipe` automatically injects `--display-allocation` and `--display-map` into the `mpicasa` invocation. This logs compute node slot bindings, socket layout, and rank placement directly in the driver output and log files for transparency.

#### H. Ancillary File Staging & Safe Backup Rotation

Stage required input files or parameter overrides and preserve prior run outputs across all execution modes via `[run]` or `[profiles.<name>]`:

```toml
[profiles.safe_rerun]
backup = true
cont_dat = "/data/cont.dat"

[profiles.sd_run]
jyperk_csv = "/data/jyperk.csv"
ancillary = ["/data/caltable1", "/data/caltable2"]

[profiles.vlass_seip]
recipe = "procedure_hifv_vlassSEIP.xml"
parameter_list = "vlass.param"
```

```bash
calibpipe run --vis /data/test.ms -p safe_rerun --workdir=./run1 --env=main
```

- `backup`: Non-destructively rotates existing non-empty working directories to `<workdir>_backup_<YYYYMMDD_HHMMSS>` (with collision suffix `_1`, `_2` if needed).
- `staticobscal`: Stages prior `flux.csv`, `antennapos.csv`, and EB `*flag*template.txt` files from `obscaldir` in `--mous` and `--ppr` modes (overridden by `flag_dir` or explicit `ancillary`).
- `oldcont`: Stages prior `cont.dat` from `obscaldir` in `--mous` and `--ppr` modes (overridden by `cont_dat`, `ancillary`, or `flag_dir`).
- `obscaldir`: Configured in `[paths].obscaldir` for static calibrator and continuum lookup.
- `cont_dat`: Copies continuum frequency selection file to `cont.dat` in the execution directory.
- `jyperk_csv`: Copies Single-Dish Kelvin-to-Jansky conversion factors to `jyperk.csv`.
- `parameter_list`: Copies parameter override file to `parameter.list` (or `SEIP_parameter.list` / `QLIP_parameter.list` when matching VLASS recipe names).
- `ancillary`: Copies additional files or directory trees directly into the target execution directory (supports `[source, dest]` pairs or `source:dest` syntax for destination renaming).
- `datapath`: Sets explicit search directories for CASA `datapath` in `.casa/config.py`.
- `rundata`: Sets candidate measures directories for `rundata` / `measurespath` in `.casa/config.py` (or `rundata = "none"` to disable).

#### I. Named Execution Profiles (`-p` / `--profile`)

Apply pre-configured hardware tuning, recipes, threading, or telemetry settings defined under `[profiles.<name>]`:

```bash
# Execute with profile settings cascaded over base configuration
calibpipe run --vis /data/test.ms -p fast_turnaround

# Execute a self-contained profile with zero CLI target flags needed
calibpipe run -p regression/pipeline_fast

# CLI target and workdir options override settings defined in the profile
calibpipe run -p regression/pipeline_fast --vis=/data/custom.ms --workdir=./custom_run
```

When a profile specifies relative dataset paths or scripts, `calibpipe` resolves them against the search directories defined in `[paths].workspaces`.

#### J. Dry-Run Execution Inspection (`--dry-run`)

Inspect generated scripts, resolved directories, and exact commands before launching:

```bash
# Preview single-run driver execution
calibpipe run -p regression/pipeline_fast --dry-run
```

Outputs the generated execution wrapper (e.g. `casa_piperun.py`), target working directory, environment configuration, and exact `mpicasa` / `casa` commands without invoking any subprocesses.

### 2. Submit a Batch (Slurm & HTCondor)

`calibpipe submit` submits one or more pipeline jobs to a cluster workload manager (Slurm or HTCondor). The submission workflow resolves configuration layers, evaluates named profiles, validates submit host policies, and renders scheduler-specific submit scripts:

```mermaid
flowchart TD
    CLI["calibpipe submit"] --> Router["cli.py (Routing & Subparsers)"]
    Router --> BatchSubmit["batch.submit_request (SubmitRequest)"]
    BatchSubmit --> CfgLoad["config.load_merged_config (Cascading TOML Layers)"]
    CfgLoad --> Resolve["config.resolve_batch_options"]
    Resolve --> CheckProfiles{"Profile in [profiles]?"}
    CheckProfiles -- Yes --> ActiveProf["active_batch = cfg.batch + profile"]
    CheckProfiles -- No --> Err["ConfigError (Available profiles list)"]
    ActiveProf --> Override["Apply CLI Overrides (-p, --workdir, --env, --dry-run)"]
    Override --> BatchOpts["ResolvedBatchOptions"]
    BatchOpts --> SubmitHostCheck["check_submit_host"]
    SubmitHostCheck --> Snapshot["build_batch_snapshot → batch.&lt;ts&gt;.toml"]
    Snapshot --> SchedulerDispatch{"Scheduler?"}
    SchedulerDispatch -- Slurm --> SbatchGen["sbatch script (calibpipe run --config=&lt;snapshot&gt; --config-only ...)"]
    SchedulerDispatch -- HTCondor --> HTCGen["HTC submit &amp; .sh wrapper (calibpipe run --config=&lt;snapshot&gt; --config-only ...)"]
```

#### A. Submission Modes: Single-Job vs. Multi-Job Pipefile

There are two primary methods for submitting `mous`- and `recipe`-based pipeline jobs using profiles in TOML, depending on whether you are running a single tailored reduction or dispatching a batch queue of multiple MOUSes:

##### Method 1: Single-Job Profile Submission (Self-Contained TOML Profile)

When running a single targeted reduction, you can define both the `mous` (or `vis`) target, the `recipe`, the runtime environment, and the cluster scheduler resources directly inside a `[profiles.<name>]` block in `config.toml`. No external pipefile or text file is needed on disk.

###### Option 1A: Standard ALMA MOUS via PMR (`pipelineMakeRequest`)

```toml
[profiles."e2e_mous_calimage"]
desc = "ALMA Science Pipeline run for single MOUS uid://A001/X123/X456"

# Execution target and recipe:
mous = "uid://A001/X123/X456"
recipe = "calimage"                 # choices: cal, image, calimage, etc.

# Runtime environment (optional overrides):
env = "dev"
casa_root = "casa-6.7.7-7-py3.12.el8"
heuristics_dir = "PROJ-101/pipeline"

# Scheduler directives (Slurm / HTCondor):
partition = "batch2"
cores = 8                           # tasks / MPI ranks
mem = 128                           # total GB (or mem_per_cpu = "16G")
walltime = "24:00:00"
backup = true
```

###### Option 1B: Direct Recipe Reduction on Local MeasurementSets (`recipe_reducer`)

For offline data processing without PMR archive queries, specify `vis` and `recipe`:

```toml
[profiles."vlass_ccip_run"]
desc = "Direct procedure_vlassCCIP execution on split MeasurementSet"

# Visibilities and recipe (shorthand or XML filename/path):
vis = ["vlass/J232327+502432/rawdata/split.ms"]
recipe = "procedure_vlassCCIP.xml"

# Scheduler directives:
partition = "batch2"
cores = 16
mem = 248
```

###### Submitting Method 1:

Submit directly to Slurm using `calibpipe submit`:

```bash
# Submit single-job profile directly to Slurm:
calibpipe submit -p e2e_mous_calimage

# Preview the rendered #SBATCH script without submitting:
calibpipe submit -p e2e_mous_calimage --dry-run
```

**What `calibpipe submit` does under the hood:**
1. Evaluates `#SBATCH` directives (`partition`, `cores`, `mem`, `walltime`, etc.) from the profile.
2. Resolves paths, environment settings, and profile overrides up front into one shared frozen snapshot (`batch.<timestamp>.toml`).
3. Writes the persistent submission script to `scipipe_logdir` (e.g. `batch.e2e_mous_calimage_<timestamp>.sbatch`).
4. Dispatches `calibpipe run --config=<snapshot> --config-only -p=e2e_mous_calimage` inside the allocated job allocation.

---

##### Method 2: Multi-Job Queue Submission (Template Profile + Pipefile)

When you need to process a queue of multiple MOUSes using a shared cluster and environment configuration:

1. **Define a Template Profile in `config.toml`:**
   Configure shared cluster resources, CASA versions, and pipeline checkouts under `[profiles.<name>]`, omitting fixed `mous` and `recipe` keys so they act as a reusable template:

   ```toml
   [profiles."alma_batch_template"]
   desc = "Shared cluster template for ALMA batch reductions"
   partition = "batch2"
   cores = 8
   mem = 64
   walltime = "12:00:00"
   casa_root = "casa-6.7.7-7-py3.12.el8"
   heuristics_dir = "PROJ-101/pipeline"
   ```

2. **Create a `pipefile.txt`:**
   Add one reduction target per line. Lines can specify MOUS UIDs or direct MeasurementSets, with an optional recipe:

   ```text
   # <mous_uid> [recipe]
   uid://A001/X123/X001 calimage
   uid://A001/X123/X002 cal
   uid://A001/X123/X003 image
   ```

3. **Submit the Batch:**

   ```bash
   calibpipe submit pipefile.txt -p alma_batch_template
   ```

**What `calibpipe submit` does under the hood:**
1. Parses each line of `pipefile.txt` (ignoring blank lines and `#` comments).
2. Writes one shared frozen config snapshot (`batch.<timestamp>.toml`) in the batch log directory so later edits to `config.toml` cannot affect queued jobs.
3. Generates an independent batch script for each entry, injecting:
   `calibpipe run --config=<snapshot> --config-only -p=alma_batch_template --mous=<mous_uid>`
4. Submits each job sequentially with a 1-second delay between submissions to guarantee unique timestamped logs (`batch.<target>_<timestamp>.sbatch`) and avoid scheduler bursts.
5. Queries and prints active queue status via `squeue` (or `condor_q`).

---

##### Method 3: Regression Test Array Submission (Test Runfile & `collect = true`)

`calibpipe` natively supports executing regression test matrices as parallel Slurm or HTCondor job arrays. Each test executes as an independent batch job inside its own dedicated sub-working directory (`<base_workdir>/<test_name>`), complete with its own isolated `.casa/` runtime, logs, and JUnit XML report.

###### Option A: Submit from a Test Runfile

If you have a test runfile (e.g. generated by `calibpipe collect -o quick.run` or written manually):

```bash
# Submit each test in quick.run as a distinct Slurm batch job
calibpipe submit quick.run -p regression_fast

# Preview rendered submit scripts without submitting
calibpipe submit quick.run -p regression_fast --dry-run
```

###### Option B: Discover & Submit in One Shot (`collect = true`)

To discover test nodes from the profile's `test_target` (with optional `test_filter` and `test_marker`) and immediately dispatch them to the scheduler without generating an intermediate file, set `collect = true` in `[profiles.<name>]`:

```toml
[profiles.regression_fast]
collect = true
test_target = "tests/regression/fast"
test_marker = "regression"
test_filter = "uid___A002"
```

```bash
# Collect tests from the profile's test_target and submit each as a Slurm batch job
calibpipe submit -p regression_fast
```

###### Option C: Automated Post-Run Synthesis (`with_report = true`)

To automatically schedule a lightweight wrap-up Slurm job that waits for all individual test jobs to finish and synthesizes test results and code coverage, set `with_report = true` in the profile:

```toml
[profiles.regression_fast]
collect = true
test_target = "tests/regression/fast"
with_report = true
```

```bash
# Submit test array and automatically schedule a dependent finalize job
calibpipe submit -p regression_fast
```

The wrap-up job runs with `#SBATCH --dependency=afterany:<job1>:<job2>:...`, ensuring it executes immediately after all tests finish (regardless of pass/fail status). When triggered, it invokes `calibpipe report <base_workdir>`, aggregating all JUnit XML files, combining distributed `.coverage.*` datasets, and generating the consolidated HTML report.

###### Option D: Batch Concurrency Throttling (`max_concurrent`)

When dispatching large regression suites (e.g., 50–100 individual tests), submitting all jobs concurrently can saturate cluster queues, exhaust available node memory, or trigger Lustre filesystem IOPS bottlenecks.

`calibpipe` provides an optional native concurrency throttle using **chained Slurm dependency slots** via `max_concurrent` in `config.toml`:

```toml
[profiles."regression/casa674_array_fast"]
collect = true
test_target = "tests/regression/fast"
max_concurrent = 8
with_report = true
```

```bash
calibpipe submit -p regression/casa674_array_fast
```

**How Native Slot Throttling Works:**

- For the first `K` jobs (`i < K`), jobs are submitted without prerequisites and begin running as cluster resources allow.
- For each subsequent job (`i ≥ K`), `calibpipe` injects a Slurm dependency linking job `i` to job `i - K`:
  ```text
  #SBATCH --dependency=afterany:<Job_{i-K}_ID>
  ```
- **Zero Daemon Overhead**: Unlike queue managers that require a running python daemon or SSH process on the login node to poll and submit jobs, this throttling mechanism is delegated entirely to the native Slurm scheduler. Even if your terminal closes or the login node reboots, Slurm cleanly releases slot `i` the instant slot `i - K` terminates (whether it succeeds, fails, or is cancelled).
- **Superior to Standard Job Arrays**: Unlike Slurm `#SBATCH --array=1-N%K` (which collapses all tasks into a single opaque job ID where individual task names are obscured in `squeue`), `calibpipe` gives every single test a descriptive job name (`{profile}/{test_name}_{timestamp}`) and separate `.sbatch` logs while enforcing the exact same concurrency ceiling.

**What `calibpipe submit` does for test arrays:**
1. **Target Identification:** Detects pytest node IDs (containing `::`) in runfiles or collects them on the fly via `calibpipe.collect`.
2. **Sub-Workdir Segregation:** Assigns each test an independent working directory: `<base_workdir>/<test_name>`.
3. **Directory Rotation Safeguard:** Sets scheduler `--chdir` to `<base_workdir>` (the parent directory) rather than `<test_workdir>`, allowing `backup=true` to rotate and re-create `<test_workdir>` safely without violating the current working directory boundary.
4. **Environment & Command Generation:** Assembles an isolated execution command referencing the frozen batch snapshot:
   ```bash
   calibpipe run --config=<snapshot> --config-only -p <profile> \
     --workdir=<base_workdir>/<test_name> \
     --cmd="import os, sys, pytest; ..." --isolate-root
   ```
   (where the inline `--cmd` pops `SCIPIPE_ROOTDIR`, prepends `<SCIPIPE_HEURISTICS>/tests` to `sys.path`, and invokes `pytest.main(['-vvs', '--junitxml=regression-results-<test_name>.xml', node])`).
5. **Scheduler Tagging:** Names each batch job after `{profilename}/{test_name}_{timestamp}`, making individual tests easily trackable in `squeue`.
6. **Concurrency Throttling (Optional):** When `max_concurrent = K` is configured, injects `#SBATCH --dependency=afterany:<Job_{i-K}>` into Job `i`, guaranteeing at most `K` test jobs run concurrently without requiring a daemon process on the login node.
7. **Automated Finalize Wrap-up (Optional):** When `with_report = true` is configured in the profile, submits a dependent wrap-up Slurm job (`#SBATCH --dependency=afterany:...`) that triggers `calibpipe report` across the run directory.

---

##### Method 4: Dynamic Target, Recipe & Environment Overrides on Submission

Targets, recipes, environments, and working directories can be overridden on the fly during submission via CLI flags:

```bash
# Override recipe dynamically:
calibpipe submit -p e2e_mous_calimage --recipe=cal

# Override MOUS target dynamically:
calibpipe submit -p e2e_mous_calimage --mous=uid://A001/X123/X789

# Override runtime environment and working directory:
calibpipe submit -p e2e_mous_calimage --env=dev --workdir=/lustre/runs/custom_mous
```

> [!TIP]
> `calibpipe submit` is the canonical batch submission subcommand, and `./scripts/runbatch.py` forwards directly to `calibpipe submit`.

#### B. Profile Execution Target Matrix

Profiles defined under `[profiles.<name>]` can encapsulate any of the five execution targets:

| Target Type | Profile TOML Definition | Execution Mechanism |
| :--- | :--- | :--- |
| **MOUS (ALMA PMR)** | `mous = "uid://A001/X1/X1"`<br/>`recipe = "calimage"` | Queries metadata via `pipelineMakeRequest`, creates OUS directory tree, stages inputs, and runs `executeppr`. |
| **Direct Recipe** | `vis = "data/target.ms"`<br/>`recipe = "procedure_hifa_calimage.xml"` | Executes `pipeline.recipereducer.reduce(vis=[...], procedure=...)` directly in `<workdir>`. |
| **Standalone PPR** | `ppr = "data/PPR.xml"`<br/>`vla = false` (or `true`) | Executes `executeppr` (ALMA) or `executevlappr` (VLA) on the pre-existing request file. |
| **Custom Script** | `script = "scripts/benchmark.py"` | Executes custom Python/CASA script within isolated `.casa/` runtime. |
| **Inline Command** | `cmd = "import pytest; pytest.main()"` | Executes inline Python snippet directly within the CASA interpreter. |

#### C. Scheduler Support: Slurm & HTCondor

`calibpipe` renders native submission scripts for both Slurm and HTCondor:

##### Slurm Submission (Default)
- Directives rendered: `#SBATCH --partition`, `#SBATCH --ntasks`, `#SBATCH --cpus-per-task`, `#SBATCH --mem` (or `--mem-per-cpu`), `#SBATCH --time`, `#SBATCH --hint`, `#SBATCH --distribution`, `#SBATCH --no-requeue`.
- Submission command: `sbatch batch.<job>.sbatch`
- Queue inspection: `squeue -u $USER`

##### HTCondor Submission
- Select via `[batch].scheduler = "htcondor"` or `scheduler = "htcondor"` in `[profiles.<name>]`.
- Generates both wrapper `batch.<job>.sh` (`ulimit`, `umask`, `calibpipe run --config=<snapshot> --config-only ...`) and submit description `batch.<job>.htc` (`request_cpus`, `request_memory`, `initialdir`, `notification`, ClassAd requirements).
- Submission command: `condor_submit batch.<job>.htc`
- Queue inspection: `condor_q $USER`

#### D. Batch Dry-Run Inspection (`--dry-run`)

Preview batch submission scripts and planned execution without submitting jobs to the scheduler:

```bash
# Preview Slurm batch submission
calibpipe submit quick.run -p debug --dry-run

# Preview HTCondor batch submission (with scheduler = "htcondor" in profile)
calibpipe submit quick.run -p condor_debug --dry-run
```

When `--dry-run` is supplied, `calibpipe submit`:
- Assembles and saves the complete job submit script (e.g. `batch.<job>.sbatch` or `batch.<job>.htc`) to disk.
- Prints the exact submission command that would be executed (e.g. `sbatch ...` or `condor_submit ...`).
- Skips scheduler execution, queue polling, and inter-submission pauses.

#### Multi-Job Concurrency and HPC Safety

When running large batches across cluster nodes (`calibpipe submit ...`), `calibpipe` implements several safeguards to guarantee conflict-free concurrent execution:

##### 1. Pixi Lockfile & Environment Concurrency (`--frozen`)

When running under a modular Pixi environment (`--env=<pixi_env>`):

- **Immutable Lockfile Enforcement:** `calibpipe` executes Pixi with `pixi run --frozen`. This guarantees Pixi
  treats `pixi.lock` as strictly read-only and never attempts to re-solve dependencies or update the lockfile
  concurrently across worker nodes.
- **Script-Based Execution (`casa_piperun.py`):** Rather than passing complex, multi-statement inline Python
  strings through nested shell layers (which can strip quotes and cause syntax errors in CASA), `calibpipe` writes
  a clean `casa_piperun.py` script directly into each MOUS's `working/` directory and executes it via
  `-c /path/to/working/casa_piperun.py`. Using an absolute path ensures CASA recognizes it as a script file
  regardless of the initial working directory of the Pixi task or MPI launcher.

> [!IMPORTANT] Pre-install Pixi Environments Before Large Batches
> Always run `pixi install` once in your Pixi workspace before submitting multi-job batches. Once initialized,
> worker nodes access shared conda/pip binaries strictly in **read-only mode** without filesystem lock
> contention. If multiple uninitialized jobs start concurrently, they will attempt to unpack packages
> simultaneously into `.pixi/envs/`, leading to race conditions.

##### 2. Isolated CASA Runtime State (`.casa`) & SQLite History Locking Prevention

Monolithic and modular CASA default to writing user configuration, cache, and telemetry SQLite state into
`~/.casa` in your home directory. In a parallel cluster environment, concurrent jobs writing to `~/.casa` can
collide or corrupt databases. Furthermore, on parallel network filesystems (e.g. Lustre or NFS), IPython's default
SQLite command history database (`history.sqlite`) encounters severe POSIX file lock contention when multiple MPI ranks
initialize concurrently, leading to 32-second file lock timeouts or `sqlite3.OperationalError: database is locked` crashes.

`calibpipe` isolates CASA state per run directory:

- **Isolated Runtime Tree:** Creates a dedicated `.casa/` directory inside `<workdir>/.casa/`.
- **Customized Configuration:** Renders private `config.py` (configuring `datapath`, `rundata`, telemetry suppression) and `startup.py`.
- **CLI Flag Passing:** Passes `--cachedir`, `--configfile`, and `--startupfile` pointing directly into that job's working directory so jobs never touch or contend for a shared `~/.casa/` directory.
- **MPI SQLite History Lock Suppression:** For batch runs (`-c casa_piperun.py`), `calibpipe` generates `.casa/ipython/profile_default/ipython_config.py` containing:
  ```python
  c = get_config()
  c.HistoryManager.enabled = False
  ```
  Disabling IPython's `HistoryManager` completely bypasses SQLite history file creation. This eliminates Lustre lock contention and prevents multi-rank MPI startup delays or crashes.
- **Interactive History Preservation:** When launching interactive developer shells (`calibpipe run -i`), `calibpipe` preserves `c.HistoryManager.enabled = True` (alongside `%load_ext autoreload`) so interactive command recall and history remain fully functional.

##### 3. Slurm Submission Safety

- **Unique Job Records:** Each job in a batch receives a unique timestamped name (`batch.<mous>_<timestr>`) with
  dedicated `.out`, `.err`, and `.sbatch` files.
- **Submission Staggering:** `calibpipe submit` pauses for 1 second between consecutive submissions to ensure unique
  timestamp resolution and prevent submission bursts.
- **Isolated Working Directories:** Each MOUS executes exclusively within its own
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

Inspect active settings, paths, and environment defaults resolved across configuration layers without executing anything:

```bash
calibpipe config show --env=main
```

When multiple configuration files are detected, `calibpipe` displays the loaded hierarchy and per-key sources in order:

```text
calibpipe Configuration Overview
Configuration Layers (lowest to highest priority):
  /etc/calibpipe/config.toml
  /home/user/.config/calibpipe/config.toml
Default Env: main
Default Profile: (none)
Selected Env: main
Selected Profile: (none)
...
```

Or inspect an alternate configuration file or isolated run:

```bash
calibpipe config show --config=/path/to/custom_config.toml --env=dev
calibpipe config show --config=/path/to/custom_config.toml --no-site-config
```

### 5. Profile Management CLI (`calibpipe profile`)

Inspect and query all defined profiles from the CLI without manually reading configuration files:

#### A. List Profiles (`calibpipe profile list`)

```bash
calibpipe profile list
# or simply:
calibpipe profile
```

Displays all defined `[profiles.<name>]` entries:

```text
Available profiles:
  debug
  heavy
  interactive_dev
  regression/pipeline_fast
  unit_tests
```

#### B. Show Profile Details (`calibpipe profile show <name>`)

```bash
calibpipe profile show regression/pipeline_fast
```

Displays configured fields for the selected profile:

```text
Profile: regression/pipeline_fast
  desc: Fast interferometry calibration regression test
  env: dev
  vis: ['regression/fast_vis.ms']
  recipe: procedure_hifa_calimage.xml
  nprocs: 8
  workdir: runs/regression_fast
  dry_run: False
```

### 6. Pipeline Request Generation & Archive Fetching (`calibpipe pmr`)

`calibpipe pmr` generates the pipeline processing directory structure, creates processing requests (`PPR.xml`), and optionally downloads raw ASDM visibility data and calibration / restore products for an ALMA Observing Unit Set (MOUS).

It replaces the external standalone executable `pipelineMakeRequest` with direct native execution of ALMA `MakeProcessingRequests`.

#### Common Use Cases

```bash
# Standard workflow: query archive and generate PPR (no data downloads)
calibpipe pmr uid://A001/X1/X1

# Download raw ASDM visibility data from archive
calibpipe pmr uid://A001/X1/X1 --download

# Download calibration and restore products
calibpipe pmr uid://A001/X1/X1 --restore

# Download both raw ASDMs and restore products
calibpipe pmr uid://A001/X1/X1 --download --restore

# Filter specific ASDMs and ignore QA0 status
calibpipe pmr uid://A001/X1/X1 --download -a uid://A002/X1,uid://A002/X2 --ignore-qa0

# Strip trailing timestamp from generated project directory (e.g. 2099.1.00001.S)
calibpipe pmr uid://A001/X1/X1 --strip-timestamp

# Explicitly rename generated project directory
calibpipe pmr uid://A001/X1/X1 --rename my_project_run

# Preview Java command without execution
calibpipe pmr uid://A001/X1/X1 --dry-run
```

---

### 7. Regression Test Discovery & Collection (`calibpipe collect`)

`calibpipe collect` discovers and inspects pytest test node IDs from pipeline source checkouts using the CASA Python interpreter. It provides an automated CLI tool for test discovery, filtering, and runfile generation, replacing manual collection and legacy test scheduling scripts.

#### Common Use Cases

```bash
# Discover fast regression tests using the environment and heuristics defined in a profile
calibpipe collect -p regression_fast

# Filter discovered tests by marker expression (e.g. 'regression and not slow')
calibpipe collect tests/regression/fast -m "regression and not slow"

# Filter discovered tests by keyword substring
calibpipe collect -p regression_fast -k "uid___A002"

# Export discovered test nodes directly into a two-column test runfile
calibpipe collect tests/regression/fast -p regression_fast -o quick.run

# Pass extra pytest options during collection
calibpipe collect -p regression_fast --test-opts="--collect-only"
```

#### Runfile Format

When `-o <runfile>` is specified, `calibpipe collect` writes a runfile formatted with `<test_name>\t<node_id>`:

```text
# calibpipe test runfile
# Syntax: <test_name>  <node_id>
uid___A002_Xc46ab2_X15ae_repSPW_spw16_17_small__PPR	tests/regression/fast/alma_if_fast_test.py::test_uid___A002_Xc46ab2_X15ae_repSPW_spw16_17_small__PPR__regression
13A_537__calibration__PPR	tests/regression/fast/vla_fast_test.py::test_13A_537__calibration__PPR__regression
```

This runfile can be submitted directly with `calibpipe submit quick.run -p regression_fast`.

---

### 8. Regression Reporting & Coverage Synthesis (`calibpipe report`)

`calibpipe report` programmatically inspects, synthesizes, and consolidates test results and code coverage data across distributed batch test executions. It replaces legacy post-processing scripts with a unified, cross-platform reporting tool.

#### Core Capabilities

1. **Distributed Coverage Synthesis:**
   - Recursively discovers `.coverage.*` files from individual test subdirectories.
   - Combines them into a single consolidated `<workdir>/.coverage` dataset using `coverage.py`.
   - Generates a terminal summary table (`coverage report`) and a fully hyperlinked HTML coverage report (`coverage html`) saved in `<workdir>/htmlcov/index.html`.
   - Automatically detects whether the `coverage` Python package is available locally or falls back to the configured CASA Python environment (`<casa_root>/bin/python3 -m coverage`).

2. **JUnit XML Aggregation:**
   - Discovers all `regression-results-*.xml` generated by individual test jobs.
   - Parses total counts of passed, failed, error, and skipped tests along with elapsed runtime.
   - Collects and formats detailed failure and error messages.

3. **Persistent Summary Reports:**
   - Displays a formatted summary in the terminal and saves persistent summary documents to disk:
     - `<workdir>/test-summary.txt` (clean ASCII table)
     - `<workdir>/test-summary.md` (GitHub-flavored Markdown table)

#### Usage

```bash
# Run report and combine coverage in current working directory
calibpipe report

# Run report in a specific regression run directory
calibpipe report /path/to/tests/projs/regression/casa674_array_fast

# Remap source paths from remote cluster paths to local checkout
calibpipe report /path/to/workdir --remap-source=/lustre/cluster/pipeline/path=/local/dev/pipeline

# Explicitly specify CASA root for coverage execution fallback
calibpipe report /path/to/workdir --casa-root=/path/to/casa-6.7.4

# Skip coverage combine and generate only XML test summary
calibpipe report /path/to/workdir --no-coverage

# Skip XML test summary and only combine coverage
calibpipe report /path/to/workdir --no-xml
```

#### Automated Finalize Workflow with Slurm

Rather than polling running batch jobs manually or running report commands by hand, `calibpipe submit` can automate this reporting step when `collect = true` and `with_report = true` are set in the profile:

```bash
# Automatically submit test jobs and a dependent wrap-up job:
calibpipe submit -p regression/casa674_array_fast
```

When all test jobs finish, Slurm launches the finalize job (`#SBATCH --dependency=afterany:<ids>`), executing `calibpipe report <workdir>` automatically.

---

### 9. Standalone Coverage Combining & Source Remapping (`calibpipe coverage`)

`calibpipe coverage` discovers, combines, and summarizes distributed `coverage.py` data files without parsing JUnit XMLs.

#### Behavior

1. **Recursive Discovery (`find_coverage_files`):** Walks subdirectories for `.coverage.*` files produced by parallel pytest or Slurm array runs, excluding the destination `.coverage` file.
2. **Source Path Remapping (`--remap-source`):** When tests run on compute nodes or containers (e.g., `/lustre/cluster/users/{user}/tickets/...`), `.coverage.*` records remote paths. `--remap-source=<remote_prefix>=<local_prefix>` translates those paths back to a local checkout for terminal and HTML reports.
3. **CASA Python Fallback:** If `coverage` is not installed in the active Python environment, `calibpipe` falls back to `<casa_root>/bin/python3 -m coverage`.

#### Usage

```bash
# Combine all .coverage.* files in current directory and generate HTML report
calibpipe coverage

# Combine coverage in a specific batch workdir
calibpipe coverage /path/to/tests/projs/regression/casa674_array_fast

# Remap cluster execution paths to a local development checkout
calibpipe coverage /path/to/workdir \
  --remap-source=/lustre/cluster/pipeline=/home/developer/pipeline

# Skip HTML report generation and only generate terminal output
calibpipe coverage /path/to/workdir --no-html

# Specify CASA root directory for coverage fallback
calibpipe coverage /path/to/workdir --casa-root=/opt/casa/casa-6.7.4
```

#### Programmatic Python API

The coverage synthesis machinery can also be called directly from Python:

```python
from pathlib import Path
from calibpipe.coverage import combine_with_report, find_coverage_files

# Discover all coverage files
cov_files = find_coverage_files(["/path/to/workdir"])

# Combine and generate reports
results = combine_with_report(
    search_paths=["/path/to/workdir"],
    output_dir="/path/to/workdir",
    source_paths=["/lustre/remote/path=/local/path"],
    casa_root="/opt/casa/casa-6.7.4",
)
print(f"Total coverage: {results['total_coverage']}%")
print(f"HTML Report: {results['index_file']}")
```

---

### 10. Updating Regression Reference Files (`calibpipe update-ref`)

When pipeline changes intentionally alter regression metrics or QA score keys, `calibpipe update-ref` stages updated `*.results.txt` files in `pipeline-testdata` while keeping existing per-key tolerances (`:::<tol>`).

- **Tolerance preservation & sorting:** Merges `*.NEW.results.txt` into the baseline reference, preserving `:::<tol>` suffixes and alphabetical key order (`PipelineTester` requirement).
- **Candidate staging:** Writes `<target>.results.txt.candidate` by default; pass `--apply` (or `--promote`) only after reviewing the diff.
- **Version stamping:** Reads CASA and Pipeline versions from live weblogs (`pipeline-*/html/index.html`), `working/pipeline_aquareport.xml`, or `casa-*.log` / `batch.out` (ignoring pre-existing `products/` in restore tests), and keeps suffixes such as `.restore.results.txt`.
- **Recursive batch scan:** `calibpipe update-ref ./` skips heavy subdirectories (`*.ms`, `pipeline-*`, `rawdata/`, `products/`, `htmlcov/`) and ignores unchanged runs unless `--force` is given.

```bash
# Preview diffs across all test runs under the current regression directory
calibpipe update-ref --diff-only ./

# Stage .candidate files in pipeline-testdata for manual inspection
calibpipe update-ref ./

# Override version tags or keep the baseline filename's existing version
calibpipe update-ref --casa-ver=6.8.0-4 --pipeline-ver=2026.3.0.43 ./
calibpipe update-ref --keep-version ./

# Promote staged changes to the canonical reference files (creates .bak backups)
calibpipe update-ref --apply ./
```

---

## Backward Compatibility

`calibpipe` preserves runner shims in `scripts/` so existing scripts and scheduled jobs continue to work:

- `scripts/calibPipeIF.py` → `calibpipe run` / `calibpipe.runner`
- `scripts/runbatch.py` → `calibpipe submit` / `calibpipe.batch`
- `scripts/calibpipe_env.sh` → `calibpipe env`
- `scripts/update_regression_reference.py` → `calibpipe update-ref`

This allows gradual migration to the unified `calibpipe` CLI without breaking older job wrappers or polluting the global `$PATH`.

### Specifying a Python Interpreter for Legacy Shims

`calibpipe` requires Python ≥ 3.10. On clusters where the system `python3` is older, the Python shims in `scripts/` use `#!/usr/bin/env python3` and add `src/` automatically, so you can select your own interpreter (such as conda, miniforge, `uv`, or the repository `.venv`):

```bash
# 1. Invoke your interpreter explicitly (works in any shell or batch script):
/path/to/conda/envs/py312/bin/python /path/to/calibpipe/scripts/calibPipeIF.py --mous=uid://A001/X128a/Xb9 --env=main
/path/to/calibpipe/.venv/bin/python /path/to/calibpipe/scripts/runbatch.py quick.run --env=main
uv run --project /path/to/calibpipe python /path/to/calibpipe/scripts/calibPipeIF.py --mous=...

# 2. Or put Python >= 3.10 first on PATH (or activate conda) so #!/usr/bin/env python3 picks it up:
conda activate py312
/path/to/calibpipe/scripts/calibPipeIF.py --mous=uid://A001/X128a/Xb9 --env=main
```

For `scripts/calibpipe_env.sh`, if `calibpipe` is not on `$PATH`, the script automatically checks `<repo>/.venv/bin/python` before falling back to `python3`. You can also override the interpreter explicitly via `CALIBPIPE_PYTHON`:

```bash
CALIBPIPE_PYTHON=/path/to/conda/envs/py312/bin/python source /path/to/calibpipe/scripts/calibpipe_env.sh --env=main
```

> [!TIP] Running directly from a checkout without installing dependencies
> Python ≥ 3.11 includes `tomllib` in the standard library, so a bare conda/miniforge Python ≥ 3.11 can run the `scripts/` shims directly from a repository checkout (`psrecord-io` is only required when `psrecord = true` profiling is enabled). Python 3.10 requires `tomli`.

---

## Testing

### Offline Unit Testing

Run the test suite from the repository root:

```bash
pytest tests/
# or with uv:
uv run --extra dev pytest tests/
```

The test suite runs 100% offline without requiring local CASA installations, Slurm daemons, or cluster filesystems. Subprocess invocations (`sbatch`, `casa`, `pipelineMakeRequest`) are mocked, and driver execution steps and scheduler scripts are validated against golden reference calls (`tests/reference/calibpipeif_calls.txt`) and unit test assertions.

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
   calibpipe run --mous=uid://A001/X1/X1 --env=main --dry-run
   ```

3. **Site & User Configuration Separation:**
   On shared observatory clusters, cluster administrators maintain site infrastructure in `config.site.toml`, `/etc/calibpipe/config.toml`, or via `$CALIBPIPE_SITE_CONFIG`. Individual users keep only personal scratch paths or custom branch checkouts in `~/.config/calibpipe/config.toml`, inheriting all cluster tooling automatically.

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
- [Execution Internals](internals.md): deep dive on project ID resolution, PMR staging, and OUS structure
- [Changelog & History](changelog.md): evolution from prototype, version-by-version release history, and pending features
- `config.example.toml`: unified configuration template (for site admins and individual users)
- [Migrating to 0.2](migration-0.2.md): breaking 0.2 interface changes and canonical TOML/CLI replacements
- [API Reference](api/index.md): generated API reference
