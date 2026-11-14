"""Configuration-first execution, compatibility boundaries, and batch transport."""

from __future__ import annotations

import os
import runpy
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from calibpipe import batch, cli, config, runner
from calibpipe.config import RunRequest, SubmitRequest


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CALIBPIPE_NO_SITE_CONFIG", "1")
    path = tmp_path / "config.toml"
    path.write_text("""default_env = "main"
[paths]
scipipe_rootdir = "root"
scipipe_logdir = "logs"
[envs.main]
casa_root = "/opt/casa/main"
[envs.other]
casa_root = "/opt/casa/other"
[profiles.science]
cmd = "print('science')"
nprocs = 3
cores = 6
cpus_per_task = 2
workdir = "science"
""")
    return path


def load(path):
    return config.load_merged_config(str(path), include_site=False)


def _parse_batch_command(command: str):
    """Parse a generated `calibpipe run --config=... --config-only ...` command."""
    tokens = [tok for tok in shlex.split(command) if tok != "\n"]
    run_idx = tokens.index("run")
    args = cli.build_parser().parse_args(tokens[run_idx:])
    req = cli.request(args, RunRequest)
    assert req.config_only is True
    assert req.config
    cfg = config.load_config(req.config)
    opts = config.resolve_run_options(cfg, req)
    return req, cfg, opts


