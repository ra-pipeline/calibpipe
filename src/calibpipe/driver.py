"""calibpipe driver: Orchestrates execution of CASA + ALMA Science Pipeline runs.

Decomposes and modernizes the legacy calibPipeIF.py script.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from xml.dom.minidom import parse

from calibpipe import config as envconfig
from calibpipe.steps.staging import find_dirs, find_files, stage_flags_and_wvr

# Expose modules/functions at module level for compatibility with existing tests/mocks
argv = sys.argv
getoutput = subprocess.getoutput


def find(pattern: str, path: str | Path) -> list[str]:
    """Find files matching pattern under path (following symlinks)."""
    return find_files(pattern, path)


def finddir(pattern: str, path: str | Path) -> list[str]:
    """Find directories matching pattern under path (following symlinks)."""
    return find_dirs(pattern, path)


class log:
    """Logger tracking command execution and formatted outputs."""

    def __init__(self, filename: str, print_flag: bool = False) -> None:
        now = datetime.now().isoformat().replace(":", "-")
        self.filename = filename
        self.print_flag = print_flag
        self.fd = open(f"{filename}.{now}.log", "w")

    def log(self, text: str) -> None:
        now = datetime.now().isoformat()
        for line in str(text).split("\n"):
            self.fd.write(f"{now}: {line}\n")
            if self.print_flag:
                print(f"{now}: {line}")
            self.fd.flush()

    def run(self, command: str) -> int:
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
        self.log(f"running '{command}' quietly.")
        retcode = subprocess.call(command, shell=True)
        if retcode > 0:
            self.log(f"nonzero exit code = {retcode}")
        return retcode

    def close(self) -> None:
        self.fd.close()


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for calibpipe run."""
    p = argparse.ArgumentParser(
        description="Assists in the execution of the interferometric pipeline.",
        add_help=True,
    )
    p.add_argument("--mous", default="", help="MOUS status UID to execute")
    p.add_argument("--flag", default="", help="Path to dir with flags/templates to re-use")
    p.add_argument("--PPR", default="", dest="ppr", help="Path to a PPR xml file to re-use")
    p.add_argument("--valid", action="store_true", help="Use validation start directory")
    p.add_argument("--config", help="Path to TOML config file")
    p.add_argument("--env", help="[envs.<name>] table to use from config")
    p.add_argument("--subdir", help="Extra subdirectory appended to SCIPIPE_ROOTDIR")
    p.add_argument("--print-env", action="store_true", help="Print resolved environment and exit")
    p.add_argument(
        "--recipe",
        default="calimage",
        choices=["cal", "image", "image_selfcal", "image_selfcal_nocube", "calimage", "calimage_selfcal", "calsurvey"],
        help="Pipeline recipe to execute (default: calimage)",
    )
    p.add_argument("--ebwfile", default="", help="File with effective bandwidth information")
    p.add_argument("--ncores", type=int, default=8, help="Number of cores (default: 8)")
    p.add_argument("--loglevel", default="debug", help="PL log level (default: debug)")
    p.add_argument("--pickle", action="store_true", help="Run picklePipeRun upon completion")
    p.add_argument("--useresume", action="store_true", help="Use break/resume instead of two contexts")
    p.add_argument("--legacy", action="store_true", help="Run fixsyscaltimes, fixplanets")
    p.add_argument("--aUdir", default="", help="Set analysisUtils directory path")
    p.add_argument("--usegetalmaflux", action="store_true", help="Use aU.getALMAFluxCsv")
    p.add_argument("--staticobscal", action="store_true", help="Use previous flux.csv from obscaldir")
    p.add_argument("--oldcont", action="store_true", help="Use previous cont.dat from obscaldir")
    p.add_argument("--noupdate", action="store_true", default=True, help="Don't update CASA IERS")
    p.add_argument("--verbose", action="store_true", help="Verbose subprocess output logging")
    p.add_argument("--semipass", default="", help="Comma-delimited EB UIDs to process")
    p.add_argument("--onlysemipass", default="", help="Process only the specified EBs")
    return p


