#
# ALMA - Atacama Large Millimeter/submillimeter Array
# (c) Associated Universities Inc., 2024
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307  USA
#

"""Pipeline Make Request (PMR) execution module.

Wraps the ALMA Java class `alma.pipelinescience.reqmanager.MakeProcessingRequests`
directly to generate the pipeline directory structure, create the Pipeline
Processing Request (PPR.xml), and optionally download ASDM visibility data or
calibration products.

Environment Variables and Java System Properties
------------------------------------------------
The underlying Java class and ACS libraries read:
  ACSROOT: Root of ACS / pipetools installation containing JARs under lib/.
  ACSDATA: Directory containing ACS schemas, config files, and archiveConfig.properties.
  SCIPIPE_SCRIPTDIR: Directory where standard procedure and intents XML files reside.
  SCIPIPE_ROOTDIR: Parent directory where project workspaces are created.
  SCIPIPE_LOGDIR: Output directory for PMR execution logs.
  DATAPACKER_HOME: Root of ALMA datapacker (required when extracting ASDMs).
  JARSDIR: Datapacker JAR libraries ($DATAPACKER_HOME/lib).
  FLUX_SERVICE_URL: ALMA flux calibration service endpoint.

JVM flags configured when launching the process:
  -Xmx<heap>: Maximum Java heap size (default: 4G).
  -Djavax.xml.transform.TransformerFactory: Forces internal Xalan XSLT implementation.
  -DORACLE_HOME=-: Prevents Oracle JDBC drivers from searching for local Oracle installations.
  -DACSDATA / -DACS.data: Passes ACS schema path to internal ACS runtime.
  --add-opens: Opens encapsulated Xerces packages on Java 9+.
"""

# Replaced the standalone pipelineMakeRequest CLI wrapper with a typed
# library/CLI module (config-backed PMRRuntime, streaming PMRResult, dry-run,
# rename/strip-timestamp, symlink shortcuts, --procedure-enable-parallel, and
# --asdm-cache). See docs/internals.md ("Differences from Standalone pipelineMakeRequest").

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from calibpipe import config as envconfig
from calibpipe.steps.staging import (
    cache_pmr_metadata,
    extract_eb_uids_from_ppr,
    extract_proposal_code_from_ppr,
    find_cached_mous_rawdata,
    populate_asdm_cache,
    stage_cached_asdms,
)
from calibpipe.workspace import create_project_symlinks


@dataclass(frozen=True)
class PMRRuntime:
    """Execution environment and JVM settings for MakeProcessingRequests."""

    acsroot: Path
    acsdata: Path
    scriptdir: Path
    rootdir: Path | None = None
    logdir: Path | None = None
    java_bin: str = "java"
    max_heap: str = "4G"
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        cfg: envconfig.CalibpipeConfig | None = None,
        java_home: str | None = None,
        max_heap: str = "4G",
    ) -> PMRRuntime:
        """Resolve and validate PMRRuntime from environment and config.

        Args:
            env: Optional environment dictionary. Defaults to `os.environ`.
            cfg: Optional CalibpipeConfig instance to read tool installation paths.
            java_home: Optional path to override JAVA_HOME.
            max_heap: Maximum Java heap size (e.g. '4G', '8G').

        Returns:
            Validated PMRRuntime instance.

        Raises:
            EnvironmentError: If ACSROOT, ACSDATA, or SCIPIPE_SCRIPTDIR cannot be resolved.
        """
        source = env.copy() if env is not None else os.environ.copy()
        if cfg is not None:
            site = cfg.site
            if site.pmr_home and "ACSROOT" not in source:
                source["ACSROOT"] = site.pmr_home
            if site.acsdata and "ACSDATA" not in source:
                source["ACSDATA"] = site.acsdata
            if site.java_home and "JAVA_HOME" not in source:
                source["JAVA_HOME"] = site.java_home

        required = {
            "ACSROOT": "Path to ACS libraries / PMR home",
            "ACSDATA": "ACS data directory with schemas",
            "SCIPIPE_SCRIPTDIR": "Science Pipeline recipes directory",
        }
        missing = [f"{k} ({desc})" for k, desc in required.items() if not source.get(k)]
        if missing:
            raise OSError(
                "Required PMR environment variables are not set:\n  - " + "\n  - ".join(missing)
            )

        effective_java_home = java_home or source.get("JAVA_HOME")
        java_bin = "java"
        if effective_java_home:
            cand = Path(effective_java_home) / "bin" / "java"
            if cand.is_file():
                java_bin = str(cand)

        return cls(
            acsroot=Path(source["ACSROOT"]).resolve(),
            acsdata=Path(source["ACSDATA"]).resolve(),
            scriptdir=Path(source["SCIPIPE_SCRIPTDIR"]).resolve(),
            rootdir=Path(source["SCIPIPE_ROOTDIR"]).resolve() if source.get("SCIPIPE_ROOTDIR") else None,
            logdir=Path(source["SCIPIPE_LOGDIR"]).resolve() if source.get("SCIPIPE_LOGDIR") else None,
            java_bin=java_bin,
            max_heap=max_heap,
            extra_env=source,
        )