@pytest.mark.parametrize(
    "flag, replacement",
    [
        ("--nprocs=4", "[run].nprocs"),
        ("--casa-ver=/opt/casa", "[envs.<name>].casa_root"),
        ("--no-backup", "[run].backup"),
    ],
)
def test_retired_run_flags_explain_replacement(flag, replacement, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", flag])
    assert exc.value.code == 2
    assert replacement in capsys.readouterr().err


@pytest.mark.parametrize(
    "fragment, expected",
    [
        ("[run]\nncores = 4", "nprocs"),
        ('[paths]\njava_home = "/opt/java"', "[site].java_home"),
        ('[site]\npixi_bin = "/opt/pixi"', "[paths].pixi_bin"),
        ("[site]\nlog2term = true", "[run].log2term"),
        ("[batches.debug]\ncores = 2", "[profiles.<name>]"),
        ("[profiles.nested.run]\nnprocs = 2", "flat"),
        ("[profiles.wrong]\nntasks = 2", "cores"),
        ('[envs.main]\ncasa_root="/opt/casa"\nUNKNOWN="value"', "extra_vars"),
        ('[run]\nprocedure = "procedure_hifa_calimage.xml"', "recipe"),
        ("[run]\nnprocs = true", "invalid value"),
        ("[run]\nunknown_option = true", "unknown_option"),
    ],
)
def test_native_toml_rejects_retired_unknown_and_invalid_keys(
    tmp_path, fragment, expected
):
    path = tmp_path / "invalid.toml"
    path.write_text(fragment)
    with pytest.raises(config.ConfigError) as exc:
        config.load_config(path)
    assert str(path) in str(exc.value)
    assert expected in str(exc.value)


def test_precedence_false_empty_and_resource_distinctions(configured):
    cfg = load(configured)
    cfg.run.backup = True
    cfg.run.ancillary = ["global.dat"]
    cfg.paths.rundata = ["/opt/measures"]
    cfg.profiles["science"].backup = False
    cfg.profiles["science"].rundata = []
    cfg.profiles["science"].ancillary = []
    run = config.resolve_run_options(cfg, RunRequest(profile="science", verbose=False))
    submit = config.resolve_batch_options(cfg, SubmitRequest(profile="science"))
    assert (run.nprocs, submit.cores, submit.cpus_per_task) == (3, 6, 2)
    assert run.backup is False
    assert run.rundata == [] and run.rundata_specified
    assert run.ancillary == []
    assert config.resolve_run_options(
        cfg, RunRequest(profile="science", overrides={"backup": True})
    ).backup


def test_config_sources_and_explicit_file_layering(tmp_path, monkeypatch):
    site = tmp_path / "site.toml"
    explicit = tmp_path / "selected.toml"
    site.write_text("[run]\nnprocs = 2\nbackup = true\nlog2term = true\n")
    explicit.write_text("[run]\nnprocs = 5\n[profiles.science]\nbackup = false\n")
    (tmp_path / "config.toml").write_text("[run]\nnprocs = 99\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CALIBPIPE_SITE_CONFIG", str(site))
    monkeypatch.delenv("CALIBPIPE_NO_SITE_CONFIG", raising=False)
    cfg = config.load_merged_config(str(explicit))
    assert cfg.loaded_layers == [site, explicit]
    assert config.resolve_run_options(cfg, RunRequest(profile="science")).nprocs == 5
    shown = config.format_config_overview(cfg, profile="science")
    assert "backup = False" in shown
    assert f"source: {explicit}" in shown
    assert f"source: {site}" in shown


@pytest.mark.parametrize(
    "target",
    [
        {"mous": "uid://A001/X1/X1"},
        {"vis": ["dataset.ms"]},
        {"script": "script.py"},
        {"cmd": "print('hello')"},
        {"ppr": "request.xml"},
        {"interactive": True},
        {"mous": "uid://A001/X1/X1", "ppr": "override.xml"},
    ],
)
def test_native_targets_reach_shared_executor(configured, target):
    with patch.object(runner, "execute", return_value=0) as execute:
        assert (
            runner.run_request(RunRequest(config=str(configured), profile="science", **target))
            == 0
        )
    resolved = execute.call_args.args[1]
    for name, value in target.items():
        assert getattr(resolved, name) == value


def test_conflicts_and_missing_targets_rejected(configured):
    with pytest.raises(config.ConfigError, match="Conflicting"):
        runner.run_request(
            RunRequest(config=str(configured), cmd="print(1)", script="script.py")
        )
    with pytest.raises(config.ConfigError, match="No execution target"):
        runner.run_request(RunRequest(config=str(configured)))


def test_moved_settings_resolve_from_toml(configured):
    configured.write_text(
        configured.read_text()
        + """[run]
flag_dir = "reuse"
subdir = "extra"
onlysemipass = "uid://A002/X1/X2"
[batch]
outfile = "stdout.log"
errfile = "stderr.log"
"""
    )
    cfg = load(configured)
    run = config.resolve_run_options(cfg, RunRequest(profile="science"))
    submit = config.resolve_batch_options(cfg, SubmitRequest(profile="science"))
    assert (run.flag_dir, run.subdir, run.onlysemipass) == (
        "reuse",
        "extra",
        "uid://A002/X1/X2",
    )
    assert (submit.outfile, submit.errfile) == ("stdout.log", "stderr.log")


def test_explicit_extra_vars_are_exported(configured):
    configured.write_text(
        configured.read_text()
        + """[envs.main.extra_vars]
CUSTOM_SETTING = "explicit"
"""
    )
    cfg = load(configured)
    env = config.build_environment(cfg, config.resolve_env(cfg), validate_paths=False)
    assert env["CUSTOM_SETTING"] == "explicit"
    assert "extra_vars" not in env


def test_job_snapshot_matches_local_resolution_and_survives_config_edits(
    configured, tmp_path, monkeypatch
):
    cfg = load(configured)
    options = config.resolve_run_options(cfg, RunRequest(profile="science"))
    monkeypatch.setenv("PRIVATE_TEST_SENTINEL", "must-not-be-snapshotted")
    snap_path = tmp_path / "batch.snapshot.toml"
    snap_text = config.dump_toml(config.build_batch_snapshot(cfg, options))
    assert "must-not-be-snapshotted" not in snap_text
    snap_path.write_text(snap_text)

    # Corrupt original config and add a conflicting CWD config.toml; --config-only ignores both.
    configured.write_text("not valid TOML anymore")
    other_cwd = tmp_path / "worker-cwd"
    other_cwd.mkdir()
    (other_cwd / "config.toml").write_text('default_env = "other"\n[envs.other]\ncasa_root = "/wrong"\n')
    monkeypatch.chdir(other_cwd)
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    with patch.object(runner, "execute", return_value=0) as execute:
        cli.main(["run", f"--config={snap_path}", "--config-only", "-p", "science"])
    frozen, actual = execute.call_args.args
    assert actual == options
    assert frozen.envs[actual.env_name].casa_root == "/opt/casa/main"
    assert os.environ["SLURM_JOB_ID"] == "12345"


def test_batch_command_is_literal_shell_data(configured, tmp_path):
    cfg = load(configured)
    marker = Path.cwd() / "shell-must-not-run"
    dangerous = f"print(\"$(touch {marker}); `touch {marker}`; $HOME; 'quoted'\")\nprint('second line')"
    run_req = RunRequest(cmd=dangerous)
    options = config.resolve_run_options(cfg, run_req)
    snap_path = tmp_path / "batch.snapshot.toml"
    snap_path.write_text(config.dump_toml(config.build_batch_snapshot(cfg, options)))
    command = batch._build_batch_command(
        snapshot_path=snap_path,
        run_request=run_req,
        run_options=options,
        isolate_root=False,
        cwd=Path.cwd(),
    )
    req, _, parsed_opts = _parse_batch_command(command)
    assert req.cmd == dangerous
    assert parsed_opts.cmd == dangerous
    # Execute the shell-quoted continuation with a harmless argv dumper via sh -c.
    after_run = command.split(" run", 1)[1]
    probe = f"{shlex.quote(sys.executable)} -c 'import json,sys; print(json.dumps(sys.argv[1:]))' {after_run}"
    result = subprocess.run(
        ["sh", "-c", probe], check=True, capture_output=True, text=True
    )
    import json

    argv_out = json.loads(result.stdout)
    assert f"--cmd={dangerous}" in argv_out
    assert not marker.exists()


@pytest.mark.parametrize("scheduler", ["slurm", "htcondor"])
def test_native_dry_submission_saves_self_contained_scripts(configured, scheduler):
    configured.write_text(
        configured.read_text()
        + f'''[batch]
scheduler = "{scheduler}"
pool = "collector.example.test"
schedd = "scheduler.example.test"
'''
    )
    with patch.object(batch.subprocess, "run") as submit:
        assert (
            batch.submit_request(
                SubmitRequest(config=str(configured), profile="science", dry_run=True)
            )
            == 0
        )
    submit.assert_not_called()
    snapshots = list(Path.cwd().rglob("batch.*.toml"))
    assert len(snapshots) == 1
    snap_text = snapshots[0].read_text()
    assert "nprocs = 3" in snap_text

    suffix = "*.sbatch" if scheduler == "slurm" else "*.sh"
    scripts = list(Path.cwd().rglob(suffix))
    assert len(scripts) == 1
    text = scripts[0].read_text()
    assert "--config-only" in text
    assert str(snapshots[0]) in text
    assert "--nprocs=" not in text
    if scheduler == "slurm":
        assert "#SBATCH --ntasks=6" in text
        assert "#SBATCH --cpus-per-task=2" in text
    else:
        htc = next(Path.cwd().rglob("*.htc")).read_text()
        assert "request_cpus = 6" in htc


def test_native_collection_isolation_throttle_report_and_env_override(configured):
    configured.write_text(
        configured.read_text()
        + """[profiles.tests]
collect = true
with_report = true
max_concurrent = 1
workdir = "test-output"
env = "other"
test_opts = "--tb=short"
"""
    )
    calls = []

    def dispatch(command, name, opts, user, dependency=None):
        calls.append((command, opts, dependency))
        return str(len(calls))

    with (
        patch.object(
            batch.collect,
            "collect_tests",
            return_value=["tests/a.py::test_a", "tests/a.py::test_b"],
        ),
        patch.object(batch, "_dispatch_job", side_effect=dispatch),
        patch.object(batch.time, "sleep"),
    ):
        assert (
            batch.submit_request(
                SubmitRequest(config=str(configured), profile="tests", env="main")
            )
            == 0
        )
    assert len(calls) == 3
    req0, _, opts0 = _parse_batch_command(calls[0][0])
    req1, _, opts1 = _parse_batch_command(calls[1][0])
    assert req0.isolate_root and req1.isolate_root
    assert req0.config == req1.config
    assert opts0.env_name == "main"
    assert opts0.workdir != opts1.workdir
    assert "--tb=short" in opts0.cmd
    assert calls[1][2] == "afterany:1"
    assert calls[2][2] == "afterany:1:2"
    assert " report " in calls[2][0]


def test_runfile_dataset_and_mous_use_shared_resolution(configured, tmp_path):
    runfile = tmp_path / "jobs.run"
    runfile.write_text('uid://A001/X1/X1 cal\n"data with spaces.ms" image\n')
    calls = []
    with (
        patch.object(
            batch,
            "_dispatch_job",
            side_effect=lambda command, *args, **kw: (
                calls.append(_parse_batch_command(command)) or "123"
            ),
        ),
        patch.object(batch.time, "sleep"),
    ):
        batch.submit_request(
            SubmitRequest(
                config=str(configured), profile="science", pipefile=str(runfile)
            )
        )
    req0, _, opts0 = calls[0]
    req1, _, opts1 = calls[1]
    assert req0.config == req1.config
    assert opts0.mous == "uid://A001/X1/X1"
    assert opts0.recipe == "cal"
    assert opts1.vis == [str(Path.cwd() / "data with spaces.ms")]
    assert opts1.recipe == "image"
    assert opts0.nprocs == opts1.nprocs == 3


def test_interactive_batch_and_config_only_without_config_rejected(configured):
    configured.write_text(
        configured.read_text()
        + """[profiles.interactive]
interactive = true
"""
    )
    with pytest.raises(config.ConfigError, match="interactive"):
        batch.submit_request(SubmitRequest(config=str(configured), profile="interactive"))
    with pytest.raises(config.ConfigError, match="--config-only requires --config"):
        runner.run_request(RunRequest(config_only=True))


@pytest.mark.parametrize(
    "script, expected", [("calibPipeIF.py", "--mous"), ("runbatch.py", "pipefile")]
)
def test_legacy_script_help_remains_available(script, expected):
    path = Path(__file__).resolve().parent.parent / "scripts" / script
    result = subprocess.run(
        [sys.executable, str(path), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert expected in result.stdout


def test_sourced_shell_shim_accepts_legacy_config(tmp_path):
    config_path = tmp_path / "env_config.toml"
    config_path.write_text("""default_env="main"
[paths]
scipipe_rootdir="/scratch/run"
scipipe_logdir="/scratch/logs"
[envs.main]
casa_root="/opt/casa/main"
""")
    script = Path(__file__).resolve().parent.parent / "scripts/calibpipe_env.sh"
    process_env = dict(os.environ)
    process_env.update(CALIBPIPE_PYTHON=sys.executable, CALIBPIPE_SKIP_PATH_CHECK="1")
    result = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1" --config "$2" --no-site-config --env main && printf "%s\\n" "$CASA_ROOT"',
            "shell-test",
            str(script),
            str(config_path),
        ],
        env=process_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/opt/casa/main"


def test_native_executor_applies_false_profile_logging_and_worker_path_checks(
    configured, tmp_path, monkeypatch
):
    # Preserve every inherited variable while isolating executor mutations to this test.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.delenv("CALIBPIPE_SKIP_PATH_CHECK", raising=False)
    cfg = load(configured)
    cfg.run.log2term = True
    cfg.profiles["science"].log2term = False
    cfg.profiles["science"].dry_run = True
    options = config.resolve_run_options(cfg, RunRequest(profile="science"))
    Path(options.workdir).mkdir(parents=True)
    snap_path = tmp_path / "batch.snapshot.toml"
    snap_path.write_text(
        config.dump_toml(config.build_batch_snapshot(cfg, options, cwd=tmp_path))
    )
    monkeypatch.setenv("SCIPIPE_ROOTDIR", "/inherited/root")
    with patch.object(config, "check_paths") as paths:
        cli.main(
            [
                "run",
                f"--config={snap_path}",
                "--config-only",
                "-p",
                "science",
                "--isolate-root",
            ]
        )
    paths.assert_called_once()
    assert "SCIPIPE_ROOTDIR" not in os.environ
    casa_config = next(Path.cwd().rglob(".casa/config.py")).read_text()
    assert "log2term = False" in casa_config


def test_default_profile_submit_and_explicit_boolean_controls(configured):
    configured.write_text('default_profile="science"\n' + configured.read_text())
    cfg = load(configured)
    cfg.profiles["science"].verbose = True
    cfg.profiles["science"].dry_run = True
    request = cli.request(
        cli.build_parser().parse_args(
            ["run", "--no-verbose", "--no-dry-run", "--config", str(configured)]
        ),
        RunRequest,
    )
    options = config.resolve_run_options(cfg, request)
    assert options.profile == "science"
    assert options.verbose is False and options.dry_run is False
    assert config.resolve_batch_options(cfg, SubmitRequest()).profile == "science"


def test_worker_isolates_test_root_without_losing_scheduler_environment(
    configured, tmp_path, monkeypatch
):
    cfg = load(configured)
    options = config.resolve_run_options(cfg, RunRequest(profile="science"))
    Path(options.workdir).mkdir(parents=True)
    snap_path = tmp_path / "batch.snapshot.toml"
    snap_path.write_text(
        config.dump_toml(config.build_batch_snapshot(cfg, options, cwd=options.workdir))
    )
    monkeypatch.setenv("SLURM_JOB_ID", "5678")
    with patch.object(runner, "execute", return_value=0) as execute:
        cli.main(
            [
                "run",
                f"--config={snap_path}",
                "--config-only",
                "-p",
                "science",
                "--isolate-root",
            ]
        )
    frozen, actual = execute.call_args.args
    assert execute.call_args.kwargs["isolate_root"] is True
    assert frozen.paths.scipipe_rootdir
    assert actual.workdir
    assert os.environ["SLURM_JOB_ID"] == "5678"


def test_scripts_forward_to_native_commands(configured):
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    argv_common = ["--config", str(configured), "-p", "science", "--dry-run"]

    with (
        patch.object(sys, "argv", [str(scripts_dir / "calibPipeIF.py"), *argv_common]),
        patch.object(runner, "run_request", return_value=0) as mock_run,
    ):
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(scripts_dir / "calibPipeIF.py"), run_name="__main__")
        assert exc.value.code in (0, None)
        mock_run.assert_called_once_with(
            RunRequest(config=str(configured), profile="science", dry_run=True)
        )

    with (
        patch.object(sys, "argv", [str(scripts_dir / "runbatch.py"), *argv_common]),
        patch.object(batch, "submit_request", return_value=0) as mock_submit,
    ):
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(scripts_dir / "runbatch.py"), run_name="__main__")
        assert exc.value.code in (0, None)
        mock_submit.assert_called_once_with(
            SubmitRequest(config=str(configured), profile="science", dry_run=True)
        )


