# calibpipe

Detailed package documentation is available in [docs/guide.md](docs/guide.md).

**calibpipe** is a lightweight, standalone Python package for driving single and batch ALMA Science Pipeline executions on local workstations or HPC Slurm clusters (e.g., NAASC cluster).

> [!WARNING]
> **Observatory Infrastructure & Cluster Dependency**
>
> `calibpipe` is an operational driver designed for ALMA Science Pipeline operations on observatory HPC clusters (e.g., NAASC) or specialized pipeline workstations. Running `calibpipe` on a standard personal machine without valid ALMA pipeline (or CASA-based pipeline) installations, Slurm, ALMA datapacker, and `pipelineMakeRequest` (PMR) will **not work out-of-the-box**.
>
> Operators on observatory clusters should configure `config.toml` using site-specific cluster paths (see `notes/config.internal.example.toml`).

---

## Features

- **Decoupled Architecture:** Pure Python 3 ($\ge 3.10$) standard library runtime with zero heavy dependencies.
- **Location & Version Agnostic:** Never hardcodes personal paths. All environments, CASA builds, and pipeline checkouts live in a gitignored `config.toml`.
- **100% Backward Compatible:** Preserves legacy runner shims in `scripts/` (`scripts/calibPipeIF.py`, `scripts/runbatch.py`, `scripts/calibpipe_env.sh`).

---

## Installation

### Standard Editable / Local Install

```bash
cd /path/to/calibpipe
pip install -e .
```

### Development Setup with `uv`

```bash
cd /path/to/calibpipe
uv sync --extra dev --extra docs
```

Run commands inside the managed environment with `uv run`, for example:

```bash
uv run --extra dev pytest tests/
uv run --extra docs zensical serve
```

### Isolated Install via `pipx` (Recommended for Cluster Users)

```bash
pipx install /path/to/calibpipe
```

---

## Configuration

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

**Config Resolution Order:**
Searches `--config=<path>` $\to$ `$CALIBPIPE_CONFIG` $\to$ `./config.toml` $\to$ `~/.config/calibpipe/config.toml`.

For the complete 3-tier schema (`[paths]`, `[envs.<name>]`, `[site]`), variable interpolation, and architecture flowcharts, see [User Guide: Configuration](docs/guide.md#configuration).

---

## Usage

### 1. Run a Single MOUS

```bash
# Using unified CLI
calibpipe run --mous=uid://A001/X128a/Xb9 --env=main --recipe=calimage

# Or direct shortcut
calibpipe --mous=uid://A001/X128a/Xb9 --env=main

# Legacy interface (backward-compatible script shim)
./scripts/calibPipeIF.py --mous=uid://A001/X128a/Xb9 --env=main
```

### 2. Batch Execution on Slurm

```bash
# Using unified CLI
calibpipe batch quick.run --env=main -c 8 -m 248 -p plwg

# Legacy interface (backward-compatible script shim)
./scripts/runbatch.py quick.run --env=main -c 8 -m 248 -p plwg
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
# Modern direct eval:
eval "$(calibpipe env --env=main)"

# Or source the helper script:
source scripts/calibpipe_env.sh --env=main

# Inspect resolved variables:
calibpipe env --env=main --print-env
```

Use `scripts/calibpipe_env.sh` when you want those variables applied directly to your current shell and prefer the legacy `source ...` workflow. It is a compatibility wrapper around `calibpipe env`, so it should be sourced rather than executed.

For advanced CLI options, Slurm batch queues, and legacy wrapper compatibility, see [User Guide: Workflows](docs/guide.md#command-line-workflows).

---

## Testing

Run the full test suite verifying against golden reference specifications:

```bash
python3 -m unittest discover -s tests -v
# or
pytest tests/
# or with uv
uv run --extra dev pytest tests/
```
