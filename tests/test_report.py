"""Unit tests for calibpipe.report module."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

import pytest

from calibpipe import report


def test_find_coverage_files(tmp_path: Path) -> None:
    """find_coverage_files should discover nested .coverage* files and honor exclusion."""
    root_cov = tmp_path / ".coverage"
    root_cov.write_text("root coverage data", encoding="utf-8")

    sub1 = tmp_path / "test_1"
    sub1.mkdir()
    cov1 = sub1 / ".coverage.hostA.1001.abc"
    cov1.write_text("cov1 data", encoding="utf-8")

    sub2 = tmp_path / "test_2"
    sub2.mkdir()
    cov2 = sub2 / ".coverage.hostB.1002.def"
    cov2.write_text("cov2 data", encoding="utf-8")

    other_file = sub2 / "results.txt"
    other_file.write_text("not coverage", encoding="utf-8")

    found = report.find_coverage_files([tmp_path], exclude_file=root_cov)
    assert len(found) == 2
    assert cov1.resolve() in found
    assert cov2.resolve() in found
    assert root_cov.resolve() not in found


def test_find_xml_reports(tmp_path: Path) -> None:
    """find_xml_reports should find all regression-results-*.xml files."""
    sub1 = tmp_path / "sub1"
    sub1.mkdir()
    xml1 = sub1 / "regression-results-test1.xml"
    xml1.write_text("<testsuite/>", encoding="utf-8")

    sub2 = tmp_path / "sub2"
    sub2.mkdir()
    xml2 = sub2 / "regression-results-test2.xml"
    xml2.write_text("<testsuite/>", encoding="utf-8")

    other_xml = sub2 / "other.xml"
    other_xml.write_text("<testsuite/>", encoding="utf-8")

    found = report.find_xml_reports([tmp_path])
    assert len(found) == 2
    assert xml1.resolve() in found
    assert xml2.resolve() in found


def test_summarize_xml_reports(tmp_path: Path) -> None:
    """summarize_xml_reports should parse test counts, times, and failures."""
    xml1_content = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1" time="10.5">
    <testcase classname="test_suite" name="test_one" time="10.5"/>
</testsuite>
"""
    xml2_content = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" errors="0" failures="1" skipped="1" tests="2" time="20.0">
    <testcase classname="test_suite" name="test_two" time="15.0">
        <failure message="AssertionError in step 2">Traceback details here</failure>
    </testcase>
    <testcase classname="test_suite" name="test_three" time="5.0">
        <skipped message="test skipped"/>
    </testcase>
</testsuite>
"""
    f1 = tmp_path / "regression-results-1.xml"
    f2 = tmp_path / "regression-results-2.xml"
    f1.write_text(xml1_content, encoding="utf-8")
    f2.write_text(xml2_content, encoding="utf-8")

    summary = report.summarize_xml_reports([f1, f2])
    assert summary["total_tests"] == 3
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["skipped"] == 1
    assert summary["errors"] == 0
    assert summary["duration"] == 30.5
    assert len(summary["failures_list"]) == 1
    assert "test_suite::test_two" in summary["failures_list"][0]["test"]
    assert "AssertionError in step 2" in summary["failures_list"][0]["message"]


def test_format_summary_text_and_markdown() -> None:
    """format_summary_text and format_summary_markdown should format output cleanly."""
    xml_sum = {
        "total_tests": 10,
        "passed": 8,
        "failed": 2,
        "errors": 0,
        "skipped": 0,
        "duration": 120.0,
        "failures_list": [
            {"test": "test_failure_one", "status": "failed", "message": "Assert error"}
        ],
    }
    cov_sum = {
        "total_coverage": 85.5,
        "index_file": "/tmp/workdir/htmlcov/index.html",
    }

    text = report.format_summary_text(xml_sum, cov_sum)
    assert "Tests: 10 | Passed: 8 | Failed: 2" in text
    assert "Total Coverage: 85.50%" in text
    assert "Assert error" in text

    md = report.format_summary_markdown(xml_sum, cov_sum)
    assert "# Calibpipe Test & Coverage Summary" in md
    assert "| **Passed** | 8 |" in md
    assert "| **Failed** | 2 |" in md
    assert "85.50%" in md


def test_generate_report_writes_summary_files(tmp_path: Path) -> None:
    """generate_report should create test-summary.txt and test-summary.md."""
    xml_content = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1" time="5.0">
    <testcase classname="tests" name="test_dummy" time="5.0"/>
</testsuite>
"""
    xml_file = tmp_path / "sub" / "regression-results-dummy.xml"
    xml_file.parent.mkdir()
    xml_file.write_text(xml_content, encoding="utf-8")

    xml_res, cov_res = report.generate_report(tmp_path, coverage=False, xml=True)
    assert xml_res is not None
    assert xml_res["total_tests"] == 1
    assert xml_res["passed"] == 1

    summary_txt = tmp_path / "test-summary.txt"
    summary_md = tmp_path / "test-summary.md"
    assert summary_txt.is_file()
    assert summary_md.is_file()
    assert "Tests: 1 | Passed: 1" in summary_txt.read_text(encoding="utf-8")
    assert "# Calibpipe Test & Coverage Summary" in summary_md.read_text(encoding="utf-8")
