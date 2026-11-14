"""Configuration loading and CASA/pipeline environment construction for calibpipe.

Replaces the legacy env_core2.sh and personal pipeline_env_<name>.sh files with
structured, strongly-typed TOML configuration loading.
"""

from __future__ import annotations

import functools
import json
import math
import os
import re
import shlex
import shutil
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

def _list_or_empty(value: Any) -> list[str]:
    """Normalize a string, list, or None into a list of non-empty strings.

    Recognizes 'none', 'null', and False as empty lists (disabled/cleared).
    """
    if value is None or value is False:
        return []
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("none", "null", "false"):
            return []
        return [s]
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for v in value:
            if v is not None and v is not False:
                s = str(v).strip()
                if s and s.lower() not in ("none", "null", "false"):
                    result.append(s)
        return result
    return []


def _ancillary_list(value: Any) -> list[Any]:
    """Normalize ancillary configuration into path strings or (source, dest) tuples.

    Supports:
        - 'path/to/file' (staged with original filename)
        - ['source/path', 'dest_name'] (staged with custom target name)
        - ('source/path', 'dest_name') (tuple variant)
        - {'src': '...', 'dest': '...'} (dictionary variant)
        - 'source/path:dest_name' (CLI colon variant)
    """
    if value is None or value is False:
        return []
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("none", "null", "false"):
            return []
        if ":" in s and not Path(s).exists():
            src_part, dst_part = s.split(":", 1)
            return [(src_part.strip(), dst_part.strip())]
        return [s]
    if isinstance(value, (list, tuple)):
        result: list[Any] = []
        for v in value:
            if isinstance(v, (list, tuple)) and len(v) == 2:
                result.append((str(v[0]), str(v[1])))
            elif isinstance(v, dict) and "src" in v and "dest" in v:
                result.append((str(v["src"]), str(v["dest"])))
            elif isinstance(v, str):
                s = v.strip()
                if s and s.lower() not in ("none", "null", "false"):
                    if ":" in s and not Path(s).exists():
                        src_part, dst_part = s.split(":", 1)
                        result.append((src_part.strip(), dst_part.strip()))
                    else:
                        result.append(s)
            elif v is not None and v is not False:
                result.append(str(v))
        return result
    return []


def _safe_exists(path: Path) -> bool:
    """Check if path exists without raising OSError on permission errors or broken mounts."""
    try:
        return path.exists()
    except OSError:
        return False


def _safe_is_dir(path: Path) -> bool:
    """Check if path is a directory without raising OSError on permission errors or broken mounts."""
    try:
        return path.is_dir()
    except OSError:
        return False


@functools.lru_cache(maxsize=256)
def _cached_is_dir(path_str: str) -> bool:
    """Cached directory check to minimize repeated stat calls on network filesystems."""
    return _safe_is_dir(Path(path_str))


def resolve_workspace_path(
    path_str: str | Path,
    workspaces: Sequence[str | Path] = (),
) -> Path:
    """Resolve a file or directory path against workspace search roots.

    If path_str exists as given (or is an absolute path), it is returned.
    Otherwise, searches workspaces in order. If the path exists relative
    to any workspace root, that resolved path is returned.
    If not found in any workspace, returns Path(path_str).

    Args:
        path_str: Relative or absolute path string or Path object.
        workspaces: Sequence of workspace search directories.

    Returns:
        Resolved Path object.
    """
    if not path_str:
        return Path("")
    p = Path(path_str).expanduser()
    if p.is_absolute() or _safe_exists(p):
        return p
    for ws in workspaces:
        if not ws:
            continue
        ws_path = Path(ws).expanduser()
        if not _cached_is_dir(str(ws_path)):
            continue
        candidate = ws_path / p
        if _safe_exists(candidate):
            try:
                return candidate.resolve()
            except OSError:
                return candidate
    return p


def resolve_recipe_procedure(recipe: str) -> str:
    """Resolve a recipe shorthand or procedure XML path into a procedure XML name or path."""
    if not recipe:
        return ""
    if recipe.endswith(".xml") or "/" in recipe:
        return recipe
    if recipe == "cal":
        procedure_short = "hifa_cal"
    elif recipe in ("calimage", "calimage_selfcal", "calimage_selfcal_nocube"):
        procedure_short = "hifa_calimage"
    elif recipe == "image":
        procedure_short = "hifa_image"
    elif recipe == "calsurvey":
        procedure_short = "hifa_calsurvey"
    else:
        procedure_short = f"hifa_{recipe}"
    return f"procedure_{procedure_short}.xml"


def resolve_first_existing_path(
    value: Any,
    workspaces: Sequence[str | Path] = (),
    user: str | None = None,
) -> str:
    """Normalize a path or list of candidate paths, picking the first one that exists.

    If value is a single string or Path, it is returned as a stripped string.
    If value is a sequence of candidate paths, each is tested in order (with ~ and
    {user} expanded). The first candidate that exists on the filesystem is returned.
    If none of the candidates exist directly, the first candidate whose parent directory
    exists is returned. If neither exists, the first candidate is returned as fallback.

    Args:
        value: A path string, Path object, sequence of candidate paths, or None.
        workspaces: Optional workspace roots to search for relative candidate paths.
        user: Optional username for {user} interpolation (defaults to $USER).

    Returns:
        The selected path string, or "" if value is empty/None.
    """
    if value is None or value is False:
        return ""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("none", "null", "false"):
            return ""
        return s

    if isinstance(value, (list, tuple)):
        candidates: list[str] = []
        for v in value:
            if v is not None and v is not False:
                s = str(v).strip()
                if s and s.lower() not in ("none", "null", "false"):
                    candidates.append(s)
        if not candidates:
            return ""

        actual_user = user or os.environ.get("USER", "")

        def _expand(cand: str) -> Path:
            interp = cand.replace("{user}", actual_user)
            return Path(interp).expanduser()

        # Pass 1: First candidate that exists
        for cand in candidates:
            exp = _expand(cand)
            if _safe_exists(exp):
                return cand
            if workspaces and not exp.is_absolute():
                ws_res = resolve_workspace_path(exp, workspaces)
                if _safe_exists(ws_res):
                    return str(ws_res)

        # Pass 2: First candidate whose parent directory exists
        for cand in candidates:
            exp = _expand(cand)
            if _safe_exists(exp.parent):
                return cand
            if workspaces and not exp.is_absolute():
                ws_res = resolve_workspace_path(exp.parent, workspaces)
                if _safe_exists(ws_res):
                    return str(ws_res / exp.name)

        # Pass 3: Fallback to the first candidate
        return candidates[0]

    return str(value).strip()


def _int_or_none(value: Any) -> int | None:
    """Return int(value) or None if value is None or empty string."""
    return None if value is None or value == "" else int(value)


def _float_or_none(value: Any) -> float | None:
    """Return float(value) or None if value is None or empty string."""
    return None if value is None or value == "" else float(value)


def _str_or_none(value: Any) -> str | None:
    """Return str(value) or None if value is None or empty string."""
    return None if value is None or value == "" else str(value)


class ConfigError(Exception):
    """Raised when configuration file or required keys are invalid or missing."""

class Model:
    @classmethod
    def from_dict(cls, data):
        return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})


@dataclass
class PathsConfig(Model):
    """Working and output directory paths for pipeline processing."""

    scipipe_rootdir: str = ""
    scipipe_logdir: str = ""
    pickle_dir: str | None = None
    obscaldir: str | None = None
    aUdir: str | None = None
    validation_dir: str | None = None
    heuristics_root: str | None = None
    workspaces: list[str] = field(default_factory=list)
    casarundata: str | None = None
    datapath: list[str] = field(default_factory=list)
    rundata: list[str] | None = None
    pixi_bin: str | None = None

    def __post_init__(self):
        for key in ("scipipe_rootdir", "scipipe_logdir", "casarundata"):
            value = getattr(self, key)
            if isinstance(value, (list, tuple)):
                setattr(
                    self,
                    key,
                    resolve_first_existing_path(value, workspaces=self.workspaces),
                )
        if self.rundata is not None:
            self.rundata = _list_or_empty(self.rundata)
        self.datapath = _list_or_empty(self.datapath)


@dataclass
class SiteConfig(Model):
    """Site, cluster, and external PMR/archive tooling configuration."""

    submit_host: str | None = None
    pmr_home: str | None = "/opt/pipetools/latest"
    datapacker_home: str | None = "/opt/datapacker/current"
    acsdata: str | None = "/opt/acsdata"
    java_home: str | None = field(
        default_factory=lambda: os.environ.get("JAVA_HOME", "/usr/lib/jvm/default-java")
    )
    flux_service_url: str = "https://almascience.org/sc/flux"
    flux_service_url_backup: str = "https://asa.alma.cl/sc/flux"
    strict_paths: bool = False


@dataclass
class BatchConfig(Model):
    """Batch cluster resource submission defaults (Slurm and HTCondor).

    Scheduler Compatibility:
      - Both Slurm & HTCondor:
          - scheduler: "slurm" or "htcondor".
          - partition: Slurm partition (--partition) or HTCondor (+partition).
          - cores: Slurm tasks (--ntasks) or HTCondor CPUs (request_cpus).
          - mem: Total memory in GB (Slurm: --mem; HTCondor: request_memory).
          - mem_per_cpu: Memory per CPU (Slurm: --mem-per-cpu; HTCondor: request_memory = cores * mem_per_cpu).
          - mail_type: Notification setting (Slurm: --mail-type; HTCondor: notification).
          - chdir: Working directory (Slurm: --chdir; HTCondor: initialdir).
          - nodelist: Pinned host (Slurm: --nodelist; HTCondor: TARGET.Machine == "<nodelist>").
          - logdir: Output directory for script records and scheduler logs.
          - dry_run: Preview submit scripts without submitting.
      - Slurm only (ignored by HTCondor):
          - node: Number of nodes (--nodes).
          - walltime: Job runtime limit (--time).
          - cpus_per_task: Logical CPUs/threads per task (--cpus-per-task; total allocated = cores * cpus_per_task).
          - ntasks_per_core: Tasks per physical silicon core (--ntasks-per-core; 1 to avoid HT sharing).
          - hint: Placement hint (--hint, e.g. "nomultithread").
          - distribution: Task distribution across nodes/sockets (--distribution).
          - no_requeue: Prevent silent job requeuing on failure (--no-requeue).
      - HTCondor only (ignored by Slurm):
          - requirements: Custom ClassAd requirements expression override.
    """

    partition: str = "plwg"  # Slurm: --partition; HTCondor: +partition (alias: queue)
    cores: int = 8  # Slurm: --ntasks; HTCondor: request_cpus (alias: ntasks)
    mem: int = 248  # Total RAM in GB (Slurm: --mem; HTCondor: request_memory)
    mem_per_cpu: str | None = (
        None  # RAM per CPU (Slurm: --mem-per-cpu; HTCondor: scaled into request_memory)
    )
    mail_type: str = "ALL"  # Slurm: --mail-type; HTCondor: notification
    chdir: str | None = None  # Slurm: --chdir; HTCondor: initialdir
    nodelist: str | None = None  # Slurm: --nodelist; HTCondor: TARGET.Machine
    scheduler: str = "slurm"  # Batch scheduler backend: "slurm" or "htcondor"
    dry_run: bool = False  # Simulate batch submission without submitting jobs
    logdir: str | None = None  # Directory for scheduler log and script output
    node: str = "1"  # Slurm: --nodes
    walltime: str | None = None  # Slurm: --time (e.g. "24:00:00")
    cpus_per_task: int | None = (
        None  # Slurm: --cpus-per-task (logical CPUs/threads per task; total = ntasks × cpus_per_task)
    )
    ntasks_per_core: int | None = (
        None  # Slurm: --ntasks-per-core (tasks per physical core; 1 prevents HT sharing)
    )
    hint: str | None = None  # Slurm: --hint (e.g. "nomultithread")
    distribution: str | None = None  # Slurm: --distribution (e.g. "cyclic:cyclic")
    no_requeue: bool = True  # Slurm: --no-requeue (prevent silent resubmission)
    requirements: str | None = None  # HTCondor: requirements expression override
    request_gpus: int = 0  # HTCondor: request_gpus
    pool: str | None = None  # HTCondor: central manager host (-pool)
    schedd: str | None = (
        None  # HTCondor: target schedd daemon name (-name <schedd>; avoids implicit -spool from -remote)
    )
    spool: bool = False  # HTCondor: spool job files over network (-spool)
    should_transfer_files: str | None = (
        None  # HTCondor: should_transfer_files ("YES", "IF_NEEDED", "NO")
    )
    when_to_transfer_output: str | None = (
        None  # HTCondor: when_to_transfer_output ("ON_EXIT", "ON_EXIT_OR_EVICT")
    )
    omp_num_threads: int | None = None
    openblas_num_threads: int | None = None
    max_concurrent: int | None = (
        None  # Slurm dependency slot throttle (alias: throttle)
    )
    outfile: str | None = None
    errfile: str | None = None
    save_runfile: str | None = None


@dataclass
class Request:
    """Typed execution request shared at the CLI parser boundary."""

    config: str | None = None
    profile: str | None = None
    env: str | None = None
    no_site_config: bool = False
    workdir: str | None = None
    recipe: str | None = None
    dry_run: bool | None = None
    verbose: bool | None = None
    overrides: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name):
        try:
            return self.overrides[name]
        except KeyError:
            raise AttributeError(name) from None


