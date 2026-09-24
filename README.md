# calibpipe

**calibpipe** is a lightweight, standalone Python package for driving single and batch CASA + ALMA Science Pipeline executions on local workstations or HPC Slurm clusters (e.g., NAASC cluster).

---

## Features

- **Decoupled Architecture:** Pure Python 3 ($\ge 3.10$) standard library runtime with zero heavy dependencies.
- **Location & Version Agnostic:** Never hardcodes personal paths. All environments, CASA builds, and pipeline checkouts live in a gitignored `config.toml`.
- **Unified Modern CLI:** Provides `calibpipe run`, `calibpipe batch`, and `calibpipe env` subcommands.
- **Pure-Python Shell Environment Resolver:** Replaces ~300 lines of brittle Bash TOML parsing with direct shell export generation.
- **100% Backward Compatible:** Provides executable shims for legacy `calibPipeIF.py`, `runbatch.py`, and `calibpipe_env.sh`.

---

## Installation

### Standard Editable / Local Install
```bash
cd /path/to/calibpip
pip install -e .
```

### Isolated Install via `pipx` (Recommended for Cluster Users)
```bash
pipx install /path/to/calibpip
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
   scipipe_rootdir = "/lustre/naasc/sciops/comm/{user}/pipeline/root"
   scipipe_logdir  = "/lustre/naasc/sciops/comm/{user}/pipeline/logs"
   pickle_dir      = "/lustre/naasc/sciops/comm/{user}/pipeline/pickles"
   obscaldir       = "/lustre/naasc/sciops/comm/rindebet/pipeline/obscal2021"

   [envs.main]
   casa_root = "/stor/naasc/sciops/comm/dkunneri/pipeline/CASA_PL/casa-6.7.4-8-pipeline-2026.2.0.23-py3.12.el8"

   [envs.pl2025]
   casa_root = "/lustre/naasc/sciops/comm/rindebet/casa/casa-6.6.6-17-pipeline-2025.1.0.35-py3.10.el8"
   heuristics_dir = "{casa_root}/pipeline"
   ```

**Config Resolution Order:**
1. `--config=<path>` CLI flag
2. `$CALIBPIPE_CONFIG` environment variable
3. `./config.toml` in the current working directory
4. `~/.config/calibpipe/config.toml`

---

## Usage

### 1. Run a Single MOUS
```bash
# Using unified CLI
calibpipe run --mous=uid://A001/X128a/Xb9 --env=main --recipe=calimage

# Or direct shortcut
calibpipe --mous=uid://A001/X128a/Xb9 --env=main

# Legacy interface (backward-compatible)
./calibPipeIF.py --mous=uid://A001/X128a/Xb9 --env=main
```

### 2. Batch Execution on Slurm
```bash
# Using unified CLI
calibpipe batch quick.run --env=main -c 8 -m 248 -p plwg

# Legacy interface (backward-compatible)
./runbatch.py quick.run --env=main -c 8 -m 248 -p plwg
```

`quick.run` format:
```text
# <mous_uid> [recipe]
uid://A001/X128a/Xb9  calimage
uid://A002/Xcff05c/Xd calimage
```

### 3. Interactive Shell Setup
Set the CASA and pipeline environment variables directly in your current shell session (`bash` or `zsh`):

```bash
# Modern direct eval:
eval "$(calibpipe env --env=main)"

# Or source the helper script:
source calibpipe_env.sh --env=main

# Inspect resolved variables:
calibpipe env --env=main --print-env
```

---

## Testing

Run the full test suite verifying against golden reference specifications:
```bash
python3 -m unittest discover -s tests -v
# or
pytest tests/
```
