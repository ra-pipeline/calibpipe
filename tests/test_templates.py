"""Tests for calibpipe.templates template loading and rendering."""

from __future__ import annotations

import unittest

from calibpipe.templates import render_template


class TestTemplates(unittest.TestCase):
    """Test suite for calibpipe template loading and rendering."""

    def test_render_casa_config(self) -> None:
        """Verify casa_config.py.in renders with telemetry variable."""
        rendered = render_template("casa_config.py.in", telemetry="True")
        self.assertIn("telemetry_enabled = True", rendered)
        self.assertIn("crashreporter_enabled = False", rendered)
        self.assertIn("measures_auto_update = False", rendered)
        self.assertIn("/home/casa/data/distro", rendered)
        self.assertIn("logfile = os.path.join(_workdir,", rendered)

    def test_render_casa_startup(self) -> None:
        """Verify casa_startup.py.in renders cleanly without arguments."""
        rendered = render_template("casa_startup.py.in")
        self.assertIn("SCIPIPE_HEURISTICS", rendered)
        self.assertIn("import pipeline", rendered)
        self.assertIn("pipeline.initcli()", rendered)
        self.assertIn("import pipeline.infrastructure.executeppr as eppr", rendered)

    def test_render_slurm_job(self) -> None:
        """Verify slurm_job.sh.in renders with pipejob command."""
        cmd = "calibpipe run --mous=uid://A001/X1/X1 --env=main"
        rendered = render_template("slurm_job.sh.in", pipejob=cmd)
        self.assertIn("#!/bin/sh", rendered)
        self.assertIn("ulimit -Sn 8192", rendered)
        self.assertIn("umask 002", rendered)
        self.assertIn(cmd, rendered)


if __name__ == "__main__":
    unittest.main()
