"""Unit tests for calibpipe.pmr module."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from calibpipe import pmr
from calibpipe.pmr import (
    PMRRequest,
    PMRResult,
    PMRRuntime,
    build_java_command,
    download_asdms,
    download_restore_products,
    parse_project_root,
    run_pmr,
    validate_files,
)


class TestPMRModels(unittest.TestCase):
    """Test PMRRuntime resolution and PMRRequest argument serialization."""

    def test_runtime_from_env_missing_vars(self):
        with self.assertRaises(EnvironmentError) as ctx:
            PMRRuntime.from_env(env={})
        self.assertIn("ACSROOT", str(ctx.exception))
        self.assertIn("ACSDATA", str(ctx.exception))
        self.assertIn("SCIPIPE_SCRIPTDIR", str(ctx.exception))

    def test_runtime_from_env_valid(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            acsroot = base / "acs"
            acsdata = base / "acsdata"
            recipes = base / "recipes"
            for p in (acsroot, acsdata, recipes):
                p.mkdir()

            env = {
                "ACSROOT": str(acsroot),
                "ACSDATA": str(acsdata),
                "SCIPIPE_SCRIPTDIR": str(recipes),
                "SCIPIPE_ROOTDIR": str(base / "runs"),
            }
            rt = PMRRuntime.from_env(env=env, max_heap="8G")
            self.assertEqual(rt.acsroot, acsroot.resolve())
            self.assertEqual(rt.acsdata, acsdata.resolve())
            self.assertEqual(rt.scriptdir, recipes.resolve())
            self.assertEqual(rt.max_heap, "8G")
            self.assertEqual(rt.java_bin, "java")

    def test_request_to_java_args(self):
        # 1. Standard request with ASDMs
        req = PMRRequest(
            mous="uid://A001/X1/X1",
            intents="intents_hifa.xml",
            procedure="procedure_hifa_calimage.xml",
            download_asdms=True,
            download_restore=False,
            asdms=["uid://A002/X1", "uid://A002/X2"],
            ignore_qa0=True,
        )
        args = req.to_java_args()
        self.assertEqual(
            args,
            [
                "uid://A001/X1/X1",
                "intents_hifa.xml",
                "procedure_hifa_calimage.xml",
                "uid://A002/X1,uid://A002/X2",
                "true",
                "false",
                "true",
            ],
        )

        # 2. Omit ASDMs
        req_omit = PMRRequest(
            mous="uid://A001/X1/X1",
            asdms=["uid://A002/X1"],
            omit_asdms=True,
        )
        args_omit = req_omit.to_java_args()
        self.assertEqual(args_omit[3], "-,uid://A002/X1")

        # 3. Restore only (empty files serialized as "''")
        req_restore = PMRRequest(
            mous="uid://A001/X1/X1",
            intents="",
            procedure="null",
            download_asdms=False,
            download_restore=True,
        )
        args_restore = req_restore.to_java_args()
        self.assertEqual(args_restore[1], "''")
        self.assertEqual(args_restore[2], "''")
        self.assertEqual(args_restore[3], "''")
        self.assertEqual(args_restore[4], "false")
        self.assertEqual(args_restore[5], "true")


class TestPMRValidationAndParsing(unittest.TestCase):
    """Test file validation and output parsing."""

    def test_validate_files(self):
        with tempfile.TemporaryDirectory() as td:
            scriptdir = Path(td)
            recipe_file = scriptdir / "procedure_hifa_calimage.xml"
            recipe_file.write_text("<xml/>")

            # Relative resolution
            validate_files("", "procedure_hifa_calimage.xml", scriptdir)

            # Skipped empty / null
            validate_files("", "", scriptdir)
            validate_files("null", "none", scriptdir)

            # Missing file raises FileNotFoundError
            with self.assertRaises(FileNotFoundError):
                validate_files("missing.xml", "", scriptdir)

    def test_parse_project_root(self):
        output = (
            "Running ALMA MakeProcessingRequests...\n"
            "Querying archive for MOUS uid://A001/X1/X1\n"
            "Project root directory is /scratch/pipeline/2099.1.00001.S_20991231-000000\n"
            "Done.\n"
        )
        parsed = parse_project_root(output)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed, Path("/scratch/pipeline/2099.1.00001.S_20991231-000000"))

        # Relative project root combined with workspace root
        output_rel = (
            "Running ALMA MakeProcessingRequests...\n"
            "Workspace root directory is /scratch/pipeline\n"
            "Project root directory is 2099.1.00001.S_20991231-000000\n"
            "Done.\n"
        )
        parsed_rel = parse_project_root(output_rel)
        self.assertEqual(parsed_rel, Path("/scratch/pipeline/2099.1.00001.S_20991231-000000"))

        # "Workspace directory is" variant
        output_ws_dir = (
            "Workspace directory is /scratch/pipeline\n"
            "Project root directory is 2099.1.00001.S_20991231-000000\n"
        )
        self.assertEqual(
            parse_project_root(output_ws_dir),
            Path("/scratch/pipeline/2099.1.00001.S_20991231-000000"),
        )

        # Fallback to SCIPIPE_ROOTDIR when no workspace directory line in output
        with unittest.mock.patch.dict(os.environ, {"SCIPIPE_ROOTDIR": "/env/pipeline"}):
            output_no_ws = "Project root directory is 2099.1.00001.S_20991231-000000\n"
            self.assertEqual(
                parse_project_root(output_no_ws),
                Path("/env/pipeline/2099.1.00001.S_20991231-000000"),
            )

        self.assertIsNone(parse_project_root("Error: MOUS not found"))


class TestPMRCommandBuilding(unittest.TestCase):
    """Test Java command synthesis and options."""

    def test_build_java_command(self):
        runtime = PMRRuntime(
            acsroot=Path("/opt/pipetools/latest"),
            acsdata=Path("/opt/acsdata"),
            scriptdir=Path("/opt/recipes"),
            max_heap="6G",
        )
        req = PMRRequest(
            mous="uid://A001/X1/X1",
            intents="intents_hifa.xml",
            procedure="procedure_hifa_calimage.xml",
            download_asdms=True,
            download_restore=False,
            asdms=["uid://A002/X1"],
            ignore_qa0=True,
        )
        cmd_list, cmd_str = build_java_command(req, runtime)

        self.assertIn("-Xmx6G", cmd_list)
        self.assertIn("-DACSDATA=/opt/acsdata", cmd_list)
        self.assertIn("/opt/pipetools/latest/lib/*", cmd_list)
        self.assertIn("alma.pipelinescience.reqmanager.MakeProcessingRequests", cmd_list)
        self.assertIn("uid://A001/X1/X1", cmd_list)
        self.assertIn("true", cmd_list)

        self.assertIn("alma.pipelinescience.reqmanager.MakeProcessingRequests", cmd_str)
        self.assertIn('classpath "/opt/pipetools/latest/lib/*"', cmd_str)
        self.assertIn("-Xmx6G", cmd_str)


class TestPMRExecution(unittest.TestCase):
    """Test run_pmr execution, dry-run, and helper shortcuts."""

    def setUp(self):
        self.runtime = PMRRuntime(
            acsroot=Path("/opt/pipetools/latest"),
            acsdata=Path("/opt/acsdata"),
            scriptdir=Path("/opt/recipes"),
            max_heap="4G",
        )

    def test_dry_run(self):
        req = PMRRequest(mous="uid://A001/X1/X1")
        with patch("calibpipe.pmr.validate_files"):
            res = run_pmr(req, runtime=self.runtime, dry_run=True)
            self.assertEqual(res.exit_code, 0)
            self.assertIn("[DRY RUN]", res.output)
            self.assertIsNone(res.project_root)

    def test_mocked_execution_success(self):
        req = PMRRequest(mous="uid://A001/X1/X1")
        with patch("calibpipe.pmr.validate_files"):
            with patch("subprocess.Popen") as mock_popen:
                process_mock = MagicMock()
                process_mock.stdout.readline.side_effect = [
                    "Initializing MakeProcessingRequests\n",
                    "Project root directory is /scratch/runs/uid___A001_X1_X1\n",
                    "",
                ]
                process_mock.wait.return_value = None
                process_mock.returncode = 0
                mock_popen.return_value = process_mock

                res = run_pmr(req, runtime=self.runtime, dry_run=False)
                self.assertEqual(res.exit_code, 0)
                self.assertEqual(res.project_root, Path("/scratch/runs/uid___A001_X1_X1"))
                self.assertIn("Project root directory is", res.output)

    def test_download_restore_products_convenience_helper(self):
        with patch("calibpipe.pmr.validate_files"):
            res = download_restore_products(
                "uid://A001/X1/X1", runtime=self.runtime, dry_run=True
            )
            self.assertEqual(res.exit_code, 0)
            self.assertIn("false true false", res.command_str)

    def test_download_asdms_convenience_helper(self):
        with patch("calibpipe.pmr.validate_files"):
            res = download_asdms(
                "uid://A001/X1/X1",
                asdms=["uid://A002/X1"],
                runtime=self.runtime,
                dry_run=True,
            )
            self.assertEqual(res.exit_code, 0)
            self.assertIn("uid://A002/X1 true false false", res.command_str)


class TestPMRCLI(unittest.TestCase):
    """Test CLI argument parsing and options for 'calibpipe pmr'."""

    def test_cli_parser_defaults(self):
        parser = pmr.build_parser()
        args = parser.parse_args(["uid://A001/X1/X1"])
        self.assertEqual(args.mous, "uid://A001/X1/X1")
        self.assertEqual(args.intents, "")
        self.assertEqual(args.procedure, "")
        self.assertFalse(args.download)
        self.assertFalse(args.restore)
        self.assertFalse(args.omit)
        self.assertFalse(args.ignore_qa0)
        self.assertFalse(args.dry_run)

    def test_cli_parser_flags(self):
        parser = pmr.build_parser()
        args = parser.parse_args([
            "uid://A001/X1/X1",
            "--procedure", "procedure_hifa_image.xml",
            "--intents", "intents_hifa.xml",
            "--download",
            "--restore",
            "-a", "uid://A002/X1,uid://A002/X2",
            "-o",
            "--ignore-qa0",
            "--dry-run",
        ])
        self.assertEqual(args.mous, "uid://A001/X1/X1")
        self.assertEqual(args.procedure, "procedure_hifa_image.xml")
        self.assertEqual(args.intents, "intents_hifa.xml")
        self.assertTrue(args.download)
        self.assertTrue(args.restore)
        self.assertEqual(args.asdms, "uid://A002/X1,uid://A002/X2")
        self.assertTrue(args.omit)
        self.assertTrue(args.ignore_qa0)
        self.assertTrue(args.dry_run)

    def test_main_restore_only(self):
        with patch("calibpipe.pmr.PMRRuntime.from_env"):
            with patch("calibpipe.pmr.run_pmr") as mock_run:
                mock_run.return_value = PMRResult(0, [], "", "", None)
                exit_code = pmr.main([
                    "uid://A001/X1/X1",
                    "--restore",
                ])
                self.assertEqual(exit_code, 0)
                req = mock_run.call_args[0][0]
                self.assertEqual(req.mous, "uid://A001/X1/X1")
                self.assertFalse(req.download_asdms)
                self.assertTrue(req.download_restore)

    def test_main_download_only(self):
        with patch("calibpipe.pmr.PMRRuntime.from_env"):
            with patch("calibpipe.pmr.run_pmr") as mock_run:
                mock_run.return_value = PMRResult(0, [], "", "", None)
                exit_code = pmr.main([
                    "uid://A001/X1/X1",
                    "--download",
                ])
                self.assertEqual(exit_code, 0)
                req = mock_run.call_args[0][0]
                self.assertEqual(req.mous, "uid://A001/X1/X1")
                self.assertTrue(req.download_asdms)
                self.assertFalse(req.download_restore)

    def test_runner_run_mous_delegates_to_pmr_run_pmr(self):
        from calibpipe import runner
        from calibpipe.config import EnvSpec, ResolvedRunOptions

        with tempfile.TemporaryDirectory() as td:
            root_dir = Path(td)
            working_dir = (
                root_dir / "SOUS_uid___A1" / "GOUS_uid___A2" / "MOUS_uid___A3" / "working"
            )
            working_dir.mkdir(parents=True)
            ppr_file = working_dir / "PPR.xml"
            ppr_file.write_text("<PPR/>")

            opts = argparse.Namespace(
                mous="uid://A001/X1/X1",
                flag="",
                ppr="",
                onlysemipass="",
            )
            run_opts = ResolvedRunOptions(
                mous="uid://A001/X1/X1",
                recipe="calimage",
                dry_run=False,
            )
            env_spec = EnvSpec(name="test_env", casa_root="/fake/casa")
            env = {
                "SCIPIPE_ROOTDIR": str(root_dir),
                "ACSROOT": "/opt/pipetools",
                "ACSDATA": "/opt/acsdata",
                "SCIPIPE_SCRIPTDIR": "/opt/recipes",
            }
            site_cfg = {"strict_paths": False}

            mock_res = PMRResult(
                exit_code=0,
                command=["java", "..."],
                command_str="java ...",
                output="Project root directory is " + str(root_dir),
                project_root=root_dir,
            )

            with patch("calibpipe.pmr.run_pmr", return_value=mock_res) as mock_pmr_run:
                with patch("calibpipe.runner.RunLogger") as mock_logger_cls:
                    mock_logger_inst = MagicMock()
                    mock_logger_inst.run.return_value = 0
                    mock_logger_inst.runquiet.return_value = 0
                    mock_logger_cls.return_value = mock_logger_inst

                    with patch.dict("os.environ", {"CALIBPIPE_SKIP_PATH_CHECK": "1"}):
                        orig_getoutput = runner.getoutput
                        try:
                            runner.getoutput = runner.subprocess.getoutput
                            exit_code = runner._run_mous(
                                opts=opts,
                                run_opts=run_opts,
                                env_spec=env_spec,
                                env=env,
                                casaroot="/fake/casa",
                                casarun="casa",
                                site_cfg=site_cfg,
                            )
                            self.assertEqual(exit_code, 0)
                            mock_pmr_run.assert_called_once()
                            req = mock_pmr_run.call_args[0][0]
                            self.assertEqual(req.mous, "uid://A001/X1/X1")
                            self.assertEqual(req.procedure, "procedure_hifa_calimage.xml")
                            self.assertTrue(req.download_asdms)
                            self.assertFalse(req.download_restore)
                            self.assertIsNone(req.rename)
                        finally:
                            runner.getoutput = orig_getoutput

    def test_runner_run_mous_passes_workdir_rename_to_pmr(self):
        from calibpipe import runner
        from calibpipe.config import EnvSpec, ResolvedRunOptions

        with tempfile.TemporaryDirectory() as td:
            root_dir = Path(td)
            custom_workdir = root_dir / "custom_mous_workdir"
            working_dir = (
                custom_workdir / "SOUS_uid___A1" / "GOUS_uid___A2" / "MOUS_uid___A3" / "working"
            )
            working_dir.mkdir(parents=True)
            ppr_file = working_dir / "PPR.xml"
            ppr_file.write_text("<PPR/>")

            opts = argparse.Namespace(
                mous="uid://A001/X1/X1",
                flag="",
                ppr="",
                onlysemipass="",
                workdir=str(custom_workdir),
            )
            run_opts = ResolvedRunOptions(
                mous="uid://A001/X1/X1",
                recipe="calimage",
                workdir=str(custom_workdir),
                dry_run=False,
            )
            env_spec = EnvSpec(name="test_env", casa_root="/fake/casa")
            env = {
                "SCIPIPE_ROOTDIR": str(root_dir),
                "ACSROOT": "/opt/pipetools",
                "ACSDATA": "/opt/acsdata",
                "SCIPIPE_SCRIPTDIR": "/opt/recipes",
            }
            site_cfg = {"strict_paths": False}

            mock_res = PMRResult(
                exit_code=0,
                command=["java", "..."],
                command_str="java ...",
                output="Project root directory is " + str(custom_workdir),
                project_root=custom_workdir,
            )

            with patch("calibpipe.pmr.run_pmr", return_value=mock_res) as mock_pmr_run:
                with patch("calibpipe.runner.RunLogger") as mock_logger_cls:
                    mock_logger_inst = MagicMock()
                    mock_logger_inst.run.return_value = 0
                    mock_logger_inst.runquiet.return_value = 0
                    mock_logger_cls.return_value = mock_logger_inst

                    with patch.dict("os.environ", {"CALIBPIPE_SKIP_PATH_CHECK": "1"}):
                        orig_getoutput = runner.getoutput
                        try:
                            runner.getoutput = runner.subprocess.getoutput
                            exit_code = runner._run_mous(
                                opts=opts,
                                run_opts=run_opts,
                                env_spec=env_spec,
                                env=env,
                                casaroot="/fake/casa",
                                casarun="casa",
                                site_cfg=site_cfg,
                            )
                            self.assertEqual(exit_code, 0)
                            mock_pmr_run.assert_called_once()
                            req = mock_pmr_run.call_args[0][0]
                            self.assertEqual(req.mous, "uid://A001/X1/X1")
                            self.assertEqual(req.rename, str(custom_workdir))
                        finally:
                            runner.getoutput = orig_getoutput


class TestPMRRenaming(unittest.TestCase):
    """Test project root directory renaming and timestamp stripping."""

    def test_strip_timestamp_from_name(self):
        from calibpipe.pmr import strip_timestamp_from_name

        self.assertEqual(
            strip_timestamp_from_name("2099.1.00001.S_2099_12_31T23_59_59.000"),
            "2099.1.00001.S",
        )
        self.assertEqual(
            strip_timestamp_from_name("2099.1.00001.S_20991231-000000"),
            "2099.1.00001.S",
        )
        self.assertEqual(
            strip_timestamp_from_name("2099.A.00001.S_2099_12_31"),
            "2099.A.00001.S",
        )
        self.assertEqual(
            strip_timestamp_from_name("my_custom_run_20991231-120000"),
            "my_custom_run",
        )
        self.assertEqual(
            strip_timestamp_from_name("clean_project_name"),
            "clean_project_name",
        )

    def test_rename_project_root_basic_and_collision(self):
        from calibpipe.pmr import rename_project_root

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            orig_dir = base / "2099.1.00001.S_2099_12_31T23_59_59.000"
            orig_dir.mkdir()
            (orig_dir / "marker.txt").write_text("orig")

            # 1. Rename to non-existing destination
            target_name = "2099.1.00001.S"
            renamed = rename_project_root(orig_dir, target_name)
            self.assertEqual(renamed, base / "2099.1.00001.S")
            self.assertTrue(renamed.is_dir())
            self.assertFalse(orig_dir.exists())
            self.assertEqual((renamed / "marker.txt").read_text(), "orig")

            # 2. Collision handling: new dir with same target name causes existing to be rotated
            new_run_dir = base / "2099.1.00001.S_second_run"
            new_run_dir.mkdir()
            (new_run_dir / "marker.txt").write_text("second")

            renamed2 = rename_project_root(new_run_dir, target_name)
            self.assertEqual(renamed2, base / "2099.1.00001.S")
            self.assertEqual((renamed2 / "marker.txt").read_text(), "second")

            # Verify backup of first was created
            backups = list(base.glob("2099.1.00001.S.backup_*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "marker.txt").read_text(), "orig")

    def test_rename_project_root_empty_target_directory_avoids_backup(self):
        from calibpipe.pmr import rename_project_root

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            orig_dir = base / "gen_project"
            orig_dir.mkdir()
            (orig_dir / "file.txt").write_text("content")

            empty_target = base / "target_dir"
            empty_target.mkdir()

            renamed = rename_project_root(orig_dir, empty_target)
            self.assertEqual(renamed, empty_target)
            self.assertTrue((empty_target / "file.txt").is_file())
            self.assertFalse(orig_dir.exists())

            # Ensure no backup directory was created for the empty target
            backups = list(base.glob("target_dir.backup_*"))
            self.assertEqual(len(backups), 0)

    def test_rename_project_root_consolidate_into_ancestor(self):
        from calibpipe.pmr import rename_project_root

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            orig_dir = base / "2099.1.00001.S_timestamp"
            orig_dir.mkdir()
            (orig_dir / "subfile.txt").write_text("new_sub")
            (orig_dir / "colliding.txt").write_text("new_content")

            # Pre-existing file in ancestor destination
            (base / "colliding.txt").write_text("old_content")

            logs: list[str] = []
            renamed = rename_project_root(orig_dir, base, logger=logs.append)
            self.assertEqual(renamed, base)
            self.assertFalse(orig_dir.exists())
            self.assertEqual((base / "subfile.txt").read_text(), "new_sub")
            self.assertEqual((base / "colliding.txt").read_text(), "new_content")

            # Verify collided file was rotated to backup
            colliding_backups = list(base.glob("colliding.txt.backup_*"))
            self.assertEqual(len(colliding_backups), 1)
            self.assertEqual(colliding_backups[0].read_text(), "old_content")

    def test_rename_project_root_expanduser(self):
        from calibpipe.pmr import rename_project_root

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            orig_dir = base / "orig"
            orig_dir.mkdir()
            (orig_dir / "data.txt").write_text("val")

            fake_home = base / "fake_home"
            fake_home.mkdir()

            with unittest.mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
                target_tilde = "~/custom_run"
                renamed = rename_project_root(orig_dir, target_tilde)
                expected = (fake_home / "custom_run").resolve()
                self.assertEqual(renamed, expected)
                self.assertTrue((expected / "data.txt").is_file())

    def test_run_pmr_with_strip_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            gen_dir = base / "2099.1.00001.S_2099_12_31T23_59_59.000"
            gen_dir.mkdir()

            req = PMRRequest(
                mous="uid://A001/X1/X1",
                strip_timestamp=True,
            )
            runtime = PMRRuntime(
                acsroot=Path("/opt/pipetools/latest"),
                acsdata=Path("/opt/acsdata"),
                scriptdir=Path("/opt/recipes"),
            )
            with patch("calibpipe.pmr.validate_files"):
                with patch("subprocess.Popen") as mock_popen:
                    proc = MagicMock()
                    proc.stdout.readline.side_effect = [
                        f"Project root directory is {gen_dir}\n",
                        "",
                    ]
                    proc.wait.return_value = None
                    proc.returncode = 0
                    mock_popen.return_value = proc

                    res = run_pmr(req, runtime=runtime)
                    self.assertEqual(res.exit_code, 0)
                    expected_root = base / "2099.1.00001.S"
                    self.assertEqual(res.project_root, expected_root)
                    self.assertTrue(expected_root.is_dir())
                    self.assertFalse(gen_dir.exists())

    def test_run_pmr_with_explicit_rename(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            gen_dir = base / "2099.1.00001.S_2099_12_31T23_59_59.000"
            gen_dir.mkdir()

            req = PMRRequest(
                mous="uid://A001/X1/X1",
                rename="custom_project_name",
            )
            runtime = PMRRuntime(
                acsroot=Path("/opt/pipetools/latest"),
                acsdata=Path("/opt/acsdata"),
                scriptdir=Path("/opt/recipes"),
            )
            with patch("calibpipe.pmr.validate_files"):
                with patch("subprocess.Popen") as mock_popen:
                    proc = MagicMock()
                    proc.stdout.readline.side_effect = [
                        f"Project root directory is {gen_dir}\n",
                        "",
                    ]
                    proc.wait.return_value = None
                    proc.returncode = 0
                    mock_popen.return_value = proc

                    res = run_pmr(req, runtime=runtime)
                    self.assertEqual(res.exit_code, 0)
                    expected_root = base / "custom_project_name"
                    self.assertEqual(res.project_root, expected_root)
                    self.assertTrue(expected_root.is_dir())


if __name__ == "__main__":
    unittest.main()
