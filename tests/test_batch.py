"""Tests for calibpipe.batch (migrated from test_runbatch.py)."""

from __future__ import annotations

import io
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from calibpipe import batch as runbatch

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REFERENCE_FILE = Path(__file__).resolve().parent / "reference" / "sbatch_commands.txt"

CONFIG = FIXTURES / "config.toml"
CONFIG_SUBMIT_HOST = FIXTURES / "config_submit_host.toml"
PIPEFILE = FIXTURES / "pipefile.txt"
PIPEFILE_ONE_LINE = FIXTURES / "pipefile_one_line.txt"

FAKE_CALIBPIPEIF = Path("/fake/checkout/scripts/calibPipeIF.py")
FAKE_USER = "testuser"
FAKE_DATE = "2026-01-01"


def _load_reference():
    """Parse reference/sbatch_commands.txt into {name: {"command": ..., "script": ...}}."""
    text = REFERENCE_FILE.read_text()
    pattern = re.compile(
        r"=====BEGIN (?P<name>\S+)=====\n"
        r"-----COMMAND-----\n(?P<command>.*?)\n"
        r"-----SCRIPT-----\n(?P<script>.*?)"
        r"=====END (?P=name)=====\n?",
        re.DOTALL,
    )
    blocks = {}
    for m in pattern.finditer(text):
        blocks[m.group("name")] = {
            "command": m.group("command"),
            "script": m.group("script"),
        }
    return blocks


def _format_block(name, command, script):
    return (
        f"=====BEGIN {name}=====\n"
        f"-----COMMAND-----\n{command}\n"
        f"-----SCRIPT-----\n{script}"
        f"=====END {name}=====\n"
    )


def _normalize(text, sbatch_script_path):
    text = text.replace(sbatch_script_path, "<SBATCH_SCRIPT>")
    text = text.replace(str(FAKE_CALIBPIPEIF), "<CALIBPIPEIF>")
    text = text.replace(str(CONFIG), "<CONFIG>")
    return text


class RunbatchCaptureCase(unittest.TestCase):
    def run_and_capture(self, argv):
        calls = []

        def fake_run(cmd, check=True):
            sbatch_path = cmd[-1]
            script_content = Path(sbatch_path).read_text()
            normalized_cmd = " ".join(_normalize(part, sbatch_path) for part in cmd)
            calls.append((normalized_cmd, _normalize(script_content, sbatch_path)))
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with patch.object(runbatch, "CALIBPIPEIF", FAKE_CALIBPIPEIF), \
             patch.object(runbatch.subprocess, "run", side_effect=fake_run), \
             patch.object(runbatch.time, "strftime", return_value=FAKE_DATE), \
             patch.object(runbatch.time, "sleep", return_value=None), \
             patch.dict(runbatch.os.environ, {"USER": FAKE_USER}), \
             patch.object(sys, "argv", ["runbatch.py"] + argv), \
             redirect_stdout(io.StringIO()):
            runbatch.main()

        return calls

    def assert_matches_reference(self, name, command, script):
        reference = _load_reference()
        self.assertIn(name, reference, f"no reference block named {name!r} in {REFERENCE_FILE}")
        self.assertEqual(command, reference[name]["command"], f"sbatch command mismatch for {name}")
        self.assertEqual(script, reference[name]["script"], f"sbatch script mismatch for {name}")


class TestBasicPipefile(RunbatchCaptureCase):
    def test_two_real_lines_produce_two_matching_submissions(self):
        calls = self.run_and_capture([
            str(PIPEFILE), "--env=main", f"--config={CONFIG}",
        ])
        self.assertEqual(len(calls), 2, "comment/blank lines should be skipped")

        command0, script0 = calls[0]
        self.assert_matches_reference("basic_line1_explicit_recipe", command0, script0)

        command1, script1 = calls[1]
        self.assert_matches_reference("basic_line2_default_recipe", command1, script1)


class TestCliFlags(RunbatchCaptureCase):
    def test_custom_cores_mem_and_batch2_queue(self):
        calls = self.run_and_capture([
            str(PIPEFILE_ONE_LINE), "--env=main", f"--config={CONFIG}",
            "-c", "4", "-m", "32", "-b",
        ])
        self.assertEqual(len(calls), 1)
        command, script = calls[0]
        self.assert_matches_reference("custom_cores_mem_batch2", command, script)

    def test_extra_args_and_different_env_are_forwarded(self):
        calls = self.run_and_capture([
            str(PIPEFILE_ONE_LINE), "--env=pl2025", f"--config={CONFIG}",
            "--extra-arg=--legacy", "--extra-arg=--noupdate",
        ])
        self.assertEqual(len(calls), 1)
        command, script = calls[0]
        self.assert_matches_reference("extra_args_and_pl2025_env", command, script)

    def test_custom_outfile_errfile_and_mail_type(self):
        calls = self.run_and_capture([
            str(PIPEFILE_ONE_LINE), "--env=main", f"--config={CONFIG}",
            "-o", "my.out", "-e", "my.err", "-M", "FAIL",
        ])
        self.assertEqual(len(calls), 1)
        command, script = calls[0]
        self.assert_matches_reference("custom_outfile_errfile_mail_type", command, script)


class TestErrorPaths(RunbatchCaptureCase):
    def test_missing_config_exits_1(self):
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            self.run_and_capture([str(PIPEFILE), "--config=/nonexistent/config.toml"])
        self.assertEqual(cm.exception.code, 1)

    def test_missing_pipefile_exits_1(self):
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            self.run_and_capture(["/nonexistent/pipefile.txt", f"--config={CONFIG}"])
        self.assertEqual(cm.exception.code, 1)

    def test_no_arguments_prints_help_and_exits_0(self):
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            self.run_and_capture([])
        self.assertEqual(cm.exception.code, 0)

    def test_submit_host_mismatch_aborts_before_any_submission(self):
        with patch.object(runbatch.subprocess, "getoutput", return_value="some-other-host"):
            with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
                self.run_and_capture([str(PIPEFILE), f"--config={CONFIG_SUBMIT_HOST}"])
            self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