@dataclass
class PMRRequest:
    """User request specification for ALMA MakeProcessingRequests."""

    mous: str
    intents: str = ""
    procedure: str = ""
    download_asdms: bool = False
    download_restore: bool = False
    asdms: list[str] = field(default_factory=list)
    omit_asdms: bool = False
    ignore_qa0: bool = False
    strip_timestamp: bool = False
    rename: str | Path | None = None

    def to_java_args(self) -> list[str]:
        """Serialize typed request fields into exact positional Java arguments.

        The ALMA Java driver expects 7 positional arguments:
        1. MOUS UID (e.g. 'uid://A001/X1/X1')
        2. Intents XML file or "''" to skip
        3. Procedure XML file or "''" to skip
        4. Comma-separated user ASDM list, "'-'" prefix if omitted, or "''"
        5. Download ASDMs ('true' / 'false')
        6. Download restore products ('true' / 'false')
        7. Ignore QA0 status ('true' / 'false')
        """
        norm_intents = self._clean_str(self.intents)
        norm_procedure = self._clean_str(self.procedure)

        if not self.asdms:
            asdm_arg = "''"
        else:
            asdm_body = ",".join(a.strip() for a in self.asdms if a.strip())
            if not asdm_body:
                asdm_arg = "''"
            elif self.omit_asdms and not asdm_body.startswith("-,"):
                asdm_arg = f"-,{asdm_body}"
            else:
                asdm_arg = asdm_body

        return [
            self.mous.strip(),
            norm_intents if norm_intents else "''",
            norm_procedure if norm_procedure else "''",
            asdm_arg,
            "true" if self.download_asdms else "false",
            "true" if self.download_restore else "false",
            "true" if self.ignore_qa0 else "false",
        ]

    @staticmethod
    def _clean_str(val: str | None) -> str:
        """Strip surrounding quotes and treat empty/null keywords as empty."""
        if not val:
            return ""
        s = val.strip()
        if s in ("", "''", '""') or s.lower() in ("null", "none"):
            return ""
        if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
            s = s[1:-1].strip()
        return s


@dataclass(frozen=True)
class PMRResult:
    """Outcome of a MakeProcessingRequests invocation."""

    exit_code: int
    command: list[str]
    command_str: str
    output: str
    project_root: Path | None = None


def validate_files(intents: str, procedure: str, scriptdir: Path) -> None:
    """Validate that input XML files exist if specified.

    Resolves relative paths against scriptdir. Empty or skipped paths pass silently.
    """
    for file_type, raw_path in (("Intents", intents), ("Procedure", procedure)):
        clean = PMRRequest._clean_str(raw_path)
        if not clean:
            continue
        p = Path(clean)
        target = p if p.is_absolute() else (scriptdir / p)
        if not target.is_file():
            raise FileNotFoundError(
                f"{file_type} file not found: {target} (resolved from {raw_path})"
            )


