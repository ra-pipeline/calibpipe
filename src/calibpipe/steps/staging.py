"""Staging utilities for copying previous calibration and flagging products."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


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
    ancillary: list[Any] | None = None,
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
            if isinstance(item, (list, tuple)) and len(item) == 2:
                src_raw, dst_raw = item
                src = Path(src_raw).expanduser().resolve()
                target = dest_dir / str(dst_raw)
            elif isinstance(item, dict) and "src" in item and "dest" in item:
                src = Path(item["src"]).expanduser().resolve()
                target = dest_dir / str(item["dest"])
            elif isinstance(item, str) and ":" in item and not Path(item).exists():
                src_raw, dst_raw = item.split(":", 1)
                src = Path(src_raw).expanduser().resolve()
                target = dest_dir / str(dst_raw)
            else:
                src = Path(item).expanduser().resolve()
                target = dest_dir / src.name

            if not src.exists():
                if log_func:
                    log_func(f"Warning: Ancillary path {src} not found, skipping.")
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            if src != target:
                if target.exists() and log_func:
                    log_func(f"Notice: Overwriting existing ancillary destination: {target}")
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


_EXECBLOCK_FALLBACK_RE = re.compile(r"<ExecBlockId>\s*([^<\s]+)\s*</ExecBlockId>")


def uid_to_underscores(uid: str) -> str:
    """Normalize an ALMA UID (e.g. ``uid://A001/X1/X1``) to underscore form."""
    return uid.strip().replace("://", "___").replace("/", "_").replace(":", "_")


def extract_eb_uids_from_ppr(ppr_path: str | Path | None) -> list[str]:
    """Extract deduplicated ``<ExecBlockId>`` values from a PPR XML file."""
    if not ppr_path:
        return []
    path = Path(ppr_path)
    if not path.is_file():
        return []

    raw_uids: list[str] = []
    try:
        tree = ET.parse(path)
        for elem in tree.getroot().iter():
            tag = elem.tag.split("}")[-1]
            if tag == "ExecBlockId" and elem.text and elem.text.strip():
                raw_uids.append(elem.text.strip())
    except ET.ParseError:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        raw_uids.extend(m.strip() for m in _EXECBLOCK_FALLBACK_RE.findall(text))
    except OSError:
        return []

    seen: set[str] = set()
    unique: list[str] = []
    for uid in raw_uids:
        if uid not in seen:
            seen.add(uid)
            unique.append(uid)
    return unique


def infer_mous_uid_from_ppr(
    ppr_path: str | Path | None,
    working_dir: str | Path | None = None,
) -> str:
    """Infer an MOUS UID from PPR XML metadata, PPR filename, or directory hierarchy."""
    if ppr_path:
        path = Path(ppr_path)
        if path.is_file():
            try:
                root = ET.parse(path).getroot()
                for elem in root.iter():
                    tag = elem.tag.split("}")[-1]
                    if tag == "RelativePath" and elem.text:
                        match = re.search(r"MOUS_(uid___[A-Za-z0-9_]+)", elem.text)
                        if match:
                            return match.group(1)
                for elem in root.iter():
                    tag = elem.tag.split("}")[-1]
                    if tag == "OusStatusEntityId" and elem.text and elem.text.strip():
                        return elem.text.strip()
            except (ET.ParseError, OSError):
                pass
            name_match = re.search(r"PPR_(uid___[A-Za-z0-9_]+)", path.name)
            if name_match:
                return name_match.group(1)

    if working_dir:
        wpath = Path(working_dir).resolve()
        for candidate in (wpath.name, wpath.parent.name):
            m = re.search(r"MOUS_(uid___[A-Za-z0-9_]+)", candidate)
            if m:
                return m.group(1)

    return ""


def _copy_staged_file(src: Path, target: Path) -> None:
    """Copy *src* to *target* unless both resolve to the same file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != target.resolve():
        shutil.copyfile(src, target, follow_symlinks=True)


def _walk_non_html_files(root: Path) -> list[Path]:
    """Walk *root* once (following symlinks), returning sorted files excluding html paths."""
    matches: list[Path] = []
    root_str = str(root)
    for cur_root, dirs, files in os.walk(root_str, followlinks=True):
        dirs[:] = sorted(d for d in dirs if "html" not in d)
        rel_root = os.path.relpath(cur_root, root_str)
        if rel_root != "." and "html" in rel_root:
            continue
        for name in files:
            rel_file = name if rel_root == "." else os.path.join(rel_root, name)
            if "html" in rel_file:
                continue
            matches.append(Path(cur_root) / name)
    matches.sort(key=lambda p: str(p))
    return matches


def stage_obscal_files(
    workdir: str | Path,
    *,
    obscaldir: str | Path | None = None,
    mous_uid: str = "",
    eb_uids: Sequence[str] = (),
    staticobscal: bool = False,
    oldcont: bool = False,
    flag_dir: str | Path | None = None,
    skip_cont_dat: bool = False,
    skip_files: Sequence[str] = (),
    log_func: Callable[[str], None] | None = None,
) -> list[Path]:
    """Stage ``flux.csv``, ``antennapos.csv``, flag templates, and ``cont.dat``.

    Precedence order for overlapping artifacts:
        1. Explicit ``--cont-dat`` / ``--ancillary`` (``skip_cont_dat`` / ``skip_files``)
        2. ``--flag`` directory (``flag_dir``)
        3. ``[paths].obscaldir`` when ``staticobscal`` / ``oldcont`` is enabled

    When multiple candidates match in ``flag_dir`` or ``obscaldir``, the
    lexicographically first non-html path is selected deterministically.
    """
    if not flag_dir and not staticobscal and not oldcont:
        return []

    dest_dir = Path(workdir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []

    skip_set = set(skip_files)
    if skip_cont_dat:
        skip_set.add("cont.dat")
    claimed: dict[str, Path | None] = {
        fname: (dest_dir / fname if fname in skip_set else None)
        for fname in ("flux.csv", "antennapos.csv", "cont.dat")
    }
    claimed_flag_names: set[str] = set(skip_set)

    def _stage_candidate(
        candidates: list[Path],
        target_name: str,
        *,
        source_desc: str,
        not_found_msg: str | None = None,
    ) -> None:
        if not candidates:
            if not_found_msg and log_func:
                log_func(not_found_msg)
            return
        src = candidates[0]
        existing = claimed[target_name]
        if existing is not None:
            if log_func:
                log_func(f"Notice: found {src}, but {existing} takes precedence")
            return
        target = dest_dir / target_name
        _copy_staged_file(src, target)
        claimed[target_name] = src
        staged.append(target)
        if log_func:
            log_func(f"Staged {target_name} from {source_desc}: {src} -> {target}")

    # 1. Inspect --flag directory first (takes precedence over obscaldir)
    if flag_dir:
        flag_root = Path(flag_dir).expanduser()
        if flag_root.is_dir():
            flag_files = _walk_non_html_files(flag_root)
            for p in flag_files:
                if fnmatch.fnmatch(p.name, "uid*flag*template.txt"):
                    claimed_flag_names.add(p.name)
            for fname in ("flux.csv", "antennapos.csv", "cont.dat"):
                _stage_candidate(
                    [p for p in flag_files if p.name == fname],
                    fname,
                    source_desc="--flag directory",
                )

    if not staticobscal and not oldcont:
        return staged

    # 2. Fall back to obscaldir for --staticobscal / --oldcont
    if not obscaldir:
        if log_func:
            log_func(
                "Warning: --staticobscal/--oldcont enabled, but [paths].obscaldir is not configured; skipping."
            )
        return staged

    obscal_root = Path(obscaldir).expanduser()
    if not obscal_root.is_dir():
        if log_func:
            log_func(
                f"Warning: obscaldir {obscal_root} does not exist or is not a directory; skipping."
            )
        return staged

    mous_under = uid_to_underscores(mous_uid) if mous_uid else ""
    eb_unders = [uid_to_underscores(eb) for eb in eb_uids if eb]
    obscal_files = _walk_non_html_files(obscal_root)

    mous_targets: list[str] = []
    if staticobscal:
        mous_targets.extend(("flux.csv", "antennapos.csv"))
    if oldcont:
        mous_targets.append("cont.dat")

    if mous_targets:
        if mous_under:
            for fname in mous_targets:
                pattern = f"*{mous_under}*{fname}"
                _stage_candidate(
                    [p for p in obscal_files if fnmatch.fnmatch(p.name, pattern)],
                    fname,
                    source_desc="obscaldir",
                    not_found_msg=f"Notice: No {pattern} found in {obscal_root}",
                )
        elif log_func:
            log_func(
                "Notice: MOUS UID unavailable; skipping MOUS obscaldir lookup."
            )

    if staticobscal and eb_unders:
        seen_flag_names = set(claimed_flag_names)
        for p in obscal_files:
            if any(fnmatch.fnmatch(p.name, f"{eb}*flag*template.txt") for eb in eb_unders):
                if p.name in seen_flag_names:
                    continue
                seen_flag_names.add(p.name)
                target = dest_dir / p.name
                _copy_staged_file(p, target)
                staged.append(target)
                if log_func:
                    log_func(f"Staged static flag template: {p} -> {target}")

    return staged


