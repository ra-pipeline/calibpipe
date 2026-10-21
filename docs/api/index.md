# API Reference

`calibpipe` provides modular Python interfaces for command-line parsing, TOML configuration loading, single-run execution, and Slurm batch management.

## Module Summary

| Module | Description |
| :--- | :--- |
| [`calibpipe.cli`](cli.md) | Unified command-line entry point and subcommand routing for `run`, `batch`, `pmr`, `env`, and `config`. |
| [`calibpipe.config`](config.md) | TOML configuration loader, environment resolution, and path reachability validation. |
| [`calibpipe.runner`](runner.md) | Single-run pipeline orchestrator, log management, and CASA subprocess execution. |
| [`calibpipe.batch`](batch.md) | Slurm batch submission generator, pipefile parser, and submit-host guards. |
| [`calibpipe.pmr`](pmr.md) | Pipeline Make Request wrapper and ALMA archive data fetcher. |

---

## Architecture Overview

```mermaid
flowchart LR
    CLI["calibpipe.cli"] --> Config["calibpipe.config"]
    CLI --> Runner["calibpipe.runner"]
    CLI --> Batch["calibpipe.batch"]
    CLI --> PMR["calibpipe.pmr"]
    Batch --> Runner
    Runner --> PMR
    Runner --> CASA["CASA Runtime (xvfb-run)"]
```
