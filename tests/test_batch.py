"""Tests for calibpipe.batch (migrated from test_runbatch.py)."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
FAKE_DATE = "20260101-000000"


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


def _normalize(text, sbatch_script_path):
    text = text.replace(sbatch_script_path, "<SBATCH_SCRIPT>")
    text = text.replace(str(FAKE_CALIBPIPEIF), "<CALIBPIPEIF>")
    text = text.replace(str(CONFIG), "<CONFIG>")
    return text


class RunbatchCaptureCase(unittest.TestCase):
    def _run_in_tmpdir(self, argv):
        """Run batch.main() from a fresh tmpdir; return (calls, tmpdir_path).

        The tmpdir is returned so callers can inspect files written there
        (e.g. .sbatch record files).  The directory persists until the caller
        cleans it up.
        """
        calls = []

        def fake_run(cmd, check=True, **kwargs):
            if cmd[0] != "sbatch":
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="fake queue output")
            sbatch_path = cmd[-1]
            script_content = Path(sbatch_path).read_text()
            normalized_cmd = " ".join(_normalize(part, sbatch_path) for part in cmd)
            calls.append((normalized_cmd, _normalize(script_content, sbatch_path)))
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        tmpdir = tempfile.mkdtemp()
        orig_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            with patch.object(runbatch, "CALIBPIPEIF", FAKE_CALIBPIPEIF), \
                 patch.object(runbatch.subprocess, "run", side_effect=fake_run), \
                 patch.object(runbatch.time, "strftime", return_value=FAKE_DATE), \
                 patch.object(runbatch.time, "sleep", return_value=None), \
                 patch.dict(runbatch.os.environ, {"USER": FAKE_USER}), \
                 patch.object(sys, "argv", ["runbatch.py"] + argv), \
                 redirect_stdout(io.StringIO()):
                runbatch.main()
        finally:
            os.chdir(orig_cwd)

        return calls, tmpdir

    def run_and_capture(self, argv):
        """Convenience wrapper that discards the tmpdir after capture."""
        calls, tmpdir = self._run_in_tmpdir(argv)
        shutil.rmtree(tmpdir, ignore_errors=True)
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

    def test_custom_partition_flag(self):
        calls = self.run_and_capture([
            str(PIPEFILE_ONE_LINE), "--env=main", f"--config={CONFIG}",
            "--partition=test_queue",
        ])
        self.assertEqual(len(calls), 1)
        command, script = calls[0]
        self.assertIn("#SBATCH --partition=test_queue", script)

    def test_batch_profile_flag(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as tf:
            tf.write("""
default_env = "main"
[paths]
scipipe_rootdir = "/fake/root/{user}"
scipipe_logdir = "/fake/logs/{user}"
[envs.main]
casa_root = "/fake/casa/main"

[batch]
queue = "plwg"
cores = 8
mem = 248

