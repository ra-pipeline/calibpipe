# calibpipe

`calibpipe` is a lightweight driver and Slurm batch wrapper for ALMA Science Pipeline runs. It is designed for operators and developers who need a repeatable command-line workflow for selecting a configured CASA environment, launching a single MOUS reduction, or submitting a batch of jobs.

The package keeps site-specific execution details in TOML configuration rather than hard-coded shell fragments, while preserving compatibility with older operational entry points (`calibPipeIF.py`, `runbatch.py`, `calibpipe_env.sh`).

> [!WARNING]
> **Observatory Infrastructure & Cluster Dependency**
>
> `calibpipe` is an operational driver for the ALMA Science Pipeline designed to run on observatory HPC clusters (e.g. NAASC) or specialized pipeline workstations. Running `calibpipe` on a standard personal workstation without valid ALMA pipeline builds, Slurm, ALMA datapacker, and `pipelineMakeRequest` (PMR) will **not work out-of-the-box**.
>
> Operators on observatory clusters should configure `config.toml` using site-specific cluster paths (see `notes/config.internal.example.toml`).

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

For the complete 3-tier schema (`[paths]`, `[envs.<name>]`, `[site]`), variable interpolation, and environment precedence diagrams, see the [Configuration Guide](guide.md#configuration).

### 3. Run a Reduction

Execute a single MOUS reduction with the configured environment:
```bash
calibpipe run --mous=uid://A001/X128a/Xb9 --env=main --recipe=calimage
```

For Slurm batch execution, interactive shell sourcing, and legacy wrapper mappings, see [Command-Line Workflows](guide.md#command-line-workflows).

---

## Documentation Map

- **[User Guide](guide.md):** Complete manual covering architecture, the 3-tier configuration schema, Slurm batching, shell environment setup, testing, and local doc previewing.
- **[API Reference](api/index.md):** Auto-generated module reference and docstrings for `cli`, `config`, `driver`, `batch`, and execution steps.
- **[Configuration Template](https://github.com/nrao/calibpipe/blob/main/config.example.toml):** Fully annotated generic configuration template.
