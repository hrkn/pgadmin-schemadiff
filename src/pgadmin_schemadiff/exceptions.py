"""Custom exception hierarchy for pgadmin-schemadiff-cli."""


class SchemaDiffError(Exception):
    """Base exception for all pgadmin-schemadiff errors."""


class ConfigError(SchemaDiffError):
    """Raised when configuration loading or validation fails."""


class DDLSourceError(SchemaDiffError):
    """Raised when loading DDL scripts from a file, directory, or zip fails."""


class DatabaseError(SchemaDiffError):
    """Raised when connecting to, resetting, or applying DDLs to PostgreSQL fails."""


class PgAdminRuntimeError(SchemaDiffError):
    """Raised when pgAdmin 4 runtime discovery or schema comparison execution fails."""


class PgDumpError(SchemaDiffError):
    """Raised when locating or running pg_dump fails."""


class SqlTweakError(SchemaDiffError):
    """Raised when parsing or rewriting SQL for a configured tweak fails."""