def test_installed_shell_launcher_forwards_to_calibpipe_env(tmp_path):
    config_path = tmp_path / "env_launcher.toml"
    config_path.write_text("""default_env="main"
[paths]
scipipe_rootdir="/scratch/run"
scipipe_logdir="/scratch/logs"
[envs.main]
casa_root="/opt/casa/main"
""")
    root = Path(__file__).resolve().parent.parent
    launcher = tmp_path / "calibpipe"
    launcher.write_text(
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' -m calibpipe.cli "$@"\n'
    )
    launcher.chmod(0o755)
    process_env = dict(os.environ)
    process_env.pop("CALIBPIPE_PYTHON", None)
    process_env.update(
        PATH=str(tmp_path) + os.pathsep + process_env.get("PATH", ""),
        PYTHONPATH=str(root / "src"),
        CALIBPIPE_SKIP_PATH_CHECK="1",
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1" --config "$2" --no-site-config --env main && printf "%s\\n" "$CASA_ROOT"',
            "shell-test",
            str(root / "scripts/calibpipe_env.sh"),
            str(config_path),
        ],
        env=process_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/opt/casa/main"


def test_batch_snapshot_formats_pixi_dir_and_preserves_user_templates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""default_env = "pixi"
[paths]
scipipe_rootdir = "/lustre/runs/{user}"
scipipe_logdir = "{user}/logs"
casarundata = "/opt/{user}/rundata"
obscaldir = "/opt/{user}/obscal"
[envs.pixi]
pixi_dir = "pixi-checkout"
heuristics_dir = "{pixi_dir}/pipeline"
[profiles.pixi_run]
env = "pixi"
cmd = "print('pixi')"
""")
    cfg = config.load_merged_config(str(cfg_path), include_site=False)
    options = config.resolve_run_options(cfg, RunRequest(profile="pixi_run"))
    snapshot = config.build_batch_snapshot(cfg, options, cwd="pixi-workdir")
    assert snapshot["paths"]["scipipe_rootdir"] == "/lustre/runs/{user}"
    assert snapshot["paths"]["scipipe_logdir"] == "{user}/logs"
    assert snapshot["paths"]["casarundata"] == "/opt/{user}/rundata"
    assert snapshot["paths"]["obscaldir"] == "/opt/{user}/obscal"
    assert snapshot["envs"]["pixi"]["heuristics_dir"] == str(
        (tmp_path / "pixi-checkout" / "pipeline").resolve()
    )


def test_profile_save_runfile_resolved_and_written(configured, tmp_path):
    runfile_out = tmp_path / "collected.run"
    configured.write_text(
        configured.read_text()
        + f"""[profiles.tests]
