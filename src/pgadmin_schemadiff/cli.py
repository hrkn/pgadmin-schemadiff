"""Command-line interface for pgadmin-schemadiff."""

from __future__ import annotations

from dataclasses import replace
import json

import click

from pgadmin_schemadiff.client import SchemaDiffClient
from pgadmin_schemadiff.config import load_config
from pgadmin_schemadiff.exceptions import ConfigError, SchemaDiffError
from pgadmin_schemadiff.pg_dump_runner import DEFAULT_FULL_DUMP_FILENAME
from pgadmin_schemadiff.sql_tweaks import AVAILABLE_TWEAKS, apply_tweak_overrides


@click.command(
    name="pgadmin-schemadiff",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Build Old and New schemas from DDLs in PostgreSQL and output "
        "migration SQL using pgAdmin 4's Schema Diff engine."
    ),
)
@click.option(
    "-c",
    "--config",
    "config_path",
    required=True,
    type=str,
    metavar="CONFIG",
    help="Path to the TOML configuration file.",
)
@click.option(
    "--old",
    "old_ddl",
    required=True,
    type=str,
    metavar="OLD",
    help="Path to the Old DDL source (.sql file, directory, or .zip archive).",
)
@click.option(
    "--new",
    "new_ddl",
    required=True,
    type=str,
    metavar="NEW",
    help="Path to the New DDL source (.sql file, directory, or .zip archive).",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["sql", "json"]),
    default="sql",
    show_default=True,
    help="Output format written to stdout.",
)
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="Apply the generated diff script to the Target (Old) database after diff extraction.",
)
@click.option(
    "--full-dump",
    "full_dump",
    is_flag=False,
    flag_value=DEFAULT_FULL_DUMP_FILENAME,
    default=None,
    type=str,
    metavar="[FILE]",
    help=(
        "Write a schema-only pg_dump of the Source (New) database after diff extraction "
        f"(default: {DEFAULT_FULL_DUMP_FILENAME})."
    ),
)
@click.option(
    "--tweak",
    "tweaks",
    multiple=True,
    type=str,
    metavar="TWEAK",
    help=(
        "Enable or override SQL post-processing tweaks (repeatable or comma-separated; "
        f"choices: {', '.join(AVAILABLE_TWEAKS)}; use '<name>=false' or 'no_<name>' to disable)."
    ),
)
@click.pass_context
def cli(
    ctx: click.Context,
    config_path: str,
    old_ddl: str,
    new_ddl: str,
    output_format: str,
    apply_changes: bool,
    full_dump: str | None,
    tweaks: tuple[str, ...],
) -> None:
    """Execute schema diff and emit SQL or JSON output."""
    try:
        config = load_config(config_path)
        if tweaks:
            config = replace(config, tweak=apply_tweak_overrides(config.tweak, tweaks))
        result = SchemaDiffClient(config).diff_ddls(
            old_ddl,
            new_ddl,
            apply=apply_changes,
            full_dump=full_dump,
        )
    except ConfigError as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        ctx.exit(2)
    except SchemaDiffError as exc:
        click.echo(f"Error: {exc}", err=True)
        ctx.exit(1)

    if output_format == "json":
        click.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        click.echo(result.script, nl=False)

    if result.apply_result is not None and not result.apply_result.succeeded:
        click.echo(f"Error: {result.apply_result.error}", err=True)
        ctx.exit(1)
    ctx.exit(0)


def main(argv: list[str] | None = None) -> int:
    """Run the pgadmin-schemadiff CLI and return a process exit code."""
    try:
        rv = cli.main(args=argv, prog_name="pgadmin-schemadiff", standalone_mode=False)
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from exc
    except click.Abort:
        click.echo("Aborted!", err=True)
        return 1
    return int(rv) if isinstance(rv, int) else 0
