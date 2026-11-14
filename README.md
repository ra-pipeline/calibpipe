# calibpipe

Detailed package documentation is available in [docs/guide.md](docs/guide.md).

**calibpipe** is a lightweight, standalone Python package for driving single and batch ALMA Science Pipeline executions on local workstations or HPC Slurm clusters (e.g., NAASC cluster).

> [!WARNING]
> **Cluster & Pipeline Tooling Dependencies**
>
> `calibpipe` is a validation and testing tool; running it requires access to CASA + ALMA pipeline builds, Slurm (for batch execution), and ALMA datapacker / `pipelineMakeRequest` (PMR) when running in MOUS mode.
>
> Configure `config.toml` using local or site-specific paths (see `config.example.toml`).

---

## Features

- **Decoupled Architecture:** Lightweight Python 3 (≥ 3.10) package with minimal runtime dependencies (`psrecord-io[plot]`, plus `tomli` on Python 3.10).
- **Location & Version Agnostic:** Never hardcodes personal paths. All environments, CASA builds, and pipeline checkouts live in a gitignored `config.toml`.
- **Compatibility Wrappers:** Preserves thin wrapper scripts in `scripts/` (`scripts/calibPipeIF.py`, `scripts/runbatch.py`, `scripts/calibpipe_env.sh`) that pass 0.2 CLI arguments directly to `calibpipe run`, `calibpipe submit`, and `calibpipe env`.

---

## Installation

### Development Setup with `uv` (Recommended)

```bash
cd /path/to/calibpipe
uv sync --extra dev --extra docs
```

Run commands directly inside the managed environment with `uv run`:

```bash
uv run calibpipe config show --env=main
uv run calibpipe run --mous=uid://A001/X128a/Xb9 --env=main
uv run --extra dev pytest tests/
uv run --extra docs zensical serve
```

### Isolated CLI Tool Install via `uv tool` (Recommended on Clusters)

Install directly to your user tool directory (`~/.local/bin/calibpipe`) with live edits linked:

```bash
uv tool install --editable /path/to/calibpipe
```

> [!NOTE] **Cluster users — NFS home directories:** The default `uv` cache (`~/.cache/uv`) counts against your home directory quota and can trigger cross-filesystem hardlink errors. Redirect it to a local scratch filesystem by adding `export UV_CACHE_DIR="/scratch/$USER/.cache/uv"` to your shell profile. Run `uv cache prune` periodically to remove stale entries. See [docs/guide.md](docs/guide.md) for full details.

### Alternative Installation Methods

- **Standard Editable / Local Install:**

  ```bash
  pip install -e .
  ```

- **Isolated Install via `pipx`:**

  ```bash
  pipx install /path/to/calibpipe
  ```

---

## Configuration

Version 0.2 uses a configuration-first CLI. Execution tuning and scheduler resources belong in TOML profiles. See [the migration guide](docs/migration-0.2.md); the compatibility wrappers in `scripts/` pass arguments straight through to `calibpipe run`, `calibpipe submit`, and `calibpipe env`.

1. Copy `config.example.toml` to `config.toml` in your working directory (or `~/.config/calibpipe/config.toml`):

   ```bash
   cp config.example.toml config.toml
   ```

2. Edit `config.toml` to define your CASA builds and pipeline checkouts:

   ```toml
   default_env = "main"

   [paths]
   scipipe_rootdir = "/data/pipeline/root/{user}"
   scipipe_logdir  = "/data/pipeline/logs/{user}"
   pickle_dir      = "/data/pipeline/pickles/{user}"
   obscaldir       = "/data/pipeline/obscal2021"

   [envs.main]
   casa_root = "/opt/casa/casa-6.7.4-8-pipeline-2026.2.0.23-py3.12"

   [envs.pl2025]
   casa_root = "/opt/casa/casa-6.6.6-17-pipeline-2025.1.0.35-py3.10"
   heuristics_dir = "{casa_root}/pipeline"
   ```

**Cascading Configuration Architecture:**
Discovers and deep-merges configuration across layers:
Site config (`/etc/calibpipe/config.toml`, `$CALIBPIPE_SITE_CONFIG`, or `config.site.toml`) → User config (`~/.config/calibpipe/config.toml`, `~/.calibpipe/config.toml`) → Workspace (`./config.toml` or `./.calibpipe.toml`) → Explicit config (`--config=<path>` or `$CALIBPIPE_CONFIG`, which replaces user/workspace layers while retaining site defaults unless `--no-site-config` is passed; `calibpipe run --config-only` loads only `--config`).
On shared clusters, users inherit site infrastructure automatically and only specify personal overrides. Pass `--no-site-config` to run fully isolated.

For the complete schema (`[paths]`, `[envs.<name>]`, `[site]`, `[batch]`, `[run]`), variable interpolation, and architecture flowcharts, see [User Guide: Configuration](docs/guide.md#configuration).

---

## Usage

### 1. Run a Single MOUS

```bash
# Using unified CLI
calibpipe run --mous=uid://A001/X128a/Xb9 --env=main --recipe=calimage

# Compatibility wrapper (passes arguments to `calibpipe run`)
./scripts/calibPipeIF.py --mous=uid://A001/X128a/Xb9 --env=main
```

### 2. Submit to Slurm

```bash
# Using unified CLI
calibpipe submit quick.run --env=main
calibpipe submit quick.run --profile=debug --env=main

# Compatibility wrapper (passes arguments to `calibpipe submit`)
./scripts/runbatch.py quick.run --profile=debug --env=main
```

`quick.run` format:

```text
# <mous_uid> [recipe]
uid://A001/X128a/Xb9  calimage
uid://A002/Xcff05c/Xd calimage
```

### 3. Interactive Shell Setup

Set the ALMA pipeline environment variables directly in your current shell session (`bash` or `zsh`):

```bash
# Direct eval:
eval "$(calibpipe env --env=main)"

# Or source the helper script:
source scripts/calibpipe_env.sh --env=main

# Inspect resolved variables:
calibpipe env --env=main --print-env
```

Use `scripts/calibpipe_env.sh` when you want those variables applied directly to your current shell and prefer the `source ...` workflow. It is a compatibility wrapper around `calibpipe env`, so it should be sourced rather than executed.

> [!NOTE]
> If the default OS `python3` is older than 3.10, run the Python wrappers with your own interpreter (`/path/to/conda/bin/python scripts/calibPipeIF.py ...`, or activate a Python ≥ 3.10 environment first), or set `CALIBPIPE_PYTHON=/path/to/conda/bin/python` when sourcing `scripts/calibpipe_env.sh`. See [Backward Compatibility](docs/guide.md#backward-compatibility) for details.

### 4. Inspect Configuration

Inspect active settings, paths, and environment defaults resolved from `config.toml`:

```bash
calibpipe config show --env=main
```

For advanced CLI options, Slurm batch queues, and legacy wrapper compatibility, see [User Guide: Workflows](docs/guide.md#command-line-workflows). For staging lifecycle, PMR resolution, and directory layout details, see [Execution Internals](docs/internals.md). For library evolution and release notes, see [Changelog](docs/changelog.md).

---

## Testing

Run the offline test suite:

```bash
uv run --extra dev pytest tests/
```
