"""calibpipe driver: Orchestrates execution of ALMA Science Pipeline runs.

Decomposes and modernizes the legacy calibPipeIF.py script.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from calibpipe import config as envconfig
from calibpipe.steps.staging import find_dirs, find_files, stage_flags_and_wvr
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
) -> Path:
    """Generate isolated config.py for CASA inside rcdir.

    Args:
        rcdir: Custom runtime configuration directory.
        site_config: Optional [site] configuration mapping.

    Returns:
        Path to the generated config.py file.
    """
    site = site_config or {}
    telemetry = "True" if site.get("casa_enable_telemetry", False) else "False"
    content = render_template("casa_config.py.in", telemetry=telemetry)
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
        default="calimage",
        choices=[
            "cal",
            "image",
            "image_selfcal",
            "image_selfcal_nocube",
            "calimage",
            "calimage_selfcal",
            "calsurvey",
        ],
        help="Pipeline recipe to execute (default: calimage)",
    )
    p.add_argument(
        "--ebwfile", default="", help="File with effective bandwidth information"
    )
    p.add_argument("--ncores", type=int, default=8, help="Number of cores (default: 8)")
    p.add_argument("--loglevel", default="debug", help="PL log level (default: debug)")
    p.add_argument(
        "--pickle", action="store_true", help="Run picklePipeRun upon completion"
    )
    p.add_argument(
        "--useresume",
        action="store_true",
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
    return p


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
    # If the first argument does not start with '-', treat it as --mous
    filtered_args = []
    for a in args_list:
        if (
            a
            and not a.startswith("-")
            and not any(x.startswith("--mous") for x in filtered_args)
        ):
            filtered_args.append(f"--mous={a}")
        else:
            filtered_args.append(a)

    opts = parser.parse_args(filtered_args)

    mous_uid = opts.mous
    flag_and_go_dir = opts.flag
    flag_and_go = bool(flag_and_go_dir)
    if flag_and_go:
        flag_and_go_dir = os.path.abspath(flag_and_go_dir)
        if not os.path.exists(flag_and_go_dir):
            print(f"Cannot find the --flag directory {flag_and_go_dir}. Does it exist?")
            sys.exit(0)

    ppr_override = opts.ppr
    if ppr_override:
        ppr_override = os.path.abspath(ppr_override)
        if not os.path.exists(ppr_override):
            print(f"Cannot find the --PPR file {ppr_override}. Does it exist?")
            sys.exit(0)

    # Load configuration
    try:
        cfg = envconfig.load_merged_config(
            cli_arg=opts.config,
            include_site=not getattr(opts, "no_site_config", False),
        )
    except envconfig.ConfigError as e:
        print(f"Configuration error: {e}")
        sys.exit(1)

    run_opts = envconfig.resolve_run_options(cfg, opts)
    env_spec = envconfig.resolve_env(cfg, run_opts.env_name)
    env = envconfig.build_environment(cfg, env_spec, subdir=run_opts.subdir)

    # Update os.environ in process so subprocesses inherit it
    for k, v in env.items():
        os.environ[k] = v

    # Isolate CASA from foreign virtualenv (e.g. from `uv run` or activated venv)
    # to prevent IPython UserWarning and cross-environment sys.path pollution
    os.environ.pop("VIRTUAL_ENV", None)

    if opts.print_env:
        print(envconfig.format_shell_exports(env, export=False))
        return

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

    # Build CASA invocation command prefix
    site_cfg = cfg.get("site", {})
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

        if run_opts.ncores > 1:
            os.environ["CASA_NPROCS"] = str(run_opts.ncores)
            casarun = (
                f"env CASA_NPROCS={run_opts.ncores} {pixi_base} casampi "
                "--nocrashreport --notelemetry --nogui --agg --nologger"
            )
        else:
            casarun = (
                f"{pixi_base} casa --nocrashreport --notelemetry --nogui --agg --nologger"
            )
    else:
        casaroot = env["CASA_ROOT"]
        casarun = f"{casaroot}/bin/casa --nocrashreport --notelemetry --nogui --agg"
        if run_opts.ncores > 1:
            casarun = f"{casaroot}/bin/mpicasa -n {run_opts.ncores}  {casarun}"

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
        write_casa_config(rcdir, site_cfg)
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

    # Prepare working execution directory and fixes script
    working_path.mkdir(parents=True, exist_ok=True)
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
            if recipe not in ["image", "image_selfcal"]:
                if run_opts.useresume:
                    code = (
                        "import os\n"
                        f"os.chdir(r'{resolved_workdir}')\n"
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
                        "import pipeline.infrastructure.executeppr as eppr\n"
                        f"eppr.executeppr('{pprfile}', importonly=True, loglevel='{run_opts.loglevel}')\n"
                        "execfile('sacmPL-fixes.casa.py.txt')\n"
                        f"eppr.executeppr('{pprfile}', importonly=False, loglevel='{run_opts.loglevel}')\n"
                    )
            else:
                code = (
                    "import os\n"
                    f"os.chdir(r'{resolved_workdir}')\n"
                    "import pipeline.infrastructure.executeppr as eppr\n"
                    f"eppr.executeppr('{pprfile}', loglevel='{run_opts.loglevel}')\n"
                )
            piperun_file.write_text(code, encoding="utf-8")
            cmd = f"xvfb-run -d {casarun} -c {piperun_file}"
        else:
            if recipe not in ["image", "image_selfcal"]:
                if run_opts.useresume:
                    cmd = (
                        f"xvfb-run -d {casarun} -c "
                        f"\"eppr.executeppr('{pprfile}',breakpoint='hifa_flagdata',bpaction='break', "
                        f"loglevel='{run_opts.loglevel}');"
                        f"execfile('sacmPL-fixes.casa.py.txt');"
                        f"eppr.executeppr('{pprfile}',breakpoint='hifa_importdata',bpaction='resume', "
                        f"loglevel='{run_opts.loglevel}');exit;\""
                    )
                else:
                    cmd = (
                        f"xvfb-run -d {casarun} -c "
                        f"\"eppr.executeppr('{pprfile}',importonly=True, loglevel='{run_opts.loglevel}');"
                        f"execfile('sacmPL-fixes.casa.py.txt');"
                        f"eppr.executeppr('{pprfile}',importonly=False,loglevel='{run_opts.loglevel}');exit;\""
                    )
            else:
                cmd = f"xvfb-run -d {casarun} -c \"eppr.executeppr('{pprfile}', loglevel='{run_opts.loglevel}');exit;\""

        if run_opts.verbose:
            retcode = mylog.run(cmd)
        else:
            retcode = mylog.runquiet(cmd)
    finally:
        if run_opts.symlink_shortcuts:
            link_weblog(ppmr_dir, working_path, log_message)
        os.chdir(orig_cwd)
        mylog.close()

    if retcode != 0:
        sys.exit(retcode)


if __name__ == "__main__":
    main()
