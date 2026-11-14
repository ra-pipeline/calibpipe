"""Submit a batch of calibpipe runs to Slurm, one per line of a pipefile.

Each non-comment line of the pipefile is "<mous_uid> [recipe]" (recipe
defaults to calimage).
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from calibpipe import collect
from calibpipe import config as envconfig
from calibpipe.config import RunRequest, SubmitRequest
from calibpipe.templates import render_template


def build_parser():
    from .cli import execution_parser

    parser = execution_parser("submit")
    parser.prog = "calibpipe submit"
    parser.add_argument("-h", "--help", action="help")
    return parser


def check_submit_host(config: dict) -> None:
    """Enforce an optional submit-host restriction from configuration.

    Args:
        config: Parsed TOML configuration dictionary.
    """
    submit_host = config.site.submit_host if isinstance(config, envconfig.CalibpipeConfig) else config.get("site", {}).get("submit_host")
    if not submit_host:
        return
    hostname = subprocess.getoutput("hostname").strip()
    if hostname != submit_host:
        print(f"Jobs must be submitted from {submit_host}, exiting.")
        sys.exit(1)


def _append_bool_cli_flag(
    parts: list[str], args: argparse.Namespace, attr: str, flag: str
) -> None:
    """Forward a BooleanOptionalAction CLI flag when explicitly True or False."""
    val = getattr(args, attr, None)
    if val is True:
        parts.append(f"--{flag}")
    elif val is False:
        parts.append(f"--no-{flag}")


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
    dependency: str | None = None,
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
        _sbatch_directive("dependency", dependency),
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
    pipejob: str,
    profile_name: str | None = None,
    test_name: str | None = None,
) -> tuple[str, str]:
    """Derive the standard batch job name and safe filename prefix from a command line.

    Args:
        pipejob: Fully assembled `calibpipe` execution command.
        profile_name: Optional named profile for single-job submission.
        test_name: Optional test name for test collection batch jobs.

    Returns:
        Tuple of (display_name, file_safe_name).
        display_name retains profile name (e.g. including '/') for scheduler display (squeue/condor_q),
        while file_safe_name normalizes path separators for stdout/stderr/sbatch file paths.
    """
    timestr = time.strftime("%Y%m%d-%H%M%S")
    if test_name:
        safe_test = re.sub(r"[^\w\-.]", "_", test_name)
        if profile_name:
            full_display = f"{profile_name}/{safe_test}_{timestr}"
            safe_prof = profile_name.replace("/", "_").replace("@", "_").replace(":", "_")
            full_safe = f"{safe_prof}_{safe_test}_{timestr}"
        else:
            full_display = f"{safe_test}_{timestr}"
            full_safe = full_display
        return full_display, full_safe

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
    # squeue_format = "%7i %13P %9u %7T %11M %11l %5D %2C %2c/%7m %16R %50j %50Z"
    squeue_format = "%7i %13P %9u %7T %11M %11l %5D %2C %2c/%7m %16R %80j"
    cmd = [squeue_exe, f"--format={squeue_format}", "-u", user]
    try:
        time.sleep(0.5)
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            print("\n" + res.stdout.strip())
    except OSError:
        pass


_dry_run_counter: int = 0


def _submit_tempfile_job(
    submit_cmd: str | Sequence[str],
    content: str,
    *,
    prefix: str,
    suffix: str,
    dry_run: bool = False,
) -> str | None:
    """Write job content to a temporary file, run submit command, and clean up.

    Returns:
        Job ID string if submitted, synthetic ID if dry run, or None on error.
    """
    global _dry_run_counter
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
            _dry_run_counter += 1
            print("    [DRY RUN] Would execute: " + " ".join(cmd))
            return f"DRY_RUN_{_dry_run_counter}"
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
        stdout_txt = (proc.stdout or "").strip()
        if stdout_txt:
            print(f"    {stdout_txt}")
            match = re.search(r"Submitted batch job (\d+)", stdout_txt)
            if match:
                return match.group(1)
            match_condor = re.search(r"submitted to cluster (\d+)", stdout_txt, re.IGNORECASE)
            if match_condor:
                return match_condor.group(1)
        return "SUBMITTED"
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or "").strip()
        print(f"    ERROR: Job submission failed (exit status {exc.returncode}) {err}")
        return None
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
) -> str | None:
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
        return None

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
    dependency: str | None = None,
) -> str | None:
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
        dependency=dependency,
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
    dependency: str | None = None,
) -> str | None:
    """Submit a single prepared job to the batch scheduler.

    Returns:
        Job ID string if submitted (or dry-run simulated), None on error.
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
        dependency=dependency,
    )


