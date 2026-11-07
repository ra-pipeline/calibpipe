"""Shared convenience shortcuts for pipeline workspaces."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path


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
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
    )
    if not html_dirs:
        products_dir = work.parent / "products"
        if products_dir.is_dir():
            html_dirs = sorted(
                products_dir.glob("pipeline-*/html"),
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
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

