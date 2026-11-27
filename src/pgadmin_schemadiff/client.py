"""High-level Python library API for pgadmin-schemadiff."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from pgadmin_schemadiff.config import Config, DatabaseConfig, load_config
from pgadmin_schemadiff.db_manager import apply_diff_script, prepare_database_files
from pgadmin_schemadiff.ddl_loader import StreamedDDL, open_ddl_inputs
from pgadmin_schemadiff.exceptions import DatabaseError
from pgadmin_schemadiff.pg_dump_runner import (
    DEFAULT_FULL_DUMP_FILENAME,
    run_pg_dump_schema,
)
from pgadmin_schemadiff.pgadmin_runner import run_pgadmin_diff
from pgadmin_schemadiff.psql_runner import resolve_psql
from pgadmin_schemadiff.script_generator import (
    ApplyResult,
    DiffResult,
    apply_diff_tweaks,
    filter_items,
    generate_diff_script,
)

ProgressCallback = Callable[[str], None]


class SchemaDiffClient:
    """Orchestrates DDL loading, database preparation, and pgAdmin 4 Schema Diff."""

    def __init__(
        self,
        config: Config,
        *,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self._progress = progress
        self._progress_lock = threading.Lock()

    def _report(self, message: str) -> None:
        if self._progress is not None:
            with self._progress_lock:
                self._progress(message)

    def _report_step(self, step: int, total: int, message: str) -> None:
        self._report(f"[{step}/{total}] {message}")

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
        """Reset both databases, apply Old/New DDLs in parallel, and generate the diff script."""
        has_full_dump = full_dump is not None and full_dump is not False
        total_steps = 4 + int(has_full_dump) + int(apply)

        self._report_step(
            1,
            total_steps,
            f"Loading DDL inputs (old: {old_ddl}, new: {new_ddl})...",
        )
        with open_ddl_inputs(old_ddl) as old_scripts, open_ddl_inputs(new_ddl) as new_scripts:
            psql = None
            if old_scripts or new_scripts:
                psql = resolve_psql(self.config.pgadmin_path, self.config.pg_dump_path)

            self._report_step(
                2,
                total_steps,
                f"Preparing Target (Old) database '{self.config.old_db.database}' "
                f"(schema '{self.config.old_db.primary_schema}', {len(old_scripts)} file(s)) "
                f"and Source (New) database '{self.config.new_db.database}' "
                f"(schema '{self.config.new_db.primary_schema}', {len(new_scripts)} file(s)) "
                "in parallel...",
            )

            def _prepare_one(
                label: str,
                db_cfg: DatabaseConfig,
                scripts: list[StreamedDDL],
            ) -> None:
                prepare_database_files(db_cfg, scripts, psql=psql)
                self._report(
                    f"  - {label} database '{db_cfg.database}' "
                    f"(schema '{db_cfg.primary_schema}') prepared."
                )

            with ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="pgadmin-sd-load"
            ) as executor:
                old_future = executor.submit(
                    _prepare_one, "Target (Old)", self.config.old_db, old_scripts
                )
                new_future = executor.submit(
                    _prepare_one, "Source (New)", self.config.new_db, new_scripts
                )
                old_future.result()
                new_future.result()

        return self._run_diff_steps(
            step_offset=2,
            total_steps=total_steps,
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
        has_full_dump = full_dump is not None and full_dump is not False
        total_steps = 2 + int(has_full_dump) + int(apply)
        return self._run_diff_steps(
            step_offset=0,
            total_steps=total_steps,
            include_identical_ddl=include_identical_ddl,
            apply=apply,
            full_dump=full_dump,
        )

    def _run_diff_steps(
        self,
        *,
        step_offset: int,
        total_steps: int,
        include_identical_ddl: bool,
        apply: bool,
        full_dump: bool | str | Path | None,
    ) -> DiffResult:
        has_full_dump = full_dump is not None and full_dump is not False
        step = step_offset + 1

        self._report_step(step, total_steps, "Running pgAdmin 4 Schema Diff...")
        all_items = run_pgadmin_diff(
            self.config,
            include_identical_ddl=include_identical_ddl,
        )

        step += 1
        self._report_step(
            step,
            total_steps,
            f"Generating diff script ({len(all_items)} object(s) compared)...",
        )
        if self.config.tweak.omit_pg_attver:
            all_items = apply_diff_tweaks(all_items, self.config.tweak)
        filtered = filter_items(all_items, self.config.output_filter)
        script = generate_diff_script(
            all_items, self.config.output_filter, source_schema=self.config.new_db.primary_schema
        )

        full_dump_path: Path | None = None
        if has_full_dump:
            step += 1
            dump_target: str | Path = (
                DEFAULT_FULL_DUMP_FILENAME if full_dump is True else full_dump
            )
            self._report_step(
                step,
                total_steps,
                f"Creating schema-only full dump '{dump_target}' from Source (New) database "
                f"'{self.config.new_db.database}' (schema '{self.config.new_db.primary_schema}')...",
            )
            full_dump_path = self.dump_source_schema(dump_target)

        apply_result: ApplyResult | None = None
        if apply:
            step += 1
            self._report_step(
                step,
                total_steps,
                f"Applying diff script to Target (Old) database '{self.config.old_db.database}' "
                f"(schema '{self.config.old_db.primary_schema}')...",
            )
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
    progress: ProgressCallback | None = None,
) -> DiffResult:
    """Convenience function to run Schema Diff from a :class:`Config` or TOML path."""
    resolved_config = config if isinstance(config, Config) else load_config(config)
    return SchemaDiffClient(resolved_config, progress=progress).diff_ddls(
        old_ddl,
        new_ddl,
        include_identical_ddl=include_identical_ddl,
        apply=apply,
        full_dump=full_dump,
    )
