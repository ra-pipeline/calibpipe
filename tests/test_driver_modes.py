"""Tests for calibpipe.driver direct execution modes (pcasa-style decoupling from PMR)."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
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

        # Check piperun script was generated
        piperun_file = workdir / "casa_piperun.py"
        self.assertTrue(piperun_file.is_file())
        script_content = piperun_file.read_text()
        self.assertIn("import pipeline.recipereducer", script_content)
        self.assertIn(f"vis=['{ms_path.resolve()}']", script_content)
        self.assertIn("procedure='procedure_hifa_calimage.xml'", script_content)

        # Check CASA execution call
        casa_runs = [c for c in calls if c[0] == "casa_run"]
        self.assertEqual(len(casa_runs), 1)
        self.assertIn(f"-c {piperun_file.resolve()}", casa_runs[0][1])
        self.assertIn("xvfb-run -d", casa_runs[0][1])

    def test_recipe_reducer_derives_procedure_from_recipe(self) -> None:
        ms_path = self.tmp / "test.ms"
        ms_path.mkdir()

        workdir = self.tmp / "work_reducer_cal"
        workdir.mkdir()

        calls, exit_code = self.run_driver([
            "--vis", str(ms_path),
            "--recipe=cal",
            f"--config={self.config}",
            f"--workdir={workdir}",
        ])

        self.assertEqual(exit_code, 0)
        piperun_file = workdir / "casa_piperun.py"
        self.assertTrue(piperun_file.is_file())
        self.assertIn("procedure='procedure_hifa_cal.xml'", piperun_file.read_text())


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

        calls, exit_code = self.run_driver([
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
        calls, exit_code = self.run_driver([
            "--mous=uid://A001/X1/X1",
            "--script=/path/to/script.py",
            f"--config={self.config}",
        ])
        self.assertEqual(exit_code, 1)

    def test_missing_targets_exits_1(self) -> None:
        calls, exit_code = self.run_driver([
            f"--config={self.config}",
            "--env=main",
        ])
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
