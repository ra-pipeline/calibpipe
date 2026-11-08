"""Finds, combines, and reports on test results and coverage data.

Supports:
1. Combining distributed `.coverage.*` files into a single `.coverage` dataset
   and generating terminal + HTML reports (analogous to plutils.metrics.coverage).
2. Parsing and consolidating JUnit XML files (e.g. `regression-results-*.xml`)
   into an aggregated test status summary table.
"""

from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Sequence
import xml.etree.ElementTree as ET

from calibpipe import coverage as cov_util


def find_coverage_files(search_paths: Sequence[str | Path], *, exclude_file: str | Path | None = None) -> list[Path]:
    """Find all files matching '.coverage*' within the given paths or directory patterns.

    Args:
        search_paths: Sequence of directory paths or glob patterns.
        exclude_file: Optional path to exclude (e.g. the destination combined file).

    Returns:
        Sorted list of resolved Path objects.
    """
    return [Path(p) for p in cov_util.find_coverage_files(search_paths, exclude_file=exclude_file)]


def combine_coverage(
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
        casa_root: Optional CASA installation directory for python fallback.

    Returns:
        Path to the combined `.coverage` file, or None on failure.
    """
    return cov_util.combine_coverage_data(
        output_dir,
        data_files,
        keep_source=keep_source,
        source_paths=source_paths,
        casa_root=casa_root,
    )


def generate_coverage_report(
    output_dir: str | Path,
    *,
    html: bool = True,
    casa_root: str | Path | None = None,
) -> dict[str, Any]:
    """Generate terminal and HTML coverage reports from .coverage in output_dir.

    Returns:
        Dictionary with coverage stats and generated report paths.
    """
    return cov_util.generate_coverage_report(output_dir, html=html, casa_root=casa_root)


def find_xml_reports(search_paths: Sequence[str | Path]) -> list[Path]:
    """Find all JUnit XML test result files within search paths."""
    found: set[Path] = set()
    for item in search_paths:
        expanded = os.path.expanduser(str(item))
        if os.path.isfile(expanded):
            p = Path(expanded).resolve()
            if p.suffix.lower() == ".xml":
                found.add(p)
            continue

        for matched in glob.glob(expanded):
            p_matched = Path(matched).resolve()
            if p_matched.is_file() and p_matched.suffix.lower() == ".xml":
                found.add(p_matched)
            elif p_matched.is_dir():
                for xml_file in p_matched.rglob("*.xml"):
                    if xml_file.is_file() and "regression-results" in xml_file.name:
                        found.add(xml_file.resolve())
    return sorted(found)


def summarize_xml_reports(xml_files: Sequence[str | Path]) -> dict[str, Any]:
    """Parse JUnit XML files and summarize results."""
    summary: dict[str, Any] = {
        "total_files": len(xml_files),
        "total_tests": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "duration": 0.0,
        "cases": [],
        "failures_list": [],
    }

    for xml_path in xml_files:
        p = Path(xml_path)
        try:
            tree = ET.parse(p)
            root = tree.getroot()
        except (ET.ParseError, OSError) as exc:
            summary["failures_list"].append({
                "test": p.name,
                "status": "error",
                "message": f"Malformed XML ({exc})",
            })
            summary["errors"] += 1
            continue

        suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
        if not suites:
            testcases = root.findall(".//testcase")
            if testcases:
                suites = [root]

        for suite in suites:
            for case in suite.findall(".//testcase"):
                name = case.attrib.get("name", "unknown")
                classname = case.attrib.get("classname", "")
                time_val = float(case.attrib.get("time", 0.0))
                summary["total_tests"] += 1
                summary["duration"] += time_val

                failure = case.find("failure")
                error = case.find("error")
                skipped = case.find("skipped")

                if failure is not None:
                    summary["failed"] += 1
                    msg = failure.attrib.get("message") or (failure.text or "").strip().splitlines()[:2]
                    msg_str = " ".join(msg) if isinstance(msg, list) else str(msg)
                    summary["failures_list"].append({
                        "test": f"{classname}::{name}" if classname else name,
                        "status": "failed",
                        "message": msg_str[:120],
                    })
                elif error is not None:
                    summary["errors"] += 1
                    msg = error.attrib.get("message") or (error.text or "").strip().splitlines()[:2]
                    msg_str = " ".join(msg) if isinstance(msg, list) else str(msg)
                    summary["failures_list"].append({
                        "test": f"{classname}::{name}" if classname else name,
                        "status": "error",
                        "message": msg_str[:120],
                    })
                elif skipped is not None:
                    summary["skipped"] += 1
                else:
                    summary["passed"] += 1

    return summary


def format_summary_text(xml_summary: dict[str, Any] | None, cov_summary: dict[str, Any] | None) -> str:
    """Format an ASCII terminal summary."""
    lines = [
        "=" * 80,
        "                    CALIBPIPE REGRESSION & COVERAGE SUMMARY",
        "=" * 80,
    ]
    if xml_summary:
        dur = f"{xml_summary['duration']:.1f}s"
        lines.append(
            f"Tests: {xml_summary['total_tests']} | "
            f"Passed: {xml_summary['passed']} | "
            f"Failed: {xml_summary['failed']} | "
            f"Errors: {xml_summary['errors']} | "
            f"Skipped: {xml_summary['skipped']} | "
            f"Time: {dur}"
        )
        if xml_summary["failures_list"]:
            lines.append("-" * 80)
            lines.append("Failures & Errors:")
            for item in xml_summary["failures_list"]:
                lines.append(f"  * [{item['status'].upper()}] {item['test']}: {item['message']}")

    if cov_summary and cov_summary.get("total_coverage") is not None:
        lines.append("-" * 80)
        lines.append(f"Total Coverage: {cov_summary['total_coverage']:.2f}%")
        if cov_summary.get("index_file"):
            lines.append(f"HTML Report:    {cov_summary['index_file']}")

    lines.append("=" * 80)
    return "\n".join(lines)


def format_summary_markdown(xml_summary: dict[str, Any] | None, cov_summary: dict[str, Any] | None) -> str:
    """Format a GitHub-flavored Markdown summary."""
    lines = ["# Calibpipe Test & Coverage Summary\n"]
    if xml_summary:
        lines.append("| Metric | Count |")
        lines.append("| :--- | :--- |")
        lines.append(f"| **Total Tests** | {xml_summary['total_tests']} |")
        lines.append(f"| **Passed** | {xml_summary['passed']} |")
        lines.append(f"| **Failed** | {xml_summary['failed']} |")
        lines.append(f"| **Errors** | {xml_summary['errors']} |")
        lines.append(f"| **Skipped** | {xml_summary['skipped']} |")
        lines.append(f"| **Duration** | {xml_summary['duration']:.1f}s |\n")

        if xml_summary["failures_list"]:
            lines.append("### Failures and Errors\n")
            lines.append("| Status | Test | Details |")
            lines.append("| :--- | :--- | :--- |")
            for item in xml_summary["failures_list"]:
                lines.append(f"| `{item['status'].upper()}` | `{item['test']}` | {item['message']} |")
            lines.append("")

    if cov_summary and cov_summary.get("total_coverage") is not None:
        lines.append("### Code Coverage\n")
        lines.append(f"- **Total Coverage**: `{cov_summary['total_coverage']:.2f}%`")
        if cov_summary.get("index_file"):
            lines.append(f"- **HTML Report**: [{cov_summary['index_file']}](file://{cov_summary['index_file']})")
        lines.append("")

    return "\n".join(lines)


def generate_report(
    workdir: str | Path,
    *,
    coverage: bool = True,
    xml: bool = True,
    html: bool = True,
    source_paths: Sequence[str] | None = None,
    casa_root: str | Path | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Synthesize test results and coverage data in workdir.

    Args:
        workdir: Directory containing individual test subdirectories.
        coverage: Whether to combine and report coverage data.
        xml: Whether to parse and summarize JUnit XML results.
        html: Whether to generate HTML coverage report.
        source_paths: Optional path remapping targets for coverage combining.
        casa_root: Optional CASA root path for python coverage fallback.

    Returns:
        Tuple of (xml_summary, cov_summary).
    """
    base_dir = Path(workdir).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)

    xml_summary: dict[str, Any] | None = None
    cov_summary: dict[str, Any] | None = None

    if xml:
        xml_files = find_xml_reports([base_dir])
        if xml_files:
            xml_summary = summarize_xml_reports(xml_files)

    if coverage:
        cov_files = find_coverage_files([base_dir], exclude_file=base_dir / ".coverage")
        if cov_files:
            combined = combine_coverage(
                base_dir,
                cov_files,
                keep_source=True,
                source_paths=source_paths,
                casa_root=casa_root,
            )
            if combined:
                cov_summary = generate_coverage_report(base_dir, html=html, casa_root=casa_root)

    # Format summaries and write records to disk
    text_summary = format_summary_text(xml_summary, cov_summary)
    md_summary = format_summary_markdown(xml_summary, cov_summary)

    print(text_summary)
    try:
        (base_dir / "test-summary.txt").write_text(text_summary + "\n", encoding="utf-8")
        (base_dir / "test-summary.md").write_text(md_summary + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"Warning: Could not save summary files in {base_dir}: {exc}", file=sys.stderr)

    return xml_summary, cov_summary


def build_parser() -> argparse.ArgumentParser:
    """Build parser for `calibpipe report` subcommand."""
    parser = argparse.ArgumentParser(
        description="Synthesize regression test results and combine coverage datasets."
    )
    parser.add_argument(
        "workdir",
        nargs="?",
        default=".",
        help="Base working directory containing test subdirectories (default: .)",
    )
    parser.add_argument(
        "--no-coverage",
        dest="coverage",
        action="store_false",
        default=True,
        help="Skip combining and reporting coverage data",
    )
    parser.add_argument(
        "--no-xml",
        dest="xml",
        action="store_false",
        default=True,
        help="Skip aggregating JUnit XML test results",
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
    """CLI entrypoint for calibpipe report."""
    parser = build_parser()
    args = parser.parse_args(argv)
    generate_report(
        args.workdir,
        coverage=args.coverage,
        xml=args.xml,
        html=args.html,
        source_paths=args.source_paths,
        casa_root=args.casa_root,
    )


if __name__ == "__main__":
    main()
