"""Unified command-line interface for calibpipe."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

import calibpipe
from calibpipe import batch, config as envconfig, driver


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI parser.

    Returns:
        Configured argument parser with `run`, `batch`, and `env`
        subcommands.
    """
    parser = argparse.ArgumentParser(
        prog="calibpipe",
        description="calibpipe: Lightweight driver and Slurm batch runner for ALMA Science Pipeline.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {calibpipe.__version__}")

    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # 'run' subcommand
    subparsers.add_parser(
        "run",
        help="Run a single ALMA pipeline execution (replaces calibPipeIF.py)",
        parents=[driver.build_parser()],
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
        "env",
        "config",
        "profile",
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
        # Pass remaining arguments to driver
        driver.main(args_list[1:])
    elif args.subcommand in ("submit", "batch"):
        batch.main(args_list[1:])
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


if __name__ == "__main__":
    main()