collect = true
workdir = "test-output"
save_runfile = "{runfile_out}"
"""
    )
    cfg = load(configured)
    resolved = config.resolve_batch_options(cfg, SubmitRequest(profile="tests"))
    assert resolved.save_runfile == str(runfile_out)
    with (
        patch.object(
            batch.collect,
            "collect_tests",
            return_value=["tests/a.py::test_a"],
        ),
        patch.object(batch, "_dispatch_job", return_value="101"),
        patch.object(batch, "print_queue"),
    ):
        assert batch.submit_request(SubmitRequest(config=str(configured), profile="tests")) == 0
    assert runfile_out.is_file()
    assert "tests/a.py::test_a" in runfile_out.read_text()


@pytest.mark.parametrize(
    "flag, expected",
    [
        ("--no-valid", "no replacement (retired historical no-op flag)"),
        ("--no-pickle", "no replacement (retired historical no-op flag)"),
        ("--no-ebwfile", "no replacement (retired historical no-op flag)"),
        ("--no-legacy", "no replacement (retired historical no-op flag)"),
        ("--no-usegetalmaflux", "no replacement (retired historical no-op flag)"),
        ("--no-noupdate", "no replacement (retired historical no-op flag)"),
        ("--no-semipass", "no replacement (retired historical no-op flag)"),
        ("--obscaldir=/tmp/obs", "[paths].obscaldir or [profiles.<name>].obscaldir"),
        ("--no-obscaldir", "[paths].obscaldir or [profiles.<name>].obscaldir"),
    ],
)
def test_retired_noop_negations_and_obscaldir_guidance(flag, expected, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", flag])
    assert exc.value.code == 2
    assert expected in capsys.readouterr().err


def test_submit_counts_report_job_propagates_no_site_config_and_prints_queue(
    configured, capsys
):
    configured.write_text(
        configured.read_text()
        + """[profiles.tests]