def build_java_command(req: PMRRequest, runtime: PMRRuntime) -> tuple[list[str], str]:
    """Construct command argument list and human-readable command string."""
    java_opts = [
        runtime.java_bin,
        f"-Xmx{runtime.max_heap}",
        "-Djavax.xml.transform.TransformerFactory=com.sun.org.apache.xalan.internal.xsltc.trax.TransformerFactoryImpl",
        "-DORACLE_HOME=-",
        f"-DACSDATA={runtime.acsdata}",
        f"-DACS.data={runtime.acsdata}",
        "-classpath",
        f"{runtime.acsroot}/lib/*",
        "--add-opens",
        "java.xml/com.sun.org.apache.xml.internal.serialize=ALL-UNNAMED",
        "alma.pipelinescience.reqmanager.MakeProcessingRequests",
    ]

    args = req.to_java_args()
    cmd_list = java_opts + args

    cmd_str = (
        f"{runtime.java_bin} -Xmx{runtime.max_heap} "
        f"-Djavax.xml.transform.TransformerFactory=com.sun.org.apache.xalan.internal.xsltc.trax.TransformerFactoryImpl "
        f"-DORACLE_HOME=- -DACSDATA={runtime.acsdata} -DACS.data={runtime.acsdata} "
        f'-classpath "{runtime.acsroot}/lib/*" '
        f"--add-opens java.xml/com.sun.org.apache.xml.internal.serialize=ALL-UNNAMED "
        f"alma.pipelinescience.reqmanager.MakeProcessingRequests "
        f"{' '.join(args)}"
    )
    return cmd_list, cmd_str


def parse_project_root(output: str) -> Path | None:
    """Extract newly generated project root directory from PMR output."""
    workspace_dir: Path | None = None
    project_dir: Path | None = None

    for line in output.splitlines():
        if "Workspace root directory is" in line or "Workspace directory is" in line:
            parts = line.strip().split()
            if parts:
                workspace_dir = Path(parts[-1])
        if "Project root directory is" in line:
            parts = line.strip().split()
            if parts:
                project_dir = Path(parts[-1])

    if project_dir is None:
        return None

    if not project_dir.is_absolute():
        if workspace_dir is not None:
            return (workspace_dir / project_dir).resolve()
        scipipe_root = os.environ.get("SCIPIPE_ROOTDIR")
        if scipipe_root:
            return (Path(scipipe_root) / project_dir).resolve()

    return project_dir.resolve()


def strip_timestamp_from_name(name: str) -> str:
    """Strip trailing timestamp suffix from a project directory name.

    Examples:
        '2099.1.00001.S_2099_12_31T23_59_59.000' -> '2099.1.00001.S'
        '2099.1.00001.S_20991231-000000' -> '2099.1.00001.S'
    """
    # Match ALMA project code prefix (e.g. 2099.1.00001.S)
    m = re.match(r"^(\d{4}\.[1-9]\.[0-9A-Za-z]+\.[A-Za-z]+)_", name)
    if m:
        return m.group(1)
    # Match date prefix before _YYYY...
    m = re.match(r"^(.*?)_\d{4}[-_]?\d{2}[-_]?\d{2}", name)
    if m and m.group(1):
        return m.group(1)
    # Fall back to stripping digits after the trailing underscore
    if "_" in name:
        prefix, suffix = name.rsplit("_", 1)
        if any(c.isdigit() for c in suffix):
            return prefix
    return name


