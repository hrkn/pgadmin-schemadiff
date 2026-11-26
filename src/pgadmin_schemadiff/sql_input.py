"""Check the statement boundaries needed to restore a batch of SQL files."""

from __future__ import annotations

from collections.abc import Iterable
import re

from pgadmin_schemadiff.exceptions import DDLSourceError


_TOKEN = re.compile(
    rb"""--[^\r\n]*|/\*|[Ee]'|'|"|\$(?:[a-zA-Z_\x80-\xff][a-zA-Z_0-9\x80-\xff]*)?\$"""
    rb"|[a-zA-Z_\x80-\xff][a-zA-Z_0-9$\x80-\xff]*|::|:[a-zA-Z_0-9]+|[^\s]"
)
_COMMENT = re.compile(rb"/\*|\*/")
_TRANSACTION = {b"ABORT", b"BEGIN", b"COMMIT", b"END", b"ROLLBACK", b"START"}
_STANDARD_STRINGS = re.compile(
    rb"""SET (?:LOCAL |SESSION )?"?STANDARD_CONFORMING_STRINGS"? (?:TO|=) ['"]?(\w+)"""
)


def _quoted_end(line: bytes, pos: int, quote: bytes, escapes: bool) -> int:
    while (end := line.find(quote, pos)) >= 0:
        pos = end + len(quote)
        if escapes:
            backslash = end
            while backslash > 0 and line[backslash - 1 : backslash] == b"\\":
                backslash -= 1
            if (end - backslash) % 2:
                continue
        if quote in (b"'", b'"') and line.startswith(quote, pos):
            pos += 1
            continue
        return pos
    return -1


def validate_sql_input(
    lines: Iterable[bytes], name: str, standard_strings: bool
) -> bool:
    """Reject SQL that can leave the batch transaction or span file boundaries."""
    quote = b""
    escapes = False
    comments = 0
    parentheses = 0
    compound = 0
    first = previous = b""
    copy_from_stdin = False
    pending_copies = 0
    setting: list[bytes] = []

    for number, line in enumerate(lines, 1):
        if pending_copies:
            if line.rstrip(b"\r\n") == b"\\.":
                pending_copies -= 1
            continue

        pos = 0
        while pos < len(line):
            if quote:
                pos = _quoted_end(line, pos, quote, escapes)
                if pos < 0:
                    break
                quote = b""
                continue
            if comments:
                marker = _COMMENT.search(line, pos)
                if marker is None:
                    break
                comments += 1 if marker[0] == b"/*" else -1
                pos = marker.end()
                continue

            match = _TOKEN.search(line, pos)
            if match is None:
                break
            pos = match.end()
            token = match[0]
            if token.startswith(b"--"):
                continue
            if token == b"/*":
                comments = 1
                continue

            word = token.upper()
            if not first and token != b";":
                if word in _TRANSACTION:
                    raise DDLSourceError(f"Transaction control is not allowed in '{name}' at line {number}.")
                if token.startswith(b":"):
                    raise DDLSourceError(f"Expected a SQL statement in '{name}' at line {number}.")
                first = word
            elif first == previous == b"PREPARE" and word == b"TRANSACTION":
                raise DDLSourceError(f"Transaction control is not allowed in '{name}' at line {number}.")

            if first in (b"SET", b"RESET") and token != b";" and len(setting) < 5:
                end = _quoted_end(line, pos, token, False) if token in (b"'", b'"') else -1
                setting.append(line[match.start() : end].upper() if end >= 0 else word)

            if token in (b"'", b'"', b"E'", b"e'") or (
                len(token) >= 2 and token.startswith(b"$") and token.endswith(b"$")
            ):
                quote = b"'" if word == b"E'" else token
                escapes = word == b"E'" or (quote == b"'" and not standard_strings)
            elif token == b"(":
                parentheses += 1
            elif token == b")":
                parentheses = max(0, parentheses - 1)
            elif (previous, word) == (b"BEGIN", b"ATOMIC") or (compound and word == b"CASE"):
                compound += 1
            elif compound and word == b"END":
                compound -= 1
            elif first == b"COPY" and not parentheses and (previous, word) == (b"FROM", b"STDIN"):
                copy_from_stdin = True
            elif token == b";" and not (parentheses or compound):
                standard_strings = _apply_string_setting(setting, standard_strings)
                setting.clear()
                pending_copies += copy_from_stdin
                copy_from_stdin = False
                first = b""
            previous = word

    if pending_copies or copy_from_stdin:
        raise DDLSourceError(f"COPY FROM STDIN data in '{name}' must end with a \\. line.")
    if quote or comments or parentheses or compound:
        raise DDLSourceError(f"Unterminated SQL in '{name}'.")
    return _apply_string_setting(setting, standard_strings)


def _apply_string_setting(tokens: list[bytes], current: bool) -> bool:
    statement = b" ".join(tokens)
    if statement in (
        b"RESET ALL",
        b"RESET STANDARD_CONFORMING_STRINGS",
        b'RESET "STANDARD_CONFORMING_STRINGS"',
    ):
        return True
    if value := _STANDARD_STRINGS.fullmatch(statement.rstrip(b"""'" """)):
        return value[1] not in (b"0", b"OF", b"OFF", b"F", b"FA", b"FAL", b"FALS", b"FALSE", b"N", b"NO")
    return current
