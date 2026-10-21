"""Submit a batch of calibpipe runs to Slurm, one per line of a pipefile.

Each non-comment line of the pipefile is "<mous_uid> [recipe]" (recipe
defaults to calimage).
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from calibpipe import config as envconfig
from calibpipe.templates import render_template

# Fallback path to calibPipeIF executable (can be overridden or patched)
_candidate = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "calibPipeIF.py"
)
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
    p.add_argument(
        "pipefile",
        nargs="?",
        default=None,
        help="Optional pipefile with one '<mous_uid> [recipe]' per line",
    )
    p.add_argument(
        "--config", help="Path to TOML config (default: resolved config.toml)"
    )
    p.add_argument(
        "--no-site-config",
        action="store_true",
        default=False,
        help="Do not load site-level configuration",
    )
    p.add_argument("--env", help="[envs.<name>] to use; default: config's default_env")
    p.add_argument(
        "-c",
        "--cores",
        "--ntasks",
        type=int,
        default=None,
        dest="cores",
        help="Tasks/CPUs per job (Slurm: --ntasks; HTCondor: request_cpus; default: from [batch].cores or 8)",
    )
    p.add_argument(
        "-M",
        "--mail-type",
        default=None,
        dest="mail_type",
        help="Notification policy (Slurm: --mail-type; HTCondor: notification; default: ALL)",
    )
    p.add_argument(
        "-n",
        "--node",
        default=None,
        dest="node",
        help="[Slurm only] Number of nodes --nodes (default: from [batch].node or 1)",
    )
    p.add_argument(
        "-o",
        "--outfile",
        dest="outfile",
        help="Stdout log file path (Slurm: --output; HTCondor: output; default: batch.<job>.out)",
    )
    p.add_argument(
        "-e",
        "--errfile",
        dest="errfile",
        help="Stderr log file path (Slurm: --error; HTCondor: error; default: batch.<job>.err)",
    )
    p.add_argument(
        "--logdir",
        default=None,
        dest="logdir",
        help="Directory for scheduler log and script output (Slurm & HTCondor; default: from [batch].logdir or [paths].scipipe_logdir)",
    )
    # Memory options (mutually exclusive)
    mem_group = p.add_mutually_exclusive_group()
    mem_group.add_argument(
        "-m",
        "--mem",
        type=int,
        default=None,
        dest="mem",
        help="Total memory in GB per job (Slurm: --mem; HTCondor: request_memory; default: 248); "
        "mutually exclusive with --mem-per-cpu",
    )
    mem_group.add_argument(
        "--mem-per-cpu",
        default=None,
        dest="mem_per_cpu",
        help="Memory per logical CPU (Slurm: --mem-per-cpu; HTCondor: request_memory = cores × mem_per_cpu; e.g. '5G'); "
        "mutually exclusive with --mem",
    )
    # Walltime / scheduling
    p.add_argument(
        "-t",
        "--walltime",
        default=None,
        dest="walltime",
        help="[Slurm only] Walltime limit --time (e.g. '24:00:00'); omitted if unset",
    )
    p.add_argument(
        "--nodelist",
        default=None,
        dest="nodelist",
        help="Target host pinning (Slurm: --nodelist; HTCondor: TARGET.Machine)",
    )
    p.add_argument(
        "--chdir",
        default=None,
        dest="chdir",
        help="Working directory override (Slurm: --chdir; HTCondor: initialdir)",
    )
    # CPU topology
    p.add_argument(
        "--cpus-per-task",
        type=int,
        default=None,
        dest="cpus_per_task",
        help="[Slurm only] Logical CPUs / hardware threads per task (--cpus-per-task) for OpenMP/multi-threading; "
        "total allocated CPUs = ntasks × cpus-per-task",
    )
    p.add_argument(
        "--ntasks-per-core",
        type=int,
        default=None,
        dest="ntasks_per_core",
        help="[Slurm only] Tasks per physical silicon core (--ntasks-per-core); set to 1 to spread tasks across physical cores and prevent hyperthread sharing",
    )
    p.add_argument(
        "--hint",
        default=None,
        dest="hint",
        help="[Slurm only] Scheduler placement hint (--hint, e.g. 'nomultithread')",
    )
    p.add_argument(
        "--distribution",
        default=None,
        dest="distribution",
        help="[Slurm only] Task distribution across nodes/sockets (--distribution, e.g. 'cyclic:cyclic')",
    )
    # Requeue
    p.add_argument(
        "--no-requeue",
        dest="no_requeue",
        action="store_true",
        default=None,
        help="[Slurm only] Prevent silent job resubmission on failure (--no-requeue, default: on)",
    )
    p.add_argument(
        "--requeue",
        dest="no_requeue",
        action="store_false",
        help="[Slurm only] Allow Slurm to requeue job on node failure",
    )
    # Batch profile selection
    p.add_argument(
        "-p",
        "--profile",
        "--batch-profile",
        dest="profile",
        default=None,
        help="Named profile from [profiles.<name>] or [batches.<name>] in config",
    )
    # Queue / partition selection
    queue = p.add_mutually_exclusive_group()
    queue.add_argument(
        "-b",
        dest="partition",
        action="store_const",
        const="batch2",
        default=None,
        help="Submit to the batch2 partition",
    )
    queue.add_argument(
        "--partition",
        "--queue",
        dest="partition",
        default=None,
        help="Slurm partition (--partition) or HTCondor (+partition) name (overrides [batch].partition or profile)",
    )
    p.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        dest="extra_args",
        help="Extra flag passed through to calibpipe driver, repeatable",
    )
    p.add_argument(
        "--log2term",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="log2term",
        help="Mirror CASA log messages to terminal/stdout in real time",
    )
    p.add_argument(
        "--omp-num-threads",
        type=int,
        default=None,
        dest="omp_num_threads",
        help="OpenMP thread count override for batch execution",
    )
    p.add_argument(
        "--openblas-num-threads",
        type=int,
        default=None,
        dest="openblas_num_threads",
        help="OpenBLAS thread count override for batch execution",
    )
    p.add_argument(
        "--scheduler",
        choices=["slurm", "htcondor"],
        default=None,
        dest="scheduler",
        help="Batch scheduler to target: 'slurm' (default) or 'htcondor'",
    )
    p.add_argument(
        "--requirements",
        default=None,
        dest="requirements",
        help="[HTCondor only] ClassAd requirements expression override (e.g. 'none', 'default', or custom expression)",
    )
    p.add_argument(
        "--request-gpus",
        "--gpus",
        type=int,
        default=None,
        dest="request_gpus",
        help="Request number of GPUs (HTCondor: request_gpus; Slurm: --gpus)",
    )
    p.add_argument(
        "--pool",
        "--central-manager",
        "--central-manager-host",
        type=str,
        default=None,
        dest="pool",
        help="[HTCondor only] Central Manager host to query or route via (-pool <host>)",
    )
    p.add_argument(
        "--schedd",
        "--schedd-name",
        "--remote",
        type=str,
        default=None,
        dest="schedd",
        help="[HTCondor only] Target schedd daemon name (condor_submit -name <schedd>; avoids implicit -spool from -remote)",
    )
    p.add_argument(
        "--spool",
        action="store_true",
        default=None,
        dest="spool",
        help="[HTCondor only] Spool job sandbox over the network to the remote schedd (-spool)",
    )
    p.add_argument(
        "--no-spool",
        action="store_false",
        dest="spool",
        help="[HTCondor only] Do not spool job files over the network",
    )
    p.add_argument(
        "--should-transfer-files",
        type=str,
        default=None,
        dest="should_transfer_files",
        help="[HTCondor only] File transfer mode: 'YES', 'NO', or 'IF_NEEDED'",
    )
    p.add_argument(
        "--when-to-transfer-output",
        type=str,
        default=None,
        dest="when_to_transfer_output",
        help="[HTCondor only] When to transfer output: 'ON_EXIT' or 'ON_EXIT_OR_EVICT'",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="Print submit scripts and commands without submitting jobs to the scheduler (Slurm & HTCondor)",
    )
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


def _build_batch_env_exports(
    *,
    outfile: str | Path,
    errfile: str | Path,
    script_record_path: str | Path | None = None,
    pipejob: str = "",
    omp_num_threads: int | None = None,
    openblas_num_threads: int | None = None,
    cpus_per_task: int | None = None,
    batch_env_exports: str | None = None,
    is_pixi: bool = False,
) -> str:
    """Build the shell export statements injected into batch wrapper scripts.

    Exports tracking variables (CALIBPIPE_BATCH_OUTFILE, CALIBPIPE_BATCH_ERRFILE,
    CALIBPIPE_BATCH_SCRIPT), UV and Pixi execution flags, and thread counts
    (OMP_NUM_THREADS, OPENBLAS_NUM_THREADS).

    Returns:
        Formatted export block with leading newline, or empty string.
    """
    if batch_env_exports is not None:
        return ("\n" + batch_env_exports.strip()) if batch_env_exports.strip() else ""
    if script_record_path is None:
        return ""

    env_lines = [
        f"export CALIBPIPE_BATCH_OUTFILE={shlex.quote(str(outfile))}",
        f"export CALIBPIPE_BATCH_ERRFILE={shlex.quote(str(errfile))}",
        f"export CALIBPIPE_BATCH_SCRIPT={shlex.quote(str(script_record_path))}",
    ]
    if "uv" in pipejob or "UV_NO_SYNC" in os.environ:
        env_lines.append("export UV_NO_SYNC=1")

    if (
        is_pixi
        or "pixi" in pipejob
        or "PIXI_FROZEN" in os.environ
        or "PIXI_NO_INSTALL" in os.environ
        or "PIXI_DIR" in os.environ
    ):
        pixi_frozen = os.environ.get("PIXI_FROZEN", "true")
        pixi_no_install = os.environ.get("PIXI_NO_INSTALL", "true")
        env_lines.append(f"export PIXI_FROZEN={pixi_frozen}")
        env_lines.append(f"export PIXI_NO_INSTALL={pixi_no_install}")

    effective_omp = omp_num_threads if omp_num_threads is not None else cpus_per_task
    if effective_omp is not None:
        env_lines.append(f"export OMP_NUM_THREADS={effective_omp}")

    effective_openblas = openblas_num_threads if openblas_num_threads is not None else cpus_per_task
    if effective_openblas is not None:
        env_lines.append(f"export OPENBLAS_NUM_THREADS={effective_openblas}")

    return "\n" + "\n".join(env_lines)


def build_sbatch_script(
    pipejob: str,
    *,
    partition: str = "plwg",
    queue: str | None = None,
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
    script_record_path: str | None = None,
    batch_env_exports: str | None = None,
    omp_num_threads: int | None = None,
    openblas_num_threads: int | None = None,
    request_gpus: int = 0,
    is_pixi: bool = False,
) -> str:
    """Create the shell script body submitted to Slurm.

    All Slurm resource parameters are embedded as ``#SBATCH`` directives so
    the saved script is a self-contained record of the submission.

    ``mem`` and ``mem_per_cpu`` are mutually exclusive: if ``mem_per_cpu`` is
    set, ``mem`` is ignored (matching Slurm's own constraint).

    Args:
        pipejob: Fully assembled `calibpipe` execution command.
        partition: Slurm partition name.
        queue: Backward-compatible alias for `partition`.
        node: Number of nodes (``--nodes``).
        cores: Number of tasks (``--ntasks``; total allocated logical CPUs = ntasks × cpus_per_task).
        mem: Total memory string (e.g. ``"248G"``); ignored when ``mem_per_cpu`` is set.
        job_name: Slurm job name.
        mail_user: Email address for Slurm notifications.
        mail_type: Slurm ``--mail-type`` value (e.g. ``"ALL"``).
        outfile: Path for Slurm stdout log.
        errfile: Path for Slurm stderr log.
        walltime: Slurm ``--time`` walltime limit (e.g. ``"24:00:00"``).
        nodelist: Pin job to a specific node or node list.
        chdir: Slurm working directory override.
        cpus_per_task: Logical CPUs (threads) per task (``--cpus-per-task``; for OpenMP/multi-threading).
        mem_per_cpu: Memory per logical CPU (e.g. ``"5G"``); mutually exclusive with ``mem``.
        hint: Slurm scheduling hint (e.g. ``"nomultithread"``).
        ntasks_per_core: Tasks per physical silicon core (e.g. ``1`` to prevent hyperthread sharing).
        distribution: Task distribution policy (e.g. ``"cyclic:cyclic"``).
        no_requeue: Emit ``--no-requeue`` to prevent silent resubmission.
        script_record_path: Optional path to the saved submission script for environment export.
        batch_env_exports: Optional explicit export block to embed in the script.

    Returns:
        Rendered shell script contents.
    """
    active_partition = queue if queue is not None else partition

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
        _sbatch_directive("gpus", request_gpus) if request_gpus else None,
        # mem and mem-per-cpu are mutually exclusive; mem-per-cpu wins
        _sbatch_directive("mem-per-cpu", mem_per_cpu)
        if mem_per_cpu
        else _sbatch_directive("mem", mem),
    ]:
        if line is not None:
            optional_lines.append(line)

    extra_directives = "\n".join(optional_lines)

    active_env_exports = _build_batch_env_exports(
        outfile=outfile,
        errfile=errfile,
        script_record_path=script_record_path,
        pipejob=pipejob,
        omp_num_threads=omp_num_threads,
        openblas_num_threads=openblas_num_threads,
        cpus_per_task=cpus_per_task,
        batch_env_exports=batch_env_exports,
        is_pixi=is_pixi,
    )

    return render_template(
        "slurm_job.sh.in",
        pipejob=pipejob,
        partition=active_partition,
        queue=active_partition,
        node=node,
        cores=cores,
        job_name=job_name,
        mail_user=mail_user,
        mail_type=mail_type,
        outfile=outfile,
        errfile=errfile,
        extra_directives=extra_directives,
        batch_env_exports=active_env_exports,
    )


def build_htcondor_script(
    pipejob: str,
    *,
    partition: str = "batch",
    cores: int = 4,
    mem: str | None = None,
    mem_per_cpu: str | None = None,
    job_name: str,
    outfile: str,
    errfile: str,
    wrapper_script_path: str,
    mail_type: str = "Always",
    chdir: str | None = None,
    nodelist: str | None = None,
    requirements: str | None = None,
    script_record_path: str | None = None,
    batch_env_exports: str | None = None,
    omp_num_threads: int | None = None,
    openblas_num_threads: int | None = None,
    cpus_per_task: int | None = None,
    request_gpus: int = 0,
    is_pixi: bool = False,
    spool: bool = False,
    should_transfer_files: str | bool | None = None,
    when_to_transfer_output: str | None = None,
) -> tuple[str, str]:
    """Create the HTCondor submit description (.htc) and shell wrapper script (.sh).

    Args:
        pipejob: Fully assembled calibpipe command line.
        partition: HTCondor partition (+partition directive).
        cores: Requested number of CPUs (request_cpus).
        mem: Memory string (request_memory, e.g. "32G").
        mem_per_cpu: Memory per CPU (e.g. "8G"); overrides mem.
        job_name: Batch job name (batch_name).
        outfile: Path for stdout log (output).
        errfile: Path for stderr log (error).
        wrapper_script_path: Path to the executable shell wrapper script.
        mail_type: Notification setting (notification).
        chdir: Initial working directory (initialdir).
        nodelist: Optional pinned machine name (TARGET.Machine == "<nodelist>").
        requirements: Optional custom ClassAd requirements expression.
        script_record_path: Optional path to the saved submission script for environment export.
        batch_env_exports: Optional explicit export block to embed in the script.

    Returns:
        Tuple of (submit_file_content, wrapper_script_content).
    """
    active_env_exports = _build_batch_env_exports(
        outfile=outfile,
        errfile=errfile,
        script_record_path=script_record_path,
        pipejob=pipejob,
        omp_num_threads=omp_num_threads,
        openblas_num_threads=openblas_num_threads,
        cpus_per_task=cpus_per_task,
        batch_env_exports=batch_env_exports,
        is_pixi=is_pixi,
    )

    runner_script = render_template(
        "htcondor_job.sh.in",
        pipejob=pipejob,
        batch_env_exports=active_env_exports,
    )

    if mem_per_cpu:
        val_str = mem_per_cpu.strip()
        num_part = ""
        unit_part = ""
        for char in val_str:
            if char.isdigit() or char == ".":
                num_part += char
            else:
                unit_part += char
        try:
            total_num = int(float(num_part) * cores)
            request_memory = f"{total_num}{unit_part}"
        except ValueError:
            request_memory = mem_per_cpu
    else:
        request_memory = mem or "32G"
    extra_req = f' && ( TARGET.Machine == "{nodelist}" )' if nodelist else ""
    log_file = f"condor.{job_name}.$(ClusterId).log"

    # Map Slurm mail_type to valid HTCondor notification keywords:
    # 'Never', 'Always', 'Complete', or 'Error'
    htc_notification_map = {
        "all": "Always",
        "always": "Always",
        "none": "Never",
        "never": "Never",
        "end": "Complete",
        "complete": "Complete",
        "fail": "Error",
        "error": "Error",
    }
    raw_notif = str(mail_type).lower().strip() if mail_type else ""
    notification = htc_notification_map.get(raw_notif, "Always")

    # Determine requirements expression
    if requirements is not None:
        req_clean = requirements.strip()
        if req_clean.lower() in ("none", "false", "any"):
            req_expr = f"NumJobStarts == 0{extra_req}"
        elif req_clean.lower() in ("default", "cluster", "nrao"):
            req_expr = f"( {partition} == True ) && ( HasLustre == True ) && ( NumJobStarts == 0 ){extra_req}"
        else:
            req_expr = f"{req_clean}{extra_req}"
    else:
        req_expr = f"( {partition} == True ) && ( HasLustre == True ) && ( NumJobStarts == 0 ){extra_req}"

    # Resolve file transfer directives (required for remote spool submit to avoid job hold)
    transfer_files_val = should_transfer_files
    if transfer_files_val is None and spool:
        transfer_files_val = "YES"
    elif isinstance(transfer_files_val, bool):
        transfer_files_val = "YES" if transfer_files_val else "NO"
    elif isinstance(transfer_files_val, str):
        transfer_files_val = transfer_files_val.strip().upper()

    transfer_output_val = when_to_transfer_output
    if transfer_output_val is None and transfer_files_val == "YES":
        transfer_output_val = "ON_EXIT"
    elif isinstance(transfer_output_val, str):
        transfer_output_val = transfer_output_val.strip().upper()

    extra_lines = []
    if transfer_files_val:
        extra_lines.append(f"should_transfer_files = {transfer_files_val}")
    if transfer_output_val:
        extra_lines.append(f"when_to_transfer_output = {transfer_output_val}")
    extra_directives_str = "\n".join(extra_lines)

    htc_content = render_template(
        "htcondor_job.htc.in",
        partition=partition,
        requirements=req_expr,
        request_gpus=request_gpus,
        request_memory=request_memory,
        request_cpus=cores,
        batch_name=job_name,
        initialdir=chdir or "./",
        output=outfile,
        error=errfile,
        log=log_file,
        notification=notification,
        arguments=wrapper_script_path,
        extra_directives=extra_directives_str,
    )
    return htc_content, runner_script


def job_name(
    pipejob: str, profile_name: str | None = None
) -> tuple[str, str]:
    """Derive the standard batch job name and safe filename prefix from a command line.

    Args:
        pipejob: Fully assembled `calibpipe` execution command.
        profile_name: Optional named profile for single-job submission.

    Returns:
        Tuple of (display_name, file_safe_name).
        display_name retains profile name (e.g. including '/') for scheduler display (squeue/condor_q),
        while file_safe_name normalizes path separators for stdout/stderr/sbatch file paths.
    """
    prefix = ""
    for tok in pipejob.split():
        if tok.startswith("--mous="):
            prefix = tok.split("=", 1)[-1][11:]
            prefix = prefix.strip("/").replace("/", "_")
            break

    timestr = time.strftime("%Y%m%d-%H%M%S")
    if prefix:
        full_display = f"{prefix}_{timestr}"
        full_safe = full_display
    elif profile_name:
        full_display = f"{profile_name}_{timestr}"
        safe_prefix = profile_name.replace("/", "_").replace("@", "_").replace(":", "_")
        full_safe = f"{safe_prefix}_{timestr}"
    else:
        full_display = f"job_{timestr}"
        full_safe = full_display

    return full_display, full_safe


def print_queue(
    user: str,
    scheduler: str = "slurm",
    *,
    pool: str | None = None,
    schedd: str | None = None,
) -> None:
    """Print the user's active batch queue matching pcasa.py format.

    Args:
        user: Username whose jobs to query.
        scheduler: Target scheduler backend ('slurm' or 'htcondor').
        pool: Optional HTCondor central manager host to query (-pool).
        schedd: Optional HTCondor schedd daemon name to query (-name).
    """
    if not user:
        return

    if scheduler == "htcondor":
        condor_exe = shutil.which("condor_q")
        if not condor_exe:
            return
        cmd = [condor_exe, user]
        if pool:
            cmd.extend(["-pool", pool])
        if schedd:
            cmd.extend(["-name", schedd])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                print("\n" + res.stdout.strip())
        except OSError:
            pass
        return

    squeue_exe = shutil.which("squeue")
    if not squeue_exe:
        return
    squeue_format = "%7i %13P %9u %7T %11M %11l %5D %2C %2c/%7m %16R %50j %50Z"
    cmd = [squeue_exe, f"--format={squeue_format}", "-u", user]
    try:
        time.sleep(0.5)
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            print("\n" + res.stdout.strip())
    except OSError:
        pass


def _submit_tempfile_job(
    submit_cmd: str | Sequence[str],
    content: str,
    *,
    prefix: str,
    suffix: str,
    dry_run: bool = False,
) -> bool:
    """Write job content to a temporary file, run submit command, and clean up.

    Returns:
        True if the job was submitted (or dry-run simulated), False on error.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", prefix=prefix, suffix=suffix, delete=False
    ) as fd:
        fd.write(content)
        temp_path = fd.name

    try:
        base_cmd = [submit_cmd] if isinstance(submit_cmd, str) else list(submit_cmd)
        cmd = base_cmd + [temp_path]
        print("    " + " ".join(cmd))
        if dry_run:
            print("    [DRY RUN] Would execute: " + " ".join(cmd))
            return True
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as exc:
        print(f"    ERROR: Job submission failed (exit status {exc.returncode})")
        return False
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _dispatch_htcondor(
    *,
    pipejob: str,
    display_name: str,
    outfile: str,
    errfile: str,
    mem_str: str | None,
    batch_opts: envconfig.ResolvedBatchOptions,
    user: str,
) -> bool:
    """Build and dispatch an HTCondor batch job."""
    script_record_sh = str(Path(outfile).with_suffix(".sh"))
    script_record_htc = str(Path(outfile).with_suffix(".htc"))

    htc_body, runner_body = build_htcondor_script(
        pipejob,
        partition=batch_opts.partition,
        cores=batch_opts.cores,
        mem=mem_str,
        mem_per_cpu=batch_opts.mem_per_cpu,
        job_name=display_name,
        outfile=outfile,
        errfile=errfile,
        wrapper_script_path=str(Path(script_record_sh).resolve()),
        mail_type=batch_opts.mail_type,
        chdir=batch_opts.chdir,
        nodelist=batch_opts.nodelist,
        requirements=batch_opts.requirements,
        script_record_path=script_record_htc,
        omp_num_threads=getattr(batch_opts, "omp_num_threads", None),
        openblas_num_threads=getattr(batch_opts, "openblas_num_threads", None),
        cpus_per_task=getattr(batch_opts, "cpus_per_task", None),
        request_gpus=getattr(batch_opts, "request_gpus", 0),
        is_pixi=bool(getattr(batch_opts, "pixi_dir", None)),
        spool=getattr(batch_opts, "spool", False),
        should_transfer_files=getattr(batch_opts, "should_transfer_files", None),
        when_to_transfer_output=getattr(batch_opts, "when_to_transfer_output", None),
    )

    script_record_sh_path = Path(script_record_sh).resolve()
    script_record_htc_path = Path(script_record_htc).resolve()
    try:
        with open(script_record_sh_path, "w", encoding="utf-8") as f:
            f.write(runner_body)
        os.chmod(script_record_sh_path, 0o755)
        with open(script_record_htc_path, "w", encoding="utf-8") as f:
            f.write(htc_body)
        print(f"    script saved → {script_record_htc_path}")
    except OSError as exc:
        print(f"    ERROR: could not save HTCondor script records: {exc}")
        return False

    cmd = ["condor_submit"]
    if getattr(batch_opts, "pool", None):
        cmd.extend(["-pool", batch_opts.pool])
    if getattr(batch_opts, "schedd", None):
        cmd.extend(["-name", batch_opts.schedd])
    if getattr(batch_opts, "spool", False):
        cmd.append("-spool")

    return _submit_tempfile_job(
        cmd,
        htc_body,
        prefix=f"{user}_htc.",
        suffix=".htc",
        dry_run=batch_opts.dry_run,
    )


