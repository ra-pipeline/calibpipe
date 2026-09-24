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
    find_config_path,
    format_shell_exports,
    load_config,
    resolve_env,
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


if __name__ == "__main__":
    unittest.main()