def _submit_report_job(
    *,
    base_workdir: Path,
    job_ids: list[str],
    batch_opts: envconfig.ResolvedBatchOptions,
    profile: envconfig.ProfileConfig | None,
    cfg: envconfig.CalibpipeConfig,
    args: Any,
    user: str,
) -> str | None:
    """Submit a dependent wrap-up Slurm job to synthesize test results and coverage."""
    if batch_opts.scheduler != "slurm":
        raise envconfig.ConfigError(
            "with_report=True (dependent wrap-up report job) is only supported with scheduler='slurm'."
        )
    valid_ids = [jid for jid in job_ids if jid]
    if not valid_ids:
        print("    No job IDs available for report job dependency.")
        return None

    dep_type = "afterany"
    dep_str = f"{dep_type}:{':'.join(valid_ids)}"

    report_cmd_parts = [*_calibpipe_argv(), "report", str(base_workdir)]
    req_overrides = getattr(args, "overrides", None) or {}
    resolved_pixi_dir = getattr(batch_opts, "pixi_dir", None)
    if isinstance(cfg, envconfig.CalibpipeConfig):
        env_name = getattr(args, "env", None) or (profile.env if profile else None) or cfg.default_env
        spec = envconfig.resolve_env(
            cfg,
            env_name,
            casa_root=getattr(args, "casa_root", None)
            or req_overrides.get("casa_root")
            or (profile.casa_root if profile else None),
            heuristics_dir=getattr(args, "heuristics_dir", None)
            or req_overrides.get("heuristics_dir")
            or (profile.heuristics_dir if profile else None),
            pixi_dir=getattr(args, "pixi_dir", None)
            or req_overrides.get("pixi_dir")
            or (profile.pixi_dir if profile else None)
            or resolved_pixi_dir,
            pixi_env=getattr(args, "pixi_env", None)
            or req_overrides.get("pixi_env")
            or (profile.pixi_env if profile else None),
            branch=getattr(args, "branch", None)
            or req_overrides.get("branch")
            or (profile.branch if profile else None),
            workspaces=req_overrides.get("workspaces")
            or (profile.workspaces if profile else None)
            or cfg.paths.workspaces,
        )
        resolved_casa_root = "" if spec.is_pixi else spec.casa_root
        if spec.is_pixi:
            resolved_pixi_dir = spec.pixi_dir
    else:
        resolved_casa_root = profile.casa_root if profile else None
        if not resolved_casa_root and profile and profile.env and profile.env in cfg.envs:
            resolved_casa_root = cfg.envs[profile.env].casa_root
    if resolved_casa_root:
        report_cmd_parts.append(f"--casa-root={resolved_casa_root}")
    report_pipejob = shlex.join(report_cmd_parts)
    if getattr(args, "no_site_config", False):
        report_pipejob = f"CALIBPIPE_NO_SITE_CONFIG=1 {report_pipejob}"

    ts = time.strftime("%Y%m%d-%H%M%S")
    display_name = f"{batch_opts.profile}/report_{ts}" if batch_opts.profile else f"report_{ts}"
    file_name = f"report_{ts}"

    report_batch_opts = dataclasses.replace(
        batch_opts,
        cores=1,
        mem=8,
        mem_per_cpu=None,
        walltime="01:00:00",
        chdir=str(base_workdir),
        outfile=f"batch.{file_name}.out",
        errfile=f"batch.{file_name}.err",
    )
    if resolved_pixi_dir:
        report_batch_opts.pixi_dir = resolved_pixi_dir

    print(f"\nSubmitting dependent finalize report job (dependency: {dep_str})...")
    return _dispatch_job(
        report_pipejob,
        (display_name, file_name),
        report_batch_opts,
        user,
        dependency=dep_str,
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Submit batch jobs via the native 'calibpipe submit' interface."""
    from .cli import main as cli_main

    args = list(argv) if argv is not None else sys.argv[1:]
    if not args:
        cli_main(["submit", "--help"])
        return
    cli_main(["submit", *args])


def _calibpipe_argv() -> list[str]:
    """Return the executable argv prefix for invoking `calibpipe` inside batch scripts."""
    sibling = Path(sys.executable).with_name("calibpipe")
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling)]
    return [sys.executable, "-m", "calibpipe.cli"]


def _format_shell_command(groups: list[list[str]]) -> str:
    """Format grouped argv tokens as a readable multi-line POSIX shell command."""
    lines = [
        " ".join(shlex.quote(tok) for tok in group)
        for group in groups
        if group
    ]
    return " \\\n    ".join(lines)


def _resolve_snapshot_dir(
    batch_opts: envconfig.ResolvedBatchOptions,
    user: str,
) -> Path:
    """Resolve and prepare the directory holding the shared batch config snapshot."""
    if batch_opts.logdir:
        raw_logdir = batch_opts.logdir.replace("{user}", user)
        target = Path(os.path.expanduser(raw_logdir)).resolve()
        try:
            target.mkdir(parents=True, exist_ok=True)
            return target
        except OSError as exc:
            print(
                f"    WARNING: could not create snapshot directory {target}: {exc}"
            )
    return Path.cwd().resolve()


def _write_batch_snapshot(
    snapshot_dir: Path,
    snapshot_data: dict[str, Any],
) -> Path:
    """Write the frozen TOML snapshot into `snapshot_dir`, avoiding name collisions."""
    timestr = time.strftime("%Y%m%d-%H%M%S")
    candidate = snapshot_dir / f"batch.{timestr}.toml"
    suffix = 1
    while candidate.exists():
        candidate = snapshot_dir / f"batch.{timestr}-{suffix}.toml"
        suffix += 1
    candidate.write_text(envconfig.dump_toml(snapshot_data), encoding="utf-8")
    return candidate


def _absolutize_batch_arg(
    value: str,
    *,
    workspaces: Sequence[str | Path],
    cwd: Path,
) -> str:
    """Resolve a path-valued CLI argument against workspaces or submission CWD."""
    resolved = envconfig.resolve_workspace_path(value, workspaces)
    resolved_path = Path(resolved).expanduser()
    if resolved_path.is_absolute():
        return str(resolved_path)
    return str((cwd / resolved_path).resolve())


def _build_batch_command(
    *,
    snapshot_path: Path,
    run_request: RunRequest,
    run_options: envconfig.ResolvedRunOptions,
    isolate_root: bool,
    cwd: Path,
) -> str:
    """Build a readable `calibpipe run --config=... --config-only ...` command."""
    groups: list[list[str]] = [
        [*_calibpipe_argv(), "run"],
        [f"--config={snapshot_path}", "--config-only"],
    ]
    if run_options.profile:
        groups.append(["-p", run_options.profile])
    if run_options.env_name:
        groups.append([f"--env={run_options.env_name}"])

    if run_request.workdir:
        abs_workdir = _absolutize_batch_arg(
            str(run_options.workdir or run_request.workdir),
            workspaces=run_options.workspaces,
            cwd=cwd,
        )
        groups.append([f"--workdir={abs_workdir}"])

    if run_request.mous:
        groups.append([f"--mous={run_request.mous}"])
    elif run_request.vis:
        vis_args = [
            _absolutize_batch_arg(str(v), workspaces=run_options.workspaces, cwd=cwd)
            for v in (run_options.vis or run_request.vis)
        ]
        groups.append(["--vis", *vis_args])
    elif run_request.script:
        abs_script = _absolutize_batch_arg(
            str(run_options.script or run_request.script),
            workspaces=run_options.workspaces,
            cwd=cwd,
        )
        groups.append([f"--script={abs_script}"])
    elif run_request.cmd:
        groups.append([f"--cmd={run_request.cmd}"])
    elif run_request.ppr:
        abs_ppr = _absolutize_batch_arg(
            str(run_options.ppr or run_request.ppr),
            workspaces=run_options.workspaces,
            cwd=cwd,
        )
        groups.append([f"--ppr={abs_ppr}"])

    if run_request.recipe:
        recipe_val = envconfig.freeze_recipe_path(
            run_options.recipe,
            workspaces=run_options.workspaces,
            cwd=cwd,
        )
        if recipe_val:
            groups.append([f"--recipe={recipe_val}"])

    if run_request.verbose is True:
        groups.append(["--verbose"])
    elif run_request.verbose is False:
        groups.append(["--no-verbose"])

    if isolate_root:
        groups.append(["--isolate-root"])

    return _format_shell_command(groups)


def submit_request(request: SubmitRequest) -> int:
    """Resolve native batch jobs and submit self-contained execution settings.

    Args:
        request: Parsed `calibpipe submit` request.

    Returns:
        Process exit status (`0` on success, `1` if any submission failed).
    """
    cfg = envconfig.load_merged_config(
        request.config, include_site=not request.no_site_config
    )
    options = envconfig.resolve_batch_options(cfg, request)
    check_submit_host(cfg)
    profile = cfg.profiles.get(options.profile) if options.profile else None

    base_request = RunRequest(
        profile=options.profile,
        env=request.env,
        workdir=request.workdir,
        recipe=request.recipe,
        verbose=request.verbose,
        overrides=dict(request.overrides),
    )
    submit_cwd = Path.cwd().resolve()
    base_run = envconfig.resolve_run_options(cfg, base_request)
    base_workdir = Path(base_run.workdir or submit_cwd).expanduser().resolve()

    tasks: list[tuple[Any, Any]] = []
    test_jobs = False

    if options.pipefile:
        if not options.pipefile.is_file():
            raise envconfig.ConfigError(f"Runfile does not exist: {options.pipefile}")
        lines = [
            line.strip()
            for line in options.pipefile.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        test_jobs = any("::" in line for line in lines)
        if test_jobs:
            if not all("::" in line for line in lines):
                raise envconfig.ConfigError(
                    "Runfile cannot mix pytest node IDs with dataset/MOUS targets"
                )
            tasks = collect.read_test_runfile(options.pipefile)
        else:
            for line in lines:
                parts = shlex.split(line)
                if len(parts) > 2:
                    raise envconfig.ConfigError(
                        f"Expected '<MOUS/dataset> [recipe]' in runfile: {line}"
                    )
                target = parts[0]
                targets = (
                    {"mous": target}
                    if target.startswith(("uid://", "uid___"))
                    else {"vis": [target]}
                )
                tasks.append(
                    (targets, parts[1] if len(parts) > 1 else base_run.recipe)
                )
    elif options.collect:
        # Array test collection mode
        test_jobs = True
        spec = envconfig.resolve_env(
            cfg,
            base_run.env_name,
            casa_root=base_run.casa_root,
            heuristics_dir=base_run.heuristics_dir,
            pixi_dir=base_run.pixi_dir,
            pixi_env=base_run.pixi_env,
            branch=base_run.branch,
            workspaces=base_run.workspaces,
        )
        nodes = collect.collect_tests(
            target=options.test_target or "tests/regression/fast",
            casa_root=spec.casa_root,
            heuristics_dir=spec.heuristics_dir,
            marker=options.test_marker,
            filter_k=options.test_filter,
            extra_opts=options.test_opts,
            logger=print,
        )
        if options.save_runfile:
            collect.write_test_runfile(options.save_runfile, nodes)
        tasks = [(node, collect.parse_test_name(node)) for node in nodes]
    else:
        # Single-job profile submission mode
        envconfig.validate_target(base_run)
        tasks = [(None, None)]

    if not tasks:
        print("No jobs to submit.")
        return 0

    if test_jobs and options.with_report and options.scheduler != "slurm":
        raise envconfig.ConfigError(
            "with_report=True (dependent wrap-up report job) is only supported with scheduler='slurm'."
        )

    resolved_tasks: list[
        tuple[RunRequest, envconfig.ResolvedRunOptions, str | tuple[str, str], envconfig.ResolvedBatchOptions]
    ] = []
    # Validate every job before writing the snapshot or submitting any jobs.
    for target, detail in tasks:
        job_options = options
        test_name: str | None = None
        if test_jobs:
            test_name = detail
            test_workdir = base_workdir / test_name
            run_request = dataclasses.replace(
                base_request,
                cmd=collect.build_test_cmd(
                    target, test_name=test_name, extra_opts=options.test_opts
                ),
                workdir=str(test_workdir),
            )
            job_options = dataclasses.replace(
                options, chdir=options.chdir or str(base_workdir)
            )
        elif target:
            run_request = dataclasses.replace(base_request, **target, recipe=detail)
        else:
            run_request = base_request

        run_options = envconfig.resolve_run_options(cfg, run_request)
        envconfig.validate_target(run_options)
        # Explicit resolved environment selection controls Pixi worker exports too.
        spec = envconfig.resolve_env(
            cfg,
            run_options.env_name,
            casa_root=run_options.casa_root,
            heuristics_dir=run_options.heuristics_dir,
            pixi_dir=run_options.pixi_dir,
            pixi_env=run_options.pixi_env,
            branch=run_options.branch,
            workspaces=run_options.workspaces,
        )
        if spec.is_pixi:
            job_options = dataclasses.replace(job_options)
            job_options.pixi_dir = spec.pixi_dir
        name = job_name(
            f"--mous={run_options.mous}" if run_options.mous else "",
            profile_name=options.profile,
            test_name=test_name,
        )
        resolved_tasks.append((run_request, run_options, name, job_options))

    user = os.environ.get("USER", "")
    snapshot_dir = _resolve_snapshot_dir(options, user)
    snapshot_data = envconfig.build_batch_snapshot(
        cfg,
        base_run,
        cwd=submit_cwd,
        overrides=request.overrides or None,
    )
    snapshot_path = _write_batch_snapshot(snapshot_dir, snapshot_data)
    print(f"Batch config snapshot: {snapshot_path}")

    prepared: list[tuple[str, str | tuple[str, str], envconfig.ResolvedBatchOptions]] = [
        (
            _build_batch_command(
                snapshot_path=snapshot_path,
                run_request=run_req,
                run_options=run_opts,
                isolate_root=test_jobs,
                cwd=submit_cwd,
            ),
            name,
            job_opts,
        )
        for run_req, run_opts, name, job_opts in resolved_tasks
    ]

    ids: list[str] = []
    failed = False
    for index, (command, name, job_options) in enumerate(prepared):
        dependency: str | None = None
        if (
            options.max_concurrent
            and options.max_concurrent > 0
            and len(ids) >= options.max_concurrent
        ):
            dependency = f"afterany:{ids[-options.max_concurrent]}"
        job_id = _dispatch_job(
            command, name, job_options, user, dependency=dependency
        )
        if job_id:
            ids.append(job_id)
        else:
            failed = True
        if index + 1 < len(prepared) and not options.dry_run:
            time.sleep(1)

    if test_jobs and options.with_report and ids:
        report_id = _submit_report_job(
            base_workdir=base_workdir,
            job_ids=ids,
            batch_opts=options,
            profile=profile,
            cfg=cfg,
            args=request,
            user=user,
        )
        if report_id:
            ids.append(report_id)
        else:
            failed = True

    print(f"Submitted {len(ids)} job(s).")
    if not options.dry_run and ids:
        print_queue(
            user,
            scheduler=options.scheduler,
            pool=options.pool,
            schedd=options.schedd,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
