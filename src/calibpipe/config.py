"""Configuration loading and CASA/pipeline environment construction for calibpipe.

Replaces the legacy env_core2.sh and personal pipeline_env_<name>.sh files with
structured, strongly-typed TOML configuration loading.
"""

from __future__ import annotations

import functools
import os
import shlex
import shutil
import sys
from collections.abc import Sequence
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

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


@dataclass
class PathsConfig:
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
    casadata: str | None = None
    datapath: list[str] = field(default_factory=list)
    rundata: list[str] = field(default_factory=list)
    pmr_home: str | None = None
    datapacker_home: str | None = None
    acsdata: str | None = None
    java_home: str | None = None
    pixi_bin: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.scipipe_rootdir, (list, tuple)):
            self.scipipe_rootdir = resolve_first_existing_path(
                self.scipipe_rootdir, workspaces=self.workspaces
            )
        elif self.scipipe_rootdir is None:
            self.scipipe_rootdir = ""
        else:
            self.scipipe_rootdir = str(self.scipipe_rootdir)

        if isinstance(self.scipipe_logdir, (list, tuple)):
            self.scipipe_logdir = resolve_first_existing_path(
                self.scipipe_logdir, workspaces=self.workspaces
            )
        elif self.scipipe_logdir is None:
            self.scipipe_logdir = ""
        else:
            self.scipipe_logdir = str(self.scipipe_logdir)

        raw_casarundata = (
            self.casarundata
            if self.casarundata is not None
            else (
                self.casadata
                if self.casadata is not None
                else (self.rundata if self.rundata else None)
            )
        )
        if isinstance(raw_casarundata, (list, tuple)):
            resolved_casarundata = (
                resolve_first_existing_path(raw_casarundata, workspaces=self.workspaces) or None
            )
        elif raw_casarundata is not None:
            resolved_casarundata = str(raw_casarundata).strip() or None
        else:
            resolved_casarundata = None

        self.casarundata = resolved_casarundata
        self.casadata = resolved_casarundata
        if not self.rundata and resolved_casarundata:
            self.rundata = [resolved_casarundata]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PathsConfig:
        workspaces = list(data.get("workspaces", []) or [])
        raw_casarundata = (
            data.get("casarundata")
            if "casarundata" in data and data.get("casarundata") is not None
            else (
                data.get("casadata")
                if "casadata" in data and data.get("casadata") is not None
                else data.get("rundata")
            )
        )
        resolved_casarundata = (
            resolve_first_existing_path(raw_casarundata, workspaces=workspaces)
            if raw_casarundata is not None
            else None
        )
        if resolved_casarundata == "":
            resolved_casarundata = None

        user_rundata = data.get("rundata")
        if user_rundata is not None:
            rundata_list = _list_or_empty(user_rundata)
        elif resolved_casarundata:
            rundata_list = [resolved_casarundata]
        else:
            rundata_list = []

        return cls(
            scipipe_rootdir=resolve_first_existing_path(
                data.get("scipipe_rootdir", ""), workspaces=workspaces
            ),
            scipipe_logdir=resolve_first_existing_path(
                data.get("scipipe_logdir", ""), workspaces=workspaces
            ),
            pickle_dir=data.get("pickle_dir"),
            obscaldir=data.get("obscaldir"),
            aUdir=data.get("aUdir"),
            validation_dir=data.get("validation_dir"),
            heuristics_root=(
                data.get("heuristics_root")
                or data.get("pipe_dir")
                or data.get("pipe_ver")
                or data.get("pl_ver")
            ),
            workspaces=workspaces,
            casarundata=resolved_casarundata,
            casadata=resolved_casarundata,
            datapath=_list_or_empty(data.get("datapath")),
            rundata=rundata_list,
            pmr_home=_str_or_none(data.get("pmr_home")),
            datapacker_home=_str_or_none(data.get("datapacker_home")),
            acsdata=_str_or_none(data.get("acsdata")),
            java_home=_str_or_none(data.get("java_home")),
            pixi_bin=_str_or_none(data.get("pixi_bin")),
        )


@dataclass
class SiteConfig:
    """Site, cluster, and external tooling configuration overrides."""

    submit_host: str | None = None
    java_home: str = field(
        default_factory=lambda: os.environ.get("JAVA_HOME", "/usr/lib/jvm/default-java")
    )
    pmr_home: str = "/opt/pipetools/latest"
    acsdata: str = "/opt/acsdata"
    datapacker_home: str = "/opt/datapacker/current"
    flux_service_url: str = "https://almascience.org/sc/flux"
    flux_service_url_backup: str = "https://asa.alma.cl/sc/flux"
    casa_enable_telemetry: bool = False
    use_custom_rcdir: bool = True
    strict_paths: bool = False
    pixi_bin: str | None = None
    log2term: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiteConfig:
        default_inst = cls()
        return cls(
            submit_host=data.get("submit_host", default_inst.submit_host),
            java_home=data.get("java_home", default_inst.java_home),
            pmr_home=data.get("pmr_home", default_inst.pmr_home),
            acsdata=data.get("acsdata", default_inst.acsdata),
            datapacker_home=data.get("datapacker_home", default_inst.datapacker_home),
            flux_service_url=data.get(
                "flux_service_url", default_inst.flux_service_url
            ),
            flux_service_url_backup=data.get(
                "flux_service_url_backup", default_inst.flux_service_url_backup
            ),
            casa_enable_telemetry=bool(
                data.get("casa_enable_telemetry", default_inst.casa_enable_telemetry)
            ),
            use_custom_rcdir=bool(
                data.get("use_custom_rcdir", default_inst.use_custom_rcdir)
            ),
            strict_paths=bool(data.get("strict_paths", default_inst.strict_paths)),
            pixi_bin=data.get("pixi_bin", default_inst.pixi_bin),
            log2term=bool(data.get("log2term", default_inst.log2term)),
        )


def _int_or_none(value: Any) -> int | None:
    """Return int(value) or None if value is None or empty string."""
    return None if value is None or value == "" else int(value)


def _float_or_none(value: Any) -> float | None:
    """Return float(value) or None if value is None or empty string."""
    return None if value is None or value == "" else float(value)


def _str_or_none(value: Any) -> str | None:
    """Return str(value) or None if value is None or empty string."""
    return None if value is None or value == "" else str(value)


@dataclass
class BatchConfig:
    """Batch cluster resource submission defaults (Slurm and HTCondor).

    Scheduler Compatibility:
      - Both Slurm & HTCondor:
          - scheduler: "slurm" or "htcondor".
          - partition / queue: Slurm partition (--partition) or HTCondor (+partition).
          - cores / ntasks: Slurm tasks (--ntasks) or HTCondor CPUs (request_cpus).
          - mem: Total memory in GB (Slurm: --mem; HTCondor: request_memory).
          - mem_per_cpu: Memory per CPU (Slurm: --mem-per-cpu; HTCondor: request_memory = cores × mem_per_cpu).
          - mail_type: Notification setting (Slurm: --mail-type; HTCondor: notification).
          - chdir: Working directory (Slurm: --chdir; HTCondor: initialdir).
          - nodelist: Pinned host (Slurm: --nodelist; HTCondor: TARGET.Machine == "<nodelist>").
          - logdir: Output directory for script records and scheduler logs.
          - dry_run: Preview submit scripts without submitting.
      - Slurm only (ignored by HTCondor):
          - node: Number of nodes (--nodes).
          - walltime: Job runtime limit (--time).
          - cpus_per_task: Logical CPUs/threads per task (--cpus-per-task; total allocated = ntasks × cpus_per_task).
          - ntasks_per_core: Tasks per physical silicon core (--ntasks-per-core; 1 to avoid HT sharing).
          - hint: Placement hint (--hint, e.g. "nomultithread").
          - distribution: Task distribution across nodes/sockets (--distribution).
          - no_requeue: Prevent silent job requeuing on failure (--no-requeue).
      - HTCondor only (ignored by Slurm):
          - requirements: Custom ClassAd requirements expression override.
    """

    # --- Shared [Slurm & HTCondor] ---
    partition: str = "plwg"  # Slurm: --partition; HTCondor: +partition (alias: queue)
    cores: int = 8  # Slurm: --ntasks; HTCondor: request_cpus (alias: ntasks)
    mem: int = 248  # Total RAM in GB (Slurm: --mem; HTCondor: request_memory)
    mem_per_cpu: str | None = None  # RAM per CPU (Slurm: --mem-per-cpu; HTCondor: scaled into request_memory)
    mail_type: str = "ALL"  # Slurm: --mail-type; HTCondor: notification
    chdir: str | None = None  # Slurm: --chdir; HTCondor: initialdir
    nodelist: str | None = None  # Slurm: --nodelist; HTCondor: TARGET.Machine
    scheduler: str = "slurm"  # Batch scheduler backend: "slurm" or "htcondor"
    dry_run: bool = False  # Simulate batch submission without submitting jobs
    logdir: str | None = None  # Directory for scheduler log and script output

    # --- Slurm-specific Directives [Slurm only] ---
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

    # --- HTCondor-specific Directives [HTCondor only] ---
    requirements: str | None = None  # HTCondor: requirements expression override

    def __init__(
        self,
        partition: str = "plwg",
        cores: int = 8,
        mem: int = 248,
        node: str = "1",
        mail_type: str = "ALL",
        walltime: str | None = None,
        nodelist: str | None = None,
        chdir: str | None = None,
        cpus_per_task: int | None = None,
        mem_per_cpu: str | None = None,
        hint: str | None = None,
        ntasks_per_core: int | None = None,
        distribution: str | None = None,
        no_requeue: bool = True,
        scheduler: str = "slurm",
        requirements: str | None = None,
        dry_run: bool = False,
        queue: str | None = None,
        logdir: str | None = None,
        ntasks: int | None = None,
        threads: int | None = None,
    ) -> None:
        self.partition = queue if queue is not None else partition
        if ntasks is not None:
            self.cores = ntasks
        else:
            self.cores = cores
        self.mem = mem
        self.node = node
        self.mail_type = mail_type
        self.walltime = walltime
        self.nodelist = nodelist
        self.chdir = chdir
        self.cpus_per_task = threads if threads is not None else cpus_per_task
        self.mem_per_cpu = mem_per_cpu
        self.hint = hint
        self.ntasks_per_core = ntasks_per_core
        self.distribution = distribution
        self.no_requeue = no_requeue
        self.scheduler = scheduler
        self.requirements = requirements
        self.dry_run = dry_run
        self.logdir = logdir

    @property
    def queue(self) -> str:
        """Backward-compatible alias for partition."""
        return self.partition

    @property
    def ntasks(self) -> int:
        """Slurm-native alias for cores."""
        return self.cores

    @property
    def threads(self) -> int | None:
        """Thread count alias for cpus_per_task."""
        return self.cpus_per_task

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchConfig:
        default_inst = cls()
        cores_val = data.get("cores")
        if cores_val is None:
            cores_val = data.get("ntasks", default_inst.cores)

        threads_val = data.get("cpus_per_task")
        if threads_val is None:
            threads_val = data.get("threads", default_inst.cpus_per_task)

        return cls(
            partition=str(
                data.get("partition", data.get("queue", default_inst.partition))
            ),
            cores=int(cores_val),
            mem=int(data.get("mem", default_inst.mem)),
            node=str(data.get("node", default_inst.node)),
            mail_type=str(data.get("mail_type", default_inst.mail_type)),
            walltime=data.get("walltime", default_inst.walltime),
            nodelist=data.get("nodelist", default_inst.nodelist),
            chdir=data.get("chdir", default_inst.chdir),
            cpus_per_task=_int_or_none(threads_val),
            mem_per_cpu=data.get("mem_per_cpu", default_inst.mem_per_cpu),
            hint=data.get("hint", default_inst.hint),
            ntasks_per_core=_int_or_none(
                data.get("ntasks_per_core", default_inst.ntasks_per_core)
            ),
            distribution=data.get("distribution", default_inst.distribution),
            no_requeue=bool(data.get("no_requeue", default_inst.no_requeue)),
            scheduler=str(data.get("scheduler", default_inst.scheduler)),
            requirements=data.get("requirements", default_inst.requirements),
            dry_run=bool(data.get("dry_run", default_inst.dry_run)),
            logdir=data.get("logdir", default_inst.logdir),
        )