def rename_project_root(
    orig_root: Path,
    target: str | Path,
    logger: Callable[[str], None] | None = None,
) -> Path:
    """Rename or move generated project root directory to target name or path."""
    log = logger if logger is not None else print
    target_path = Path(target).expanduser()
    if not target_path.is_absolute():
        target_path = (orig_root.parent / target_path).resolve()
    else:
        target_path = target_path.resolve()

    orig_resolved = orig_root.resolve()
    if target_path == orig_resolved:
        return orig_root

    # If target_path is the parent or ancestor of orig_root, move contents into target_path
    if orig_resolved.is_relative_to(target_path):
        log(f"Notice: Destination {target_path} contains {orig_root.name}; consolidating directory contents")
        for child in list(orig_root.iterdir()):
            dest = target_path / child.name
            if dest.exists():
                backup_suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup_child = dest.with_name(f"{dest.name}.backup_{backup_suffix}")
                log(f"Notice: Destination item {dest.name} exists; rotating to {backup_child.name}")
                shutil.move(dest, backup_child)
            shutil.move(child, dest)
        try:
            orig_root.rmdir()
        except OSError:
            pass
        return target_path

    # If an empty target directory already exists, clear it for the move
    if target_path.exists() and target_path.is_dir() and not any(target_path.iterdir()):
        try:
            target_path.rmdir()
        except OSError:
            pass

    if target_path.exists():
        backup_suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = target_path.with_name(f"{target_path.name}.backup_{backup_suffix}")
        log(f"Notice: Destination {target_path} exists; rotating existing directory to {backup_path.name}")
        shutil.move(target_path, backup_path)

    log(f"Renaming project directory: {orig_root.name} -> {target_path.name}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(orig_root, target_path)

    return target_path


def enable_ppr_parallel(root: Path, script: Path, logger: Callable[[str], None]) -> None:
    """Apply the pipeline checkout's parallel converter to generated PPR files."""
    pprs = sorted(root.glob("SOUS_*/GOUS_*/MOUS_*/working/PPR*.xml"))
    if not pprs:
        raise RuntimeError(f"No generated PPR XML files found under {root}; cannot enable parallel")
    for ppr in pprs:
        with tempfile.TemporaryDirectory(prefix=".parallel-", dir=ppr.parent) as td:
            output = Path(td) / ppr.name
            result = subprocess.run(
                [sys.executable, str(script), str(ppr), str(output)],
                capture_output=True, text=True, check=False,
            )
            if result.stdout.strip():
                logger(result.stdout.strip())
            if result.stderr.strip():
                logger(result.stderr.strip())
            if result.returncode != 0:
                raise RuntimeError(f"Parallel conversion failed for {ppr} (exit {result.returncode})")
            if not output.is_file():
                raise RuntimeError(f"Parallel converter did not produce output for {ppr}")
            output.chmod(ppr.stat().st_mode)
            output.replace(ppr)
            logger(f"Enabled parallel keywords in {ppr}")


def _run_pmr_subprocess(
    cmd_str: str,
    runtime: PMRRuntime,
    log: Callable[[str], None],
) -> tuple[int, str, Path | None]:
    """Execute the Java PMR command and stream output to *log*."""
    log("Executing ALMA MakeProcessingRequests...")
    log(f"Command: {cmd_str}")
    process = subprocess.Popen(
        cmd_str,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=runtime.extra_env,
    )
    output_lines: list[str] = []
    if process.stdout:
        for line in iter(process.stdout.readline, ""):
            output_lines.append(line)
            log(line.rstrip())
    process.wait()

    combined_output = "".join(output_lines)
    exit_code = process.returncode
    root = parse_project_root(combined_output)
    return exit_code, combined_output, root


def run_pmr(
    req: PMRRequest,
    runtime: PMRRuntime | None = None,
    env: dict[str, str] | None = None,
    cfg: envconfig.CalibpipeConfig | None = None,
    dry_run: bool = False,
    logger: Callable[[str], None] | None = None,
    symlink_shortcuts: bool = False,
    parallel: bool = False,
    asdm_cache: str | Path | None = None,
) -> PMRResult:
    """Execute Java MakeProcessingRequests with real-time log streaming.

    Args:
        req: PMRRequest containing MOUS, recipe, and download options.
        runtime: Optional PMRRuntime instance. If None, derived from `env` and `cfg`.
        env: Optional environment dictionary (used if `runtime` is None).
        cfg: Optional CalibpipeConfig (used if `runtime` is None).
        dry_run: If True, previews Java command without executing.
        logger: Optional logging callback. Defaults to `print`.
        symlink_shortcuts: Create project shortcuts (default: False).
        parallel: Add parallel=True to supported PPR tasks; requires a procedure.
        asdm_cache: Optional ASDM cache root directory; overrides ``cfg.paths.asdm_cache``.

    Returns:
        PMRResult with exit code, command, combined output, and parsed project root.
    """
    log = logger if logger is not None else print

    if runtime is None:
        runtime = PMRRuntime.from_env(env=env, cfg=cfg)

    effective_cache = asdm_cache
    if effective_cache is None and cfg is not None and cfg.run.use_asdm_cache:
        effective_cache = cfg.paths.asdm_cache

    cached_rawdata: Path | None = None
    effective_req = req
    if req.download_asdms and effective_cache:
        cached_rawdata = find_cached_mous_rawdata(effective_cache, req.mous)
        if cached_rawdata is not None:
            effective_req = replace(req, download_asdms=False)

    validate_files(effective_req.intents, effective_req.procedure, runtime.scriptdir)
    cmd_list, cmd_str = build_java_command(effective_req, runtime)

    parallel_script = runtime.scriptdir.resolve().parent.parent / "scripts/procedure_add_parallel.py"
    if parallel:
        error = None
        if not req.procedure.strip():
            error = "--procedure-enable-parallel requires --procedure"
        elif not parallel_script.is_file():
            error = (
                f"Parallel converter not found: {parallel_script}. "
                "Set SCIPIPE_SCRIPTDIR to the pipeline checkout's pipeline/recipes directory."
            )
        if error:
            log(f"ERROR: {error}")
            return PMRResult(1, cmd_list, cmd_str, error, None)

    if dry_run:
        log("[DRY RUN] PMR invocation:")
        log(f"[DRY RUN] MOUS UID: {req.mous}")
        log(f"[DRY RUN] Command:\n    {cmd_str}")
        if req.rename:
            log(f"[DRY RUN] Planned directory rename: {req.rename}")
        elif req.strip_timestamp:
            log("[DRY RUN] Planned directory rename: strip trailing timestamp")
        if parallel:
            log(f"[DRY RUN] Will update generated working/PPR*.xml using {parallel_script}")
        if req.download_asdms and effective_cache:
            if cached_rawdata is not None:
                log(f"[DRY RUN] ASDM cache: hit ({cached_rawdata})")
            else:
                log(f"[DRY RUN] ASDM cache: miss (will populate {effective_cache})")
        return PMRResult(
            exit_code=0,
            command=cmd_list,
            command_str=cmd_str,
            output="[DRY RUN] Command preview generated.",
            project_root=None,
        )

    try:
        exit_code, combined_output, root = _run_pmr_subprocess(cmd_str, runtime, log)

        if root and root.exists() and exit_code == 0:
            mous_dirs = sorted(root.glob("SOUS_*/GOUS_*/MOUS_*")) or sorted(root.glob("MOUS_*"))
            if req.download_asdms and effective_cache and cached_rawdata is not None:
                all_missing: list[str] = []
                for mous_dir in mous_dirs:
                    ppr_candidates = sorted((mous_dir / "working").glob("PPR*.xml"))
                    ppr_path = ppr_candidates[0] if ppr_candidates else None
                    eb_uids = extract_eb_uids_from_ppr(ppr_path)
                    _, missing_cached = stage_cached_asdms(
                        cached_rawdata,
                        mous_dir / "rawdata",
                        eb_uids,
                        log_func=log,
                    )
                    all_missing.extend(missing_cached)
                if all_missing:
                    log(
                        f"Notice: ASDM cache {cached_rawdata} is missing EB(s) "
                        f"({', '.join(all_missing)}); falling back to full PMR download."
                    )
                    shutil.rmtree(root, ignore_errors=True)
                    cached_rawdata = None
                    cmd_list, cmd_str = build_java_command(req, runtime)
                    exit_code, combined_output, root = _run_pmr_subprocess(cmd_str, runtime, log)
                    mous_dirs = (
                        sorted(root.glob("SOUS_*/GOUS_*/MOUS_*")) or sorted(root.glob("MOUS_*"))
                        if root and root.exists()
                        else []
                    )

        if root and root.exists() and exit_code == 0:
            if req.download_asdms and effective_cache and cached_rawdata is None:
                mous_dirs = sorted(root.glob("SOUS_*/GOUS_*/MOUS_*")) or sorted(root.glob("MOUS_*"))
                for mous_dir in mous_dirs:
                    ppr_candidates = sorted((mous_dir / "working").glob("PPR*.xml"))
                    ppr_path = ppr_candidates[0] if ppr_candidates else None
                    eb_uids = extract_eb_uids_from_ppr(ppr_path)
                    proposal_code = extract_proposal_code_from_ppr(ppr_path)
                    mous_subpath = mous_dir.relative_to(root)
                    mous_rel_path = (
                        Path(proposal_code) / mous_subpath if proposal_code else mous_subpath
                    )
                    populate_asdm_cache(
                        effective_cache,
                        mous_rel_path,
                        mous_dir / "rawdata",
                        eb_uids,
                        log_func=log,
                    )

            if effective_cache:
                mous_dirs = sorted(root.glob("SOUS_*/GOUS_*/MOUS_*")) or sorted(root.glob("MOUS_*"))
                for mous_dir in mous_dirs:
                    ppr_candidates = sorted((mous_dir / "working").glob("PPR*.xml"))
                    ppr_path = ppr_candidates[0] if ppr_candidates else None
                    proposal_code = extract_proposal_code_from_ppr(ppr_path)
                    cache_pmr_metadata(
                        effective_cache,
                        proposal_code,
                        root,
                        mous_dir,
                        log_func=log,
                    )

            if req.rename:
                root = rename_project_root(root, req.rename, logger=log)
            elif req.strip_timestamp:
                stripped = strip_timestamp_from_name(root.name)
                if stripped != root.name:
                    root = rename_project_root(root, stripped, logger=log)

            if parallel:
                enable_ppr_parallel(root, parallel_script, log)

            if symlink_shortcuts:
                working_dirs = sorted(root.glob("SOUS_*/GOUS_*/MOUS_*/working"))
                if len(working_dirs) == 1:
                    create_project_symlinks(root, working_dirs[0], log)
                elif len(working_dirs) > 1:
                    log("warning: skipped project shortcuts: multiple MOUS working directories found")

        if parallel and exit_code == 0 and (root is None or not root.is_dir()):
            raise RuntimeError("PMR did not produce an accessible project root; cannot enable parallel")

        return PMRResult(
            exit_code=exit_code,
            command=cmd_list,
            command_str=cmd_str,
            output=combined_output,
            project_root=root,
        )

    except (OSError, RuntimeError) as err:
        log(f"ERROR: PMR failed: {err}")
        return PMRResult(
            exit_code=1,
            command=cmd_list,
            command_str=cmd_str,
            output=str(err),
            project_root=None,
        )


def download_restore_products(
    mous: str,
    procedure: str = "procedure_hifa_image.xml",
    intents: str = "",
    runtime: PMRRuntime | None = None,
    env: dict[str, str] | None = None,
    cfg: envconfig.CalibpipeConfig | None = None,
    dry_run: bool = False,
    logger: Callable[[str], None] | None = None,
) -> PMRResult:
    """Download calibration / restore products without re-downloading raw ASDMs."""
    req = PMRRequest(
        mous=mous,
        intents=intents,
        procedure=procedure,
        download_asdms=False,
        download_restore=True,
    )
    return run_pmr(req, runtime=runtime, env=env, cfg=cfg, dry_run=dry_run, logger=logger)


def download_asdms(
    mous: str,
    asdms: list[str] | str | None = None,
    procedure: str = "procedure_hifa_calimage.xml",
    intents: str = "",
    runtime: PMRRuntime | None = None,
    env: dict[str, str] | None = None,
    cfg: envconfig.CalibpipeConfig | None = None,
    dry_run: bool = False,
    logger: Callable[[str], None] | None = None,
) -> PMRResult:
    """Download raw ASDM visibility data from the ALMA archive."""
    asdm_list: list[str] = []
    if asdms:
        if isinstance(asdms, str):
            asdm_list = [a.strip() for a in asdms.split(",") if a.strip()]
        else:
            asdm_list = list(asdms)

    req = PMRRequest(
        mous=mous,
        intents=intents,
        procedure=procedure,
        download_asdms=True,
        download_restore=False,
        asdms=asdm_list,
    )
    return run_pmr(req, runtime=runtime, env=env, cfg=cfg, dry_run=dry_run, logger=logger)


def build_parser(add_help: bool = True) -> argparse.ArgumentParser:
    """Build argument parser for 'calibpipe pmr' subcommand."""
    parser = argparse.ArgumentParser(
        prog="calibpipe pmr",
        description="ALMA Science Pipeline processing request generator and archive fetcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=add_help,
        epilog="""
Examples:
  # Generate directory structure & PPR only (no data downloads)
  calibpipe pmr uid://A001/X1/X1

  # Download raw ASDMs and generate PPR
  calibpipe pmr uid://A001/X1/X1 --download

  # Download calibration / restore products
  calibpipe pmr uid://A001/X1/X1 --restore

  # Download both raw ASDMs and restore products
  calibpipe pmr uid://A001/X1/X1 --download --restore

  # Filter specific ASDMs and ignore QA0 status
  calibpipe pmr uid://A001/X1/X1 --download -a uid://A002/X1,uid://A002/X2 --ignore-qa0

  # Preview Java command without execution
  calibpipe pmr uid://A001/X1/X1 --dry-run
        """,
    )

    parser.add_argument(
        "mous",
        help="Observing unit set UID (e.g. uid://A001/X1/X1)",
    )
    parser.add_argument(
        "--intents",
        default="",
        help="Path to intents XML file (e.g. intents_hifa.xml)",
    )
    parser.add_argument(
        "--procedure",
        default="",
        help="Path to procedure XML file (e.g. procedure_hifa_calimage.xml)",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        default=False,
        help="Download raw ASDM visibility data from the archive",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        default=False,
        help="Download calibration / restore products",
    )
    parser.add_argument(
        "-a",
        "--asdms",
        help="Comma-delimited ASDM UIDs to include or omit",
    )
    parser.add_argument(
        "-o",
        "--omit",
        action="store_true",
        default=False,
        help="Omit specified ASDMs rather than include them",
    )
    parser.add_argument(
        "--ignore-qa0",
        action="store_true",
        default=False,
        help="Ignore QA0 status of specified ASDM list",
    )
    parser.add_argument(
        "--procedure-enable-parallel",
        action="store_true",
        help="Add parallel=True to supported tasks in generated PPRs (requires --procedure)",
    )
    parser.add_argument(
        "--symlink-shortcuts",
        action="store_true",
        default=False,
        help="Create project shortcuts (disabled by default)",
    )
    parser.add_argument(
        "--strip-timestamp",
        action="store_true",
        default=False,
        help="Remove timestamp suffix from generated project root directory (e.g. 2099.1.00001.S)",
    )
    parser.add_argument(
        "--rename",
        help="Rename generated project root directory to specified name or path",
    )
    parser.add_argument(
        "--asdm-cache",
        help="Shared ASDM cache root directory (overrides [paths].asdm_cache)",
    )
    parser.add_argument(
        "--config",
        help="Path to TOML configuration file",
    )
    parser.add_argument(
        "--env",
        help="[envs.<name>] table to resolve environment paths from",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview Java command without executing",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for 'calibpipe pmr'."""
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.procedure_enable_parallel and not args.procedure:
        parser.error("--procedure-enable-parallel requires --procedure")


    asdm_list: list[str] = []
    if args.asdms:
        asdm_list = [a.strip() for a in args.asdms.split(",") if a.strip()]

    cfg = None
    env = None
    explicit_cfg = bool(args.config or args.env or os.environ.get("CALIBPIPE_CONFIG"))
    try:
        cfg = envconfig.load_merged_config(cli_arg=args.config)
    except (envconfig.ConfigError, FileNotFoundError, ValueError) as exc:
        if explicit_cfg or not str(exc).startswith("No config file found"):
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    if cfg is not None and (args.env or cfg.envs):
        try:
            spec = envconfig.resolve_env(cfg, args.env)
            env = envconfig.build_environment(cfg, spec)
        except (envconfig.ConfigError, FileNotFoundError, ValueError) as exc:
            if explicit_cfg:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1

    req = PMRRequest(
        mous=args.mous,
        intents=args.intents,
        procedure=args.procedure,
        download_asdms=args.download,
        download_restore=args.restore,
        asdms=asdm_list,
        omit_asdms=args.omit,
        ignore_qa0=args.ignore_qa0,
        strip_timestamp=args.strip_timestamp,
        rename=args.rename,
    )

    try:
        runtime = PMRRuntime.from_env(env=env, cfg=cfg)
    except OSError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    result = run_pmr(
        req, runtime=runtime, cfg=cfg, dry_run=args.dry_run,
        symlink_shortcuts=args.symlink_shortcuts,
        parallel=args.procedure_enable_parallel,
        asdm_cache=args.asdm_cache,
    )
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
