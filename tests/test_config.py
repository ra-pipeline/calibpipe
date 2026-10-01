"""Unit tests for calibpipe.config."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from calibpipe.config import (
    ConfigError,
    EnvSpec,
    build_environment,
    check_paths,
    find_config_path,
    format_shell_exports,
    load_config,
    resolve_batch_options,
    resolve_env,
    resolve_run_options,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONFIG = FIXTURES / "config.toml"


class TestConfigLoading(unittest.TestCase):
    def test_find_config_path_explicit(self):
        path = find_config_path(str(CONFIG))
        self.assertEqual(path, CONFIG.resolve())

    def test_find_config_path_missing_explicit_raises(self):
        with self.assertRaises(ConfigError):
            find_config_path("/nonexistent/config.toml")

    def test_find_config_path_env_var(self):
        with patch.dict(os.environ, {"CALIBPIPE_CONFIG": str(CONFIG)}):
            path = find_config_path()
            self.assertEqual(path, CONFIG.resolve())

    def test_load_config_valid(self):
        data = load_config(CONFIG)
        self.assertIn("default_env", data)
        self.assertEqual(data["default_env"], "main")
        self.assertIn("envs", data)

    def test_resolve_default_env(self):
        cfg = load_config(CONFIG)
        spec = resolve_env(cfg)
        self.assertIsInstance(spec, EnvSpec)
        self.assertEqual(spec.name, "main")
        self.assertEqual(spec.casa_root, "/fake/casa/main")

    def test_resolve_named_env(self):
        cfg = load_config(CONFIG)
        spec = resolve_env(cfg, "pl2025")
        self.assertEqual(spec.name, "pl2025")
        self.assertEqual(spec.casa_root, "/fake/casa/pl2025")

    def test_resolve_missing_env_raises(self):
        cfg = load_config(CONFIG)
        with self.assertRaises(ConfigError) as cm:
            resolve_env(cfg, "nonexistent")
        self.assertIn("Available", str(cm.exception))


class TestEnvironmentBuilding(unittest.TestCase):
    def test_build_environment(self):
        cfg = {
            "default_env": "main",
            "paths": {
                "scipipe_rootdir": "/tmp/scipipe_{user}_root",
                "scipipe_logdir": "/tmp/scipipe_{user}_logs",
            },
            "envs": {
                "main": {
                    "casa_root": "/stor/casa/main",
                }
            }
        }
        spec = resolve_env(cfg, "main")
        env = build_environment(cfg, spec, subdir="sub1")

        self.assertEqual(env["CASA_ROOT"], "/stor/casa/main")
        self.assertEqual(env["CASA_PATH"], "/stor/casa/main/bin")
        self.assertIn("/stor/casa/main/bin", env["PATH"])
        self.assertTrue(env["SCIPIPE_ROOTDIR"].endswith("/sub1"))

    def test_format_shell_exports(self):
        env = {
            "CASA_ROOT": "/opt/casa",
            "PATH": "/opt/casa/bin:/bin",
        }
        exports = format_shell_exports(env, export=True, only_keys=["CASA_ROOT"])
        self.assertEqual(exports, "export CASA_ROOT=/opt/casa")

        plain = format_shell_exports(env, export=False, only_keys=["CASA_ROOT"])
        self.assertEqual(plain, "CASA_ROOT=/opt/casa")

    def test_check_paths_warns_on_missing(self):
        env = {
            "CASA_ROOT": "/nonexistent/casa/path",
            "ACSROOT": "/nonexistent/pmr/path",
        }
        warnings = check_paths(env, strict=False)
        self.assertTrue(len(warnings) >= 2)
        self.assertTrue(any("CASA_ROOT" in w for w in warnings))
        self.assertTrue(any("pmr_home" in w for w in warnings))

    def test_check_paths_strict_raises(self):
        env = {
            "CASA_ROOT": "/nonexistent/casa/path",
        }
        with self.assertRaises(ConfigError) as cm:
            check_paths(env, strict=True)
        self.assertIn("CASA_ROOT", str(cm.exception))

    def test_check_paths_all_exist(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            env = {
                "CASA_ROOT": td,
                "ACSROOT": td,
                "DATAPACKER_HOME": td,
                "ACSDATA": td,
                "JAVA_HOME": td,
            }
            warnings = check_paths(env, strict=True)
            self.assertEqual(warnings, [])


class TestTypedModels(unittest.TestCase):
    def test_calibpipe_config_typed_and_dict_access(self):
        cfg = load_config(CONFIG)
        self.assertEqual(cfg.default_env, "main")
        self.assertEqual(cfg["default_env"], "main")
        self.assertTrue(cfg.site.use_custom_rcdir)
        self.assertTrue(cfg["site"]["use_custom_rcdir"])
        self.assertEqual(cfg.batch.queue, "plwg")
        self.assertEqual(cfg.run.recipe, "calimage")

    def test_resolve_run_options_precedence(self):
        import argparse
        cfg = load_config(CONFIG)

        # 1. Defaults when CLI flags are unset
        empty_opts = argparse.Namespace(mous="uid://A001/X1/X1")
        res1 = resolve_run_options(cfg, empty_opts)
        self.assertEqual(res1.mous, "uid://A001/X1/X1")
        self.assertEqual(res1.recipe, "calimage")
        self.assertEqual(res1.ncores, 8)
        self.assertTrue(res1.use_custom_rcdir)

        # 2. CLI overrides
        override_opts = argparse.Namespace(
            mous="uid://A001/X1/X1",
            recipe="image",
            ncores=16,
            custom_rcdir=False,
            useresume=True,
            loglevel="info",
        )
        res2 = resolve_run_options(cfg, override_opts)
        self.assertEqual(res2.recipe, "image")
        self.assertEqual(res2.ncores, 16)
        self.assertFalse(res2.use_custom_rcdir)
        self.assertTrue(res2.useresume)
        self.assertEqual(res2.loglevel, "info")

    def test_resolve_batch_options_precedence(self):
        import argparse
        cfg = load_config(CONFIG)

        # 1. Defaults when CLI args are unset
        default_args = argparse.Namespace(
            pipefile="test.txt", queue=None, cores=None, mem=None, node=None, mail_type=None
        )
        res1 = resolve_batch_options(cfg, default_args)
        self.assertEqual(res1.queue, "plwg")
        self.assertEqual(res1.cores, 8)
        self.assertEqual(res1.mem, 248)
        self.assertIsNone(res1.walltime)
        self.assertTrue(res1.no_requeue)

        # 2. CLI overrides
        override_args = argparse.Namespace(
            pipefile="test.txt",
            queue="batch2",
            cores=16,
            mem=512,
            node="2",
            mail_type="FAIL",
            extra_args=["--verbose"],
            walltime="12:00:00",
            nodelist="cvpost01",
            chdir="/lustre/work",
            cpus_per_task=4,
            mem_per_cpu="30G",
            hint="nomultithread",
            ntasks_per_core=1,
            distribution="cyclic:cyclic",
            no_requeue=False,
        )
        res2 = resolve_batch_options(cfg, override_args)
        self.assertEqual(res2.queue, "batch2")
        self.assertEqual(res2.cores, 16)
        self.assertEqual(res2.mem, 512)
        self.assertEqual(res2.node, "2")
        self.assertEqual(res2.mail_type, "FAIL")
        self.assertEqual(res2.extra_args, ["--verbose"])
        self.assertEqual(res2.walltime, "12:00:00")
        self.assertEqual(res2.nodelist, "cvpost01")
        self.assertEqual(res2.chdir, "/lustre/work")
        self.assertEqual(res2.cpus_per_task, 4)
        self.assertEqual(res2.mem_per_cpu, "30G")
        self.assertEqual(res2.hint, "nomultithread")
        self.assertEqual(res2.ntasks_per_core, 1)
        self.assertEqual(res2.distribution, "cyclic:cyclic")
        self.assertFalse(res2.no_requeue)

    def test_format_config_overview(self):
        from calibpipe.config import BatchConfig, format_config_overview
        cfg = load_config(CONFIG)
        overview = format_config_overview(cfg, env_name="main", config_path=CONFIG)
        self.assertIn("calibpipe Configuration Overview", overview)
        self.assertIn("Default Env:      main", overview)
        self.assertIn("Custom RCDIR:    True", overview)
        self.assertIn("Queue:           plwg", overview)

        # When optional batch directives are configured
        cfg.batch = BatchConfig(
            queue="batch2",
            cores=16,
            mem=64,
            walltime="24:00:00",
            cpus_per_task=2,
            mem_per_cpu="16G",
            nodelist="node01",
            chdir="/work",
            hint="nomultithread",
            ntasks_per_core=1,
            distribution="cyclic",
            no_requeue=False,
        )
        opt_overview = format_config_overview(cfg, env_name="main")
        self.assertIn("Walltime:        24:00:00", opt_overview)
        self.assertIn("CPUs/Task:       2", opt_overview)
        self.assertIn("16G/CPU", opt_overview)
        self.assertIn("Nodelist:        node01", opt_overview)
        self.assertIn("Working Dir:     /work", opt_overview)
        self.assertIn("Hint:            nomultithread", opt_overview)
        self.assertIn("Tasks/Core:      1", opt_overview)
        self.assertIn("Distribution:    cyclic", opt_overview)
        self.assertIn("Requeue:         True", opt_overview)


class TestCliConfigSubcommand(unittest.TestCase):
    def test_cli_config_show(self):
        import io
        from contextlib import redirect_stdout
        from calibpipe import cli

        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["config", "show", f"--config={CONFIG}", "--env=main"])
        out = buf.getvalue()
        self.assertIn("calibpipe Configuration Overview", out)
        self.assertIn("Selected Env:     main", out)


if __name__ == "__main__":
    unittest.main()
