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


if __name__ == "__main__":
    unittest.main()
