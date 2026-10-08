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
        self.assertIn("log2term = False", rendered)
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
        rendered = render_template(
            "slurm_job.sh.in",
            pipejob=cmd,
            queue="plwg",
            node="1",
            cores="8",
            job_name="myjob",
            mail_user="testuser",
            mail_type="ALL",
            outfile="my.out",
            errfile="my.err",
            extra_directives="",
        )
        self.assertIn("#!/bin/sh", rendered)
        self.assertIn("#SBATCH --partition=plwg", rendered)
        self.assertIn("#SBATCH --output=my.out", rendered)
        self.assertIn("#SBATCH --error=my.err", rendered)
        self.assertIn("ulimit -Sn 8192", rendered)
        self.assertIn("umask 002", rendered)
        self.assertIn(cmd, rendered)

    def test_render_htcondor_submit(self) -> None:
        """Verify htcondor_job.htc.in renders with HTCondor submit directives."""
        rendered = render_template(
            "htcondor_job.htc.in",
            partition="batch",
            extra_requirements=' && ( TARGET.Machine == "node01" )',
            request_memory="64G",
            request_cpus="8",
            batch_name="mous_job",
            initialdir="/tmp/work",
            output="batch.out",
            error="batch.err",
            log="batch.log",
            notification="Always",
            arguments="wrapper.sh",
        )
        self.assertIn("+partition = \"batch\"", rendered)
        self.assertIn("( ( batch == True ) && ( HasLustre == True ) && ( NumJobStarts == 0 ) && ( TARGET.Machine == \"node01\" ) )", rendered)
        self.assertIn("request_cpus = 8", rendered)
        self.assertIn("request_memory = 64G", rendered)
        self.assertIn("batch_name = mous_job", rendered)
        self.assertIn("arguments = wrapper.sh", rendered)
        self.assertIn("periodic_remove = JobStatus == 1 && NumJobStarts > 0", rendered)

    def test_render_htcondor_wrapper(self) -> None:
        """Verify htcondor_job.sh.in renders with pipejob command."""
        cmd = "calibpipe run --mous=uid://A001/X1/X1 --env=main"
        rendered = render_template("htcondor_job.sh.in", pipejob=cmd)
        self.assertIn("#!/bin/bash", rendered)
        self.assertIn("ulimit -Sn 8192", rendered)
        self.assertIn("umask 002", rendered)
        self.assertIn(cmd, rendered)


if __name__ == "__main__":
    unittest.main()

