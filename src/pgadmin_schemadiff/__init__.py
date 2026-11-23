"""Python CLI and library wrapper around pgAdmin 4's Schema Diff engine."""

from pgadmin_schemadiff.client import SchemaDiffClient, run_schema_diff
from pgadmin_schemadiff.config import (
    Config,
    DatabaseConfig,
    IgnoreOptions,
    OutputFilterOptions,
    load_config,
)
from pgadmin_schemadiff.db_manager import (
    apply_ddl_scripts,
    apply_diff_script,
    apply_sql_script,
    connect_db,
    ensure_and_reset_schema,
    prepare_database,
)
from pgadmin_schemadiff.ddl_loader import DDLScript, load_ddl_scripts
from pgadmin_schemadiff.exceptions import (
    ConfigError,
    DatabaseError,
    DDLSourceError,
    PgAdminRuntimeError,
    PgDumpError,
    SchemaDiffError,
)
from pgadmin_schemadiff.pg_dump_runner import (
    DEFAULT_FULL_DUMP_FILENAME,
    resolve_pg_dump,
    run_pg_dump_schema,
)
from pgadmin_schemadiff.pgadmin_runner import (
    PgAdminLayout,
    resolve_pgadmin_layout,
    run_pgadmin_diff,
)
from pgadmin_schemadiff.script_generator import (
    SCRIPT_HEADER,
    ApplyResult,
    DiffItem,
    DiffResult,
    DiffStatus,
    compute_depend_levels,
    filter_items,
    generate_diff_script,
)

__all__ = [
    "ApplyResult",
    "Config",
    "ConfigError",
    "DDLScript",
    "DDLSourceError",
    "DEFAULT_FULL_DUMP_FILENAME",
    "DatabaseConfig",
    "DatabaseError",
    "DiffItem",
    "DiffResult",
    "DiffStatus",
    "IgnoreOptions",
    "OutputFilterOptions",
    "PgAdminLayout",
    "PgAdminRuntimeError",
    "PgDumpError",
    "SCRIPT_HEADER",
    "SchemaDiffClient",
    "SchemaDiffError",
    "apply_ddl_scripts",
    "apply_diff_script",
    "apply_sql_script",
    "compute_depend_levels",
    "connect_db",
    "ensure_and_reset_schema",
    "filter_items",
    "generate_diff_script",
    "load_config",
    "load_ddl_scripts",
    "prepare_database",
    "resolve_pg_dump",
    "resolve_pgadmin_layout",
    "run_pg_dump_schema",
    "run_pgadmin_diff",
    "run_schema_diff",
]