[batches.debug]
queue = "debug"
cores = 4
mem = 32
walltime = "01:00:00"
""")
            tf_path = tf.name

        try:
            calls = self.run_and_capture([
                str(PIPEFILE_ONE_LINE), "--env=main", f"--config={tf_path}",
                "--profile=debug",
            ])
            self.assertEqual(len(calls), 1)
            command, script = calls[0]
            self.assertIn("#SBATCH --partition=debug", script)
            self.assertIn("#SBATCH --ntasks=4", script)
            self.assertIn("#SBATCH --mem=32G", script)
            self.assertIn("#SBATCH --time=01:00:00", script)
        finally:
            Path(tf_path).unlink(missing_ok=True)


class TestErrorPaths(RunbatchCaptureCase):
    def test_unknown_batch_profile_exits_1(self):
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            self.run_and_capture([str(PIPEFILE_ONE_LINE), f"--config={CONFIG}", "--profile=nonexistent"])
        self.assertEqual(cm.exception.code, 1)

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


class TestScriptRecord(RunbatchCaptureCase):
    """Verify that a .sbatch record file is written alongside each job's logs."""

    def test_record_file_is_created_next_to_out_log(self):
        calls, tmpdir = self._run_in_tmpdir([
            str(PIPEFILE_ONE_LINE), "--env=main", f"--config={CONFIG}",
        ])
        try:
            # Default outfile is batch.<name>.out; record should be batch.<name>.sbatch
            expected_name = f"batch.X3_X3_{FAKE_DATE}.sbatch"
            record_path = Path(tmpdir) / expected_name
            self.assertTrue(record_path.exists(), f"Expected record file {expected_name} in tmpdir")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_record_file_contains_sbatch_directives(self):
        calls, tmpdir = self._run_in_tmpdir([
            str(PIPEFILE_ONE_LINE), "--env=main", f"--config={CONFIG}",
        ])
        try:
            expected_name = f"batch.X3_X3_{FAKE_DATE}.sbatch"
            content = (Path(tmpdir) / expected_name).read_text()
            self.assertIn("#SBATCH --partition=plwg", content)
            self.assertIn("#SBATCH --mem=248G", content)
            self.assertIn(f"#SBATCH --job-name=X3_X3_{FAKE_DATE}", content)
            self.assertIn("#SBATCH --mail-user=testuser", content)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_record_write_failure_does_not_abort_submission(self):
        """If the record file can't be written, the job is still submitted."""
        calls, tmpdir = self._run_in_tmpdir([
            str(PIPEFILE_ONE_LINE), "--env=main", f"--config={CONFIG}",
            "-o", "/nonexistent/dir/batch.out", "-e", "/nonexistent/dir/batch.err",
        ])
        try:
            # submission should still have happened despite the bad outfile dir
            self.assertEqual(len(calls), 1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestSlurmDirectives(RunbatchCaptureCase):
    """Verify that advanced Slurm directives render correctly in submitted scripts."""

    def test_advanced_directives_rendered_in_sbatch(self):
        calls = self.run_and_capture([
            str(PIPEFILE_ONE_LINE),
            "--env=main",
            f"--config={CONFIG}",
            "-t", "12:00:00",
            "--nodelist=cvpost01",
            "--chdir=/lustre/work",
            "--cpus-per-task=4",
            "--ntasks-per-core=1",
            "--hint=nomultithread",
            "--distribution=cyclic:cyclic",
        ])
        self.assertEqual(len(calls), 1)
        _, script = calls[0]
        self.assertIn("#SBATCH --time=12:00:00", script)
        self.assertIn("#SBATCH --nodelist=cvpost01", script)
        self.assertIn("#SBATCH --chdir=/lustre/work", script)
        self.assertIn("#SBATCH --cpus-per-task=4", script)
        self.assertIn("#SBATCH --ntasks-per-core=1", script)
        self.assertIn("#SBATCH --hint=nomultithread", script)
        self.assertIn("#SBATCH --distribution=cyclic:cyclic", script)
        self.assertIn("#SBATCH --no-requeue", script)
        self.assertIn("#SBATCH --mem=248G", script)

    def test_mem_per_cpu_suppresses_mem(self):
        calls = self.run_and_capture([
            str(PIPEFILE_ONE_LINE),
            "--env=main",
            f"--config={CONFIG}",
            "--mem-per-cpu=30G",
        ])
        self.assertEqual(len(calls), 1)
        _, script = calls[0]
        self.assertIn("#SBATCH --mem-per-cpu=30G", script)
        self.assertNotIn("#SBATCH --mem=", script)

    def test_requeue_flag_omits_no_requeue(self):
        calls = self.run_and_capture([
            str(PIPEFILE_ONE_LINE),
            "--env=main",
            f"--config={CONFIG}",
            "--requeue",
        ])
        self.assertEqual(len(calls), 1)
        _, script = calls[0]
        self.assertNotIn("#SBATCH --no-requeue", script)

    def test_mem_and_mem_per_cpu_cli_mutual_exclusion(self):
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            self.run_and_capture([
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
                "-m", "100",
                "--mem-per-cpu=30G",
            ])
        self.assertEqual(cm.exception.code, 2)

    def test_print_queue_executes_squeue_with_pcasa_format(self):
        with patch.object(runbatch.shutil, "which", return_value="/usr/bin/squeue"), \
             patch.object(runbatch.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="  12345 plwg rxue R ..."
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                runbatch.print_queue("rxue")
            mock_run.assert_called_once()
            cmd_args = mock_run.call_args[0][0]
            self.assertEqual(cmd_args[0], "/usr/bin/squeue")
            self.assertIn("--format=%7i %13P %9u %7T %11M %11l %5D %2C %2c/%7m %16R %50j %50Z", cmd_args[1])
            self.assertIn("rxue", cmd_args)
            self.assertIn("12345 plwg rxue R", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
