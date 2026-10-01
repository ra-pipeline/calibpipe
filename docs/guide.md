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
├── docs/
│   ├── index.md
│   ├── guide.md
│   └── api.md
├── src/
│   └── calibpipe/
│       ├── cli.py
│       ├── config.py
│       ├── driver.py
│       ├── batch.py
│       ├── steps/
│       └── legacy/
└── tests/
```

Key implementation modules:

- `src/calibpipe/cli.py` provides the unified command-line interface.
- `src/calibpipe/config.py` loads TOML configuration and builds the runtime environment.
- `src/calibpipe/driver.py` orchestrates a single pipeline run.
- `src/calibpipe/batch.py` generates and submits Slurm jobs.
- `src/calibpipe/steps/` houses modular execution phases (staging, PMR, PPR, CASA runner).
- `src/calibpipe/legacy/` contains compatibility wrappers.

---

## Installation

Install from the canonical top-level `calibpipe/` checkout:

```bash
cd /path/to/calibpipe
pip install -e .
```

For an isolated user install (recommended on shared clusters):

```bash
pipx install /path/to/calibpipe
```

---

## Configuration

Start from `config.example.toml` in the repository root and create a `config.toml` for your local or cluster environment.

Configuration lookup order:

1. `--config=<path>`
2. `$CALIBPIPE_CONFIG`
3. `./config.toml` in the current working directory
4. `~/.config/calibpipe/config.toml`

Typical configuration content:

```toml
default_env = "main"

[paths]
scipipe_rootdir = "/path/to/pipeline/root/{user}"
scipipe_logdir = "/path/to/pipeline/logs/{user}"
pickle_dir = "/path/to/pipeline/pickles/{user}"
obscaldir = "/path/to/pipeline/obscal2021"

[envs.main]
casa_root = "/path/to/casa-with-pipeline"

[envs.pl2025]
casa_root = "/path/to/alternate/casa-with-pipeline"
heuristics_dir = "{casa_root}/pipeline"
```

Each `[envs.<name>]` entry identifies a CASA + pipeline installation that `calibpipe` can launch.

---

## Command-Line Workflows

`calibpipe` provides one CLI with three primary workflows.

### 1. Run a Single MOUS

```bash
calibpipe run --mous=uid://A001/X128a/Xb9 --env=main --recipe=calimage
```

This resolves the selected environment, stages the run inputs, and launches the underlying pipeline execution.

The legacy wrapper still works:

```bash
./calibPipeIF.py --mous=uid://A001/X128a/Xb9 --env=main
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

The legacy wrapper also remains available:

```bash
./runbatch.py quick.run --env=main -c 8 -m 248 -p plwg
```

### 3. Resolve a Shell Environment

```bash
eval "$(calibpipe env --env=main)"
```

This prints shell exports that can be evaluated directly in `bash` or `zsh`.

The compatibility wrapper is:

```bash
source calibpipe_env.sh --env=main
```

Use `calibpipe_env.sh` when you want the resolved CASA and pipeline variables loaded into your current shell session and prefer the older `source ...` workflow. It must be sourced rather than executed, and it exists mainly as a compatibility wrapper around `calibpipe env`.

---

## Backward Compatibility

`calibpipe` preserves legacy entry points so existing user scripts do not need to change immediately.

- `calibPipeIF.py` forwards to `calibpipe run`
- `runbatch.py` forwards to `calibpipe batch`
- `calibpipe_env.sh` forwards to `calibpipe env`

This allows gradual migration to the unified CLI without breaking older job wrappers.

---

## Testing

Run the test suite from the project root:

```bash
pytest tests/
```

The suite covers configuration loading, driver behavior, and batch submission logic. Golden reference files under `tests/reference/` verify generated call sequences and `sbatch` command content.

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
- [API Reference](api.md): generated API reference