@dataclass
class RunConfig:
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
    cont_dat: str | None = None
    jyperk_csv: str | None = None
    parameter_list: str | None = None
    ancillary: list[Any] = field(default_factory=list)
    datapath: list[str] = field(default_factory=list)
    rundata: list[str] = field(default_factory=list)
    dry_run: bool = False
    autoreload: bool = True
    nologger: bool = False
    logfile: str | None = None
    use_custom_rcdir: bool = True
    casa_enable_telemetry: bool = False

    def __init__(
        self,
        recipe: str = "calimage",
        nprocs: int = 8,
        loglevel: str = "debug",
        useresume: bool = False,
        symlink_shortcuts: bool = True,
        log2term: bool = False,
        verbose: bool = False,
        omp_num_threads: int | None = None,
        openblas_num_threads: int | None = None,
        omp_max_threads: int | None = None,
        mem_frac: float | None = None,
        oversubscribe: bool = True,
        bind_to: str | None = None,
        map_by: str | None = None,
        psrecord: bool = False,
        memstats: bool = False,
        pl_psrecord: bool = False,
        backup: bool = False,
        cont_dat: str | None = None,
        jyperk_csv: str | None = None,
        parameter_list: str | None = None,
        ancillary: list[Any] | None = None,
        datapath: list[str] | None = None,
        rundata: list[str] | None = None,
        dry_run: bool = False,
        autoreload: bool = True,
        ncores: int | None = None,
        nologger: bool = False,
        logfile: str | None = None,
        use_custom_rcdir: bool = True,
        casa_enable_telemetry: bool = False,
    ) -> None:
        self.recipe = recipe
        self.nprocs = ncores if ncores is not None else nprocs
        self.loglevel = loglevel
        self.useresume = useresume
        self.symlink_shortcuts = symlink_shortcuts
        self.log2term = log2term
        self.verbose = verbose
        self.omp_num_threads = omp_num_threads
        self.openblas_num_threads = openblas_num_threads
        self.omp_max_threads = omp_max_threads
        self.mem_frac = mem_frac
        self.oversubscribe = oversubscribe
        self.bind_to = bind_to
        self.map_by = map_by
        self.psrecord = psrecord
        self.memstats = memstats
        self.pl_psrecord = pl_psrecord
        self.backup = backup
        self.cont_dat = cont_dat
        self.jyperk_csv = jyperk_csv
        self.parameter_list = parameter_list
        self.ancillary = _ancillary_list(ancillary)
        self.datapath = list(datapath or [])
        self.rundata = list(rundata or [])
        self.dry_run = dry_run
        self.autoreload = autoreload
        self.nologger = nologger
        self.logfile = logfile
        self.use_custom_rcdir = use_custom_rcdir
        self.casa_enable_telemetry = casa_enable_telemetry

    @property
    def ncores(self) -> int:
        """Backward-compatible alias for nprocs."""
        return self.nprocs

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunConfig:
        default_inst = cls()
        nprocs_val = data.get("nprocs", data.get("ncores", default_inst.nprocs))
        nologger_val = data.get("nologger", data.get("noglogger", default_inst.nologger))
        return cls(
            recipe=str(data.get("recipe", default_inst.recipe)),
            nprocs=int(nprocs_val),
            loglevel=str(data.get("loglevel", default_inst.loglevel)),
            useresume=bool(data.get("useresume", default_inst.useresume)),
            symlink_shortcuts=bool(
                data.get("symlink_shortcuts", default_inst.symlink_shortcuts)
            ),
            log2term=bool(data.get("log2term", default_inst.log2term)),
            verbose=bool(data.get("verbose", default_inst.verbose)),
            omp_num_threads=_int_or_none(data.get("omp_num_threads")),
            openblas_num_threads=_int_or_none(data.get("openblas_num_threads")),
            omp_max_threads=_int_or_none(data.get("omp_max_threads")),
            mem_frac=_float_or_none(data.get("mem_frac")),
            oversubscribe=bool(data.get("oversubscribe", default_inst.oversubscribe)),
            bind_to=_str_or_none(data.get("bind_to")),
            map_by=_str_or_none(data.get("map_by")),
            psrecord=bool(data.get("psrecord", default_inst.psrecord)),
            memstats=bool(data.get("memstats", default_inst.memstats)),
            pl_psrecord=bool(data.get("pl_psrecord", default_inst.pl_psrecord)),
            backup=bool(data.get("backup", default_inst.backup)),
            cont_dat=_str_or_none(data.get("cont_dat")),
            jyperk_csv=_str_or_none(data.get("jyperk_csv")),
            parameter_list=_str_or_none(data.get("parameter_list")),
            ancillary=_ancillary_list(data.get("ancillary", default_inst.ancillary)),
            datapath=_list_or_empty(data.get("datapath", default_inst.datapath)),
            rundata=_list_or_empty(data.get("rundata", default_inst.rundata)),
            dry_run=bool(data.get("dry_run", default_inst.dry_run)),
            autoreload=bool(data.get("autoreload", default_inst.autoreload)),
            nologger=bool(nologger_val),
            logfile=_str_or_none(data.get("logfile")),
            use_custom_rcdir=bool(
                data.get("use_custom_rcdir", default_inst.use_custom_rcdir)
            ),
            casa_enable_telemetry=bool(
                data.get("casa_enable_telemetry", default_inst.casa_enable_telemetry)
            ),
        )


# Backward-compatible SITE_DEFAULTS dict populated from SiteConfig
SITE_DEFAULTS: dict[str, Any] = asdict(SiteConfig())


class ConfigError(Exception):
    """Raised when configuration file or required keys are invalid or missing."""


