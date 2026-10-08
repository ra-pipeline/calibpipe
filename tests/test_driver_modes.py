"""Tests for calibpipe.driver direct execution modes (pcasa-style decoupling from PMR)."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from calibpipe import driver


class DriverModesTestCase(unittest.TestCase):
    """Base fixture for testing alternative driver execution targets."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        self.tmp = Path(self._tmpdir.name)

        self.root = self.tmp / "rootdir"
        self.logdir = self.tmp / "logdir"
        self.config = self.tmp / "config.toml"
        self.root.mkdir()
        self.logdir.mkdir()

        self.config.write_text(f"""
default_env = "main"

[paths]
scipipe_rootdir = "{self.root}"
scipipe_logdir  = "{self.logdir}"

[envs.main]
casa_root = "/fake/casa/main"
""")

    def run_driver(self, args: list[str]) -> tuple[list[tuple[str, str]], int]:
        """Run driver with captured subprocess and logger calls."""
        calls: list[tuple[str, str]] = []

        def fake_getoutput(cmd: str) -> str:
            calls.append(("getoutput", cmd))
            return ""

        def fake_run(cmd: str) -> int:
            calls.append(("casa_run", cmd))
            return 0

        def fake_call(cmd: str, *a: object, **kw: object) -> int:
            calls.append(("subprocess_call", cmd))
            return 0

        with (
            patch.object(driver, "getoutput", side_effect=fake_getoutput),
            patch.object(driver.RunLogger, "run", side_effect=fake_run),
            patch.object(driver.RunLogger, "runquiet", side_effect=fake_run),
            patch.object(driver.subprocess, "call", side_effect=fake_call),
            patch.dict(driver.os.environ, {}, clear=False),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            try:
                driver.main(args)
                exit_code = 0
            except SystemExit as e:
                exit_code = int(e.code) if e.code is not None else 0
            finally:
                self.last_env = dict(driver.os.environ)

        return calls, exit_code


class TestDirectRecipeReducer(DriverModesTestCase):
    """Tests for direct recipe reduction via pipeline.recipereducer.reduce."""

    def test_recipe_reducer_invocation(self) -> None:
        ms_path = self.tmp / "test.ms"
        ms_path.mkdir()

        workdir = self.tmp / "work_reducer"
        workdir.mkdir()

        calls, exit_code = self.run_driver([
            "--vis", str(ms_path),
            "--procedure", "procedure_hifa_calimage.xml",
            f"--config={self.config}",
            f"--workdir={workdir}",
        ])

        self.assertEqual(exit_code, 0)
        # Ensure PMR was never called
        pmr_calls = [c for c in calls if "pipelineMakeRequest" in c[1]]
        self.assertEqual(len(pmr_calls), 0)

        # Check subdirectories and symlink in rawdata
        self.assertTrue((workdir / "working").is_dir())
        self.assertTrue((workdir / "products").is_dir())
        self.assertTrue((workdir / "rawdata").is_dir())
        raw_symlink = workdir / "rawdata" / ms_path.name
        self.assertTrue(raw_symlink.is_symlink())
        self.assertEqual(raw_symlink.resolve(), ms_path.resolve())

        # Check piperun script was generated inside working/
        piperun_file = workdir / "working" / "casa_piperun.py"
        self.assertTrue(piperun_file.is_file())
        script_content = piperun_file.read_text()
        self.assertIn("import pipeline.recipereducer", script_content)
        self.assertIn(f"vis=['{ms_path.resolve()}']", script_content)
        self.assertIn("procedure='procedure_hifa_calimage.xml'", script_content)

        # Check CASA execution call
        casa_runs = [c for c in calls if c[0] == "casa_run"]
        self.assertEqual(len(casa_runs), 1)
        self.assertIn(f"-c {piperun_file.resolve()}", casa_runs[0][1])
        self.assertIn("xvfb-run -a", casa_runs[0][1])

    def test_recipe_reducer_derives_procedure_from_recipe(self) -> None:
        ms_path = self.tmp / "test.ms"
        ms_path.mkdir()

        workdir = self.tmp / "work_reducer_cal"
        workdir.mkdir()

        _, exit_code = self.run_driver([
            "--vis", str(ms_path),
            "--recipe=cal",
            f"--config={self.config}",
            f"--workdir={workdir}",
        ])

        self.assertEqual(exit_code, 0)
        piperun_file = workdir / "working" / "casa_piperun.py"
        self.assertTrue(piperun_file.is_file())
        self.assertIn("procedure='procedure_hifa_cal.xml'", piperun_file.read_text())

    def test_recipe_reducer_with_workdir_ending_in_working(self) -> None:
        ms_path = self.tmp / "test.ms"
        ms_path.mkdir()

        project_dir = self.tmp / "explicit_proj"
        working_dir = project_dir / "working"
        working_dir.mkdir(parents=True)

        _, exit_code = self.run_driver([
            "--vis", str(ms_path),
            "--recipe=cal",
            f"--config={self.config}",
            f"--workdir={working_dir}",
        ])

        self.assertEqual(exit_code, 0)
        # Should not create nested working/working
        self.assertFalse((working_dir / "working").exists())
        self.assertTrue((project_dir / "products").is_dir())
        self.assertTrue((project_dir / "rawdata").is_dir())
        raw_symlink = project_dir / "rawdata" / ms_path.name
        self.assertTrue(raw_symlink.is_symlink())
        self.assertEqual(raw_symlink.resolve(), ms_path.resolve())
        self.assertTrue((working_dir / "casa_piperun.py").is_file())


class TestStandalonePPR(DriverModesTestCase):
    """Tests for standalone PPR execution without PMR."""

    def test_standalone_ppr_alma(self) -> None:
        ppr_file = self.tmp / "PPR_test.xml"
        ppr_file.write_text("<PPR/>")

        workdir = self.tmp / "work_ppr"
        workdir.mkdir()

        calls, exit_code = self.run_driver([
            "--PPR", str(ppr_file),
            f"--config={self.config}",
            f"--workdir={workdir}",
        ])

        self.assertEqual(exit_code, 0)
        # Verify no PMR
        pmr_calls = [c for c in calls if "pipelineMakeRequest" in c[1]]
        self.assertEqual(len(pmr_calls), 0)

        piperun_file = workdir / "casa_piperun.py"
        self.assertTrue(piperun_file.is_file())
        content = piperun_file.read_text()
        self.assertIn("import pipeline.infrastructure.executeppr as eppr", content)
        self.assertIn(f"eppr.executeppr('{ppr_file.resolve()}'", content)

    def test_standalone_ppr_vla(self) -> None:
        ppr_file = self.tmp / "PPR_vla.xml"
        ppr_file.write_text("<PPR/>")

        workdir = self.tmp / "work_vla"
        workdir.mkdir()

        _, exit_code = self.run_driver([
            "--PPR", str(ppr_file),
            "--vla",
            f"--config={self.config}",
            f"--workdir={workdir}",
        ])

        self.assertEqual(exit_code, 0)
        piperun_file = workdir / "casa_piperun.py"
        self.assertTrue(piperun_file.is_file())
        content = piperun_file.read_text()
        self.assertIn("import pipeline.infrastructure.executevlappr as eppr", content)
        self.assertIn(f"eppr.executeppr('{ppr_file.resolve()}'", content)


class TestScriptAndCmdExecution(DriverModesTestCase):
    """Tests for script (--script) and inline code (--cmd) execution."""

    def test_script_execution(self) -> None:
        script_file = self.tmp / "custom_task.py"
        script_file.write_text("print('running custom CASA task')\n")

        workdir = self.tmp / "work_script"
        workdir.mkdir()

        calls, exit_code = self.run_driver([
            "--script", str(script_file),
            f"--config={self.config}",
            f"--workdir={workdir}",
        ])

        self.assertEqual(exit_code, 0)
        casa_runs = [c for c in calls if c[0] == "casa_run"]
        self.assertEqual(len(casa_runs), 1)
        self.assertIn(f"-c {script_file.resolve()}", casa_runs[0][1])

    def test_cmd_execution(self) -> None:
        workdir = self.tmp / "work_cmd"
        workdir.mkdir()

        inline_cmd = "import pytest; pytest.main(['-vv', 'tests/test_fast.py'])"
        calls, exit_code = self.run_driver([
            "--cmd", inline_cmd,
            f"--config={self.config}",
            f"--workdir={workdir}",
        ])

        self.assertEqual(exit_code, 0)
        cmd_file = workdir / "casa_cmd.py"
        self.assertTrue(cmd_file.is_file())
        self.assertIn(inline_cmd, cmd_file.read_text())

        casa_runs = [c for c in calls if c[0] == "casa_run"]
        self.assertEqual(len(casa_runs), 1)
        self.assertIn(f"-c {cmd_file.resolve()}", casa_runs[0][1])


class TestInteractiveMode(DriverModesTestCase):
    """Tests for interactive CASA shell session (-i / --interactive)."""

    def test_interactive_mode(self) -> None:
        workdir = self.tmp / "work_interactive"
        workdir.mkdir()

        calls, exit_code = self.run_driver([
            "-i",
            f"--config={self.config}",
            f"--workdir={workdir}",
        ])

        self.assertEqual(exit_code, 0)
        subproc_calls = [c for c in calls if c[0] == "subprocess_call"]
        self.assertEqual(len(subproc_calls), 1)
        cmd = subproc_calls[0][1]
        self.assertIn("/fake/casa/main/bin/casa", cmd)
        self.assertNotIn("xvfb-run", cmd)
        self.assertNotIn("-c ", cmd)


class TestTargetConflictAndValidation(DriverModesTestCase):
    """Tests for mutual exclusion and missing target handling."""

    def test_conflicting_targets_exits_1(self) -> None:
        _, exit_code = self.run_driver([
            "--mous=uid://A001/X1/X1",
            "--script=/path/to/script.py",
            f"--config={self.config}",
        ])
        self.assertEqual(exit_code, 1)

    def test_missing_targets_exits_1(self) -> None:
        _, exit_code = self.run_driver([
            f"--config={self.config}",
            "--env=main",
        ])
        self.assertEqual(exit_code, 1)


class TestHardwareAndMpiTuning(DriverModesTestCase):
    """Tests for hardware tuning, thread controls, and MPI flags."""

    def test_clean_environment_and_default_threads(self) -> None:
        workdir = self.tmp / "work_hw_def"
        workdir.mkdir()
        script_file = self.tmp / "test.py"
        script_file.touch()

        with patch.dict(os.environ, {"SESSION_MANAGER": "local/host:1234"}, clear=False):
            _, exit_code = self.run_driver([
                "--script", str(script_file),
                f"--config={self.config}",
                f"--workdir={workdir}",
            ])

            self.assertEqual(exit_code, 0)
            self.assertEqual(self.last_env.get("PYTHONNOUSERSITE"), "1")
            self.assertEqual(self.last_env.get("KOKKOS_DISABLE_WARNINGS"), "1")
            self.assertNotIn("SESSION_MANAGER", self.last_env)
            # Default ncores is 8 (> 1), so OMP and OPENBLAS threads default to 1
            self.assertEqual(self.last_env.get("OMP_NUM_THREADS"), "1")
            self.assertEqual(self.last_env.get("OPENBLAS_NUM_THREADS"), "1")

    def test_explicit_thread_overrides(self) -> None:
        workdir = self.tmp / "work_hw_override"
        workdir.mkdir()
        script_file = self.tmp / "test.py"
        script_file.touch()

        _, exit_code = self.run_driver([
            "--script", str(script_file),
            "--omp-num-threads=4",
            "--openblas-num-threads=2",
            f"--config={self.config}",
            f"--workdir={workdir}",
        ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.last_env.get("OMP_NUM_THREADS"), "4")
        self.assertEqual(self.last_env.get("OPENBLAS_NUM_THREADS"), "2")

    def test_mpi_flags_rendering(self) -> None:
        workdir = self.tmp / "work_mpi"
        workdir.mkdir()
        script_file = self.tmp / "test.py"
        script_file.touch()

        calls, exit_code = self.run_driver([
            "--script", str(script_file),
            "--oversubscribe",
            "--bind-to=core",
            "--map-by=socket",
            f"--config={self.config}",
            f"--workdir={workdir}",
        ])

        self.assertEqual(exit_code, 0)
        casa_runs = [c for c in calls if c[0] == "casa_run"]
        self.assertEqual(len(casa_runs), 1)
        cmd = casa_runs[0][1]
        self.assertIn("mpicasa -n 8 --oversubscribe --bind-to core --map-by socket  /fake/casa/main/bin/casa", cmd)


class TestTelemetryAndProfiling(DriverModesTestCase):
    """Tests for piperun telemetry preambles and psrecord profiling."""

    def test_piperun_preamble_mem_frac_and_memstats(self) -> None:
        workdir = self.tmp / "work_telemetry_1"
        workdir.mkdir()
        ms_path = self.tmp / "test.ms"
        ms_path.mkdir()

        _, exit_code = self.run_driver([
            "--vis", str(ms_path),
            "--mem-frac=0.75",
            "--memstats",
            f"--config={self.config}",
            f"--workdir={workdir}",
        ])

        self.assertEqual(exit_code, 0)
        piperun = workdir / "working" / "casa_piperun.py"
        self.assertTrue(piperun.is_file())
        content = piperun.read_text()
        self.assertIn("casalog.setMemoryFraction(0.75)", content)
        self.assertIn("utils.enable_memstats()", content)
        self.assertNotIn("utils.enable_psrecord()", content)

    def test_piperun_preamble_omp_max_threads_and_pl_psrecord(self) -> None:
        workdir = self.tmp / "work_telemetry_2"
        workdir.mkdir()
        ms_path = self.tmp / "test.ms"
        ms_path.mkdir()

        _, exit_code = self.run_driver([
            "--vis", str(ms_path),
            "--omp-max-threads=6",
            "--pl-psrecord",
            f"--config={self.config}",
            f"--workdir={workdir}",
        ])

        self.assertEqual(exit_code, 0)
        piperun = workdir / "working" / "casa_piperun.py"
        self.assertTrue(piperun.is_file())
        content = piperun.read_text()
        self.assertIn("casalog.ompSetNumThreads(6)", content)
        self.assertIn("utils.enable_psrecord()", content)
        self.assertNotIn("utils.enable_memstats()", content)

    def test_psrecord_cli_wrapper_when_available(self) -> None:
        workdir = self.tmp / "work_psrecord"
        workdir.mkdir()

        with patch("calibpipe.driver.shutil.which", return_value="/usr/bin/psrecord"):
            calls, exit_code = self.run_driver([
                "--cmd", "print(123)",
                "--psrecord",
                f"--config={self.config}",
                f"--workdir={workdir}",
            ])

        self.assertEqual(exit_code, 0)
        casa_runs = [c for c in calls if c[0] == "casa_run"]
        self.assertEqual(len(casa_runs), 1)
        cmd = casa_runs[0][1]
        self.assertTrue(cmd.startswith("xvfb-run -a /usr/bin/psrecord "))
        self.assertIn("--log ", cmd)
        self.assertIn(".rec", cmd)
        self.assertIn("--plot ", cmd)
        self.assertIn(".rec.png", cmd)
        self.assertIn("--interval 2", cmd)

    def test_psrecord_cli_fallback_when_missing(self) -> None:
        workdir = self.tmp / "work_no_psrecord"
        workdir.mkdir()

        with patch("calibpipe.driver.shutil.which", return_value=None):
            calls, exit_code = self.run_driver([
                "--cmd", "print(123)",
                "--psrecord",
                f"--config={self.config}",
                f"--workdir={workdir}",
            ])

        self.assertEqual(exit_code, 0)
        casa_runs = [c for c in calls if c[0] == "casa_run"]
        self.assertEqual(len(casa_runs), 1)
        cmd = casa_runs[0][1]
        # Should not use psrecord wrapper if missing
        self.assertNotIn("psrecord ", cmd)
        self.assertNotIn("--include-children", cmd)
        self.assertTrue(cmd.startswith("xvfb-run -a /fake/casa"))


class TestStagingAndDirectoryRotation(DriverModesTestCase):
    """Tests for safe directory rotation and ancillary file staging."""

    def test_rotate_directory_renames_existing_dir(self) -> None:
        from calibpipe.steps.staging import rotate_directory

        target = self.tmp / "test_target"
        target.mkdir()
        (target / "data.txt").write_text("sample data")

        backup_path = rotate_directory(target)
        self.assertIsNotNone(backup_path)
        self.assertFalse(target.exists())
        self.assertTrue(backup_path.exists())
        self.assertIn("_backup_", backup_path.name)
        self.assertEqual((backup_path / "data.txt").read_text(), "sample data")

    def test_rotate_directory_handles_collision(self) -> None:
        from calibpipe.steps.staging import rotate_directory

        target = self.tmp / "test_target_collision"
        target.mkdir()
        (target / "foo.txt").write_text("hello")

        backup_1 = rotate_directory(target)
        self.assertIsNotNone(backup_1)
        self.assertTrue(backup_1.exists())

        # Create target again at the same path with same mtime
        target.mkdir()
        (target / "bar.txt").write_text("world")
        os.utime(target, (backup_1.stat().st_atime, backup_1.stat().st_mtime))

        backup_2 = rotate_directory(target)
        self.assertIsNotNone(backup_2)
        self.assertTrue(backup_2.exists())
        self.assertNotEqual(backup_1, backup_2)
        self.assertTrue(backup_2.name.endswith("_1"))

    def test_rotate_directory_nonexistent_returns_none(self) -> None:
        from calibpipe.steps.staging import rotate_directory

        nonexistent = self.tmp / "does_not_exist"
        self.assertIsNone(rotate_directory(nonexistent))

    def test_rotate_directory_current_working_dir_returns_none(self) -> None:
        from calibpipe.steps.staging import rotate_directory

        cwd_path = Path.cwd()
        self.assertIsNone(rotate_directory(cwd_path))

    def test_stage_ancillary_files(self) -> None:
        from calibpipe.steps.staging import stage_ancillary_files

        workdir = self.tmp / "staged_work"
        cont = self.tmp / "test_cont.dat"
        cont.write_text("cont ranges")
        jyperk = self.tmp / "test_jyperk.csv"
        jyperk.write_text("jyperk factors")
        param = self.tmp / "test_param.list"
        param.write_text("param list")
        extra_dir = self.tmp / "extra_dir"
        extra_dir.mkdir()
        (extra_dir / "nested.txt").write_text("nested")

        staged = stage_ancillary_files(
            workdir,
            cont_dat=cont,
            jyperk_csv=jyperk,
            parameter_list=param,
            ancillary=[str(extra_dir)],
            recipe="SEIP_workflow",
        )

        self.assertEqual(len(staged), 4)
        self.assertTrue((workdir / "cont.dat").is_file())
        self.assertEqual((workdir / "cont.dat").read_text(), "cont ranges")
        self.assertTrue((workdir / "jyperk.csv").is_file())
        self.assertEqual((workdir / "jyperk.csv").read_text(), "jyperk factors")
        # Recipe contains SEIP, so parameter list gets SEIP_ prefix
        self.assertTrue((workdir / "SEIP_parameter.list").is_file())
        self.assertEqual((workdir / "SEIP_parameter.list").read_text(), "param list")
        self.assertTrue((workdir / "extra_dir" / "nested.txt").is_file())
        self.assertEqual((workdir / "extra_dir" / "nested.txt").read_text(), "nested")

    def test_stage_ancillary_files_same_source_and_target_no_error(self) -> None:
        from calibpipe.steps.staging import stage_ancillary_files

        workdir = self.tmp / "staged_same"
        workdir.mkdir()
        cont = workdir / "cont.dat"
        cont.write_text("already here")
        nested_dir = workdir / "sub"
        nested_dir.mkdir()
        (nested_dir / "file.txt").write_text("nested content")

        # Staging a file and directory already located in workdir should not raise SameFileError or delete sub
        staged = stage_ancillary_files(
            workdir,
            cont_dat=cont,
            ancillary=[str(nested_dir)],
        )

        self.assertEqual(len(staged), 2)
        self.assertTrue(cont.is_file())
        self.assertEqual(cont.read_text(), "already here")
        self.assertTrue((nested_dir / "file.txt").is_file())

    def test_stage_ancillary_missing_file_logs_warning(self) -> None:
        from calibpipe.steps.staging import stage_ancillary_files

        workdir = self.tmp / "staged_warn"
        workdir.mkdir()
        logs: list[str] = []

        staged = stage_ancillary_files(
            workdir,
            cont_dat=self.tmp / "nonexistent_cont.dat",
            jyperk_csv=self.tmp / "nonexistent_jyperk.csv",
            parameter_list=self.tmp / "nonexistent_param.list",
            ancillary=[str(self.tmp / "nonexistent_extra")],
            log_func=logs.append,
        )

        self.assertEqual(len(staged), 0)
        self.assertEqual(len(logs), 4)
        self.assertTrue(all("Warning:" in log for log in logs))

    def test_driver_backup_and_ancillary_execution(self) -> None:
        workdir = self.tmp / "work_backup_test"
        workdir.mkdir()
        (workdir / "previous_marker.txt").write_text("old run")

        cont_file = self.tmp / "run_cont.dat"
        cont_file.write_text("continuum")

        script_file = self.tmp / "test_backup.py"
        script_file.touch()

        _, exit_code = self.run_driver([
            "--script", str(script_file),
            "--backup",
            f"--cont-dat={cont_file}",
            f"--config={self.config}",
            f"--workdir={workdir}",
        ])

        self.assertEqual(exit_code, 0)
        # Previous run folder was rotated
        backups = list(self.tmp.glob("work_backup_test_backup_*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "previous_marker.txt").is_file())

        # Fresh workdir was created with cont.dat staged
        self.assertTrue(workdir.is_dir())
        self.assertFalse((workdir / "previous_marker.txt").exists())
        self.assertTrue((workdir / "cont.dat").is_file())
        self.assertEqual((workdir / "cont.dat").read_text(), "continuum")


class TestNamedProfiles(DriverModesTestCase):
    """Tests for named execution profile resolution in driver runs."""

    def test_driver_run_with_profile(self) -> None:
        from calibpipe import config as envconfig
        from calibpipe.config import ProfileConfig

        workdir = self.tmp / "work_profile_test"
        workdir.mkdir()
        script_file = self.tmp / "test_prof.py"
        script_file.touch()

        cfg = envconfig.load_merged_config(cli_arg=str(self.config))
        cfg.profiles["fast_prof"] = ProfileConfig(
            ncores=4,
            log2term=True,
            omp_num_threads=2,
            openblas_num_threads=2,
        )

        with patch("calibpipe.driver.envconfig.load_merged_config", return_value=cfg):
            calls, exit_code = self.run_driver([
                "--script", str(script_file),
                "--profile=fast_prof",
                f"--config={self.config}",
                f"--workdir={workdir}",
            ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.last_env.get("OMP_NUM_THREADS"), "2")
        self.assertEqual(self.last_env.get("OPENBLAS_NUM_THREADS"), "2")
        casa_runs = [c for c in calls if c[0] == "casa_run"]
        self.assertEqual(len(casa_runs), 1)
        cmd = casa_runs[0][1]
        self.assertIn("mpicasa -n 4", cmd)
        self.assertIn("--log2term", cmd)

    def test_driver_run_with_self_contained_profile_targets(self) -> None:
        from calibpipe import config as envconfig
        from calibpipe.config import ProfileConfig

        workdir = self.tmp / "work_self_contained"
        workdir.mkdir()
        vis_file = self.tmp / "test.ms"
        vis_file.touch()

        cfg = envconfig.load_merged_config(cli_arg=str(self.config))
        cfg.profiles["self_contained"] = ProfileConfig(
            vis=str(vis_file),
            procedure="procedure_hifa_calimage.xml",
            ncores=4,
            workdir=str(workdir),
        )

        with patch("calibpipe.driver.envconfig.load_merged_config", return_value=cfg):
            calls, exit_code = self.run_driver([
                "-p", "self_contained",
                f"--config={self.config}",
            ])

        self.assertEqual(exit_code, 0)
        casa_runs = [c for c in calls if c[0] == "casa_run"]
        self.assertEqual(len(casa_runs), 1)
        cmd = casa_runs[0][1]
        self.assertIn("mpicasa -n 4", cmd)
        self.assertIn("casa_piperun.py", cmd)
        piperun_file = workdir / "working" / "casa_piperun.py"
        self.assertTrue(piperun_file.is_file())
        script_content = piperun_file.read_text()
        self.assertIn("import pipeline.recipereducer", script_content)
        self.assertIn("procedure='procedure_hifa_calimage.xml'", script_content)

    def test_driver_run_profile_target_overridden_by_cli(self) -> None:
        from calibpipe import config as envconfig
        from calibpipe.config import ProfileConfig

        workdir = self.tmp / "work_override_target"
        workdir.mkdir()
        script_file = self.tmp / "script.py"
        script_file.touch()

        cfg = envconfig.load_merged_config(cli_arg=str(self.config))
        cfg.profiles["script_prof"] = ProfileConfig(
            script=str(script_file),
            workdir=str(workdir),
        )

        with patch("calibpipe.driver.envconfig.load_merged_config", return_value=cfg):
            calls, exit_code = self.run_driver([
                "-p", "script_prof",
                "--cmd", "import casatasks",
                f"--config={self.config}",
            ])

        self.assertEqual(exit_code, 0)
        casa_runs = [c for c in calls if c[0] == "casa_run"]
        self.assertEqual(len(casa_runs), 1)
        cmd = casa_runs[0][1]
        self.assertIn("casa_cmd.py", cmd)
        cmd_file = workdir / "casa_cmd.py"
        self.assertTrue(cmd_file.is_file())
        self.assertIn("import casatasks", cmd_file.read_text())

    def test_driver_dry_run_flag(self) -> None:
        workdir = self.tmp / "work_dry_run"
        workdir.mkdir()
        script_file = self.tmp / "dry_test.py"
        script_file.touch()

        calls, exit_code = self.run_driver([
            "--script", str(script_file),
            "--dry-run",
            f"--config={self.config}",
            f"--workdir={workdir}",
        ])

        self.assertEqual(exit_code, 0)
        casa_runs = [c for c in calls if c[0] == "casa_run"]
        self.assertEqual(len(casa_runs), 0)

    def test_write_ipython_config(self) -> None:
        from calibpipe.driver import write_ipython_config

        rcdir = self.tmp / "rc_autoreload"
        cfg_file = write_ipython_config(rcdir, autoreload=True)
        self.assertTrue(cfg_file.is_file())
        content = cfg_file.read_text()
        self.assertIn("%load_ext autoreload", content)
        self.assertIn("%autoreload 2", content)

        rcdir_no = self.tmp / "rc_no_autoreload"
        cfg_file_no = write_ipython_config(rcdir_no, autoreload=False)
        self.assertTrue(cfg_file_no.is_file())
        content_no = cfg_file_no.read_text()
        self.assertNotIn("%load_ext autoreload", content_no)

    def test_write_casa_config_datapath_and_rundata(self) -> None:
        from calibpipe.driver import write_casa_config

        rcdir = self.tmp / "rc_datapath_test"
        rcdir.mkdir(parents=True)
        config_file = write_casa_config(
            rcdir,
            datapath=["/tmp/my_data1", "/tmp/my_data2"],
            rundata=["/tmp/my_rundata"],
            rundata_specified=True,
        )
        self.assertTrue(config_file.is_file())
        content = config_file.read_text()
        self.assertIn("_user_datapath = ['/tmp/my_data1', '/tmp/my_data2']", content)
        self.assertIn("_user_rundata = ['/tmp/my_rundata']", content)
        self.assertIn("_rundata_specified = True", content)


if __name__ == "__main__":
    unittest.main()



