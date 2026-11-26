"""Load DDL scripts from a single file, a directory, or a ZIP archive."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from os import fstat
from pathlib import Path
import re
import stat
import tempfile
import zipfile
from typing import BinaryIO

from pgadmin_schemadiff.exceptions import DDLSourceError
from pgadmin_schemadiff.sql_input import validate_sql_input

_DUMP_HEADER = re.compile(rb"--\r?\n-- PostgreSQL database dump\r?\n--\r?\n(?:\r?\n)*")
_RESTRICT = re.compile(rb"\\restrict ([A-Za-z0-9]+)(?=\r?\n)")
_DUMP_FOOTER = re.compile(
    rb"(?m)^--\r?\n-- PostgreSQL database dump complete\r?\n--\r?\n"
    rb"(?:\r?\n)*(\\unrestrict ([A-Za-z0-9]+))(?:\r?\n)*\Z"
)


@dataclass(frozen=True)
class DDLScript:
    """In-memory representation of a single DDL script."""

    name: str
    sql: str


@dataclass(frozen=True)
class StreamedDDL:
    """SQL file and the byte ranges to skip when sending it to restricted psql."""

    path: Path
    size: int
    skip: tuple[tuple[int, int], ...]

    @property
    def name(self) -> str:
        return self.path.name


def _read_sql_file(file_path: Path) -> DDLScript:
    try:
        sql = file_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise DDLSourceError(f"Failed to read DDL file '{file_path}': {exc}") from exc
    return DDLScript(name=file_path.name, sql=sql)


def _list_sql_files(directory: Path) -> list[Path]:
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        raise DDLSourceError(f"Failed to list DDL directory '{directory}': {exc}") from exc

    return sorted(
        (
            entry
            for entry in entries
            if entry.is_file() and entry.suffix.lower() == ".sql"
        ),
        key=lambda p: p.name,
    )


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


@contextmanager
def _open_ddl_paths(source: str | Path) -> Iterator[list[Path]]:
    path = Path(source)
    if not path.exists():
        raise DDLSourceError(f"DDL source does not exist: '{path}'")

    if path.is_dir():
        yield _list_sql_files(path)
        return
    if not path.is_file():
        raise DDLSourceError(f"DDL source is neither a regular file nor a directory: '{path}'")
    if path.suffix.lower() != ".zip" and not zipfile.is_zipfile(path):
        yield [path]
        return

    with tempfile.TemporaryDirectory(prefix="pgadmin_sd_zip_") as tmp_dir:
        extract_root = Path(tmp_dir)
        try:
            with zipfile.ZipFile(path, "r") as zf:
                _validate_zip_members(zf, extract_root, path)
                zf.extractall(extract_root)
        except (zipfile.BadZipFile, OSError, UnicodeDecodeError) as exc:
            raise DDLSourceError(f"Failed to extract DDL ZIP archive '{path}': {exc}") from exc
        yield _list_sql_files(extract_root)


def _validation_lines(
    source: BinaryIO, skip: list[tuple[int, int]], *, utf8: bool
) -> Iterator[bytes]:
    source.seek(0)
    offset = 0
    marker_ranges = iter(skip)
    marker = next(marker_ranges, None)
    for line in source:
        end = offset + len(line)
        if marker is not None and offset <= marker[0] < end:
            start, stop = marker
            line = line[: start - offset] + line[stop - offset :]
            marker = next(marker_ranges, None)
        if utf8:
            line.decode("utf-8")
        yield line
        offset = end


def _inspect_ddl_file(path: Path, standard_strings: bool) -> tuple[StreamedDDL, bool]:
    try:
        with path.open("rb") as fh:
            size = fstat(fh.fileno()).st_size
            header = fh.read(8192)
            start = 3 if header.startswith(b"\xef\xbb\xbf") else 0
            dump = _DUMP_HEADER.match(header, start)
            skip = [(0, start)] if start else []
            restrict = _RESTRICT.match(header, dump.end()) if dump else None
            if restrict:
                # The restore uses its own restriction key for the entire input.
                tail_start = max(0, size - 8192)
                fh.seek(tail_start)
                footer = _DUMP_FOOTER.search(fh.read(8192))
                if not footer or footer[2] != restrict[1]:
                    raise DDLSourceError(f"Unmatched pg_dump restriction markers in '{path}'.")
                skip.append(restrict.span())
                skip.append((tail_start + footer.start(1), tail_start + footer.end(1)))
            standard_strings = validate_sql_input(
                _validation_lines(fh, skip, utf8=not dump), path.name, standard_strings
            )
            return StreamedDDL(path, size, tuple(skip)), standard_strings
    except (OSError, UnicodeDecodeError) as exc:
        raise DDLSourceError(f"Failed to read DDL file '{path}': {exc}") from exc


@contextmanager
def open_ddl_inputs(source: str | Path) -> Iterator[list[StreamedDDL]]:
    """Discover SQL files while keeping any extracted ZIP files available for execution."""
    with _open_ddl_paths(source) as paths:
        scripts = []
        standard_strings = True
        for path in paths:
            script, standard_strings = _inspect_ddl_file(path, standard_strings)
            scripts.append(script)
        yield scripts


def load_ddl_scripts(source: str | Path) -> list[DDLScript]:
    """Load DDL scripts into memory from a file, directory, or ZIP archive."""
    with _open_ddl_paths(source) as paths:
        return [_read_sql_file(path) for path in paths]
