"""Stream SQL files to PostgreSQL with psql's COPY support."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
import os
from pathlib import Path
import re
from secrets import token_hex
import shutil
import subprocess
import tempfile
from typing import BinaryIO

from psycopg.conninfo import make_conninfo

from pgadmin_schemadiff.config import DatabaseConfig
from pgadmin_schemadiff.ddl_loader import StreamedDDL
from pgadmin_schemadiff.exceptions import DatabaseError


# These releases include the fix for CVE-2026-18408 in restricted mode.
_FIXED_MINOR = {14: 24, 15: 19, 16: 15, 17: 11, 18: 6}
_PSQL_VERSION = re.compile(r"^psql \(PostgreSQL\) (\d+)\.(\d+)\b")
_STDIN_LOCATION = re.compile(r"(?m)^psql:<stdin>:(\d+):")


def resolve_psql(pgadmin_path: Path, pg_dump_path: Path | None) -> Path:
    """Locate a psql that can safely run a script in restricted mode."""
    directories = [
        pgadmin_path / "runtime",
        pgadmin_path / "Contents" / "SharedSupport",
        pgadmin_path.parent / "bin",
    ]
    if pg_dump_path is not None:
        directories.append(pg_dump_path if pg_dump_path.is_dir() else pg_dump_path.parent)
    candidates = [directory / name for directory in directories for name in ("psql.exe", "psql")]
    path_match = shutil.which("psql")
    if path_match:
        candidates.append(Path(path_match))

    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file():
            continue
        try:
            result = subprocess.run(
                [str(candidate.resolve()), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        version = _PSQL_VERSION.match(result.stdout)
        if result.returncode == 0 and version:
            major, minor = map(int, version.groups())
            if major >= 19 or (major in _FIXED_MINOR and minor >= _FIXED_MINOR[major]):
                return candidate.resolve()

    raise DatabaseError(
        "SQL file streaming requires a psql with patched restricted mode "
        "(14.24+, 15.19+, 16.15+, 17.11+, 18.6+, or 19+). "
        "Install it alongside PostgreSQL or add it to PATH."
    )


def _send_sql(source: BinaryIO, destination: BinaryIO, script: StreamedDDL) -> int:
    newlines = 0
    for start, end in (*script.skip, (script.size, script.size)):
        remaining = start - source.tell()
        while remaining > 0:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise OSError("SQL file was truncated during restore")
            destination.write(chunk)
            newlines += chunk.count(b"\n")
            remaining -= len(chunk)
        source.seek(end)
    return newlines


def run_psql_files(
    psql: Path,
    db_config: DatabaseConfig,
    search_path_sql: str,
    scripts: Sequence[StreamedDDL],
) -> None:
    """Restore all input files in order through a single psql transaction."""
    if not scripts:
        return

    conninfo = make_conninfo(
        host=db_config.host,
        port=db_config.port,
        dbname=db_config.database,
        user=db_config.user,
        sslmode=db_config.sslmode,
    )
    env = os.environ.copy()
    env["PGCLIENTENCODING"] = "UTF8"
    env["PGPASSWORD"] = db_config.password
    env["PGOPTIONS"] = f"{env.get('PGOPTIONS', '')} -c standard_conforming_strings=on".strip()
    cmd = [
        str(psql),
        "--no-psqlrc",
        "--no-password",
        "--quiet",
        "--set=ON_ERROR_STOP=1",
        "--single-transaction",
        f"--dbname={conninfo}",
        f"--command=\\restrict {token_hex(32)}",
        "--file=-",
    ]
    failure = f"Failed to execute DDL on database '{db_config.database}'"
    file_setup = f"SET client_encoding TO 'UTF8';\n{search_path_sql};\n".encode("utf-8")
    setup_lines = file_setup.count(b"\n")
    file_locations: list[tuple[int, str]] = []

    def input_location(match: re.Match[str]) -> str:
        line = int(match[1])
        for first_line, name in reversed(file_locations):
            if line >= first_line:
                return f"psql:{name}:{line - first_line + 1}:"
        return match[0]

    try:
        with tempfile.TemporaryFile() as stderr:
            with subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr, env=env
            ) as process:
                pipe = process.stdin
                assert pipe is not None
                pipe_closed_early = False
                try:
                    line = 1
                    for script in scripts:
                        with script.path.open("rb") as source:
                            pipe.write(file_setup)
                            file_locations.append((line + setup_lines, script.name))
                            line += _send_sql(source, pipe, script) + setup_lines + 2
                            # Keep the next file separate even if this one ends without ';'.
                            pipe.write(b"\n;\n")
                    pipe.close()
                except BrokenPipeError:
                    pipe_closed_early = True
                    with suppress(BrokenPipeError):
                        pipe.close()
                except BaseException:
                    # Stop psql before EOF can commit an incomplete script.
                    process.kill()
                    with suppress(OSError):
                        pipe.close()
                    raise
                returncode = process.wait()

            if returncode != 0 or pipe_closed_early:
                stderr_size = stderr.seek(0, os.SEEK_END)
                stderr.seek(max(0, stderr_size - 16384))
                detail = stderr.read().decode("utf-8", errors="replace").strip()
                detail = _STDIN_LOCATION.sub(input_location, detail)
                raise DatabaseError(
                    f"{failure}: {detail or 'psql stopped before completing the restore'} "
                    f"(psql exit code {returncode})"
                )
    except OSError as exc:
        raise DatabaseError(f"{failure}: {exc}") from exc
