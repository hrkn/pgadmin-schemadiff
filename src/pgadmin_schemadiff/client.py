"""High-level Python library API for pgadmin-schemadiff."""

from __future__ import annotations

from pathlib import Path

from pgadmin_schemadiff.config import Config, load_config
from pgadmin_schemadiff.db_manager import apply_diff_script, prepare_database
from pgadmin_schemadiff.ddl_loader import load_ddl_scripts
from pgadmin_schemadiff.exceptions import DatabaseError
from pgadmin_schemadiff.pg_dump_runner import (
    DEFAULT_FULL_DUMP_FILENAME,
    run_pg_dump_schema,
)
from pgadmin_schemadiff.pgadmin_runner import run_pgadmin_diff
from pgadmin_schemadiff.script_generator import (
    ApplyResult,
    DiffResult,
    apply_diff_tweaks,
    filter_items,
    generate_diff_script,
)


class SchemaDiffClient:
    """Orchestrates DDL loading, database preparation, and pgAdmin 4 Schema Diff."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def apply_diff(self, script: str) -> None:
        """Apply the generated migration SQL script to the Target (Old) database."""
        apply_diff_script(self.config.old_db, script)

    def dump_source_schema(
        self,
        output_path: str | Path = DEFAULT_FULL_DUMP_FILENAME,
    ) -> Path:
        """Create a schema-only pg_dump from the Source (New) database."""
        return run_pg_dump_schema(self.config, output_path)

    def diff_ddls(
        self,
        old_ddl: str | Path,
        new_ddl: str | Path,
        *,
        include_identical_ddl: bool = False,
        apply: bool = False,
        full_dump: bool | str | Path | None = None,
    ) -> DiffResult:
        """Reset both databases, apply Old/New DDLs, and generate the diff script."""
        old_scripts = load_ddl_scripts(old_ddl)
        new_scripts = load_ddl_scripts(new_ddl)

        prepare_database(self.config.old_db, old_scripts)
        prepare_database(self.config.new_db, new_scripts)

        return self.diff_existing(
            include_identical_ddl=include_identical_ddl,
            apply=apply,
            full_dump=full_dump,
        )

    def diff_existing(
        self,
        *,
        include_identical_ddl: bool = False,
        apply: bool = False,
        full_dump: bool | str | Path | None = None,
    ) -> DiffResult:
        """Compare the already-populated Old and New schemas using pgAdmin 4."""
        all_items = run_pgadmin_diff(
            self.config,
            include_identical_ddl=include_identical_ddl,
        )
        if self.config.tweak.omit_pg_attver:
            all_items = apply_diff_tweaks(all_items, self.config.tweak)
        filtered = filter_items(all_items, self.config.output_filter)
        script = generate_diff_script(all_items, self.config.output_filter)

        full_dump_path: Path | None = None
        if full_dump is not None and full_dump is not False:
            dump_target: str | Path = (
                DEFAULT_FULL_DUMP_FILENAME if full_dump is True else full_dump
            )
            full_dump_path = self.dump_source_schema(dump_target)

        apply_result: ApplyResult | None = None
        if apply:
            try:
                self.apply_diff(script)
            except DatabaseError as exc:
                apply_result = ApplyResult(succeeded=False, error=str(exc))
            else:
                apply_result = ApplyResult(succeeded=True)

        return DiffResult(
            script=script,
            items=filtered,
            all_items=all_items,
            apply_result=apply_result,
            full_dump_path=full_dump_path,
        )


def run_schema_diff(
    config: Config | str | Path,
    old_ddl: str | Path,
    new_ddl: str | Path,
    *,
    include_identical_ddl: bool = False,
    apply: bool = False,
    full_dump: bool | str | Path | None = None,
) -> DiffResult:
    """Convenience function to run Schema Diff from a :class:`Config` or TOML path."""
    resolved_config = config if isinstance(config, Config) else load_config(config)
    return SchemaDiffClient(resolved_config).diff_ddls(
        old_ddl,
        new_ddl,
        include_identical_ddl=include_identical_ddl,
        apply=apply,
        full_dump=full_dump,
    )
