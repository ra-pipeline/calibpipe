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
from calibpipe.templates import render_template

# Fallback path to calibPipeIF executable (can be overridden or patched)
_candidate = Path(__file__).resolve().parent.parent.parent / "scripts" / "calibPipeIF.py"
if _candidate.is_file():
    CALIBPIPEIF = _candidate
else:
    CALIBPIPEIF = Path(shutil.which("calibpipe") or "calibpipe")


def build_parser() -> argparse.ArgumentParser:
    """Build the parser for Slurm batch submission.

    Returns:
        Configured argument parser for `calibpipe batch`.
    """
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pipefile", help="File with one '<mous_uid> [recipe]' per line")
    p.add_argument("--config", help="Path to TOML config (default: resolved config.toml)")
    p.add_argument(
        "--no-site-config",
        action="store_true",
        default=False,
        help="Do not load site-level configuration",
    )
    p.add_argument("--env", help="[envs.<name>] to use; default: config's default_env")
    p.add_argument("-c", "--cores", type=int, default=None, dest="cores",
                   help="Tasks/cores per job (--ntasks) (default: from [batch].cores or 8)")
    p.add_argument("-M", "--mail-type", default=None, dest="mail_type",
                   help="Slurm --mail-type (default: from [batch].mail_type or ALL)")
    p.add_argument("-n", "--node", default=None, dest="node",
                   help="Number of nodes --nodes (default: from [batch].node or 1)")
    p.add_argument("-o", "--outfile", dest="outfile", help="Slurm stdout file (default: batch.<job>.out)")
    p.add_argument("-e", "--errfile", dest="errfile", help="Slurm stderr file (default: batch.<job>.err)")
    # Memory options (mutually exclusive)
    mem_group = p.add_mutually_exclusive_group()
    mem_group.add_argument("-m", "--mem", type=int, default=None, dest="mem",
                           help="GB total memory per job (default: from [batch].mem or 248); "
                                "mutually exclusive with --mem-per-cpu")
    mem_group.add_argument("--mem-per-cpu", default=None, dest="mem_per_cpu",
                           help="Memory per CPU (e.g. '30G'); mutually exclusive with --mem")
    # Walltime / scheduling
    p.add_argument("-t", "--walltime", default=None, dest="walltime",
                   help="Slurm --time walltime limit (e.g. '24:00:00'); omitted if unset")
    p.add_argument("--nodelist", default=None, dest="nodelist",
                   help="Pin job to a specific node or node list (--nodelist)")
    p.add_argument("--chdir", default=None, dest="chdir",
                   help="Slurm --chdir working directory override")
    # CPU topology
    p.add_argument("--cpus-per-task", type=int, default=None, dest="cpus_per_task",
                   help="CPUs per MPI task (--cpus-per-task); useful for mpicasa "
                        "(total cores = ntasks × cpus-per-task)")
    p.add_argument("--ntasks-per-core", type=int, default=None, dest="ntasks_per_core",
                   help="Tasks per physical core; set to 1 to disable hyperthreading")
    p.add_argument("--hint", default=None, dest="hint",
                   help="Slurm scheduling hint (e.g. 'nomultithread')")
    p.add_argument("--distribution", default=None, dest="distribution",
                   help="Task distribution across nodes/sockets (e.g. 'cyclic:cyclic')")
    # Requeue
    p.add_argument("--no-requeue", dest="no_requeue", action="store_true", default=None,
                   help="Add --no-requeue directive (default: on; prevents silent resubmission)")
    p.add_argument("--requeue", dest="no_requeue", action="store_false",
                   help="Allow Slurm to requeue job on node failure")
    # Queue selection
    queue = p.add_mutually_exclusive_group()
    queue.add_argument("-p", dest="queue", action="store_const", const="plwg", default=None,
                       help="Submit to the plwg queue (default)")
    queue.add_argument("-b", dest="queue", action="store_const", const="batch2",
                       help="Submit to the batch2 queue")
    p.add_argument("--extra-arg", action="append", default=[], dest="extra_args",
                   help="Extra flag passed through to calibpipe driver, repeatable")
    return p


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for batch submission.

    Args:
        args: Optional argument sequence.

    Returns:
        Parsed namespace.
    """
    parser = build_parser()
    if args is None and len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)
    return parser.parse_args(args)


def check_submit_host(config: dict) -> None:
    """Enforce an optional submit-host restriction from configuration.

    Args:
        config: Parsed TOML configuration dictionary.
    """
    submit_host = config.get("site", {}).get("submit_host")
    if not submit_host:
        return
    hostname = subprocess.getoutput("hostname").strip()
    if hostname != submit_host:
        print(f"Jobs must be submitted from {submit_host}, exiting.")
        sys.exit(1)


def _sbatch_directive(flag: str, value: object) -> str | None:
    """Return a ``#SBATCH --flag=value`` line, or None if value is None, empty, or False."""
    if value is None or value == "" or value is False:
        return None
    if value is True:
        return f"#SBATCH --{flag}"
    return f"#SBATCH --{flag}={value}"


