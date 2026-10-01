# calibpipe

`calibpipe` is a lightweight driver and Slurm batch wrapper for ALMA Science Pipeline runs. It is designed for operators and developers who need a repeatable command-line workflow for selecting a configured CASA environment, launching a single MOUS reduction, or submitting a batch of jobs.

The package keeps site-specific execution details in TOML configuration rather than hard-coded shell fragments, while preserving compatibility with older operational entry points (`calibPipeIF.py`, `runbatch.py`, `calibpipe_env.sh`).

---

## What You Can Do with calibpipe

- **Run single pipeline executions:** `calibpipe run --mous=... --env=...`
- **Submit batches to Slurm:** `calibpipe batch pipefile.txt --env=...`
- **Resolve interactive shell environments:** `source calibpipe_env.sh --env=...` or `eval "$(calibpipe env --env=...)"`
- **Preserve existing workflows:** seamlessly drop in for legacy scripts without breaking cron jobs or cluster submission scripts.

---

## Quick Start

### 1. Installation

Install in editable mode for local development:
```bash
pip install -e .
```
Or install in an isolated environment via `pipx` (recommended on multi-user clusters):
```bash
pipx install .
```

### 2. Configuration

Create your personal `config.toml` from the template (this file is gitignored and will never be committed):
```bash
cp config.example.toml config.toml
```

Edit `config.toml` to specify your CASA builds and pipeline checkouts:
```toml
default_env = "main"

[envs.main]
casa_root = "/opt/casa/casa-6.7.4-8-pipeline-2026.2.0.23-py3.12"
```

### 3. Run a Reduction

Execute a single MOUS reduction with the configured environment:
```bash
calibpipe run --mous=uid://A001/X128a/Xb9 --env=main --recipe=calimage
```

### 4. Submit a Batch to Slurm

Submit multiple MOUS reductions from a batch file (`quick.run`) to the cluster:
```bash
calibpipe batch quick.run --env=main -c 8 -m 248 -p plwg
```

### 5. Interactive Shell Setup

Export the resolved CASA and pipeline environment directly into your current shell session (`bash` or `zsh`):
```bash
source calibpipe_env.sh --env=main
```

---

## Documentation Map

- **[User Guide](guide.md):** Detailed guide on installation, TOML configuration schema, command-line workflows, and testing.
- **[API Reference](api.md):** Auto-generated module reference and docstrings for `cli`, `config`, `driver`, `batch`, and execution steps.

---

## Building the Documentation

To view or build this documentation site locally:

```bash
# Start local live-reloading preview server
uv run --extra docs zensical serve

# Or build static HTML to site/
uv run --extra docs zensical build
```