@dataclass
class RunRequest(Request):
    """Typed request for single-run execution."""

    mous: str | None = None
    vis: list[str] | None = None
    script: str | None = None
    cmd: str | None = None
    ppr: str | None = None
    interactive: bool | None = None
    config_only: bool = False
    isolate_root: bool = False


@dataclass
class SubmitRequest(Request):
    """Typed request for batch submission."""

    pipefile: str | None = None


@dataclass
class RunConfig(Model):
    """Driver defaults for single-run execution."""

    recipe: str = "calimage"
    nprocs: int = 8
    loglevel: str = "debug"
    useresume: bool = False
    symlink_shortcuts: bool = True
    log2term: bool = False
    verbose: bool = False
    omp_num_threads: int | None = None
    openblas_num_threads: int | None = None
    omp_max_threads: int | None = None
    mem_frac: float | None = None
    oversubscribe: bool = True
    bind_to: str | None = None
    map_by: str | None = None
    psrecord: bool = False
    memstats: bool = False
    pl_psrecord: bool = False
    backup: bool = False
    staticobscal: bool = False
    oldcont: bool = False
    cont_dat: str | None = None
    jyperk_csv: str | None = None
    parameter_list: str | None = None
    ancillary: list[Any] = field(default_factory=list)
    datapath: list[str] | None = None
    rundata: list[str] | None = None
    dry_run: bool = False
    autoreload: bool = True
    nologger: bool = False
    logfile: str | None = None
    use_custom_rcdir: bool = True
    rawdata: str | None = None
    ppr: str | None = None
    vla: bool = False
    workdir: str | None = None
    flag_dir: str | None = None
    subdir: str | None = None
    onlysemipass: str | None = None


@dataclass
class EnvSpec(Model):
    """Specification for a specific CASA + pipeline environment (monolithic or Pixi modular)."""

    name: str
    casa_root: str = ""
    branch: str = ""
    heuristics_dir: str | None = None
    pixi_dir: str | None = None
    pixi_env: str = "default"
    extra_vars: dict[str, str] = field(default_factory=dict)

    @property
    def is_pixi(self):
        return bool(self.pixi_dir)


@dataclass
class ProfileConfig(Model):
    """Named execution and batch resource profile."""

    desc: str | None = None
    env: str | None = None
    recipe: str | None = None
    nprocs: int | None = None
    loglevel: str | None = None
    useresume: bool | None = None
    symlink_shortcuts: bool | None = None
    log2term: bool | None = None
    verbose: bool | None = None
    omp_num_threads: int | None = None
    openblas_num_threads: int | None = None
    omp_max_threads: int | None = None
    mem_frac: float | None = None
    oversubscribe: bool | None = None
    bind_to: str | None = None
    map_by: str | None = None
    psrecord: bool | None = None
    memstats: bool | None = None
    pl_psrecord: bool | None = None
    backup: bool | None = None
    staticobscal: bool | None = None
    oldcont: bool | None = None
    cont_dat: str | None = None
    jyperk_csv: str | None = None
    parameter_list: str | None = None
    ancillary: list[Any] | None = None
    workdir: str | None = None
    partition: str | None = (
        None  # Slurm: --partition; HTCondor: +partition (alias: queue)
    )
    cores: int | None = None  # Slurm: --ntasks; HTCondor: request_cpus (alias: ntasks)
    mem: int | None = None  # Total RAM in GB (Slurm: --mem; HTCondor: request_memory)
    mem_per_cpu: str | None = (
        None  # RAM per CPU (Slurm: --mem-per-cpu; HTCondor: scaled into request_memory)
    )
    mail_type: str | None = None  # Slurm: --mail-type; HTCondor: notification
    chdir: str | None = None  # Slurm: --chdir; HTCondor: initialdir
    nodelist: str | None = None  # Slurm: --nodelist; HTCondor: TARGET.Machine
    logdir: str | None = None  # Output directory for script records and logs
    scheduler: str | None = None  # "slurm" or "htcondor"
    node: str | None = None  # Slurm: --nodes
    walltime: str | None = None  # Slurm: --time (e.g. "24:00:00")
    cpus_per_task: int | None = (
        None  # Slurm: --cpus-per-task (logical CPUs/threads per task; total = ntasks × cpus_per_task)
    )
    ntasks_per_core: int | None = (
        None  # Slurm: --ntasks-per-core (tasks per physical core; 1 prevents HT sharing)
    )
    hint: str | None = None  # Slurm: --hint (e.g. "nomultithread")
    distribution: str | None = None  # Slurm: --distribution (e.g. "cyclic:cyclic")
    no_requeue: bool | None = None  # Slurm: --no-requeue
    requirements: str | None = None  # HTCondor: requirements expression override
    request_gpus: int | None = None  # HTCondor: request_gpus
    pool: str | None = None  # HTCondor: central manager host (-pool)
    schedd: str | None = (
        None  # HTCondor: target schedd daemon name (-name <schedd>; avoids implicit -spool from -remote)
    )
    spool: bool | None = None  # HTCondor: spool job files over network (-spool)
    should_transfer_files: str | None = None  # HTCondor: should_transfer_files
    when_to_transfer_output: str | None = None  # HTCondor: when_to_transfer_output
    casa_root: str | None = None
    heuristics_dir: str | None = None
    pixi_dir: str | None = None
    pixi_env: str | None = None
    branch: str | None = None
    workspaces: list[str] | None = None
    scipipe_rootdir: str | None = None
    scipipe_logdir: str | None = None
    casarundata: str | None = None
    pickle_dir: str | None = None
    obscaldir: str | None = None
    aUdir: str | None = None
    validation_dir: str | None = None
    mous: str | None = None
    vis: list[str] | None = None
    script: str | None = None
    cmd: str | None = None
    ppr: str | None = None
    vla: bool | None = None
    rawdata: str | None = None
    interactive: bool | None = None
    datapath: list[str] | None = None
    rundata: list[str] | None = None
    dry_run: bool | None = None
    autoreload: bool | None = None
    nologger: bool | None = None
    logfile: str | None = None
    use_custom_rcdir: bool | None = None
    collect: bool | None = None
    test_target: str | None = None
    test_marker: str | None = None
    test_filter: str | None = None
    test_opts: str | None = None
    with_report: bool | None = None
    max_concurrent: int | None = None
    flag_dir: str | None = None
    subdir: str | None = None
    onlysemipass: str | None = None
    outfile: str | None = None
    errfile: str | None = None
    save_runfile: str | None = None


@dataclass
class ResolvedRunOptions(Model):
    """Fully resolved execution options for a single calibpipe run."""

    profile: str | None = None
    mous: str = ""
    env_name: str = ""
    recipe: str = ""
    nprocs: int = 8
    loglevel: str = "debug"
    useresume: bool = False
    use_custom_rcdir: bool = True
    symlink_shortcuts: bool = True
    log2term: bool = False
    flag_dir: str | None = None
    ppr: str | None = None
    rawdata: str | None = None
    subdir: str | None = None
    onlysemipass: str = ""
    verbose: bool = False
    vis: list[str] = field(default_factory=list)
    script: str = ""
    cmd: str = ""
    vla: bool = False
    workdir: str = ""
    interactive: bool = False
    omp_num_threads: int | None = None
    openblas_num_threads: int | None = None
    omp_max_threads: int | None = None
    mem_frac: float | None = None
    oversubscribe: bool = True
    bind_to: str | None = None
    map_by: str | None = None
    psrecord: bool = False
    memstats: bool = False
    pl_psrecord: bool = False
    backup: bool = False
    staticobscal: bool = False
    oldcont: bool = False
    cont_dat: str | None = None
    jyperk_csv: str | None = None
    parameter_list: str | None = None
    ancillary: list[Any] = field(default_factory=list)
    datapath: list[str] = field(default_factory=list)
    rundata: list[str] = field(default_factory=list)
    rundata_specified: bool = False
    dry_run: bool = False
    autoreload: bool = True
    nologger: bool = False
    logfile: str | None = None
    casa_root: str | None = None
    heuristics_dir: str | None = None
    pixi_dir: str | None = None
    pixi_env: str | None = None
    branch: str | None = None
    workspaces: list[str] = field(default_factory=list)
    scipipe_rootdir: str | None = None
    scipipe_logdir: str | None = None
    casarundata: str | None = None
    obscaldir: str | None = None


@dataclass
class ResolvedBatchOptions(Model):
    """Fully resolved batch submission options."""

    pipefile: Path | None
    env_name: str
    partition: str
    cores: int  # Tasks/CPUs per job (Slurm: --ntasks; HTCondor: request_cpus; alias: ntasks)
    mem: int  # Total RAM in GB (Slurm: --mem; HTCondor: request_memory)
    node: str  # [Slurm only] --nodes
    mail_type: str  # [Slurm & HTCondor] Notification policy
    outfile: str | None = None
    errfile: str | None = None
    walltime: str | None = None  # [Slurm only] --time
    nodelist: str | None = None  # [Slurm & HTCondor] Pinned host
    chdir: str | None = None  # [Slurm & HTCondor] Initial directory
    cpus_per_task: int | None = (
        None  # [Slurm only] Logical CPUs/threads per task (--cpus-per-task)
    )
    mem_per_cpu: str | None = None  # [Slurm & HTCondor] Memory per CPU
    hint: str | None = None  # [Slurm only] Placement hint (--hint)
    ntasks_per_core: int | None = (
        None  # [Slurm only] Tasks per physical core (--ntasks-per-core)
    )
    distribution: str | None = None  # [Slurm only] Task distribution (--distribution)
    no_requeue: bool = True  # [Slurm only] Prevent auto-requeue (--no-requeue)
    scheduler: str = "slurm"  # [Slurm & HTCondor] "slurm" or "htcondor"
    requirements: str | None = None  # [HTCondor only] ClassAd expression override
    request_gpus: int = 0  # [HTCondor: request_gpus; Slurm: --gpus]
    pool: str | None = None  # [HTCondor only] Central manager host (-pool)
    schedd: str | None = (
        None  # [HTCondor only] Target schedd daemon name (-name <schedd>; avoids implicit -spool from -remote)
    )
    spool: bool = False  # [HTCondor only] Spool job files over network (-spool)
    should_transfer_files: str | None = None  # [HTCondor only] should_transfer_files
    when_to_transfer_output: str | None = (
        None  # [HTCondor only] when_to_transfer_output
    )
    profile: str | None = None
    dry_run: bool = False
    logdir: str | None = None
    omp_num_threads: int | None = None
    openblas_num_threads: int | None = None
    collect: bool = False
    test_target: str | None = None
    test_marker: str | None = None
    test_filter: str | None = None
    test_opts: str | None = None
    with_report: bool = False
    max_concurrent: int | None = None
    save_runfile: str | None = None


@dataclass
class CalibpipeConfig:
    """Strongly-typed, single source of truth configuration object for calibpipe."""

    default_env: str = "main"
    default_profile: str | None = None
    paths: PathsConfig = field(default_factory=PathsConfig)
    site: SiteConfig = field(default_factory=SiteConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    run: RunConfig = field(default_factory=RunConfig)
    envs: dict[str, EnvSpec] = field(default_factory=dict)
    profiles: dict[str, ProfileConfig] = field(default_factory=dict)
    loaded_layers: list[Path] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data, loaded_layers=None):
        cfg = cls(
            default_env=data.get("default_env", "main"),
            default_profile=data.get("default_profile"),
            paths=PathsConfig.from_dict(data.get("paths", {})),
            site=SiteConfig.from_dict(data.get("site", {})),
            batch=BatchConfig.from_dict(data.get("batch", {})),
            run=RunConfig.from_dict(data.get("run", {})),
            envs={
                name: EnvSpec.from_dict({"branch": name, **value, "name": name})
                for name, value in data.get("envs", {}).items()
            },
            profiles={
                name: ProfileConfig.from_dict(value)
                for name, value in data.get("profiles", {}).items()
            },
            loaded_layers=[Path(p) for p in loaded_layers or []],
        )
        if "default_env" not in data and cfg.default_profile in cfg.profiles:
            cfg.default_env = cfg.profiles[cfg.default_profile].env or "main"
        for model in [cfg.run, *cfg.profiles.values()]:
            if model.ancillary is not None:
                model.ancillary = _ancillary_list(model.ancillary)
            if model.datapath is not None:
                model.datapath = _list_or_empty(model.datapath)
            if model.rundata is not None:
                model.rundata = _list_or_empty(model.rundata)
            if isinstance(model, ProfileConfig) and model.vis is not None:
                model.vis = _list_or_empty(model.vis)
        for model in cfg.profiles.values():
            for key in ("scipipe_rootdir", "scipipe_logdir", "casarundata"):
                value = getattr(model, key)
                if isinstance(value, (list, tuple)):
                    setattr(
                        model,
                        key,
                        resolve_first_existing_path(
                            value,
                            workspaces=model.workspaces
                            if model.workspaces is not None
                            else cfg.paths.workspaces,
                        ),
                    )
        return cfg


