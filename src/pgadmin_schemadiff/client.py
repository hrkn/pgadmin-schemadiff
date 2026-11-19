"""High-level Python library API for pgadmin-schemadiff."""

from __future__ import annotations

from pathlib import Path

from pgadmin_schemadiff.config import Config, load_config
from pgadmin_schemadiff.db_manager import prepare_database
from pgadmin_schemadiff.ddl_loader import load_ddl_scripts
from pgadmin_schemadiff.pgadmin_runner import run_pgadmin_diff
from pgadmin_schemadiff.script_generator import (
    DiffResult,
    filter_items,
    generate_diff_script,
)


class SchemaDiffClient:
    """Orchestrates DDL loading, database preparation, and pgAdmin 4 Schema Diff."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def diff_ddls(
        self,
        old_ddl: str | Path,
        new_ddl: str | Path,
        *,
        include_identical_ddl: bool = False,
    ) -> DiffResult:
        """Reset both databases, apply Old/New DDLs, and generate the diff script."""
        old_scripts = load_ddl_scripts(old_ddl)
        new_scripts = load_ddl_scripts(new_ddl)

        prepare_database(self.config.old_db, old_scripts)
        prepare_database(self.config.new_db, new_scripts)

        return self.diff_existing(include_identical_ddl=include_identical_ddl)

    def diff_existing(
        self,
        *,
        include_identical_ddl: bool = False,
    ) -> DiffResult:
        """Compare the already-populated Old and New schemas using pgAdmin 4."""
        all_items = run_pgadmin_diff(
            self.config,
            include_identical_ddl=include_identical_ddl,
        )
        filtered = filter_items(all_items, self.config.output_filter)
        script = generate_diff_script(all_items, self.config.output_filter)
        return DiffResult(script=script, items=filtered, all_items=all_items)


def run_schema_diff(
    config: Config | str | Path,
    old_ddl: str | Path,
    new_ddl: str | Path,
    *,
    include_identical_ddl: bool = False,
) -> DiffResult:
    """Convenience function to run Schema Diff from a :class:`Config` or TOML path."""
    resolved_config = config if isinstance(config, Config) else load_config(config)
    return SchemaDiffClient(resolved_config).diff_ddls(
        old_ddl,
        new_ddl,
        include_identical_ddl=include_identical_ddl,
    )