@dataclass
class EnvSpec:
    """Specification for a specific CASA + pipeline environment (monolithic or Pixi modular)."""

    name: str
    casa_root: str = ""
    branch: str = ""
    heuristics_dir: str | None = None
    pixi_dir: str | None = None
    pixi_env: str = "default"
    extra_vars: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate environment spec and set defaults."""
        if not self.branch:
            self.branch = self.name
        if not self.casa_root and not self.pixi_dir:
            raise ConfigError(
                f"[envs.{self.name}] must specify either 'casa_root' (monolithic CASA) "
                f"or 'pixi_dir' (Pixi modular CASA)."
            )

    @property
    def is_pixi(self) -> bool:
        """Return True if this environment is configured as a Pixi modular setup."""
        return bool(self.pixi_dir)


@dataclass
class ProfileConfig:
    """Named execution and batch resource profile."""

    env: str | None = None

    # Run options
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
    cont_dat: str | None = None
    jyperk_csv: str | None = None
    parameter_list: str | None = None
    ancillary: list[Any] = field(default_factory=list)
    workdir: str | None = None

    # Batch options - Shared [Slurm & HTCondor]
    partition: str | None = None  # Slurm: --partition; HTCondor: +partition (alias: queue)
    cores: int | None = None  # Slurm: --ntasks; HTCondor: request_cpus (alias: ntasks)
    mem: int | None = None  # Total RAM in GB (Slurm: --mem; HTCondor: request_memory)
    mem_per_cpu: str | None = None  # RAM per CPU (Slurm: --mem-per-cpu; HTCondor: scaled into request_memory)
    mail_type: str | None = None  # Slurm: --mail-type; HTCondor: notification
    chdir: str | None = None  # Slurm: --chdir; HTCondor: initialdir
    nodelist: str | None = None  # Slurm: --nodelist; HTCondor: TARGET.Machine
    logdir: str | None = None  # Output directory for script records and logs
    scheduler: str | None = None  # "slurm" or "htcondor"

    # Batch options - Slurm only
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

    # Batch options - HTCondor only
    requirements: str | None = None  # HTCondor: requirements expression override

    # Environment overrides
    casa_root: str | None = None
    heuristics_dir: str | None = None
    pixi_dir: str | None = None
    pixi_env: str | None = None
    branch: str | None = None

    # Paths overrides
    workspaces: list[str] | None = None
    scipipe_rootdir: str | None = None
    scipipe_logdir: str | None = None
    casarundata: str | None = None
    casadata: str | None = None
    pickle_dir: str | None = None
    obscaldir: str | None = None
    aUdir: str | None = None
    validation_dir: str | None = None

    # Execution targets & modes
    mous: str | None = None
    vis: list[str] = field(default_factory=list)
    procedure: str | None = None
    script: str | None = None
    cmd: str | None = None
    ppr: str | None = None
    vla: bool | None = None
    interactive: bool | None = None
    datapath: list[str] | None = None
    rundata: list[str] | None = None
    dry_run: bool | None = None
    autoreload: bool | None = None
    nologger: bool | None = None
    logfile: str | None = None
    use_custom_rcdir: bool | None = None
    casa_enable_telemetry: bool | None = None

    @property
    def threads(self) -> int | None:
        """Canonical thread count alias for cpus_per_task."""
        return self.cpus_per_task

    def __init__(self, **kwargs: Any) -> None:
        if "noglogger" in kwargs and "nologger" not in kwargs:
            kwargs["nologger"] = kwargs.pop("noglogger")
        if "queue" in kwargs and "partition" not in kwargs:
            kwargs["partition"] = kwargs.pop("queue")
        if "threads" in kwargs and "cpus_per_task" not in kwargs:
            kwargs["cpus_per_task"] = kwargs.pop("threads")
        if "ntasks" in kwargs and "cores" not in kwargs:
            kwargs["cores"] = kwargs.pop("ntasks")
        if "ncores" in kwargs and "nprocs" not in kwargs:
            kwargs["nprocs"] = kwargs.pop("ncores")
        if "work_path" in kwargs and "workdir" not in kwargs:
            kwargs["workdir"] = kwargs.pop("work_path")
        if "casa_ver" in kwargs and "casa_root" not in kwargs:
            kwargs["casa_root"] = kwargs.pop("casa_ver")
        if "casa_dir" in kwargs and "casa_root" not in kwargs:
            kwargs["casa_root"] = kwargs.pop("casa_dir")
        if "pl_ver" in kwargs and "heuristics_dir" not in kwargs:
            kwargs["heuristics_dir"] = kwargs.pop("pl_ver")
        if "pipe_ver" in kwargs and "heuristics_dir" not in kwargs:
            kwargs["heuristics_dir"] = kwargs.pop("pipe_ver")
        if "pipe_dir" in kwargs and "heuristics_dir" not in kwargs:
            kwargs["heuristics_dir"] = kwargs.pop("pipe_dir")
        if "heuristics_root" in kwargs and "heuristics_dir" not in kwargs:
            kwargs["heuristics_dir"] = kwargs.pop("heuristics_root")
        if "ancillary" in kwargs:
            kwargs["ancillary"] = _ancillary_list(kwargs["ancillary"])
        if "scipipe_rootdir" in kwargs:
            raw_root = kwargs["scipipe_rootdir"]
            if isinstance(raw_root, (list, tuple)):
                kwargs["scipipe_rootdir"] = resolve_first_existing_path(
                    raw_root, workspaces=kwargs.get("workspaces") or ()
                )
            elif raw_root is not None:
                kwargs["scipipe_rootdir"] = str(raw_root)
        if "scipipe_logdir" in kwargs:
            raw_log = kwargs["scipipe_logdir"]
            if isinstance(raw_log, (list, tuple)):
                kwargs["scipipe_logdir"] = resolve_first_existing_path(
                    raw_log, workspaces=kwargs.get("workspaces") or ()
                )
            elif raw_log is not None:
                kwargs["scipipe_logdir"] = str(raw_log)
        if "casarundata" in kwargs:
            raw_crd = kwargs["casarundata"]
            if isinstance(raw_crd, (list, tuple)):
                kwargs["casarundata"] = (
                    resolve_first_existing_path(raw_crd, workspaces=kwargs.get("workspaces") or ()) or None
                )
            elif raw_crd is not None:
                kwargs["casarundata"] = str(raw_crd)
            if "casadata" not in kwargs:
                kwargs["casadata"] = kwargs["casarundata"]
        elif "casadata" in kwargs:
            raw_crd = kwargs["casadata"]
            if isinstance(raw_crd, (list, tuple)):
                kwargs["casadata"] = (
                    resolve_first_existing_path(raw_crd, workspaces=kwargs.get("workspaces") or ()) or None
                )
            elif raw_crd is not None:
                kwargs["casadata"] = str(raw_crd)
            kwargs["casarundata"] = kwargs["casadata"]
        for f in fields(self):
            if f.name in kwargs:
                setattr(self, f.name, kwargs[f.name])
            elif f.default_factory is not MISSING:
                setattr(self, f.name, f.default_factory())
            else:
                setattr(self, f.name, f.default)

    @property
    def queue(self) -> str | None:
        """Backward-compatible alias for partition."""
        return self.partition

    @property
    def ntasks(self) -> int | None:
        """Backward-compatible and Slurm-native alias for cores."""
        return self.cores

    @property
    def ncores(self) -> int | None:
        """Backward-compatible alias for nprocs."""
        return self.nprocs

    @property
    def work_path(self) -> str | None:
        """Backward-compatible alias for workdir."""
        return self.workdir

    @property
    def casa_ver(self) -> str | None:
        """Backward-compatible alias for casa_root."""
        return self.casa_root

    @property
    def casa_dir(self) -> str | None:
        """Backward-compatible alias for casa_root."""
        return self.casa_root

    @property
    def pl_ver(self) -> str | None:
        """Backward-compatible alias for heuristics_dir."""
        return self.heuristics_dir

    @property
    def pipe_dir(self) -> str | None:
        """Backward-compatible alias for heuristics_dir."""
        return self.heuristics_dir

    @property
    def heuristics_root(self) -> str | None:
        """Backward-compatible alias for heuristics_dir."""
        return self.heuristics_dir

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileConfig:
        flat = dict(data)
        env_raw = flat.get("env")
        if isinstance(env_raw, str):
            env_name = env_raw
        elif isinstance(env_raw, dict):
            env_name = env_raw.get("name") or env_raw.get("env")
        else:
            env_name = None

        for sub_name in ("paths", "run", "batch", "env", "envs"):
            sub = flat.get(sub_name)
            if isinstance(sub, dict):
                for k, v in sub.items():
                    if k not in flat or flat[k] is None:
                        flat[k] = v

        vis_raw = flat.get("vis")
        if isinstance(vis_raw, str):
            vis_list = [vis_raw]
        elif isinstance(vis_raw, (list, tuple)):
            vis_list = list(vis_raw)
        else:
            vis_list = []

        return cls(
            env=_str_or_none(env_name),
            casa_root=_str_or_none(
                flat.get("casa_root") or flat.get("casa_dir") or flat.get("casa_ver")
            ),
            heuristics_dir=_str_or_none(
                flat.get("heuristics_dir")
                or flat.get("heuristics_root")
                or flat.get("pipe_dir")
                or flat.get("pipe_ver")
                or flat.get("pl_ver")
            ),
            pixi_dir=_str_or_none(flat.get("pixi_dir")),
            pixi_env=_str_or_none(flat.get("pixi_env")),
            branch=_str_or_none(flat.get("branch")),
            workspaces=(
                list(flat["workspaces"])
                if "workspaces" in flat and isinstance(flat["workspaces"], (list, tuple))
                else None
            ),
            scipipe_rootdir=(
                resolve_first_existing_path(
                    flat["scipipe_rootdir"], workspaces=flat.get("workspaces") or ()
                )
                if "scipipe_rootdir" in flat and flat["scipipe_rootdir"] is not None
                else None
            ),
            scipipe_logdir=(
                resolve_first_existing_path(
                    flat["scipipe_logdir"], workspaces=flat.get("workspaces") or ()
                )
                if "scipipe_logdir" in flat and flat["scipipe_logdir"] is not None
                else None
            ),
            casarundata=(
                resolve_first_existing_path(
                    flat.get("casarundata") if "casarundata" in flat else flat.get("casadata", flat.get("rundata")),
                    workspaces=flat.get("workspaces") or (),
                )
                if ("casarundata" in flat or "casadata" in flat or "rundata" in flat)
                else None
            ),
            casadata=(
                resolve_first_existing_path(
                    flat.get("casarundata") if "casarundata" in flat else flat.get("casadata", flat.get("rundata")),
                    workspaces=flat.get("workspaces") or (),
                )
                if ("casarundata" in flat or "casadata" in flat or "rundata" in flat)
                else None
            ),
            pickle_dir=_str_or_none(flat.get("pickle_dir")),
            obscaldir=_str_or_none(flat.get("obscaldir")),
            aUdir=_str_or_none(flat.get("aUdir")),
            validation_dir=_str_or_none(flat.get("validation_dir")),
            recipe=_str_or_none(flat.get("recipe")),
            nprocs=_int_or_none(flat.get("nprocs", flat.get("ncores"))),
            loglevel=_str_or_none(flat.get("loglevel")),
            useresume=bool(flat["useresume"]) if "useresume" in flat else None,
            symlink_shortcuts=bool(flat["symlink_shortcuts"])
            if "symlink_shortcuts" in flat
            else None,
            log2term=bool(flat["log2term"]) if "log2term" in flat else None,
            verbose=bool(flat["verbose"]) if "verbose" in flat else None,
            omp_num_threads=_int_or_none(flat.get("omp_num_threads")),
            openblas_num_threads=_int_or_none(flat.get("openblas_num_threads")),
            omp_max_threads=_int_or_none(flat.get("omp_max_threads")),
            mem_frac=_float_or_none(flat.get("mem_frac")),
            oversubscribe=bool(flat["oversubscribe"])
            if "oversubscribe" in flat
            else None,
            bind_to=_str_or_none(flat.get("bind_to")),
            map_by=_str_or_none(flat.get("map_by")),
            psrecord=bool(flat["psrecord"]) if "psrecord" in flat else None,
            memstats=bool(flat["memstats"]) if "memstats" in flat else None,
            pl_psrecord=bool(flat["pl_psrecord"]) if "pl_psrecord" in flat else None,
            backup=bool(flat["backup"]) if "backup" in flat else None,
            cont_dat=_str_or_none(flat.get("cont_dat")),
            jyperk_csv=_str_or_none(flat.get("jyperk_csv")),
            parameter_list=_str_or_none(flat.get("parameter_list")),
            ancillary=_ancillary_list(flat.get("ancillary", [])),
            workdir=_str_or_none(flat.get("workdir") or flat.get("work_path")),
            partition=_str_or_none(flat.get("partition", flat.get("queue"))),
            cores=_int_or_none(flat.get("cores", flat.get("ntasks"))),
            mem=_int_or_none(flat.get("mem")),
            node=_str_or_none(flat.get("node")),
            mail_type=_str_or_none(flat.get("mail_type")),
            walltime=_str_or_none(flat.get("walltime")),
            nodelist=_str_or_none(flat.get("nodelist")),
            chdir=_str_or_none(flat.get("chdir")),
            logdir=_str_or_none(flat.get("logdir")),
            cpus_per_task=_int_or_none(flat.get("cpus_per_task", flat.get("threads"))),
            mem_per_cpu=_str_or_none(flat.get("mem_per_cpu")),
            hint=_str_or_none(flat.get("hint")),
            ntasks_per_core=_int_or_none(flat.get("ntasks_per_core")),
            distribution=_str_or_none(flat.get("distribution")),
            no_requeue=bool(flat["no_requeue"]) if "no_requeue" in flat else None,
            scheduler=_str_or_none(flat.get("scheduler")),
            requirements=_str_or_none(flat.get("requirements")),
            mous=_str_or_none(flat.get("mous")),
            vis=vis_list,
            procedure=_str_or_none(flat.get("procedure")),
            script=_str_or_none(flat.get("script")),
            cmd=_str_or_none(flat.get("cmd")),
            ppr=_str_or_none(flat.get("ppr")),
            vla=bool(flat["vla"]) if "vla" in flat else None,
            interactive=bool(flat["interactive"]) if "interactive" in flat else None,
            datapath=_list_or_empty(flat["datapath"]) if "datapath" in flat else None,
            rundata=_list_or_empty(flat["rundata"]) if "rundata" in flat else None,
            dry_run=bool(flat["dry_run"]) if "dry_run" in flat else None,
            autoreload=bool(flat["autoreload"]) if "autoreload" in flat else None,
            nologger=(
                bool(flat["nologger"])
                if "nologger" in flat
                else (bool(flat["noglogger"]) if "noglogger" in flat else None)
            ),
            logfile=_str_or_none(flat.get("logfile")),
            use_custom_rcdir=(
                bool(flat["use_custom_rcdir"]) if "use_custom_rcdir" in flat else None
            ),
            casa_enable_telemetry=(
                bool(flat["casa_enable_telemetry"])
                if "casa_enable_telemetry" in flat
                else None
            ),
        )


class CalibpipeConfig(dict):
    """Strongly-typed, single source of truth configuration object for calibpipe.

    Provides typed attribute access (e.g. `cfg.site.use_custom_rcdir`, `cfg.paths.scipipe_rootdir`)
    while maintaining full dict-mapping compatibility (e.g. `cfg['site']`, `cfg.get('paths')`).
    """

    def __init__(
        self,
        default_env: str = "main",
        default_profile: str | None = None,
        paths: PathsConfig | None = None,
        envs: dict[str, EnvSpec] | None = None,
        site: SiteConfig | None = None,
        batch: BatchConfig | None = None,
        batches: dict[str, BatchConfig] | None = None,
        run: RunConfig | None = None,
        profiles: dict[str, ProfileConfig] | None = None,
        raw_dict: dict[str, Any] | None = None,
        loaded_layers: Sequence[str | Path] | None = None,
    ) -> None:
        super().__init__(raw_dict or {})
        self.default_env = default_env
        self.default_profile = default_profile
        self.paths = paths or PathsConfig()
        self.envs = envs or {}
        self.site = site or SiteConfig()
        self.batch = batch or BatchConfig()
        self.batches = batches or {}
        self.run = run or RunConfig()
        self.profiles = profiles or {}
        self.loaded_layers: list[Path] = (
            [Path(p) for p in loaded_layers] if loaded_layers else []
        )

        # Synchronize dictionary keys for backwards compatibility
        self["default_env"] = self.default_env
        if self.default_profile is not None:
            self["default_profile"] = self.default_profile
        self["paths"] = asdict(self.paths)
        self["envs"] = {
            k: asdict(v) if is_dataclass(v) else v for k, v in self.envs.items()
        }
        self["site"] = asdict(self.site)
        self["batch"] = asdict(self.batch)
        self["batches"] = {
            k: asdict(v) if is_dataclass(v) else v for k, v in self.batches.items()
        }
        self["run"] = asdict(self.run)
        self["profiles"] = {
            k: asdict(v) if is_dataclass(v) else v for k, v in self.profiles.items()
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        loaded_layers: Sequence[str | Path] | None = None,
    ) -> CalibpipeConfig:
        """Construct a strongly-typed CalibpipeConfig from a parsed TOML dictionary."""
        default_profile_raw = data.get("default_profile")
        default_profile = str(default_profile_raw) if default_profile_raw is not None else None
        default_env_raw = data.get("default_env")
        paths_data = dict(data.get("paths", {}))
        site_data = dict(data.get("site", {}))

        # Tool installation paths configured in [paths] take effect in site config if not set in [site]
        for tool_key in ("pmr_home", "datapacker_home", "acsdata", "java_home", "pixi_bin"):
            if tool_key in paths_data and paths_data[tool_key] and tool_key not in site_data:
                site_data[tool_key] = paths_data[tool_key]
            elif tool_key in site_data and site_data[tool_key] and tool_key not in paths_data:
                paths_data[tool_key] = site_data[tool_key]

        paths = PathsConfig.from_dict(paths_data)
        site = SiteConfig.from_dict(site_data)
        batch = BatchConfig.from_dict(data.get("batch", {}))
        run = RunConfig.from_dict(data.get("run", {}))

        batches: dict[str, BatchConfig] = {}
        for b_name, b_data in data.get("batches", {}).items():
            if isinstance(b_data, dict):
                b_copy = dict(b_data)
                if "queue" in b_copy and "partition" not in b_copy:
                    b_copy["partition"] = b_copy["queue"]
                if "ntasks" in b_copy and "cores" not in b_copy:
                    b_copy["cores"] = b_copy["ntasks"]
                if "threads" in b_copy and "cpus_per_task" not in b_copy:
                    b_copy["cpus_per_task"] = b_copy["threads"]
                merged_batch_data = dict(asdict(batch))
                merged_batch_data.update(b_copy)
                batches[b_name] = BatchConfig.from_dict(merged_batch_data)

        profiles: dict[str, ProfileConfig] = {}
        for prof_name, prof_data in data.get("profiles", {}).items():
            if isinstance(prof_data, dict):
                profiles[prof_name] = ProfileConfig.from_dict(prof_data)

        # Mirror any [batches] entries into profiles so all presets exist in profiles
        for b_name, b_cfg in batches.items():
            if b_name not in profiles:
                profiles[b_name] = ProfileConfig(
                    partition=b_cfg.partition,
                    cores=b_cfg.cores,
                    mem=b_cfg.mem,
                    mem_per_cpu=b_cfg.mem_per_cpu,
                    node=b_cfg.node,
                    walltime=b_cfg.walltime,
                    nodelist=b_cfg.nodelist,
                    chdir=b_cfg.chdir,
                    cpus_per_task=b_cfg.cpus_per_task,
                    ntasks_per_core=b_cfg.ntasks_per_core,
                    hint=b_cfg.hint,
                    distribution=b_cfg.distribution,
                    no_requeue=b_cfg.no_requeue,
                    scheduler=b_cfg.scheduler,
                    requirements=b_cfg.requirements,
                    dry_run=b_cfg.dry_run,
                    logdir=b_cfg.logdir,
                    mail_type=b_cfg.mail_type,
                )

        workspaces = paths.workspaces
        envs: dict[str, EnvSpec] = {}
        for env_name, env_data in data.get("envs", {}).items():
            if isinstance(env_data, dict):
                raw_casa_root = (
                    env_data.get("casa_root")
                    or env_data.get("casa_dir")
                    or env_data.get("casa_ver")
                    or ""
                )
                if raw_casa_root and workspaces:
                    resolved_casa = resolve_workspace_path(
                        raw_casa_root.rstrip("/"), workspaces
                    )
                    if _safe_exists(resolved_casa):
                        raw_casa_root = str(resolved_casa)

                raw_pixi_dir = env_data.get("pixi_dir")
                if raw_pixi_dir and workspaces:
                    resolved_pixi = resolve_workspace_path(
                        raw_pixi_dir.rstrip("/"), workspaces
                    )
                    if _safe_exists(resolved_pixi):
                        raw_pixi_dir = str(resolved_pixi)

                raw_heuristics = (
                    env_data.get("heuristics_dir")
                    or env_data.get("heuristics_root")
                    or env_data.get("pipe_dir")
                    or env_data.get("pipe_ver")
                    or env_data.get("pl_ver")
                )
                if raw_heuristics and workspaces and "{" not in raw_heuristics:
                    resolved_heur = resolve_workspace_path(
                        raw_heuristics.rstrip("/"), workspaces
                    )
                    if _safe_exists(resolved_heur):
                        raw_heuristics = str(resolved_heur)

                envs[env_name] = EnvSpec(
                    name=env_name,
                    casa_root=raw_casa_root,
                    branch=env_data.get("branch", env_name),
                    heuristics_dir=raw_heuristics,
                    pixi_dir=raw_pixi_dir,
                    pixi_env=env_data.get("pixi_env", "default"),
                    extra_vars={
                        k: str(v)
                        for k, v in env_data.items()
                        if k
                        not in (
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
                        )
                    },
                )

        if default_env_raw is not None:
            default_env = str(default_env_raw)
        elif default_profile and default_profile in profiles and profiles[default_profile].env:
            default_env = str(profiles[default_profile].env)
        elif default_profile and default_profile in envs:
            default_env = default_profile
        else:
            default_env = "main"

        return cls(
            default_env=default_env,
            default_profile=default_profile,
            paths=paths,
            envs=envs,
            site=site,
            batch=batch,
            batches=batches,
            run=run,
            profiles=profiles,
            raw_dict=data,
            loaded_layers=loaded_layers,
        )


@dataclass
class ResolvedRunOptions:
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
    subdir: str | None = None
    onlysemipass: str = ""
    verbose: bool = False

    vis: list[str] = field(default_factory=list)
    procedure: str = ""
    script: str = ""
    cmd: str = ""
    vla: bool = False
    workdir: str = ""
    interactive: bool = False

    # Hardware & MPI tuning
    omp_num_threads: int | None = None
    openblas_num_threads: int | None = None
    omp_max_threads: int | None = None
    mem_frac: float | None = None
    oversubscribe: bool = True
    bind_to: str | None = None
    map_by: str | None = None

    # Telemetry profiling
    psrecord: bool = False
    memstats: bool = False
    pl_psrecord: bool = False

    # Ancillary staging & backup
    backup: bool = False
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
    casa_enable_telemetry: bool = False

    # Environment & Path overrides
    casa_root: str | None = None
    heuristics_dir: str | None = None
    pixi_dir: str | None = None
    pixi_env: str | None = None
    branch: str | None = None
    workspaces: list[str] = field(default_factory=list)
    scipipe_rootdir: str | None = None
    scipipe_logdir: str | None = None
    casarundata: str | None = None
    casadata: str | None = None

    def __init__(
        self,
        profile: str | None = None,
        mous: str = "",
        env_name: str = "",
        recipe: str = "",
        nprocs: int = 8,
        loglevel: str = "debug",
        useresume: bool = False,
        use_custom_rcdir: bool = True,
        symlink_shortcuts: bool = True,
        log2term: bool = False,
        flag_dir: str | None = None,
        ppr: str | None = None,
        subdir: str | None = None,
        onlysemipass: str = "",
        verbose: bool = False,
        vis: list[str] | None = None,
        procedure: str = "",
        script: str = "",
        cmd: str = "",
        vla: bool = False,
        workdir: str = "",
        interactive: bool = False,
        omp_num_threads: int | None = None,
        openblas_num_threads: int | None = None,
        omp_max_threads: int | None = None,
        mem_frac: float | None = None,
        oversubscribe: bool = True,
        bind_to: str | None = None,
        map_by: str | None = None,
        psrecord: bool = False,
        memstats: bool = False,
        pl_psrecord: bool = False,
        backup: bool = False,
        cont_dat: str | None = None,
        jyperk_csv: str | None = None,
        parameter_list: str | None = None,
        ancillary: list[Any] | None = None,
        datapath: list[str] | None = None,
        rundata: list[str] | None = None,
        rundata_specified: bool = False,
        dry_run: bool = False,
        autoreload: bool = True,
        nologger: bool = False,
        logfile: str | None = None,
        casa_enable_telemetry: bool = False,
        casa_root: str | None = None,
        heuristics_dir: str | None = None,
        pixi_dir: str | None = None,
        pixi_env: str | None = None,
        branch: str | None = None,
        workspaces: list[str] | None = None,
        scipipe_rootdir: str | None = None,
        scipipe_logdir: str | None = None,
        casarundata: str | None = None,
        casadata: str | None = None,
        casa_ver: str | None = None,
        casa_dir: str | None = None,
        pl_ver: str | None = None,
        pipe_ver: str | None = None,
        pipe_dir: str | None = None,
        heuristics_root: str | None = None,
        ncores: int | None = None,
    ) -> None:
        self.profile = profile
        self.mous = mous
        self.env_name = env_name
        self.recipe = recipe
        self.nprocs = ncores if ncores is not None else nprocs
        self.loglevel = loglevel
        self.useresume = useresume
        self.use_custom_rcdir = use_custom_rcdir
        self.symlink_shortcuts = symlink_shortcuts
        self.log2term = log2term
        self.flag_dir = flag_dir
        self.ppr = ppr
        self.subdir = subdir
        self.onlysemipass = onlysemipass
        self.verbose = verbose
        self.vis = list(vis or [])
        self.procedure = procedure
        self.script = script
        self.cmd = cmd
        self.vla = vla
        self.workdir = workdir
        self.interactive = interactive
        self.omp_num_threads = omp_num_threads
        self.openblas_num_threads = openblas_num_threads
        self.omp_max_threads = omp_max_threads
        self.mem_frac = mem_frac
        self.oversubscribe = oversubscribe
        self.bind_to = bind_to
        self.map_by = map_by
        self.psrecord = psrecord
        self.memstats = memstats
        self.pl_psrecord = pl_psrecord
        self.backup = backup
        self.cont_dat = cont_dat
        self.jyperk_csv = jyperk_csv
        self.parameter_list = parameter_list
        self.ancillary = _ancillary_list(ancillary)
        self.datapath = list(datapath or [])
        self.rundata = list(rundata or [])
        self.rundata_specified = rundata_specified
        self.dry_run = dry_run
        self.autoreload = autoreload
        self.nologger = nologger
        self.logfile = logfile
        self.casa_enable_telemetry = casa_enable_telemetry
        self.casa_root = casa_root or casa_dir or casa_ver
        self.heuristics_dir = (
            heuristics_dir or heuristics_root or pipe_dir or pipe_ver or pl_ver
        )
        self.pixi_dir = pixi_dir
        self.pixi_env = pixi_env
        self.branch = branch
        self.workspaces = list(workspaces or [])
        self.scipipe_rootdir = (
            resolve_first_existing_path(scipipe_rootdir, workspaces=self.workspaces)
            if isinstance(scipipe_rootdir, (list, tuple))
            else scipipe_rootdir
        )
        self.scipipe_logdir = (
            resolve_first_existing_path(scipipe_logdir, workspaces=self.workspaces)
            if isinstance(scipipe_logdir, (list, tuple))
            else scipipe_logdir
        )
        chosen_crd = casarundata or casadata
        self.casarundata = (
            resolve_first_existing_path(chosen_crd, workspaces=self.workspaces) or None
            if isinstance(chosen_crd, (list, tuple))
            else (str(chosen_crd).strip() or None if chosen_crd is not None else None)
        )
        self.casadata = self.casarundata

    @property
    def ncores(self) -> int:
        """Backward-compatible alias for nprocs."""
        return self.nprocs

    @property
    def casa_ver(self) -> str | None:
        """Backward-compatible alias for casa_root."""
        return self.casa_root

    @property
    def casa_dir(self) -> str | None:
        """Backward-compatible alias for casa_root."""
        return self.casa_root

    @property
    def pl_ver(self) -> str | None:
        """Backward-compatible alias for heuristics_dir."""
        return self.heuristics_dir

    @property
    def pipe_dir(self) -> str | None:
        """Backward-compatible alias for heuristics_dir."""
        return self.heuristics_dir

    @property
    def heuristics_root(self) -> str | None:
        """Backward-compatible alias for heuristics_dir."""
        return self.heuristics_dir


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
        if cli_v is not None:
            return cli_v
        if profile is not None:
            prof_v = getattr(profile, attr, None)
            if prof_v is not None:
                return prof_v
        return cfg_val

    # Custom rcdir: CLI flag > Profile > [run] > [site]
    rcdir_cfg_fallback = (
        cfg.run.use_custom_rcdir
        if cfg.run.use_custom_rcdir is not None
        else cfg.site.use_custom_rcdir
    )
    cli_rcdir = (
        getattr(cli_opts, "custom_rcdir", None)
        if getattr(cli_opts, "custom_rcdir", None) is not None
        else getattr(cli_opts, "use_custom_rcdir", None)
    )
    prof_rcdir = profile.use_custom_rcdir if profile and profile.use_custom_rcdir is not None else None
    use_custom_rcdir = bool(
        cli_rcdir if cli_rcdir is not None else (prof_rcdir if prof_rcdir is not None else rcdir_cfg_fallback)
    )

    # Telemetry: CLI flag > Profile > [run] > [site]
    telemetry_cfg_fallback = (
        cfg.run.casa_enable_telemetry
        if cfg.run.casa_enable_telemetry is not None
        else cfg.site.casa_enable_telemetry
    )
    cli_telemetry = (
        getattr(cli_opts, "casa_enable_telemetry", None)
        if getattr(cli_opts, "casa_enable_telemetry", None) is not None
        else getattr(cli_opts, "telemetry", None)
    )
    prof_telemetry = profile.casa_enable_telemetry if profile and profile.casa_enable_telemetry is not None else None
    casa_enable_telemetry = bool(
        cli_telemetry if cli_telemetry is not None else (prof_telemetry if prof_telemetry is not None else telemetry_cfg_fallback)
    )

    symlink_shortcuts = _val("symlink_shortcuts", cfg.run.symlink_shortcuts)
    log2term = _val("log2term", cfg.run.log2term or cfg.site.log2term)
    verbose = _val("verbose", cfg.run.verbose)
    recipe = _val("recipe", cfg.run.recipe)
    cli_nprocs = (
        getattr(cli_opts, "nprocs", None)
        if getattr(cli_opts, "nprocs", None) is not None
        else getattr(cli_opts, "ncores", None)
    )
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
        or ""
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
        ppr = profile.ppr
        interactive = (
            bool(profile.interactive) if profile.interactive is not None else False
        )
    else:
        mous = ""
        vis = []
        script = ""
        cmd = ""
        ppr = None
        interactive = False

    procedure = (
        getattr(cli_opts, "procedure", "")
        or (profile.procedure if profile and profile.procedure else "")
        or ""
    )
    cli_vla = getattr(cli_opts, "vla", None)
    if cli_vla:
        vla = True
    elif profile is not None and profile.vla is not None:
        vla = bool(profile.vla)
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
    if cli_logfile:
        logfile = str(cli_logfile).strip()
    elif profile is not None and profile.logfile:
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
    elif cfg.run.datapath:
        resolved_datapath = list(cfg.run.datapath)
    elif cfg.paths.datapath:
        resolved_datapath = list(cfg.paths.datapath)
    else:
        resolved_datapath = []

    cli_rundata = getattr(cli_opts, "rundata", None)
    rundata_specified = False
    if cli_rundata is not None:
        resolved_rundata = _list_or_empty(cli_rundata)
        rundata_specified = True
    elif profile is not None and profile.rundata is not None:
        resolved_rundata = list(profile.rundata)
        rundata_specified = True
    elif cfg.run.rundata:
        resolved_rundata = list(cfg.run.rundata)
        rundata_specified = True
    elif cfg.paths.rundata:
        resolved_rundata = list(cfg.paths.rundata)
        rundata_specified = True
    else:
        resolved_rundata = []

    # Resolve environment and path overrides: CLI > Profile
    cli_casa_root = (
        getattr(cli_opts, "casa_root", None)
        or getattr(cli_opts, "casa_dir", None)
        or getattr(cli_opts, "casa_ver", None)
    )
    prof_casa_root = profile.casa_root if profile else None
    resolved_casa_root = cli_casa_root or prof_casa_root

    cli_heuristics_dir = (
        getattr(cli_opts, "heuristics_dir", None)
        or getattr(cli_opts, "heuristics_root", None)
        or getattr(cli_opts, "pipe_dir", None)
        or getattr(cli_opts, "pipe_ver", None)
        or getattr(cli_opts, "pl_ver", None)
    )
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

    cli_casarundata = getattr(cli_opts, "casarundata", None)
    cli_casadata = getattr(cli_opts, "casadata", None)
    prof_casarundata = (
        getattr(profile, "casarundata", None)
        or getattr(profile, "casadata", None)
        if profile
        else None
    )
    resolved_casarundata = (
        cli_casarundata
        or cli_casadata
        or prof_casarundata
        or getattr(cfg.paths, "casarundata", None)
        or cfg.paths.casadata
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

    resolved_casadata = resolved_casarundata
    if not resolved_rundata and resolved_casarundata and not rundata_specified:
        resolved_rundata = [resolved_casarundata]
        rundata_specified = True

    cli_scipipe_rootdir = getattr(cli_opts, "scipipe_rootdir", None)
    prof_scipipe_rootdir = profile.scipipe_rootdir if profile else None
    resolved_scipipe_rootdir = cli_scipipe_rootdir or prof_scipipe_rootdir

    cli_scipipe_logdir = getattr(cli_opts, "scipipe_logdir", None)
    prof_scipipe_logdir = profile.scipipe_logdir if profile else None
    resolved_scipipe_logdir = cli_scipipe_logdir or prof_scipipe_logdir

    prof_workspaces = profile.workspaces if profile and profile.workspaces else None
    resolved_workspaces = list(prof_workspaces or cfg.paths.workspaces or [])

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
        if procedure:
            procedure = str(_resolve_one(procedure))
        if script:
            script = str(_resolve_one(script))
        if ppr:
            ppr = str(_resolve_one(ppr))
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

    if workdir:
        workdir_search = []
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
        if root_dir is not None:
            workdir_search.append(str(root_dir))
        if workspaces:
            workdir_search.extend(workspaces)
        if workdir_search:
            resolved_wd = resolve_workspace_path(workdir, workdir_search)
            if _safe_exists(resolved_wd):
                workdir = str(resolved_wd)
            elif (
                root_dir is not None
                and not Path(workdir).is_absolute()
                and not workdir.startswith("~")
            ):
                workdir = str(root_dir / workdir)

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
        flag_dir=getattr(cli_opts, "flag", None),
        ppr=ppr,
        subdir=getattr(cli_opts, "subdir", None),
        onlysemipass=getattr(cli_opts, "onlysemipass", "") or "",
        verbose=bool(verbose),
        vis=vis,
        procedure=procedure,
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
        casa_enable_telemetry=casa_enable_telemetry,
        casa_root=resolved_casa_root,
        heuristics_dir=resolved_heuristics_dir,
        pixi_dir=resolved_pixi_dir,
        pixi_env=resolved_pixi_env,
        branch=resolved_branch,
        workspaces=resolved_workspaces,
        scipipe_rootdir=resolved_scipipe_rootdir,
        scipipe_logdir=resolved_scipipe_logdir,
        casarundata=resolved_casarundata,
        casadata=resolved_casadata,
    )


@dataclass
class ResolvedBatchOptions:
    """Fully resolved options for a batch submission (Slurm or HTCondor).

    Scheduler Compatibility:
      - Shared [Slurm & HTCondor]:
          pipefile, env_name, partition, cores (ntasks), mem, mem_per_cpu,
          mail_type, chdir, nodelist, scheduler, dry_run, logdir, outfile, errfile.
      - Slurm only:
          node, walltime, cpus_per_task, ntasks_per_core, hint, distribution, no_requeue.
      - HTCondor only:
          requirements.
    """

    pipefile: Path | None
    env_name: str
    partition: str
    cores: int  # Tasks/CPUs per job (Slurm: --ntasks; HTCondor: request_cpus; alias: ntasks)
    mem: int  # Total RAM in GB (Slurm: --mem; HTCondor: request_memory)
    node: str  # [Slurm only] --nodes
    mail_type: str  # [Slurm & HTCondor] Notification policy
    outfile: str | None = None
    errfile: str | None = None
    extra_args: list[str] = field(default_factory=list)
    # Directives
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
    profile: str | None = None
    dry_run: bool = False
    logdir: str | None = None

    @property
    def queue(self) -> str:
        """Backward-compatible alias for partition."""
        return self.partition

    @property
    def ntasks(self) -> int:
        """Slurm-native alias for cores."""
        return self.cores


def resolve_batch_options(
    config: CalibpipeConfig | dict[str, Any],
    cli_args: Any,
) -> ResolvedBatchOptions:
    """Resolve batch options by cascading CLI flags over profile over config.toml [batch] over defaults."""
    if isinstance(config, CalibpipeConfig):
        cfg = config
    else:
        cfg = CalibpipeConfig.from_dict(config)

    profile_name = getattr(cli_args, "profile", None)
    pipefile_arg = getattr(cli_args, "pipefile", None)
    if not pipefile_arg and not profile_name:
        raise ConfigError("Either a pipefile or a --profile must be specified.")

    profile: ProfileConfig | None = None
    active_batch: BatchConfig = cfg.batch

    if profile_name:
        if profile_name in cfg.profiles:
            profile = cfg.profiles[profile_name]
            if getattr(profile, "interactive", False) or getattr(profile, "target", "") == "interactive":
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
        elif profile_name in cfg.batches:
            active_batch = cfg.batches[profile_name]
        else:
            available = sorted(set(cfg.profiles.keys()) | set(cfg.batches.keys()))
            raise ConfigError(
                f"Unknown batch profile '{profile_name}'. Available profiles: {available}"
            )

    env_name = (
        getattr(cli_args, "env", None)
        or (profile.env if profile and profile.env else None)
        or cfg.default_env
    )

    cli_partition = getattr(cli_args, "partition", None) or getattr(
        cli_args, "queue", None
    )
    partition = cli_partition if cli_partition else active_batch.partition

    cores = getattr(cli_args, "cores", None)
    if cores is None:
        cores = getattr(cli_args, "ntasks", None)
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

    walltime = _cli_or_cfg("walltime", active_batch.walltime)
    nodelist = _cli_or_cfg("nodelist", active_batch.nodelist)
    chdir = _cli_or_cfg("chdir", active_batch.chdir)
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
    raw_logdir = active_batch.logdir or cfg.paths.scipipe_logdir or None
    if isinstance(raw_logdir, (list, tuple)):
        raw_logdir = resolve_first_existing_path(raw_logdir)
    logdir = _cli_or_cfg("logdir", raw_logdir)

    return ResolvedBatchOptions(
        pipefile=Path(pipefile_arg) if pipefile_arg else None,
        env_name=env_name,
        partition=str(partition),
        cores=int(cores),
        mem=int(mem),
        node=str(node),
        mail_type=str(mail_type),
        outfile=getattr(cli_args, "outfile", None),
        errfile=getattr(cli_args, "errfile", None),
        extra_args=list(getattr(cli_args, "extra_args", [])),
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
        profile=profile_name,
        dry_run=dry_run,
        logdir=logdir,
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

    repo_site_config = (
        Path(__file__).resolve().parent.parent.parent / "config.site.toml"
    )
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
            repo_root_config = (
                Path(__file__).resolve().parent.parent.parent / "config.toml"
            )
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
        Parsed configuration mapping with typed attributes and backward-compatible dict access.

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
        merged_data = _deep_merge_dict(merged_data, data)
        resolved_layers.append(resolved)

    cfg = CalibpipeConfig.from_dict(merged_data, loaded_layers=resolved_layers)
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


def format_config_overview(
    config: CalibpipeConfig | dict[str, Any],
    env_name: str | None = None,
    config_path: str | Path | None = None,
    config_layers: Sequence[str | Path] | None = None,
) -> str:
    """Format a human-readable summary of the resolved configuration.

    Args:
        config: Loaded configuration object or dictionary.
        env_name: Environment name to display details for.
        config_path: Path to the loaded configuration file.
        config_layers: Optional sequence of loaded configuration layer paths.

    Returns:
        Formatted multi-line summary.
    """
    if isinstance(config, CalibpipeConfig):
        cfg = config
    else:
        cfg = CalibpipeConfig.from_dict(config)

    selected_env = env_name or cfg.default_env
    env_spec = cfg.envs.get(selected_env)

    lines = [
        "=" * 80,
        "calibpipe Configuration Overview",
        "=" * 80,
    ]

    layers: list[Path] = [Path(p) for p in config_layers] if config_layers else []
    if not layers and isinstance(config, CalibpipeConfig) and config.loaded_layers:
        layers = config.loaded_layers

    if layers and len(layers) > 1:
        lines.append("Configuration Layers (lowest to highest priority):")
        for i, lp in enumerate(layers, 1):
            lines.append(f"  [{i}] {lp}")
    elif config_path:
        lines.append(f"Config File:      {config_path}")
    elif layers and len(layers) == 1:
        lines.append(f"Config File:      {layers[0]}")
    if cfg.default_profile:
        lines.append(f"Default Profile:  {cfg.default_profile}")
    lines.extend(
        [
            f"Default Env:      {cfg.default_env}",
            f"Selected Env:     {selected_env}",
        ]
    )

    if env_spec:
        if env_spec.is_pixi:
            lines.extend(
                [
                    "  Runtime:        Modular Pixi",
                    f"  Pixi Dir:       {env_spec.pixi_dir}",
                    f"  Pixi Env:       {env_spec.pixi_env}",
                    f"  Branch:         {env_spec.branch}",
                    f"  Heuristics:     {env_spec.heuristics_dir or '(bundled / default)'}",
                ]
            )
        else:
            lines.extend(
                [
                    "  Runtime:        Monolithic CASA",
                    f"  CASA Root:      {env_spec.casa_root}",
                    f"  Branch:         {env_spec.branch}",
                    f"  Heuristics:     {env_spec.heuristics_dir or '(bundled / default)'}",
                ]
            )
    else:
        lines.append(f"  [envs.{selected_env}] NOT DEFINED in config")

    lines.extend(
        [
            "",
            "Paths:",
            f"  scipipe_rootdir: {cfg.paths.scipipe_rootdir or '(not set)'}",
            f"  scipipe_logdir:  {cfg.paths.scipipe_logdir or '(not set)'}",
        ]
    )
    if cfg.paths.workspaces:
        lines.append(f"  workspaces:      {', '.join(cfg.paths.workspaces)}")
    if cfg.paths.casarundata or cfg.paths.casadata:
        lines.append(f"  casarundata:     {cfg.paths.casarundata or cfg.paths.casadata}")
    if cfg.paths.datapath:
        lines.append(f"  datapath:        {', '.join(cfg.paths.datapath)}")
    if cfg.paths.rundata:
        lines.append(f"  rundata:         {', '.join(cfg.paths.rundata)}")
    if cfg.paths.pickle_dir:
        lines.append(f"  pickle_dir:      {cfg.paths.pickle_dir}")
    if cfg.paths.obscaldir:
        lines.append(f"  obscaldir:       {cfg.paths.obscaldir}")

    lines.extend(
        [
            "",
            "Site & Cluster Integration ([site]):",
            f"  Submit Host:     {cfg.site.submit_host or '(none - any host allowed)'}",
        ]
    )
    if cfg.site.pixi_bin:
        lines.append(f"  Pixi Bin:        {cfg.site.pixi_bin}")
    lines.extend(
        [
            f"  Java Home:       {cfg.site.java_home}",
            f"  PMR Home:        {cfg.site.pmr_home}",
            f"  Datapacker:      {cfg.site.datapacker_home}",
            f"  ACS Data:        {cfg.site.acsdata}",
            f"  Custom RCDIR:    {cfg.site.use_custom_rcdir}",
            f"  Telemetry:       {cfg.site.casa_enable_telemetry}",
            f"  Log2term:        {cfg.run.log2term or cfg.site.log2term}",
            "",
            "Slurm Batch Defaults ([batch]):",
            f"  Scheduler:       {cfg.batch.scheduler}",
            f"  Partition:       {cfg.batch.partition}",
            f"  Cores / Memory:  {cfg.batch.cores} cores, "
            + (
                f"{cfg.batch.mem_per_cpu}/CPU"
                if cfg.batch.mem_per_cpu
                else f"{cfg.batch.mem} GB"
            ),
            f"  Node:            {cfg.batch.node}",
            f"  Mail Type:       {cfg.batch.mail_type}",
        ]
    )
    if cfg.batch.walltime:
        lines.append(f"  Walltime:        {cfg.batch.walltime}")
    if cfg.batch.cpus_per_task:
        lines.append(f"  CPUs/Task:       {cfg.batch.cpus_per_task}")
    if cfg.batch.nodelist:
        lines.append(f"  Nodelist:        {cfg.batch.nodelist}")
    if cfg.batch.chdir:
        lines.append(f"  Working Dir:     {cfg.batch.chdir}")
    if cfg.batch.hint:
        lines.append(f"  Hint:            {cfg.batch.hint}")
    if cfg.batch.ntasks_per_core:
        lines.append(f"  Tasks/Core:      {cfg.batch.ntasks_per_core}")
    if cfg.batch.distribution:
        lines.append(f"  Distribution:    {cfg.batch.distribution}")
    if not cfg.batch.no_requeue:
        lines.append("  Requeue:         True")

    if cfg.batches:
        lines.extend(["", "Slurm Batch Profiles ([batches.<name>]):"])
        for b_name in sorted(cfg.batches.keys()):
            b_cfg = cfg.batches[b_name]
            mem_display = (
                f"{b_cfg.mem_per_cpu}/CPU" if b_cfg.mem_per_cpu else f"{b_cfg.mem} GB"
            )
            details = [
                f"partition={b_cfg.partition}",
                f"cores={b_cfg.cores}",
                f"mem={mem_display}",
            ]
            if b_cfg.walltime:
                details.append(f"time={b_cfg.walltime}")
            if b_cfg.nodelist:
                details.append(f"nodelist={b_cfg.nodelist}")
            lines.append(f"  [{b_name}] " + ", ".join(details))

    lines.extend(
        [
            "",
            "Pipeline Run Defaults ([run]):",
            f"  Recipe:          {cfg.run.recipe}",
            f"  Processes:       {cfg.run.nprocs}",
            f"  Log Level:       {cfg.run.loglevel}",
            f"  Use Resume:      {cfg.run.useresume}",
        ]
    )

    if cfg.profiles:
        lines.append("")
        lines.append("Execution Profiles ([profiles]):")
        for pname in sorted(cfg.profiles.keys()):
            p = cfg.profiles[pname]
            pdetails = []
            if p.cmd:
                pdetails.append("target=cmd")
            elif p.script:
                pdetails.append(f"target=script({p.script})")
            elif p.vis:
                pdetails.append(f"target=vis({len(p.vis)} MS)")
            elif p.mous:
                pdetails.append(f"target=mous({p.mous})")
            elif p.ppr:
                pdetails.append(f"target=ppr({p.ppr})")
            elif p.interactive:
                pdetails.append("target=interactive")

            if p.env:
                pdetails.append(f"env={p.env}")
            if p.recipe:
                pdetails.append(f"recipe={p.recipe}")
            if p.nprocs:
                pdetails.append(f"nprocs={p.nprocs}")
            if p.scheduler:
                pdetails.append(f"scheduler={p.scheduler}")
            detail_str = f" ({', '.join(pdetails)})" if pdetails else ""
            lines.append(f"  {pname}{detail_str}")

    lines.append("=" * 80)
    return "\n".join(lines)


def format_profile_list(config: CalibpipeConfig | dict[str, Any]) -> str:
    """Format a summary list of all available execution and batch profiles.

    Args:
        config: Loaded configuration object or dictionary.

    Returns:
        Formatted multi-line summary of profiles.
    """
    if isinstance(config, CalibpipeConfig):
        cfg = config
    else:
        cfg = CalibpipeConfig.from_dict(config)

    lines = [
        "=" * 80,
        "calibpipe Profiles",
        "=" * 80,
    ]

    lines.append("\nExecution Profiles ([profiles.<name>]):")
    if not cfg.profiles:
        lines.append("  (none defined)")
    else:
        for pname in sorted(cfg.profiles.keys()):
            if pname in cfg.batches:
                continue
            p = cfg.profiles[pname]
            pdetails = []
            if p.cmd:
                target_str = "cmd"
            elif p.script:
                target_str = f"script({p.script})"
            elif p.vis:
                target_str = f"vis({len(p.vis)} MS)"
            elif p.mous:
                target_str = f"mous({p.mous})"
            elif p.ppr:
                target_str = f"ppr({p.ppr})"
            elif p.interactive:
                target_str = "interactive"
            elif p.recipe:
                target_str = f"recipe({p.recipe})"
            elif p.partition or p.mem or p.mem_per_cpu or p.walltime or p.scheduler:
                target_str = p.scheduler or "batch"
            else:
                target_str = "tuning"

            if p.env:
                pdetails.append(f"env={p.env}")
            if p.partition:
                pdetails.append(f"partition={p.partition}")
            if p.nprocs:
                pdetails.append(f"nprocs={p.nprocs}")
            if p.threads and p.threads != 1:
                pdetails.append(f"threads={p.threads}")
            if p.mem:
                pdetails.append(f"mem={p.mem} GB")
            if p.mem_per_cpu:
                pdetails.append(f"mem_per_cpu={p.mem_per_cpu}")
            if p.walltime:
                pdetails.append(f"time={p.walltime}")
            if p.psrecord:
                pdetails.append("psrecord=True")
            if p.backup:
                pdetails.append("backup=True")
            if p.workdir:
                pdetails.append(f"workdir={p.workdir}")

            detail_str = f"  [{target_str}]".ljust(18) + (
                f"({', '.join(pdetails)})" if pdetails else ""
            )
            lines.append(f"  {pname:<28} {detail_str}")

    lines.append("\nBatch Profiles ([batches.<name>]):")
    if not cfg.batches:
        lines.append("  (none defined)")
    else:
        for bname in sorted(cfg.batches.keys()):
            b = cfg.batches[bname]
            bdetails = [f"partition={b.partition}", f"cores={b.cores}"]
            mem_display = f"{b.mem_per_cpu}/CPU" if b.mem_per_cpu else f"{b.mem} GB"
            bdetails.append(f"mem={mem_display}")
            if b.walltime:
                bdetails.append(f"time={b.walltime}")
            if b.scheduler:
                bdetails.append(f"scheduler={b.scheduler}")
            target_str = f"[{b.scheduler}]"
            detail_str = f"  {target_str}".ljust(18) + f"({', '.join(bdetails)})"
            lines.append(f"  {bname:<28} {detail_str}")

    lines.append("=" * 80)
    return "\n".join(lines)


def format_profile_details(config: CalibpipeConfig | dict[str, Any], name: str) -> str:
    """Format detailed information for a single execution or batch profile.

    Args:
        config: Loaded configuration object or dictionary.
        name: Name of the profile to inspect.

    Returns:
        Formatted multi-line summary of the profile's settings.

    Raises:
        ConfigError: If the profile is not found in cfg.profiles or cfg.batches.
    """
    if isinstance(config, CalibpipeConfig):
        cfg = config
    else:
        cfg = CalibpipeConfig.from_dict(config)

    lines = [
        "=" * 80,
        f"Profile: {name}",
        "=" * 80,
    ]

    found = False
    if name in cfg.profiles and name not in cfg.batches:
        found = True
        p = cfg.profiles[name]
        lines.append("Type: Execution Profile ([profiles])")
        for k, v in asdict(p).items():
            if v is not None and v != [] and v != "":
                lines.append(f"  {k:<22}: {v}")

    if name in cfg.batches:
        found = True
        b = cfg.batches[name]
        lines.append("Type: Batch Profile ([batches])")
        for k, v in asdict(b).items():
            if v is not None and v != "":
                lines.append(f"  {k:<22}: {v}")

    if not found:
        available = sorted(set(cfg.profiles.keys()) | set(cfg.batches.keys()))
        avail_str = ", ".join(available) or "none defined"
        raise ConfigError(
            f"Profile '{name}' not found. Available profiles: {avail_str}"
        )

    lines.append("=" * 80)
    return "\n".join(lines)


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
        raise ConfigError("No --env given and config file has no default_env or default_profile set.")
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

    casa_root_key = next((k for k in ("casa_root", "casa_dir", "casa_ver") if k in table and table[k]), None)
    if not casa_root_key and "pixi_dir" not in table:
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
        resolved_casa = resolve_workspace_path(resolved_casa_root.rstrip("/"), effective_workspaces)
        if _safe_exists(resolved_casa):
            resolved_casa_root = str(resolved_casa)

    resolved_pixi_dir = table.get("pixi_dir")
    if resolved_pixi_dir and effective_workspaces:
        resolved_pixi = resolve_workspace_path(resolved_pixi_dir.rstrip("/"), effective_workspaces)
        if _safe_exists(resolved_pixi):
            resolved_pixi_dir = str(resolved_pixi)

    resolved_heuristics = (
        table.get("heuristics_dir")
        or table.get("heuristics_root")
        or table.get("pipe_dir")
        or table.get("pipe_ver")
        or table.get("pl_ver")
    )
    if resolved_heuristics and effective_workspaces and "{" not in resolved_heuristics:
        resolved_heur = resolve_workspace_path(resolved_heuristics.rstrip("/"), effective_workspaces)
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
            k: str(v)
            for k, v in table.items()
            if k
            not in (
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
            )
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
    if isinstance(env_spec, dict):
        name = env_spec.get("name", "unknown")
        casa_root_raw = (
            env_spec.get("casa_root")
            or env_spec.get("casa_dir")
            or env_spec.get("casa_ver")
            or ""
        )
        heuristics_raw = (
            env_spec.get("heuristics_dir")
            or env_spec.get("heuristics_root")
            or env_spec.get("pipe_dir")
            or env_spec.get("pipe_ver")
            or env_spec.get("pl_ver")
        )
        spec = EnvSpec(
            name=name,
            casa_root=casa_root_raw,
            branch=env_spec.get("branch", name),
            heuristics_dir=heuristics_raw,
            pixi_dir=env_spec.get("pixi_dir"),
            pixi_env=env_spec.get("pixi_env", "default"),
            extra_vars={
                k: str(v)
                for k, v in env_spec.items()
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
                )
            },
        )
    else:
        spec = env_spec

    paths = config.get("paths", {})
    workspaces = paths.get("workspaces", [])
    site = {**SITE_DEFAULTS, **config.get("site", {})}
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
            template = resolve_first_existing_path(template, workspaces=workspaces, user=user)
        resolved = os.path.expanduser(template.format(user=user))
        if var == "SCIPIPE_ROOTDIR" and subdir:
            resolved = str(Path(resolved) / subdir)
        try:
            os.makedirs(resolved, exist_ok=True)
        except OSError:
            pass
        env[var] = resolved

    # Discover casarundata / casadata / measurespath
    casadata_val = (
        paths.get("casarundata")
        if "casarundata" in paths and paths.get("casarundata") is not None
        else (
            paths.get("casadata")
            if "casadata" in paths and paths.get("casadata") is not None
            else paths.get("rundata")
        )
    )
    if casadata_val:
        if isinstance(casadata_val, (list, tuple)):
            casadata_val = resolve_first_existing_path(casadata_val, workspaces=workspaces)
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
    env["CASA_ENABLE_TELEMETRY"] = "true" if site["casa_enable_telemetry"] else "false"

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
        "CASA_ENABLE_TELEMETRY",
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
