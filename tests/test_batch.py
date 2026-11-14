"""Tests for calibpipe.batch (migrated from test_runbatch.py)."""

from __future__ import annotations

import io
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from calibpipe import batch as runbatch
from calibpipe import cli
from calibpipe.config import ProfileConfig, RunRequest, load_config, resolve_run_options

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CONFIG = FIXTURES / "config.toml"
CONFIG_SUBMIT_HOST = FIXTURES / "config_submit_host.toml"
PIPEFILE = FIXTURES / "pipefile.txt"
PIPEFILE_ONE_LINE = FIXTURES / "pipefile_one_line.txt"

FAKE_USER = "testuser"
FAKE_DATE = "20260101-000000"


def _normalize(text, sbatch_script_path):
    text = text.replace(sbatch_script_path, "<SBATCH_SCRIPT>")
    text = text.replace(str(CONFIG), "<CONFIG>")
    return text


def _extract_calibpipe_tokens(script: str) -> list[str]:
    """Extract argv tokens starting at `run` from a generated batch script."""
    lines = script.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if "calibpipe" in stripped and (
            " run " in stripped or stripped.endswith((" run \\", " run"))
        ):
            tokens = [tok for tok in shlex.split("\n".join(lines[idx:])) if tok != "\n"]
            run_idx = tokens.index("run")
            return tokens[run_idx:]
    raise AssertionError("calibpipe run command not found in batch script")


def _extract_run_state(
    script: str, snapshots: dict[str, str] | None = None
) -> dict:
    """Parse `calibpipe run` flags and its frozen `batch.<ts>.toml` snapshot."""
    tokens = _extract_calibpipe_tokens(script)
    args = cli.build_parser().parse_args(tokens)
    req = cli.request(args, RunRequest)
    assert req.config_only, f"Expected --config-only in batch script:\n{script}"
    assert req.config, f"Expected --config in batch script:\n{script}"

    cfg_path = Path(req.config)
    if cfg_path.is_file():
        cfg = load_config(cfg_path)
    elif snapshots and req.config in snapshots:
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as tf:
            tf.write(snapshots[req.config])
            tmp_cfg = Path(tf.name)
        try:
            cfg = load_config(tmp_cfg)
        finally:
            tmp_cfg.unlink(missing_ok=True)
    else:
        raise AssertionError(f"Snapshot config missing: {req.config}")

    resolved_run = resolve_run_options(cfg, req)
    return {
        "tokens": tokens,
        "request": req,
        "config": cfg,
        "run": resolved_run,
    }


class RunbatchCaptureCase(unittest.TestCase):
    def _write_temp_config(self, toml_content: str) -> str:
        """Write a temporary TOML config file and schedule its removal."""
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as tf:
            tf.write(toml_content)
        self.addCleanup(lambda p=tf.name: Path(p).unlink(missing_ok=True))
        return tf.name

    def _run_in_tmpdir(self, argv):
        """Run batch.main() from a fresh tmpdir; return (calls, tmpdir_path).

        The tmpdir is returned so callers can inspect files written there
        (e.g. .sbatch record files).  The directory persists until the caller
        cleans it up.
        """
        calls = []
        self._captured_snapshots = {}

        def fake_run(cmd, check=True, **kwargs):
            if cmd[0] not in ("sbatch", "condor_submit"):
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="fake queue output"
                )
            sub_path = cmd[-1]
            script_content = Path(sub_path).read_text()
            for tok in shlex.split(script_content):
                if tok.startswith("--config="):
                    snap = Path(tok.split("=", 1)[1])
                    if snap.is_file():
                        self._captured_snapshots[str(snap)] = snap.read_text()
            normalized_cmd = " ".join(_normalize(part, sub_path) for part in cmd)
            calls.append((normalized_cmd, _normalize(script_content, sub_path)))
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        tmpdir = tempfile.mkdtemp()
        orig_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            with (
                patch.object(subprocess, "run", side_effect=fake_run),
                patch.object(runbatch.time, "strftime", return_value=FAKE_DATE),
                patch.object(runbatch.time, "sleep", return_value=None),
                patch.dict(
                    runbatch.os.environ,
                    {"USER": FAKE_USER, "CALIBPIPE_NO_SITE_CONFIG": "1"},
                ),
                patch.object(sys, "argv", ["runbatch.py"] + argv),
                redirect_stdout(io.StringIO()),
            ):
                runbatch.main()
            for snap in Path(tmpdir).rglob("batch.*.toml"):
                self._captured_snapshots.setdefault(str(snap), snap.read_text())
        finally:
            os.chdir(orig_cwd)

        return calls, tmpdir

    def _extract_run_state(self, script: str) -> dict:
        return _extract_run_state(script, getattr(self, "_captured_snapshots", None))

    def run_and_capture(self, argv):
        """Convenience wrapper that discards the tmpdir after capture."""
        calls, tmpdir = self._run_in_tmpdir(argv)
        shutil.rmtree(tmpdir, ignore_errors=True)
        return calls