def build_sbatch_script(
    pipejob: str,
    *,
    queue: str,
    node: str,
    cores: int,
    mem: str | None,
    job_name: str,
    mail_user: str,
    mail_type: str,
    outfile: str,
    errfile: str,
    # Optional directives
    walltime: str | None = None,
    nodelist: str | None = None,
    chdir: str | None = None,
    cpus_per_task: int | None = None,
    mem_per_cpu: str | None = None,
    hint: str | None = None,
    ntasks_per_core: int | None = None,
    distribution: str | None = None,
    no_requeue: bool = True,
) -> str:
    """Create the shell script body submitted to Slurm.

    All Slurm resource parameters are embedded as ``#SBATCH`` directives so
    the saved script is a self-contained record of the submission.

    ``mem`` and ``mem_per_cpu`` are mutually exclusive: if ``mem_per_cpu`` is
    set, ``mem`` is ignored (matching Slurm's own constraint).

    Args:
        pipejob: Fully assembled `calibpipe` execution command.
        queue: Slurm partition name.
        node: Number of nodes (``--nodes``).
        cores: Number of tasks (``--ntasks``).
        mem: Total memory string (e.g. ``"248G"``); ignored when ``mem_per_cpu`` is set.
        job_name: Slurm job name.
        mail_user: Email address for Slurm notifications.
        mail_type: Slurm ``--mail-type`` value (e.g. ``"ALL"``).
        outfile: Path for Slurm stdout log.
        errfile: Path for Slurm stderr log.
        walltime: Slurm ``--time`` walltime limit (e.g. ``"24:00:00"``).
        nodelist: Pin job to a specific node or node list.
        chdir: Slurm working directory override.
        cpus_per_task: CPUs per MPI task (useful for mpicasa).
        mem_per_cpu: Memory per CPU (e.g. ``"30G"``); mutually exclusive with ``mem``.
        hint: Slurm scheduling hint (e.g. ``"nomultithread"``).
        ntasks_per_core: Tasks per physical core (e.g. ``1`` to disable HT).
        distribution: Task distribution policy (e.g. ``"cyclic:cyclic"``).
        no_requeue: Emit ``--no-requeue`` to prevent silent resubmission.

    Returns:
        Rendered shell script contents.
    """
    # Build the optional directives block in the same order as pcasa.py's job_slurm()
    optional_lines = []
    for line in [
        _sbatch_directive("no-requeue", no_requeue),
        _sbatch_directive("time", walltime),
        _sbatch_directive("nodelist", nodelist),
        _sbatch_directive("chdir", chdir),
        _sbatch_directive("cpus-per-task", cpus_per_task),
        _sbatch_directive("ntasks-per-core", ntasks_per_core),
        _sbatch_directive("hint", hint),
        _sbatch_directive("distribution", distribution),
        # mem and mem-per-cpu are mutually exclusive; mem-per-cpu wins
        _sbatch_directive("mem-per-cpu", mem_per_cpu) if mem_per_cpu else _sbatch_directive("mem", mem),
    ]:
        if line is not None:
            optional_lines.append(line)

    extra_directives = "\n".join(optional_lines)

    return render_template(
        'slurm_job.sh.in',
        pipejob=pipejob,
        queue=queue,
        node=node,
        cores=cores,
        job_name=job_name,
        mail_user=mail_user,
        mail_type=mail_type,
        outfile=outfile,
        errfile=errfile,
        extra_directives=extra_directives,
    )


def job_name(pipejob: str) -> str:
    """Derive the standard Slurm job name from a command line.

    Args:
        pipejob: Fully assembled `calibpipe` execution command.

    Returns:
        Stable job name containing the MOUS identifier and current timestamp.
    """
    mous = ""
    for tok in pipejob.split():
        if "mous" in tok:
            mous = tok.split("=", 1)[-1][11:]
            mous = mous.strip("/").replace("/", "_")
            break
    timestr = time.strftime("%Y%m%d-%H%M%S")
    return f"{mous}_{timestr}"


