"""Tests for calibpipe.driver (migrated from test_calibPipeIF.py)."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from calibpipe import driver as calibPipeIF

REFERENCE_FILE = Path(__file__).resolve().parent / "reference" / "calibpipeif_calls.txt"

MOUS = "uid://A001/X1/X1"
PPMR_REL_DIR = "2019.1.01234.S_run1"
SOUS_DIR = "SOUS_uid___A001_X1_Xa"
GOUS_DIR = "GOUS_uid___A001_X1_Xb"
MOUS_DIR = "MOUS_uid___A001_X1_Xc"
PRODUCTS_BASENAME = "uid___A001_X1_Xc"

PPR_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<PipelineProcessingRequest>
  <RelativePath>{PPMR_REL_DIR}/{SOUS_DIR}/{GOUS_DIR}/{MOUS_DIR}</RelativePath>
  <ProjectSummary>
    <ExecBlockId>uid://A002/Xaaa/Xbbb</ExecBlockId>
  </ProjectSummary>
</PipelineProcessingRequest>
"""

PMR_OUTPUT = (
    "Running pipelineMakeRequest...\n"
    f"Project root directory is {PPMR_REL_DIR}\n"
    "no files missing\n"
)


def _write_config(path, root, logdir):
    path.write_text(f"""
default_env = "main"

[paths]
scipipe_rootdir = "{root}"
scipipe_logdir  = "{logdir}"

[envs.main]
casa_root = "/fake/casa/main"
""")


def _write_pixi_config(
    path, root, logdir, pixi_dir, pixi_env="default", pixi_bin="/fake/bin/pixi"
):
    path.write_text(f"""
default_env = "modular"

[paths]
scipipe_rootdir = "{root}"
scipipe_logdir  = "{logdir}"

[site]
pixi_bin = "{pixi_bin}"

[envs.modular]
pixi_dir = "{pixi_dir}"
pixi_env = "{pixi_env}"
""")


def _build_ppmr_tree(root):
    mous_dir = root / PPMR_REL_DIR / SOUS_DIR / GOUS_DIR / MOUS_DIR
    working = mous_dir / "working"
    rawdata = mous_dir / "rawdata"
    products = mous_dir / "products"
    for d in (
        rawdata,
        products,
        working / "pipeline-0" / "html",
        working / "pipeline-1" / "html",
    ):
        d.mkdir(parents=True, exist_ok=True)

    (working / f"PPR_{PRODUCTS_BASENAME}.xml").write_text(PPR_XML)
    (working / "casa_pipescript.py.txt").write_text("print('fixes placeholder')\n")
    (products / f"{PRODUCTS_BASENAME}.casa_pipescript.py").write_text(
        "hifa_importdata(vis=['uid___A001_X1_Xc.ms'])\n"
    )
    (products / f"{PRODUCTS_BASENAME}.casa_piperestorescript.py").write_text(
        "hifa_restoredata(vis=['uid___A001_X1_Xc.ms'])\n"
    )


def _build_flag_dir(flagdir):
    flagdir.mkdir(parents=True, exist_ok=True)
    (flagdir / f"{PRODUCTS_BASENAME}.flagtsystemplate.txt").write_text("# flags\n")
    (flagdir / f"{PRODUCTS_BASENAME}.ms.wvr").mkdir()


def _normalize(text, tmp_root):
    return text.replace(str(tmp_root), "<TMP>")


def _format_calls(calls, tmp_root):
    lines = []
    for entry in calls:
        normalized = [_normalize(str(field), tmp_root) for field in entry]
        lines.append(" | ".join(normalized))
    return "\n".join(lines) + "\n"


def _load_reference():
    text = REFERENCE_FILE.read_text()
    blocks = {}
    name = None
    body = []
    for line in text.splitlines(keepends=True):
        if line.startswith("=====BEGIN "):
            name = line[len("=====BEGIN ") :].split("=====")[0]
            body = []
        elif line.startswith("=====END "):
            blocks[name] = "".join(body)
            name = None
        elif name is not None:
            body.append(line)
    return blocks


class CalibPipeIFCaptureCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        self.tmp = Path(self._tmpdir.name)

        self.root = self.tmp / "rootdir"
        self.logdir = self.tmp / "logdir"
        self.config = self.tmp / "config.toml"
        self.working = (
            self.root / PPMR_REL_DIR / SOUS_DIR / GOUS_DIR / MOUS_DIR / "working"
        )
        self.rawdata = (
            self.root / PPMR_REL_DIR / SOUS_DIR / GOUS_DIR / MOUS_DIR / "rawdata"
        )
        _write_config(self.config, self.root, self.logdir)
        _build_ppmr_tree(self.root)

    def run_and_capture(self, extra_args):
        calls = []
        real_getoutput = subprocess.getoutput
        real_copy = shutil.copy
        real_copytree = shutil.copytree

        def fake_getoutput(cmd):
            if cmd.startswith("pipelineMakeRequest"):
                calls.append(("pipelineMakeRequest", cmd))
                return PMR_OUTPUT
            if cmd.startswith("cp "):
                calls.append(("copy", cmd))
            return real_getoutput(cmd)

        def fake_copy(src, dst, *a, **kw):
            calls.append(("copy", "shutil.copy", src, dst))
            return real_copy(src, dst, *a, **kw)

        def fake_copytree(src, dst, *a, **kw):
            calls.append(("copy", "shutil.copytree", src, dst))
            return real_copytree(src, dst, *a, **kw)

        self.casa_cwd = None
        self.casa_environ = None

        def fake_run(cmd):
            if "xvfb-run" in cmd and (
                "/bin/casa " in cmd
                or "/bin/mpicasa " in cmd
                or " casa " in cmd
                or " casampi " in cmd
            ):
                self.casa_cwd = Path(os.getcwd()).resolve()
                self.casa_environ = dict(os.environ)
                calls.append(("casa", cmd))
            return 0

        argv = (
            ["calibPipeIF.py"]
            + [
                f"--mous={MOUS}",
                f"--config={self.config}",
                "--env=main",
                "--recipe=calimage",
            ]
            + extra_args
        )

        with (
            patch.object(calibPipeIF, "argv", argv),
            patch.object(calibPipeIF, "getoutput", side_effect=fake_getoutput),
            patch.object(calibPipeIF.shutil, "copy", side_effect=fake_copy),
            patch.object(calibPipeIF.shutil, "copytree", side_effect=fake_copytree),
            patch.object(calibPipeIF.RunLogger, "run", side_effect=fake_run),
            patch.object(calibPipeIF.RunLogger, "runquiet", side_effect=fake_run),
            patch.dict(calibPipeIF.os.environ, {}, clear=False),
            redirect_stdout(io.StringIO()),
        ):
            calibPipeIF.main()

        return calls

    def assert_matches_reference(self, name, calls):
        reference = _load_reference()
        self.assertIn(
            name, reference, f"no reference block named {name!r} in {REFERENCE_FILE}"
        )
        actual = _format_calls(calls, self.tmp)
        self.assertEqual(actual, reference[name], f"call sequence mismatch for {name}")


class TestFlagDirCopiesOldProducts(CalibPipeIFCaptureCase):
    def test_flag_dir_yields_pipelineMakeRequest_then_copies_then_casa(self):
        flagdir = self.tmp / "flagdir"
        _build_flag_dir(flagdir)
        calls = self.run_and_capture([f"--flag={flagdir}"])

        categories = [c[0] for c in calls]
        self.assertEqual(
            categories,
            ["pipelineMakeRequest", "copy", "copy", "copy", "casa"],
            "expected pipelineMakeRequest, then the old-flag-file/wvr copies, then casa",
        )
        self.assert_matches_reference("with_flag_dir", calls)