class TestBasicPipefile(RunbatchCaptureCase):
    def test_two_real_lines_produce_two_matching_submissions(self):
        cfg = runbatch.envconfig.load_merged_config(str(CONFIG), include_site=False)
        calls = self.run_and_capture(
            [
                str(PIPEFILE),
                "--env=main",
                f"--config={CONFIG}",
            ]
        )
        self.assertEqual(len(calls), 2, "comment/blank lines should be skipped")

        command0, script0 = calls[0]
        self.assertEqual(command0, "sbatch <SBATCH_SCRIPT>")
        self.assertIn("#SBATCH --partition=plwg", script0)
        self.assertIn("#SBATCH --nodes=1", script0)
        self.assertIn("#SBATCH --ntasks=8", script0)
        self.assertIn("#SBATCH --mem=248G", script0)
        self.assertIn(f"#SBATCH --job-name=X1_X1_{FAKE_DATE}", script0)
        self.assertIn("#SBATCH --mail-user=testuser", script0)
        self.assertIn("#SBATCH --mail-type=ALL", script0)
        self.assertIn("#SBATCH --no-requeue", script0)
        self.assertIn("calibpipe", script0)
        self.assertIn("--config-only", script0)
        state0 = self._extract_run_state(script0)
        self.assertEqual(state0["run"].mous, "uid___A001_X1_X1")
        self.assertEqual(state0["run"].recipe, "calimage")
        self.assertEqual(state0["run"].env_name, "main")
        self.assertEqual(state0["config"].envs["main"].casa_root, "/fake/casa/main")

        command1, script1 = calls[1]
        self.assertEqual(command1, "sbatch <SBATCH_SCRIPT>")
        self.assertIn(f"#SBATCH --job-name=X2_X2_{FAKE_DATE}", script1)
        state1 = self._extract_run_state(script1)
        self.assertEqual(state1["run"].mous, "uid___A001_X2_X2")
        self.assertEqual(state1["run"].recipe, cfg.run.recipe)
        self.assertEqual(state1["run"].env_name, "main")
        self.assertEqual(state0["request"].config, state1["request"].config)


