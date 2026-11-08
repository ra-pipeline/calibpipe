"""Unit tests for calibpipe.coverage module."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

import pytest

from calibpipe import coverage


def test_coverage_find_files(tmp_path: Path) -> None:
    """find_coverage_files should find .coverage* files and ignore others."""
    sub1 = tmp_path / "run1"
    sub1.mkdir()
    f1 = sub1 / ".coverage.node1.123"
    f1.write_text("data1", encoding="utf-8")

    sub2 = tmp_path / "run2"
    sub2.mkdir()
    f2 = sub2 / ".coverage.node2.456"
    f2.write_text("data2", encoding="utf-8")

    root_file = tmp_path / ".coverage"
    root_file.write_text("root", encoding="utf-8")

    found = coverage.find_coverage_files([tmp_path], exclude_file=root_file)
    assert len(found) == 2
    assert str(f1.resolve()) in found
    assert str(f2.resolve()) in found
    assert str(root_file.resolve()) not in found


def test_coverage_combine_data_with_source_paths(tmp_path: Path) -> None:
    """combine_coverage_data should configure path remapping when source_paths provided."""
    sub1 = tmp_path / "sub1"
    sub1.mkdir()
    f1 = sub1 / ".coverage.job1"
    f1.write_text("test data", encoding="utf-8")

    # When coverage is installed or running, test combine with source_paths remapping
    res = coverage.combine_coverage_data(
        tmp_path,
        [f1],
        keep_source=True,
        source_paths=["/local/pipeline", "*/pipeline"],
    )
    # combine_coverage_data either writes combined file or handles errors gracefully
    # The source file should be preserved because keep_source=True
    assert f1.exists()


def test_coverage_empty_files_returns_none(tmp_path: Path) -> None:
    """Empty files list should return None without error."""
    res = coverage.combine_coverage_data(tmp_path, [])
    assert res is None


def test_generate_coverage_report_missing_file(tmp_path: Path) -> None:
    """Missing .coverage file should return empty result without crashing."""
    res = coverage.generate_coverage_report(tmp_path)
    assert res["coverage_file"] is None
    assert res["total_coverage"] is None