class TestNoFlagDirSkipsCopies(CalibPipeIFCaptureCase):
    def test_no_flag_dir_yields_pipelineMakeRequest_then_casa_with_no_copies(self):
        calls = self.run_and_capture([])

        categories = [c[0] for c in calls]
        self.assertEqual(
            categories,
            ["pipelineMakeRequest", "casa"],
            "with no --flag dir there's nothing old to copy",
        )
        self.assert_matches_reference("without_flag_dir", calls)


class TestCustomRcdir(CalibPipeIFCaptureCase):
    def test_custom_rcdir_creates_startup_and_config(self):
        calls = self.run_and_capture([])
        casa_calls = [c[1] for c in calls if c[0] == "casa"]
        self.assertEqual(len(casa_calls), 1)
        self.assertIn("--cachedir", casa_calls[0])
        self.assertIn("--startupfile", casa_calls[0])
        self.assertIn("--configfile", casa_calls[0])

        startup_file = self.working / ".casa" / "startup.py"
        config_file = self.working / ".casa" / "config.py"
        self.assertTrue(startup_file.is_file())
        self.assertTrue(config_file.is_file())

        startup_content = startup_file.read_text()
        self.assertIn(
            "import pipeline.infrastructure.executeppr as eppr", startup_content
        )
        self.assertIn("pipeline.initcli()", startup_content)

        config_content = config_file.read_text()
        self.assertIn("telemetry_enabled = False", config_content)
        self.assertIn("crashreporter_enabled = False", config_content)

    def test_no_custom_rcdir_omits_rcdir_args(self):
        calls = self.run_and_capture(["--no-custom-rcdir"])
        casa_calls = [c[1] for c in calls if c[0] == "casa"]
        self.assertEqual(len(casa_calls), 1)
        self.assertNotIn("--cachedir", casa_calls[0])
        self.assertNotIn("--rcdir", casa_calls[0])

    def test_get_casa_rcdir_args_legacy_fallback(self):
        legacy_dir = self.tmp / "legacy_casa"
        legacy_dir.mkdir()
        rcdir = self.working / ".casa"
        args = calibPipeIF.get_casa_rcdir_args(legacy_dir, rcdir)
        self.assertEqual(args, ["--rcdir", str(rcdir)])


class TestWorkingDirExecution(CalibPipeIFCaptureCase):
    def test_working_fixes_file_created_and_cwd_switched(self):
        fixes_file = self.working / "sacmPL-fixes.casa.py.txt"
        self.assertFalse(fixes_file.exists())
        orig_cwd = Path(os.getcwd()).resolve()
        self.run_and_capture([])
        self.assertTrue(fixes_file.exists())
        self.assertEqual(self.casa_cwd, self.working.resolve())
        self.assertEqual(Path(os.getcwd()).resolve(), orig_cwd)

    def test_rawdata_fixes_copied_to_working(self):
        rawdata_fixes = self.rawdata / "sacmPL-fixes.casa.py.txt"
        rawdata_fixes.write_text("# custom fix from rawdata\n")
        fixes_file = self.working / "sacmPL-fixes.casa.py.txt"
        self.run_and_capture([])
        self.assertTrue(fixes_file.exists())
        self.assertEqual(fixes_file.read_text(), "# custom fix from rawdata\n")


class TestRunLoggerContextManager(unittest.TestCase):
    def test_run_logger_context_manager_closes_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test_driver"
            with calibPipeIF.RunLogger(str(log_path)) as l:
                l.log("hello world")
                self.assertFalse(l.fd.closed)
            self.assertTrue(l.fd.closed)


