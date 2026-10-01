"""Unit tests for calibpipe.config."""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from calibpipe import cli
from calibpipe.config import (
    ConfigError,
    EnvSpec,
    _deep_merge_dict,
    build_environment,
    check_paths,
    find_config_layers,
    find_config_path,
    find_site_config,
    find_user_configs,
    format_shell_exports,
    load_config,
    load_merged_config,
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
            },
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
            pipefile="test.txt",
            queue=None,
            cores=None,
            mem=None,
            node=None,
            mail_type=None,
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
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["config", "show", f"--config={CONFIG}", "--env=main"])
        out = buf.getvalue()
        self.assertIn("calibpipe Configuration Overview", out)
        self.assertIn("Selected Env:     main", out)


class TestPixiConfig(unittest.TestCase):
    def test_env_spec_pixi(self):
        spec = EnvSpec(name="modular", pixi_dir="/fake/pixi", pixi_env="casa674-py312")
        self.assertTrue(spec.is_pixi)
        self.assertEqual(spec.pixi_dir, "/fake/pixi")
        self.assertEqual(spec.pixi_env, "casa674-py312")
        self.assertEqual(spec.branch, "modular")

    def test_env_spec_validation_requires_target(self):
        with self.assertRaises(ConfigError) as cm:
            EnvSpec(name="invalid")
        self.assertIn("must specify either 'casa_root'", str(cm.exception))

    def test_resolve_pixi_env(self):
        cfg = {
            "default_env": "modular",
            "envs": {
                "modular": {
                    "pixi_dir": "/stor/pixi/workspace",
                    "pixi_env": "casa676-py312",
                }
            },
        }
        spec = resolve_env(cfg, "modular")
        self.assertTrue(spec.is_pixi)
        self.assertEqual(spec.pixi_dir, "/stor/pixi/workspace")
        self.assertEqual(spec.pixi_env, "casa676-py312")

    def test_build_environment_pixi(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            pixi_path = Path(td)
            pipe_dir = pixi_path / "pipeline"
            recipes_dir = pipe_dir / "recipes"
            recipes_dir.mkdir(parents=True)
            cfg = {
                "paths": {
                    "scipipe_rootdir": f"{td}/root",
                    "scipipe_logdir": f"{td}/logs",
                },
                "site": {
                    "pixi_bin": f"{td}/bin/pixi",
                },
                "envs": {
                    "modular": {
                        "pixi_dir": str(pixi_path),
                        "pixi_env": "default",
                    }
                },
            }
            spec = resolve_env(cfg, "modular")
            env = build_environment(cfg, spec, validate_paths=False)
            self.assertEqual(env["PIXI_DIR"], str(pixi_path))
            self.assertEqual(env["PIXI_ENV"], "default")
            self.assertEqual(env["SCIPIPE_HEURISTICS"], str(pixi_path))
            self.assertEqual(env["SCIPIPE_SCRIPTDIR"], str(recipes_dir))

    def test_check_paths_pixi(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            pixi_dir = Path(td) / "pixi_ws"
            pixi_dir.mkdir()
            fake_pixi = Path(td) / "pixi"
            fake_pixi.touch()
            fake_pixi.chmod(0o755)

            env = {
                "PIXI_DIR": str(pixi_dir),
                "ACSROOT": td,
                "DATAPACKER_HOME": td,
                "ACSDATA": td,
                "JAVA_HOME": td,
            }
            site_cfg = {"pixi_bin": str(fake_pixi)}
            warnings = check_paths(env, site_config=site_cfg, strict=True)
            self.assertEqual(warnings, [])

            # When pixi_dir does not exist
            env_missing = dict(env)
            env_missing["PIXI_DIR"] = "/nonexistent/pixi_ws"
            with self.assertRaises(ConfigError) as cm:
                check_paths(env_missing, site_config=site_cfg, strict=True)
            self.assertIn("pixi_dir", str(cm.exception))

    def test_format_config_overview_pixi(self):
        from calibpipe.config import CalibpipeConfig, format_config_overview

        cfg_dict = {
            "default_env": "modular",
            "site": {
                "pixi_bin": "/opt/pixi/bin/pixi",
            },
            "envs": {
                "modular": {
                    "pixi_dir": "/stor/pixi/modular",
                    "pixi_env": "py312",
                }
            },
        }
        cfg = CalibpipeConfig.from_dict(cfg_dict)
        overview = format_config_overview(cfg, env_name="modular")
        self.assertIn("Runtime:        Modular Pixi", overview)
        self.assertIn("Pixi Dir:       /stor/pixi/modular", overview)
        self.assertIn("Pixi Env:       py312", overview)
        self.assertIn("Pixi Bin:        /opt/pixi/bin/pixi", overview)


class TestCascadingConfig(unittest.TestCase):
    def test_deep_merge_dict(self):
        base = {
            "default_env": "main",
            "site": {"submit_host": "cluster01", "pixi_bin": "/opt/pixi"},
            "envs": {"main": {"casa_root": "/opt/casa"}},
        }
        overlay = {
            "default_env": "dev",
            "site": {"pixi_bin": "/user/pixi"},
            "envs": {"dev": {"casa_root": "/user/casa"}},
        }
        merged = _deep_merge_dict(base, overlay)
        self.assertEqual(merged["default_env"], "dev")
        self.assertEqual(merged["site"]["submit_host"], "cluster01")
        self.assertEqual(merged["site"]["pixi_bin"], "/user/pixi")
        self.assertEqual(merged["envs"]["main"]["casa_root"], "/opt/casa")
        self.assertEqual(merged["envs"]["dev"]["casa_root"], "/user/casa")

    def test_find_site_config_env_var(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            site_file = Path(td) / "site.toml"
            site_file.touch()
            with patch.dict(os.environ, {"CALIBPIPE_SITE_CONFIG": str(site_file)}):
                found = find_site_config()
                self.assertEqual(found, site_file.resolve())

            with patch.dict(os.environ, {"CALIBPIPE_SITE_CONFIG": str(Path(td) / "missing.toml")}):
                with self.assertRaises(ConfigError):
                    find_site_config()

    def test_find_config_layers_site_and_cli(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            site_file = Path(td) / "site.toml"
            site_file.touch()
            cli_file = Path(td) / "cli.toml"
            cli_file.touch()

            with patch.dict(os.environ, {"CALIBPIPE_SITE_CONFIG": str(site_file)}):
                layers = find_config_layers(cli_arg=str(cli_file))
                self.assertEqual(layers, [site_file.resolve(), cli_file.resolve()])

    def test_find_config_layers_no_site_flag(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            site_file = Path(td) / "site.toml"
            site_file.touch()
            cli_file = Path(td) / "cli.toml"
            cli_file.touch()

            with patch.dict(os.environ, {"CALIBPIPE_SITE_CONFIG": str(site_file)}):
                # When include_site is False
                layers = find_config_layers(cli_arg=str(cli_file), include_site=False)
                self.assertEqual(layers, [cli_file.resolve()])

                # When CALIBPIPE_NO_SITE_CONFIG is set
                with patch.dict(os.environ, {"CALIBPIPE_NO_SITE_CONFIG": "1"}):
                    layers2 = find_config_layers(cli_arg=str(cli_file))
                    self.assertEqual(layers2, [cli_file.resolve()])

    def test_find_user_configs_xdg_and_dot(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            xdg_dir = fake_home / ".config" / "calibpipe"
            xdg_dir.mkdir(parents=True)
            xdg_file = xdg_dir / "config.toml"
            xdg_file.touch()

            dot_dir = fake_home / ".calibpipe"
            dot_dir.mkdir(parents=True)
            dot_file = dot_dir / "config.toml"
            dot_file.touch()

            with patch("pathlib.Path.home", return_value=fake_home):
                with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(fake_home / ".config")}):
                    cfgs = find_user_configs()
                    self.assertEqual(cfgs, [xdg_file.resolve(), dot_file.resolve()])

    def test_load_config_cascading_merge(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            site_file = Path(td) / "site.toml"
            site_file.write_text("""
default_env = "main"

[site]
submit_host = "cluster01"
pmr_home = "/opt/pmr"

[batch]
queue = "plwg"
cores = 8

[paths]
scipipe_rootdir = "/site/root/{user}"
obscaldir = "/site/obscal"

[envs.main]
casa_root = "/opt/casa/main"
""")

            user_file = Path(td) / "user.toml"
            user_file.write_text("""
default_env = "custom_pixi"

[paths]
scipipe_rootdir = "/user/root/{user}"

[batch]
mail_type = "FAIL"

[envs.custom_pixi]
pixi_dir = "/user/ws"
""")

            cfg = load_config([site_file, user_file])
            self.assertEqual(cfg.default_env, "custom_pixi")
            # Inherited site tools
            self.assertEqual(cfg.site.submit_host, "cluster01")
            self.assertEqual(cfg.site.pmr_home, "/opt/pmr")
            # Inherited paths and overridden paths
            self.assertEqual(cfg.paths.obscaldir, "/site/obscal")
            self.assertEqual(cfg.paths.scipipe_rootdir, "/user/root/{user}")
            # Inherited and overridden batch directives
            self.assertEqual(cfg.batch.queue, "plwg")
            self.assertEqual(cfg.batch.cores, 8)
            self.assertEqual(cfg.batch.mail_type, "FAIL")
            # Additive environments
            self.assertIn("main", cfg.envs)
            self.assertIn("custom_pixi", cfg.envs)
            self.assertEqual(cfg.envs["main"].casa_root, "/opt/casa/main")
            self.assertEqual(cfg.envs["custom_pixi"].pixi_dir, "/user/ws")
            # Provenance tracking
            self.assertEqual(cfg.loaded_layers, [site_file.resolve(), user_file.resolve()])

    def test_load_merged_config(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            site_file = Path(td) / "site.toml"
            site_file.write_text("""
[site]
submit_host = "submit01"
[envs.main]
casa_root = "/opt/casa"
""")
            cli_file = Path(td) / "job.toml"
            cli_file.write_text("""
[paths]
scipipe_rootdir = "/tmp/root"
""")
            with patch.dict(os.environ, {"CALIBPIPE_SITE_CONFIG": str(site_file)}):
                cfg = load_merged_config(cli_arg=cli_file)
                self.assertEqual(cfg.site.submit_host, "submit01")
                self.assertEqual(cfg.paths.scipipe_rootdir, "/tmp/root")
                self.assertEqual(cfg.loaded_layers, [site_file.resolve(), cli_file.resolve()])

    def test_format_config_overview_multiple_layers(self):
        from calibpipe.config import CalibpipeConfig, format_config_overview

        cfg = CalibpipeConfig(
            default_env="main",
            loaded_layers=[Path("/etc/calibpipe/config.toml"), Path("/home/user/.config/calibpipe/config.toml")],
        )
        overview = format_config_overview(cfg)
        self.assertIn("Configuration Layers (lowest to highest priority):", overview)
        self.assertIn("[1] /etc/calibpipe/config.toml", overview)
        self.assertIn("[2] /home/user/.config/calibpipe/config.toml", overview)


class TestConfigExampleSchemaDrift(unittest.TestCase):
    """Ensure config.example.toml never drifts from the typed dataclass schema in config.py."""

    def test_config_example_covers_all_schema_fields(self):
        import dataclasses
        from calibpipe.config import (
            BatchConfig,
            CalibpipeConfig,
            EnvSpec,
            PathsConfig,
            RunConfig,
            SiteConfig,
        )

        example_path = Path(__file__).resolve().parent.parent / "config.example.toml"
        self.assertTrue(example_path.is_file(), f"config.example.toml not found at {example_path}")

        # 1. Ensure config.example.toml parses cleanly without syntax or type errors
        cfg = load_config(example_path)
        self.assertIsInstance(cfg, CalibpipeConfig)

        # 2. Ensure every field of every config model is documented in config.example.toml
        content = example_path.read_text(encoding="utf-8")
        missing_fields: list[str] = []
        for model_cls in (PathsConfig, SiteConfig, BatchConfig, RunConfig, EnvSpec):
            for f in dataclasses.fields(model_cls):
                if f.name in ("name", "extra_vars"):
                    continue
                if f.name not in content:
                    missing_fields.append(f"{model_cls.__name__}.{f.name}")

        self.assertEqual(
            missing_fields,
            [],
            f"The following schema fields are missing from config.example.toml: {missing_fields}. "
            "Please document them in config.example.toml to keep the template up-to-date.",
        )


if __name__ == "__main__":
    unittest.main()
