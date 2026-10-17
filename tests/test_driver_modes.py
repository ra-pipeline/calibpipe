"""Tests for calibpipe.driver direct execution modes (pcasa-style decoupling from PMR)."""

from __future__ import annotations

import io
import os
import shutil
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
        self.assertIn("mpicasa -n 8 --display-allocation --display-map --oversubscribe --bind-to core --map-by socket  /fake/casa/main/bin/casa", cmd)


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
        renamed_src = self.tmp / "test_orig.list"
        renamed_src.write_text("orig list")
        dict_src = self.tmp / "dict_orig.txt"
        dict_src.write_text("dict content")
        colon_src = self.tmp / "colon_orig.txt"
        colon_src.write_text("colon content")

        staged = stage_ancillary_files(
            workdir,
            cont_dat=cont,
            jyperk_csv=jyperk,
            parameter_list=param,
            ancillary=[
                str(extra_dir),
                [str(renamed_src), "renamed_target.list"],
                {"src": str(dict_src), "dest": "renamed_from_dict.txt"},
                f"{colon_src}:renamed_from_colon.txt",
            ],
            recipe="SEIP_workflow",
        )

        self.assertEqual(len(staged), 7)
        self.assertTrue((workdir / "cont.dat").is_file())
        self.assertEqual((workdir / "cont.dat").read_text(), "cont ranges")
        self.assertTrue((workdir / "jyperk.csv").is_file())
        self.assertEqual((workdir / "jyperk.csv").read_text(), "jyperk factors")
        # Recipe contains SEIP, so parameter list gets SEIP_ prefix
        self.assertTrue((workdir / "SEIP_parameter.list").is_file())
        self.assertEqual((workdir / "SEIP_parameter.list").read_text(), "param list")
        self.assertTrue((workdir / "extra_dir" / "nested.txt").is_file())
        self.assertEqual((workdir / "extra_dir" / "nested.txt").read_text(), "nested")
        self.assertTrue((workdir / "renamed_target.list").is_file())
        self.assertEqual((workdir / "renamed_target.list").read_text(), "orig list")
        self.assertTrue((workdir / "renamed_from_dict.txt").is_file())
        self.assertEqual((workdir / "renamed_from_dict.txt").read_text(), "dict content")
        self.assertTrue((workdir / "renamed_from_colon.txt").is_file())
        self.assertEqual((workdir / "renamed_from_colon.txt").read_text(), "colon content")

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