def tool_settings(config):
    return {
        **asdict(config.site),
        **{
            key: getattr(config.run, key)
            for key in ("use_custom_rcdir", "log2term")
        },
        "pixi_bin": config.paths.pixi_bin,
    }

ALIASES = {
    "description": "desc",
    "procedure": "recipe",
    "casa_dir": "casa_root",
    "casa_ver": "casa_root",
    "pl_ver": "heuristics_dir",
    "pipe_ver": "heuristics_dir",
    "pipe_dir": "heuristics_dir",
    "heuristics_root": "heuristics_dir",
    "casadata": "casarundata",
    "data": "casarundata",
    "ncores": "nprocs",
    "ntasks": "cores",
    "threads": "cpus_per_task",
    "queue": "partition",
    "noglogger": "nologger",
    "work_path": "workdir",
    "pp_rawdata": "rawdata",
    "pp_request": "ppr",
    "pp_vlarequest": "ppr",
    "gpus": "request_gpus",
    "central_manager": "pool",
    "central_manager_host": "pool",
    "central-manager-host": "pool",
    "schedd_name": "schedd",
    "schedd-name": "schedd",
    "remote": "schedd",
    "transfer_files": "should_transfer_files",
    "throttle": "max_concurrent",
    "report": "with_report",
    "test_dir": "test_target",
    "marker": "test_marker",
    "filter": "test_filter",
    "pytest_opts": "test_opts",
    "test_options": "test_opts",
}
SITE_TOOLS = {"pmr_home", "datapacker_home", "acsdata", "java_home"}
PATH_TOOLS = {"pixi_bin"}
BEHAVIOR = {"use_custom_rcdir", "log2term"}


def matches(value, annotation):
    if annotation is Any:
        return True
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        return any(matches(value, t) for t in get_args(annotation))
    if origin is list:
        return isinstance(value, list) and all(
            matches(v, get_args(annotation)[0]) for v in value
        )
    if origin is dict:
        return isinstance(value, dict) and all(
            isinstance(k, str) and matches(v, get_args(annotation)[1])
            for k, v in value.items()
        )
    if annotation is float:
        return type(value) in (int, float)
    if annotation is int:
        return type(value) is int
    return isinstance(value, annotation)


def validate_table(data, model, location, file):
    if not isinstance(data, dict):
        raise ConfigError(f"{file}: [{location}] must be a table")
    types = get_type_hints(model)
    allowed = {f.name for f in fields(model)} - (
        {"name"} if model is EnvSpec else set()
    )
    for key, value in data.items():
        if key not in allowed:
            if location == "paths" and key in SITE_TOOLS:
                hint = f"move it to [site].{key}"
            elif location == "site" and key in PATH_TOOLS:
                hint = f"move it to [paths].{key}"
            elif location == "site" and key in BEHAVIOR:
                hint = f"move it to [run].{key}"
            elif key in ALIASES:
                hint = f"use {ALIASES[key]}"
            elif model is ProfileConfig and isinstance(value, dict):
                hint = "use flat [profiles.<name>] keys"
            elif model is EnvSpec:
                hint = "custom environment variables belong in extra_vars"
            else:
                hint = "see config.example.toml"
            raise ConfigError(
                f"{file}: unknown or retired key [{location}].{key}; {hint}"
            )
        # Existing path candidates and explicit runtime-data disabling remain supported.
        if (
            key in {"scipipe_rootdir", "scipipe_logdir", "casarundata"}
            and isinstance(value, list)
            and all(isinstance(v, str) for v in value)
        ):
            continue
        if key in {"datapath", "rundata"} and (
            isinstance(value, str) or value is False
        ):
            continue
        if key == "vis" and isinstance(value, str):
            continue
        if key == "should_transfer_files" and isinstance(value, bool):
            continue
        if not matches(value, types[key]):
            raise ConfigError(
                f"{file}: invalid value for [{location}].{key}; expected {types[key]}"
            )


def validate_config(data, file):
    tables = {
        "paths": PathsConfig,
        "site": SiteConfig,
        "run": RunConfig,
        "batch": BatchConfig,
    }
    for key, value in data.items():
        if key in tables:
            validate_table(value, tables[key], key, file)
        elif key in {"envs", "profiles"}:
            if not isinstance(value, dict):
                raise ConfigError(f"{file}: [{key}] must be a table")
            for name, table in value.items():
                validate_table(
                    table,
                    EnvSpec if key == "envs" else ProfileConfig,
                    f"{key}.{name}",
                    file,
                )
        elif key in {"default_env", "default_profile"}:
            if not isinstance(value, str):
                raise ConfigError(f"{file}: {key} must be a string")
        else:
            hint = (
                "use [profiles.<name>]"
                if key == "batches"
                else "see config.example.toml"
            )
            raise ConfigError(f"{file}: unknown or retired table/key {key}; {hint}")

