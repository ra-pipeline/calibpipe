# Migrating to calibpipe 0.2

The native `calibpipe run` and `calibpipe submit` interfaces select targets and
profiles. Persistent execution and scheduler settings belong in TOML. This is a
breaking interface release; `scripts/calibPipeIF.py`, `scripts/runbatch.py`, and
`scripts/calibpipe_env.sh` forward directly to `calibpipe run`, `calibpipe submit`,
and `calibpipe env`.

## Native commands

```bash
calibpipe run --config config.toml -p science
calibpipe run --config config.toml -p science --vis /data/new.ms --dry-run
calibpipe submit quick.run --config config.toml -p cluster
calibpipe config show --config config.toml -p science
```

`run` keeps `--mous`, `--vis`, `--script`, `--cmd`, `--ppr`, `--recipe`, and `-i/--interactive`.
Both execution commands keep `--config`, `--no-site-config`, `-p/--profile`, `--env`,
`--workdir`, `--recipe`, `--dry-run/--no-dry-run`, and `-v/--verbose/--no-verbose`.
A PPR can still override the request used with `--mous`.
`submit` accepts a runfile or a profile containing a target or `collect = true`.

| Previous native option/key | Canonical replacement |
| --- | --- |
| Bare `calibpipe --mous=…` | `calibpipe run --mous=…` |
| `calibpipe batch`, `calibpipe updateref` | `calibpipe submit`, `calibpipe update-ref` |
| `--PPR` | `--ppr` |
| `--batch-profile` | `--profile` |
| `--nprocs` / `--ncores` | `[run].nprocs` or profile `nprocs` |
| CASA / Pixi installation and branch flags | `[envs.<name>]` or profile environment keys |
| `casa_dir`, `casa_ver` | `casa_root` |
| `pipe_dir`, `pipe_ver`, `pl_ver`, environment `heuristics_root` | `heuristics_dir` |
| `casadata`, `data` | `casarundata` |
| `--casarundata`, `--obscaldir`, `--aUdir` | `[paths]` keys (or profile `casarundata`, `obscaldir`) |
| `--flag`, `--subdir`, `--onlysemipass` | `[run].flag_dir`, `.subdir`, `.onlysemipass` or profile keys |
| `--procedure` / `[run].procedure` / profile `procedure` | `recipe` (accepts shorthand or `.xml` filename/path) |
| Logging, backup, ancillary staging, MPI placement, monitoring flags | Corresponding `[run]` or profile keys; use underscores in TOML |
| `--custom-rcdir` | `use_custom_rcdir` under `[run]` or profile |
| `--cores` / `--ntasks`, `--cpus-per-task`, `--mem`, `--partition` / `--queue` | `[batch]` or profile `cores`, `cpus_per_task`, `mem`, `partition` |
| `ntasks`, `threads`, `queue`, `gpus` | `cores`, `cpus_per_task`, `partition`, `request_gpus` |
| Other scheduler/resource/routing flags | Corresponding `[batch]` or profile keys |
| `central_manager` / `central_manager_host`, `schedd_name` / `remote` | `pool`, `schedd` |
| `transfer_files`, `throttle`, `noglogger`, `work_path` | `should_transfer_files`, `max_concurrent`, `nologger`, `workdir` |
| `--collect`, collection filters, `--with-report` | Profile `collect`, `test_target`, `test_marker`, `test_filter`, `test_opts`, `with_report` |
| `--save-runfile` | `[batch]` or profile `save_runfile` |
| `--outfile`, `--errfile` | `[batch]` or profile `outfile`, `errfile` |
| `--extra-arg` | Configure the runner setting explicitly; arbitrary forwarding is removed |
| `--print-env` on `run` | `calibpipe env --print-env` |
| `[batches.<name>]`, nested profile subtables | Flat `[profiles.<name>]` |
| `pmr_home`, `datapacker_home`, `acsdata`, `java_home` | `[site]` |
| `pixi_bin` | `[paths].pixi_bin` |
| `use_custom_rcdir`, `casa_enable_telemetry`, `log2term` under `[site]` | `[run]` |
| Implicit extra keys under `[envs.<name>]` | `[envs.<name>.extra_vars]` |

`[paths].heuristics_root` remains a parent directory for branch discovery. It differs
from an individual environment's `heuristics_dir`. Likewise, `rundata` is an explicit
list of CASA runtime-data search directories; keep it distinct from `casarundata`.

MPI process count (`nprocs`), scheduler task/CPU allocation (`cores`), and threads per
task (`cpus_per_task`) are independent. Renaming `ntasks` to `cores` must not change
`nprocs`. Omitted values inherit; `false` and empty lists are explicit overrides.

Unknown native keys now fail with the file and table location. `config show -p NAME`
shows effective settings and source files before execution.

## Batch behavior

Native jobs save one frozen `batch.<timestamp>.toml` snapshot per `submit` invocation and
call `calibpipe run --config=<snapshot> --config-only ...` inside the generated scheduler
script. Workers use those submitted settings even if the original TOML files change while
a job waits in the queue. The worker inherits the scheduler environment and checks paths
on the execution host. It does not store a copy of the submitting shell's environment.

## Existing scripts

`scripts/calibPipeIF.py`, `scripts/runbatch.py`, and `scripts/calibpipe_env.sh`
forward directly to `calibpipe run`, `calibpipe submit`, and `calibpipe env`. They
use the canonical 0.2 CLI flags and TOML schema.
