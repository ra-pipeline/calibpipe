"""Finds, combines, and reports on multiple coverage.py data files.

Provides a robust, modernized alternative to plutils.metrics.coverage with:
1. Native programmatic coverage combination and report generation.
2. Cross-environment path remapping (e.g. cluster compute nodes to local checkout).
3. Automatic fallback to CASA Python's coverage module when running in environments
   where coverage is not installed in the system Python.
"""

from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Sequence


def find_coverage_files(search_patterns: Sequence[str | Path], *, exclude_file: str | Path | None = None) -> list[str]:
    """Find all files matching '.coverage*' within given directory patterns or files.

    Args:
        search_patterns: Sequence of directory paths, file paths, or glob expressions.
        exclude_file: Optional path to exclude (e.g. the destination combined file).

    Returns:
        Sorted list of absolute path strings.
    """
    found_files: set[str] = set()
    exclude_resolved = str(Path(exclude_file).resolve()) if exclude_file else None

    for pattern in search_patterns:
        expanded_pattern = os.path.expanduser(str(pattern))

        # Direct file check
        if os.path.isfile(expanded_pattern):
            resolved = str(Path(expanded_pattern).resolve())
            if ".coverage" in os.path.basename(resolved) and resolved != exclude_resolved:
                found_files.add(resolved)
            continue

        # Glob / directory check
        for matched in glob.glob(expanded_pattern):
            p = Path(matched).resolve()
            if p.is_file() and ".coverage" in p.name and str(p) != exclude_resolved:
                found_files.add(str(p))
            elif p.is_dir():
                for cov_file in p.rglob(".coverage*"):
                    if cov_file.is_file() and str(cov_file.resolve()) != exclude_resolved:
                        found_files.add(str(cov_file.resolve()))

    return sorted(found_files)


def _resolve_coverage_runner(casa_root: str | Path | None = None) -> list[str] | None:
    """Find a Python executable or command with coverage available."""
    try:
        import coverage  # noqa: F401
        return [sys.executable, "-m", "coverage"]
    except ImportError:
        pass

    if casa_root:
        casa_py = Path(casa_root) / "bin" / "python3"
        if not casa_py.is_file():
            casa_py = Path(casa_root) / "lib" / "py" / "bin" / "python3"
        if casa_py.is_file():
            try:
                res = subprocess.run([str(casa_py), "-c", "import coverage"], capture_output=True, check=False)
                if res.returncode == 0:
                    return [str(casa_py), "-m", "coverage"]
            except OSError:
                pass

    cov_exe = shutil.which("coverage")
    if cov_exe:
        return [cov_exe]

    return None


def combine_coverage_data(
    output_dir: str | Path,
    data_files: Sequence[str | Path],
    *,
    keep_source: bool = True,
    source_paths: Sequence[str] | None = None,
    casa_root: str | Path | None = None,
) -> Path | None:
    """Combine coverage data files into a single .coverage file in output_dir.

    Args:
        output_dir: Target directory where combined `.coverage` will be saved.
        data_files: List of coverage data file paths.
        keep_source: If True, keep source files after combining.
        source_paths: Optional list of source directory paths to configure path remapping.
        casa_root: Optional CASA installation directory for Python coverage fallback.

    Returns:
        Path to the combined `.coverage` file, or None on failure.
    """
    if not data_files:
        print("No coverage files found to combine.", file=sys.stderr)
        return None

    out_path = Path(output_dir).resolve()
    out_path.mkdir(parents=True, exist_ok=True)
    combined_file = out_path / ".coverage"

    # Try native Python API first
    try:
        from coverage import Coverage
        from coverage.exceptions import CoverageException

        orig_dir = os.getcwd()
        try:
            os.chdir(out_path)
            cov = Coverage(data_file=str(combined_file))
            if source_paths:
                cov.config.paths["source"] = [str(p) for p in source_paths]
            cov.combine(data_paths=[str(p) for p in data_files], keep=keep_source)
            cov.save()
            if combined_file.exists():
                return combined_file
        except CoverageException as e:
            print(f"Warning: Coverage combine API error: {e}", file=sys.stderr)
        finally:
            os.chdir(orig_dir)
    except ImportError:
        pass

    # Fallback to subprocess coverage runner
    runner = _resolve_coverage_runner(casa_root=casa_root)
    if not runner:
        print("Warning: Neither coverage library nor executable found; skipping coverage merge.", file=sys.stderr)
        return None

    cmd = list(runner) + ["combine"]
    if keep_source:
        cmd.append("--keep")

    # If source_paths provided, create a temporary .coveragerc for CLI remapping
    rc_file: str | None = None
    if source_paths:
        with tempfile.NamedTemporaryFile("w", suffix=".rc", delete=False) as fd:
            fd.write("[paths]\nsource =\n")
            for sp in source_paths:
                fd.write(f"    {sp}\n")
            rc_file = fd.name
        cmd.extend(["--rcfile", rc_file])

    cmd.extend(str(p) for p in data_files)

    try:
        subprocess.run(cmd, cwd=str(out_path), check=True, capture_output=True, text=True)
        if combined_file.exists():
            return combined_file
    except subprocess.CalledProcessError as exc:
        print(f"Warning: Coverage combine subprocess failed (code {exc.returncode}): {exc.stderr}", file=sys.stderr)
    finally:
        if rc_file and os.path.exists(rc_file):
            os.unlink(rc_file)

    return combined_file if combined_file.exists() else None


