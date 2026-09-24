"""Submit a batch of calibpipe runs to Slurm, one per line of a pipefile.

Each non-comment line of the pipefile is "<mous_uid> [recipe]" (recipe
defaults to calimage).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence

from calibpipe import config as envconfig

# Fallback path to calibPipeIF executable (can be overridden or patched)
CALIBPIPEIF = Path(__file__).resolve().parent / "legacy" / "calibPipeIF.py"
if not CALIBPIPEIF.exists():
    CALIBPIPEIF = Path(shutil.which("calibpipe") or "calibpipe")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for batch submission."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pipefile", help="File with one '<mous_uid> [recipe]' per line")
    p.add_argument("--config", help="Path to TOML config (default: resolved config.toml)")
    p.add_argument("--env", help="[envs.<name>] to use; default: config's default_env")
    p.add_argument("-c", "--cores", type=int, default=8, dest="cores", help="Cores per job (default: 8)")
    p.add_argument("-m", "--mem", type=int, default=248, dest="mem", help="GB memory per job (default: 248)")
    p.add_argument("-M", "--mail-type", default="ALL", dest="mail_type", help="Slurm --mail-type (default: ALL)")
    p.add_argument("-n", "--node", default="1", dest="node", help="Slurm -N nodes (default: 1)")
    p.add_argument("-o", "--outfile", dest="outfile", help="Slurm stdout file (default: batch.<job>.out)")
    p.add_argument("-e", "--errfile", dest="errfile", help="Slurm stderr file (default: batch.<job>.err)")
    queue = p.add_mutually_exclusive_group()
    queue.add_argument("-p", dest="queue", action="store_const", const="plwg", default="plwg",
                        help="Submit to the plwg queue (default)")
    queue.add_argument("-b", dest="queue", action="store_const", const="batch2",
                        help="Submit to the batch2 queue")
    p.add_argument("--extra-arg", action="append", default=[], dest="extra_args",
                    help="Extra flag passed through to calibpipe driver, repeatable")
    return p


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = build_parser()
    if args is None and len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)
    return parser.parse_args(args)


def check_submit_host(config: dict) -> None:
    """Verify current host matches required submit host if configured."""
    submit_host = config.get("site", {}).get("submit_host")
    if not submit_host:
        return
    hostname = subprocess.getoutput("hostname").strip()
    if hostname != submit_host:
        print(f"Jobs must be submitted from {submit_host}, exiting.")
        sys.exit(1)


def build_sbatch_script(pipejob: str) -> str:
    """Generate shell script contents for sbatch submission."""
    return f"""#!/bin/sh
ulimit -Sn 8192
umask 002
{pipejob}
"""


def job_name(pipejob: str) -> str:
    """Derive standard Slurm job name from pipejob command."""
    mous = ""
    for tok in pipejob.split():
        if "mous" in tok:
            mous = tok.split("=", 1)[-1][11:]
            mous = mous.strip("/").replace("/", "_")
            break
    date = time.strftime("%Y-%m-%d")
    return f"{mous}_{date}"


def main(argv: Sequence[str] | None = None) -> None:
    """Main entrypoint for Slurm batch submission."""
    args = parse_args(argv)

    try:
        config_path = envconfig.find_config_path(args.config)
        cfg = envconfig.load_config(config_path)
    except envconfig.ConfigError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    check_submit_host(cfg)

    pipefile = Path(args.pipefile)
    if not pipefile.is_file():
        print(f"{pipefile} does not exist, exiting")
        sys.exit(1)

    mem = f"{args.mem}G"

    with open(pipefile) as fd:
        lines = fd.readlines()

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        mousname = parts[0]
        recipe = parts[1] if len(parts) > 1 else "calimage"

        pipejob_parts = [
            str(CALIBPIPEIF),
            "--staticobscal",
            f"--mous={mousname}",
            f"--recipe={recipe}",
        ]
        if args.config:
            pipejob_parts.append(f"--config={args.config}")
        if args.env:
            pipejob_parts.append(f"--env={args.env}")
        pipejob_parts.extend(args.extra_args)
        pipejob = " ".join(pipejob_parts)

        name = job_name(pipejob)
        print(f"job name = {name}")
        outfile = args.outfile or f"batch.{name}.out"
        errfile = args.errfile or f"batch.{name}.err"

        user = os.environ.get("USER", "")
        with tempfile.NamedTemporaryFile(
            mode="w", prefix=f"{user}_sbatch.", delete=False
        ) as sbatch_fd:
            sbatch_fd.write(build_sbatch_script(pipejob))
            sbatch_path = sbatch_fd.name

        try:
            cmd = [
                "sbatch",
                "-p", args.queue,
                "-N", args.node,
                "-n", str(args.cores),
                "--export=ALL",
                f"--job-name={name}",
                f"--mail-user={user}",
                f"--mail-type={args.mail_type}",
                f"--mem={mem}",
                "-o", outfile,
                "-e", errfile,
                sbatch_path,
            ]
            print("    " + " ".join(cmd))
            subprocess.run(cmd, check=True)
        finally:
            if os.path.exists(sbatch_path):
                os.unlink(sbatch_path)

        print("Waiting 5 seconds to minimize directory naming collision risk")
        time.sleep(5)


if __name__ == "__main__":
    main()
