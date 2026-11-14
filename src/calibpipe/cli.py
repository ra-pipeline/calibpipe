"""Unified command-line interface for calibpipe."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import fields

import calibpipe
from calibpipe import batch, collect, coverage, pmr, report, runner, update_ref
from calibpipe import config as envconfig
from calibpipe.config import RunRequest, SubmitRequest

RETIRED_GROUPS = {
    "[run].flag_dir or [profiles.<name>].flag_dir": ("--flag",),
    "--ppr": ("--PPR",),
    "[run].ppr or [profiles.<name>].ppr": ("--no-PPR",),
    "no replacement (retired historical no-op flag)": (
        "--valid",
        "--ebwfile",
        "--pickle",
        "--legacy",
        "--usegetalmaflux",
        "--noupdate",
        "--semipass",
    ),
    "[envs.<name>].casa_root or [profiles.<name>].casa_root": (
        "--casa-root",
        "--casa-dir",
        "--casa-ver",
    ),
    "[envs.<name>].heuristics_dir or [profiles.<name>].heuristics_dir": (
        "--heuristics-dir",
        "--heuristics-root",
        "--pipe-dir",
        "--pipe-ver",
        "--pl-ver",
    ),
    "[envs.<name>].pixi_dir or [profiles.<name>].pixi_dir": ("--pixi-dir",),
    "[envs.<name>].pixi_env or [profiles.<name>].pixi_env": ("--pixi-env",),
    "[envs.<name>].branch or [profiles.<name>].branch": ("--branch",),
    "[paths].casarundata": ("--casarundata", "--casadata"),
    "[paths].obscaldir or [profiles.<name>].obscaldir": ("--obscaldir",),
    "[run].subdir or [profiles.<name>].subdir": ("--subdir",),
    "calibpipe env --print-env": ("--print-env",),
    "[batch].node or [profiles.<name>].node": ("--node",),
    "[run].nprocs or [profiles.<name>].nprocs": ("--nprocs", "--ncores"),
    "[run].loglevel or [profiles.<name>].loglevel": ("--loglevel",),
    "[run].useresume or [profiles.<name>].useresume": ("--useresume",),
    "[paths].aUdir": ("--aUdir",),
    "[run].staticobscal or [profiles.<name>].staticobscal": ("--staticobscal",),
    "[run].oldcont or [profiles.<name>].oldcont": ("--oldcont",),
    "[run].onlysemipass or [profiles.<name>].onlysemipass": ("--onlysemipass",),
    "[run].use_custom_rcdir or [profiles.<name>].use_custom_rcdir": ("--custom-rcdir",),
    "[run].symlink_shortcuts or [profiles.<name>].symlink_shortcuts": (
        "--symlink-shortcuts",
    ),
    "[run].log2term or [profiles.<name>].log2term": ("--log2term",),
    "[run].nologger or [profiles.<name>].nologger": ("--nologger",),
    "[run].logfile or [profiles.<name>].logfile": ("--logfile",),
    "[run].recipe or [profiles.<name>].recipe": ("--procedure",),
    "[run].vla or [profiles.<name>].vla": ("--vla",),
    "[run].rawdata or [profiles.<name>].rawdata": ("--rawdata",),
    "[run].omp_num_threads or [profiles.<name>].omp_num_threads": (
        "--omp-num-threads",
    ),
    "[run].openblas_num_threads or [profiles.<name>].openblas_num_threads": (
        "--openblas-num-threads",
    ),
    "[run].omp_max_threads or [profiles.<name>].omp_max_threads": (
        "--omp-max-threads",
    ),
    "[run].mem_frac or [profiles.<name>].mem_frac": ("--mem-frac",),
    "[run].oversubscribe or [profiles.<name>].oversubscribe": ("--oversubscribe",),
    "[run].bind_to or [profiles.<name>].bind_to": ("--bind-to",),
    "[run].map_by or [profiles.<name>].map_by": ("--map-by",),
    "[run].psrecord or [profiles.<name>].psrecord": ("--psrecord",),
    "[run].memstats or [profiles.<name>].memstats": ("--memstats",),
    "[run].pl_psrecord or [profiles.<name>].pl_psrecord": ("--pl-psrecord",),
    "[run].backup or [profiles.<name>].backup": ("--backup",),
    "[run].cont_dat or [profiles.<name>].cont_dat": ("--cont-dat",),
    "[run].jyperk_csv or [profiles.<name>].jyperk_csv": ("--jyperk-csv",),
    "[run].parameter_list or [profiles.<name>].parameter_list": ("--parameter-list",),
    "[run].ancillary or [profiles.<name>].ancillary": ("--ancillary",),
    "[run].datapath or [profiles.<name>].datapath": ("--datapath",),
    "[run].rundata or [profiles.<name>].rundata": ("--rundata",),
    "[run].autoreload or [profiles.<name>].autoreload": ("--autoreload",),
    "[batch].cores or [profiles.<name>].cores": ("-c", "--cores", "--ntasks"),
    "[batch].mail_type or [profiles.<name>].mail_type": ("-M", "--mail-type"),
    "[batch].outfile or [profiles.<name>].outfile": ("-o", "--outfile"),
    "[batch].errfile or [profiles.<name>].errfile": ("-e", "--errfile"),
    "[batch].logdir or [profiles.<name>].logdir": ("--logdir",),
    "[batch].mem or [profiles.<name>].mem": ("-m", "--mem"),
    "[batch].mem_per_cpu or [profiles.<name>].mem_per_cpu": ("--mem-per-cpu",),
    "[batch].walltime or [profiles.<name>].walltime": ("-t", "--walltime"),
    "[batch].nodelist or [profiles.<name>].nodelist": ("--nodelist",),
    "[batch].chdir or [profiles.<name>].chdir": ("--chdir",),
    "[batch].cpus_per_task or [profiles.<name>].cpus_per_task": ("--cpus-per-task",),
    "[batch].ntasks_per_core or [profiles.<name>].ntasks_per_core": (
        "--ntasks-per-core",
    ),
    "[batch].hint or [profiles.<name>].hint": ("--hint",),
    "[batch].distribution or [profiles.<name>].distribution": ("--distribution",),
    "[batch].no_requeue or [profiles.<name>].no_requeue": ("--requeue",),
    "--profile": ("--batch-profile", "--no-batch-profile"),
    "[batch].partition or [profiles.<name>].partition": (
        "-b",
        "--partition",
        "--queue",
    ),
    "typed settings in [run] or [profiles.<name>] (arbitrary flag forwarding was removed)": (
        "--extra-arg",
        "--no-extra-arg",
    ),
    "[batch].scheduler or [profiles.<name>].scheduler": ("--scheduler",),
    "[batch].requirements or [profiles.<name>].requirements": ("--requirements",),
    "[batch].request_gpus or [profiles.<name>].request_gpus": (
        "--request-gpus",
        "--gpus",
    ),
    "[batch].pool or [profiles.<name>].pool": (
        "--pool",
        "--central-manager",
        "--central-manager-host",
    ),
    "[batch].schedd or [profiles.<name>].schedd": (
        "--schedd",
        "--schedd-name",
        "--remote",
    ),
    "[batch].spool or [profiles.<name>].spool": ("--spool",),
    "[batch].should_transfer_files or [profiles.<name>].should_transfer_files": (
        "--should-transfer-files",
    ),
    "[batch].when_to_transfer_output or [profiles.<name>].when_to_transfer_output": (
        "--when-to-transfer-output",
    ),
    "[profiles.<name>].collect": ("--collect",),
    "[profiles.<name>].test_target": ("--target", "--test-target"),
    "[profiles.<name>].test_marker": ("--marker", "--test-marker"),
    "[profiles.<name>].test_filter": ("-k", "--filter", "--test-filter"),
    "[profiles.<name>].test_opts": ("--test-opts", "--pytest-opts"),
    "[batch].save_runfile or [profiles.<name>].save_runfile": ("--save-runfile",),
    "[profiles.<name>].with_report": ("--with-report", "--report"),
    "[batch].max_concurrent or [profiles.<name>].max_concurrent": (
        "--max-concurrent",
        "--throttle",
    ),
    "[run].nprocs (run) or [batch].node (submit)": ("-n",),
}
_RETIRED_NEGATABLE_FLAGS = frozenset(
    {
        "--valid",
        "--ebwfile",
        "--pickle",
        "--legacy",
        "--usegetalmaflux",
        "--noupdate",
        "--semipass",
        "--obscaldir",
        "--casarundata",
        "--casadata",
        "--rawdata",
        "--print-env",
        "--useresume",
        "--staticobscal",
        "--oldcont",
        "--onlysemipass",
        "--custom-rcdir",
        "--symlink-shortcuts",
        "--log2term",
        "--nologger",
        "--vla",
        "--oversubscribe",
        "--psrecord",
        "--memstats",
        "--pl-psrecord",
        "--backup",
        "--datapath",
        "--rundata",
        "--autoreload",
        "--requeue",
        "--spool",
        "--collect",
        "--with-report",
        "--report",
    }
)
RETIRED = {
    flag: destination for destination, flags in RETIRED_GROUPS.items() for flag in flags
}
RETIRED.update(
    {
        f"--no-{flag[2:]}": destination
        for flag, destination in list(RETIRED.items())
        if flag in _RETIRED_NEGATABLE_FLAGS
    }
)


class Parser(argparse.ArgumentParser):
    """ArgumentParser that disables flag abbreviation and adds 0.2 migration hints."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        if message.startswith("unrecognized arguments:"):
            for token in message.split(":", 1)[1].split():
                flag = token.split("=", 1)[0]
                if flag in RETIRED:
                    message += (
                        f"\n{flag} moved to {RETIRED[flag]}. See docs/migration-0.2.md."
                    )
                    break
        super().error(message)


