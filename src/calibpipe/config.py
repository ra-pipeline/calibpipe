"""Configuration loading and CASA/pipeline environment construction for calibpipe.

Replaces the legacy env_core2.sh and personal pipeline_env_<name>.sh files with
structured, strongly-typed TOML configuration loading.
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


# Generic site defaults.
# These can be overridden in personal config.toml under the [site] table.
SITE_DEFAULTS: dict[str, Any] = {
    "java_home": os.environ.get("JAVA_HOME", "/usr/lib/jvm/default-java"),
    "pmr_home": "/opt/pipetools/latest",
    "acsdata": "/opt/acsdata",
    "datapacker_home": "/opt/datapacker/current",
    "flux_service_url": "https://almascience.org/sc/flux",
    "flux_service_url_backup": "https://asa.alma.cl/sc/flux",
    "casa_enable_telemetry": False,
    "submit_host": None,  # runbatch / batch guard; None = no restriction
}


class ConfigError(Exception):
    """Raised when configuration file or required keys are invalid or missing."""


@dataclass
class EnvSpec:
    """Specification for a specific CASA + pipeline environment."""

    name: str
    casa_root: str
    branch: str = ""
    heuristics_dir: str | None = None
    extra_vars: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Default the branch name to the environment name when omitted."""
        if not self.branch:
            self.branch = self.name


def find_config_path(cli_arg: str | Path | None = None) -> Path:
    """Resolve which TOML config file to use.

    Search order:
    1. Explicit CLI argument (--config=<path>)
    2. $CALIBPIPE_CONFIG environment variable
    3. config.toml in current working directory
    4. ~/.config/calibpipe/config.toml
    5. config.toml in project root (when running from source)

    Args:
        cli_arg: Explicit config path passed on the command line.

    Returns:
        Resolved path to the configuration file.

    Raises:
        ConfigError: If an explicit or environment-provided path does not
            exist, or no usable config file can be found.
    """
    if cli_arg:
        path = Path(cli_arg).expanduser().resolve()
        if not path.exists():
            raise ConfigError(f"--config file not found: {path}")
        return path

    env_path = os.environ.get("CALIBPIPE_CONFIG")
    if env_path:
        path = Path(env_path).expanduser().resolve()
        if not path.exists():
            raise ConfigError(f"$CALIBPIPE_CONFIG file not found: {path}")
        return path

    cwd_config = Path.cwd() / "config.toml"
    if cwd_config.exists():
        return cwd_config.resolve()

    user_config = Path.home() / ".config" / "calibpipe" / "config.toml"
    if user_config.exists():
        return user_config.resolve()

    # Look in repository root (up from src/calibpipe/config.py)
    repo_root_config = Path(__file__).resolve().parent.parent.parent / "config.toml"
    if repo_root_config.exists():
        return repo_root_config.resolve()

    # Legacy location: in same dir as config.py
    local_config = Path(__file__).resolve().parent / "config.toml"
    if local_config.exists():
        return local_config.resolve()

    raise ConfigError(
        "No config file found. Pass --config=<path>, set $CALIBPIPE_CONFIG, "
        "or create config.toml (see config.example.toml for the schema)."
    )


def load_config(path: str | Path) -> dict[str, Any]:
    """Parse a TOML configuration file.

    Args:
        path: Path to the TOML file.

    Returns:
        Parsed configuration mapping.

    Raises:
        ConfigError: If the file does not exist or cannot be parsed.
    """
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"Config file does not exist: {config_path}")
    try:
        with open(config_path, "rb") as fd:
            return tomllib.load(fd)
    except Exception as e:
        raise ConfigError(f"Failed to parse TOML config {config_path}: {e}") from e


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
    if "casa_root" not in table:
        raise ConfigError(f"[envs.{name}] is missing casa_root")

    return EnvSpec(
        name=name,
        casa_root=table["casa_root"],
        branch=table.get("branch", name),
        heuristics_dir=table.get("heuristics_dir"),
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
            casa_root=env_spec["casa_root"],
            branch=env_spec.get("branch", name),
            heuristics_dir=env_spec.get("heuristics_dir"),
        )
    else:
        spec = env_spec

    paths = config.get("paths", {})
    site = {**SITE_DEFAULTS, **config.get("site", {})}
    user = os.environ.get("USER", "")

    casa_root = spec.casa_root.rstrip("/")
    branch = spec.branch
    casa_path = f"{casa_root}/bin"

    env = os.environ.copy()
    env["CASA_ROOT"] = casa_root
    env["CASA_PATH"] = casa_path
    env["PIPE_BRANCH"] = branch

    if spec.heuristics_dir:
        heuristics = spec.heuristics_dir.format(casa_root=casa_root, branch=branch)
    else:
        heuristics_root = paths.get("heuristics_root")
        if heuristics_root:
            heuristics = f"{heuristics_root.rstrip('/')}/{branch}"
        else:
            # Fallback to parent directory of checkout or current working dir
            heuristics = str(Path(__file__).resolve().parent.parent.parent)

    env["SCIPIPE_HEURISTICS"] = heuristics
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
    for prepend in (casa_path, pmr_bin):
        if prepend in path_parts:
            path_parts.remove(prepend)
        path_parts.insert(0, prepend)
    env["PATH"] = ":".join(path_parts)

    env["MATPLOTLIBRC"] = str(Path.home() / ".casa" / "matplotlib")

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
    checks: list[tuple[str, str, str]] = [
        (
            "CASA_ROOT",
            env.get("CASA_ROOT", ""),
            "Please configure [envs.<name>].casa_root in config.toml to point to a valid CASA installation.",
        ),
        (
            "pmr_home",
            env.get("ACSROOT", ""),
            "Please configure [site].pmr_home in config.toml (or copy from notes/config.internal.example.toml for NAASC cluster).",
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
