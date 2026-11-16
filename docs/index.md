# calibpipe

`calibpipe` is a lightweight driver and Slurm batch wrapper for ALMA Science Pipeline development. It is designed for validation, testing, and development workflows needing a repeatable command-line interface for selecting a configured CASA environment, launching a single MOUS reduction, or submitting a batch of jobs.

The package keeps site-specific execution details in TOML configuration rather than hard-coded shell fragments, while preserving compatibility with older script entry points in `scripts/` (`scripts/calibPipeIF.py`, `scripts/runbatch.py`, `scripts/calibpipe_env.sh`).

> [!WARNING] Cluster & Pipeline Tooling Dependencies
>
> `calibpipe` is a validation and testing tool; running it requires access to CASA + ALMA pipeline builds, Slurm (for batch execution), and ALMA datapacker / `pipelineMakeRequest` (PMR) only when running in MOUS mode.
>
> On shared observatory clusters, site administrators can deploy a central site configuration (`/etc/calibpipe/config.toml`, `$CALIBPIPE_SITE_CONFIG`, or `config.site.toml`), allowing users to run immediately or overlay personal settings.

---

## What You Can Do with calibpipe

<div class="grid cards" markdown>

-   :lucide-play:{ .lg .middle } **Single Pipeline Runs**

    ---

    Launch standard ALMA MOUS reductions (`--mous`), direct recipe runs on local datasets (`--vis` with configured `recipe`), standalone PPRs (`--ppr`), custom scripts (`--script`), inline commands (`--cmd`), or interactive CASA sessions (`-i`).

-   :lucide-layers:{ .lg .middle } **Slurm & HTCondor Batch Queues**

    ---

    Submit pipefiles (`calibpipe submit pipefile.txt`), regression test arrays (`calibpipe submit quick.run -p <profile>` or a profile with `collect = true`), throttled arrays (profile `max_concurrent`), or single jobs from named profiles (`calibpipe submit -p <profile>`).

-   :lucide-search:{ .lg .middle } **Regression Test Discovery**

    ---

    Collect pytest test nodes inside CASA Python with marker and keyword filtering via `calibpipe collect [target] -p <profile> -o runfile.txt`.

-   :lucide-bar-chart-2:{ .lg .middle } **Result & Coverage Synthesis**

    ---

    Consolidate distributed JUnit XML test summaries, merge `.coverage.*` files with source path remapping, and build HTML reports (`calibpipe report`, `calibpipe coverage`, or profile `with_report = true`).

-   :lucide-file-check:{ .lg .middle } **Reference File Updates**

    ---

    Merge `*.NEW.results.txt` outputs into `pipeline-testdata` references while preserving custom tolerances and staging `.candidate` files for review (`calibpipe update-ref`).

-   :lucide-sliders:{ .lg .middle } **Profiles, Shells & Legacy Shims**

    ---

    Target reusable `[profiles.<name>]` definitions, resolve shell environments (`source scripts/calibpipe_env.sh --env=...` or `eval "$(calibpipe env --env=...)"`), and keep drop-in compatibility for legacy scripts.

</div>

---

## Quick Start

### 1. Installation & Environment Setup

=== "uv (recommended)"

    ```bash
    # Sync local virtual environment with dev and docs tools:
    uv sync --extra dev --extra docs

    # Or install globally as an isolated CLI tool on your cluster account:
    uv tool install --editable .
    ```

=== "pip / pipx"

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

For the complete multi-layer cascading architecture (`[paths]`, `[envs.<name>]`, `[site]`, `[batch]`, `[run]`, `[profiles.<name>]`), variable interpolation, and precedence rules, see the [Configuration Guide](guide.md#configuration).

### 3. Run a Reduction

Execute a single MOUS reduction with the configured environment:

```bash
calibpipe run --mous=uid://A001/X128a/Xb9 --env=main
```

For Slurm batch execution, interactive shell sourcing, and legacy wrapper mappings, see [Command-Line Workflows](guide.md#command-line-workflows).

---

## Documentation Map

<div class="grid cards" markdown>

-   :lucide-book-open:{ .lg .middle } **[User Guide](guide.md)**

    ---

    Complete manual covering architecture, 5-tier configuration, batch queues, profiles, shell setup, testing, and reference updates.

-   :lucide-cpu:{ .lg .middle } **[Execution Internals](internals.md)**

    ---

    Technical details on project ID resolution, `pipelineMakeRequest` (PMR) staging, OUS directory layout, and FAQ.

-   :lucide-history:{ .lg .middle } **[Changelog & History](changelog.md)**

    ---

    Evolution from prototype scripts to `calibpipe`, version-by-version release notes, and unreleased changes.

-   :lucide-terminal:{ .lg .middle } **[API Reference](api/index.md)**

    ---

    Auto-generated module reference and docstrings for `cli`, `config`, `runner`, `batch`, `collect`, `coverage`, `report`, `update_ref`, and `pmr`.

</div>

- **[Configuration Template](https://github.com/ra-pipeline/calibpipe/blob/main/config.example.toml):** Annotated generic `config.toml` template.