def print_queue(user: str) -> None:
    """Print the user's active Slurm queue matching pcasa.py format.

    Args:
        user: Username whose jobs to query with squeue.
    """
    squeue_exe = shutil.which("squeue")
    if not squeue_exe or not user:
        return
    squeue_format = "%7i %13P %9u %7T %11M %11l %5D %2C %2c/%7m %16R %50j %50Z"
    cmd = [squeue_exe, f"--format={squeue_format}", "-u", user]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            print("\n" + res.stdout.strip())
    except OSError:
        pass


def main(argv: Sequence[str] | None = None) -> None:
    """Submit one or more pipeline jobs to Slurm.

    Args:
        argv: Optional argument sequence. When omitted, values are read from
            `sys.argv`.
    """
    args = parse_args(argv)

    try:
        cfg = envconfig.load_merged_config(
            cli_arg=args.config,
            include_site=not getattr(args, "no_site_config", False),
        )
    except envconfig.ConfigError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    batch_opts = envconfig.resolve_batch_options(cfg, args)
    check_submit_host(cfg)

    pipefile = batch_opts.pipefile
    if not pipefile.is_file():
        print(f"{pipefile} does not exist, exiting")
        sys.exit(1)

    with open(pipefile, encoding="utf-8") as fd:
        lines = fd.readlines()

    valid_lines = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]

    submitted = 0
    for idx, line in enumerate(valid_lines, 1):
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
        if getattr(args, "no_site_config", False):
            pipejob_parts.append("--no-site-config")
        if batch_opts.env_name:
            pipejob_parts.append(f"--env={batch_opts.env_name}")
        pipejob_parts.extend(batch_opts.extra_args)
        pipejob = " ".join(pipejob_parts)

        name = job_name(pipejob)
        print(f"job name = {name}")
        outfile = batch_opts.outfile or f"batch.{name}.out"
        errfile = batch_opts.errfile or f"batch.{name}.err"

        user = os.environ.get('USER', '')
        # --mem and --mem-per-cpu are mutually exclusive; pass None for mem when mem_per_cpu is set
        mem_str = None if batch_opts.mem_per_cpu else f"{batch_opts.mem}G"
        script_body = build_sbatch_script(
            pipejob,
            queue=batch_opts.queue,
            node=batch_opts.node,
            cores=batch_opts.cores,
            mem=mem_str,
            job_name=name,
            mail_user=user,
            mail_type=batch_opts.mail_type,
            outfile=outfile,
            errfile=errfile,
            walltime=batch_opts.walltime,
            nodelist=batch_opts.nodelist,
            chdir=batch_opts.chdir,
            cpus_per_task=batch_opts.cpus_per_task,
            mem_per_cpu=batch_opts.mem_per_cpu,
            hint=batch_opts.hint,
            ntasks_per_core=batch_opts.ntasks_per_core,
            distribution=batch_opts.distribution,
            no_requeue=batch_opts.no_requeue,
        )

        # Persist a copy alongside the .out/.err logs for record keeping.
        # A non-fatal warning is issued if the directory does not yet exist
        # so that the job submission itself is never blocked by a missing dir.
        script_record = str(Path(outfile).with_suffix('.sbatch'))
        try:
            with open(script_record, 'w', encoding='utf-8') as f:
                f.write(script_body)
            print(f'    script saved → {script_record}')
        except OSError as exc:
            print(f'    WARNING: could not save script record {script_record}: {exc}')

        with tempfile.NamedTemporaryFile(
            mode='w', prefix=f'{user}_sbatch.', suffix='.sh', delete=False
        ) as sbatch_fd:
            sbatch_fd.write(script_body)
            sbatch_path = sbatch_fd.name

        try:
            # All resource parameters are embedded as #SBATCH directives in
            # the script, so only the script path is needed here.
            cmd = ['sbatch', sbatch_path]
            print('    ' + ' '.join(cmd))
            subprocess.run(cmd, check=True)
            submitted += 1
        finally:
            if os.path.exists(sbatch_path):
                os.unlink(sbatch_path)

        # Briefly pause between consecutive submissions so timestamps remain unique
        if idx < len(valid_lines):
            time.sleep(1)

    if submitted > 0:
        current_user = os.environ.get('USER', '')
        if current_user:
            print_queue(current_user)


if __name__ == "__main__":
    main()
