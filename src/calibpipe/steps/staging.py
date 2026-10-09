"""Staging utilities for copying previous calibration and flagging products."""

from __future__ import annotations

import fnmatch
import os
import shutil
from collections.abc import Callable
from pathlib import Path


def find_files(pattern: str, path: str | Path) -> list[str]:
    """Find all files matching glob pattern under path (following symlinks)."""
    result = []
    for root, _, files in os.walk(str(path), followlinks=True):
        for name in files:
            if fnmatch.fnmatch(name, pattern):
                result.append(os.path.join(root, name))
    return result


def find_dirs(pattern: str, path: str | Path) -> list[str]:
    """Find all directories matching glob pattern under path (following symlinks)."""
    result = []
    for root, dirs, _ in os.walk(str(path), followlinks=True):
        for name in dirs:
            if fnmatch.fnmatch(name, pattern):
                result.append(os.path.join(root, name))
    return result


def stage_flags_and_wvr(
    flag_dir: str,
    ppmr_fulldir: str,
    log_func: Callable[[str], None],
) -> None:
    """Copy old flag templates and WVR tables from flag_dir into target working dir.

    Maintains exact behavioral compatibility with calibPipeIF.
    """
    target_working_dir = ppmr_fulldir + "/working/"

    # 1. Match uid*flag*template.txt
    old_flag_files = find_files("uid*flag*template.txt", flag_dir)
    for f in old_flag_files:
        if "html" not in f:
            shutil.copy(f, target_working_dir)
            log_func(f"Copying the flags commands from last pipeline execution {f} {target_working_dir}")

    # 2. Match uid*flagtsystemplate.txt
    old_tsys_files = find_files("uid*flagtsystemplate.txt", flag_dir)
    for f in old_tsys_files:
        if "html" not in f:
            shutil.copy(f, target_working_dir)
            log_func(f"Copying the tsys flags commands from last pipeline execution {f} {target_working_dir}")

    # 3. Match uid*ms.wvr*
    old_wvr_dirs = find_dirs("uid*ms.wvr*", flag_dir)
    for d in old_wvr_dirs:
        table_name = os.path.basename(d)
        dest = os.path.join(ppmr_fulldir, "working", table_name)
        shutil.copytree(d, dest)
        log_func(f"Copying the WVR offsets table from the --flag area {d} {target_working_dir}")


def rotate_directory(
    path: str | Path,
    log_func: Callable[[str], None] | None = None,
) -> Path | None:
    """Safely rotate an existing directory to a timestamped backup path.

    Prevents destructive deletion of prior pipeline runs by renaming
    the existing path to `<name>_backup_<YYYYMMDD_HHMMSS>`.

    Args:
        path: Path to directory or symlink to rotate.
        log_func: Optional logging callback.

    Returns:
        Path to the newly created backup directory, or None if path does not exist.
    """
    from datetime import datetime

    p = Path(path).resolve()
    if p == Path.cwd().resolve():
        if log_func:
            log_func(f"Cannot rotate current working directory {p}, skipping.")
        return None

    if not p.exists() and not p.is_symlink():
        return None

    try:
        mtime = p.stat().st_mtime
        mtime_str = datetime.fromtimestamp(mtime).strftime("%Y%m%d_%H%M%S")
    except OSError:
        mtime_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    backup_name = f"{p.name}_backup_{mtime_str}"
    backup_path = p.parent / backup_name
    counter = 1
    while backup_path.exists():
        backup_path = p.parent / f"{backup_name}_{counter}"
        counter += 1

    shutil.move(str(p), str(backup_path))
    if log_func:
        log_func(f"Rotated existing directory {p} to {backup_path}")

    return backup_path


def stage_ancillary_files(
    workdir: str | Path,
    cont_dat: str | Path | None = None,
    jyperk_csv: str | Path | None = None,
    parameter_list: str | Path | None = None,
    ancillary: list[str | Path] | None = None,
    recipe: str = "",
    log_func: Callable[[str], None] | None = None,
) -> list[Path]:
    """Stage ancillary data files and parameter overrides into target working directory.

    Supports:
        - cont.dat for continuum subtraction/imaging
        - jyperk.csv for Single-Dish Kelvin-to-Jansky conversion factors
        - parameter.list (with optional SEIP_ or QLIP_ prefix based on recipe)
        - Arbitrary ancillary files and directory trees

    Args:
        workdir: Destination working directory where files will be staged.
        cont_dat: Optional path to cont.dat file.
        jyperk_csv: Optional path to jyperk.csv file.
        parameter_list: Optional path to parameter override list.
        ancillary: Optional sequence of additional files or directories to stage.
        recipe: Active pipeline recipe name used for prefixing parameter lists.
        log_func: Optional logging callback.

    Returns:
        List of destination paths for successfully staged items.
    """
    dest_dir = Path(workdir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []

    # 1. cont.dat
    if cont_dat:
        src = Path(cont_dat).expanduser().resolve()
        if not src.is_file():
            if log_func:
                log_func(f"Warning: Continuum file {src} not found, skipping.")
        else:
            target = dest_dir / "cont.dat"
            if src != target:
                shutil.copyfile(src, target, follow_symlinks=True)
            staged.append(target)
            if log_func:
                log_func(f"Staged continuum file {src} -> {target}")

    # 2. jyperk.csv
    if jyperk_csv:
        src = Path(jyperk_csv).expanduser().resolve()
        if not src.is_file():
            if log_func:
                log_func(f"Warning: Jy/K factor file {src} not found, skipping.")
        else:
            target = dest_dir / "jyperk.csv"
            if src != target:
                shutil.copyfile(src, target, follow_symlinks=True)
            staged.append(target)
            if log_func:
                log_func(f"Staged Jy/K factor file {src} -> {target}")

    # 3. parameter.list (with optional VLASS recipe prefix)
    if parameter_list:
        src = Path(parameter_list).expanduser().resolve()
        if not src.is_file():
            if log_func:
                log_func(f"Warning: Parameter list file {src} not found, skipping.")
        else:
            prefix = ""
            if "SEIP" in recipe:
                prefix = "SEIP_"
            elif "QLIP" in recipe:
                prefix = "QLIP_"
            target = dest_dir / f"{prefix}parameter.list"
            if src != target:
                shutil.copyfile(src, target, follow_symlinks=True)
            staged.append(target)
            if log_func:
                log_func(f"Staged parameter list {src} -> {target}")

    # 4. Arbitrary additional ancillary files / directories
    if ancillary:
        for item in ancillary:
            src = Path(item).expanduser().resolve()
            if not src.exists():
                if log_func:
                    log_func(f"Warning: Ancillary path {src} not found, skipping.")
                continue
            target = dest_dir / src.name
            if src != target:
                if src.is_dir():
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(src, target)
                else:
                    shutil.copyfile(src, target, follow_symlinks=True)
            staged.append(target)
            if log_func:
                log_func(f"Staged ancillary item {src} -> {target}")

    return staged