def _dispatch_slurm(
    *,
    pipejob: str,
    display_name: str,
    outfile: str,
    errfile: str,
    mem_str: str | None,
    batch_opts: envconfig.ResolvedBatchOptions,
    user: str,
) -> bool:
    """Build and dispatch a Slurm batch job."""
    script_record_path = str(Path(outfile).with_suffix(".sbatch"))
    script_body = build_sbatch_script(
        pipejob,
        partition=batch_opts.partition,
        node=batch_opts.node,
        cores=batch_opts.cores,
        mem=mem_str,
        job_name=display_name,
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
        script_record_path=script_record_path,
        omp_num_threads=getattr(batch_opts, "omp_num_threads", None),
        openblas_num_threads=getattr(batch_opts, "openblas_num_threads", None),
        request_gpus=getattr(batch_opts, "request_gpus", 0),
        is_pixi=bool(getattr(batch_opts, "pixi_dir", None)),
    )

    script_record_path_obj = Path(script_record_path).resolve()
    try:
        with open(script_record_path_obj, "w", encoding="utf-8") as f:
            f.write(script_body)
        print(f"    script saved → {script_record_path_obj}")
    except OSError as exc:
        print(
            f"    WARNING: could not save script record {script_record_path_obj}: {exc}"
        )

    return _submit_tempfile_job(
        "sbatch",
        script_body,
        prefix=f"{user}_sbatch.",
        suffix=".sh",
        dry_run=batch_opts.dry_run,
    )