class TestPixiExecution(CalibPipeIFCaptureCase):
    def setUp(self):
        super().setUp()
        self.pixi_dir = self.tmp / "pixi_ws"
        self.pixi_dir.mkdir()
        (self.pixi_dir / "pyproject.toml").touch()
        self.pixi_config = self.tmp / "pixi_config.toml"
        _write_pixi_config(
            self.pixi_config,
            self.root,
            self.logdir,
            self.pixi_dir,
            pixi_bin="/opt/pixi/bin/pixi",
        )

    def test_pixi_serial_execution(self):
        calls = self.run_and_capture(
            [
                f"--config={self.pixi_config}",
                "--env=modular",
                "--ncores=1",
            ]
        )
        casa_calls = [c[1] for c in calls if c[0] == "casa"]
        self.assertEqual(len(casa_calls), 1)
        cmd = casa_calls[0]
        self.assertIn("/opt/pixi/bin/pixi run --frozen --manifest-path", cmd)
        self.assertIn(f"{self.pixi_dir}/pyproject.toml", cmd)
        self.assertIn(" casa --nocrashreport", cmd)
        self.assertNotIn("casampi", cmd)
        self.assertNotIn("CASA_NPROCS", cmd)
        # Verify custom rcdir casaconfig flags
        self.assertIn("--cachedir", cmd)
        self.assertIn("--configfile", cmd)
        self.assertIn("--startupfile", cmd)
        self.assertIn("--nologger", cmd)
        piperun = self.working / "casa_piperun.py"
        self.assertIn(f"-c {piperun.resolve()}", cmd)
        self.assertTrue(piperun.is_file())
        self.assertIn("eppr.executeppr", piperun.read_text())

    def test_pixi_mpi_execution(self):
        calls = self.run_and_capture(
            [
                f"--config={self.pixi_config}",
                "--env=modular",
                "--ncores=16",
            ]
        )
        casa_calls = [c[1] for c in calls if c[0] == "casa"]
        self.assertEqual(len(casa_calls), 1)
        cmd = casa_calls[0]
        self.assertIn("env CASA_NPROCS=16 /opt/pixi/bin/pixi run --frozen --manifest-path", cmd)
        self.assertIn(" casampi --nocrashreport", cmd)
        self.assertIn("--nologger", cmd)
        piperun = self.working / "casa_piperun.py"
        self.assertIn(f"-c {piperun.resolve()}", cmd)
        self.assertTrue(piperun.is_file())

    def test_pixi_custom_env(self):
        custom_cfg = self.tmp / "custom_pixi.toml"
        _write_pixi_config(
            custom_cfg,
            self.root,
            self.logdir,
            self.pixi_dir,
            pixi_env="casa676-py312",
            pixi_bin="/opt/pixi/bin/pixi",
        )
        calls = self.run_and_capture(
            [
                f"--config={custom_cfg}",
                "--env=modular",
                "--ncores=8",
            ]
        )
        casa_calls = [c[1] for c in calls if c[0] == "casa"]
        self.assertEqual(len(casa_calls), 1)
        cmd = casa_calls[0]
        self.assertIn("-e casa676-py312", cmd)
        self.assertIn("casampi", cmd)

    def test_virtual_env_popped_from_environment(self):
        with patch.dict(os.environ, {"VIRTUAL_ENV": "/tmp/fake_venv"}):
            self.run_and_capture(
                [
                    f"--config={self.pixi_config}",
                    "--env=modular",
                    "--ncores=1",
                ]
            )
            self.assertIsNotNone(self.casa_environ)
            self.assertNotIn("VIRTUAL_ENV", self.casa_environ)


