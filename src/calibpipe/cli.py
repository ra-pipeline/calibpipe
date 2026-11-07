"""Unified command-line interface for calibpipe."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

import calibpipe
from calibpipe import batch, collect, config as envconfig, pmr, runner


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI parser.

    Returns:
        Configured argument parser with `run`, `batch`, and `env`
        subcommands.
    """
    parser = argparse.ArgumentParser(
        prog="calibpipe",
        description="calibpipe: Lightweight runner and Slurm batch coordinator for ALMA Science Pipeline.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {calibpipe.__version__}")

    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # 'run' subcommand
    subparsers.add_parser(
        "run",
        help="Run a single ALMA pipeline execution (replaces calibPipeIF.py)",
        parents=[runner.build_parser()],
        conflict_handler="resolve",
    )

    # 'submit' subcommand (with 'batch' alias)
    subparsers.add_parser(
        "submit",
        aliases=["batch"],
        help="Submit pipeline runs to Slurm (replaces runbatch.py)",
        parents=[batch.build_parser()],
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

    # Ergonomic routing: if first arg is not a known subcommand or global option,
    # assume the user wants `run` (e.g. `calibpipe --mous=uid://...`)
    known_commands = {
        "run",
        "submit",
        "batch",
        "pmr",
        "env",
        "config",
        "profile",
        "collect",
        "-h",
        "--help",
        "-V",
        "--version",
    }
    if args_list[0] not in known_commands:
        args_list.insert(0, "run")

    parser = build_parser()
    args = parser.parse_args(args_list)

    if args.subcommand == "run":
        # Pass remaining arguments to runner
        runner.main(args_list[1:])
    elif args.subcommand in ("submit", "batch"):
        batch.main(args_list[1:])
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
            print(envconfig.format_config_overview(cfg, env_name=args.env))
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
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
