"""Locate pg_dump and generate schema-only dumps from the Source database."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

from pgadmin_schemadiff.config import Config, DatabaseConfig, IgnoreOptions
from pgadmin_schemadiff.exceptions import PgDumpError, SqlTweakError
from pgadmin_schemadiff.sql_tweaks import comment_meta_restrict_in_sql

DEFAULT_FULL_DUMP_FILENAME = "full_dump.sql"


def resolve_pg_dump(
    pgadmin_path: Path,
    pg_dump_path: Path | None = None,
) -> Path:
    """Locate the ``pg_dump`` executable from config, pgAdmin 4 layout, or ``PATH``."""
    if pg_dump_path is not None:
        explicit = Path(pg_dump_path)
        if explicit.is_file():
            return explicit
        if explicit.is_dir():
            for name in ("pg_dump.exe", "pg_dump"):
                candidate = explicit / name
                if candidate.is_file():
                    return candidate
            raise PgDumpError(
                f"Could not locate 'pg_dump' inside configured directory '{explicit}'."
            )
        raise PgDumpError(f"Configured pg_dump executable does not exist: '{explicit}'")

    root = Path(pgadmin_path)
    candidates = (
        root / "runtime" / "pg_dump.exe",
        root / "runtime" / "pg_dump",
        root / "Contents" / "SharedSupport" / "pg_dump",
        root.parent / "bin" / "pg_dump.exe",
        root.parent / "bin" / "pg_dump",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    which_match = shutil.which("pg_dump")
    if which_match:
        return Path(which_match)

    raise PgDumpError(
        f"Could not locate 'pg_dump' under pgAdmin 4 installation '{root}' or in PATH. "
        "Set '[pgadmin].pg_dump_path' in config."
    )


def _quote_pg_dump_schema_pattern(schema: str) -> str:
    """Double-quote a schema name so ``pg_dump --schema`` matches it literally."""
    escaped = schema.replace('"', '""')
    return f'"{escaped}"'


def _build_pg_dump_command(
    pg_dump_exe: Path,
    db_config: DatabaseConfig,
    ignore: IgnoreOptions,
    output_path: Path,
) -> list[str]:
    cmd = [
        str(pg_dump_exe),
        "--schema-only",
        "--format=plain",
        "--encoding=UTF8",
        "--no-password",
        "--strict-names",
        "--host",
        db_config.host,
        "--port",
        str(db_config.port),
        "--username",
        db_config.user,
        "--dbname",
        db_config.database,
        "--schema",
        _quote_pg_dump_schema_pattern(db_config.primary_schema),
    ]
    if ignore.owner:
        cmd.append("--no-owner")
    if ignore.tablespace:
        cmd.append("--no-tablespaces")
    if ignore.grants:
        cmd.append("--no-privileges")
    cmd.extend(["--file", str(output_path)])
    return cmd


def _build_pg_dump_env(pg_dump_exe: Path, db_config: DatabaseConfig) -> dict[str, str]:
    env = os.environ.copy()
    env["PGSSLMODE"] = db_config.sslmode
    if db_config.password:
        env["PGPASSWORD"] = db_config.password
    else:
        env.pop("PGPASSWORD", None)

    exe_dir = str(pg_dump_exe.parent)
    existing_path = env.get("PATH", "")
    env["PATH"] = f"{exe_dir}{os.pathsep}{existing_path}" if existing_path else exe_dir
    return env


def run_pg_dump_schema(
    config: Config,
    output_path: str | Path = DEFAULT_FULL_DUMP_FILENAME,
) -> Path:
    """Run ``pg_dump --schema-only`` against the Source (New) database primary schema."""
    if isinstance(output_path, str) and not output_path.strip():
        raise PgDumpError("Full dump output path must not be empty.")

    dest = Path(output_path)
    if dest.exists() and dest.is_dir():
        raise PgDumpError(f"Full dump output path is a directory: '{dest}'")

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PgDumpError(
            f"Failed to create parent directory for full dump '{dest.parent}': {exc}"
        ) from exc

    pg_dump_exe = resolve_pg_dump(config.pgadmin_path, config.pg_dump_path)
    # Source = config.new_db in pgadmin-schemadiff's comparison mapping.
    cmd = _build_pg_dump_command(pg_dump_exe, config.new_db, config.ignore, dest)
    env = _build_pg_dump_env(pg_dump_exe, config.new_db)

    try:
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout_seconds,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PgDumpError(
            f"pg_dump timed out after {config.timeout_seconds} second(s)."
        ) from exc
    except OSError as exc:
        raise PgDumpError(
            f"Failed to launch pg_dump '{pg_dump_exe}': {exc}"
        ) from exc

    if proc.returncode != 0:
        err_detail = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
        raise PgDumpError(
            f"pg_dump failed (exit code {proc.returncode}): {err_detail}"
        )

    if config.tweak.comment_meta_restrict:
        try:
            raw_dump = dest.read_bytes().decode("utf-8")
            tweaked_dump = comment_meta_restrict_in_sql(raw_dump)
            if tweaked_dump != raw_dump:
                dest.write_bytes(tweaked_dump.encode("utf-8"))
        except (OSError, UnicodeDecodeError, SqlTweakError) as exc:
            raise PgDumpError(
                f"Failed to apply comment_meta_restrict tweak to '{dest}': {exc}"
            ) from exc

    return dest
