"""Staging utilities for copying previous calibration and flagging products."""

from __future__ import annotations

import fnmatch
import os
import shutil
from pathlib import Path
from typing import Callable, List


def find_files(pattern: str, path: str | Path) -> List[str]:
    """Find all files matching glob pattern under path (following symlinks)."""
    result = []
    for root, _, files in os.walk(str(path), followlinks=True):
        for name in files:
            if fnmatch.fnmatch(name, pattern):
                result.append(os.path.join(root, name))
    return result


def find_dirs(pattern: str, path: str | Path) -> List[str]:
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