def resolve_run_options(
    config: CalibpipeConfig | dict[str, Any],
    cli_opts: Any,
) -> ResolvedRunOptions:
    """Resolve runtime options by cascading CLI flags over profile over config.toml over defaults.

    Precedence: CLI flag > profile ([profiles.<name>]) > config.toml ([run] / [site]) > built-in defaults.
    """
    if isinstance(config, CalibpipeConfig):
        cfg = config
    else:
        cfg = CalibpipeConfig.from_dict(config)

    profile_name = getattr(cli_opts, "profile", None)
    if not profile_name and cfg.default_profile:
        profile_name = cfg.default_profile
    profile: ProfileConfig | None = None
    if profile_name:
        if profile_name not in cfg.profiles:
            valid_profiles = ", ".join(cfg.profiles.keys()) or "none defined"
            raise ConfigError(
                f"Unknown profile '{profile_name}'. Available profiles in config: {valid_profiles}"
            )
        profile = cfg.profiles[profile_name]

    def _val(attr: str, cfg_val: Any) -> Any:
        cli_v = getattr(cli_opts, attr, None)
        if cli_v is None and isinstance(getattr(cli_opts, "overrides", None), dict):
            cli_v = cli_opts.overrides.get(attr)
        if cli_v is not None:
            return cli_v
        if profile is not None:
            prof_v = getattr(profile, attr, None)
            if prof_v is not None:
                return prof_v
        return cfg_val

    use_custom_rcdir = bool(_val("use_custom_rcdir", cfg.run.use_custom_rcdir))

    symlink_shortcuts = _val("symlink_shortcuts", cfg.run.symlink_shortcuts)
    log2term = _val("log2term", cfg.run.log2term)
    verbose = _val("verbose", cfg.run.verbose)
    recipe = _val("recipe", cfg.run.recipe)
    cli_nprocs = getattr(cli_opts, "nprocs", None)
    prof_nprocs = profile.nprocs if profile else None
    nprocs = (
        cli_nprocs
        if cli_nprocs is not None
        else (prof_nprocs if prof_nprocs is not None else cfg.run.nprocs)
    )
    loglevel = _val("loglevel", cfg.run.loglevel)
    useresume = _val("useresume", cfg.run.useresume)
    env_name = (
        getattr(cli_opts, "env", None)
        or (profile.env if profile and profile.env else None)
        or cfg.default_env
    )
    workdir = (
        getattr(cli_opts, "workdir", "")
        or (profile.workdir if profile and profile.workdir else "")
        or getattr(cfg.run, "workdir", "")
        or ""
    )
    rawdata = (
        getattr(cli_opts, "rawdata", None)
        or (profile.rawdata if profile and profile.rawdata else None)
        or getattr(cfg.run, "rawdata", None)
        or None
    )

    omp_num_threads = _val("omp_num_threads", cfg.run.omp_num_threads)
    openblas_num_threads = _val("openblas_num_threads", cfg.run.openblas_num_threads)
    omp_max_threads = _val("omp_max_threads", cfg.run.omp_max_threads)
    mem_frac = _val("mem_frac", cfg.run.mem_frac)
    oversubscribe = _val("oversubscribe", cfg.run.oversubscribe)
    bind_to = _val("bind_to", cfg.run.bind_to)
    map_by = _val("map_by", cfg.run.map_by)
    psrecord = _val("psrecord", cfg.run.psrecord)
    memstats = _val("memstats", cfg.run.memstats)
    pl_psrecord = _val("pl_psrecord", cfg.run.pl_psrecord)
    backup = _val("backup", cfg.run.backup)
    staticobscal = _val("staticobscal", cfg.run.staticobscal)
    oldcont = _val("oldcont", cfg.run.oldcont)
    cont_dat = _val("cont_dat", cfg.run.cont_dat)
    jyperk_csv = _val("jyperk_csv", cfg.run.jyperk_csv)
    parameter_list = _val("parameter_list", cfg.run.parameter_list)
    ancillary = _ancillary_list(_val("ancillary", cfg.run.ancillary))

    # Execution target resolution: CLI flags > profile targets > default empty
    cli_mous = getattr(cli_opts, "mous", "") or ""
    cli_vis = list(getattr(cli_opts, "vis", []) or [])
    cli_script = getattr(cli_opts, "script", "") or ""
    cli_cmd = getattr(cli_opts, "cmd", "") or ""
    cli_ppr = getattr(cli_opts, "ppr", None)
    cli_interactive = bool(getattr(cli_opts, "interactive", False))

    cli_has_target = bool(
        cli_mous
        or cli_vis
        or cli_script
        or cli_cmd
        or (cli_ppr and not cli_mous)
        or cli_interactive
    )

    if cli_has_target:
        mous = cli_mous
        vis = cli_vis
        script = cli_script
        cmd = cli_cmd
        ppr = cli_ppr
        interactive = cli_interactive
    elif profile is not None:
        prof_targets = []
        if profile.mous:
            prof_targets.append(f"mous={profile.mous}")
        if profile.vis:
            prof_targets.append(f"vis={' '.join(profile.vis)}")
        if profile.script:
            prof_targets.append(f"script={profile.script}")
        if profile.cmd:
            prof_targets.append("cmd")
        if profile.interactive:
            prof_targets.append("interactive")
        if profile.ppr and not profile.mous:
            prof_targets.append(f"ppr={profile.ppr}")

        if len(prof_targets) > 1:
            raise ConfigError(
                f"Conflicting execution targets specified in profile '{profile_name}': "
                f"{', '.join(prof_targets)}. Please specify only one target."
            )

        mous = profile.mous or ""
        vis = list(profile.vis or [])
        script = profile.script or ""
        cmd = profile.cmd or ""
        ppr = profile.ppr or (
            getattr(cfg.run, "ppr", None) if not prof_targets else None
        )
        interactive = (
            bool(profile.interactive) if profile.interactive is not None else False
        )
    else:
        mous = ""
        vis = []
        script = ""
        cmd = ""
        ppr = getattr(cfg.run, "ppr", None)
        interactive = False

    if getattr(cli_opts, "interactive", None) is not None:
        interactive = bool(cli_opts.interactive)

    cli_vla = getattr(cli_opts, "vla", None)
    if cli_vla is not None:
        vla = bool(cli_vla)
    elif profile is not None and profile.vla is not None:
        vla = bool(profile.vla)
    elif getattr(cfg.run, "vla", None) is not None:
        vla = bool(cfg.run.vla)
    else:
        vla = False

    # Dry-run: CLI > Profile > [run]
    cli_dry_run = getattr(cli_opts, "dry_run", None)
    if cli_dry_run is not None:
        dry_run = bool(cli_dry_run)
    elif profile is not None and profile.dry_run is not None:
        dry_run = bool(profile.dry_run)
    else:
        dry_run = cfg.run.dry_run

    # Autoreload: CLI > Profile > [run]
    cli_autoreload = getattr(cli_opts, "autoreload", None)
    if cli_autoreload is not None:
        autoreload = bool(cli_autoreload)
    elif profile is not None and profile.autoreload is not None:
        autoreload = bool(profile.autoreload)
    else:
        autoreload = cfg.run.autoreload

    # Nologger: CLI > Profile > [run]
    cli_nologger = getattr(cli_opts, "nologger", None)
    if cli_nologger is not None:
        nologger = bool(cli_nologger)
    elif profile is not None and profile.nologger is not None:
        nologger = bool(profile.nologger)
    else:
        nologger = cfg.run.nologger

    # Logfile: CLI > Profile > [run]
    cli_logfile = getattr(cli_opts, "logfile", None)
    if cli_logfile is not None:
        logfile = str(cli_logfile).strip()
    elif profile is not None and profile.logfile is not None:
        logfile = str(profile.logfile).strip()
    elif cfg.run.logfile:
        logfile = str(cfg.run.logfile).strip()
    else:
        logfile = None

    # Datapath & rundata resolution: CLI > Profile > [run] > [paths]
    cli_datapath = getattr(cli_opts, "datapath", None)
    if cli_datapath is not None:
        resolved_datapath = _list_or_empty(cli_datapath)
    elif profile is not None and profile.datapath is not None:
        resolved_datapath = list(profile.datapath)
    elif cfg.run.datapath is not None:
        resolved_datapath = list(cfg.run.datapath)
    elif cfg.paths.datapath:
        resolved_datapath = list(cfg.paths.datapath)
    else:
        resolved_datapath = []

    cli_rundata = getattr(cli_opts, "rundata", None)
    cli_casarundata = getattr(cli_opts, "casarundata", None)
    cli_crd = cli_casarundata

    rundata_specified = False
    if cli_rundata is not None:
        resolved_rundata = _list_or_empty(cli_rundata)
        rundata_specified = True
    elif cli_crd is not None:
        resolved_rundata = [cli_crd] if isinstance(cli_crd, str) else list(cli_crd)
        rundata_specified = True
    elif profile is not None and profile.rundata is not None:
        resolved_rundata = list(profile.rundata)
        rundata_specified = True
    elif profile is not None and (profile.casarundata):
        prof_crd = profile.casarundata
        resolved_rundata = [prof_crd]
        rundata_specified = True
    elif cfg.run.rundata is not None:
        resolved_rundata = list(cfg.run.rundata)
        rundata_specified = True
    elif cfg.paths.rundata is not None:
        resolved_rundata = list(cfg.paths.rundata)
        rundata_specified = True
    else:
        resolved_rundata = []

    # Resolve environment and path overrides: CLI > Profile
    cli_casa_root = getattr(cli_opts, "casa_root", None)
    prof_casa_root = profile.casa_root if profile else None
    resolved_casa_root = cli_casa_root or prof_casa_root

    cli_heuristics_dir = getattr(cli_opts, "heuristics_dir", None)
    prof_heuristics_dir = profile.heuristics_dir if profile else None
    resolved_heuristics_dir = cli_heuristics_dir or prof_heuristics_dir

    cli_pixi_dir = getattr(cli_opts, "pixi_dir", None)
    prof_pixi_dir = profile.pixi_dir if profile else None
    resolved_pixi_dir = cli_pixi_dir or prof_pixi_dir

    cli_pixi_env = getattr(cli_opts, "pixi_env", None)
    prof_pixi_env = profile.pixi_env if profile else None
    resolved_pixi_env = cli_pixi_env or prof_pixi_env

    cli_branch = getattr(cli_opts, "branch", None)
    prof_branch = profile.branch if profile else None
    resolved_branch = cli_branch or prof_branch

    resolved_workspaces = list(
        profile.workspaces
        if profile is not None and profile.workspaces is not None
        else cfg.paths.workspaces
    )

    cli_casarundata = getattr(cli_opts, "casarundata", None)
    prof_casarundata = getattr(profile, "casarundata", None) if profile else None
    resolved_casarundata = (
        cli_casarundata
        if cli_casarundata is not None
        else (
            prof_casarundata if prof_casarundata is not None else cfg.paths.casarundata
        )
    )
    if isinstance(resolved_casarundata, (list, tuple)):
        resolved_casarundata = (
            resolve_first_existing_path(
                resolved_casarundata, workspaces=resolved_workspaces
            )
            or None
        )
    elif resolved_casarundata is not None:
        resolved_casarundata = str(resolved_casarundata).strip() or None

    if not resolved_rundata and resolved_casarundata and not rundata_specified:
        resolved_rundata = [resolved_casarundata]
        rundata_specified = True

    cli_scipipe_rootdir = getattr(cli_opts, "scipipe_rootdir", None)
    prof_scipipe_rootdir = profile.scipipe_rootdir if profile else None
    resolved_scipipe_rootdir = cli_scipipe_rootdir or prof_scipipe_rootdir

    cli_scipipe_logdir = getattr(cli_opts, "scipipe_logdir", None)
    prof_scipipe_logdir = profile.scipipe_logdir if profile else None
    resolved_scipipe_logdir = cli_scipipe_logdir or prof_scipipe_logdir

    cli_obscaldir = getattr(cli_opts, "obscaldir", None)
    prof_obscaldir = profile.obscaldir if profile else None
    raw_obscaldir = cli_obscaldir or prof_obscaldir or cfg.paths.obscaldir
    if isinstance(raw_obscaldir, (list, tuple)):
        obscal_str = resolve_first_existing_path(
            raw_obscaldir, workspaces=resolved_workspaces
        )
    elif raw_obscaldir is not None:
        obscal_str = str(raw_obscaldir).strip()
    else:
        obscal_str = ""
    resolved_obscaldir = os.path.expanduser(obscal_str) if obscal_str else None

    if isinstance(resolved_scipipe_rootdir, (list, tuple)):
        resolved_scipipe_rootdir = resolve_first_existing_path(
            resolved_scipipe_rootdir, workspaces=resolved_workspaces
        )
    elif resolved_scipipe_rootdir is not None:
        resolved_scipipe_rootdir = str(resolved_scipipe_rootdir)

    if isinstance(resolved_scipipe_logdir, (list, tuple)):
        resolved_scipipe_logdir = resolve_first_existing_path(
            resolved_scipipe_logdir, workspaces=resolved_workspaces
        )
    elif resolved_scipipe_logdir is not None:
        resolved_scipipe_logdir = str(resolved_scipipe_logdir)

    # Dynamic workspace search path resolution
    workspaces = resolved_workspaces
    if workspaces:
        accessible_workspaces = [
            ws for ws in workspaces if ws and _cached_is_dir(str(Path(ws).expanduser()))
        ]
        search_workspaces = (
            accessible_workspaces if accessible_workspaces else list(workspaces)
        )
        preferred_ws: str | None = None

        def _resolve_one(item: str | Path) -> Path:
            nonlocal preferred_ws
            ordered = (
                [preferred_ws] + [w for w in search_workspaces if w != preferred_ws]
                if preferred_ws
                else search_workspaces
            )
            res = resolve_workspace_path(item, ordered)
            if not preferred_ws and res != Path(item):
                res_str = str(res)
                for ws in search_workspaces:
                    if res_str.startswith(str(Path(ws).expanduser())):
                        preferred_ws = ws
                        break
            return res

        if vis:
            vis = [str(_resolve_one(v)) for v in vis]
        if recipe and (recipe.endswith(".xml") or "/" in recipe):
            recipe = str(_resolve_one(recipe))
        if script:
            script = str(_resolve_one(script))
        if ppr:
            ppr = str(_resolve_one(ppr))
        if rawdata:
            rawdata = str(_resolve_one(rawdata))
        if cont_dat:
            cont_dat = str(_resolve_one(cont_dat))
        if jyperk_csv:
            jyperk_csv = str(_resolve_one(jyperk_csv))
        if parameter_list:
            parameter_list = str(_resolve_one(parameter_list))
        if ancillary:
            resolved_anc: list[Any] = []
            for a in ancillary:
                if isinstance(a, (list, tuple)) and len(a) == 2:
                    src_resolved = str(_resolve_one(a[0]))
                    resolved_anc.append((src_resolved, str(a[1])))
                elif isinstance(a, dict) and "src" in a and "dest" in a:
                    src_resolved = str(_resolve_one(a["src"]))
                    resolved_anc.append((src_resolved, str(a["dest"])))
                elif isinstance(a, str) and ":" in a and not Path(a).exists():
                    src_part, dst_part = a.split(":", 1)
                    src_resolved = str(_resolve_one(src_part))
                    resolved_anc.append((src_resolved, str(dst_part)))
                else:
                    resolved_anc.append(str(_resolve_one(a)))
            ancillary = resolved_anc
        if resolved_datapath:
            resolved_datapath = [str(_resolve_one(d)) for d in resolved_datapath]
        if resolved_rundata:
            resolved_rundata = [str(_resolve_one(r)) for r in resolved_rundata]

    if resolved_datapath:
        resolved_datapath = [
            str(Path(p).expanduser().resolve())
            if _safe_exists(Path(p).expanduser())
            else str(Path(p).expanduser())
            for p in resolved_datapath
        ]
    if resolved_rundata:
        resolved_rundata = [
            str(Path(p).expanduser().resolve())
            if _safe_exists(Path(p).expanduser())
            else str(Path(p).expanduser())
            for p in resolved_rundata
        ]
    if rawdata:
        rawdata_p = Path(rawdata).expanduser()
        rawdata = (
            str(rawdata_p.resolve()) if _safe_exists(rawdata_p) else str(rawdata_p)
        )

    if workdir:
        user_name = os.environ.get("USER", "")
        effective_rootdir = resolved_scipipe_rootdir or cfg.paths.scipipe_rootdir
        if isinstance(effective_rootdir, (list, tuple)):
            effective_rootdir = resolve_first_existing_path(
                effective_rootdir, workspaces=workspaces, user=user_name
            )
        root_dir = (
            Path(effective_rootdir.replace("{user}", user_name)).expanduser()
            if effective_rootdir
            else None
        )
        raw_wd = Path(workdir).expanduser()
        if not raw_wd.is_absolute() and not str(workdir).startswith("~"):
            found_in_ws = None
            if workspaces:
                for ws in workspaces:
                    candidate = Path(ws).expanduser() / raw_wd
                    if _safe_exists(candidate):
                        found_in_ws = str(candidate.resolve())
                        break
            if found_in_ws:
                workdir = found_in_ws
            elif root_dir is not None:
                workdir = str((root_dir / raw_wd).resolve())
            else:
                workdir = str(raw_wd.resolve())
        else:
            workdir = str(raw_wd.resolve())

    return ResolvedRunOptions(
        profile=profile_name,
        mous=mous,
        env_name=env_name,
        recipe=recipe,
        nprocs=int(nprocs),
        loglevel=loglevel,
        useresume=bool(useresume),
        use_custom_rcdir=use_custom_rcdir,
        symlink_shortcuts=bool(symlink_shortcuts),
        log2term=bool(log2term),
        flag_dir=_val("flag_dir", cfg.run.flag_dir),
        ppr=ppr,
        rawdata=rawdata,
        subdir=_val("subdir", cfg.run.subdir),
        onlysemipass=_val("onlysemipass", cfg.run.onlysemipass) or "",
        verbose=bool(verbose),
        vis=vis,
        script=script,
        cmd=cmd,
        vla=vla,
        workdir=workdir,
        interactive=interactive,
        omp_num_threads=int(omp_num_threads) if omp_num_threads is not None else None,
        openblas_num_threads=int(openblas_num_threads)
        if openblas_num_threads is not None
        else None,
        omp_max_threads=int(omp_max_threads) if omp_max_threads is not None else None,
        mem_frac=float(mem_frac) if mem_frac is not None else None,
        oversubscribe=bool(oversubscribe),
        bind_to=str(bind_to) if bind_to is not None else None,
        map_by=str(map_by) if map_by is not None else None,
        psrecord=bool(psrecord),
        memstats=bool(memstats),
        pl_psrecord=bool(pl_psrecord),
        backup=bool(backup),
        staticobscal=bool(staticobscal),
        oldcont=bool(oldcont),
        cont_dat=str(cont_dat) if cont_dat is not None else None,
        jyperk_csv=str(jyperk_csv) if jyperk_csv is not None else None,
        parameter_list=str(parameter_list) if parameter_list is not None else None,
        ancillary=list(ancillary or []),
        datapath=resolved_datapath,
        rundata=resolved_rundata,
        rundata_specified=rundata_specified,
        dry_run=dry_run,
        autoreload=autoreload,
        nologger=nologger,
        logfile=logfile,
        casa_root=resolved_casa_root,
        heuristics_dir=resolved_heuristics_dir,
        pixi_dir=resolved_pixi_dir,
        pixi_env=resolved_pixi_env,
        branch=resolved_branch,
        workspaces=resolved_workspaces,
        scipipe_rootdir=resolved_scipipe_rootdir,
        scipipe_logdir=resolved_scipipe_logdir,
        casarundata=resolved_casarundata,
        obscaldir=resolved_obscaldir,
    )


