"""Tests for calibpipe.cli unified command-line interface."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from calibpipe import cli


class TestCliRouting(unittest.TestCase):
    """Test top-level CLI argument parsing and subcommand routing."""

    def test_submit_routes_to_batch_main(self) -> None:
        with patch("calibpipe.batch.main") as mock_batch_main:
            cli.main(["submit", "pipefile.txt", "--env=main"])
            mock_batch_main.assert_called_once_with(["pipefile.txt", "--env=main"])

    def test_batch_alias_routes_to_batch_main(self) -> None:
        with patch("calibpipe.batch.main") as mock_batch_main:
            cli.main(["batch", "pipefile.txt", "--env=main"])
            mock_batch_main.assert_called_once_with(["pipefile.txt", "--env=main"])

    def test_run_subcommand_routes_to_driver_main(self) -> None:
        with patch("calibpipe.driver.main") as mock_driver_main:
            cli.main(["run", "--mous=uid://A001/X1/X1", "--env=main"])
            mock_driver_main.assert_called_once_with(["--mous=uid://A001/X1/X1", "--env=main"])

    def test_implicit_run_routing(self) -> None:
        with patch("calibpipe.driver.main") as mock_driver_main:
            cli.main(["--mous=uid://A001/X1/X1", "--env=main"])
            mock_driver_main.assert_called_once_with(["--mous=uid://A001/X1/X1", "--env=main"])

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
vis = "test.ms"
ncores = 4
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
vis = "test.ms"
ncores = 4
""")
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["profile", "show", "fast_test", f"--config={cfg_path}"])
            self.assertIn("Profile: fast_test", buf.getvalue())
            self.assertIn("nprocs                : 4", buf.getvalue())

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
target = "interactive"
interactive = true
env = "main"
dry_run = true
""")
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["run", "-p=interactive", f"--config={cfg_path}", "--no-site-config"])
            self.assertIn("Execution mode: interactive", buf.getvalue())

    def test_run_subcommand_empty_args_shows_help(self) -> None:
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
                # When run without arguments, prints driver help and exits 0
                with self.assertRaises(SystemExit) as cm:
                    cli.main(["run"])
                self.assertEqual(cm.exception.code, 0)
            self.assertIn("Assists in the execution of the interferometric pipeline", buf.getvalue())

    def test_submit_subcommand_rejects_interactive_profile(self) -> None:
        import tempfile
        from pathlib import Path
        from calibpipe import batch

        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.toml"
            cfg_path.write_text("""
default_env = "main"

[envs.main]
casa_root = "/fake/casa"

[profiles.interactive]
target = "interactive"
interactive = true
env = "main"
""")
            with self.assertRaises(SystemExit) as cm:
                batch.main(["-p", "interactive", f"--config={cfg_path}", "--no-site-config"])
            self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()