class TestSymlinkShortcuts(unittest.TestCase):
    """Test convenience symlink generation for project root."""

    def test_create_project_symlinks_relative(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ppmr_dir = root / "2016.2.00105.S_run1"
            working = ppmr_dir / "SOUS_uid___1" / "GOUS_uid___2" / "MOUS_uid___3" / "working"
            working.mkdir(parents=True)
            products = working.parent / "products"
            products.mkdir()
            rawdata = working.parent / "rawdata"
            rawdata.mkdir()

            links = calibPipeIF.create_project_symlinks(ppmr_dir, working)
            self.assertEqual(len(links), 3)

            link_working = ppmr_dir / "working"
            link_products = ppmr_dir / "products"
            link_rawdata = ppmr_dir / "rawdata"

            self.assertTrue(link_working.is_symlink())
            self.assertTrue(link_products.is_symlink())
            self.assertTrue(link_rawdata.is_symlink())

            # Verify target resolution
            self.assertEqual(link_working.resolve(), working.resolve())
            self.assertEqual(link_products.resolve(), products.resolve())
            self.assertEqual(link_rawdata.resolve(), rawdata.resolve())

            # Verify symlinks are relative, not absolute
            target_str = os.readlink(link_working)
            self.assertFalse(os.path.isabs(target_str))
            self.assertEqual(
                target_str,
                os.path.join("SOUS_uid___1", "GOUS_uid___2", "MOUS_uid___3", "working"),
            )

            # Re-running is idempotent and does not error
            links2 = calibPipeIF.create_project_symlinks(ppmr_dir, working)
            self.assertEqual(len(links2), 3)

    def test_create_project_symlinks_flat_noop(self):
        with tempfile.TemporaryDirectory() as td:
            ppmr_dir = Path(td)
            working = ppmr_dir / "working"
            working.mkdir()

            links = calibPipeIF.create_project_symlinks(ppmr_dir, working)
            self.assertEqual(links, [])

    def test_link_weblog_picks_latest_and_relative(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ppmr_dir = root / "2016.2.00105.S_run1"
            working = ppmr_dir / "SOUS_uid___1" / "GOUS_uid___2" / "MOUS_uid___3" / "working"
            working.mkdir(parents=True)

            # Initially no weblog
            self.assertIsNone(calibPipeIF.link_weblog(ppmr_dir, working))

            # First weblog generated
            wl1 = working / "pipeline-20261001T100000" / "html"
            wl1.mkdir(parents=True)
            os.utime(wl1, (1000, 1000))

            link = calibPipeIF.link_weblog(ppmr_dir, working)
            self.assertIsNotNone(link)
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), wl1.resolve())
            target_str = os.readlink(link)
            self.assertFalse(os.path.isabs(target_str))
            self.assertIn("pipeline-20261001T100000", target_str)

            # Second (newer) weblog generated
            wl2 = working / "pipeline-20261001T120000" / "html"
            wl2.mkdir(parents=True)
            os.utime(wl2, (2000, 2000))

            link2 = calibPipeIF.link_weblog(ppmr_dir, working)
            self.assertIsNotNone(link2)
            self.assertEqual(link2.resolve(), wl2.resolve())
            self.assertIn("pipeline-20261001T120000", os.readlink(link2))

    def test_link_weblog_finds_in_products_if_not_in_working(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ppmr_dir = root / "2016.2.00105.S_run1"
            working = ppmr_dir / "SOUS_uid___1" / "GOUS_uid___2" / "MOUS_uid___3" / "working"
            working.mkdir(parents=True)
            products = working.parent / "products"
            wl_prod = products / "pipeline-20261001T150000" / "html"
            wl_prod.mkdir(parents=True)

            link = calibPipeIF.link_weblog(ppmr_dir, working)
            self.assertIsNotNone(link)
            self.assertEqual(link.resolve(), wl_prod.resolve())
            self.assertIn("products", os.readlink(link))

    def test_driver_parser_symlink_shortcuts_flags(self):
        p = calibPipeIF.build_parser()
        args_default = p.parse_args(["--mous=uid://A001/X1/X1"])
        self.assertIsNone(args_default.symlink_shortcuts)

        args_on = p.parse_args(["--mous=uid://A001/X1/X1", "--symlink-shortcuts"])
        self.assertTrue(args_on.symlink_shortcuts)

        args_off = p.parse_args(["--mous=uid://A001/X1/X1", "--no-symlink-shortcuts"])
        self.assertFalse(args_off.symlink_shortcuts)


if __name__ == "__main__":
    unittest.main()