def _dispatch_job(
    pipejob: str,
    name: str | tuple[str, str],
    batch_opts: envconfig.ResolvedBatchOptions,
    user: str,
) -> bool:
    """Submit a single prepared job to the batch scheduler.

    Returns:
        True if the job was submitted (or dry-run simulated), False on error.
    """
    if isinstance(name, tuple):
        display_name, file_name = name
    else:
        display_name = name
        file_name = name.replace("/", "_").replace("@", "_").replace(":", "_")

    print(f"job name = {display_name}")
    if batch_opts.logdir:
        raw_logdir = batch_opts.logdir.replace("{user}", user)
        expanded_logdir = Path(os.path.expanduser(raw_logdir)).resolve()
        try:
            expanded_logdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(
                f"    WARNING: could not create batch logdir {expanded_logdir}: {exc}"
            )
        out_name = batch_opts.outfile or f"batch.{file_name}.out"
        err_name = batch_opts.errfile or f"batch.{file_name}.err"
        outfile = (
            str(expanded_logdir / out_name)
            if not Path(out_name).is_absolute()
            else out_name
        )
        errfile = (
            str(expanded_logdir / err_name)
            if not Path(err_name).is_absolute()
            else err_name
        )
    else:
        outfile = batch_opts.outfile or f"batch.{file_name}.out"
        errfile = batch_opts.errfile or f"batch.{file_name}.err"

    if batch_opts.chdir:
        expanded_chdir = Path(os.path.expanduser(batch_opts.chdir)).resolve()
        try:
            expanded_chdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(
                f"    WARNING: could not pre-create batch chdir {expanded_chdir}: {exc}"
            )

    mem_str = None if batch_opts.mem_per_cpu else f"{batch_opts.mem}G"

    if batch_opts.scheduler == "htcondor":
        return _dispatch_htcondor(
            pipejob=pipejob,
            display_name=display_name,
            outfile=outfile,
            errfile=errfile,
            mem_str=mem_str,
            batch_opts=batch_opts,
            user=user,
        )

    return _dispatch_slurm(
        pipejob=pipejob,
        display_name=display_name,
        outfile=outfile,
        errfile=errfile,
        mem_str=mem_str,
        batch_opts=batch_opts,
        user=user,
    )


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
        batch_opts = envconfig.resolve_batch_options(cfg, args)
    except envconfig.ConfigError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    check_submit_host(cfg)

    user = os.environ.get("USER", "")
    submitted = 0

    pipefile = batch_opts.pipefile
    if pipefile is not None:
        if not pipefile.is_file():
            print(f"{pipefile} does not exist, exiting")
            sys.exit(1)

        with open(pipefile, encoding="utf-8") as fd:
            lines = fd.readlines()

        valid_lines = [
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]

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
            if (
                getattr(args, "profile", None)
                and args.profile in cfg.profiles
                and args.profile not in cfg.batches
            ):
                p = cfg.profiles[args.profile]
                has_run_or_env = any(
                    [
                        p.casa_root,
                        p.heuristics_dir,
                        p.pixi_dir,
                        p.pixi_env,
                        p.branch,
                        p.recipe,
                        p.loglevel,
                        p.useresume,
                        p.omp_num_threads,
                        p.psrecord,
                        p.memstats,
                        p.backup,
                        p.workspaces,
                        p.casadata,
                    ]
                )
                if has_run_or_env:
                    pipejob_parts.append(f"--profile={args.profile}")
            if batch_opts.env_name:
                pipejob_parts.append(f"--env={batch_opts.env_name}")
            if getattr(args, "log2term", None) is True:
                pipejob_parts.append("--log2term")
            elif getattr(args, "log2term", None) is False:
                pipejob_parts.append("--no-log2term")
            pipejob_parts.extend(batch_opts.extra_args)
            pipejob = " ".join(pipejob_parts)

            name = job_name(pipejob, profile_name=batch_opts.profile)
            if _dispatch_job(pipejob, name, batch_opts, user):
                submitted += 1

            if idx < len(valid_lines) and not batch_opts.dry_run:
                time.sleep(1)

    elif batch_opts.profile:
        # Single-job submission mode from named profile
        calibpipe_exe = shutil.which("calibpipe") or "calibpipe"
        pipejob_parts = [calibpipe_exe, "run", f"-p={batch_opts.profile}"]
        if args.config:
            pipejob_parts.append(f"--config={args.config}")
        if getattr(args, "no_site_config", False):
            pipejob_parts.append("--no-site-config")
        if getattr(args, "log2term", None) is True:
            pipejob_parts.append("--log2term")
        elif getattr(args, "log2term", None) is False:
            pipejob_parts.append("--no-log2term")
        pipejob_parts.extend(batch_opts.extra_args)
        pipejob = " ".join(pipejob_parts)

        name = job_name(pipejob, profile_name=batch_opts.profile)
        if _dispatch_job(pipejob, name, batch_opts, user):
            submitted += 1

    if submitted > 0 and not batch_opts.dry_run:
        current_user = os.environ.get("USER", "")
        if current_user:
            print_queue(
                current_user,
                scheduler=batch_opts.scheduler,
                pool=getattr(batch_opts, "pool", None),
                schedd=getattr(batch_opts, "schedd", None),
            )


if __name__ == "__main__":
    main()