class TestCliFlags(RunbatchCaptureCase):
    def test_custom_cores_mem_and_partition_from_config(self):
        cfg_path = self._write_temp_config("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa/main"
[batch]
partition = "plwg2"
cores = 4
mem = 32
""")
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={cfg_path}",
            ]
        )
        self.assertEqual(len(calls), 1)
        command, script = calls[0]
        self.assertEqual(command, "sbatch <SBATCH_SCRIPT>")
        self.assertIn("#SBATCH --partition=plwg2", script)
        self.assertIn("#SBATCH --ntasks=4", script)
        self.assertIn("#SBATCH --mem=32G", script)

    def test_run_options_and_different_env_are_forwarded(self):
        cfg_path = self._write_temp_config("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa/main"
[envs.pl2025]
casa_root = "/fake/casa/pl2025"
[run]
backup = false
nprocs = 2
""")
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=pl2025",
                f"--config={cfg_path}",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, script = calls[0]
        state = self._extract_run_state(script)
        self.assertEqual(state["run"].env_name, "pl2025")
        self.assertFalse(state["run"].backup)
        self.assertEqual(state["run"].nprocs, 2)
        self.assertEqual(state["config"].envs["pl2025"].casa_root, "/fake/casa/pl2025")

    def test_custom_outfile_errfile_and_mail_type(self):
        cfg_path = self._write_temp_config("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa/main"
[batch]
outfile = "my.out"
errfile = "my.err"
mail_type = "FAIL"
""")
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={cfg_path}",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, script = calls[0]
        self.assertIn("#SBATCH --output=my.out", script)
        self.assertIn("#SBATCH --error=my.err", script)
        self.assertIn("#SBATCH --mail-type=FAIL", script)
        self.assertIn("export CALIBPIPE_BATCH_OUTFILE=my.out", script)
        self.assertIn("export CALIBPIPE_BATCH_ERRFILE=my.err", script)

    def test_batch_profile_flag(self):
        tf_path = self._write_temp_config("""
default_env = "main"
[paths]
scipipe_rootdir = "/fake/root/{user}"
scipipe_logdir = "/fake/logs/{user}"
[envs.main]
casa_root = "/fake/casa/main"

[batch]
partition = "plwg"
cores = 8
mem = 248

[profiles.debug]
partition = "debug"
cores = 4
mem = 32
walltime = "01:00:00"
""")
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
        self.assertIn("-p debug", script)
        state = self._extract_run_state(script)
        self.assertEqual(state["run"].profile, "debug")


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
            subprocess, "getoutput", return_value="some-other-host"
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
        cfg_path = self._write_temp_config("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa/main"
[batch]
outfile = "/nonexistent/dir/batch.out"
errfile = "/nonexistent/dir/batch.err"
""")
        calls, tmpdir = self._run_in_tmpdir(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={cfg_path}",
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
        cfg_path = self._write_temp_config("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa/main"
[batch]
walltime = "12:00:00"
nodelist = "node01"
chdir = "/scratch/work"
cpus_per_task = 4
ntasks_per_core = 1
hint = "nomultithread"
distribution = "cyclic:cyclic"
""")
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={cfg_path}",
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
        cfg_path = self._write_temp_config("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa/main"
[batch]
mem_per_cpu = "30G"
""")
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={cfg_path}",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, script = calls[0]
        self.assertIn("#SBATCH --mem-per-cpu=30G", script)
        self.assertNotIn("#SBATCH --mem=", script)

    def test_requeue_flag_omits_no_requeue(self):
        cfg_path = self._write_temp_config("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa/main"
[batch]
no_requeue = false
""")
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={cfg_path}",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, script = calls[0]
        self.assertNotIn("#SBATCH --no-requeue", script)

    def test_print_queue_executes_squeue_with_pcasa_format(self):
        with (
            patch.object(shutil, "which", return_value="/usr/bin/squeue"),
            patch.object(subprocess, "run") as mock_run,
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
                "--format=%7i %13P %9u %7T %11M %11l %5D %2C %2c/%7m %16R %80j",
                cmd_args[1],
            )
            self.assertIn("testuser", cmd_args)
            self.assertIn("12345 plwg testuser R", buf.getvalue())


class TestHTCondorDirectives(RunbatchCaptureCase):
    """Test HTCondor batch submission and directive generation."""

    def test_htcondor_submit_generates_correct_directives(self):
        cfg_path = self._write_temp_config("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa/main"
[batch]
scheduler = "htcondor"
cores = 4
mem = 32
partition = "plwg"
""")
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={cfg_path}",
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
        cfg_path = self._write_temp_config("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa/main"
[batch]
scheduler = "htcondor"
nodelist = "node01"
""")
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={cfg_path}",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, htc_script = calls[0]
        self.assertIn('( TARGET.Machine == "node01" )', htc_script)

    def test_htcondor_requirements_override_none(self):
        cfg_path = self._write_temp_config("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa/main"
[batch]
scheduler = "htcondor"
requirements = "none"
""")
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={cfg_path}",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, htc_script = calls[0]
        self.assertIn("requirements = ( NumJobStarts == 0 )", htc_script)
        self.assertNotIn("HasLustre", htc_script)

    def test_htcondor_requirements_override_custom(self):
        cfg_path = self._write_temp_config("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa/main"
[batch]
scheduler = "htcondor"
requirements = '( TARGET.Arch == "X86_64" )'
""")
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={cfg_path}",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, htc_script = calls[0]
        self.assertIn('requirements = ( ( TARGET.Arch == "X86_64" ) )', htc_script)

    def test_htcondor_mem_per_cpu_scales_by_cores(self):
        cfg_path = self._write_temp_config("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa/main"
[batch]
scheduler = "htcondor"
cores = 4
mem_per_cpu = "8G"
""")
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={cfg_path}",
            ]
        )
        self.assertEqual(len(calls), 1)
        _, htc_script = calls[0]
        self.assertIn("request_cpus = 4", htc_script)
        self.assertIn("request_memory = 32G", htc_script)

    def test_print_queue_executes_condor_q(self):
        with (
            patch.object(shutil, "which", return_value="/usr/bin/condor_q"),
            patch.object(subprocess, "run") as mock_run,
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
            patch.object(shutil, "which", return_value="/usr/bin/condor_q"),
            patch.object(subprocess, "run") as mock_run,
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
        cfg_path = self._write_temp_config("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa/main"
[batch]
scheduler = "htcondor"
cores = 4
pool = "cm.example.org"
schedd = "schedd.example.org"
spool = true
""")
        calls = self.run_and_capture(
            [
                str(PIPEFILE_ONE_LINE),
                "--env=main",
                f"--config={cfg_path}",
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
        cfg = runbatch.envconfig.load_merged_config(cli_arg=str(CONFIG), include_site=False)
        cfg.profiles["htc_profile"] = ProfileConfig(
            scheduler="htcondor",
            cores=12,
            mem=64,
            partition="batch",
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
        cfg = runbatch.envconfig.load_merged_config(cli_arg=str(CONFIG), include_site=False)
        cfg.profiles["test_prof"] = ProfileConfig(
            cores=4,
            mem=32,
            partition="test_partition",
            cmd="print(1)",
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
            self.assertIn("--config-only", script)
            state = self._extract_run_state(script)
            self.assertEqual(state["run"].profile, "test_prof")
            self.assertEqual(state["run"].cmd, "print(1)")

    def test_batch_profile_auto_chdir_emits_directive_and_creates_dir(self):
        with tempfile.TemporaryDirectory() as td:
            target_dir = Path(td) / "new_project_run" / "working"
            self.assertFalse(target_dir.parent.exists())

            cfg = runbatch.envconfig.load_merged_config(cli_arg=str(CONFIG), include_site=False)
            cfg.profiles["auto_chdir_prof"] = ProfileConfig(
                cores=4,
                mem=32,
                partition="test_partition",
                workdir=str(target_dir),
                cmd="print(1)",
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

    def test_batch_profile_backup_derives_parent_chdir_for_non_working_dir(self):
        with tempfile.TemporaryDirectory() as td:
            target_dir = Path(td) / "regression" / "casa_cov_test"
            cfg = runbatch.envconfig.load_merged_config(cli_arg=str(CONFIG), include_site=False)
            cfg.profiles["reg_backup_prof"] = ProfileConfig(
                cores=4,
                mem=32,
                partition="test_partition",
                workdir=str(target_dir),
                backup=True,
                cmd="print(1)",
            )
            with patch("calibpipe.batch.envconfig.load_merged_config", return_value=cfg):
                calls = self.run_and_capture(
                    [
                        f"--config={CONFIG}",
                        "-p",
                        "reg_backup_prof",
                    ]
                )
                self.assertEqual(len(calls), 1)
                cmd, script = calls[0]
                self.assertTrue(cmd.startswith("sbatch "))
                expected_parent = str(target_dir.parent)
                self.assertIn(f"#SBATCH --chdir={expected_parent}", script)

    def test_batch_dry_run_skips_submission(self):
        buf = io.StringIO()
        tmpdir = tempfile.mkdtemp()
        orig_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            with (
                patch.object(subprocess, "run") as mock_run,
                patch.object(runbatch.time, "strftime", return_value=FAKE_DATE),
                patch.dict(
                    runbatch.os.environ,
                    {"USER": FAKE_USER, "CALIBPIPE_NO_SITE_CONFIG": "1"},
                ),
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
            cfg_path = self._write_temp_config(f"""
default_env = "main"
[envs.main]
casa_root = "/fake/casa/main"
[batch]
logdir = "{tmp_logdir}"
""")
            calls, tmpdir = self._run_in_tmpdir(
                [
                    str(PIPEFILE_ONE_LINE),
                    "--env=main",
                    f"--config={cfg_path}",
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

    def test_batch_submit_test_runfile(self):
        """Verify submitting a test runfile dispatches discrete jobs with pytest commands."""
        with tempfile.NamedTemporaryFile("w", suffix=".runfile", delete=False) as f:
            f.write(
                "uid___A002_X1\ttests/regression/fast/test_alma.py::test_uid___A002_X1__regression\n"
                "uid___A002_X2\ttests/regression/fast/test_alma.py::test_uid___A002_X2__regression\n"
            )
            runfile_path = f.name

        try:
            calls = self.run_and_capture(["--config", str(CONFIG), runfile_path])
            self.assertEqual(len(calls), 2)
            _, script1 = calls[0]
            state1 = self._extract_run_state(script1)
            self.assertIn("pytest.main", state1["run"].cmd)
            self.assertIn("uid___A002_X1", state1["run"].cmd)
            self.assertIn("uid___A002_X1", state1["run"].workdir)
            self.assertTrue(state1["request"].isolate_root)
            self.assertIn(
                "#SBATCH --job-name=tests_regression_fast_test_alma.py__test_uid___A002_X1__regression_20260101-000000",
                script1,
            )

            _, script2 = calls[1]
            state2 = self._extract_run_state(script2)
            self.assertIn("pytest.main", state2["run"].cmd)
            self.assertIn("uid___A002_X2", state2["run"].cmd)
            self.assertTrue(state2["request"].isolate_root)
            self.assertIn(
                "#SBATCH --job-name=tests_regression_fast_test_alma.py__test_uid___A002_X2__regression_20260101-000000",
                script2,
            )
        finally:
            os.unlink(runfile_path)

    def test_batch_submit_collect_flag(self):
        """Verify collect = true queries test nodes and dispatches an array of batch jobs."""
        cfg_path = self._write_temp_config(
            'default_env = "main"\n'
            "[envs.main]\n"
            'casa_root = "/fake/casa/main"\n'
            'heuristics_dir = "/fake/pipeline"\n'
            "[profiles.regression_fast]\n"
            'env = "main"\n'
            "collect = true\n"
            'test_target = "tests/regression/fast"\n'
            'workdir = "/tmp/fake_workdir"\n'
        )

        fake_nodes = [
            "tests/regression/fast/alma.py::test_uid___A002_X100__regression",
            "tests/regression/fast/alma.py::test_uid___A002_X200__regression",
        ]
        with patch("calibpipe.collect.collect_tests", return_value=fake_nodes) as mock_collect:
            calls = self.run_and_capture(
                ["--config", cfg_path, "-p", "regression_fast"]
            )
            mock_collect.assert_called_once()
            self.assertEqual(len(calls), 2)
            _, script1 = calls[0]
            state1 = self._extract_run_state(script1)
            self.assertIn("uid___A002_X100", state1["run"].cmd)
            self.assertEqual(state1["run"].workdir, "/tmp/fake_workdir/uid___A002_X100")
            self.assertIn("#SBATCH --job-name=regression_fast/uid___A002_X100_20260101-000000", script1)

            _, script2 = calls[1]
            state2 = self._extract_run_state(script2)
            self.assertIn("uid___A002_X200", state2["run"].cmd)
            self.assertEqual(state2["run"].workdir, "/tmp/fake_workdir/uid___A002_X200")
            self.assertIn("#SBATCH --job-name=regression_fast/uid___A002_X200_20260101-000000", script2)

    def test_job_name_with_test_name_collect_profile_format(self):
        """Verify job_name formats {profilename}/{test_name}_{timestamp}."""
        display, file_safe = runbatch.job_name(
            "calibpipe run", profile_name="regression/casa674", test_name="uid___A002_X1"
        )
        self.assertTrue(display.startswith("regression/casa674/uid___A002_X1_"))
        self.assertTrue(file_safe.startswith("regression_casa674_uid___A002_X1_"))
        self.assertNotIn("/", file_safe)

        # Without profile
        display_no_prof, file_safe_no_prof = runbatch.job_name("calibpipe run", test_name="uid___A002_X1")
        self.assertTrue(display_no_prof.startswith("uid___A002_X1_"))
        self.assertTrue(file_safe_no_prof.startswith("uid___A002_X1_"))
        self.assertNotIn("/", file_safe_no_prof)

    def test_build_sbatch_script_with_dependency(self):
        """Verify build_sbatch_script emits --dependency directive."""
        from calibpipe.batch import build_sbatch_script
        script = build_sbatch_script(
            "calibpipe report /tmp/workdir",
            node="1",
            cores=1,
            mem="8G",
            job_name="report_job",
            mail_user="user@example.com",
            mail_type="NONE",
            outfile="/tmp/out.log",
            errfile="/tmp/err.log",
            dependency="afterany:9001:9002",
        )
        self.assertIn("#SBATCH --dependency=afterany:9001:9002", script)

    def test_batch_submit_with_report_flag(self):
        """Verify with_report = true submits a dependent wrap-up job following test jobs."""
        cfg_path = self._write_temp_config(
            'default_env = "main"\n'
            "[envs.main]\n"
            'casa_root = "/fake/casa/main"\n'
            'heuristics_dir = "/fake/pipeline"\n'
            "[profiles.regression_fast]\n"
            'env = "main"\n'
            "collect = true\n"
            "with_report = true\n"
            'test_target = "tests/regression/fast"\n'
            'workdir = "/tmp/fake_workdir"\n'
        )

        fake_nodes = [
            "tests/regression/fast/alma.py::test_uid___A002_X100__regression",
            "tests/regression/fast/alma.py::test_uid___A002_X200__regression",
        ]
        with patch("calibpipe.collect.collect_tests", return_value=fake_nodes):
            calls = self.run_and_capture(
                ["--config", cfg_path, "-p", "regression_fast"]
            )
            # 2 test jobs + 1 dependent report job
            self.assertEqual(len(calls), 3)

            # Report job is the 3rd call
            _, report_script = calls[2]
            self.assertIn("calibpipe report /tmp/fake_workdir", report_script)
            self.assertIn("--casa-root=/fake/casa/main", report_script)
            self.assertIn("#SBATCH --dependency=afterany:", report_script)
            self.assertIn("regression_fast/report_", report_script)

    def test_batch_submit_with_throttle_slots(self):
        """Verify max_concurrent chains slot dependencies across submitted batch jobs."""
        cfg_path = self._write_temp_config(
            'default_env = "main"\n'
            "[envs.main]\n"
            'casa_root = "/fake/casa/main"\n'
            'heuristics_dir = "/fake/pipeline"\n'
            "[profiles.regression_throttle]\n"
            'env = "main"\n'
            "collect = true\n"
            "max_concurrent = 2\n"
            'test_target = "tests/regression/fast"\n'
            'workdir = "/tmp/fake_workdir"\n'
        )

        fake_nodes = [
            "tests/regression/fast/alma.py::test_uid___A002_X1__regression",
            "tests/regression/fast/alma.py::test_uid___A002_X2__regression",
            "tests/regression/fast/alma.py::test_uid___A002_X3__regression",
            "tests/regression/fast/alma.py::test_uid___A002_X4__regression",
        ]
        with patch("calibpipe.collect.collect_tests", return_value=fake_nodes):
            calls = self.run_and_capture(
                ["--config", cfg_path, "-p", "regression_throttle"]
            )
            self.assertEqual(len(calls), 4)

            _, script1 = calls[0]
            _, script2 = calls[1]
            _, script3 = calls[2]
            _, script4 = calls[3]

            # Jobs 1 and 2 start immediately (slots 1 and 2)
            self.assertNotIn("#SBATCH --dependency=", script1)
            self.assertNotIn("#SBATCH --dependency=", script2)

            # Job 3 fills slot 1 when Job 1 completes
            self.assertIn("#SBATCH --dependency=afterany:", script3)
            # Job 4 fills slot 2 when Job 2 completes
            self.assertIn("#SBATCH --dependency=afterany:", script4)

        # Test max_concurrent from [batch] table
        cfg_path2 = self._write_temp_config(
            "[batch]\n"
            "max_concurrent = 2\n"
            "[envs.main]\n"
            'casa_root = "/fake/casa/main"\n'
            'heuristics_dir = "/fake/pipeline"\n'
            "[profiles.regression_batch_throttle]\n"
            'env = "main"\n'
            "collect = true\n"
            'test_target = "tests/regression/fast"\n'
            'workdir = "/tmp/fake_workdir"\n'
        )
        with patch("calibpipe.collect.collect_tests", return_value=fake_nodes):
            calls2 = self.run_and_capture(
                ["--config", cfg_path2, "-p", "regression_batch_throttle"]
            )
            self.assertEqual(len(calls2), 4)
            self.assertNotIn("#SBATCH --dependency=", calls2[0][1])
            self.assertNotIn("#SBATCH --dependency=", calls2[1][1])
            self.assertIn("#SBATCH --dependency=afterany:", calls2[2][1])
            self.assertIn("#SBATCH --dependency=afterany:", calls2[3][1])

    def test_batch_submit_forwards_staticobscal_and_oldcont(self):
        """Verify pipefile and single-job profile submissions resolve obscal settings into payloads."""
        test_cfg = (
            'default_env = "main"\n'
            "[envs.main]\n"
            'casa_root = "/fake/casa/main"\n'
            "[paths]\n"
            'obscaldir = "/site/obscal"\n'
            "[run]\n"
            "staticobscal = true\n"
            "[profiles.mous_prof]\n"
            'env = "main"\n'
            'mous = "uid://A001/X1/X1"\n'
            "staticobscal = true\n"
            "oldcont = true\n"
            'obscaldir = "/cli/obscal"\n'
        )
        cfg_path = self._write_temp_config(test_cfg)
        cfg2_path = self._write_temp_config(
            test_cfg
            + (
                "[profiles.obscal_only]\n"
                "staticobscal = false\n"
                "oldcont = true\n"
                'obscaldir = "/cli/obscal"\n'
            )
        )
        with tempfile.NamedTemporaryFile("w", suffix=".run", delete=False) as pf:
            pf.write("uid___A001_X1_X1 calimage\n")
            pipe_path = pf.name
        try:
            calls_pipe = self.run_and_capture(
                [
                    "--config",
                    cfg2_path,
                    "-p",
                    "obscal_only",
                    pipe_path,
                ]
            )
            self.assertEqual(len(calls_pipe), 1)
            _, pipe_script = calls_pipe[0]
            pipe_state = self._extract_run_state(pipe_script)
            self.assertFalse(pipe_state["run"].staticobscal)
            self.assertTrue(pipe_state["run"].oldcont)
            self.assertEqual(pipe_state["run"].obscaldir, "/cli/obscal")
            self.assertEqual(pipe_state["run"].profile, "obscal_only")

            calls_prof = self.run_and_capture(
                [
                    "--config",
                    cfg_path,
                    "-p",
                    "mous_prof",
                ]
            )
            self.assertEqual(len(calls_prof), 1)
            _, prof_script = calls_prof[0]
            prof_state = self._extract_run_state(prof_script)
            self.assertTrue(prof_state["run"].staticobscal)
            self.assertTrue(prof_state["run"].oldcont)
            self.assertEqual(prof_state["run"].obscaldir, "/cli/obscal")
            self.assertEqual(prof_state["run"].profile, "mous_prof")
        finally:
            os.unlink(pipe_path)


if __name__ == "__main__":
    unittest.main()