class TestBatchJobColocation(unittest.TestCase):
    """Test colocation of batch submission script and live log symlinks replaced by final copies."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.working = self.tmp / "working"
        self.working.mkdir()
        self.logs_dir = self.tmp / "central_logs"
        self.logs_dir.mkdir()

    def test_batch_colocation_lifecycle_and_finalization(self) -> None:
        out_file = self.logs_dir / "batch.testjob.out"
        err_file = self.logs_dir / "batch.testjob.err"
        script_file = self.logs_dir / "batch.testjob.sbatch"

        out_file.write_text("INITIAL STDOUT\n")
        err_file.write_text("INITIAL STDERR\n")
        script_file.write_text("#!/bin/sh\n#SBATCH --job-name=testjob\n")

        with patch.dict(
            os.environ,
            {
                "CALIBPIPE_BATCH_OUTFILE": str(out_file),
                "CALIBPIPE_BATCH_ERRFILE": str(err_file),
                "CALIBPIPE_BATCH_SCRIPT": str(script_file),
            },
        ):
            # Job start: script should be copied, logs should be symlinked
            driver.link_batch_job_files_start(self.working)

            copied_script = self.working / "batch.testjob.sbatch"
            self.assertTrue(copied_script.is_file())
            self.assertFalse(copied_script.is_symlink())
            self.assertIn("#SBATCH --job-name=testjob", copied_script.read_text())

            sym_out = self.working / "batch.out"
            sym_err = self.working / "batch.err"
            self.assertTrue(sym_out.is_symlink())
            self.assertTrue(sym_err.is_symlink())
            self.assertEqual(sym_out.resolve(), out_file.resolve())
            self.assertEqual(sym_err.resolve(), err_file.resolve())
            self.assertEqual(sym_out.read_text(), "INITIAL STDOUT\n")

            # Simulate scheduler appending execution logs while job runs
            with open(out_file, "a") as f:
                f.write("TASK COMPLETED IN CASA\n")
            with open(err_file, "a") as f:
                f.write("SOME WARNING IN CASA\n")

            # Operator reading symlink sees live output
            self.assertIn("TASK COMPLETED IN CASA", sym_out.read_text())

            # Job finish: symlinks are unlinked and replaced with real file copies
            driver.finalize_batch_job_logs(self.working)

            final_out = self.working / "batch.out"
            final_err = self.working / "batch.err"
            self.assertFalse(final_out.is_symlink(), "batch.out should be a real file after finalization")
            self.assertFalse(final_err.is_symlink(), "batch.err should be a real file after finalization")
            self.assertTrue(final_out.is_file())
            self.assertTrue(final_err.is_file())
            self.assertIn("TASK COMPLETED IN CASA", final_out.read_text())
            self.assertIn("SOME WARNING IN CASA", final_err.read_text())

            # Simulate external cleanup of logs_dir; working/ retains full standalone records
            shutil.rmtree(self.logs_dir)
            self.assertTrue(final_out.is_file())
            self.assertIn("TASK COMPLETED IN CASA", final_out.read_text())
            self.assertTrue(copied_script.is_file())

    def test_batch_colocation_slurm_env_fallback(self) -> None:
        submit_dir = self.tmp / "submit"
        submit_dir.mkdir()
        job_name = "fallback_job_123"
        (submit_dir / f"batch.{job_name}.out").write_text("FALLBACK STDOUT\n")
        (submit_dir / f"batch.{job_name}.err").write_text("FALLBACK STDERR\n")
        (submit_dir / f"batch.{job_name}.sbatch").write_text("#!/bin/sh\n#SBATCH ...\n")

        env_clean = {k: v for k, v in os.environ.items() if not k.startswith("CALIBPIPE_BATCH_")}
        env_clean["SLURM_SUBMIT_DIR"] = str(submit_dir)
        env_clean["SLURM_JOB_NAME"] = job_name

        with patch.dict(os.environ, env_clean, clear=True):
            driver.link_batch_job_files_start(self.working)
            self.assertTrue((self.working / f"batch.{job_name}.sbatch").is_file())
            self.assertTrue((self.working / "batch.out").is_symlink())
            self.assertEqual((self.working / "batch.out").read_text(), "FALLBACK STDOUT\n")

            driver.finalize_batch_job_logs(self.working)
            self.assertFalse((self.working / "batch.out").is_symlink())
            self.assertEqual((self.working / "batch.out").read_text(), "FALLBACK STDOUT\n")

    def test_batch_colocation_non_batch_noop(self) -> None:
        env_clean = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("CALIBPIPE_BATCH_") and not k.startswith("SLURM_")
        }
        with patch.dict(os.environ, env_clean, clear=True):
            driver.link_batch_job_files_start(self.working)
            self.assertFalse((self.working / "batch.out").exists())
            self.assertFalse((self.working / "batch.err").exists())
            driver.finalize_batch_job_logs(self.working)
            self.assertFalse((self.working / "batch.out").exists())

    def test_batch_colocation_same_destination_file_guard(self) -> None:
        # If output files already exist inside working directory itself
        existing_out = self.working / "batch.out"
        existing_out.write_text("DIRECT OUTPUT\n")
        existing_script = self.working / "batch.job.sbatch"
        existing_script.write_text("#!/bin/sh\n")

        with patch.dict(
            os.environ,
            {
                "CALIBPIPE_BATCH_OUTFILE": str(existing_out),
                "CALIBPIPE_BATCH_SCRIPT": str(existing_script),
            },
        ):
            # Should not unlink the real file or raise an error trying to copy onto itself
            driver.link_batch_job_files_start(self.working)
            self.assertFalse(existing_out.is_symlink())
            self.assertEqual(existing_out.read_text(), "DIRECT OUTPUT\n")
            self.assertEqual(existing_script.read_text(), "#!/bin/sh\n")


if __name__ == "__main__":
    unittest.main()




