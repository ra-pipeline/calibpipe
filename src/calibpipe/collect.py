"""Regression test discovery, node collection, and command building for calibpipe."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path


def parse_test_name(node_id: str) -> str:
    """Extract a concise, filesystem-safe test name from a pytest node ID.

    Examples:
        >>> parse_test_name("tests/regression/fast/alma_if_fast_test.py::test_uid___A002_X123__PPR__regression")
        'uid___A002_X123__PPR'
        >>> parse_test_name("tests/regression/fast/vla_fast_test.py::test_13A_537__calibration__PPR__regression")
        '13A_537__calibration__PPR'
    """
    raw_name = node_id.split("::")[-1]
    name = re.sub(r"^test_", "", raw_name)
    name = re.sub(r"(__regression|_regression)$", "", name)
    sanitized = re.sub(r"[^\w\-.]", "_", name)
    return sanitized or raw_name


def build_test_cmd(
    node_id: str,
    test_name: str | None = None,
    extra_opts: Sequence[str] | str | None = None,
) -> str:
    """Build an inline Python command string to execute a single test in CASA.

    Args:
        node_id: Full pytest node ID (e.g. 'tests/.../test_x.py::test_fn').
        test_name: Optional short name used for the JUnit XML filename.
        extra_opts: Optional extra arguments to append to pytest.main().

    Returns:
        Python one-liner suitable for passing to `calibpipe run --cmd=...`.
    """
    short_name = test_name or parse_test_name(node_id)
    opts_list = ["-vvs", f"--junitxml=regression-results-{short_name}.xml"]
    if extra_opts:
        if isinstance(extra_opts, str):
            opts_list.extend(extra_opts.split())
        else:
            opts_list.extend(list(extra_opts))
    opts_list.append(node_id)
    opts_repr = repr(opts_list)

    return (
        f"import os, pytest; os.environ.pop('SCIPIPE_ROOTDIR', None); "
        f"pytest.main({opts_repr})"
    )


def resolve_python_executable(casa_root: str | None = None) -> str:
    """Find the Python executable under casa_root or fall back to sys.executable."""
    if casa_root:
        root_path = Path(casa_root).expanduser().resolve()
        candidates = [
            root_path / "bin" / "python3",
            root_path / "lib" / "py" / "bin" / "python3",
            root_path / "bin" / "python",
        ]
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)

    return sys.executable


def collect_tests(
    target: str | Path = "tests/regression/fast",
    *,
    casa_root: str | None = None,
    heuristics_dir: str | Path | None = None,
    marker: str | None = None,
    filter_k: str | None = None,
    extra_opts: Sequence[str] | str | None = None,
    timeout: int = 120,
    logger: Callable[[str], None] | None = None,
) -> list[str]:
    """Collect test node IDs from a pipeline repository using CASA Python.

    Args:
        target: Test file or directory path (relative to heuristics_dir or absolute).
        casa_root: Path to CASA installation directory.
        heuristics_dir: Path to pipeline source checkout.
        marker: Pytest '-m' marker expression (e.g. 'not slow').
        filter_k: Pytest '-k' keyword filter expression.
        extra_opts: Optional extra pytest flags.
        timeout: Subprocess timeout in seconds.
        logger: Optional logging callback.

    Returns:
        List of collected test node IDs.
    """
    heuristics_path = (
        Path(heuristics_dir).expanduser().resolve()
        if heuristics_dir
        else Path.cwd().resolve()
    )

    target_str = str(target)
    target_path = Path(target_str)
    if not target_path.is_absolute():
        candidate = heuristics_path / target_path
        if candidate.exists():
            target_str = str(candidate.relative_to(heuristics_path))

    python_exe = resolve_python_executable(casa_root)

    cmd = [
        python_exe,
        "-m",
        "pytest",
        "-qq",
        "--disable-warnings",
        "--collect-tests",
        target_str,
    ]
    if marker:
        cmd.extend(["-m", str(marker)])
    if filter_k:
        cmd.extend(["-k", str(filter_k)])
    if extra_opts:
        if isinstance(extra_opts, str):
            cmd.extend(extra_opts.split())
        else:
            cmd.extend(list(extra_opts))

    if logger:
        logger(f"Collecting tests in {heuristics_path}: {' '.join(cmd)}")

    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    ppath = [str(heuristics_path), str(heuristics_path / "tests")]
    if "PYTHONPATH" in env:
        ppath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = ":".join(ppath)

    collected_file = heuristics_path / "collected_tests.txt"
    if collected_file.exists():
        try:
            collected_file.unlink()
        except OSError:
            pass

    proc = subprocess.run(
        cmd,
        cwd=str(heuristics_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )

    node_ids: list[str] = []
    if collected_file.exists():
        try:
            raw_text = collected_file.read_text(encoding="utf-8")
            node_ids = [line.strip() for line in raw_text.splitlines() if line.strip()]
            collected_file.unlink()
        except OSError as err:
            if logger:
                logger(f"Warning: Failed reading {collected_file}: {err}")

    if not node_ids and proc.stdout:
        for line in proc.stdout.splitlines():
            line_str = line.strip()
            if "::" in line_str and not line_str.startswith("#"):
                clean = re.sub(r"^\[.*?\]\s*", "", line_str)
                node_ids.append(clean)

    # Normalize paths relative to heuristics_path
    normalized: list[str] = []
    prefix = str(heuristics_path)
    for node in node_ids:
        if node.startswith(prefix):
            rel = node[len(prefix):].lstrip("/")
            normalized.append(rel)
        else:
            normalized.append(node)

    return normalized


def write_test_runfile(path: str | Path, node_ids: Sequence[str]) -> Path:
    """Write collected test node IDs to a runfile for batch execution."""
    out_path = Path(path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# calibpipe test runfile",
        "# Syntax: <test_name>  <node_id>",
    ]
    for node in node_ids:
        test_name = parse_test_name(node)
        lines.append(f"{test_name}\t{node}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def read_test_runfile(path: str | Path) -> list[tuple[str, str]]:
    """Parse a test runfile into a list of (test_name, node_id) tuples."""
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Test runfile not found: {file_path}")

    results: list[tuple[str, str]] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line_clean = line.strip()
        if not line_clean or line_clean.startswith("#"):
            continue
        parts = line_clean.split(None, 1)
        if len(parts) == 2 and "::" in parts[1]:
            results.append((parts[0], parts[1]))
        elif len(parts) >= 1 and "::" in parts[0]:
            node_id = parts[0]
            results.append((parse_test_name(node_id), node_id))

    return results