def main(custom_argv: Sequence[str] | None = None) -> None:
    """Main execution orchestrator for calibpipe run."""
    args_list = custom_argv if custom_argv is not None else argv[1:]
    parser = build_parser()
    if not args_list:
        parser.print_help()
        sys.exit(0)

    # Support both standard flags and legacy positional mous argument
    # If the first argument does not start with '-', treat it as --mous
    filtered_args = []
    for a in args_list:
        if a and not a.startswith("-") and not any(x.startswith("--mous") for x in filtered_args):
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
        cfg_path = envconfig.find_config_path(opts.config)
        cfg = envconfig.load_config(cfg_path)
    except envconfig.ConfigError as e:
        print(f"Configuration error: {e}")
        sys.exit(1)

    env_spec = envconfig.resolve_env(cfg, opts.env)
    env = envconfig.build_environment(cfg, env_spec, subdir=opts.subdir)

    # Update os.environ in process so subprocesses inherit it
    for k, v in env.items():
        os.environ[k] = v

    if opts.print_env:
        print(envconfig.format_shell_exports(env, export=False))
        return

    # Derive recipe details
    recipe = opts.recipe
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
    casaroot = env["CASA_ROOT"]
    casarun = f"{casaroot}/bin/casa --nocrashreport --notelemetry --nogui --agg"
    casarun_serial = f"{casaroot}/bin/casa --nocrashreport --notelemetry --nogui"
    if opts.ncores > 1:
        casarun = f"{casaroot}/bin/mpicasa -n {opts.ncores}  {casarun}"

    # Check for pre-existing ASDMs
    oldasdms = []
    oldpprfile = ""
    if flag_and_go:
        oldpprfiles = find_files("PPR*xml", flag_and_go_dir)
        if oldpprfiles:
            oldpprfile = oldpprfiles[0]
        oldasdmxmls = find_files("ASDM.xml", flag_and_go_dir)
        for oa in oldasdmxmls:
            oldasdms.append(os.path.dirname(oa))

    # Pipeline Make Request
    asdms_arg = ""
    if opts.onlysemipass:
        asdms_arg = f"--asdms [{opts.onlysemipass}] "

    intents_xml = "intents_hsd.xml" if "hsd" in procedure else "intents_hifa.xml"
    dl_asdms = "false" if oldasdms else "true"
    dl_cal = "true" if (recipe in ["image", "image_selfcal"] and not flag_and_go) else "false"

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
    mylog = log(log_name, print_flag=True)
    l = mylog.log
    l(f"just finished running pipelineMakeRequest with this output:\n{cmdoutput}")

    # Determine working directory structure
    dir_working_output = getoutput(f"ls -d1 {ppmr_dir}/SOUS_uid___*/GOUS_uid___*/MOUS_uid___*/working/")
    if "No such file" in dir_working_output:
        dir_working_output = getoutput(f"ls -d1 {ppmr_dir}/MOUS_uid___*/working/")
    dir_working = dir_working_output.split("\n")[0]
    ppmr_fulldir = dir_working.split("working")[0] + "/"

    # File staging from --flag
    if flag_and_go:
        stage_flags_and_wvr(flag_and_go_dir, ppmr_fulldir, l)

    # Locate PPR XML
    working_path = Path(ppmr_fulldir.replace("//", "/")) / "working"
    ppr_matches = list(working_path.glob("PPR*.xml"))
    pprfile = f"../working/{ppr_matches[0].name}" if ppr_matches else f"{ppmr_fulldir}/working/PPR.xml"

    # Assemble CASA execution command
    if recipe not in ["image", "image_selfcal"]:
        if opts.useresume:
            cmd = (
                f"xvfb-run -d {casarun} -c "
                f"\"eppr.executeppr('{pprfile}',breakpoint='hifa_flagdata',bpaction='break', loglevel='{opts.loglevel}');"
                f"execfile('sacmPL-fixes.casa.py.txt');"
                f"eppr.executeppr('{pprfile}',breakpoint='hifa_importdata',bpaction='resume', loglevel='{opts.loglevel}');exit;\""
            )
        else:
            cmd = (
                f"xvfb-run -d {casarun} -c "
                f"\"eppr.executeppr('{pprfile}',importonly=True, loglevel='{opts.loglevel}');"
                f"execfile('sacmPL-fixes.casa.py.txt');"
                f"eppr.executeppr('{pprfile}',importonly=False,loglevel='{opts.loglevel}');exit;\""
            )
    else:
        cmd = f"xvfb-run -d {casarun} -c \"eppr.executeppr('{pprfile}', loglevel='{opts.loglevel}');exit;\""

    if opts.verbose:
        retcode = mylog.run(cmd)
    else:
        retcode = mylog.runquiet(cmd)

    mylog.close()


if __name__ == "__main__":
    main()