def execution_parser(kind: str) -> Parser:
    """Build the shared argument parser for `run` and `submit`."""
    p = Parser(add_help=False)
    p.add_argument("--config", help="TOML configuration file")
    p.add_argument(
        "--no-site-config", action="store_true", help="Exclude site configuration"
    )
    p.add_argument("-p", "--profile", help="Named configuration profile")
    p.add_argument("--env", help="Named CASA/Pixi environment")
    p.add_argument("--workdir", help="Execution working directory")
    p.add_argument("--recipe", help="Pipeline reduction recipe override")
    p.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument(
        "-v", "--verbose", action=argparse.BooleanOptionalAction, default=None
    )
    if kind == "run":
        for flag in ("mous", "script", "cmd", "ppr"):
            p.add_argument(f"--{flag}")
        p.add_argument("--vis", nargs="+")
        p.add_argument(
            "-i", "--interactive", action=argparse.BooleanOptionalAction, default=None
        )
        p.add_argument(
            "--config-only",
            action="store_true",
            help="Load only the explicit --config file without merging user or site layers",
        )
        p.add_argument(
            "--isolate-root",
            action="store_true",
            help=argparse.SUPPRESS,
        )
    else:
        p.add_argument(
            "pipefile", nargs="?", help="MOUS/dataset runfile or pytest node IDs"
        )
    return p


