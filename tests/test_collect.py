"""Unit tests for test discovery, node collection, and runfile generation."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from calibpipe import collect


def test_parse_test_name() -> None:
    # ALMA fast regression test
    node1 = (
        "tests/regression/fast/alma_if_fast_test.py::"
        "test_uid___A002_Xc46ab2_X15ae_repSPW_spw16_17_small__PPR__regression"
    )
    assert (
        collect.parse_test_name(node1)
        == "uid___A002_Xc46ab2_X15ae_repSPW_spw16_17_small__PPR"
    )

    # VLA fast regression test
    node2 = (
        "tests/regression/fast/vla_fast_test.py::"
        "test_13A_537__calibration__PPR__regression"
    )
    assert collect.parse_test_name(node2) == "13A_537__calibration__PPR"

    # Parametrized or special character test
    node3 = "tests/test_foo.py::test_bar[param1-param2]"
    assert collect.parse_test_name(node3) == "bar_param1-param2_"


def test_build_test_cmd() -> None:
    node = "tests/regression/fast/test_alma.py::test_uid123__regression"
    cmd = collect.build_test_cmd(node, test_name="uid123")
    assert "os.environ.pop('SCIPIPE_ROOTDIR', None)" in cmd
    assert "pytest.main(" in cmd
    assert "--junitxml=regression-results-uid123.xml" in cmd
    assert node in cmd

    # Extra options
    cmd_extra = collect.build_test_cmd(node, extra_opts=["-s", "--maxfail=1"])
    assert "'-s'" in cmd_extra
    assert "'--maxfail=1'" in cmd_extra


def test_resolve_python_executable(tmp_path: Path) -> None:
    # When fake CASA root has bin/python3
    casa_root = tmp_path / "casa_fake"
    bin_dir = casa_root / "bin"
    bin_dir.mkdir(parents=True)
    fake_py = bin_dir / "python3"
    fake_py.write_text("#!/bin/sh\n")
    fake_py.chmod(0o755)

    assert collect.resolve_python_executable(str(casa_root)) == str(fake_py)

    # When casa_root is None or invalid, falls back to sys.executable
    assert "python" in collect.resolve_python_executable(None)


def test_collect_tests_with_collected_tests_file(tmp_path: Path) -> None:
    heuristics_dir = tmp_path / "pipeline"
    heuristics_dir.mkdir()
    collected_txt = heuristics_dir / "collected_tests.txt"

    def fake_subprocess_run(cmd, **kwargs):
        # Simulate pytest writing collected_tests.txt
        collected_txt.write_text(
            "tests/regression/fast/test_1.py::test_a__regression\n"
            "tests/regression/fast/test_2.py::test_b__regression\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="Tests collection completed.")

    with patch("subprocess.run", side_effect=fake_subprocess_run):
        nodes = collect.collect_tests(
            target="tests/regression/fast",
            heuristics_dir=heuristics_dir,
            marker="regression",
        )
        assert len(nodes) == 2
        assert nodes[0] == "tests/regression/fast/test_1.py::test_a__regression"
        assert nodes[1] == "tests/regression/fast/test_2.py::test_b__regression"
        assert not collected_txt.exists()  # Cleaned up after reading


def test_collect_tests_stdout_fallback(tmp_path: Path) -> None:
    heuristics_dir = tmp_path / "pipeline"
    heuristics_dir.mkdir()

    stdout_output = (
        "rootdir: /tmp/pipeline\n"
        "[pytest] tests/regression/fast/test_1.py::test_a__regression\n"
        "tests/regression/fast/test_2.py::test_b__regression\n"
    )

    with patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, stdout=stdout_output, stderr=""),
    ):
        nodes = collect.collect_tests(
            target="tests/regression/fast",
            heuristics_dir=heuristics_dir,
        )
        assert len(nodes) == 2
        assert nodes[0] == "tests/regression/fast/test_1.py::test_a__regression"
        assert nodes[1] == "tests/regression/fast/test_2.py::test_b__regression"


def test_write_and_read_test_runfile(tmp_path: Path) -> None:
    nodes = [
        "tests/regression/fast/test_1.py::test_uid___A002_X1__regression",
        "tests/regression/fast/test_2.py::test_uid___A002_X2__regression",
    ]
    out_file = tmp_path / "my_tests.runfile"
    written = collect.write_test_runfile(out_file, nodes)
    assert written.is_file()

    parsed = collect.read_test_runfile(written)
    assert len(parsed) == 2
    assert parsed[0] == ("uid___A002_X1", nodes[0])
    assert parsed[1] == ("uid___A002_X2", nodes[1])
