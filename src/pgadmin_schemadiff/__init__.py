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
    SchemaDiffError,
)
from pgadmin_schemadiff.pgadmin_runner import (
    PgAdminLayout,
    resolve_pgadmin_layout,
    run_pgadmin_diff,
)
from pgadmin_schemadiff.script_generator import (
    SCRIPT_HEADER,
    DiffItem,
    DiffResult,
    DiffStatus,
    compute_depend_levels,
    filter_items,
    generate_diff_script,
)

__all__ = [
    "Config",
    "ConfigError",
    "DDLScript",
    "DDLSourceError",
    "DatabaseConfig",
    "DatabaseError",
    "DiffItem",
    "DiffResult",
    "DiffStatus",
    "IgnoreOptions",
    "OutputFilterOptions",
    "PgAdminLayout",
    "PgAdminRuntimeError",
    "SCRIPT_HEADER",
    "SchemaDiffClient",
    "SchemaDiffError",
    "apply_ddl_scripts",
    "compute_depend_levels",
    "connect_db",
    "ensure_and_reset_schema",
    "filter_items",
    "generate_diff_script",
    "load_config",
    "load_ddl_scripts",
    "prepare_database",
    "resolve_pgadmin_layout",
    "run_pgadmin_diff",
    "run_schema_diff",
]
