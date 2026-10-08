"""calibpipe driver: Orchestrates execution of ALMA Science Pipeline runs.

Decomposes and modernizes the legacy calibPipeIF.py script.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from calibpipe import config as envconfig
from calibpipe.steps.staging import (
    find_dirs,
    find_files,
    rotate_directory,
    stage_ancillary_files,
    stage_flags_and_wvr,
)
from calibpipe.templates import render_template

# Expose modules/functions at module level for compatibility with existing tests/mocks
argv = sys.argv
getoutput = subprocess.getoutput


def find(pattern: str, path: str | Path) -> list[str]:
    """Find files matching pattern under path (following symlinks)."""
    return find_files(pattern, path)


def finddir(pattern: str, path: str | Path) -> list[str]:
    """Find directories matching pattern under path (following symlinks)."""
    return find_dirs(pattern, path)


class RunLogger:
    """Logger tracking command execution and formatted outputs for a pipeline run."""

    def __init__(self, filename: str, print_flag: bool = False) -> None:
        """Open a timestamped log file for a pipeline run."""
        now = datetime.now().isoformat().replace(":", "-")
        self.filename = filename
        self.print_flag = print_flag
        self.fd = open(f"{filename}.{now}.log", "w", encoding="utf-8")

    def __enter__(self) -> RunLogger:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def log(self, text: str) -> None:
        """Write timestamped text to the log file and optional stdout."""
        now = datetime.now().isoformat()
        for line in str(text).split("\n"):
            self.fd.write(f"{now}: {line}\n")
            if self.print_flag:
                print(f"{now}: {line}")
            self.fd.flush()

    def run(self, command: str) -> int:
        """Run a shell command and stream combined output into the log."""
        self.log(f"running '{command}':\n\n")
        p = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        if p.stdout:
            while True:
                line = p.stdout.readline()
                if not line:
                    break
                self.log(line.rstrip())
        p.wait()
        if p.returncode > 0:
            self.log(f"nonzero exit code = {p.returncode}")
        return p.returncode

    def runquiet(self, command: str) -> int:
        """Run a shell command without streaming stdout line by line."""
        self.log(f"running '{command}' quietly.")
        retcode = subprocess.call(command, shell=True)
        if retcode > 0:
            self.log(f"nonzero exit code = {retcode}")
        return retcode

    def close(self) -> None:
        """Close the underlying log file handle."""
        self.fd.close()


def write_casa_config(
    rcdir: Path,
    site_config: dict[str, Any] | None = None,
    log2term: bool = False,
    casadata: str | None = None,
    datapath: list[str] | None = None,
    rundata: list[str] | None = None,
    rundata_specified: bool = False,
) -> Path:
    """Generate isolated config.py for CASA inside rcdir.

    Args:
        rcdir: Custom runtime configuration directory.
        site_config: Optional [site] configuration mapping.
        log2term: Whether to mirror CASA logs to terminal/stdout.
        casadata: Optional resolved casadata/measurespath path.
        datapath: Optional list of directories for datapath.
        rundata: Optional list of directories for rundata candidates.
        rundata_specified: Whether rundata was explicitly specified by profile/CLI.

    Returns:
        Path to the generated config.py file.
    """
    site = site_config or {}
    telemetry = "True" if site.get("casa_enable_telemetry", False) else "False"
    log2term_str = "True" if (site.get("log2term", False) or log2term) else "False"
    casadata_val = casadata or site.get("casadata") or os.environ.get("CASADATA", "")
    content = render_template(
        "casa_config.py.in",
        telemetry=telemetry,
        log2term=log2term_str,
        casadata=casadata_val,
        datapath=repr(datapath) if datapath else "None",
        rundata=repr(rundata) if rundata else ("[]" if rundata_specified else "None"),
        rundata_specified="True" if rundata_specified else "False",
    )
    config_file = rcdir / "config.py"
    config_file.write_text(content, encoding="utf-8")
    return config_file


def write_casa_startup(
    rcdir: Path,
) -> Path:
    """Generate isolated startup.py for CASA inside rcdir.

    Ensures pipeline heuristics and executeppr (eppr) are available in the CASA session.

    Args:
        rcdir: Custom runtime configuration directory.

    Returns:
        Path to the generated startup.py file.
    """
    content = render_template("casa_startup.py.in")
    startup_file = rcdir / "startup.py"
    startup_file.write_text(content, encoding="utf-8")
    return startup_file


def write_ipython_config(
    rcdir: Path,
    autoreload: bool = True,
) -> Path:
    """Generate isolated ipython_config.py for CASA under rcdir.

    Args:
        rcdir: Custom runtime configuration directory.
        autoreload: Whether to configure %load_ext autoreload and %autoreload 2.

    Returns:
        Path to the generated ipython_config.py file.
    """
    ipython_dir = rcdir / "ipython" / "profile_default"
    ipython_dir.mkdir(parents=True, exist_ok=True)
    ipython_file = ipython_dir / "ipython_config.py"

    lines = [
        "# c = get_config()",
        "c.HistoryManager.enabled = False",
    ]
    if autoreload:
        lines.extend([
            "c.InteractiveShellApp.exec_lines = [",
            "    '%load_ext autoreload',",
            "    '%autoreload 2',",
            "]",
        ])
    ipython_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ipython_file


def get_casa_rcdir_args(casaroot: str | Path, rcdir: Path) -> list[str]:
    """Determine CASA CLI options for custom rcdir (casaconfig vs legacy --rcdir).

    Args:
        casaroot: CASA installation root directory.
        rcdir: Custom runtime configuration directory.

    Returns:
        List of command-line arguments to pass to CASA.
    """
    casa_path = Path(casaroot)
    has_casaconfig = False
    if casa_path.exists():
        # Check common library subpaths first to avoid exhaustive recursive scan on network filesystems
        candidate_subdirs = [
            casa_path / "lib" / "py",
            casa_path / "lib",
            casa_path,
        ]
        for search_root in candidate_subdirs:
            if search_root.is_dir():
                if next(search_root.glob("**/casaconfig"), None) is not None:
                    has_casaconfig = True
                    break
    else:
        # Default to modern casaconfig flags for mocked or nonexistent paths
        has_casaconfig = True

    if has_casaconfig:
        return [
            "--cachedir",
            str(rcdir),
            "--configfile",
            str(rcdir / "config.py"),
            "--startupfile",
            str(rcdir / "startup.py"),
        ]
    return ["--rcdir", str(rcdir)]


def link_weblog(
    ppmr_dir: str | Path,
    working_dir: str | Path,
    log_message: Callable[[str], None] | None = None,
) -> Path | None:
    """Create a convenience symlink at project root to the latest pipeline weblog HTML directory.

    Scans working_dir (and products/) for pipeline-*/html directories and creates:
    - ppmr_dir/weblog -> relative path to .../pipeline-*/html

    Args:
        ppmr_dir: Project root directory.
        working_dir: Pipeline working directory.
        log_message: Optional logging callback.

    Returns:
        Path to the created symlink, or None if no weblog directory was found.
    """
    root = Path(ppmr_dir).resolve()
    work = Path(working_dir).resolve()
    html_dirs = sorted(
        work.glob("pipeline-*/html"),
        key=lambda p: (p.stat().st_mtime if p.exists() else 0),
    )
    if not html_dirs:
        products_dir = work.parent / "products"
        if products_dir.is_dir():
            html_dirs = sorted(
                products_dir.glob("pipeline-*/html"),
                key=lambda p: (p.stat().st_mtime if p.exists() else 0),
            )
    if not html_dirs:
        return None

    latest_weblog = html_dirs[-1]
    link_path = root / "weblog"
    if link_path.exists() and not link_path.is_symlink():
        return None

    try:
        rel_target = os.path.relpath(latest_weblog, root)
        if link_path.is_symlink():
            if os.readlink(link_path) == rel_target:
                return link_path
            link_path.unlink()
        link_path.symlink_to(rel_target)
        if log_message:
            log_message(f"created weblog shortcut {link_path} -> {rel_target}")
        return link_path
    except OSError as e:
        if log_message:
            log_message(f"warning: could not create weblog symlink {link_path}: {e}")
        return None


def create_project_symlinks(
    ppmr_dir: str | Path,
    working_dir: str | Path,
    log_message: Callable[[str], None] | None = None,
) -> list[Path]:
    """Create convenience symlinks in the project root pointing to OUS subdirectories.

    Links created in ppmr_dir (if working_dir is nested):
    - ppmr_dir/working -> relative path to working
    - ppmr_dir/products -> relative path to products
    - ppmr_dir/rawdata -> relative path to rawdata
    - ppmr_dir/weblog -> relative path to latest pipeline-*/html (if present)

    Args:
        ppmr_dir: Project root directory (e.g. root/<project_code>_<timestamp>).
        working_dir: Deeply nested pipeline working directory.
        log_message: Optional logging callback.

    Returns:
        List of created symlink paths.
    """
    root = Path(ppmr_dir).resolve()
    work = Path(working_dir).resolve()
    created: list[Path] = []

    # If working directory is directly inside or equal to root, no nesting to shortcut
    if work == root or work.parent == root:
        return created

    try:
        work.relative_to(root)
    except ValueError:
        return created

    ous_dir = work.parent
    targets = {
        "working": work,
        "products": ous_dir / "products",
        "rawdata": ous_dir / "rawdata",
    }

    for name, target in targets.items():
        link_path = root / name
        # If destination exists as a real file/directory (not symlink), do not overwrite
        if link_path.exists() and not link_path.is_symlink():
            continue
        try:
            rel_target = os.path.relpath(target, root)
            if link_path.is_symlink():
                if os.readlink(link_path) == rel_target:
                    created.append(link_path)
                    continue
                link_path.unlink()
            link_path.symlink_to(rel_target)
            created.append(link_path)
            if log_message:
                log_message(f"created convenience symlink {link_path} -> {rel_target}")
        except OSError as e:
            if log_message:
                log_message(f"warning: could not create symlink {link_path}: {e}")

    # Also link weblog if already generated
    wl = link_weblog(ppmr_dir, working_dir, log_message)
    if wl and wl not in created:
        created.append(wl)

    return created


def build_parser() -> argparse.ArgumentParser:
    """Build the parser for the `calibpipe run` workflow.

    Returns:
        Configured argument parser for single-run execution.
    """
    p = argparse.ArgumentParser(
        description="Assists in the execution of the interferometric pipeline.",
        add_help=True,
    )
    p.add_argument("--mous", default="", help="MOUS status UID to execute")
    p.add_argument(
        "--flag", default="", help="Path to dir with flags/templates to re-use"
    )
    p.add_argument(
        "--PPR", default="", dest="ppr", help="Path to a PPR xml file to re-use"
    )
    p.add_argument(
        "--valid", action="store_true", help="Use validation start directory"
    )
    p.add_argument("--config", help="Path to TOML config file")
    p.add_argument(
        "--no-site-config",
        action="store_true",
        default=False,
        help="Do not load site-level configuration",
    )
    p.add_argument("--env", help="[envs.<name>] table to use from config")
    p.add_argument("--subdir", help="Extra subdirectory appended to SCIPIPE_ROOTDIR")
    p.add_argument(
        "--print-env", action="store_true", help="Print resolved environment and exit"
    )
    p.add_argument(
        "--recipe",
        default=None,
        choices=[
            "cal",
            "image",
            "image_selfcal",
            "image_selfcal_nocube",
            "calimage",
            "calimage_selfcal",
            "calsurvey",
        ],
        help="Pipeline recipe to execute (default: from [run].recipe or calimage)",
    )
    p.add_argument(
        "--ebwfile", default="", help="File with effective bandwidth information"
    )
    p.add_argument(
        "--ncores",
        type=int,
        default=None,
        help="Number of cores (default: from [run].ncores or 8)",
    )
    p.add_argument(
        "--loglevel",
        default=None,
        help="PL log level (default: from [run].loglevel or debug)",
    )
    p.add_argument(
        "--pickle", action="store_true", help="Run picklePipeRun upon completion"
    )
    p.add_argument(
        "--useresume",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use break/resume instead of two contexts",
    )
    p.add_argument(
        "--legacy", action="store_true", help="Run fixsyscaltimes, fixplanets"
    )
    p.add_argument("--aUdir", default="", help="Set analysisUtils directory path")
    p.add_argument(
        "--usegetalmaflux", action="store_true", help="Use aU.getALMAFluxCsv"
    )
    p.add_argument(
        "--staticobscal",
        action="store_true",
        help="Use previous flux.csv from obscaldir",
    )
    p.add_argument(
        "--oldcont", action="store_true", help="Use previous cont.dat from obscaldir"
    )
    p.add_argument(
        "--noupdate", action="store_true", default=True, help="Don't update CASA IERS"
    )
    p.add_argument(
        "--verbose", action="store_true", help="Verbose subprocess output logging"
    )
    p.add_argument("--semipass", default="", help="Comma-delimited EB UIDs to process")
    p.add_argument("--onlysemipass", default="", help="Process only the specified EBs")
    p.add_argument(
        "--custom-rcdir",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="custom_rcdir",
        help="Generate isolated CASA rcdir (config.py/startup.py) in run tree (default: from [site].use_custom_rcdir)",
    )
    p.add_argument(
        "--symlink-shortcuts",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="symlink_shortcuts",
        help=(
            "Create convenience symlinks (working, products, rawdata, weblog) in project root "
            "(default: from [run].symlink_shortcuts)"
        ),
    )
    p.add_argument(
        "--log2term",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="log2term",
        help=(
            "Mirror CASA log messages to terminal/stdout in real time "
            "(default: from [run].log2term / [site].log2term)"
        ),
    )
    # Alternative direct execution modes (pcasa-style decoupling from PMR)
    p.add_argument(
        "--vis",
        nargs="+",
        default=[],
        help="One or more MeasurementSet or ASDM paths to process directly via recipe reducer",
    )
    p.add_argument(
        "--procedure",
        default="",
        help="Pipeline procedure XML filename or path (default: resolved from --recipe)",
    )
    p.add_argument(
        "--script",
        default="",
        help="Path to Python/CASA script to execute directly under the CASA environment",
    )
    p.add_argument(
        "--cmd",
        default="",
        help="Inline Python command string to execute in CASA (e.g. pytest invocation)",
    )
    p.add_argument(
        "--vla",
        action="store_true",
        default=False,
        help="Use VLA pipeline interface (executevlappr) instead of ALMA (executeppr)",
    )
    p.add_argument(
        "--workdir",
        default="",
        help="Working directory for execution (default: working/ or current directory)",
    )
    p.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        default=False,
        help="Launch an interactive CASA shell session",
    )
    # Hardware & MPI runtime tuning
    p.add_argument(
        "--omp-num-threads",
        type=int,
        default=None,
        dest="omp_num_threads",
        help="Set OMP_NUM_THREADS environment variable",
    )
    p.add_argument(
        "--openblas-num-threads",
        type=int,
        default=None,
        dest="openblas_num_threads",
        help="Set OPENBLAS_NUM_THREADS environment variable",
    )
    p.add_argument(
        "--omp-max-threads",
        type=int,
        default=None,
        dest="omp_max_threads",
        help="Restrict maximum OpenMP threads via casalog.ompSetNumThreads",
    )
    p.add_argument(
        "--mem-frac",
        type=float,
        default=None,
        dest="mem_frac",
        help="Set CASA memory fraction limit via casalog.setMemoryFraction (0.0 - 1.0)",
    )
    p.add_argument(
        "--oversubscribe",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="oversubscribe",
        help="Allow OpenMPI process oversubscription (--oversubscribe / --no-oversubscribe)",
    )
    p.add_argument(
        "--bind-to",
        type=str,
        default=None,
        dest="bind_to",
        help="OpenMPI process binding policy (e.g. none, core, socket)",
    )
    p.add_argument(
        "--map-by",
        type=str,
        default=None,
        dest="map_by",
        help="OpenMPI process mapping policy (e.g. core, socket, node)",
    )

    # Telemetry and resource profiling
    p.add_argument(
        "--psrecord",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="psrecord",
        help="Profile execution using psrecord CLI utility (--psrecord / --no-psrecord)",
    )
    p.add_argument(
        "--memstats",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="memstats",
        help="Enable pipeline memory statistics tracking (--memstats / --no-memstats)",
    )
    p.add_argument(
        "--pl-psrecord",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="pl_psrecord",
        help="Enable pipeline internal psrecord profiling (--pl-psrecord / --no-pl-psrecord)",
    )

    # Ancillary staging & safe backup rotation
    p.add_argument(
        "--backup",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="backup",
        help="Rotate existing working directories to timestamped _backup_ directories before execution",
    )
    p.add_argument(
        "--cont-dat",
        default=None,
        dest="cont_dat",
        help="Path to cont.dat file to stage into working directory",
    )
    p.add_argument(
        "--jyperk-csv",
        default=None,
        dest="jyperk_csv",
        help="Path to jyperk.csv file to stage into working directory",
    )
    p.add_argument(
        "--parameter-list",
        default=None,
        dest="parameter_list",
        help="Path to parameter.list file to stage into working directory",
    )
    p.add_argument(
        "--ancillary",
        nargs="+",
        default=None,
        dest="ancillary",
        help="Additional ancillary files or directories to stage into working directory",
    )
    p.add_argument(
        "--datapath",
        nargs="+",
        default=None,
        dest="datapath",
        help="One or more directories to search for CASA runtime data, ephemerides, or testdata",
    )
    p.add_argument(
        "--rundata",
        nargs="+",
        default=None,
        dest="rundata",
        help="One or more directories to search for CASA measures/geodetic tables (or 'none' to disable)",
    )
    p.add_argument(
        "-p",
        "--profile",
        default=None,
        dest="profile",
        help="Named execution profile from [profiles.<name>]",
    )
    p.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="dry_run",
        help="Simulate execution without running subprocess commands (--dry-run / --no-dry-run)",
    )
    p.add_argument(
        "--autoreload",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="autoreload",
        help="Enable IPython %autoreload 2 for interactive CASA sessions (--autoreload / --no-autoreload)",
    )
    return p


def build_piperun_preamble(run_opts: envconfig.ResolvedRunOptions) -> str:
    """Generate Python preamble code for hardware tuning and telemetry.

    Args:
        run_opts: Resolved runtime execution options.

    Returns:
        Formatted Python code string to prepend to piperun scripts.
    """
    preamble_lines: list[str] = []

    has_casalog_tuning = (
        run_opts.omp_max_threads is not None or run_opts.mem_frac is not None
    )
    if has_casalog_tuning:
        preamble_lines.append("try:")
        preamble_lines.append("    from casatasks import casalog")
        if run_opts.omp_max_threads is not None:
            preamble_lines.append(
                f"    if casalog.ompGetNumThreads() > {run_opts.omp_max_threads}:"
            )
            preamble_lines.append(
                f"        casalog.ompSetNumThreads({run_opts.omp_max_threads})"
            )
        if run_opts.mem_frac is not None:
            preamble_lines.append(
                f"    casalog.setMemoryFraction({run_opts.mem_frac})"
            )
        preamble_lines.append("except Exception as exc:")
        preamble_lines.append(
            "    print(f'Warning: Failed to apply casalog tuning: {exc}')"
        )

    if run_opts.memstats or run_opts.pl_psrecord:
        preamble_lines.append("try:")
        preamble_lines.append("    import pipeline.infrastructure.utils as utils")
        if run_opts.memstats:
            preamble_lines.append("    utils.enable_memstats()")
        if run_opts.pl_psrecord:
            preamble_lines.append("    utils.enable_psrecord()")
        preamble_lines.append("except Exception as exc:")
        preamble_lines.append(
            "    print(f'Warning: Failed to enable pipeline telemetry: {exc}')"
        )

    return "\n".join(preamble_lines) + ("\n" if preamble_lines else "")


def wrap_cmd(
    inner_cmd: str,
    workdir: Path,
    rec_prefix: str,
    run_opts: envconfig.ResolvedRunOptions,
    logger_fn: Callable[[str], None] | None = None,
) -> str:
    """Wrap a CASA command with xvfb-run and optionally psrecord resource profiling.

    Args:
        inner_cmd: Base command without xvfb-run prefix.
        workdir: Working directory of execution.
        rec_prefix: Base filename path for .rec and .rec.png telemetry files.
        run_opts: Resolved runtime options.
        logger_fn: Optional logging function for diagnostic messages.

    Returns:
        Full shell command string ready for execution.
    """
    if run_opts.psrecord:
        psrecord_exe = shutil.which("psrecord")
        if psrecord_exe:
            quoted_cmd = shlex.quote(inner_cmd)
            rec_file = f"{rec_prefix}.rec"
            plot_file = f"{rec_prefix}.rec.png"
            extra_flags = ["--include-children", "--include-io", "--include-cache"]
            try:
                help_out = subprocess.run([psrecord_exe, "--help"], capture_output=True, text=True, check=False).stdout
                if "--use-timestamp" in help_out:
                    extra_flags.append("--use-timestamp")
            except Exception:
                pass
            flags_str = " ".join(extra_flags)
            return (
                f"xvfb-run -a {psrecord_exe} {quoted_cmd} "
                f"--log {rec_file} {flags_str} "
                f"--interval 2 --plot {plot_file}"
            )
        msg = "psrecord requested but 'psrecord' executable not found in PATH; running without psrecord."
        if logger_fn:
            logger_fn(msg)
        else:
            print(f"WARNING: {msg}", file=sys.stderr)

    return f"xvfb-run -a {inner_cmd}"


def build_casarun_prefix(
    run_opts: envconfig.ResolvedRunOptions,
    env_spec: envconfig.EnvironmentSpec,
    env: dict[str, str],
    site_cfg: dict[str, Any],
) -> tuple[str, str]:
    """Assemble base CASA executable command prefix.

    Returns:
        tuple of (casarun, casaroot)
    """
    if env_spec.is_pixi:
        pixi_bin = site_cfg.get("pixi_bin") or shutil.which("pixi") or "pixi"
        pixi_dir = env_spec.pixi_dir
        manifest_file = Path(pixi_dir) / "pyproject.toml"
        if not manifest_file.exists():
            manifest_file = Path(pixi_dir) / "pixi.toml"
        manifest_arg = (
            f"--manifest-path {manifest_file}"
            if manifest_file.exists()
            else f"--manifest-path {pixi_dir}"
        )
        env_arg = (
            f"-e {env_spec.pixi_env}"
            if env_spec.pixi_env and env_spec.pixi_env != "default"
            else ""
        )
        pixi_parts = [pixi_bin, "run", "--frozen", manifest_arg]
        if env_arg:
            pixi_parts.append(env_arg)
        pixi_base = " ".join(pixi_parts)

        if run_opts.interactive:
            casarun = f"{pixi_base} casa --nocrashreport --notelemetry"
        elif run_opts.ncores > 1:
            os.environ["CASA_NPROCS"] = str(run_opts.ncores)
            casarun = (
                f"env CASA_NPROCS={run_opts.ncores} {pixi_base} casampi "
                "--nocrashreport --notelemetry --nogui --agg --nologger"
            )
        else:
            casarun = (
                f"{pixi_base} casa --nocrashreport --notelemetry --nogui --agg --nologger"
            )
        casaroot = ""
    else:
        casaroot = env.get("CASA_ROOT", "")
        if run_opts.interactive:
            casarun = f"{casaroot}/bin/casa --nocrashreport --notelemetry"
        else:
            casarun = f"{casaroot}/bin/casa --nocrashreport --notelemetry --nogui --agg"
            if run_opts.ncores > 1:
                mpi_flags: list[str] = []
                if run_opts.oversubscribe:
                    mpi_flags.append("--oversubscribe")
                if run_opts.bind_to:
                    mpi_flags.append(f"--bind-to {run_opts.bind_to}")
                if run_opts.map_by:
                    mpi_flags.append(f"--map-by {run_opts.map_by}")

                extra_mpi = f" {' '.join(mpi_flags)}" if mpi_flags else ""
                casarun = f"{casaroot}/bin/mpicasa -n {run_opts.ncores}{extra_mpi}  {casarun}"

    return casarun, casaroot


def _run_mous(
    opts: argparse.Namespace,
    run_opts: envconfig.ResolvedRunOptions,
    env_spec: envconfig.EnvironmentSpec,
    env: dict[str, str],
    casaroot: str,
    casarun: str,
    site_cfg: dict[str, Any],
) -> int:
    """Execute ALMA Science Pipeline via pipelineMakeRequest (legacy PMR mode)."""
    mous_uid = run_opts.mous or opts.mous
    flag_and_go_dir = run_opts.flag_dir or opts.flag
    flag_and_go = bool(flag_and_go_dir)
    if flag_and_go:
        flag_and_go_dir = os.path.abspath(flag_and_go_dir)
        if not os.path.exists(flag_and_go_dir):
            print(f"Cannot find the --flag directory {flag_and_go_dir}. Does it exist?")
            sys.exit(0)

    ppr_override = run_opts.ppr or opts.ppr
    if ppr_override:
        ppr_override = os.path.abspath(ppr_override)
        if not os.path.exists(ppr_override):
            print(f"Cannot find the --PPR file {ppr_override}. Does it exist?")
            sys.exit(0)

    # Derive recipe details
    recipe = run_opts.recipe
    if recipe == "cal":
        procedure_short = "hifa_cal"
    elif recipe in ["calimage", "calimage_selfcal", "calimage_selfcal_nocube"]:
        procedure_short = "hifa_calimage"
    elif recipe == "image":
        procedure_short = "hifa_image"
    elif recipe == "calsurvey":
        procedure_short = "hifa_calsurvey"
    else:
        procedure_short = f"hifa_{recipe}"

    procedure = f"procedure_{procedure_short}.xml"

    # Check for pre-existing ASDMs
    oldasdms = []
    if flag_and_go:
        oldasdmxmls = find_files("ASDM.xml", flag_and_go_dir)
        for oa in oldasdmxmls:
            oldasdms.append(os.path.dirname(oa))

    # Pipeline Make Request
    asdms_arg = ""
    if opts.onlysemipass:
        asdms_arg = f"--asdms [{opts.onlysemipass}] "

    intents_xml = "intents_hsd.xml" if "hsd" in procedure else "intents_hifa.xml"
    dl_asdms = "false" if oldasdms else "true"
    dl_cal = (
        "true"
        if (recipe in ["image", "image_selfcal"] and not flag_and_go)
        else "false"
    )

    pmr_cmd = f"pipelineMakeRequest {asdms_arg}{mous_uid} {intents_xml} {procedure} {dl_asdms} {dl_cal}"
    if run_opts.dry_run:
        print("[DRY RUN] Execution mode: mous")
        print(f"[DRY RUN] MOUS UID: {mous_uid}")
        print(f"[DRY RUN] Environment: {env_spec.name} ({casaroot})")
        print(f"[DRY RUN] PMR command:\n    {pmr_cmd}")
        print(f"[DRY RUN] Planned CASA execution prefix:\n    {casarun}")
        return 0

    cmdoutput = getoutput(pmr_cmd)

    ppmr_rel_dir = ""
    piperootdir = env.get("SCIPIPE_ROOTDIR", "")
    for line in cmdoutput.split("\n"):
        if "Project root directory is" in line:
            ppmr_rel_dir = line.split()[-1]
            break

    ppmr_dir = f"{piperootdir}/{ppmr_rel_dir}" if ppmr_rel_dir else piperootdir

    # Logger initialization
    log_name = f"{ppmr_dir}/calibPipeIF.{ppmr_rel_dir}.{mous_uid.replace('/', '_').replace(':', '_')}"
    mylog = RunLogger(log_name, print_flag=True)
    log_message = mylog.log
    log_message(
        f"just finished running pipelineMakeRequest with this output:\n{cmdoutput}"
    )

    if not ppmr_rel_dir:
        log_message(
            f"ERROR: pipelineMakeRequest failed (could not determine project root directory):\n{cmdoutput}"
        )
        mylog.close()
        sys.exit(1)

    # Determine working directory structure
    dir_working_output = getoutput(
        f"ls -d1 {ppmr_dir}/SOUS_uid___*/GOUS_uid___*/MOUS_uid___*/working/"
    )
    if "No such file" in dir_working_output or "cannot access" in dir_working_output:
        dir_working_output = getoutput(f"ls -d1 {ppmr_dir}/MOUS_uid___*/working/")

    if "No such file" in dir_working_output or "cannot access" in dir_working_output:
        log_message(
            f"ERROR: Could not find working directory in {ppmr_dir}. pipelineMakeRequest may have failed:\n{cmdoutput}"
        )
        mylog.close()
        sys.exit(1)

    dir_working = dir_working_output.split("\n")[0]
    ppmr_fulldir = dir_working.split("working")[0] + "/"

    # Create convenience symlinks in project root (working, products, rawdata)
    if run_opts.symlink_shortcuts:
        create_project_symlinks(ppmr_dir, dir_working, log_message)

    # File staging from --flag
    if flag_and_go:
        stage_flags_and_wvr(flag_and_go_dir, ppmr_fulldir, log_message)

    # Locate PPR XML
    working_path = Path(ppmr_fulldir.replace("//", "/")) / "working"
    ppr_matches = list(working_path.glob("PPR*.xml"))
    pprfile = (
        f"../working/{ppr_matches[0].name}"
        if ppr_matches
        else f"{ppmr_fulldir}/working/PPR.xml"
    )

    # Setup isolated CASA runtime directory (rcdir)
    if run_opts.use_custom_rcdir:
        rcdir = working_path / ".casa"
        rcdir.mkdir(parents=True, exist_ok=True)
        write_casa_config(
            rcdir,
            site_cfg,
            log2term=run_opts.log2term,
            casadata=env.get("CASADATA") or os.environ.get("CASADATA") or site_cfg.get("casadata"),
            datapath=run_opts.datapath,
            rundata=run_opts.rundata,
            rundata_specified=run_opts.rundata_specified,
        )
        write_casa_startup(rcdir)
        if env_spec.is_pixi:
            rcdir_args = [
                "--cachedir",
                str(rcdir),
                "--configfile",
                str(rcdir / "config.py"),
                "--startupfile",
                str(rcdir / "startup.py"),
            ]
        else:
            rcdir_args = get_casa_rcdir_args(casaroot, rcdir)
        casarun = f"{casarun} " + " ".join(rcdir_args)
        log_message(f"created isolated CASA rcdir at {rcdir}")

    if env_spec.is_pixi:
        casa_logfile = working_path / f"casa-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        casarun = f"{casarun} --logfile {casa_logfile}"

    if run_opts.log2term:
        casarun = f"{casarun} --log2term"

    # Prepare working execution directory and fixes script
    working_path.mkdir(parents=True, exist_ok=True)
    stage_ancillary_files(
        working_path,
        cont_dat=run_opts.cont_dat,
        jyperk_csv=run_opts.jyperk_csv,
        parameter_list=run_opts.parameter_list,
        ancillary=run_opts.ancillary,
        recipe=recipe,
        log_func=log_message,
    )
    if recipe not in ["image", "image_selfcal"]:
        fixes_file = working_path / "sacmPL-fixes.casa.py.txt"
        rawdata_fixes = (
            Path(ppmr_fulldir.replace("//", "/"))
            / "rawdata"
            / "sacmPL-fixes.casa.py.txt"
        )
        if rawdata_fixes.is_file() and not fixes_file.exists():
            shutil.copy(rawdata_fixes, fixes_file)
        elif not fixes_file.exists():
            fixes_file.touch()

    orig_cwd = os.getcwd()
    try:
        # Change working directory to working/ for CASA execution
        os.chdir(working_path)

        # Assemble CASA execution command
        if env_spec.is_pixi:
            piperun_file = (working_path / "casa_piperun.py").resolve()
            resolved_workdir = str(working_path.resolve())
            preamble = build_piperun_preamble(run_opts)
            if recipe not in ["image", "image_selfcal"]:
                if run_opts.useresume:
                    code = (
                        "import os\n"
                        f"os.chdir(r'{resolved_workdir}')\n"
                        f"{preamble}"
                        "import pipeline.infrastructure.executeppr as eppr\n"
                        f"eppr.executeppr('{pprfile}', breakpoint='hifa_flagdata', bpaction='break', "
                        f"loglevel='{run_opts.loglevel}')\n"
                        "execfile('sacmPL-fixes.casa.py.txt')\n"
                        f"eppr.executeppr('{pprfile}', breakpoint='hifa_importdata', bpaction='resume', "
                        f"loglevel='{run_opts.loglevel}')\n"
                    )
                else:
                    code = (
                        "import os\n"
                        f"os.chdir(r'{resolved_workdir}')\n"
                        f"{preamble}"
                        "import pipeline.infrastructure.executeppr as eppr\n"
                        f"eppr.executeppr('{pprfile}', importonly=True, loglevel='{run_opts.loglevel}')\n"
                        "execfile('sacmPL-fixes.casa.py.txt')\n"
                        f"eppr.executeppr('{pprfile}', importonly=False, loglevel='{run_opts.loglevel}')\n"
                    )
            else:
                code = (
                    "import os\n"
                    f"os.chdir(r'{resolved_workdir}')\n"
                    f"{preamble}"
                    "import pipeline.infrastructure.executeppr as eppr\n"
                    f"eppr.executeppr('{pprfile}', loglevel='{run_opts.loglevel}')\n"
                )
            piperun_file.write_text(code, encoding="utf-8")
            cmd = wrap_cmd(
                f"{casarun} -c {piperun_file}",
                workdir=working_path,
                rec_prefix=f"{working_path}/calibpipe.mous.{mous_uid.replace('/', '_').replace(':', '_')}",
                run_opts=run_opts,
                logger_fn=log_message,
            )
        else:
            preamble_cmd = ""
            if run_opts.omp_max_threads is not None:
                preamble_cmd += f"from casatasks import casalog; casalog.ompSetNumThreads({run_opts.omp_max_threads}); "
            if run_opts.mem_frac is not None:
                preamble_cmd += f"from casatasks import casalog; casalog.setMemoryFraction({run_opts.mem_frac}); "
            if run_opts.memstats:
                preamble_cmd += "import pipeline.infrastructure.utils as utils; utils.enable_memstats(); "
            if run_opts.pl_psrecord:
                preamble_cmd += "import pipeline.infrastructure.utils as utils; utils.enable_psrecord(); "

            if recipe not in ["image", "image_selfcal"]:
                if run_opts.useresume:
                    inner_cmd = (
                        f"{casarun} -c "
                        f"\"{preamble_cmd}eppr.executeppr('{pprfile}',breakpoint='hifa_flagdata',bpaction='break', "
                        f"loglevel='{run_opts.loglevel}');"
                        f"execfile('sacmPL-fixes.casa.py.txt');"
                        f"eppr.executeppr('{pprfile}',breakpoint='hifa_importdata',bpaction='resume', "
                        f"loglevel='{run_opts.loglevel}');exit;\""
                    )
                else:
                    inner_cmd = (
                        f"{casarun} -c "
                        f"\"{preamble_cmd}eppr.executeppr('{pprfile}',importonly=True, loglevel='{run_opts.loglevel}');"
                        f"execfile('sacmPL-fixes.casa.py.txt');"
                        f"eppr.executeppr('{pprfile}',importonly=False,loglevel='{run_opts.loglevel}');exit;\""
                    )
            else:
                inner_cmd = f"{casarun} -c \"{preamble_cmd}eppr.executeppr('{pprfile}', loglevel='{run_opts.loglevel}');exit;\""

            cmd = wrap_cmd(
                inner_cmd,
                workdir=working_path,
                rec_prefix=f"{working_path}/calibpipe.mous.{mous_uid.replace('/', '_').replace(':', '_')}",
                run_opts=run_opts,
                logger_fn=log_message,
            )

        if run_opts.verbose:
            retcode = mylog.run(cmd)
        else:
            retcode = mylog.runquiet(cmd)
    finally:
        if run_opts.symlink_shortcuts:
            link_weblog(ppmr_dir, working_path, log_message)
        os.chdir(orig_cwd)
        mylog.close()

    return retcode


def _run_recipe_reducer(
    opts: argparse.Namespace,
    run_opts: envconfig.ResolvedRunOptions,
    env_spec: envconfig.EnvironmentSpec,
    casaroot: str,
    casarun: str,
    site_cfg: dict[str, Any],
) -> int:
    target_path = (
        Path(run_opts.workdir).expanduser().resolve()
        if run_opts.workdir
        else (Path(opts.workdir).expanduser().resolve() if opts.workdir else Path.cwd())
    )
    if target_path.name == "working":
        project_root = target_path.parent
        working_path = target_path
    else:
        project_root = target_path
        working_path = project_root / "working"

    products_path = project_root / "products"
    rawdata_path = project_root / "rawdata"

    if run_opts.backup and not run_opts.dry_run:
        for sub_p in (working_path, products_path, rawdata_path):
            if sub_p.exists() and (sub_p.is_symlink() or any(sub_p.iterdir())):
                rotate_directory(sub_p)

    working_path.mkdir(parents=True, exist_ok=True)
    products_path.mkdir(parents=True, exist_ok=True)
    rawdata_path.mkdir(parents=True, exist_ok=True)

    procedure = run_opts.procedure or opts.procedure
    if not procedure:
        recipe = run_opts.recipe
        if recipe.endswith(".xml"):
            procedure = recipe
        else:
            if recipe == "cal":
                procedure_short = "hifa_cal"
            elif recipe in ["calimage", "calimage_selfcal", "calimage_selfcal_nocube"]:
                procedure_short = "hifa_calimage"
            elif recipe == "image":
                procedure_short = "hifa_image"
            elif recipe == "calsurvey":
                procedure_short = "hifa_calsurvey"
            else:
                procedure_short = f"hifa_{recipe}"
            procedure = f"procedure_{procedure_short}.xml"

    raw_vis = run_opts.vis or opts.vis
    vis_list = [str(Path(v).resolve()) if Path(v).exists() else v for v in raw_vis]

    # Symlink raw visibility data into rawdata/ if not already inside rawdata_path
    if not run_opts.dry_run:
        for v in vis_list:
            v_p = Path(v)
            if v_p.exists():
                dst = rawdata_path / v_p.name
                if dst.is_symlink() or dst.exists():
                    try:
                        if dst.resolve() == v_p.resolve():
                            continue
                        if dst.is_symlink():
                            dst.unlink()
                    except OSError:
                        pass
                try:
                    dst.symlink_to(v_p.resolve())
                except OSError:
                    pass

    now_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_name = f"{working_path}/calibpipe.recipe_reduce.{now_ts}"
    mylog = RunLogger(log_name, print_flag=True)

    stage_ancillary_files(
        working_path,
        cont_dat=run_opts.cont_dat,
        jyperk_csv=run_opts.jyperk_csv,
        parameter_list=run_opts.parameter_list,
        ancillary=run_opts.ancillary,
        recipe=procedure,
        log_func=mylog.log,
    )

    if run_opts.use_custom_rcdir:
        rcdir = working_path / ".casa"
        rcdir.mkdir(parents=True, exist_ok=True)
        write_casa_config(
            rcdir,
            site_cfg,
            log2term=run_opts.log2term,
            casadata=os.environ.get("CASADATA") or site_cfg.get("casadata"),
            datapath=run_opts.datapath,
            rundata=run_opts.rundata,
            rundata_specified=run_opts.rundata_specified,
        )
        write_casa_startup(rcdir)
        if env_spec.is_pixi:
            rcdir_args = [
                "--cachedir", str(rcdir),
                "--configfile", str(rcdir / "config.py"),
                "--startupfile", str(rcdir / "startup.py"),
            ]
        else:
            rcdir_args = get_casa_rcdir_args(casaroot, rcdir)
        casarun = f"{casarun} " + " ".join(rcdir_args)
        mylog.log(f"created isolated CASA rcdir at {rcdir}")

    if env_spec.is_pixi:
        casa_logfile = working_path / f"casa-{now_ts}.log"
        casarun = f"{casarun} --logfile {casa_logfile}"

    if run_opts.log2term:
        casarun = f"{casarun} --log2term"

    piperun_file = (working_path / "casa_piperun.py").resolve()
    resolved_workdir = str(working_path.resolve())
    preamble = build_piperun_preamble(run_opts)
    code = (
        "import os\n"
        f"os.chdir(r'{resolved_workdir}')\n"
        f"{preamble}"
        "import pipeline\n"
        "import pipeline.recipereducer\n"
        f"pipeline.recipereducer.reduce(vis={vis_list!r}, procedure={procedure!r}, loglevel={run_opts.loglevel!r})\n"
    )
    piperun_file.write_text(code, encoding="utf-8")
    cmd = wrap_cmd(
        f"{casarun} -c {piperun_file}",
        workdir=working_path,
        rec_prefix=f"{working_path}/calibpipe.recipe_reduce.{now_ts}",
        run_opts=run_opts,
        logger_fn=mylog.log,
    )

    if run_opts.dry_run:
        print("[DRY RUN] Execution mode: recipe_reducer")
        print(f"[DRY RUN] Working directory: {working_path}")
        print(f"[DRY RUN] Procedure: {procedure}")
        print(f"[DRY RUN] Vis list: {vis_list}")
        print(f"[DRY RUN] Generated script: {piperun_file}")
        print(f"[DRY RUN] Command to execute:\n    {cmd}")
        mylog.close()
        return 0

    orig_cwd = os.getcwd()
    try:
        os.chdir(working_path)
        if run_opts.verbose:
            retcode = mylog.run(cmd)
        else:
            retcode = mylog.runquiet(cmd)
    finally:
        if run_opts.symlink_shortcuts:
            link_weblog(
                working_path.parent if working_path.name == "working" else working_path,
                working_path,
                mylog.log,
            )
        os.chdir(orig_cwd)
        mylog.close()

    return retcode


def _run_standalone_ppr(
    opts: argparse.Namespace,
    run_opts: envconfig.ResolvedRunOptions,
    env_spec: envconfig.EnvironmentSpec,
    casaroot: str,
    casarun: str,
    site_cfg: dict[str, Any],
) -> int:
    """Execute standalone PPR XML via executeppr / executevlappr without PMR."""
    ppr_str = run_opts.ppr or opts.ppr
    ppr_path = Path(ppr_str).resolve()
    if not ppr_path.is_file():
        print(f"ERROR: Cannot find the --PPR file {ppr_path}. Does it exist?", file=sys.stderr)
        return 1

    working_path = (
        Path(run_opts.workdir).expanduser().resolve()
        if run_opts.workdir
        else (Path(opts.workdir).expanduser().resolve() if opts.workdir else ppr_path.parent)
    )
    if run_opts.backup and working_path.exists() and any(working_path.iterdir()):
        if (run_opts.workdir or opts.workdir) and working_path != ppr_path.parent:
            rotate_directory(working_path)
        elif (working_path / "working").exists() and any((working_path / "working").iterdir()):
            rotate_directory(working_path / "working")

    working_path.mkdir(parents=True, exist_ok=True)

    now_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_name = f"{working_path}/calibpipe.ppr.{now_ts}"
    mylog = RunLogger(log_name, print_flag=True)

    stage_ancillary_files(
        working_path,
        cont_dat=run_opts.cont_dat,
        jyperk_csv=run_opts.jyperk_csv,
        parameter_list=run_opts.parameter_list,
        ancillary=run_opts.ancillary,
        recipe=run_opts.recipe,
        log_func=mylog.log,
    )

    if run_opts.use_custom_rcdir:
        rcdir = working_path / ".casa"
        rcdir.mkdir(parents=True, exist_ok=True)
        write_casa_config(
            rcdir,
            site_cfg,
            log2term=run_opts.log2term,
            casadata=os.environ.get("CASADATA") or site_cfg.get("casadata"),
            datapath=run_opts.datapath,
            rundata=run_opts.rundata,
            rundata_specified=run_opts.rundata_specified,
        )
        write_casa_startup(rcdir)
        if env_spec.is_pixi:
            rcdir_args = [
                "--cachedir", str(rcdir),
                "--configfile", str(rcdir / "config.py"),
                "--startupfile", str(rcdir / "startup.py"),
            ]
        else:
            rcdir_args = get_casa_rcdir_args(casaroot, rcdir)
        casarun = f"{casarun} " + " ".join(rcdir_args)
        mylog.log(f"created isolated CASA rcdir at {rcdir}")

    if env_spec.is_pixi:
        casa_logfile = working_path / f"casa-{now_ts}.log"
        casarun = f"{casarun} --logfile {casa_logfile}"

    if run_opts.log2term:
        casarun = f"{casarun} --log2term"

    piperun_file = (working_path / "casa_piperun.py").resolve()
    resolved_workdir = str(working_path.resolve())
    import_module = (
        "pipeline.infrastructure.executevlappr"
        if (run_opts.vla or opts.vla)
        else "pipeline.infrastructure.executeppr"
    )
    preamble = build_piperun_preamble(run_opts)
    code = (
        "import os\n"
        f"os.chdir(r'{resolved_workdir}')\n"
        f"{preamble}"
        "import pipeline\n"
        f"import {import_module} as eppr\n"
        f"eppr.executeppr({str(ppr_path)!r}, importonly={run_opts.useresume!r}, loglevel={run_opts.loglevel!r})\n"
    )
    piperun_file.write_text(code, encoding="utf-8")
    cmd = wrap_cmd(
        f"{casarun} -c {piperun_file}",
        workdir=working_path,
        rec_prefix=f"{working_path}/calibpipe.ppr.{now_ts}",
        run_opts=run_opts,
        logger_fn=mylog.log,
    )

    if run_opts.dry_run:
        print("[DRY RUN] Execution mode: standalone_ppr")
        print(f"[DRY RUN] Working directory: {working_path}")
        print(f"[DRY RUN] PPR file: {ppr_path}")
        print(f"[DRY RUN] Generated script: {piperun_file}")
        print(f"[DRY RUN] Command to execute:\n    {cmd}")
        mylog.close()
        return 0

    orig_cwd = os.getcwd()
    try:
        os.chdir(working_path)
        if run_opts.verbose:
            retcode = mylog.run(cmd)
        else:
            retcode = mylog.runquiet(cmd)
    finally:
        if run_opts.symlink_shortcuts:
            link_weblog(
                working_path.parent if working_path.name == "working" else working_path,
                working_path,
                mylog.log,
            )
        os.chdir(orig_cwd)
        mylog.close()

    return retcode


def _run_script(
    opts: argparse.Namespace,
    run_opts: envconfig.ResolvedRunOptions,
    env_spec: envconfig.EnvironmentSpec,
    casaroot: str,
    casarun: str,
    site_cfg: dict[str, Any],
) -> int:
    """Execute a Python/CASA script under the managed CASA environment."""
    script_str = run_opts.script or opts.script
    script_path = Path(script_str).resolve()
    if not script_path.is_file():
        print(f"ERROR: Cannot find script file {script_path}. Does it exist?", file=sys.stderr)
        return 1

    working_path = (
        Path(run_opts.workdir).expanduser().resolve()
        if run_opts.workdir
        else (Path(opts.workdir).expanduser().resolve() if opts.workdir else Path.cwd())
    )
    if run_opts.backup and (run_opts.workdir or opts.workdir) and working_path.exists() and any(working_path.iterdir()):
        rotate_directory(working_path)

    working_path.mkdir(parents=True, exist_ok=True)

    now_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_name = f"{working_path}/calibpipe.script.{now_ts}"
    mylog = RunLogger(log_name, print_flag=True)

    stage_ancillary_files(
        working_path,
        cont_dat=run_opts.cont_dat,
        jyperk_csv=run_opts.jyperk_csv,
        parameter_list=run_opts.parameter_list,
        ancillary=run_opts.ancillary,
        recipe=run_opts.recipe,
        log_func=mylog.log,
    )

    if run_opts.use_custom_rcdir:
        rcdir = working_path / ".casa"
        rcdir.mkdir(parents=True, exist_ok=True)
        write_casa_config(
            rcdir,
            site_cfg,
            log2term=run_opts.log2term,
            casadata=os.environ.get("CASADATA") or site_cfg.get("casadata"),
            datapath=run_opts.datapath,
            rundata=run_opts.rundata,
            rundata_specified=run_opts.rundata_specified,
        )
        write_casa_startup(rcdir)
        if env_spec.is_pixi:
            rcdir_args = [
                "--cachedir", str(rcdir),
                "--configfile", str(rcdir / "config.py"),
                "--startupfile", str(rcdir / "startup.py"),
            ]
        else:
            rcdir_args = get_casa_rcdir_args(casaroot, rcdir)
        casarun = f"{casarun} " + " ".join(rcdir_args)
        mylog.log(f"created isolated CASA rcdir at {rcdir}")

    if env_spec.is_pixi:
        casa_logfile = working_path / f"casa-{now_ts}.log"
        casarun = f"{casarun} --logfile {casa_logfile}"

    if run_opts.log2term:
        casarun = f"{casarun} --log2term"

    cmd = wrap_cmd(
        f"{casarun} -c {script_path}",
        workdir=working_path,
        rec_prefix=f"{working_path}/calibpipe.script.{now_ts}",
        run_opts=run_opts,
        logger_fn=mylog.log,
    )

    if run_opts.dry_run:
        print("[DRY RUN] Execution mode: script")
        print(f"[DRY RUN] Working directory: {working_path}")
        print(f"[DRY RUN] Script: {script_path}")
        print(f"[DRY RUN] Command to execute:\n    {cmd}")
        mylog.close()
        return 0

    orig_cwd = os.getcwd()
    try:
        os.chdir(working_path)
        if run_opts.verbose:
            retcode = mylog.run(cmd)
        else:
            retcode = mylog.runquiet(cmd)
    finally:
        os.chdir(orig_cwd)
        mylog.close()

    return retcode


def _run_cmd(
    opts: argparse.Namespace,
    run_opts: envconfig.ResolvedRunOptions,
    env_spec: envconfig.EnvironmentSpec,
    casaroot: str,
    casarun: str,
    site_cfg: dict[str, Any],
) -> int:
    """Execute an inline Python command string under the managed CASA environment."""
    working_path = (
        Path(run_opts.workdir).expanduser().resolve()
        if run_opts.workdir
        else (Path(opts.workdir).expanduser().resolve() if opts.workdir else Path.cwd())
    )
    if run_opts.backup and (run_opts.workdir or opts.workdir) and working_path.exists() and any(working_path.iterdir()):
        rotate_directory(working_path)

    working_path.mkdir(parents=True, exist_ok=True)

    now_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_name = f"{working_path}/calibpipe.cmd.{now_ts}"
    mylog = RunLogger(log_name, print_flag=True)

    stage_ancillary_files(
        working_path,
        cont_dat=run_opts.cont_dat,
        jyperk_csv=run_opts.jyperk_csv,
        parameter_list=run_opts.parameter_list,
        ancillary=run_opts.ancillary,
        recipe=run_opts.recipe,
        log_func=mylog.log,
    )

    if run_opts.use_custom_rcdir:
        rcdir = working_path / ".casa"
        rcdir.mkdir(parents=True, exist_ok=True)
        write_casa_config(
            rcdir,
            site_cfg,
            log2term=run_opts.log2term,
            casadata=os.environ.get("CASADATA") or site_cfg.get("casadata"),
            datapath=run_opts.datapath,
            rundata=run_opts.rundata,
            rundata_specified=run_opts.rundata_specified,
        )
        write_casa_startup(rcdir)
        if env_spec.is_pixi:
            rcdir_args = [
                "--cachedir", str(rcdir),
                "--configfile", str(rcdir / "config.py"),
                "--startupfile", str(rcdir / "startup.py"),
            ]
        else:
            rcdir_args = get_casa_rcdir_args(casaroot, rcdir)
        casarun = f"{casarun} " + " ".join(rcdir_args)
        mylog.log(f"created isolated CASA rcdir at {rcdir}")

    if env_spec.is_pixi:
        casa_logfile = working_path / f"casa-{now_ts}.log"
        casarun = f"{casarun} --logfile {casa_logfile}"

    if run_opts.log2term:
        casarun = f"{casarun} --log2term"

    cmd_file = (working_path / "casa_cmd.py").resolve()
    resolved_workdir = str(working_path.resolve())
    cmd_str = run_opts.cmd or opts.cmd
    code = f"import os\nos.chdir(r'{resolved_workdir}')\n{cmd_str}\n"
    cmd_file.write_text(code, encoding="utf-8")
    cmd = wrap_cmd(
        f"{casarun} -c {cmd_file}",
        workdir=working_path,
        rec_prefix=f"{working_path}/calibpipe.cmd.{now_ts}",
        run_opts=run_opts,
        logger_fn=mylog.log,
    )

    if run_opts.dry_run:
        print("[DRY RUN] Execution mode: cmd")
        print(f"[DRY RUN] Working directory: {working_path}")
        print(f"[DRY RUN] Generated script: {cmd_file}")
        print(f"[DRY RUN] Command to execute:\n    {cmd}")
        mylog.close()
        return 0

    orig_cwd = os.getcwd()
    try:
        os.chdir(working_path)
        if run_opts.verbose:
            retcode = mylog.run(cmd)
        else:
            retcode = mylog.runquiet(cmd)
    finally:
        os.chdir(orig_cwd)
        mylog.close()

    return retcode


def _run_interactive(
    opts: argparse.Namespace,
    run_opts: envconfig.ResolvedRunOptions,
    env_spec: envconfig.EnvironmentSpec,
    casaroot: str,
    casarun: str,
    site_cfg: dict[str, Any],
) -> int:
    """Launch an interactive CASA session under the resolved environment."""
    working_path = (
        Path(run_opts.workdir).expanduser().resolve()
        if run_opts.workdir
        else (Path(opts.workdir).expanduser().resolve() if opts.workdir else Path.cwd())
    )
    working_path.mkdir(parents=True, exist_ok=True)

    if run_opts.use_custom_rcdir:
        rcdir = working_path / ".casa"
        rcdir.mkdir(parents=True, exist_ok=True)
        write_casa_config(
            rcdir,
            site_cfg,
            log2term=run_opts.log2term,
            casadata=os.environ.get("CASADATA") or site_cfg.get("casadata"),
            datapath=run_opts.datapath,
            rundata=run_opts.rundata,
            rundata_specified=run_opts.rundata_specified,
        )
        write_casa_startup(rcdir)
        write_ipython_config(rcdir, autoreload=run_opts.autoreload)
        if env_spec.is_pixi:
            rcdir_args = [
                "--cachedir", str(rcdir),
                "--configfile", str(rcdir / "config.py"),
                "--startupfile", str(rcdir / "startup.py"),
            ]
        else:
            rcdir_args = get_casa_rcdir_args(casaroot, rcdir)
        casarun = f"{casarun} " + " ".join(rcdir_args)

    if run_opts.dry_run:
        print("[DRY RUN] Execution mode: interactive")
        print(f"[DRY RUN] Working directory: {working_path}")
        print(f"[DRY RUN] Command to execute:\n    {casarun}")
        return 0

    orig_cwd = os.getcwd()
    try:
        os.chdir(working_path)
        return subprocess.call(casarun, shell=True)
    finally:
        os.chdir(orig_cwd)


def main(custom_argv: Sequence[str] | None = None) -> None:
    """Execute a single pipeline run from command-line arguments.

    Args:
        custom_argv: Optional argument sequence. When omitted, the legacy
            module-level `argv` wrapper is used for compatibility.
    """
    args_list = custom_argv if custom_argv is not None else argv[1:]
    parser = build_parser()
    if not args_list:
        parser.print_help()
        sys.exit(0)

    # Support both standard flags and legacy positional mous argument
    # If no target flags are passed, treat the first bare argument as --mous
    has_target_flag = any(
        arg.startswith(
            (
                "--vis",
                "--script",
                "--cmd",
                "--interactive",
                "-i",
                "--PPR",
                "--ppr",
                "--mous",
                "-p",
                "--profile",
            )
        )
        for arg in args_list
    )
    filtered_args = []
    for a in args_list:
        if (
            a
            and not a.startswith("-")
            and not has_target_flag
            and not any(x.startswith("--mous") for x in filtered_args)
        ):
            filtered_args.append(f"--mous={a}")
        else:
            filtered_args.append(a)

    opts = parser.parse_args(filtered_args)

    targets = []
    if opts.mous:
        targets.append(f"--mous={opts.mous}")
    if opts.vis:
        targets.append(f"--vis={' '.join(opts.vis)}")
    if opts.script:
        targets.append(f"--script={opts.script}")
    if opts.cmd:
        targets.append(f"--cmd={opts.cmd}")
    if opts.interactive:
        targets.append("-i/--interactive")
    if opts.ppr and not opts.mous:
        targets.append(f"--PPR={opts.ppr}")

    if len(targets) > 1:
        print(
            f"ERROR: Conflicting execution targets specified: {', '.join(targets)}. "
            "Please specify only one target.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load configuration
    try:
        cfg = envconfig.load_merged_config(
            cli_arg=opts.config,
            include_site=not getattr(opts, "no_site_config", False),
        )
    except envconfig.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        run_opts = envconfig.resolve_run_options(cfg, opts)
    except envconfig.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    resolved_targets = []
    if run_opts.mous:
        resolved_targets.append(f"mous={run_opts.mous}")
    if run_opts.vis:
        resolved_targets.append(f"vis={' '.join(run_opts.vis)}")
    if run_opts.script:
        resolved_targets.append(f"script={run_opts.script}")
    if run_opts.cmd:
        resolved_targets.append("cmd")
    if run_opts.interactive:
        resolved_targets.append("interactive")
    if run_opts.ppr and not run_opts.mous:
        resolved_targets.append(f"ppr={run_opts.ppr}")

    if not resolved_targets and not opts.print_env:
        print(
            "ERROR: No execution target specified. Provide --mous, --vis, --script, "
            "--cmd, --PPR, or -i/--interactive (or define a target in the profile).",
            file=sys.stderr,
        )
        sys.exit(1)

    env_spec = envconfig.resolve_env(cfg, run_opts.env_name)
    env = envconfig.build_environment(cfg, env_spec, subdir=run_opts.subdir)

    # Update os.environ in process so subprocesses inherit it
    for k, v in env.items():
        os.environ[k] = v

    # Isolate CASA from foreign virtualenv (e.g. from `uv run` or activated venv)
    # to prevent IPython UserWarning and cross-environment sys.path pollution
    os.environ.pop("VIRTUAL_ENV", None)

    # Clean runtime environment (avoid Qt session errors and foreign user-site packages)
    os.environ.pop("SESSION_MANAGER", None)
    os.environ["PYTHONNOUSERSITE"] = "1"
    os.environ["KOKKOS_DISABLE_WARNINGS"] = "1"

    # Configure OpenMP and OpenBLAS threading
    if run_opts.omp_num_threads is not None:
        os.environ["OMP_NUM_THREADS"] = str(run_opts.omp_num_threads)
    elif run_opts.ncores > 1:
        os.environ.setdefault("OMP_NUM_THREADS", "1")

    if run_opts.openblas_num_threads is not None:
        os.environ["OPENBLAS_NUM_THREADS"] = str(run_opts.openblas_num_threads)
    elif run_opts.ncores > 1:
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

    if opts.print_env:
        print(envconfig.format_shell_exports(env, export=False))
        return

    site_cfg = cfg.get("site", {})
    casarun, casaroot = build_casarun_prefix(run_opts, env_spec, env, site_cfg)

    if run_opts.mous:
        retcode = _run_mous(opts, run_opts, env_spec, env, casaroot, casarun, site_cfg)
    elif run_opts.vis:
        retcode = _run_recipe_reducer(opts, run_opts, env_spec, casaroot, casarun, site_cfg)
    elif run_opts.ppr and not run_opts.mous:
        retcode = _run_standalone_ppr(opts, run_opts, env_spec, casaroot, casarun, site_cfg)
    elif run_opts.script:
        retcode = _run_script(opts, run_opts, env_spec, casaroot, casarun, site_cfg)
    elif run_opts.cmd:
        retcode = _run_cmd(opts, run_opts, env_spec, casaroot, casarun, site_cfg)
    elif run_opts.interactive:
        retcode = _run_interactive(opts, run_opts, env_spec, casaroot, casarun, site_cfg)
    else:
        retcode = 0

    if retcode != 0:
        sys.exit(retcode)


if __name__ == "__main__":
    main()

