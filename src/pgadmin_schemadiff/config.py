"""Configuration loading and validation for pgadmin-schemadiff."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib
from typing import Any

from pgadmin_schemadiff.exceptions import ConfigError


def _require_str(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"Configuration '{field_name}' must be a string.")
    if not allow_empty and not value.strip():
        raise ConfigError(f"Configuration '{field_name}' must not be empty.")
    return value


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"Configuration '{field_name}' must be a boolean.")
    return value


def _require_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Configuration '{field_name}' must be an integer.")
    return value


def _require_table(value: Any, section_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"Configuration section '[{section_name}]' must be a table.")
    return value


def _reject_unknown_keys(
    table: dict[str, Any],
    allowed: set[str],
    section_name: str,
) -> None:
    unknown = sorted(set(table.keys()) - allowed)
    if unknown:
        keys = ", ".join(unknown)
        raise ConfigError(
            f"Unknown configuration key(s) in '[{section_name}]': {keys}"
        )


def _resolve_alias(
    table: dict[str, Any],
    primary: str,
    alias: str,
    section_name: str,
) -> tuple[bool, Any]:
    has_primary = primary in table
    has_alias = alias in table
    if has_primary and has_alias:
        if table[primary] != table[alias]:
            raise ConfigError(
                f"Conflicting values for '{primary}' and '{alias}' in '[{section_name}]'."
            )
        return True, table[primary]
    if has_primary:
        return True, table[primary]
    if has_alias:
        return True, table[alias]
    return False, None


@dataclass(frozen=True)
class DatabaseConfig:
    """Connection and target schema configuration for one PostgreSQL database."""

    host: str
    port: int = 5432
    database: str = "postgres"
    user: str = ""
    password: str = ""
    search_path: str = "public"
    sslmode: str = "prefer"

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host.strip():
            raise ConfigError("Database 'host' must be a non-empty string.")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise ConfigError("Database 'port' must be an integer between 1 and 65535.")
        if not isinstance(self.database, str) or not self.database.strip():
            raise ConfigError("Database 'database' must be a non-empty string.")
        if not isinstance(self.user, str) or not self.user.strip():
            raise ConfigError("Database 'user' must be a non-empty string.")
        if not isinstance(self.password, str):
            raise ConfigError("Database 'password' must be a string.")
        if not isinstance(self.search_path, str) or not self.search_path.strip():
            raise ConfigError("Database 'search_path' must be a non-empty string.")
        if not isinstance(self.sslmode, str) or not self.sslmode.strip():
            raise ConfigError("Database 'sslmode' must be a non-empty string.")
        _ = self.primary_schema

    def search_path_schemas(self) -> list[str]:
        """Parse ``search_path`` into an ordered list of schema entries."""
        schemas: list[str] = []
        for raw in self.search_path.split(","):
            item = raw.strip()
            if len(item) >= 2 and item.startswith('"') and item.endswith('"'):
                item = item[1:-1].replace('""', '"').strip()
            if item:
                schemas.append(item)
        if not schemas:
            raise ConfigError("Database 'search_path' must contain at least one schema name.")
        return schemas

    @property
    def primary_schema(self) -> str:
        """Return the first concrete schema name in ``search_path`` (ignoring ``$user``)."""
        for schema in self.search_path_schemas():
            if schema != "$user":
                return schema
        raise ConfigError(
            "Database 'search_path' must include a concrete schema name (not only '$user')."
        )


@dataclass(frozen=True)
class IgnoreOptions:
    """Comparison ignore flags matching pgAdmin 4 Schema Diff options."""

    owner: bool = False
    whitespace: bool = False
    tablespace: bool = False
    grants: bool = False


@dataclass(frozen=True)
class OutputFilterOptions:
    """Result status filter options matching pgAdmin 4 Schema Diff filter menu."""

    source_only: bool = True
    target_only: bool = True
    different: bool = True
    same: bool = False

    def selected_statuses(self) -> set[str]:
        """Return the set of pgAdmin status strings enabled by this filter."""
        statuses: set[str] = set()
        if self.source_only:
            statuses.add("Source Only")
        if self.target_only:
            statuses.add("Target Only")
        if self.different:
            statuses.add("Different")
        if self.same:
            statuses.add("Identical")
        return statuses


@dataclass(frozen=True)
class Config:
    """Top-level configuration for pgadmin-schemadiff."""

    pgadmin_path: Path
    old_db: DatabaseConfig
    new_db: DatabaseConfig
    ignore: IgnoreOptions = field(default_factory=IgnoreOptions)
    output_filter: OutputFilterOptions = field(default_factory=OutputFilterOptions)
    timeout_seconds: int = 300
    pg_dump_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.pgadmin_path, Path) or not str(self.pgadmin_path).strip():
            raise ConfigError("Configuration 'pgadmin_path' must be a valid Path.")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, int) or self.timeout_seconds <= 0:
            raise ConfigError("Configuration 'timeout_seconds' must be a positive integer.")
        if self.pg_dump_path is not None and (
            not isinstance(self.pg_dump_path, Path) or not str(self.pg_dump_path).strip()
        ):
            raise ConfigError("Configuration 'pg_dump_path' must be a valid Path.")

        old_key = (
            self.old_db.host.strip().lower(),
            self.old_db.port,
            self.old_db.database.strip(),
            self.old_db.primary_schema,
        )
        new_key = (
            self.new_db.host.strip().lower(),
            self.new_db.port,
            self.new_db.database.strip(),
            self.new_db.primary_schema,
        )
        if old_key == new_key:
            raise ConfigError(
                "Old and New database targets must differ (host, port, database, and primary schema cannot all be identical)."
            )


def _parse_pgadmin_section(raw: dict[str, Any]) -> tuple[Path, int, Path | None]:
    has_top_path = "pgadmin_path" in raw
    has_pgadmin_section = "pgadmin" in raw

    install_path_str: str | None = None
    timeout_seconds = 300
    pg_dump_path: Path | None = None

    if has_top_path:
        install_path_str = _require_str(raw["pgadmin_path"], "pgadmin_path")

    if has_pgadmin_section:
        pgadmin_tbl = _require_table(raw["pgadmin"], "pgadmin")
        _reject_unknown_keys(
            pgadmin_tbl,
            {"install_path", "path", "timeout_seconds", "pg_dump_path"},
            "pgadmin",
        )
        has_path, resolved_path = _resolve_alias(pgadmin_tbl, "install_path", "path", "pgadmin")
        if has_path:
            section_path = _require_str(resolved_path, "pgadmin.install_path")
            if install_path_str is not None and install_path_str != section_path:
                raise ConfigError(
                    "Conflicting values for top-level 'pgadmin_path' and '[pgadmin].install_path'."
                )
            install_path_str = section_path
        if "timeout_seconds" in pgadmin_tbl:
            timeout_seconds = _require_int(
                pgadmin_tbl["timeout_seconds"],
                "pgadmin.timeout_seconds",
            )
            if timeout_seconds <= 0:
                raise ConfigError("Configuration 'pgadmin.timeout_seconds' must be positive.")
        if "pg_dump_path" in pgadmin_tbl:
            pg_dump_path = Path(
                _require_str(pgadmin_tbl["pg_dump_path"], "pgadmin.pg_dump_path")
            )

    if install_path_str is None:
        raise ConfigError(
            "Missing pgAdmin 4 installation path: set 'install_path' under '[pgadmin]'."
        )

    return Path(install_path_str), timeout_seconds, pg_dump_path


def _parse_ignore_section(raw: dict[str, Any]) -> IgnoreOptions:
    if "ignore" not in raw:
        return IgnoreOptions()
    tbl = _require_table(raw["ignore"], "ignore")
    _reject_unknown_keys(tbl, {"owner", "whitespace", "whitespaces", "tablespace", "grants"}, "ignore")

    owner = _require_bool(tbl["owner"], "ignore.owner") if "owner" in tbl else False
    tablespace = (
        _require_bool(tbl["tablespace"], "ignore.tablespace")
        if "tablespace" in tbl
        else False
    )
    grants = _require_bool(tbl["grants"], "ignore.grants") if "grants" in tbl else False

    has_ws, ws_val = _resolve_alias(tbl, "whitespace", "whitespaces", "ignore")
    whitespace = _require_bool(ws_val, "ignore.whitespace") if has_ws else False

    return IgnoreOptions(
        owner=owner,
        whitespace=whitespace,
        tablespace=tablespace,
        grants=grants,
    )


def _parse_filter_section(raw: dict[str, Any]) -> OutputFilterOptions:
    if "filter" in raw and "output_filter" in raw:
        raise ConfigError("Specify either '[filter]' or '[output_filter]', not both.")
    section_key = "filter" if "filter" in raw else ("output_filter" if "output_filter" in raw else None)
    if section_key is None:
        return OutputFilterOptions()

    tbl = _require_table(raw[section_key], section_key)
    _reject_unknown_keys(
        tbl,
        {"source_only", "target_only", "different", "same", "identical"},
        section_key,
    )

    source_only = (
        _require_bool(tbl["source_only"], f"{section_key}.source_only")
        if "source_only" in tbl
        else True
    )
    target_only = (
        _require_bool(tbl["target_only"], f"{section_key}.target_only")
        if "target_only" in tbl
        else True
    )
    different = (
        _require_bool(tbl["different"], f"{section_key}.different")
        if "different" in tbl
        else True
    )
    has_same, same_val = _resolve_alias(tbl, "same", "identical", section_key)
    same = _require_bool(same_val, f"{section_key}.same") if has_same else False

    return OutputFilterOptions(
        source_only=source_only,
        target_only=target_only,
        different=different,
        same=same,
    )


def _parse_database_entry(raw_db: Any, name: str) -> DatabaseConfig:
    section_name = f"databases.{name}"
    tbl = _require_table(raw_db, section_name)
    _reject_unknown_keys(
        tbl,
        {
            "host",
            "port",
            "database",
            "dbname",
            "user",
            "username",
            "password",
            "password_env",
            "search_path",
            "sslmode",
        },
        section_name,
    )

    if "host" not in tbl:
        raise ConfigError(f"Missing required key 'host' in '[{section_name}]'.")
    host = _require_str(tbl["host"], f"{section_name}.host")

    port = _require_int(tbl["port"], f"{section_name}.port") if "port" in tbl else 5432

    has_db, db_val = _resolve_alias(tbl, "database", "dbname", section_name)
    database = _require_str(db_val, f"{section_name}.database") if has_db else "postgres"

    has_user, user_val = _resolve_alias(tbl, "user", "username", section_name)
    if not has_user:
        raise ConfigError(f"Missing required key 'user' in '[{section_name}]'.")
    user = _require_str(user_val, f"{section_name}.user")

    if "password" in tbl and "password_env" in tbl:
        raise ConfigError(
            f"Specify either 'password' or 'password_env' in '[{section_name}]', not both."
        )
    if "password_env" in tbl:
        env_var = _require_str(tbl["password_env"], f"{section_name}.password_env")
        if env_var not in os.environ:
            raise ConfigError(
                f"Environment variable '{env_var}' referenced by '{section_name}.password_env' is not set."
            )
        password = os.environ[env_var]
    elif "password" in tbl:
        password = _require_str(tbl["password"], f"{section_name}.password", allow_empty=True)
    else:
        password = ""

    search_path = (
        _require_str(tbl["search_path"], f"{section_name}.search_path")
        if "search_path" in tbl
        else "public"
    )
    sslmode = (
        _require_str(tbl["sslmode"], f"{section_name}.sslmode")
        if "sslmode" in tbl
        else "prefer"
    )

    return DatabaseConfig(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        search_path=search_path,
        sslmode=sslmode,
    )


def _parse_databases_section(raw: dict[str, Any]) -> tuple[DatabaseConfig, DatabaseConfig]:
    if "databases" not in raw:
        raise ConfigError("Missing required '[databases]' section.")
    dbs_tbl = _require_table(raw["databases"], "databases")
    _reject_unknown_keys(dbs_tbl, {"old", "new"}, "databases")

    if "old" not in dbs_tbl:
        raise ConfigError("Missing required '[databases.old]' section.")
    if "new" not in dbs_tbl:
        raise ConfigError("Missing required '[databases.new]' section.")

    old_db = _parse_database_entry(dbs_tbl["old"], "old")
    new_db = _parse_database_entry(dbs_tbl["new"], "new")
    return old_db, new_db


def load_config(path: str | Path) -> Config:
    """Load and validate a TOML configuration file."""
    config_path = Path(path)
    try:
        content = config_path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Unable to read configuration file '{config_path}': {exc}") from exc

    try:
        raw = tomllib.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Invalid TOML in configuration file '{config_path}': {exc}") from exc

    unknown_top = sorted(
        set(raw.keys()) - {"pgadmin", "pgadmin_path", "ignore", "filter", "output_filter", "databases"}
    )
    if unknown_top:
        raise ConfigError(f"Unknown top-level configuration section(s): {', '.join(unknown_top)}")

    pgadmin_path, timeout_seconds, pg_dump_path = _parse_pgadmin_section(raw)
    ignore = _parse_ignore_section(raw)
    output_filter = _parse_filter_section(raw)
    old_db, new_db = _parse_databases_section(raw)

    return Config(
        pgadmin_path=pgadmin_path,
        old_db=old_db,
        new_db=new_db,
        ignore=ignore,
        output_filter=output_filter,
        timeout_seconds=timeout_seconds,
        pg_dump_path=pg_dump_path,
    )
