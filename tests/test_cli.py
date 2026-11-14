"""Tests for calibpipe.cli unified command-line interface."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from calibpipe import cli


class TestCliRouting(unittest.TestCase):
    """Test top-level CLI argument parsing and subcommand routing."""

    def test_submit_routes_to_typed_handler(self) -> None:
        from calibpipe.config import SubmitRequest
        with patch("calibpipe.batch.submit_request", return_value=0) as handler:
            cli.main(["submit", "pipefile.txt", "--env=main"])
            handler.assert_called_once_with(SubmitRequest(pipefile="pipefile.txt", env="main"))

    def test_batch_alias_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            cli.main(["batch", "pipefile.txt"])
        self.assertEqual(cm.exception.code, 2)

    def test_run_routes_to_typed_handler(self) -> None:
        from calibpipe.config import RunRequest
        with patch("calibpipe.runner.run_request", return_value=0) as handler:
            cli.main(["run", "--mous=uid://A001/X1/X1", "--env=main"])
            handler.assert_called_once_with(RunRequest(mous="uid://A001/X1/X1", env="main"))

    def test_run_config_only_routes_to_typed_handler(self) -> None:
        from calibpipe.config import RunRequest
        with patch("calibpipe.runner.run_request", return_value=0) as handler:
            cli.main(["run", "--config=snap.toml", "--config-only", "--cmd=print(1)"])
            handler.assert_called_once_with(
                RunRequest(config="snap.toml", config_only=True, cmd="print(1)")
            )

    def test_pmr_subcommand_routes_to_pmr_main(self) -> None:
        with patch("calibpipe.pmr.main", return_value=0) as mock_pmr_main:
            with self.assertRaises(SystemExit) as cm:
                cli.main(["pmr", "uid://A001/X1/X1", "--download"])
            self.assertEqual(cm.exception.code, 0)
            mock_pmr_main.assert_called_once_with(["uid://A001/X1/X1", "--download"])

    def test_implicit_run_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            cli.main(["--mous=uid://A001/X1/X1"])
        self.assertEqual(cm.exception.code, 2)

    def test_empty_arguments_exits_0(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            cli.main([])
        self.assertEqual(cm.exception.code, 0)


class TestCliProfileSubcommand(unittest.TestCase):
    """Test calibpipe profile CLI subcommands."""

    def test_profile_list_and_default(self) -> None:
        import io
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.toml"
            cfg_path.write_text("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa"

[profiles.fast_test]
desc = "Fast test profile"
vis = ["test.ms"]
nprocs = 4
""")
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["profile", "list", f"--config={cfg_path}"])
            self.assertIn("fast_test", buf.getvalue())

            # Default action is list
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                cli.main(["profile", f"--config={cfg_path}"])
            self.assertIn("fast_test", buf2.getvalue())

    def test_profile_show(self) -> None:
        import io
        import tempfile
        from contextlib import redirect_stderr, redirect_stdout
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.toml"
            cfg_path.write_text("""
default_env = "main"
[envs.main]
casa_root = "/fake/casa"

[profiles.fast_test]
desc = "Fast test profile"
vis = ["test.ms"]
nprocs = 4
""")
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["profile", "show", "fast_test", f"--config={cfg_path}"])
            self.assertIn("Profile: fast_test", buf.getvalue())
            self.assertIn("nprocs: 4", buf.getvalue())

            # Missing profile exits with 1
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                cli.main(["profile", "show", "nonexistent", f"--config={cfg_path}"])
            self.assertEqual(cm.exception.code, 1)

    def test_run_subcommand_interactive_flag(self) -> None:
        import io
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.toml"
            cfg_path.write_text("""
default_env = "main"

[paths]
scipipe_rootdir = "/fake/root"
scipipe_logdir = "/fake/logs"
workspaces = ["/fake/ws"]

[envs.main]
casa_root = "/fake/casa"

[profiles.interactive]
interactive = true
env = "main"
dry_run = true
""")
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["run", "-p=interactive", f"--config={cfg_path}", "--no-site-config"])
            self.assertIn("Execution mode: interactive", buf.getvalue())

    def test_run_subcommand_without_target_reports_error(self) -> None:
        import io
        import os
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.toml"
            cfg_path.write_text("""
default_env = "main"

[paths]
scipipe_rootdir = "/fake/root"
scipipe_logdir = "/fake/logs"
workspaces = ["/fake/ws"]

[envs.main]
casa_root = "/fake/casa"
""")
            buf = io.StringIO()
            with redirect_stdout(buf), patch.dict(
                os.environ, {"CALIBPIPE_CONFIG": str(cfg_path), "CALIBPIPE_NO_SITE_CONFIG": "1"}
            ):
                # An explicit run without a target or default profile is an error.
                with self.assertRaises(SystemExit) as cm:
                    cli.main(["run"])
                self.assertEqual(cm.exception.code, 1)

    def test_submit_subcommand_rejects_interactive_profile(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.toml"
            cfg_path.write_text("""
default_env = "main"

[envs.main]
casa_root = "/fake/casa"

[profiles.interactive]
interactive = true
env = "main"
""")
            with self.assertRaises(SystemExit) as cm:
                cli.main(["submit", "-p", "interactive", f"--config={cfg_path}", "--no-site-config"])
            self.assertEqual(cm.exception.code, 1)

    def test_report_subcommand_dispatch(self) -> None:
        with patch("calibpipe.report.main") as mock_report:
            cli.main(["report", "/tmp/fake_dir"])
            mock_report.assert_called_once_with(["/tmp/fake_dir"])

    def test_coverage_subcommand_dispatch(self) -> None:
        with patch("calibpipe.coverage.main") as mock_cov:
            cli.main(["coverage", "/tmp/fake_dir"])
            mock_cov.assert_called_once_with(["/tmp/fake_dir"])


if __name__ == "__main__":
    unittest.main()