def generate_coverage_report(
    output_dir: str | Path,
    *,
    html: bool = True,
    casa_root: str | Path | None = None,
) -> dict[str, Any]:
    """Generate terminal and HTML coverage reports from .coverage in output_dir.

    Args:
        output_dir: Directory containing the `.coverage` file.
        html: Whether to generate an HTML report in `htmlcov/`.
        casa_root: Optional CASA installation directory for Python coverage fallback.

    Returns:
        Dictionary containing `total_coverage`, `html_dir`, and `index_file`.
    """
    out_path = Path(output_dir).resolve()
    cov_file = out_path / ".coverage"
    html_dir = out_path / "htmlcov"
    result: dict[str, Any] = {
        "coverage_file": str(cov_file) if cov_file.exists() else None,
        "total_coverage": None,
        "html_dir": None,
        "index_file": None,
    }

    if not cov_file.exists():
        print(f"No .coverage file found in {out_path}", file=sys.stderr)
        return result

    # Try native Python API first
    try:
        from coverage import Coverage
        from coverage.exceptions import CoverageException

        orig_dir = os.getcwd()
        try:
            os.chdir(out_path)
            cov = Coverage(data_file=str(cov_file))
            cov.load()
            total = cov.report(show_missing=True)
            result["total_coverage"] = float(total)

            if html:
                html_dir.mkdir(parents=True, exist_ok=True)
                cov.html_report(directory=str(html_dir))
                index_path = html_dir / "index.html"
                if index_path.exists():
                    result["html_dir"] = str(html_dir)
                    result["index_file"] = str(index_path)
            return result
        except CoverageException as e:
            print(f"Warning: Coverage reporting API error: {e}", file=sys.stderr)
        finally:
            os.chdir(orig_dir)
    except ImportError:
        pass

    # Fallback to subprocess
    runner = _resolve_coverage_runner(casa_root=casa_root)
    if runner:
        try:
            rep_cmd = list(runner) + ["report"]
            proc = subprocess.run(rep_cmd, cwd=str(out_path), capture_output=True, text=True, check=False)
            if proc.returncode == 0:
                print("\n" + proc.stdout)
                for line in proc.stdout.strip().splitlines():
                    if line.startswith("TOTAL"):
                        parts = line.split()
                        if parts:
                            pct = parts[-1].rstrip("%")
                            try:
                                result["total_coverage"] = float(pct)
                            except ValueError:
                                pass

            if html:
                html_cmd = list(runner) + ["html", "-d", str(html_dir)]
                subprocess.run(html_cmd, cwd=str(out_path), check=False)
                index_path = html_dir / "index.html"
                if index_path.exists():
                    result["html_dir"] = str(html_dir)
                    result["index_file"] = str(index_path)
        except OSError as exc:
            print(f"Warning: Failed to execute coverage report: {exc}", file=sys.stderr)

    return result


def combine_with_report(
    search_paths: Sequence[str | Path],
    output_dir: str | Path,
    *,
    keep_source: bool = True,
    source_paths: Sequence[str] | None = None,
    casa_root: str | Path | None = None,
) -> dict[str, Any]:
    """Find, combine, and generate reports for multiple coverage.py data files.

    Automates the full coverage workflow:
    1. Finding all `.coverage*` files across search paths.
    2. Combining them into a single `.coverage` file in `output_dir` (with optional path remapping).
    3. Generating terminal summary and full HTML report in `output_dir/htmlcov`.

    Args:
        search_paths: Paths or glob patterns to search for `.coverage*` files.
        output_dir: Destination directory for combined `.coverage` and HTML reports.
        keep_source: If True, keep source files after combining.
        source_paths: Optional path remapping targets.
        casa_root: Optional CASA installation directory for Python coverage fallback.

    Returns:
        Dictionary with reporting details.
    """
    out_path = Path(output_dir).resolve()
    cov_files = find_coverage_files(search_paths, exclude_file=out_path / ".coverage")
    if not cov_files:
        print("No coverage files found across search paths.", file=sys.stderr)
        return {"total_coverage": None, "html_dir": None, "index_file": None}

    combined = combine_coverage_data(
        out_path,
        cov_files,
        keep_source=keep_source,
        source_paths=source_paths,
        casa_root=casa_root,
    )
    if not combined:
        return {"total_coverage": None, "html_dir": None, "index_file": None}

    return generate_coverage_report(out_path, html=True, casa_root=casa_root)


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser for `calibpipe coverage` subcommand."""
    parser = argparse.ArgumentParser(
        description="Find, combine, and report on multiple coverage.py data files."
    )
    parser.add_argument(
        "workdir",
        nargs="?",
        default=".",
        help="Base directory containing test runs or .coverage data files (default: .)",
    )
    parser.add_argument(
        "--no-html",
        dest="html",
        action="store_false",
        default=True,
        help="Skip generating HTML coverage report",
    )
    parser.add_argument(
        "--remap-source",
        action="append",
        dest="source_paths",
        default=None,
        help="Path remapping prefix (can be specified multiple times)",
    )
    parser.add_argument(
        "--casa-root",
        help="CASA root path for Python coverage execution fallback",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point for `calibpipe coverage`."""
    parser = build_parser()
    args = parser.parse_args(argv)
    workdir = Path(args.workdir).resolve()
    combine_with_report(
        [workdir],
        workdir,
        source_paths=args.source_paths,
        casa_root=args.casa_root,
    )


if __name__ == "__main__":
    main()