collect = true
with_report = true
workdir = "test-output"
"""
    )
    calls = []

    def dispatch(command, name, opts, user, dependency=None):
        calls.append((command, name, opts, dependency))
        return str(len(calls))

    with (
        patch.object(
            batch.collect,
            "collect_tests",
            return_value=["tests/a.py::test_a"],
        ),
        patch.object(batch, "_dispatch_job", side_effect=dispatch),
        patch.object(batch, "print_queue") as mock_print_queue,
        patch.object(batch.time, "sleep"),
    ):
        rc = batch.submit_request(
            SubmitRequest(config=str(configured), profile="tests", no_site_config=True)
        )
    assert rc == 0
    assert len(calls) == 2
    assert "CALIBPIPE_NO_SITE_CONFIG=1 " in calls[1][0]
    assert " report " in calls[1][0]
    assert "Submitted 2 job(s)." in capsys.readouterr().out
    mock_print_queue.assert_called_once()


def test_recipe_cli_override_in_run_and_submit(configured):
    with patch.object(runner, "execute", return_value=0) as execute:
        cli.main(
            [
                "run",
                "--config",
                str(configured),
                "--no-site-config",
                "--mous",
                "uid://A001/X1/X1",
                "--recipe",
                "cal",
            ]
        )
    assert execute.call_args.args[1].recipe == "cal"

    configured.write_text(
        configured.read_text() + '\n[profiles.mous_prof]\nmous = "uid://A001/X1/X1"\nrecipe = "calimage"\n'
    )
    calls = []
    with (
        patch.object(batch, "_dispatch_job", side_effect=lambda *a, **kw: calls.append(a[0]) or "1"),
        patch.object(batch, "print_queue"),
    ):
        cli.main(
            [
                "submit",
                "--config",
                str(configured),
                "--no-site-config",
                "-p",
                "mous_prof",
                "--recipe",
                "cal",
            ]
        )
    assert _parse_batch_command(calls[0])[2].recipe == "cal"


def test_recipe_smart_xml_resolution_for_mous_and_snapshot(
    configured, tmp_path, monkeypatch, capsys
):
    from calibpipe import config as envconfig

    assert envconfig.resolve_recipe_procedure("calimage") == "procedure_hifa_calimage.xml"
    assert envconfig.resolve_recipe_procedure("procedure_custom.xml") == "procedure_custom.xml"
    assert envconfig.resolve_recipe_procedure("PIPE-3250/scripts/proc.xml") == "PIPE-3250/scripts/proc.xml"

    # Built-in XML filename and shorthands stay relative in batch snapshot;
    # relative paths with "/" are absolutized before scheduler cwd changes.
    cfg = load(configured)
    builtin_opts = envconfig.resolve_run_options(
        cfg,
        RunRequest(mous="uid://A001/X1/X1", recipe="procedure_custom.xml", dry_run=True),
    )
    assert (
        envconfig.freeze_recipe_path(
            builtin_opts.recipe, workspaces=builtin_opts.workspaces, cwd=tmp_path
        )
        == "procedure_custom.xml"
    )

    local_dir = tmp_path / "sub"
    local_dir.mkdir()
    local_xml = local_dir / "custom.xml"
    local_xml.write_text("<procedure/>")
    monkeypatch.chdir(tmp_path)
    path_opts = envconfig.resolve_run_options(
        cfg, RunRequest(vis=["data.ms"], recipe="sub/custom.xml")
    )
    assert (
        envconfig.freeze_recipe_path(
            path_opts.recipe, workspaces=path_opts.workspaces, cwd=tmp_path
        )
        == str(local_xml.resolve())
    )

    # MOUS mode preserves XML filenames instead of wrapping them in procedure_hifa_*.xml
    monkeypatch.setenv("CALIBPIPE_SKIP_PATH_CHECK", "1")
    runner.execute(cfg, builtin_opts)
    out = capsys.readouterr().out
    assert "intents_hifa.xml procedure_custom.xml" in out


def test_snapshot_preserves_relative_logfile_and_ancillary_destinations(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("a")
    (ws / "c.txt").write_text("c")
    (tmp_path / "casa").mkdir()

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """default_env = "main"
[paths]
workspaces = ["ws"]
[envs.main]
casa_root = "casa"
[run]
logfile = "casa-custom.log"
[profiles.p]
cmd = "print('ok')"
ancillary = [["a.txt", "dest/a.txt"], "b.txt:dest/b.txt"]
"""
    )
    cfg = load(cfg_path)
    opts = config.resolve_run_options(cfg, RunRequest(profile="p"))

    snap_toml = config.build_batch_snapshot(cfg, opts, cwd=tmp_path)
    assert snap_toml["run"]["logfile"] == "casa-custom.log"
    assert snap_toml["profiles"]["p"]["ancillary"] == [
        [str((ws / "a.txt").resolve()), "dest/a.txt"],
        [str((tmp_path / "b.txt").resolve()), "dest/b.txt"],
    ]

    snap_override = config.build_batch_snapshot(
        cfg,
        opts,
        cwd=tmp_path,
        overrides={
            "ancillary": [
                "b.txt:dest/b.txt",
                {"src": "c.txt", "dest": "dest/c.txt"},
            ]
        },
    )
    assert snap_override["profiles"]["p"]["ancillary"] == [
        f"{(tmp_path / 'b.txt').resolve()}:dest/b.txt",
        {"src": str((ws / "c.txt").resolve()), "dest": "dest/c.txt"},
    ]


def test_snapshot_respects_profile_workspaces_and_scipipe_rootdir_overrides(tmp_path):
    prof_ws = tmp_path / "prof_ws"
    (prof_ws / "casa_rel").mkdir(parents=True)
    (prof_ws / "heur_rel").mkdir(parents=True)

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """default_env = "main"
[paths]
scipipe_rootdir = ""
[envs.main]
casa_root = "casa_rel"
heuristics_dir = "heur_rel"
[profiles.p]
workspaces = ["prof_ws"]
scipipe_rootdir = "prof_root"
workdir = "rel_job"
cmd = "print(1)"
"""
    )
    cfg = load(cfg_path)
    opts = config.resolve_run_options(cfg, RunRequest(profile="p"))
    snapshot = config.build_batch_snapshot(cfg, opts, cwd=tmp_path)

    assert snapshot["envs"]["main"]["casa_root"] == str((prof_ws / "casa_rel").resolve())
    assert snapshot["envs"]["main"]["heuristics_dir"] == str((prof_ws / "heur_rel").resolve())
    assert snapshot["profiles"]["p"]["scipipe_rootdir"] == str((tmp_path / "prof_root").resolve())
    assert snapshot["profiles"]["p"]["workdir"] == "rel_job"


def test_profile_vis_single_string_and_optional_display(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """default_env = "main"
[envs.main]
casa_root = "/opt/casa"
[profiles.single]
vis = "foo.ms"
[profiles.mous_only]
mous = "uid://A001/X1/X1"
"""
    )
    cfg = load(cfg_path)
    assert cfg.profiles["single"].vis == ["foo.ms"]
    assert cfg.profiles["mous_only"].vis is None
    assert "vis" in config.format_profile_details(cfg, "single")
    assert "vis" not in config.format_profile_details(cfg, "mous_only")


def test_submit_report_job_uses_calibpipe_argv_handles_pixi_and_rejects_htcondor(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    htc_opts = config.ResolvedBatchOptions(
        pipefile=None,
        env_name="main",
        partition="plwg",
        cores=1,
        mem=4,
        node=None,
        mail_type="FAIL",
        profile="p",
        scheduler="htcondor",
        dry_run=True,
        with_report=True,
        logdir=str(tmp_path),
    )
    with pytest.raises(config.ConfigError, match="scheduler='slurm'"):
        batch._submit_report_job(
            base_workdir=tmp_path,
            job_ids=["123"],
            batch_opts=htc_opts,
            profile=None,
            cfg=config.CalibpipeConfig(),
            args=SimpleNamespace(),
            user="testuser",
        )

    runfile = tmp_path / "tests.txt"
    runfile.write_text("tests/test_fast.py::test_example\n")
    htc_cfg = tmp_path / "htc.toml"
    htc_cfg.write_text(
        f"""default_env = "main"
