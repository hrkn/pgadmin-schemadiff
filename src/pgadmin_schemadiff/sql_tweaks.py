"""Post-processing SQL tweaks powered by pglast."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
import re

from pglast import ast, parse_sql, split
from pglast.enums import AlterTableType
from pglast.parser import ParseError, scan
from pglast.stream import RawStream

from pgadmin_schemadiff.config import TweakOptions
from pgadmin_schemadiff.exceptions import ConfigError, SqlTweakError

AVAILABLE_TWEAKS: tuple[str, ...] = (
    "omit_pg_attver",
    "comment_meta_restrict",
)

_RESTRICT_LINE_RE = re.compile(
    r"^([ \t]*)(\\(?:un)?restrict)(?=[ \t]|$|\r|\n)([ \t]+[A-Za-z0-9_-]+)?"
)
_SENTINEL_PREFIX = "__pgadmin_sd_restrict_probe_"
_TRUE_VALUES = {"true", "1", "yes", "on"}
_FALSE_VALUES = {"false", "0", "no", "off"}


def apply_tweak_overrides(
    base: TweakOptions,
    specs: Sequence[str],
) -> TweakOptions:
    """Merge repeatable or comma-separated ``--tweak`` CLI flags into :class:`TweakOptions`."""
    updates: dict[str, bool] = {}
    for spec in specs:
        for raw_part in spec.split(","):
            token = raw_part.strip()
            if not token:
                raise ConfigError(
                    "Empty tweak name in '--tweak'. Available tweaks: "
                    f"{', '.join(AVAILABLE_TWEAKS)}."
                )

            if "=" in token:
                raw_name, raw_val = token.split("=", 1)
                name = raw_name.strip().lower()
                val_lower = raw_val.strip().lower()
                if val_lower in _TRUE_VALUES:
                    enabled = True
                elif val_lower in _FALSE_VALUES:
                    enabled = False
                else:
                    raise ConfigError(
                        f"Invalid boolean value '{raw_val.strip()}' for tweak '{raw_name.strip()}'."
                    )
            else:
                lowered = token.lower()
                if lowered.startswith(("no_", "no-")):
                    name = lowered[3:]
                    enabled = False
                else:
                    name = lowered
                    enabled = True

            if name not in AVAILABLE_TWEAKS:
                raise ConfigError(
                    f"Unknown tweak '{token}'. Available tweaks: {', '.join(AVAILABLE_TWEAKS)}."
                )
            updates[name] = enabled

    return replace(base, **updates)


def _is_pg_attver_defelem(node: object) -> bool:
    return (
        isinstance(node, ast.DefElem)
        and not getattr(node, "defnamespace", None)
        and isinstance(node.defname, str)
        and node.defname.lower() == "pg_attver"
    )


def _strip_pg_attver_from_stmt(stmt: ast.Node) -> tuple[bool, ast.Node | None]:
    """Remove column-level ``pg_attver`` option clauses from an ``AlterTableStmt``."""
    if not isinstance(stmt, ast.AlterTableStmt) or not stmt.cmds:
        return False, stmt

    modified = False
    kept_cmds: list[ast.AlterTableCmd] = []
    for cmd in stmt.cmds:
        if (
            isinstance(cmd, ast.AlterTableCmd)
            and cmd.subtype in (AlterTableType.AT_SetOptions, AlterTableType.AT_ResetOptions)
            and cmd.def_
        ):
            kept_elems = tuple(el for el in cmd.def_ if not _is_pg_attver_defelem(el))
            if len(kept_elems) != len(cmd.def_):
                modified = True
                if kept_elems:
                    kept_cmds.append(
                        ast.AlterTableCmd(
                            subtype=cmd.subtype,
                            name=cmd.name,
                            num=cmd.num,
                            def_=kept_elems,
                            behavior=cmd.behavior,
                            missing_ok=cmd.missing_ok,
                            recurse=cmd.recurse,
                        )
                    )
                continue
        kept_cmds.append(cmd)

    if not modified:
        return False, stmt
    if not kept_cmds:
        return True, None
    return True, ast.AlterTableStmt(
        relation=stmt.relation,
        cmds=tuple(kept_cmds),
        objtype=stmt.objtype,
        missing_ok=stmt.missing_ok,
    )


def _find_statement_semicolon_end(sql: str, stmt_end: int, limit: int) -> int:
    """Return index immediately after the terminating semicolon, or ``stmt_end`` if absent."""
    tail = sql[stmt_end:limit]
    if not tail:
        return stmt_end
    for tok in scan(tail):
        if tok.name in ("SQL_COMMENT", "C_COMMENT"):
            continue
        if tok.name == "ASCII_59":
            return stmt_end + tok.end + 1
        break
    return stmt_end


def _normalize_after_removals(text: str, original_ended_with_newline: bool) -> str:
    if not text.strip():
        return ""
    newline = "\r\n" if "\r\n" in text else "\n"
    normalized = re.sub(r"(?:\r?\n){3,}", newline * 2, text)
    normalized = re.sub(r"^(?:[ \t]*\r?\n)+", "", normalized)
    if original_ended_with_newline:
        normalized = re.sub(r"(?:[ \t]*\r?\n)+$", newline, normalized)
    else:
        normalized = re.sub(r"[ \t]+$", "", re.sub(r"(?:[ \t]*\r?\n)+$", "", normalized))
    return normalized


def omit_pg_attver_from_sql(sql: str) -> str:
    """Remove ``ALTER COLUMN ... SET/RESET (pg_attver...)`` clauses/statements using ``pglast``."""
    if not sql.strip():
        return sql

    try:
        slices = split(sql, with_parser=True, only_slices=True)
    except ParseError as exc:
        raise SqlTweakError(f"Failed to parse SQL for 'omit_pg_attver': {exc}") from exc

    if not slices:
        return sql

    parsed_items: list[tuple[slice, bool, ast.Node | None]] = []
    any_modified = False
    for slc in slices:
        stmt_text = sql[slc]
        try:
            raw_stmts = parse_sql(stmt_text)
        except ParseError as exc:
            raise SqlTweakError(f"Failed to parse SQL statement for 'omit_pg_attver': {exc}") from exc

        if not raw_stmts:  # pragma: no cover - split() excludes empty statements
            parsed_items.append((slc, False, None))
            continue

        modified, new_stmt = _strip_pg_attver_from_stmt(raw_stmts[0].stmt)
        if modified:
            any_modified = True
        parsed_items.append((slc, modified, new_stmt))

    if not any_modified:
        return sql

    parts: list[str] = []
    cursor = 0
    removed_any = False

    for idx, (slc, modified, new_stmt) in enumerate(parsed_items):
        if not modified:
            parts.append(sql[cursor:slc.stop])
            cursor = slc.stop
            continue

        if new_stmt is not None:
            parts.append(sql[cursor:slc.start])
            parts.append(RawStream()(new_stmt))
            cursor = slc.stop
            continue

        removed_any = True
        prefix = sql[cursor:slc.start]
        prefix = re.sub(r"(?:^|(?<=\n))[ \t]+$", "", prefix)
        parts.append(prefix)

        limit = slices[idx + 1].start if idx + 1 < len(slices) else len(sql)
        semi_end = _find_statement_semicolon_end(sql, slc.stop, limit)
        trailing = sql[semi_end:limit]
        eol_match = re.match(r"[ \t]*(?:\r?\n)?", trailing)
        # Only swallow trailing horizontal whitespace/newline if nothing else sits on that line.
        if eol_match and (
            eol_match.group(0).endswith("\n") or semi_end + eol_match.end() == len(sql)
        ):
            cursor = semi_end + eol_match.end()
        else:
            cursor = semi_end

    parts.append(sql[cursor:])
    rebuilt = "".join(parts)

    if removed_any:
        return _normalize_after_removals(rebuilt, sql.endswith(("\n", "\r")))
    return rebuilt


def comment_meta_restrict_in_sql(sql: str) -> str:
    """Comment out top-level ``\\restrict`` / ``\\unrestrict`` lines validated via ``pglast``."""
    if not sql:
        return sql

    lines = sql.splitlines(keepends=True)
    candidates: dict[int, int] = {}
    probed_lines: list[str] = []
    offset = 0

    for idx, line in enumerate(lines):
        match = _RESTRICT_LINE_RE.match(line)
        if match:
            indent = match.group(1)
            sentinel = f"{_SENTINEL_PREFIX}{idx}__"
            probed_line = f"{indent}{sentinel}{line[match.end():]}"
            candidates[idx] = offset + len(indent)
            probed_lines.append(probed_line)
            offset += len(probed_line)
        else:
            probed_lines.append(line)
            offset += len(line)

    if not candidates:
        return sql

    probed_sql = "".join(probed_lines)
    try:
        tokens = scan(probed_sql)
    except ParseError as exc:
        raise SqlTweakError(
            f"Failed to scan SQL for 'comment_meta_restrict': {exc}"
        ) from exc

    ident_positions: dict[int, str] = {
        tok.start: probed_sql[tok.start : tok.end + 1]
        for tok in tokens
        if tok.name == "IDENT"
    }

    changed = False
    for idx, expected_start in candidates.items():
        sentinel = f"{_SENTINEL_PREFIX}{idx}__"
        if ident_positions.get(expected_start) == sentinel:
            lines[idx] = f"-- {lines[idx]}"
            changed = True

    return "".join(lines) if changed else sql
