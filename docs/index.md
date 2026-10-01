# calibpipe

`calibpipe` is a lightweight driver and Slurm batch wrapper for ALMA Science Pipeline runs. It is designed for operators and developers who need a repeatable command-line workflow for selecting a configured CASA environment, launching a single MOUS reduction, or submitting a batch of jobs.

The package keeps site-specific execution details in TOML configuration rather than hard-coded shell fragments, while preserving compatibility with older operational script entry points in `scripts/` (`scripts/calibPipeIF.py`, `scripts/runbatch.py`, `scripts/calibpipe_env.sh`).

> [!WARNING] Observatory Infrastructure & Cluster Dependency
>
> `calibpipe` is an operational driver for the ALMA Science Pipeline designed to run on observatory HPC clusters (e.g. NAASC) or specialized pipeline workstations. Running `calibpipe` on a standard personal workstation without valid ALMA pipeline builds, Slurm, ALMA datapacker, and `pipelineMakeRequest` (PMR) will **not work out-of-the-box**.
>
> On shared observatory clusters, site administrators can deploy a central site configuration (`/etc/calibpipe/config.toml`, `$CALIBPIPE_SITE_CONFIG`, or `config.site.toml`), allowing users to run immediately or overlay personal settings.

---

## What You Can Do with calibpipe

- **Run single pipeline executions:** `calibpipe run --mous=... --env=...`
- **Submit batches to Slurm:** `calibpipe batch pipefile.txt --env=...`
- **Resolve interactive shell environments:** `source scripts/calibpipe_env.sh --env=...` or `eval "$(calibpipe env --env=...)"`
- **Preserve existing workflows:** seamlessly drop in for legacy scripts without breaking cron jobs or cluster submission scripts.

---

## Quick Start

### 1. Installation & Environment Setup

Using `uv` (recommended):

```bash
# Sync local virtual environment with dev and docs tools:
uv sync --extra dev --extra docs

# Or install globally as an isolated CLI tool on your cluster account:
uv tool install --editable .
```

Alternative pip / pipx methods:

```bash
pip install -e .     # or: pipx install .
```

### 2. Configuration

On shared clusters, site defaults (`[site]`, cluster `[batch]`, and canonical `[envs]`) are automatically discovered and inherited.

To configure personal overrides, copy `config.example.toml` to `~/.config/calibpipe/config.toml` (or `./config.toml`):

```bash
cp config.example.toml ~/.config/calibpipe/config.toml
```

Edit your configuration to specify custom scratch paths or custom pipeline checkouts:

```toml
default_env = "main"

[envs.main]
casa_root = "/opt/casa/casa-6.7.4-8-pipeline-2026.2.0.23-py3.12"
```

For the complete multi-layer cascading architecture (`[paths]`, `[envs.<name>]`, `[site]`, `[batch]`, `[run]`), variable interpolation, and precedence rules, see the [Configuration Guide](guide.md#configuration).

### 3. Run a Reduction

Execute a single MOUS reduction with the configured environment:

```bash
calibpipe run --mous=uid://A001/X128a/Xb9 --env=main --recipe=calimage
```

For Slurm batch execution, interactive shell sourcing, and legacy wrapper mappings, see [Command-Line Workflows](guide.md#command-line-workflows).

---

## Documentation Map

- **[User Guide](guide.md):** Complete manual covering architecture, the 5-tier configuration schema, Slurm batching, shell environment setup, testing, and local doc previewing.
- **[Execution Internals](internals.md):** In-depth technical details on project ID resolution, `pipelineMakeRequest` (PMR) staging, OUS directory layout, and FAQ.
- **[API Reference](api/index.md):** Auto-generated module reference and docstrings for `cli`, `config`, `driver`, `batch`, and execution steps.
- **[Configuration Template](https://github.com/nrao/calibpipe/blob/main/config.example.toml):** Fully annotated generic configuration template.
