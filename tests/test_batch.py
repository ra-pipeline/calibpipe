"""Tests for calibpipe.batch (migrated from test_runbatch.py)."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
            if cmd[0] not in ("sbatch", "condor_submit"):
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="fake queue output"
                )
            sub_path = cmd[-1]
            script_content = Path(sub_path).read_text()
            normalized_cmd = " ".join(_normalize(part, sub_path) for part in cmd)
            calls.append((normalized_cmd, _normalize(script_content, sub_path)))
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        tmpdir = tempfile.mkdtemp()
        orig_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            with (
                patch.object(runbatch, "CALIBPIPEIF", FAKE_CALIBPIPEIF),
                patch.object(runbatch.subprocess, "run", side_effect=fake_run),
                patch.object(runbatch.time, "strftime", return_value=FAKE_DATE),
                patch.object(runbatch.time, "sleep", return_value=None),
                patch.dict(runbatch.os.environ, {"USER": FAKE_USER}),
                patch.object(sys, "argv", ["runbatch.py"] + argv),
                redirect_stdout(io.StringIO()),
            ):
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
        self.assertIn(
            name, reference, f"no reference block named {name!r} in {REFERENCE_FILE}"
        )
        self.assertEqual(
            command, reference[name]["command"], f"sbatch command mismatch for {name}"
        )
        self.assertEqual(
            script, reference[name]["script"], f"sbatch script mismatch for {name}"
        )


class TestBasicPipefile(RunbatchCaptureCase):
    def test_two_real_lines_produce_two_matching_submissions(self):
        calls = self.run_and_capture(
            [
                str(PIPEFILE),
                "--env=main",
                f"--config={CONFIG}",
            ]
        )
        self.assertEqual(len(calls), 2, "comment/blank lines should be skipped")

        command0, script0 = calls[0]
        self.assert_matches_reference("basic_line1_explicit_recipe", command0, script0)

        command1, script1 = calls[1]
        self.assert_matches_reference("basic_line2_default_recipe", command1, script1)


class TestCliFlags(RunbatchCaptureCase):
    def test_custom_cores_mem_and_batch2_queue(self):
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
                "-c",
                "4",
                "-m",
                "32",
                "-b",
            ]
        )
        self.assertEqual(len(calls), 1)
        command, script = calls[0]
        self.assert_matches_reference("custom_cores_mem_batch2", command, script)

    def test_extra_args_and_different_env_are_forwarded(self):
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=pl2025",
                f"--config={CONFIG}",
                "--extra-arg=--legacy",
                "--extra-arg=--noupdate",
            ]
        )
        self.assertEqual(len(calls), 1)
        command, script = calls[0]
        self.assert_matches_reference("extra_args_and_pl2025_env", command, script)

    def test_custom_outfile_errfile_and_mail_type(self):
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
                "-o",
                "my.out",
                "-e",
                "my.err",
                "-M",
                "FAIL",
            ]
        )
        self.assertEqual(len(calls), 1)
        command, script = calls[0]
        self.assert_matches_reference(
            "custom_outfile_errfile_mail_type", command, script
        )

    def test_custom_partition_flag(self):
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
                "--partition=test_queue",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, script = calls[0]
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
            calls = self.run_and_capture(
                [
                    str(PIPEFILE_ONE_LINE),
                    "--env=main",
                    f"--config={tf_path}",
                    "--profile=debug",
                ]
            )
            self.assertEqual(len(calls), 1)
            _, script = calls[0]
            self.assertIn("#SBATCH --partition=debug", script)
            self.assertIn("#SBATCH --ntasks=4", script)
            self.assertIn("#SBATCH --mem=32G", script)
            self.assertIn("#SBATCH --time=01:00:00", script)
            self.assertNotIn("--profile=debug", script)
        finally:
            Path(tf_path).unlink(missing_ok=True)


class TestErrorPaths(RunbatchCaptureCase):
    def test_unknown_batch_profile_exits_1(self):
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            self.run_and_capture(
                [str(PIPEFILE_ONE_LINE), f"--config={CONFIG}", "--profile=nonexistent"]
            )
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
        with patch.object(
            runbatch.subprocess, "getoutput", return_value="some-other-host"
        ):
            with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
                self.run_and_capture([str(PIPEFILE), f"--config={CONFIG_SUBMIT_HOST}"])
            self.assertEqual(cm.exception.code, 1)


class TestScriptRecord(RunbatchCaptureCase):
    """Verify that a .sbatch record file is written alongside each job's logs."""

    def test_record_file_is_created_next_to_out_log(self):
        _, tmpdir = self._run_in_tmpdir(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
            ]
        )
        try:
            # Default outfile is batch.<name>.out; record should be batch.<name>.sbatch
            expected_name = f"batch.X3_X3_{FAKE_DATE}.sbatch"
            record_path = Path(tmpdir) / expected_name
            self.assertTrue(
                record_path.exists(), f"Expected record file {expected_name} in tmpdir"
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_record_file_contains_sbatch_directives(self):
        _, tmpdir = self._run_in_tmpdir(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
            ]
        )
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
        calls, tmpdir = self._run_in_tmpdir(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
                "-o",
                "/nonexistent/dir/batch.out",
                "-e",
                "/nonexistent/dir/batch.err",
            ]
        )
        try:
            # submission should still have happened despite the bad outfile dir
            self.assertEqual(len(calls), 1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestSlurmDirectives(RunbatchCaptureCase):
    """Verify that advanced Slurm directives render correctly in submitted scripts."""

    def test_advanced_directives_rendered_in_sbatch(self):
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
                "-t",
                "12:00:00",
                "--nodelist=node01",
                "--chdir=/scratch/work",
                "--cpus-per-task=4",
                "--ntasks-per-core=1",
                "--hint=nomultithread",
                "--distribution=cyclic:cyclic",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, script = calls[0]
        self.assertIn("#SBATCH --time=12:00:00", script)
        self.assertIn("#SBATCH --nodelist=node01", script)
        self.assertIn("#SBATCH --chdir=/scratch/work", script)
        self.assertIn("#SBATCH --cpus-per-task=4", script)
        self.assertIn("#SBATCH --ntasks-per-core=1", script)
        self.assertIn("#SBATCH --hint=nomultithread", script)
        self.assertIn("#SBATCH --distribution=cyclic:cyclic", script)
        self.assertIn("#SBATCH --no-requeue", script)
        self.assertIn("#SBATCH --mem=248G", script)

    def test_mem_per_cpu_suppresses_mem(self):
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
                "--mem-per-cpu=30G",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, script = calls[0]
        self.assertIn("#SBATCH --mem-per-cpu=30G", script)
        self.assertNotIn("#SBATCH --mem=", script)

    def test_requeue_flag_omits_no_requeue(self):
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
                "--requeue",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, script = calls[0]
        self.assertNotIn("#SBATCH --no-requeue", script)

    def test_mem_and_mem_per_cpu_cli_mutual_exclusion(self):
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            self.run_and_capture(
                [
                    str(PIPEFILE_ONE_LINE),
                    "--env=main",
                    f"--config={CONFIG}",
                    "-m",
                    "100",
                    "--mem-per-cpu=30G",
                ]
            )
        self.assertEqual(cm.exception.code, 2)

    def test_print_queue_executes_squeue_with_pcasa_format(self):
        with (
            patch.object(runbatch.shutil, "which", return_value="/usr/bin/squeue"),
            patch.object(runbatch.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="  12345 plwg testuser R ..."
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                runbatch.print_queue("testuser")
            mock_run.assert_called_once()
            cmd_args = mock_run.call_args[0][0]
            self.assertEqual(cmd_args[0], "/usr/bin/squeue")
            self.assertIn(
                "--format=%7i %13P %9u %7T %11M %11l %5D %2C %2c/%7m %16R %50j %50Z",
                cmd_args[1],
            )
            self.assertIn("testuser", cmd_args)
            self.assertIn("12345 plwg testuser R", buf.getvalue())


class TestHTCondorDirectives(RunbatchCaptureCase):
    """Test HTCondor batch submission and directive generation."""

    def test_htcondor_submit_generates_correct_directives(self):
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
                "--scheduler=htcondor",
                "-c",
                "4",
                "-m",
                "32",
                "--partition=plwg",
            ]
        )
        self.assertEqual(len(calls), 1)
        cmd, htc_script = calls[0]
        self.assertTrue(cmd.startswith("condor_submit "))
        self.assertIn(
            "requirements = ( ( plwg == True ) && ( HasLustre == True ) && ( NumJobStarts == 0 ) )",
            htc_script,
        )
        self.assertIn("request_cpus = 4", htc_script)
        self.assertIn("request_memory = 32G", htc_script)
        self.assertIn('+partition = "plwg"', htc_script)
        self.assertIn("getenv = true", htc_script)
        self.assertIn(
            "periodic_remove = JobStatus == 1 && NumJobStarts > 0", htc_script
        )

    def test_htcondor_nodelist_adds_target_machine(self):
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
                "--scheduler=htcondor",
                "--nodelist=node01",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, htc_script = calls[0]
        self.assertIn('( TARGET.Machine == "node01" )', htc_script)

    def test_htcondor_requirements_override_none(self):
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
                "--scheduler=htcondor",
                "--requirements=none",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, htc_script = calls[0]
        self.assertIn("requirements = ( NumJobStarts == 0 )", htc_script)
        self.assertNotIn("HasLustre", htc_script)

    def test_htcondor_requirements_override_custom(self):
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
                "--scheduler=htcondor",
                '--requirements=( TARGET.Arch == "X86_64" )',
            ]
        )
        self.assertEqual(len(calls), 1)
        _, htc_script = calls[0]
        self.assertIn('requirements = ( ( TARGET.Arch == "X86_64" ) )', htc_script)

    def test_htcondor_mem_per_cpu_scales_by_cores(self):
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
                "--scheduler=htcondor",
                "-c",
                "4",
                "--mem-per-cpu=8G",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, htc_script = calls[0]
        self.assertIn("request_cpus = 4", htc_script)
        self.assertIn("request_memory = 32G", htc_script)

    def test_print_queue_executes_condor_q(self):
        with (
            patch.object(runbatch.shutil, "which", return_value="/usr/bin/condor_q"),
            patch.object(runbatch.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="  123.0  testuser  RUNNING ..."
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                runbatch.print_queue("testuser", scheduler="htcondor")
            mock_run.assert_called_once()
            cmd_args = mock_run.call_args[0][0]
            self.assertEqual(cmd_args, ["/usr/bin/condor_q", "testuser"])
            self.assertIn("123.0  testuser  RUNNING", buf.getvalue())

        with (
            patch.object(runbatch.shutil, "which", return_value="/usr/bin/condor_q"),
            patch.object(runbatch.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="  123.0  testuser  RUNNING ..."
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                runbatch.print_queue(
                    "testuser",
                    scheduler="htcondor",
                    pool="cm.example.org",
                    schedd="schedd.example.org",
                )
            mock_run.assert_called_once()
            cmd_args = mock_run.call_args[0][0]
            self.assertEqual(
                cmd_args,
                [
                    "/usr/bin/condor_q",
                    "testuser",
                    "-pool",
                    "cm.example.org",
                    "-name",
                    "schedd.example.org",
                ],
            )

    def test_htcondor_remote_submission_flags(self):
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={CONFIG}",
                "--scheduler=htcondor",
                "-c",
                "4",
                "--pool=cm.example.org",
                "--schedd=schedd.example.org",
                "--spool",
            ]
        )
        self.assertEqual(len(calls), 1)
        cmd, htc_script = calls[0]
        self.assertTrue(cmd.startswith("condor_submit "))
        self.assertIn("-pool cm.example.org", cmd)
        self.assertIn("-name schedd.example.org", cmd)
        self.assertIn("-spool", cmd)
        self.assertIn("should_transfer_files = YES", htc_script)
        self.assertIn("when_to_transfer_output = ON_EXIT", htc_script)

    def test_htcondor_explicit_transfer_directives(self):
        from calibpipe.batch import build_htcondor_script

        htc_body, _ = build_htcondor_script(
            "calibpipe run -p=transfer_prof",
            partition="batch",
            cores=4,
            mem="16G",
            job_name="test_transfer_job",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
            wrapper_script_path="/tmp/test.sh",
            script_record_path="/tmp/test.htc",
            should_transfer_files="YES",
            when_to_transfer_output="ON_EXIT",
        )
        self.assertIn("should_transfer_files = YES", htc_body)
        self.assertIn("when_to_transfer_output = ON_EXIT", htc_body)

        # When spool=False and should_transfer_files is None, transfer directives are omitted
        htc_body_default, _ = build_htcondor_script(
            "calibpipe run -p=default_prof",
            partition="batch",
            cores=4,
            mem="16G",
            job_name="test_default_job",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
            wrapper_script_path="/tmp/test.sh",
            script_record_path="/tmp/test.htc",
        )
        self.assertNotIn("should_transfer_files", htc_body_default)
        self.assertNotIn("when_to_transfer_output", htc_body_default)


class TestBatchProfiles(RunbatchCaptureCase):
    """Test named profile resolution in batch submission."""

    def test_batch_profile_sets_scheduler_and_directives(self):
        from calibpipe.config import ProfileConfig

        cfg = runbatch.envconfig.load_merged_config(cli_arg=str(CONFIG))
        cfg.profiles["htc_profile"] = ProfileConfig(
            scheduler="htcondor",
            cores=12,
            mem=64,
            queue="batch",
        )
        with patch("calibpipe.batch.envconfig.load_merged_config", return_value=cfg):
            calls = self.run_and_capture(
                [
                    str(PIPEFILE_ONE_LINE),
                    "--env=main",
                    f"--config={CONFIG}",
                    "--profile=htc_profile",
                ]
            )

            self.assertEqual(len(calls), 1)
            cmd, htc_script = calls[0]
            self.assertTrue(cmd.startswith("condor_submit "))
            self.assertIn("request_cpus = 12", htc_script)
            self.assertIn("request_memory = 64G", htc_script)
            self.assertIn('+partition = "batch"', htc_script)

    def test_profile_direct_submission_without_pipefile(self):
        from calibpipe.config import ProfileConfig

        cfg = runbatch.envconfig.load_merged_config(cli_arg=str(CONFIG))
        cfg.profiles["test_prof"] = ProfileConfig(
            cores=4,
            mem=32,
            queue="test_partition",
        )
        with patch("calibpipe.batch.envconfig.load_merged_config", return_value=cfg):
            calls = self.run_and_capture(
                [
                    f"--config={CONFIG}",
                    "-p",
                    "test_prof",
                ]
            )
            self.assertEqual(len(calls), 1)
            cmd, script = calls[0]
            self.assertTrue(cmd.startswith("sbatch "))
            self.assertIn("#SBATCH --partition=test_partition", script)
            self.assertIn("#SBATCH --ntasks=4", script)
            self.assertIn("#SBATCH --mem=32G", script)
            self.assertIn("#SBATCH --job-name=test_prof_", script)
            self.assertIn("calibpipe run -p=test_prof", script)

    def test_batch_profile_auto_chdir_emits_directive_and_creates_dir(self):
        from calibpipe.config import ProfileConfig

        with tempfile.TemporaryDirectory() as td:
            target_dir = Path(td) / "new_project_run" / "working"
            self.assertFalse(target_dir.parent.exists())

            cfg = runbatch.envconfig.load_merged_config(cli_arg=str(CONFIG))
            cfg.profiles["auto_chdir_prof"] = ProfileConfig(
                cores=4,
                mem=32,
                queue="test_partition",
                workdir=str(target_dir),
            )
            with patch("calibpipe.batch.envconfig.load_merged_config", return_value=cfg):
                calls = self.run_and_capture(
                    [
                        f"--config={CONFIG}",
                        "-p",
                        "auto_chdir_prof",
                    ]
                )
                self.assertEqual(len(calls), 1)
                cmd, script = calls[0]
                self.assertTrue(cmd.startswith("sbatch "))
                expected_project_root = str(target_dir.parent)
                self.assertIn(f"#SBATCH --chdir={expected_project_root}", script)
                # Ensure the directory was pre-created before submission
                self.assertTrue(target_dir.parent.is_dir())

    def test_batch_dry_run_skips_submission(self):
        buf = io.StringIO()
        tmpdir = tempfile.mkdtemp()
        orig_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            with (
                patch.object(runbatch, "CALIBPIPEIF", FAKE_CALIBPIPEIF),
                patch.object(runbatch.subprocess, "run") as mock_run,
                patch.object(runbatch.time, "strftime", return_value=FAKE_DATE),
                patch.dict(runbatch.os.environ, {"USER": FAKE_USER}),
                patch.object(
                    sys,
                    "argv",
                    [
                        "runbatch.py",
                        str(PIPEFILE_ONE_LINE),
                        "--env=main",
                        f"--config={CONFIG}",
                        "--dry-run",
                    ],
                ),
                redirect_stdout(buf),
            ):
                runbatch.main()
                mock_run.assert_not_called()
                out = buf.getvalue()
                self.assertIn("[DRY RUN] Would execute: sbatch", out)
                self.assertIn("script saved →", out)
        finally:
            os.chdir(orig_cwd)
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestBatchLogdir(RunbatchCaptureCase):
    """Test log directory consolidation for batch jobs."""

    def test_cli_logdir_routes_artifacts_and_exports_batch_env(self):
        tmp_logdir = tempfile.mkdtemp()
        try:
            calls, tmpdir = self._run_in_tmpdir(
                [
                    str(PIPEFILE_ONE_LINE),
                    "--env=main",
                    f"--config={CONFIG}",
                    f"--logdir={tmp_logdir}",
                ]
            )
            self.assertEqual(len(calls), 1)
            _, script = calls[0]

            expected_sbatch = Path(tmp_logdir) / f"batch.X3_X3_{FAKE_DATE}.sbatch"
            self.assertTrue(
                expected_sbatch.exists(), f"Expected {expected_sbatch} to exist"
            )

            # Verify submit dir remained clean
            submit_dir_sbatch = Path(tmpdir) / f"batch.X3_X3_{FAKE_DATE}.sbatch"
            self.assertFalse(
                submit_dir_sbatch.exists(),
                "Submit dir should not contain .sbatch record",
            )

            # Directives and exports should point to the consolidated logdir
            expected_out = str(Path(tmp_logdir) / f"batch.X3_X3_{FAKE_DATE}.out")
            expected_err = str(Path(tmp_logdir) / f"batch.X3_X3_{FAKE_DATE}.err")
            self.assertIn(f"#SBATCH --output={expected_out}", script)
            self.assertIn(f"#SBATCH --error={expected_err}", script)
            self.assertIn(f"export CALIBPIPE_BATCH_OUTFILE={expected_out}", script)
            self.assertIn(f"export CALIBPIPE_BATCH_ERRFILE={expected_err}", script)
            self.assertIn(f"export CALIBPIPE_BATCH_SCRIPT={expected_sbatch}", script)
        finally:
            shutil.rmtree(tmp_logdir, ignore_errors=True)

    def test_config_logdir_user_placeholder_expanded(self):
        tmp_base = tempfile.mkdtemp()
        cfg_path = Path(tmp_base) / "config.toml"
        logdir_pattern = f"{tmp_base}/logs/{{user}}"
        cfg_path.write_text(f"""
default_env = "main"
[paths]
scipipe_logdir = "{logdir_pattern}"
[envs.main]
casa_root = "/fake/casa/main"
""")
        try:
            calls, _ = self._run_in_tmpdir(
                [
                    str(PIPEFILE_ONE_LINE),
                    "--env=main",
                    f"--config={cfg_path}",
                ]
            )
            self.assertEqual(len(calls), 1)
            _, script = calls[0]

            expected_dir = Path(tmp_base) / "logs" / FAKE_USER
            expected_sbatch = expected_dir / f"batch.X3_X3_{FAKE_DATE}.sbatch"
            self.assertTrue(expected_sbatch.exists())
            self.assertIn(str(expected_dir), script)
        finally:
            shutil.rmtree(tmp_base, ignore_errors=True)

    def test_build_script_exports_uv_no_sync_when_uv_in_pipejob(self):
        from calibpipe.batch import build_htcondor_script, build_sbatch_script

        # 1. Slurm with uv in pipejob
        slurm_script = build_sbatch_script(
            "uv run calibpipe run -p=my_prof",
            partition="batch",
            node="1",
            cores=8,
            mem="32G",
            job_name="test_uv_job",
            mail_user="test",
            mail_type="ALL",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
            script_record_path="/tmp/test.sbatch",
        )
        self.assertIn("export UV_NO_SYNC=1", slurm_script)

        # 2. Slurm without uv in pipejob
        slurm_script_plain = build_sbatch_script(
            "/usr/bin/python script.py",
            partition="batch",
            node="1",
            cores=8,
            mem="32G",
            job_name="test_plain_job",
            mail_user="test",
            mail_type="ALL",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
            script_record_path="/tmp/test.sbatch",
        )
        self.assertNotIn("export UV_NO_SYNC=1", slurm_script_plain)

        # 3. HTCondor with uv in pipejob
        _, htc_wrapper = build_htcondor_script(
            "uv run calibpipe run -p=my_prof",
            partition="batch",
            cores=8,
            mem="32G",
            job_name="test_htc_job",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
            wrapper_script_path="/tmp/test.sh",
            script_record_path="/tmp/test.htc",
        )
        self.assertIn("export UV_NO_SYNC=1", htc_wrapper)

    def test_build_script_exports_pixi_flags(self):
        from calibpipe.batch import build_htcondor_script, build_sbatch_script

        # 1. Slurm with pixi in pipejob
        slurm_script = build_sbatch_script(
            "pixi run calibpipe run -p=my_prof",
            partition="batch",
            node="1",
            cores=8,
            mem="32G",
            job_name="test_pixi_job",
            mail_user="test",
            mail_type="ALL",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
            script_record_path="/tmp/test.sbatch",
        )
        self.assertIn("export PIXI_FROZEN=true", slurm_script)
        self.assertIn("export PIXI_NO_INSTALL=true", slurm_script)

        # 2. Slurm with is_pixi=True
        slurm_script_flag = build_sbatch_script(
            "calibpipe run -p=my_prof",
            partition="batch",
            node="1",
            cores=8,
            mem="32G",
            job_name="test_pixi_job",
            mail_user="test",
            mail_type="ALL",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
            script_record_path="/tmp/test.sbatch",
            is_pixi=True,
        )
        self.assertIn("export PIXI_FROZEN=true", slurm_script_flag)
        self.assertIn("export PIXI_NO_INSTALL=true", slurm_script_flag)

        # 3. Slurm without pixi
        slurm_script_plain = build_sbatch_script(
            "/usr/bin/python script.py",
            partition="batch",
            node="1",
            cores=8,
            mem="32G",
            job_name="test_plain_job",
            mail_user="test",
            mail_type="ALL",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
            script_record_path="/tmp/test.sbatch",
        )
        self.assertNotIn("export PIXI_FROZEN", slurm_script_plain)
        self.assertNotIn("export PIXI_NO_INSTALL", slurm_script_plain)

        # 4. HTCondor with is_pixi=True
        _, htc_wrapper = build_htcondor_script(
            "calibpipe run -p=my_prof",
            partition="batch",
            cores=8,
            mem="32G",
            job_name="test_htc_job",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
            wrapper_script_path="/tmp/test.sh",
            script_record_path="/tmp/test.htc",
            is_pixi=True,
        )
        self.assertIn("export PIXI_FROZEN=true", htc_wrapper)
        self.assertIn("export PIXI_NO_INSTALL=true", htc_wrapper)

    def test_build_script_exports_thread_environment(self):
        from calibpipe.batch import build_htcondor_script, build_sbatch_script

        # 1. Slurm with cpus_per_task fallback
        slurm_script = build_sbatch_script(
            "calibpipe run -p=my_prof",
            partition="batch",
            node="1",
            cores=8,
            mem="32G",
            cpus_per_task=2,
            job_name="test_thread_job",
            mail_user="test",
            mail_type="ALL",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
            script_record_path="/tmp/test.sbatch",
        )
        self.assertIn("export OMP_NUM_THREADS=2", slurm_script)
        self.assertIn("export OPENBLAS_NUM_THREADS=2", slurm_script)

        # 2. Slurm with explicit omp_num_threads override
        slurm_override = build_sbatch_script(
            "calibpipe run -p=my_prof",
            partition="batch",
            node="1",
            cores=8,
            mem="32G",
            cpus_per_task=4,
            omp_num_threads=1,
            openblas_num_threads=1,
            job_name="test_override_job",
            mail_user="test",
            mail_type="ALL",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
            script_record_path="/tmp/test.sbatch",
        )
        self.assertIn("export OMP_NUM_THREADS=1", slurm_override)
        self.assertIn("export OPENBLAS_NUM_THREADS=1", slurm_override)

        # 3. HTCondor with explicit omp_num_threads
        _, htc_wrapper = build_htcondor_script(
            "calibpipe run -p=my_prof",
            partition="batch",
            cores=8,
            mem="32G",
            omp_num_threads=2,
            openblas_num_threads=2,
            job_name="test_htc_thread_job",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
            wrapper_script_path="/tmp/test.sh",
            script_record_path="/tmp/test.htc",
        )
        self.assertIn("export OMP_NUM_THREADS=2", htc_wrapper)
        self.assertIn("export OPENBLAS_NUM_THREADS=2", htc_wrapper)

    def test_build_htcondor_and_slurm_with_request_gpus(self):
        from calibpipe.batch import build_htcondor_script, build_sbatch_script

        # HTCondor with request_gpus = 2
        htc_body, _ = build_htcondor_script(
            "calibpipe run -p=gpu_prof",
            partition="batch",
            cores=8,
            mem="32G",
            request_gpus=2,
            job_name="test_gpu_job",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
            wrapper_script_path="/tmp/test.sh",
            script_record_path="/tmp/test.htc",
        )
        self.assertIn("request_gpus = 2", htc_body)

        # HTCondor default request_gpus = 0
        htc_body_default, _ = build_htcondor_script(
            "calibpipe run -p=default_prof",
            partition="batch",
            cores=8,
            mem="32G",
            job_name="test_default_job",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
            wrapper_script_path="/tmp/test.sh",
            script_record_path="/tmp/test.htc",
        )
        self.assertIn("request_gpus = 0", htc_body_default)

        # Slurm with request_gpus = 1
        slurm_gpu = build_sbatch_script(
            "calibpipe run -p=gpu_prof",
            partition="gpu",
            node="1",
            cores=8,
            mem="32G",
            request_gpus=1,
            job_name="test_slurm_gpu",
            mail_user="test",
            mail_type="ALL",
            outfile="/tmp/test.out",
            errfile="/tmp/test.err",
        )
        self.assertIn("#SBATCH --gpus=1", slurm_gpu)

    def test_job_name_profile_with_slashes(self):
        """Verify profile names with slashes retain slashes in display_name and are sanitized in file_name."""
        display, file_safe = runbatch.job_name("calibpipe run -p=vlass/cube/7x3", profile_name="vlass/cube/7x3")
        self.assertTrue(display.startswith("vlass/cube/7x3_"))
        self.assertTrue(file_safe.startswith("vlass_cube_7x3_"))
        self.assertNotIn("/", file_safe)

    def test_job_name_profile_containing_mous_substring(self):
        """Verify profile names containing 'mous' (e.g. e2e_mous_calimage) are not misidentified as --mous."""
        display, file_safe = runbatch.job_name(
            "calibpipe run -p=e2e_mous_calimage", profile_name="e2e_mous_calimage"
        )
        self.assertTrue(display.startswith("e2e_mous_calimage_"))
        self.assertTrue(file_safe.startswith("e2e_mous_calimage_"))


if __name__ == "__main__":
    unittest.main()
