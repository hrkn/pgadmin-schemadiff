"""Command-line interface for pgadmin-schemadiff."""

from __future__ import annotations

import argparse
import json
import sys

from pgadmin_schemadiff.client import SchemaDiffClient
from pgadmin_schemadiff.config import load_config
from pgadmin_schemadiff.exceptions import ConfigError, SchemaDiffError
from pgadmin_schemadiff.pg_dump_runner import DEFAULT_FULL_DUMP_FILENAME


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="pgadmin-schemadiff",
        description=(
            "Build Old and New schemas from DDLs in PostgreSQL and output "
            "migration SQL using pgAdmin 4's Schema Diff engine."
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        help="Path to the TOML configuration file.",
    )
    parser.add_argument(
        "--old",
        required=True,
        help="Path to the Old DDL source (.sql file, directory, or .zip archive).",
    )
    parser.add_argument(
        "--new",
        required=True,
        help="Path to the New DDL source (.sql file, directory, or .zip archive).",
    )
    parser.add_argument(
        "--format",
        choices=["sql", "json"],
        default="sql",
        help="Output format written to stdout (default: sql).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the generated diff script to the Target (Old) database after diff extraction.",
    )
    parser.add_argument(
        "--full-dump",
        nargs="?",
        const=DEFAULT_FULL_DUMP_FILENAME,
        default=None,
        metavar="FILE",
        help=(
            "Write a schema-only pg_dump of the Source (New) database after diff extraction "
            f"(default: {DEFAULT_FULL_DUMP_FILENAME})."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the pgadmin-schemadiff CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        result = SchemaDiffClient(config).diff_ddls(
            args.old,
            args.new,
            apply=args.apply,
            full_dump=args.full_dump,
        )
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except SchemaDiffError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(result.script)

    if result.apply_result is not None and not result.apply_result.succeeded:
        print(f"Error: {result.apply_result.error}", file=sys.stderr)
        return 1
    return 0
