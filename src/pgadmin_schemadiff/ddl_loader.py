"""Load DDL scripts from a single file, a directory, or a ZIP archive."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat
import tempfile
import zipfile

from pgadmin_schemadiff.exceptions import DDLSourceError


@dataclass(frozen=True)
class DDLScript:
    """In-memory representation of a single DDL script."""

    name: str
    sql: str


def _read_sql_file(file_path: Path) -> DDLScript:
    try:
        sql = file_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise DDLSourceError(f"Failed to read DDL file '{file_path}': {exc}") from exc
    return DDLScript(name=file_path.name, sql=sql)


def _load_from_directory(directory: Path) -> list[DDLScript]:
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        raise DDLSourceError(f"Failed to list DDL directory '{directory}': {exc}") from exc

    sql_files = sorted(
        (
            entry
            for entry in entries
            if entry.is_file() and entry.suffix.lower() == ".sql"
        ),
        key=lambda p: p.name,
    )
    return [_read_sql_file(sql_file) for sql_file in sql_files]


def _validate_zip_members(zf: zipfile.ZipFile, extract_root: Path, zip_path: Path) -> None:
    resolved_root = extract_root.resolve()
    for member in zf.infolist():
        mode = member.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise DDLSourceError(
                f"Unsafe symlink entry '{member.filename}' in ZIP archive '{zip_path}'."
            )
        target_path = (resolved_root / member.filename).resolve()
        if not target_path.is_relative_to(resolved_root):
            raise DDLSourceError(
                f"Unsafe path traversal entry '{member.filename}' in ZIP archive '{zip_path}'."
            )


def _load_from_zip(zip_path: Path) -> list[DDLScript]:
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            with tempfile.TemporaryDirectory(prefix="pgadmin_sd_zip_") as tmp_dir:
                extract_root = Path(tmp_dir)
                _validate_zip_members(zf, extract_root, zip_path)
                zf.extractall(extract_root)
                return _load_from_directory(extract_root)
    except DDLSourceError:
        raise
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError) as exc:
        raise DDLSourceError(
            f"Failed to extract DDL ZIP archive '{zip_path}': {exc}"
        ) from exc


def load_ddl_scripts(source: str | Path) -> list[DDLScript]:
    """Load DDL scripts from a file, directory (direct ``.sql`` files), or ZIP archive."""
    path = Path(source)
    if not path.exists():
        raise DDLSourceError(f"DDL source does not exist: '{path}'")

    if path.is_dir():
        return _load_from_directory(path)

    if path.is_file():
        is_zip = path.suffix.lower() == ".zip" or zipfile.is_zipfile(path)
        if is_zip:
            return _load_from_zip(path)
        return [_read_sql_file(path)]

    raise DDLSourceError(f"DDL source is neither a regular file nor a directory: '{path}'")