[envs.main]
casa_root = "/opt/casa"
[profiles.p]
scheduler = "htcondor"
with_report = true
logdir = "{tmp_path}"
cmd = "print(1)"
"""
    )
    with pytest.raises(config.ConfigError, match="scheduler='slurm'"):
        batch.submit_request(
            SubmitRequest(
                config=str(htc_cfg),
                no_site_config=True,
                profile="p",
                pipefile=str(runfile),
                dry_run=True,
            )
        )

    ws = tmp_path / "ws"
    casa = ws / "casa_rel"
    pixi = ws / "pixi_rel"
    casa.mkdir(parents=True)
    pixi.mkdir(parents=True)
    cfg_path = tmp_path / "report.toml"
    cfg_path.write_text(
        f"""default_env = "mono"
[paths]
workspaces = ["{ws}"]
[envs.mono]
casa_root = "casa_rel"
[envs.pixi]
pixi_dir = "pixi_rel"
"""
    )
    cfg = load(cfg_path)
    dispatched: list[tuple[str, config.ResolvedBatchOptions]] = []
    monkeypatch.setattr(batch, "_calibpipe_argv", lambda: ["/custom/bin/calibpipe"])
    monkeypatch.setattr(
        batch,
        "_dispatch_job",
        lambda pipejob, job_name_val, batch_opts, user, dependency=None: (
            dispatched.append((pipejob, batch_opts)) or "999"
        ),
    )

    slurm_opts = config.ResolvedBatchOptions(
        pipefile=None,
        env_name="mono",
        partition="plwg",
        cores=1,
        mem=4,
        node=None,
        mail_type="FAIL",
        profile="p",
        scheduler="slurm",
        dry_run=True,
        logdir=str(tmp_path),
    )
    batch._submit_report_job(
        base_workdir=tmp_path,
        job_ids=["111"],
        batch_opts=slurm_opts,
        profile=config.ProfileConfig(env="mono"),
        cfg=cfg,
        args=SimpleNamespace(),
        user="testuser",
    )
    mono_cmd, mono_opts = dispatched.pop(0)
    assert mono_cmd.startswith(f"/custom/bin/calibpipe report {tmp_path}")
    assert f"--casa-root={casa.resolve()}" in mono_cmd
    assert getattr(mono_opts, "pixi_dir", None) is None

    batch._submit_report_job(
        base_workdir=tmp_path,
        job_ids=["222"],
        batch_opts=slurm_opts,
        profile=config.ProfileConfig(env="pixi"),
        cfg=cfg,
        args=SimpleNamespace(),
        user="testuser",
    )
    pixi_cmd, pixi_opts = dispatched.pop(0)
    assert pixi_cmd.startswith(f"/custom/bin/calibpipe report {tmp_path}")
    assert "--casa-root=" not in pixi_cmd
    assert getattr(pixi_opts, "pixi_dir", None) == str(pixi.resolve())


def test_run_mous_returns_one_on_missing_flag_dir_or_ppr(tmp_path, capsys):
    env_spec = config.EnvSpec(name="main", casa_root="/opt/casa")
    env = {"CASA_ROOT": "/opt/casa"}

    rc_flag = runner._run_mous(
        config.ResolvedRunOptions(
            mous="uid://A001/X1/X1",
            flag_dir=str(tmp_path / "missing_flags"),
        ),
        env_spec,
        env,
        "/opt/casa",
        "/opt/casa/bin/casa",
        {},
    )
    assert rc_flag == 1
    assert "flag_dir" in capsys.readouterr().err

    rc_ppr = runner._run_mous(
        config.ResolvedRunOptions(
            mous="uid://A001/X1/X1",
            ppr=str(tmp_path / "missing_PPR.xml"),
        ),
        env_spec,
        env,
        "/opt/casa",
        "/opt/casa/bin/casa",
        {},
    )
    assert rc_ppr == 1
    assert "ppr (--ppr)" in capsys.readouterr().err


def test_retired_hints_accurate_and_no_spurious_negations():
    assert cli.RETIRED["--no-batch-profile"] == "--profile"
    assert cli.RETIRED["--no-extra-arg"] == cli.RETIRED["--extra-arg"]
    assert "retired:" not in cli.RETIRED["--no-extra-arg"].lower()
    assert cli.RETIRED["--no-obscaldir"] == "[paths].obscaldir or [profiles.<name>].obscaldir"
    for bad in (
        "--no-no-batch-profile",
        "--no-no-PPR",
        "--no-no-extra-arg",
        "--no-nprocs",
        "--no-cores",
        "--no-casa-root",
    ):
        assert bad not in cli.RETIRED