def request(namespace: argparse.Namespace, model):
    """Convert a parsed argparse.Namespace into a typed request dataclass."""
    values = vars(namespace)
    return model(**{f.name: values[f.name] for f in fields(model) if f.name in values})


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI parser.

    Returns:
        Configured argument parser with `run`, `submit`, and `env`
        subcommands.
    """
    parser = Parser(
        prog="calibpipe",
        description="calibpipe: Lightweight runner and Slurm batch coordinator for ALMA Science Pipeline.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {calibpipe.__version__}")

    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # 'run' subcommand
    subparsers.add_parser(
        "run",
        help="Run a single ALMA pipeline execution (replaces calibPipeIF.py)",
        parents=[execution_parser("run")],
        conflict_handler="resolve",
    )

    # 'submit' subcommand
    subparsers.add_parser(
        "submit",
        help="Submit pipeline runs to Slurm (replaces runbatch.py)",
        parents=[execution_parser("submit")],
        conflict_handler="resolve",
    )

    # 'pmr' subcommand
    subparsers.add_parser(
        "pmr",
        help="Generate pipeline request (PPR) or download ALMA archive products (replaces pipelineMakeRequest)",
        parents=[pmr.build_parser(add_help=False)],
        conflict_handler="resolve",
    )

    # 'env' subcommand
    env_parser = subparsers.add_parser(
        "env",
        help="Resolve and print CASA/pipeline environment variables for shell sourcing",
    )
    env_parser.add_argument("--config", help="Path to TOML config file")
    env_parser.add_argument(
        "--no-site-config",
        action="store_true",
        default=False,
        help="Do not load site-level configuration",
    )
    env_parser.add_argument("--env", help="[envs.<name>] table to resolve")
    env_parser.add_argument("--subdir", help="Subdirectory appended to SCIPIPE_ROOTDIR")
    env_parser.add_argument(
        "--print-env",
        action="store_true",
        help="Print plain KEY=value pairs instead of 'export KEY=value'",
    )

    # 'config' subcommand
    config_parser = subparsers.add_parser(
        "config",
        help="Inspect resolved configuration values, paths, and environment definitions",
    )
    config_parser.add_argument("action", nargs="?", default="show", choices=["show"], help="Action to perform (default: show)")
    config_parser.add_argument("--config", help="Path to TOML config file")
    config_parser.add_argument(
        "--no-site-config",
        action="store_true",
        default=False,
        help="Do not load site-level configuration",
    )
    config_parser.add_argument("--env", help="[envs.<name>] table to inspect")
    config_parser.add_argument("-p", "--profile", help="Profile whose effective settings to inspect")

    # 'profile' subcommand
    profile_parser = subparsers.add_parser(
        "profile",
        help="List or inspect execution and batch profiles",
    )
    profile_parser.add_argument("--config", help="Path to TOML config file")
    profile_parser.add_argument(
        "--no-site-config",
        action="store_true",
        default=False,
        help="Do not load site-level configuration",
    )
    profile_subparsers = profile_parser.add_subparsers(dest="profile_action")

    list_p = profile_subparsers.add_parser("list", help="List all available execution and batch profiles")
    list_p.add_argument("--config", help="Path to TOML config file")
    list_p.add_argument(
        "--no-site-config",
        action="store_true",
        default=False,
        help="Do not load site-level configuration",
    )

    show_p = profile_subparsers.add_parser("show", help="Show details of a specific execution or batch profile")
    show_p.add_argument("name", help="Profile name to inspect")
    show_p.add_argument("--config", help="Path to TOML config file")
    show_p.add_argument(
        "--no-site-config",
        action="store_true",
        default=False,
        help="Do not load site-level configuration",
    )

    # 'collect' subcommand
    collect_parser = subparsers.add_parser(
        "collect",
        help="Discover and collect pytest test node IDs for regression test arrays",
    )
    collect_parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Test directory or file path to collect from (e.g. tests/regression/fast)",
    )
    collect_parser.add_argument(
        "-p",
        "--profile",
        help="Named profile from config to supply defaults (target, marker, filter, casa, heuristics)",
    )
    collect_parser.add_argument(
        "-m",
        "--marker",
        help="Pytest marker filter expression (e.g. 'regression')",
    )
    collect_parser.add_argument(
        "-k",
        "--filter",
        help="Pytest keyword filter expression (e.g. 'uid___A002')",
    )
    collect_parser.add_argument(
        "-o",
        "--output",
        help="Write collected test node IDs to a runfile for batch execution",
    )
    collect_parser.add_argument(
        "--config", help="Path to TOML config file"
    )
    collect_parser.add_argument(
        "--no-site-config",
        action="store_true",
        default=False,
        help="Do not load site-level configuration",
    )
    collect_parser.add_argument(
        "--casa-root",
        help="CASA root path override",
    )
    collect_parser.add_argument(
        "--heuristics-dir",
        help="Pipeline heuristics directory override",
    )
    collect_parser.add_argument(
        "--test-opts",
        help="Extra pytest options passed to test collection",
    )

    # 'report' subcommand
    subparsers.add_parser(
        "report",
        help="Synthesize regression test results and combine coverage datasets",
        parents=[report.build_parser()],
        conflict_handler="resolve",
    )

    # 'coverage' subcommand
    subparsers.add_parser(
        "coverage",
        help="Find, combine, and report on multiple coverage.py data files",
        parents=[coverage.build_parser()],
        conflict_handler="resolve",
    )

    # 'update-ref' subcommand
    subparsers.add_parser(
        "update-ref",
        help="Update and stage regression test reference results files (*.results.txt)",
        parents=[update_ref.build_parser(add_help=False)],
        conflict_handler="resolve",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run the unified calibpipe command-line interface.

    Args:
        argv: Optional argument sequence. When omitted, arguments are read
            from `sys.argv`.
    """
    args_list = list(sys.argv[1:] if argv is None else argv)

    # If invoked with no arguments, print help and exit
    if not args_list:
        build_parser().print_help()
        sys.exit(0)

    parser = build_parser()
    args = parser.parse_args(args_list)

    if args.subcommand == "run":
        # Execution receives the already parsed typed request.
        try:
            code = runner.run_request(request(args, RunRequest))
        except envconfig.ConfigError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            raise SystemExit(1) from None
        if code:
            raise SystemExit(code)
    elif args.subcommand == "submit":
        try:
            code = batch.submit_request(request(args, SubmitRequest))
        except envconfig.ConfigError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            raise SystemExit(1) from None
        if code:
            raise SystemExit(code)
    elif args.subcommand == "pmr":
        sys.exit(pmr.main(args_list[1:]))
    elif args.subcommand == "env":
        try:
            cfg = envconfig.load_merged_config(
                cli_arg=args.config,
                include_site=not getattr(args, "no_site_config", False),
            )
            spec = envconfig.resolve_env(cfg, args.env)
            env = envconfig.build_environment(cfg, spec, subdir=args.subdir)
            print(envconfig.format_shell_exports(env, export=not args.print_env))
        except envconfig.ConfigError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.subcommand == "config":
        try:
            cfg = envconfig.load_merged_config(
                cli_arg=args.config,
                include_site=not getattr(args, "no_site_config", False),
            )
            print(envconfig.format_config_overview(cfg, env_name=args.env, profile=args.profile))
        except envconfig.ConfigError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.subcommand == "profile":
        action = getattr(args, "profile_action", None) or "list"
        config_arg = getattr(args, "config", None)
        no_site = getattr(args, "no_site_config", False)
        try:
            cfg = envconfig.load_merged_config(
                cli_arg=config_arg,
                include_site=not no_site,
            )
            if action == "list":
                print(envconfig.format_profile_list(cfg))
            elif action == "show":
                print(envconfig.format_profile_details(cfg, args.name))
        except envconfig.ConfigError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.subcommand == "collect":
        try:
            cfg = envconfig.load_merged_config(
                cli_arg=args.config,
                include_site=not getattr(args, "no_site_config", False),
            )
            prof = cfg.profiles.get(args.profile) if args.profile else None

            target = (
                args.target
                or (prof.test_target if prof else None)
                or "tests/regression/fast"
            )
            marker = args.marker or (prof.test_marker if prof else None)
            filter_k = args.filter or (prof.test_filter if prof else None)
            test_opts = args.test_opts or (prof.test_opts if prof else None)

            env_name = (prof.env if prof and prof.env else None) or cfg.default_env
            env_spec = (
                envconfig.resolve_env(
                    cfg,
                    env_name,
                    casa_root=args.casa_root or (prof.casa_root if prof else None),
                    heuristics_dir=args.heuristics_dir or (prof.heuristics_dir if prof else None),
                    workspaces=prof.workspaces if prof else None,
                )
                if env_name
                else None
            )
            casa_root = env_spec.casa_root if env_spec else (args.casa_root or (prof.casa_root if prof else None))
            heuristics_dir = env_spec.heuristics_dir if env_spec else (args.heuristics_dir or (prof.heuristics_dir if prof else None))

            nodes = collect.collect_tests(
                target=target,
                casa_root=casa_root,
                heuristics_dir=heuristics_dir,
                marker=marker,
                filter_k=filter_k,
                extra_opts=test_opts,
            )
            if args.output:
                out_path = collect.write_test_runfile(args.output, nodes)
                print(f"Collected {len(nodes)} test(s) -> {out_path}")
            else:
                for node in nodes:
                    print(node)
        except (
            envconfig.ConfigError,
            OSError,
            subprocess.SubprocessError,
            ValueError,
            RuntimeError,
        ) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.subcommand == "report":
        report.main(args_list[1:])
    elif args.subcommand == "coverage":
        coverage.main(args_list[1:])
    elif args.subcommand == "update-ref":
        sys.exit(update_ref.main(args_list[1:]))


if __name__ == "__main__":
    main()
