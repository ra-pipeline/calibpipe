"""Configuration loading and CASA/pipeline environment construction for calibpipe.

Replaces the legacy env_core2.sh and personal pipeline_env_<name>.sh files with
structured, strongly-typed TOML configuration loading.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PathsConfig:
        return cls(
            scipipe_rootdir=str(data.get("scipipe_rootdir", "")),
            scipipe_logdir=str(data.get("scipipe_logdir", "")),
            pickle_dir=data.get("pickle_dir"),
            obscaldir=data.get("obscaldir"),
            aUdir=data.get("aUdir"),
            validation_dir=data.get("validation_dir"),
            heuristics_root=data.get("heuristics_root"),
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


@dataclass
class BatchConfig:
    """Slurm batch cluster resource submission defaults."""

    queue: str = "plwg"
    cores: int = 8
    mem: int = 248
    node: str = "1"
    mail_type: str = "ALL"
    # Optional Slurm directives — None means the directive is omitted entirely.
    walltime: str | None = None  # --time  (e.g. "24:00:00")
    nodelist: str | None = None  # --nodelist  (pin to a specific node)
    chdir: str | None = None  # --chdir  (Slurm working directory)
    cpus_per_task: int | None = (
        None  # --cpus-per-task  (for mpicasa: ntasks × cpus-per-task)
    )
    mem_per_cpu: str | None = None  # --mem-per-cpu  (mutually exclusive with mem)
    hint: str | None = None  # --hint  (e.g. "nomultithread")
    ntasks_per_core: int | None = None  # --ntasks-per-core  (e.g. 1 to disable HT)
    distribution: str | None = None  # --distribution  (e.g. "cyclic:cyclic")
    no_requeue: bool = True  # --no-requeue  (prevent silent resubmission)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchConfig:
        default_inst = cls()
        return cls(
            queue=str(data.get("queue", default_inst.queue)),
            cores=int(data.get("cores", default_inst.cores)),
            mem=int(data.get("mem", default_inst.mem)),
            node=str(data.get("node", default_inst.node)),
            mail_type=str(data.get("mail_type", default_inst.mail_type)),
            walltime=data.get("walltime", default_inst.walltime),
            nodelist=data.get("nodelist", default_inst.nodelist),
            chdir=data.get("chdir", default_inst.chdir),
            cpus_per_task=_int_or_none(
                data.get("cpus_per_task", default_inst.cpus_per_task)
            ),
            mem_per_cpu=data.get("mem_per_cpu", default_inst.mem_per_cpu),
            hint=data.get("hint", default_inst.hint),
            ntasks_per_core=_int_or_none(
                data.get("ntasks_per_core", default_inst.ntasks_per_core)
            ),
            distribution=data.get("distribution", default_inst.distribution),
            no_requeue=bool(data.get("no_requeue", default_inst.no_requeue)),
        )


@dataclass
class RunConfig:
    """Driver defaults for single-run execution."""

    recipe: str = "calimage"
    ncores: int = 8
    loglevel: str = "debug"
    useresume: bool = False
    symlink_shortcuts: bool = True
    log2term: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunConfig:
        default_inst = cls()
        return cls(
            recipe=str(data.get("recipe", default_inst.recipe)),
            ncores=int(data.get("ncores", default_inst.ncores)),
            loglevel=str(data.get("loglevel", default_inst.loglevel)),
            useresume=bool(data.get("useresume", default_inst.useresume)),
            symlink_shortcuts=bool(
                data.get("symlink_shortcuts", default_inst.symlink_shortcuts)
            ),
            log2term=bool(data.get("log2term", default_inst.log2term)),
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


class CalibpipeConfig(dict):
    """Strongly-typed, single source of truth configuration object for calibpipe.

    Provides typed attribute access (e.g. `cfg.site.use_custom_rcdir`, `cfg.paths.scipipe_rootdir`)
    while maintaining full dict-mapping compatibility (e.g. `cfg['site']`, `cfg.get('paths')`).
    """

    def __init__(
        self,
        default_env: str = "main",
        paths: PathsConfig | None = None,
        envs: dict[str, EnvSpec] | None = None,
        site: SiteConfig | None = None,
        batch: BatchConfig | None = None,
        run: RunConfig | None = None,
        raw_dict: dict[str, Any] | None = None,
        loaded_layers: Sequence[str | Path] | None = None,
    ) -> None:
        super().__init__(raw_dict or {})
        self.default_env = default_env
        self.paths = paths or PathsConfig()
        self.envs = envs or {}
        self.site = site or SiteConfig()
        self.batch = batch or BatchConfig()
        self.run = run or RunConfig()
        self.loaded_layers: list[Path] = [Path(p) for p in loaded_layers] if loaded_layers else []

        # Synchronize dictionary keys for backwards compatibility
        self["default_env"] = self.default_env
        self["paths"] = asdict(self.paths)
        self["envs"] = {
            k: asdict(v) if is_dataclass(v) else v for k, v in self.envs.items()
        }
        self["site"] = asdict(self.site)
        self["batch"] = asdict(self.batch)
        self["run"] = asdict(self.run)

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        loaded_layers: Sequence[str | Path] | None = None,
    ) -> CalibpipeConfig:
        """Construct a strongly-typed CalibpipeConfig from a parsed TOML dictionary."""
        default_env = str(data.get("default_env", "main"))
        paths = PathsConfig.from_dict(data.get("paths", {}))
        site = SiteConfig.from_dict(data.get("site", {}))
        batch = BatchConfig.from_dict(data.get("batch", {}))
        run = RunConfig.from_dict(data.get("run", {}))

        envs: dict[str, EnvSpec] = {}
        for env_name, env_data in data.get("envs", {}).items():
            if isinstance(env_data, dict):
                envs[env_name] = EnvSpec(
                    name=env_name,
                    casa_root=env_data.get("casa_root", ""),
                    branch=env_data.get("branch", env_name),
                    heuristics_dir=env_data.get("heuristics_dir"),
                    pixi_dir=env_data.get("pixi_dir"),
                    pixi_env=env_data.get("pixi_env", "default"),
                    extra_vars={
                        k: str(v)
                        for k, v in env_data.items()
                        if k
                        not in (
                            "casa_root",
                            "branch",
                            "heuristics_dir",
                            "pixi_dir",
                            "pixi_env",
                        )
                    },
                )

        return cls(
            default_env=default_env,
            paths=paths,
            envs=envs,
            site=site,
            batch=batch,
            run=run,
            raw_dict=data,
            loaded_layers=loaded_layers,
        )


@dataclass
class ResolvedRunOptions:
    """Fully resolved execution options for a single calibpipe run."""

    mous: str
    env_name: str
    recipe: str
    ncores: int
    loglevel: str
    useresume: bool
    use_custom_rcdir: bool
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


def resolve_run_options(
    config: CalibpipeConfig | dict[str, Any],
    cli_opts: Any,
) -> ResolvedRunOptions:
    """Resolve runtime options by cascading CLI flags over config.toml over defaults.

    Precedence: CLI flag > config.toml ([run] / [site]) > built-in defaults.
    """
    if isinstance(config, CalibpipeConfig):
        cfg = config
    else:
        cfg = CalibpipeConfig.from_dict(config)

    # Custom rcdir: CLI flag > [site].use_custom_rcdir > default
    if getattr(cli_opts, "custom_rcdir", None) is not None:
        use_custom_rcdir = bool(cli_opts.custom_rcdir)
    else:
        use_custom_rcdir = cfg.site.use_custom_rcdir

    if getattr(cli_opts, "symlink_shortcuts", None) is not None:
        symlink_shortcuts = bool(cli_opts.symlink_shortcuts)
    else:
        symlink_shortcuts = cfg.run.symlink_shortcuts

    if getattr(cli_opts, "log2term", None) is not None:
        log2term = bool(cli_opts.log2term)
    else:
        log2term = cfg.run.log2term or cfg.site.log2term

    recipe = getattr(cli_opts, "recipe", None) or cfg.run.recipe
    ncores = getattr(cli_opts, "ncores", None)
    if ncores is None:
        ncores = cfg.run.ncores
    loglevel = getattr(cli_opts, "loglevel", None) or cfg.run.loglevel
    useresume = getattr(cli_opts, "useresume", False) or cfg.run.useresume
    env_name = getattr(cli_opts, "env", None) or cfg.default_env

    return ResolvedRunOptions(
        mous=getattr(cli_opts, "mous", "") or "",
        env_name=env_name,
        recipe=recipe,
        ncores=int(ncores),
        loglevel=loglevel,
        useresume=bool(useresume),
        use_custom_rcdir=use_custom_rcdir,
        symlink_shortcuts=symlink_shortcuts,
        log2term=log2term,
        flag_dir=getattr(cli_opts, "flag", None),
        ppr=getattr(cli_opts, "ppr", None),
        subdir=getattr(cli_opts, "subdir", None),
        onlysemipass=getattr(cli_opts, "onlysemipass", "") or "",
        verbose=bool(getattr(cli_opts, "verbose", False)),
        vis=list(getattr(cli_opts, "vis", []) or []),
        procedure=getattr(cli_opts, "procedure", "") or "",
        script=getattr(cli_opts, "script", "") or "",
        cmd=getattr(cli_opts, "cmd", "") or "",
        vla=bool(getattr(cli_opts, "vla", False)),
        workdir=getattr(cli_opts, "workdir", "") or "",
        interactive=bool(getattr(cli_opts, "interactive", False)),
    )


@dataclass
class ResolvedBatchOptions:
    """Fully resolved options for a Slurm batch submission."""

    pipefile: Path
    env_name: str
    queue: str
    cores: int
    mem: int
    node: str
    mail_type: str
    outfile: str | None = None
    errfile: str | None = None
    extra_args: list[str] = field(default_factory=list)
    # Optional Slurm directives
    walltime: str | None = None
    nodelist: str | None = None
    chdir: str | None = None
    cpus_per_task: int | None = None
    mem_per_cpu: str | None = None
    hint: str | None = None
    ntasks_per_core: int | None = None
    distribution: str | None = None
    no_requeue: bool = True


def resolve_batch_options(
    config: CalibpipeConfig | dict[str, Any],
    cli_args: Any,
) -> ResolvedBatchOptions:
    """Resolve batch options by cascading CLI flags over config.toml [batch] over defaults."""
    if isinstance(config, CalibpipeConfig):
        cfg = config
    else:
        cfg = CalibpipeConfig.from_dict(config)

    env_name = getattr(cli_args, "env", None) or cfg.default_env

    cli_queue = getattr(cli_args, "queue", None)
    queue = cli_queue if cli_queue else cfg.batch.queue

    cores = getattr(cli_args, "cores", None)
    if cores is None:
        cores = cfg.batch.cores

    mem = getattr(cli_args, "mem", None)
    if mem is None:
        mem = cfg.batch.mem

    node = getattr(cli_args, "node", None) or cfg.batch.node
    mail_type = getattr(cli_args, "mail_type", None) or cfg.batch.mail_type

    # Optional directives — CLI overrides config, then falls back to None/default.
    def _cli_or_cfg(attr: str, cfg_val: Any) -> Any:
        v = getattr(cli_args, attr, None)
        return v if v is not None else cfg_val

    walltime = _cli_or_cfg("walltime", cfg.batch.walltime)
    nodelist = _cli_or_cfg("nodelist", cfg.batch.nodelist)
    chdir = _cli_or_cfg("chdir", cfg.batch.chdir)
    cpus_per_task = _int_or_none(_cli_or_cfg("cpus_per_task", cfg.batch.cpus_per_task))
    mem_per_cpu = _cli_or_cfg("mem_per_cpu", cfg.batch.mem_per_cpu)
    hint = _cli_or_cfg("hint", cfg.batch.hint)
    ntasks_per_core = _int_or_none(
        _cli_or_cfg("ntasks_per_core", cfg.batch.ntasks_per_core)
    )
    distribution = _cli_or_cfg("distribution", cfg.batch.distribution)

    # no_requeue: CLI flag takes precedence; default True (safe default)
    cli_no_requeue = getattr(cli_args, "no_requeue", None)
    no_requeue = cli_no_requeue if cli_no_requeue is not None else cfg.batch.no_requeue

    return ResolvedBatchOptions(
        pipefile=Path(cli_args.pipefile),
        env_name=env_name,
        queue=queue,
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
    no_site_env = os.environ.get("CALIBPIPE_NO_SITE_CONFIG", "").lower() in ("1", "true", "yes")
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
            f"  Queue:           {cfg.batch.queue}",
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

    lines.extend(
        [
            "",
            "Pipeline Run Defaults ([run]):",
            f"  Recipe:          {cfg.run.recipe}",
            f"  Cores:           {cfg.run.ncores}",
            f"  Log Level:       {cfg.run.loglevel}",
            f"  Use Resume:      {cfg.run.useresume}",
            "=" * 80,
        ]
    )
    return "\n".join(lines)


def resolve_env(config: dict[str, Any], env_name: str | None = None) -> EnvSpec:
    """Resolve a named CASA environment from the loaded config.

    Args:
        config: Parsed TOML configuration dictionary.
        env_name: Explicit environment name. If omitted, `default_env` is used.

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
    if not name:
        raise ConfigError("No --env given and config file has no default_env set.")
    if name not in envs:
        available = ", ".join(sorted(envs))
        raise ConfigError(f"No [envs.{name}] in config file. Available: {available}")

    table = dict(envs[name])
    if "casa_root" not in table and "pixi_dir" not in table:
        raise ConfigError(
            f"[envs.{name}] must specify either 'casa_root' (monolithic CASA) "
            f"or 'pixi_dir' (Pixi modular CASA)."
        )

    return EnvSpec(
        name=name,
        casa_root=table.get("casa_root", ""),
        branch=table.get("branch", name),
        heuristics_dir=table.get("heuristics_dir"),
        pixi_dir=table.get("pixi_dir"),
        pixi_env=table.get("pixi_env", "default"),
        extra_vars={
            k: str(v)
            for k, v in table.items()
            if k
            not in ("casa_root", "branch", "heuristics_dir", "pixi_dir", "pixi_env")
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
        spec = EnvSpec(
            name=name,
            casa_root=env_spec.get("casa_root", ""),
            branch=env_spec.get("branch", name),
            heuristics_dir=env_spec.get("heuristics_dir"),
            pixi_dir=env_spec.get("pixi_dir"),
            pixi_env=env_spec.get("pixi_env", "default"),
            extra_vars={
                k: str(v)
                for k, v in env_spec.items()
                if k
                not in (
                    "name",
                    "casa_root",
                    "branch",
                    "heuristics_dir",
                    "pixi_dir",
                    "pixi_env",
                )
            },
        )
    else:
        spec = env_spec

    paths = config.get("paths", {})
    site = {**SITE_DEFAULTS, **config.get("site", {})}
    user = os.environ.get("USER", "")

    branch = spec.branch
    env = os.environ.copy()
    env["PIPE_BRANCH"] = branch

    if spec.is_pixi:
        pixi_dir = (spec.pixi_dir or "").rstrip("/")
        env["PIXI_DIR"] = pixi_dir
        env["PIXI_ENV"] = spec.pixi_env
        env["CASA_ROOT"] = pixi_dir

        if spec.heuristics_dir:
            heuristics = spec.heuristics_dir.format(pixi_dir=pixi_dir, branch=branch)
        else:
            heuristics_root = paths.get("heuristics_root")
            if heuristics_root:
                heuristics = f"{heuristics_root.rstrip('/')}/{branch}"
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
        casa_root = spec.casa_root.rstrip("/")
        casa_path = f"{casa_root}/bin"
        env["CASA_ROOT"] = casa_root
        env["CASA_PATH"] = casa_path

        if spec.heuristics_dir:
            heuristics = spec.heuristics_dir.format(casa_root=casa_root, branch=branch)
        else:
            heuristics_root = paths.get("heuristics_root")
            if heuristics_root:
                heuristics = f"{heuristics_root.rstrip('/')}/{branch}"
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
        resolved = template.format(user=user)
        if var == "SCIPIPE_ROOTDIR" and subdir:
            resolved = str(Path(resolved) / subdir)
        try:
            os.makedirs(resolved, exist_ok=True)
        except OSError:
            pass
        env[var] = resolved

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
        prepend_paths.insert(0, f"{spec.casa_root.rstrip('/')}/bin")
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
) -> list[str]:
    """Verify that key executable and support directories exist on disk.

    If any path is unreachable, issues an actionable warning (or raises ConfigError
    if strict=True), advising the user how to configure it in config.toml.

    Args:
        env: Resolved environment mapping from build_environment.
        site_config: Optional [site] table from config.toml.
        strict: If True, raises ConfigError on the first unreachable path instead of warning.

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