def resolve_batch_options(
    config: CalibpipeConfig | dict[str, Any],
    cli_args: Any,
) -> ResolvedBatchOptions:
    """Resolve batch options by cascading CLI flags over profile over config.toml [batch] over defaults."""
    if isinstance(config, CalibpipeConfig):
        cfg = config
    else:
        cfg = CalibpipeConfig.from_dict(config)

    profile_name = getattr(cli_args, "profile", None) or cfg.default_profile
    pipefile_arg = getattr(cli_args, "pipefile", None)
    if not pipefile_arg and not profile_name:
        raise ConfigError("Either a pipefile or a --profile must be specified.")

    profile: ProfileConfig | None = None
    active_batch: BatchConfig = cfg.batch

    if profile_name:
        if profile_name in cfg.profiles:
            profile = cfg.profiles[profile_name]
            if profile.interactive:
                raise ConfigError(
                    f"Cannot submit interactive profile '{profile_name}' to the batch scheduler. "
                    "Interactive CASA sessions must be run locally using 'calibpipe run -i' or 'calibpipe run -p <name>'."
                )
            prof_dict = {
                k: v
                for k, v in asdict(profile).items()
                if v is not None and hasattr(cfg.batch, k)
            }
            if "partition" not in prof_dict and profile.partition is not None:
                prof_dict["partition"] = profile.partition
            merged_dict = asdict(cfg.batch)
            merged_dict.update(prof_dict)
            active_batch = BatchConfig.from_dict(merged_dict)
        else:
            available = sorted(cfg.profiles)
            raise ConfigError(
                f"Unknown batch profile '{profile_name}'. Available profiles: {available}"
            )

    env_name = (
        getattr(cli_args, "env", None)
        or (profile.env if profile and profile.env else None)
        or cfg.default_env
    )

    cli_partition = getattr(cli_args, "partition", None)
    partition = cli_partition if cli_partition else active_batch.partition

    cores = getattr(cli_args, "cores", None)
    if cores is None:
        cores = active_batch.cores

    mem = getattr(cli_args, "mem", None)
    if mem is None:
        mem = active_batch.mem

    node = getattr(cli_args, "node", None) or active_batch.node
    mail_type = getattr(cli_args, "mail_type", None) or active_batch.mail_type
    scheduler = getattr(cli_args, "scheduler", None) or active_batch.scheduler

    # Optional directives — CLI overrides config, then falls back to None/default.
    def _cli_or_cfg(attr: str, cfg_val: Any) -> Any:
        v = getattr(cli_args, attr, None)
        return v if v is not None else cfg_val

    user_name = os.environ.get("USER", "")
    cli_rootdir = getattr(cli_args, "scipipe_rootdir", None)
    prof_rootdir = profile.scipipe_rootdir if profile else None
    effective_rootdir = cli_rootdir or prof_rootdir or cfg.paths.scipipe_rootdir
    if isinstance(effective_rootdir, (list, tuple)):
        resolved_workspaces = list(
            profile.workspaces
            if profile is not None and profile.workspaces is not None
            else cfg.paths.workspaces
        )
        effective_rootdir = resolve_first_existing_path(
            effective_rootdir, workspaces=resolved_workspaces, user=user_name
        )
    root_dir = (
        Path(effective_rootdir.replace("{user}", user_name)).expanduser()
        if effective_rootdir
        else None
    )

    walltime = _cli_or_cfg("walltime", active_batch.walltime)
    nodelist = _cli_or_cfg("nodelist", active_batch.nodelist)
    chdir = _cli_or_cfg("chdir", active_batch.chdir)
    requested_workdir = getattr(cli_args, "workdir", None) or (
        profile.workdir if profile else None
    )
    if chdir is None and requested_workdir:
        raw_workdir = Path(os.path.expanduser(requested_workdir))
        # Derive project_root so working/ or custom workdir can be cleanly rotated if backup=True
        cli_backup = getattr(cli_args, "backup", None)
        is_backup = bool(
            cli_backup
            if cli_backup is not None
            else (profile.backup if profile else False)
            or getattr(cfg.run, "backup", False)
        )
        raw_chdir = (
            raw_workdir.parent
            if (raw_workdir.name == "working" or is_backup)
            else raw_workdir
        )
        if (
            not raw_chdir.is_absolute()
            and not str(raw_chdir).startswith("~")
            and root_dir is not None
        ):
            chdir = str(root_dir / raw_chdir)
        else:
            chdir = str(raw_chdir)
    elif chdir is not None:
        raw_chdir = Path(os.path.expanduser(str(chdir)))
        if (
            not raw_chdir.is_absolute()
            and not str(raw_chdir).startswith("~")
            and root_dir is not None
        ):
            chdir = str(root_dir / raw_chdir)
        else:
            chdir = str(raw_chdir)
    cpus_per_task = _int_or_none(
        _cli_or_cfg("cpus_per_task", active_batch.cpus_per_task)
    )
    mem_per_cpu = _cli_or_cfg("mem_per_cpu", active_batch.mem_per_cpu)
    hint = _cli_or_cfg("hint", active_batch.hint)
    ntasks_per_core = _int_or_none(
        _cli_or_cfg("ntasks_per_core", active_batch.ntasks_per_core)
    )
    distribution = _cli_or_cfg("distribution", active_batch.distribution)

    # no_requeue: CLI flag takes precedence; default True (safe default)
    cli_no_requeue = getattr(cli_args, "no_requeue", None)
    no_requeue = (
        cli_no_requeue if cli_no_requeue is not None else active_batch.no_requeue
    )

    cli_dry_run = getattr(cli_args, "dry_run", None)
    dry_run = bool(cli_dry_run if cli_dry_run is not None else active_batch.dry_run)

    requirements = _cli_or_cfg(
        "requirements", getattr(active_batch, "requirements", None)
    )
    cli_omp = getattr(cli_args, "omp_num_threads", None)
    omp_num_threads = _int_or_none(
        cli_omp
        if cli_omp is not None
        else (
            getattr(profile, "omp_num_threads", None)
            if profile and getattr(profile, "omp_num_threads", None) is not None
            else getattr(active_batch, "omp_num_threads", None)
        )
    )
    if omp_num_threads is None and cfg.run.omp_num_threads is not None:
        omp_num_threads = cfg.run.omp_num_threads

    cli_openblas = getattr(cli_args, "openblas_num_threads", None)
    openblas_num_threads = _int_or_none(
        cli_openblas
        if cli_openblas is not None
        else (
            getattr(profile, "openblas_num_threads", None)
            if profile and getattr(profile, "openblas_num_threads", None) is not None
            else getattr(active_batch, "openblas_num_threads", None)
        )
    )
    if openblas_num_threads is None and cfg.run.openblas_num_threads is not None:
        openblas_num_threads = cfg.run.openblas_num_threads

    raw_logdir = active_batch.logdir or cfg.paths.scipipe_logdir or None
    if isinstance(raw_logdir, (list, tuple)):
        raw_logdir = resolve_first_existing_path(raw_logdir)
    logdir = _cli_or_cfg("logdir", raw_logdir)

    cli_gpus = getattr(cli_args, "request_gpus", None)
    if cli_gpus is not None:
        request_gpus = int(cli_gpus)
    elif profile and profile.request_gpus is not None:
        request_gpus = int(profile.request_gpus)
    else:
        request_gpus = int(getattr(active_batch, "request_gpus", 0))

    cli_pool = getattr(cli_args, "pool", None)
    if cli_pool is not None:
        pool = cli_pool
    elif profile and profile.pool is not None:
        pool = profile.pool
    else:
        pool = getattr(active_batch, "pool", None)

    cli_schedd = getattr(cli_args, "schedd", None)
    if cli_schedd is not None:
        schedd = cli_schedd
    elif profile and profile.schedd is not None:
        schedd = profile.schedd
    else:
        schedd = getattr(active_batch, "schedd", None)

    cli_spool = getattr(cli_args, "spool", None)
    if cli_spool is not None:
        spool = bool(cli_spool)
    elif profile and profile.spool is not None:
        spool = bool(profile.spool)
    else:
        spool = bool(getattr(active_batch, "spool", False))

    cli_transfer = getattr(cli_args, "should_transfer_files", None)
    if cli_transfer is not None:
        should_transfer_files = str(cli_transfer)
    elif profile and profile.should_transfer_files is not None:
        should_transfer_files = str(profile.should_transfer_files)
    else:
        should_transfer_files = getattr(active_batch, "should_transfer_files", None)

    cli_when = getattr(cli_args, "when_to_transfer_output", None)
    if cli_when is not None:
        when_to_transfer_output = str(cli_when)
    elif profile and profile.when_to_transfer_output is not None:
        when_to_transfer_output = str(profile.when_to_transfer_output)
    else:
        when_to_transfer_output = getattr(active_batch, "when_to_transfer_output", None)

    cli_collect = getattr(cli_args, "collect", None)
    collect = bool(
        cli_collect
        if cli_collect is not None
        else (profile.collect if profile and profile.collect is not None else False)
    )

    cli_test_target = getattr(cli_args, "test_target", None)
    test_target = (
        cli_test_target
        if cli_test_target is not None
        else (profile.test_target if profile else None)
    )

    cli_test_marker = getattr(cli_args, "test_marker", None)
    test_marker = (
        cli_test_marker
        if cli_test_marker is not None
        else (profile.test_marker if profile else None)
    )

    cli_test_filter = getattr(cli_args, "test_filter", None)
    test_filter = (
        cli_test_filter
        if cli_test_filter is not None
        else (profile.test_filter if profile else None)
    )

    cli_test_opts = getattr(cli_args, "test_opts", None)
    test_opts = (
        cli_test_opts
        if cli_test_opts is not None
        else (profile.test_opts if profile else None)
    )

    cli_with_report = getattr(cli_args, "with_report", None)
    with_report = bool(
        cli_with_report
        if cli_with_report is not None
        else (
            profile.with_report
            if profile and profile.with_report is not None
            else False
        )
    )

    cli_throttle = getattr(cli_args, "max_concurrent", None)
    max_concurrent = _int_or_none(
        cli_throttle
        if cli_throttle is not None
        else (
            profile.max_concurrent
            if profile and profile.max_concurrent is not None
            else getattr(active_batch, "max_concurrent", None)
        )
    )

    return ResolvedBatchOptions(
        pipefile=Path(pipefile_arg) if pipefile_arg else None,
        env_name=env_name,
        partition=str(partition),
        cores=int(cores),
        mem=int(mem),
        node=str(node),
        mail_type=str(mail_type),
        outfile=_cli_or_cfg("outfile", active_batch.outfile),
        errfile=_cli_or_cfg("errfile", active_batch.errfile),
        walltime=walltime,
        nodelist=nodelist,
        chdir=chdir,
        cpus_per_task=cpus_per_task,
        mem_per_cpu=mem_per_cpu,
        hint=hint,
        ntasks_per_core=ntasks_per_core,
        distribution=distribution,
        no_requeue=bool(no_requeue),
        scheduler=str(scheduler),
        requirements=requirements,
        request_gpus=request_gpus,
        pool=pool,
        schedd=schedd,
        spool=spool,
        should_transfer_files=should_transfer_files,
        when_to_transfer_output=when_to_transfer_output,
        profile=profile_name,
        dry_run=dry_run,
        logdir=logdir,
        omp_num_threads=omp_num_threads,
        openblas_num_threads=openblas_num_threads,
        collect=collect,
        test_target=test_target,
        test_marker=test_marker,
        test_filter=test_filter,
        test_opts=test_opts,
        with_report=with_report,
        max_concurrent=max_concurrent,
        save_runfile=_cli_or_cfg("save_runfile", active_batch.save_runfile),
    )


def validate_target(options, *, require=True):
    targets = [
        name
        for name in ("mous", "vis", "script", "cmd", "interactive")
        if getattr(options, name, None)
    ]
    if options.ppr and not options.mous:
        targets.append("ppr")
    if len(targets) > 1:
        raise ConfigError(
            f"Conflicting execution targets specified: {', '.join(targets)}. Please specify only one target."
        )
    if not targets and require:
        raise ConfigError(
            "No execution target specified. Provide --mous, --vis, --script, --cmd, --ppr, or --interactive (or define a target in the profile)."
        )

