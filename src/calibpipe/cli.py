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

    # 'batch' subcommand
    subparsers.add_parser(
        "batch",
        help="Submit a batch of pipeline runs to Slurm (replaces runbatch.py)",
        parents=[batch.build_parser()],
        conflict_handler="resolve",
    )

    # 'env' subcommand
    env_parser = subparsers.add_parser(
        "env",
        help="Resolve and print CASA/pipeline environment variables for shell sourcing",
    )
    env_parser.add_argument("--config", help="Path to TOML config file")
    env_parser.add_argument("--env", help="[envs.<name>] table to resolve")
    env_parser.add_argument("--subdir", help="Subdirectory appended to SCIPIPE_ROOTDIR")
    env_parser.add_argument(
        "--print-env",
        action="store_true",
        help="Print plain KEY=value pairs instead of 'export KEY=value'",
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
    known_commands = {"run", "batch", "env", "-h", "--help", "-V", "--version"}
    if args_list[0] not in known_commands:
        args_list.insert(0, "run")

    parser = build_parser()
    args = parser.parse_args(args_list)

    if args.subcommand == "run":
        # Pass remaining arguments to driver
        driver.main(args_list[1:])
    elif args.subcommand == "batch":
        batch.main(args_list[1:])
    elif args.subcommand == "env":
        try:
            cfg_path = envconfig.find_config_path(args.config)
            cfg = envconfig.load_config(cfg_path)
            spec = envconfig.resolve_env(cfg, args.env)
            env = envconfig.build_environment(cfg, spec, subdir=args.subdir)
            print(envconfig.format_shell_exports(env, export=not args.print_env))
        except envconfig.ConfigError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