def _deep_merge_dict(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge overlay dictionary into base dictionary.

    Nested dictionaries are merged recursively. All other values in overlay
    replace the corresponding value in base.

    Args:
        base: Baseline mapping (e.g. from site configuration).
        overlay: Higher-priority mapping (e.g. from user or project configuration).

    Returns:
        New dictionary containing the deep-merged result.
    """
    merged = dict(base)
    for key, val in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge_dict(merged[key], val)
        else:
            merged[key] = val
    return merged


def find_site_config() -> Path | None:
    """Locate the site-level configuration file if present.

    Search order:
    1. $CALIBPIPE_SITE_CONFIG environment variable (must exist if set).
    2. /etc/calibpipe/config.toml (standard Linux system configuration).
    3. config.site.toml in the repository root (e.g. for cluster shared checkouts).
    4. config.site.toml alongside the calibpipe package.

    Returns:
        Resolved Path to the site configuration file, or None if no site configuration exists.

    Raises:
        ConfigError: If $CALIBPIPE_SITE_CONFIG is set but points to a non-existent file.
    """
    env_site = os.environ.get("CALIBPIPE_SITE_CONFIG")
    if env_site:
        path = Path(env_site).expanduser().resolve()
        if not path.is_file():
            raise ConfigError(f"$CALIBPIPE_SITE_CONFIG file not found: {path}")
        return path

    etc_config = Path("/etc/calibpipe/config.toml")
    if etc_config.is_file():
        return etc_config.resolve()

    repo_site_config = Path(__file__).resolve().parent.parent.parent / "config.site.toml"
    if repo_site_config.is_file():
        return repo_site_config.resolve()

    pkg_site_config = Path(__file__).resolve().parent / "config.site.toml"
    if pkg_site_config.is_file():
        return pkg_site_config.resolve()

    return None


def find_xdg_user_config() -> Path | None:
    """Locate user XDG configuration file (~/.config/calibpipe/config.toml).

    Returns:
        Resolved Path to the XDG config file, or None if not found.
    """
    xdg_base = os.environ.get("XDG_CONFIG_HOME")
    if xdg_base:
        user_xdg = Path(xdg_base).expanduser() / "calibpipe" / "config.toml"
    else:
        user_xdg = Path.home() / ".config" / "calibpipe" / "config.toml"

    if user_xdg.is_file():
        return user_xdg.resolve()
    return None


def find_dot_user_config() -> Path | None:
    """Locate user dot-directory configuration file (~/.calibpipe/config.toml).

    Returns:
        Resolved Path to the ~/.calibpipe/config.toml file, or None if not found.
    """
    dot_user = Path.home() / ".calibpipe" / "config.toml"
    if dot_user.is_file():
        return dot_user.resolve()
    return None


def find_user_configs() -> list[Path]:
    """Locate all existing user-level configuration files in merge order.

    Returns paths for:
    1. $XDG_CONFIG_HOME/calibpipe/config.toml (or ~/.config/calibpipe/config.toml)
    2. ~/.calibpipe/config.toml

    Returns:
        List of existing user configuration paths in increasing priority order.
    """
    configs: list[Path] = []
    xdg = find_xdg_user_config()
    if xdg:
        configs.append(xdg)
    dot = find_dot_user_config()
    if dot:
        configs.append(dot)
    return configs


def find_user_config() -> Path | None:
    """Locate the highest-precedence user-level configuration file if present.

    Search order:
    1. ~/.calibpipe/config.toml
    2. $XDG_CONFIG_HOME/calibpipe/config.toml (or ~/.config/calibpipe/config.toml)

    Returns:
        Resolved Path to the user configuration file, or None if not found.
    """
    configs = find_user_configs()
    return configs[-1] if configs else None


def find_project_config() -> Path | None:
    """Locate the workspace or current-directory configuration file if present.

    Search order:
    1. ./config.toml (in current working directory).
    2. ./.calibpipe.toml.

    Returns:
        Resolved Path to the project configuration file, or None if not found.
    """
    cwd_config = Path.cwd() / "config.toml"
    if cwd_config.is_file():
        return cwd_config.resolve()

    dot_config = Path.cwd() / ".calibpipe.toml"
    if dot_config.is_file():
        return dot_config.resolve()

    return None


def find_config_layers(
    cli_arg: str | Path | None = None,
    *,
    include_site: bool = True,
) -> list[Path]:
    """Discover all applicable configuration files in increasing priority order.

    Resolution layers (lowest to highest priority):
    1. Site configuration (system-wide cluster defaults, tool paths, baseline envs)
    2. User XDG configuration (~/.config/calibpipe/config.toml)
    3. User home configuration (~/.calibpipe/config.toml)
    4. Workspace configuration (./config.toml)
    5. Explicit CLI argument (--config) or $CALIBPIPE_CONFIG

    If an explicit CLI argument or $CALIBPIPE_CONFIG is provided, it replaces
    the user and workspace layers, but still layers on top of the site configuration
    (unless site config is excluded via include_site=False or $CALIBPIPE_NO_SITE_CONFIG=1).

    Args:
        cli_arg: Optional explicit configuration path provided by CLI.
        include_site: Whether to search for and include the site-level configuration layer.

    Returns:
        List of resolved Paths in order of application (base first, overlay last).

    Raises:
        ConfigError: If an explicit path does not exist, or if no configuration file
            can be found at any layer.
    """
    no_site_env = os.environ.get("CALIBPIPE_NO_SITE_CONFIG", "").lower() in (
        "1",
        "true",
        "yes",
    )
    should_include_site = include_site and not no_site_env

    layers: list[Path] = []

    if should_include_site:
        site_cfg = find_site_config()
        if site_cfg:
            layers.append(site_cfg)

    explicit_path: Path | None = None
    if cli_arg:
        path = Path(cli_arg).expanduser().resolve()
        if not path.exists():
            raise ConfigError(f"--config file not found: {path}")
        explicit_path = path
    elif "CALIBPIPE_CONFIG" in os.environ:
        env_val = os.environ["CALIBPIPE_CONFIG"]
        if env_val:
            path = Path(env_val).expanduser().resolve()
            if not path.exists():
                raise ConfigError(f"$CALIBPIPE_CONFIG file not found: {path}")
            explicit_path = path

    if explicit_path:
        layers.append(explicit_path)
    else:
        user_cfgs = find_user_configs()
        layers.extend(user_cfgs)

        proj_cfg = find_project_config()
        if proj_cfg:
            layers.append(proj_cfg)

        # Fallback to repo root config.toml if no user or project config was found
        if not user_cfgs and not proj_cfg:
            repo_root_config = Path(__file__).resolve().parent.parent.parent / "config.toml"
            if repo_root_config.is_file():
                layers.append(repo_root_config.resolve())
            else:
                local_config = Path(__file__).resolve().parent / "config.toml"
                if local_config.is_file():
                    layers.append(local_config.resolve())

    # Deduplicate while preserving order
    unique_layers: list[Path] = []
    seen: set[Path] = set()
    for p in layers:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_layers.append(resolved)

    if not unique_layers:
        raise ConfigError(
            "No config file found. Pass --config=<path>, set $CALIBPIPE_CONFIG, "
            "or create config.toml (see config.example.toml for the schema)."
        )

    return unique_layers


def find_config_path(cli_arg: str | Path | None = None) -> Path:
    """Locate the primary TOML configuration file to use.

    Maintained for backward compatibility. Returns the highest-priority configuration
    file found, or raises ConfigError if none is found.

    Args:
        cli_arg: Optional explicit path provided by the caller.

    Returns:
        Resolved Path to the highest-priority config file.

    Raises:
        ConfigError: If an explicit or environment-provided path does not
            exist, or no usable config file can be found.
    """
    layers = find_config_layers(cli_arg)
    return layers[-1]


def load_config(
    path: str | Path | Sequence[str | Path] | None = None,
    *,
    site_config: str | Path | None = None,
    no_site_config: bool = False,
) -> CalibpipeConfig:
    """Parse one or more TOML configuration files into a typed CalibpipeConfig object.

    When multiple paths are provided (as a sequence), they are deep-merged in order,
    allowing user/project configs to override site defaults while inheriting shared settings.

    Args:
        path: A single path, sequence of paths to merge, or None (to discover layers automatically).
        site_config: Optional explicit site configuration path to prepend as the base layer.
        no_site_config: If True, do not automatically discover or include site configuration.

    Returns:
        Canonical typed configuration.

    Raises:
        ConfigError: If a file does not exist, cannot be parsed, or no config file is found.
    """
    if path is None:
        raw_layers: list[Path] = find_config_layers(include_site=not no_site_config)
    elif isinstance(path, (str, Path)):
        raw_layers = [Path(path)]
        if site_config is not None:
            raw_layers.insert(0, Path(site_config))
    else:
        raw_layers = [Path(p) for p in path]
        if site_config is not None:
            raw_layers.insert(0, Path(site_config))

    merged_data: dict[str, Any] = {}
    sources: dict[str, str] = {}
    resolved_layers: list[Path] = []
    for p in raw_layers:
        resolved = Path(p).expanduser().resolve()
        if not resolved.is_file():
            raise ConfigError(f"Config file does not exist: {resolved}")
        try:
            with open(resolved, "rb") as fd:
                data = tomllib.load(fd)
        except Exception as e:
            if isinstance(e, ConfigError):
                raise
            raise ConfigError(f"Failed to parse TOML config {resolved}: {e}") from e
        validate_config(data, resolved)
        record_sources(data, str(resolved), sources)
        merged_data = _deep_merge_dict(merged_data, data)
        resolved_layers.append(resolved)

    cfg = CalibpipeConfig.from_dict(merged_data, loaded_layers=resolved_layers)
    cfg.sources = sources
    return cfg


def load_merged_config(
    cli_arg: str | Path | None = None,
    *,
    include_site: bool = True,
) -> CalibpipeConfig:
    """Discover configuration layers and return the deep-merged CalibpipeConfig.

    Discovers files across the hierarchy:
    1. Site configuration (from $CALIBPIPE_SITE_CONFIG, /etc/calibpipe/config.toml, etc.)
    2. User configuration (~/.config/calibpipe/config.toml)
    3. Workspace configuration (./config.toml)
    4. Explicit CLI/env override (--config or $CALIBPIPE_CONFIG)

    Args:
        cli_arg: Optional explicit configuration path from CLI.
        include_site: Whether to search for and include site-level configuration.

    Returns:
        Deep-merged CalibpipeConfig with loaded_layers populated.

    Raises:
        ConfigError: If an explicit config file does not exist, or if no config
            file is found across all layers.
    """
    layers = find_config_layers(cli_arg, include_site=include_site)
    return load_config(layers)


def record_sources(data, source, result, prefix=""):
    for key, value in data.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            record_sources(value, source, result, name)
        else:
            result[name] = source

def _config(config):
    return (
        config
        if isinstance(config, CalibpipeConfig)
        else CalibpipeConfig.from_dict(config)
    )


def format_config_overview(
    config, env_name=None, config_path=None, config_layers=None, profile=None
):
    cfg = _config(config)
    options = resolve_run_options(cfg, RunRequest(profile=profile, env=env_name))
    selected = options.profile
    lines = [
        "calibpipe Configuration Overview",
        "Configuration Layers (lowest to highest priority):",
    ]
    lines.extend(
        f"  {p}"
        for p in (
            config_layers or cfg.loaded_layers or ([config_path] if config_path else [])
        )
    )
    lines.extend(
        [
            f"Default Env: {cfg.default_env}",
            f"Default Profile: {cfg.default_profile or '(none)'}",
            f"Selected Env: {options.env_name}",
            f"Selected Profile: {selected or '(none)'}",
        ]
    )

    def display(section, values, defaults):
        lines.append(f"\n{section}:")
        for key, value in values.items():
            if section in {"Run", "Paths", "Batch", "Environment"}:
                profkey = f"profiles.{selected}.{key}" if selected else ""
                prof = cfg.profiles.get(selected)
                use_profile = prof is not None and getattr(prof, key, None) is not None
                path = profkey if use_profile else f"{defaults}.{key}"
            else:
                path = f"{defaults}.{key}"
            origin = cfg.sources.get(path, "built-in default or derived")
            if key == "env_name" and env_name:
                origin = "CLI --env"
            lines.append(f"  {key} = {value!r}  [source: {origin}]")

    effective_paths = replace(cfg.paths)
    for key in (
        "workspaces",
        "scipipe_rootdir",
        "scipipe_logdir",
        "casarundata",
        "obscaldir",
    ):
        value = getattr(options, key, None)
        if value is not None:
            setattr(effective_paths, key, value)
    display("Paths", asdict(effective_paths), "paths")
    display("Site", asdict(cfg.site), "site")
    if options.env_name in cfg.envs:
        env = resolve_env(
            cfg,
            options.env_name,
            casa_root=options.casa_root,
            heuristics_dir=options.heuristics_dir,
            pixi_dir=options.pixi_dir,
            pixi_env=options.pixi_env,
            branch=options.branch,
            workspaces=options.workspaces,
        )
        display("Environment", asdict(env), f"envs.{options.env_name}")
    display("Run", asdict(options), "run")
    if not options.interactive:
        batch = resolve_batch_options(
            cfg, SubmitRequest(profile=selected, env=env_name, pipefile="<inspection>")
        )
        display(
            "Batch",
            {key: value for key, value in asdict(batch).items() if key != "pipefile"},
            "batch",
        )
    return "\n".join(lines)


def format_profile_list(config):
    cfg = _config(config)
    return "Available profiles:\n" + "\n".join(
        f"  {name}" for name in sorted(cfg.profiles)
    )


def format_profile_details(config, name):
    cfg = _config(config)
    if name not in cfg.profiles:
        raise ConfigError(
            f"Unknown profile '{name}'. Available profiles: {', '.join(sorted(cfg.profiles))}"
        )
    return f"Profile: {name}\n" + "\n".join(
        f"  {key}: {value}"
        for key, value in asdict(cfg.profiles[name]).items()
        if value is not None
    )

def resolve_env(
    config: dict[str, Any],
    env_name: str | None = None,
    *,
    casa_root: str | None = None,
    heuristics_dir: str | None = None,
    pixi_dir: str | None = None,
    pixi_env: str | None = None,
    branch: str | None = None,
    workspaces: Sequence[str | Path] | None = None,
) -> EnvSpec:
    """Resolve a named CASA environment from the loaded config.

    Args:
        config: Parsed TOML configuration dictionary.
        env_name: Explicit environment name. If omitted, `default_env` is used.
        casa_root: Optional override for CASA installation root.
        heuristics_dir: Optional override for pipeline heuristics directory.
        pixi_dir: Optional override for Pixi workspace directory.
        pixi_env: Optional override for Pixi environment name.
        branch: Optional override for pipeline branch name.
        workspaces: Optional override for workspace search roots.

    Returns:
        Resolved environment specification.

    Raises:
        ConfigError: If the config has no environments, the named environment
            is missing, or required keys are absent.
    """
    if isinstance(config, CalibpipeConfig):
        config = asdict(config)
    envs = config.get("envs", {})
    if not envs:
        raise ConfigError("Config file has no [envs.*] tables defined.")

    name = env_name or config.get("default_env")
    if not name and config.get("default_profile"):
        dp = config.get("default_profile")
        profs = config.get("profiles", {})
        if dp in profs and profs[dp].get("env"):
            name = profs[dp]["env"]
        elif dp in envs:
            name = dp
    if not name:
        raise ConfigError(
            "No --env given and config file has no default_env or default_profile set."
        )
    if name not in envs:
        available = ", ".join(sorted(envs))
        raise ConfigError(f"No [envs.{name}] in config file. Available: {available}")

    table = dict(envs[name])
    if casa_root is not None:
        table["casa_root"] = casa_root
    if heuristics_dir is not None:
        table["heuristics_dir"] = heuristics_dir
    if pixi_dir is not None:
        table["pixi_dir"] = pixi_dir
    if pixi_env is not None:
        table["pixi_env"] = pixi_env
    if branch is not None:
        table["branch"] = branch

    casa_root_key = "casa_root" if table.get("casa_root") else None
    if not casa_root_key and not table.get("pixi_dir"):
        raise ConfigError(
            f"[envs.{name}] must specify either 'casa_root' (monolithic CASA) "
            f"or 'pixi_dir' (Pixi modular CASA)."
        )

    effective_workspaces = (
        workspaces
        if workspaces is not None
        else config.get("paths", {}).get("workspaces", [])
    )
    resolved_casa_root = str(table.get(casa_root_key, "")) if casa_root_key else ""
    if resolved_casa_root and effective_workspaces:
        resolved_casa = resolve_workspace_path(
            resolved_casa_root.rstrip("/"), effective_workspaces
        )
        if _safe_exists(resolved_casa):
            resolved_casa_root = str(resolved_casa)

    resolved_pixi_dir = table.get("pixi_dir")
    if resolved_pixi_dir and effective_workspaces:
        resolved_pixi = resolve_workspace_path(
            resolved_pixi_dir.rstrip("/"), effective_workspaces
        )
        if _safe_exists(resolved_pixi):
            resolved_pixi_dir = str(resolved_pixi)

    resolved_heuristics = table.get("heuristics_dir")
    if resolved_heuristics and effective_workspaces and "{" not in resolved_heuristics:
        resolved_heur = resolve_workspace_path(
            resolved_heuristics.rstrip("/"), effective_workspaces
        )
        if _safe_exists(resolved_heur):
            resolved_heuristics = str(resolved_heur)

    return EnvSpec(
        name=name,
        casa_root=resolved_casa_root,
        branch=table.get("branch", name),
        heuristics_dir=resolved_heuristics,
        pixi_dir=resolved_pixi_dir,
        pixi_env=table.get("pixi_env", "default"),
        extra_vars={
            **table.get("extra_vars", {}),
            **{
                k: str(v)
                for k, v in table.items()
                if k
                not in (
                    "name",
                    "casa_root",
                    "casa_dir",
                    "casa_ver",
                    "branch",
                    "heuristics_dir",
                    "heuristics_root",
                    "pipe_dir",
                    "pipe_ver",
                    "pl_ver",
                    "pixi_dir",
                    "pixi_env",
                    "extra_vars",
                )
            },
        },
    )


def build_environment(
    config: dict[str, Any],
    env_spec: EnvSpec | dict[str, Any],
    subdir: str | None = None,
    validate_paths: bool = True,
) -> dict[str, str]:
    """Build the process environment for a CASA + pipeline run.

    Args:
        config: Parsed TOML configuration dictionary.
        env_spec: Resolved environment spec or equivalent dictionary.
        subdir: Optional extra path component appended to `SCIPIPE_ROOTDIR`.
        validate_paths: If True, checks that key paths exist on disk and warns
            (or raises ConfigError if strict_paths is enabled) if unreachable.

    Returns:
        Environment mapping ready to inject into subprocesses.
    """
    if isinstance(config, CalibpipeConfig):
        config = {**asdict(config), "site": tool_settings(config)}
        site = {**asdict(SiteConfig()), **config.get("site", {})}
    else:
        cfg_obj = CalibpipeConfig.from_dict(config)
        site = {**tool_settings(cfg_obj), **config.get("site", {})}
    if isinstance(env_spec, dict):
        spec = EnvSpec.from_dict(env_spec)
    else:
        spec = env_spec

    paths = config.get("paths", {})
    workspaces = paths.get("workspaces", [])
    user = os.environ.get("USER", "")

    branch = spec.branch
    env = os.environ.copy()
    env["PIPE_BRANCH"] = branch

    if spec.is_pixi:
        pixi_dir_val = (spec.pixi_dir or "").rstrip("/")
        if workspaces:
            p = resolve_workspace_path(pixi_dir_val, workspaces)
            pixi_dir = str(p.expanduser().resolve() if p.exists() else p.expanduser())
        else:
            pixi_dir = os.path.expanduser(pixi_dir_val)
        env["PIXI_DIR"] = pixi_dir
        env["PIXI_ENV"] = spec.pixi_env
        env["CASA_ROOT"] = pixi_dir
        env["PIXI_FROZEN"] = os.environ.get("PIXI_FROZEN", "true")
        env["PIXI_NO_INSTALL"] = os.environ.get("PIXI_NO_INSTALL", "true")

        if spec.heuristics_dir:
            heur_raw = spec.heuristics_dir.format(pixi_dir=pixi_dir, branch=branch)
            if workspaces:
                p = resolve_workspace_path(heur_raw, workspaces)
                heuristics = str(
                    p.expanduser().resolve() if p.exists() else p.expanduser()
                )
            else:
                heuristics = os.path.expanduser(heur_raw)
        else:
            heuristics_root = paths.get("heuristics_root")
            if heuristics_root:
                heuristics = os.path.expanduser(
                    f"{heuristics_root.rstrip('/')}/{branch}"
                )
            else:
                heuristics = pixi_dir

        env["SCIPIPE_HEURISTICS"] = heuristics
        if (Path(heuristics) / "pipeline" / "recipes").is_dir():
            env["SCIPIPE_SCRIPTDIR"] = str(Path(heuristics) / "pipeline" / "recipes")
        elif (Path(heuristics) / "recipes").is_dir():
            env["SCIPIPE_SCRIPTDIR"] = str(Path(heuristics) / "recipes")
        else:
            env["SCIPIPE_SCRIPTDIR"] = f"{heuristics}/pipeline/recipes"
    else:
        casa_root_val = spec.casa_root.rstrip("/")
        if workspaces:
            p = resolve_workspace_path(casa_root_val, workspaces)
            casa_root = str(p.expanduser().resolve() if p.exists() else p.expanduser())
        else:
            casa_root = os.path.expanduser(casa_root_val)
        casa_path = f"{casa_root}/bin"
        env["CASA_ROOT"] = casa_root
        env["CASA_PATH"] = casa_path

        if spec.heuristics_dir:
            heur_raw = spec.heuristics_dir.format(casa_root=casa_root, branch=branch)
            if workspaces:
                p = resolve_workspace_path(heur_raw, workspaces)
                heuristics = str(
                    p.expanduser().resolve() if p.exists() else p.expanduser()
                )
            else:
                heuristics = os.path.expanduser(heur_raw)
        else:
            heuristics_root = paths.get("heuristics_root")
            if heuristics_root:
                heuristics = os.path.expanduser(
                    f"{heuristics_root.rstrip('/')}/{branch}"
                )
            else:
                heuristics = str(Path(__file__).resolve().parent.parent.parent)

        env["SCIPIPE_HEURISTICS"] = heuristics
        if (Path(heuristics) / "pipeline" / "recipes").is_dir():
            env["SCIPIPE_SCRIPTDIR"] = str(Path(heuristics) / "pipeline" / "recipes")
        elif (Path(heuristics) / "recipes").is_dir():
            env["SCIPIPE_SCRIPTDIR"] = str(Path(heuristics) / "recipes")
        else:
            env["SCIPIPE_SCRIPTDIR"] = f"{heuristics}/pipeline/recipes"

    for var, key, required in (
        ("SCIPIPE_ROOTDIR", "scipipe_rootdir", True),
        ("SCIPIPE_LOGDIR", "scipipe_logdir", True),
    ):
        template = paths.get(key)
        if not template:
            if required:
                raise ConfigError(f"[paths].{key} is required")
            continue
        if isinstance(template, (list, tuple)):
            template = resolve_first_existing_path(
                template, workspaces=workspaces, user=user
            )
        resolved = os.path.expanduser(template.format(user=user))
        if var == "SCIPIPE_ROOTDIR" and subdir:
            resolved = str(Path(resolved) / subdir)
        try:
            os.makedirs(resolved, exist_ok=True)
        except OSError:
            pass
        env[var] = resolved

    # Discover casarundata / casadata / measurespath
    casadata_val = paths.get("casarundata")
    if casadata_val:
        if isinstance(casadata_val, (list, tuple)):
            casadata_val = resolve_first_existing_path(
                casadata_val, workspaces=workspaces
            )
        if casadata_val:
            p = resolve_workspace_path(casadata_val, workspaces)
            resolved_casadata = str(p.resolve() if p.exists() else p.expanduser())
            env["CASADATA"] = resolved_casadata
            env["CASARUNDATA"] = resolved_casadata
    elif workspaces and "CASADATA" not in env:
        for ws in workspaces:
            for cand_name in ("casarundata", "casa-data", "data"):
                cand = Path(ws).expanduser() / cand_name
                if cand.is_dir() and (cand / "geodetic").is_dir():
                    resolved_cand = str(cand.resolve())
                    env["CASADATA"] = resolved_cand
                    env["CASARUNDATA"] = resolved_cand
                    break
            if "CASADATA" in env:
                break

    env["JAVA_HOME"] = site["java_home"]
    env["ACSDATA"] = site["acsdata"]
    env["ACSROOT"] = site["pmr_home"]
    env["DATAPACKER_HOME"] = site["datapacker_home"]
    env["JARSDIR"] = f"{site['datapacker_home']}/lib"
    env["FLUX_SERVICE_URL"] = site["flux_service_url"]
    env["FLUX_SERVICE_URL_BACKUP"] = site["flux_service_url_backup"]

    pmr_bin = f"{site['pmr_home']}/bin"
    path_parts = env.get("PATH", "").split(":")
    prepend_paths = [pmr_bin]
    if not spec.is_pixi:
        prepend_paths.insert(0, f"{casa_root}/bin")
    else:
        pixi_bin = site.get("pixi_bin")
        if pixi_bin and Path(pixi_bin).is_file():
            pixi_bin_dir = str(Path(pixi_bin).parent)
            if pixi_bin_dir not in prepend_paths:
                prepend_paths.insert(0, pixi_bin_dir)

    for prepend in prepend_paths:
        if prepend in path_parts:
            path_parts.remove(prepend)
        path_parts.insert(0, prepend)
    env["PATH"] = ":".join(path_parts)

    env["MATPLOTLIBRC"] = str(Path.home() / ".casa" / "matplotlib")

    for k, v in spec.extra_vars.items():
        env[k] = v

    if validate_paths and not os.environ.get("CALIBPIPE_SKIP_PATH_CHECK"):
        strict = bool(site.get("strict_paths", False))
        check_paths(env, site_config=config.get("site", {}), strict=strict)

    return env


def check_paths(
    env: dict[str, str],
    site_config: dict[str, Any] | None = None,
    strict: bool = False,
    check_pmr: bool = False,
) -> list[str]:
    """Verify that key executable and support directories exist on disk.

    If any path is unreachable, issues an actionable warning (or raises ConfigError
    if strict=True), advising the user how to configure it in config.toml.

    Args:
        env: Resolved environment mapping from build_environment.
        site_config: Optional [site] table from config.toml.
        strict: If True, raises ConfigError on the first unreachable path instead of warning.
        check_pmr: If True, also checks PMR, datapacker, acsdata, and Java paths required
            only for legacy pipelineMakeRequest MOUS workflows.

    Returns:
        List of warning messages for unreachable paths.

    Raises:
        ConfigError: If strict is True and any checked path does not exist.
    """
    site = site_config or {}
    is_pixi = bool(env.get("PIXI_DIR"))

    checks: list[tuple[str, str, str]] = []
    if is_pixi:
        pixi_dir = env.get("PIXI_DIR", "")
        checks.append(
            (
                "pixi_dir",
                pixi_dir,
                (
                    "Please configure [envs.<name>].pixi_dir in config.toml "
                    "to point to a valid Pixi environment directory."
                ),
            )
        )
        pixi_bin = site.get("pixi_bin") or shutil.which("pixi")
        if pixi_bin:
            checks.append(
                (
                    "pixi_bin",
                    pixi_bin,
                    "Configured [site].pixi_bin does not exist. Please check the path.",
                )
            )
        else:
            msg = (
                "Executable 'pixi' not found in PATH or [site].pixi_bin.\n"
                "  -> Please install Pixi or set [site].pixi_bin in config.toml."
            )
            if strict:
                raise ConfigError(msg)
            print(f"WARNING: [calibpipe] {msg}", file=sys.stderr)
    else:
        checks.append(
            (
                "CASA_ROOT",
                env.get("CASA_ROOT", ""),
                "Please configure [envs.<name>].casa_root in config.toml to point to a valid CASA installation.",
            )
        )

    if check_pmr:
        checks.extend(
            [
                (
                    "pmr_home",
                    env.get("ACSROOT", ""),
                    (
                        "Please configure [site].pmr_home in config.toml "
                        "(or copy from notes/config.internal.example.toml for NAASC cluster)."
                    ),
                ),
                (
                    "datapacker_home",
                    env.get("DATAPACKER_HOME", ""),
                    "Please configure [site].datapacker_home in config.toml.",
                ),
                (
                    "acsdata",
                    env.get("ACSDATA", ""),
                    "Please configure [site].acsdata in config.toml.",
                ),
                (
                    "java_home",
                    env.get("JAVA_HOME", ""),
                    "Please configure [site].java_home or set $JAVA_HOME in your environment.",
                ),
            ]
        )

    warnings: list[str] = []
    for label, path_str, hint in checks:
        if not path_str:
            continue
        p = Path(path_str).expanduser()
        if not p.exists():
            msg = f"Path for '{label}' does not exist or is not reachable: {path_str}\n  -> {hint}"
            warnings.append(msg)
            if strict:
                raise ConfigError(msg)
            print(f"WARNING: [calibpipe] {msg}", file=sys.stderr)

    return warnings


def format_shell_exports(
    env: dict[str, str],
    export: bool = True,
    only_keys: list[str] | None = None,
) -> str:
    """Format environment dictionary as shell statements.

    If export=True, produces `export KEY='val'`.
    If export=False, produces `KEY=val` for plain inspection.

    Args:
        env: Environment mapping.
        export: Whether to emit `export` statements.
        only_keys: Optional ordered subset of keys to print.

    Returns:
        Newline-delimited shell statements.
    """
    keys = only_keys or [
        "CASA_ROOT",
        "CASA_PATH",
        "PIPE_BRANCH",
        "PIXI_DIR",
        "PIXI_ENV",
        "PIXI_FROZEN",
        "PIXI_NO_INSTALL",
        "SCIPIPE_HEURISTICS",
        "SCIPIPE_SCRIPTDIR",
        "SCIPIPE_ROOTDIR",
        "SCIPIPE_LOGDIR",
        "JAVA_HOME",
        "ACSDATA",
        "ACSROOT",
        "DATAPACKER_HOME",
        "JARSDIR",
        "FLUX_SERVICE_URL",
        "FLUX_SERVICE_URL_BACKUP",
        "PATH",
        "MATPLOTLIBRC",
    ]
    lines = []
    for k in keys:
        if k in env:
            val = env[k]
            if export:
                lines.append(f"export {k}={shlex.quote(val)}")
            else:
                lines.append(f"{k}={val}")
    return "\n".join(lines)


_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_SNAPSHOT_PATH_KEYS: dict[str, tuple[str, ...]] = {
    "paths": (
        "workspaces",
        "heuristics_root",
        "pixi_bin",
        "scipipe_rootdir",
        "scipipe_logdir",
        "casarundata",
        "obscaldir",
        "pickle_dir",
        "aUdir",
        "validation_dir",
        "datapath",
        "rundata",
    ),
    "site": ("pmr_home", "datapacker_home", "acsdata", "java_home"),
    "run": (
        "script",
        "ppr",
        "rawdata",
        "cont_dat",
        "jyperk_csv",
        "parameter_list",
        "casarundata",
        "obscaldir",
        "vis",
        "datapath",
        "rundata",
        "ancillary",
        "flag_dir",
    ),
}


def _toml_key(key: str) -> str:
    return key if _BARE_TOML_KEY.match(key) else json.dumps(str(key))


def _toml_val(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigError(f"Cannot serialize non-finite float to TOML: {value}")
        return repr(value)
    if isinstance(value, (str, Path)):
        return json.dumps(str(value))
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_val(item) for item in value) + "]"
    if isinstance(value, dict):
        items = ", ".join(
            f"{_toml_key(str(k))} = {_toml_val(v)}"
            for k, v in value.items()
            if v is not None
        )
        return "{" + items + "}"
    raise ConfigError(f"Unsupported TOML value type: {type(value).__name__}")


def dump_toml(data: dict[str, Any]) -> str:
    """Serialize a nested configuration dictionary into valid TOML text."""
    lines: list[str] = []

    def _emit(table: dict[str, Any], prefix: tuple[str, ...]) -> None:
        scalars = [
            (str(k), v) for k, v in table.items() if v is not None and not isinstance(v, dict)
        ]
        subtables = [(str(k), v) for k, v in table.items() if isinstance(v, dict)]
        if prefix and (scalars or not subtables):
            if lines:
                lines.append("")
            lines.append(f"[{'.'.join(_toml_key(part) for part in prefix)}]")
        for k, v in scalars:
            lines.append(f"{_toml_key(k)} = {_toml_val(v)}")
        for k, sub in subtables:
            _emit(sub, prefix + (k,))

    _emit(data, ())
    return "\n".join(lines) + "\n"


def _freeze_value(
    value: Any,
    *,
    workspaces: Sequence[str | Path] = (),
    cwd: Path | None = None,
    resolve_in_ws: bool = False,
    preserve_templates: bool = True,
) -> Any:
    """Absolutize path strings, lists, or ancillary dicts relative to `cwd`/workspaces."""
    if isinstance(value, (list, tuple)):
        return [
            _freeze_value(
                item,
                workspaces=workspaces,
                cwd=cwd,
                resolve_in_ws=resolve_in_ws,
                preserve_templates=preserve_templates,
            )
            for item in value
        ]
    if isinstance(value, dict):
        copied = dict(value)
        for key in ("path", "src"):
            if key in copied:
                copied[key] = _freeze_value(
                    copied[key],
                    workspaces=workspaces,
                    cwd=cwd,
                    resolve_in_ws=resolve_in_ws,
                    preserve_templates=preserve_templates,
                )
        return copied
    if not isinstance(value, (str, Path)):
        return value
    text = str(value).strip()
    if not text or (preserve_templates and "{" in text and "}" in text):
        return text
    base = cwd if cwd is not None else Path.cwd()
    if resolve_in_ws:
        resolved = Path(resolve_workspace_path(text, workspaces)).expanduser()
        if resolved.is_absolute():
            return str(resolved)
        return str((resolved if _safe_exists(resolved) else base / resolved).resolve())
    expanded = Path(text).expanduser()
    return str(expanded if expanded.is_absolute() else (base / expanded).resolve())


def freeze_recipe_path(
    recipe: str | None,
    *,
    workspaces: Sequence[str | Path] = (),
    cwd: Path | None = None,
) -> str | None:
    """Absolutize an explicit/local XML recipe path while leaving built-in names untouched."""
    if recipe is None:
        return None
    text = str(recipe).strip()
    if not text or ("/" not in text and not text.endswith(".xml")):
        return text
    base = cwd if cwd is not None else Path.cwd()
    resolved = Path(resolve_workspace_path(text, workspaces)).expanduser()
    if resolved.is_absolute() and _safe_exists(resolved):
        return str(resolved)
    candidate = resolved if _safe_exists(resolved) else (base / resolved)
    if _safe_exists(candidate) or "/" in text or text.startswith("."):
        return str(candidate.resolve())
    return text


def _freeze_ancillary(
    value: Any,
    *,
    workspaces: Sequence[str | Path] = (),
    cwd: Path,
    resolve_in_ws: bool = False,
) -> Any:
    """Absolutize only the source path in ancillary entries, leaving relative destinations intact."""
    if not isinstance(value, (list, tuple)):
        return value
    frozen_items: list[Any] = []
    for item in value:
        if isinstance(item, (list, tuple)):
            if item:
                src = _freeze_value(
                    item[0], workspaces=workspaces, cwd=cwd, resolve_in_ws=resolve_in_ws
                )
                frozen_items.append([src, *item[1:]])
            else:
                frozen_items.append(list(item))
        elif isinstance(item, dict):
            frozen_items.append(
                _freeze_value(item, workspaces=workspaces, cwd=cwd, resolve_in_ws=resolve_in_ws)
            )
        elif isinstance(item, str) and ":" in item and "://" not in item:
            src_part, dst_part = item.split(":", 1)
            frozen_src = _freeze_value(
                src_part, workspaces=workspaces, cwd=cwd, resolve_in_ws=resolve_in_ws
            )
            frozen_items.append(f"{frozen_src}:{dst_part}")
        else:
            frozen_items.append(
                _freeze_value(item, workspaces=workspaces, cwd=cwd, resolve_in_ws=resolve_in_ws)
            )
    return frozen_items


def _freeze_table(
    raw: dict[str, Any],
    path_keys: Sequence[str],
    *,
    workspaces: Sequence[str | Path] = (),
    cwd: Path,
    resolve_in_ws: bool = False,
    freeze_workdir: bool = False,
) -> dict[str, Any]:
    """Return a `None`-free dict copy with configured path fields absolutized."""
    table = {k: v for k, v in raw.items() if v is not None}
    for key in path_keys:
        if key in table:
            if key == "ancillary":
                table[key] = _freeze_ancillary(
                    table[key],
                    workspaces=workspaces,
                    cwd=cwd,
                    resolve_in_ws=resolve_in_ws,
                )
            else:
                table[key] = _freeze_value(
                    table[key],
                    workspaces=workspaces,
                    cwd=cwd,
                    resolve_in_ws=resolve_in_ws,
                )
    if freeze_workdir and "workdir" in table and not table.get("scipipe_rootdir"):
        table["workdir"] = _freeze_value(table["workdir"], cwd=cwd)
    if isinstance(table.get("recipe"), str):
        table["recipe"] = freeze_recipe_path(
            table["recipe"], workspaces=workspaces, cwd=cwd
        )
    return table


def build_batch_snapshot(
    config: CalibpipeConfig,
    options: ResolvedRunOptions,
    *,
    cwd: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a self-contained, path-frozen TOML config dict for submitted batch jobs."""
    base_cwd = Path(cwd).expanduser().resolve() if cwd is not None else Path.cwd().resolve()
    config_cwd = Path.cwd().resolve()

    paths_dict = _freeze_table(
        asdict(config.paths), _SNAPSHOT_PATH_KEYS["paths"], cwd=base_cwd
    )
    site_dict = _freeze_table(
        asdict(config.site), _SNAPSHOT_PATH_KEYS["site"], cwd=base_cwd
    )

    profile_name = options.profile
    prof_raw: dict[str, Any] | None = None
    if profile_name and profile_name in config.profiles:
        prof_raw = asdict(config.profiles[profile_name])
        if overrides:
            valid = {f.name for f in fields(ProfileConfig)}
            prof_raw.update(
                {k: v for k, v in overrides.items() if v is not None and k in valid}
            )
        if prof_raw.get("workspaces") is not None:
            prof_raw["workspaces"] = _freeze_value(prof_raw["workspaces"], cwd=base_cwd)

    effective_workspaces = (
        prof_raw["workspaces"]
        if prof_raw is not None and prof_raw.get("workspaces")
        else paths_dict.get("workspaces", [])
    )
    effective_rootdir = (
        prof_raw.get("scipipe_rootdir") if prof_raw is not None else None
    ) or paths_dict.get("scipipe_rootdir")

    spec = resolve_env(
        config,
        options.env_name,
        casa_root=options.casa_root,
        heuristics_dir=options.heuristics_dir,
        pixi_dir=options.pixi_dir,
        pixi_env=options.pixi_env,
        branch=options.branch,
        workspaces=effective_workspaces,
    )
    raw_env = {k: v for k, v in asdict(spec).items() if v is not None and k != "name"}
    if not raw_env.get("extra_vars"):
        raw_env.pop("extra_vars", None)
    for key in ("casa_root", "pixi_dir"):
        if raw_env.get(key):
            raw_env[key] = _freeze_value(
                raw_env[key],
                workspaces=effective_workspaces,
                cwd=config_cwd,
                resolve_in_ws=True,
                preserve_templates=False,
            )
    if raw_env.get("heuristics_dir"):
        formatted_heur = str(raw_env["heuristics_dir"]).format_map(
            {
                "casa_root": raw_env.get("casa_root") or "",
                "pixi_dir": raw_env.get("pixi_dir") or "",
                "branch": raw_env.get("branch") or "",
            }
        )
        raw_env["heuristics_dir"] = _freeze_value(
            formatted_heur,
            workspaces=effective_workspaces,
            cwd=config_cwd,
            resolve_in_ws=True,
            preserve_templates=False,
        )

    freeze_wd = not bool(effective_rootdir)
    run_dict = _freeze_table(
        asdict(config.run),
        _SNAPSHOT_PATH_KEYS["run"],
        workspaces=effective_workspaces,
        cwd=base_cwd,
        resolve_in_ws=True,
        freeze_workdir=freeze_wd,
    )

    snapshot: dict[str, Any] = {"default_env": options.env_name}
    if prof_raw is not None and profile_name:
        snapshot["default_profile"] = profile_name
        prof_dict = _freeze_table(
            prof_raw,
            (*_SNAPSHOT_PATH_KEYS["run"], *_SNAPSHOT_PATH_KEYS["paths"], "logdir", "chdir"),
            workspaces=effective_workspaces,
            cwd=base_cwd,
            resolve_in_ws=True,
            freeze_workdir=freeze_wd,
        )
        profiles_out = {profile_name: prof_dict}
    else:
        profiles_out = {}
        if overrides:
            valid = {f.name for f in fields(RunConfig)}
            extra = {k: v for k, v in overrides.items() if v is not None and k in valid}
            if extra:
                run_dict.update(
                    _freeze_table(
                        extra,
                        _SNAPSHOT_PATH_KEYS["run"],
                        workspaces=effective_workspaces,
                        cwd=base_cwd,
                        resolve_in_ws=True,
                        freeze_workdir=freeze_wd,
                    )
                )

    if paths_dict:
        snapshot["paths"] = paths_dict
    if site_dict:
        snapshot["site"] = site_dict
    if run_dict:
        snapshot["run"] = run_dict
    snapshot["envs"] = {options.env_name: raw_env}
    if profiles_out:
        snapshot["profiles"] = profiles_out
    return snapshot
