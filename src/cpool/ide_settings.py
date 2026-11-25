"""Set claudeCode.claudeProcessWrapper in VS Code's settings.json (JSONC) by editing text spans, so comments and formatting survive."""
from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SETTING_KEY = "claudeCode.claudeProcessWrapper"

_MAX_DEPTH = 128
_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")
_JSON_WS = " \t\r\n"

Status = Literal["created", "added", "updated", "already"]


class SettingsError(Exception):
    pass


@dataclass(frozen=True)
class PatchResult:
    status: Status
    text: str
    previous: str | None = None


@dataclass(frozen=True)
class WriteResult:
    status: Status
    path: Path
    backup: Path | None = None
    previous: str | None = None


@dataclass(frozen=True)
class _Member:
    key: str
    key_start: int
    val_start: int
    val_end: int
    comma_pos: int | None


class _Scanner:
    def __init__(self, body: str) -> None:
        self.s = body

    def trivia(self, i: int) -> int:
        s, n = self.s, len(self.s)
        while i < n:
            if s[i] in _JSON_WS:
                i += 1
            elif s.startswith("//", i):
                nl = s.find("\n", i)
                i = n if nl < 0 else nl
            elif s.startswith("/*", i):
                close = s.find("*/", i + 2)
                if close < 0:
                    raise SettingsError("unterminated /* comment")
                i = close + 2
            else:
                break
        return i

    def string(self, i: int) -> int:
        s, j = self.s, i + 1
        while j < len(s):
            if s[j] == "\\":
                j += 2
            elif s[j] == '"':
                try:
                    json.loads(s[i : j + 1])
                except ValueError as e:
                    raise SettingsError(f"invalid string at offset {i}: {e}") from None
                return j + 1
            else:
                j += 1
        raise SettingsError(f"unterminated string at offset {i}")

    def value(self, i: int, depth: int) -> int:
        if depth > _MAX_DEPTH:
            raise SettingsError("nesting too deep")
        s = self.s
        match s[i : i + 1]:
            case "{":
                return self.object(i, depth)[0]
            case "[":
                return self.array(i, depth)
            case '"':
                return self.string(i)
        if m := _NUMBER.match(s, i):
            return m.end()
        for literal in ("true", "false", "null"):
            if s.startswith(literal, i):
                return i + len(literal)
        raise SettingsError(f"unexpected token at offset {i}")

    def array(self, i: int, depth: int) -> int:
        s = self.s
        j = self.trivia(i + 1)
        while s[j : j + 1] != "]":
            j = self.trivia(self.value(j, depth + 1))
            if s[j : j + 1] == ",":
                j = self.trivia(j + 1)
            elif s[j : j + 1] != "]":
                raise SettingsError(f"expected ',' or ']' at offset {j}")
        return j + 1

    def object(self, i: int, depth: int) -> tuple[int, list[_Member]]:
        s = self.s
        members: list[_Member] = []
        j = self.trivia(i + 1)
        while s[j : j + 1] != "}":
            if s[j : j + 1] != '"':
                raise SettingsError(f"expected a string key at offset {j}")
            key_end = self.string(j)
            colon = self.trivia(key_end)
            if s[colon : colon + 1] != ":":
                raise SettingsError(f"expected ':' at offset {colon}")
            val_start = self.trivia(colon + 1)
            val_end = self.value(val_start, depth + 1)
            key_start, j = j, self.trivia(val_end)
            comma = None
            if s[j : j + 1] == ",":
                comma = j
                j = self.trivia(j + 1)
            elif s[j : j + 1] != "}":
                raise SettingsError(f"expected ',' or '}}' at offset {j}")
            members.append(_Member(json.loads(s[key_start:key_end]), key_start, val_start, val_end, comma))
        return j + 1, members


def _parse(body: str) -> tuple[int, int, list[_Member]]:
    scanner = _Scanner(body)
    open_brace = scanner.trivia(0)
    if body[open_brace : open_brace + 1] != "{":
        raise SettingsError("settings root is not an object")
    end, members = scanner.object(open_brace, 1)
    if scanner.trivia(end) != len(body):
        raise SettingsError("unexpected content after the settings object")
    return open_brace, end - 1, members


def _string_value(body: str, m: _Member) -> str | None:
    raw = body[m.val_start : m.val_end]
    return json.loads(raw) if raw.startswith('"') else None


def _indent(body: str, members: list[_Member]) -> str:
    for m in reversed(members):
        pad = body[body.rfind("\n", 0, m.key_start) + 1 : m.key_start]
        if pad and not pad.strip(" \t"):
            return pad
    return "  "


def _insert(body: str, open_brace: int, close_brace: int, members: list[_Member], entry: str, eol: str) -> str:
    if not members and not body[open_brace + 1 : close_brace].strip(_JSON_WS):
        return f"{body[:open_brace]}{{{eol}  {entry}{eol}}}{body[close_brace + 1:]}"
    need_comma = bool(members) and members[-1].comma_pos is None
    if "\n" not in body[open_brace:close_brace]:
        insert_pos = close_brace
        insertion = f"{entry} " if body[close_brace - 1] in " \t" else f" {entry}"
    else:
        indent = _indent(body, members)
        last_nl = body.rfind("\n", open_brace, close_brace)
        if not body[last_nl + 1 : close_brace].strip(" \t"):
            insert_pos, insertion = last_nl + 1, f"{indent}{entry}{eol}"
        else:
            insert_pos, insertion = close_brace, f"{eol}{indent}{entry}{eol}"
    comma_at = members[-1].val_end if need_comma else insert_pos
    comma = "," if need_comma else ""
    return f"{body[:comma_at]}{comma}{body[comma_at:insert_pos]}{insertion}{body[insert_pos:]}"


def patch_jsonc(text: str, key: str, value: str) -> PatchResult:
    bom = "﻿" if text.startswith("﻿") else ""
    body = text[len(bom) :]
    eol = "\r\n" if "\r\n" in body else "\n"
    entry = f"{json.dumps(key)}: {json.dumps(value)}"
    if not body.strip(_JSON_WS):
        return PatchResult("added", f"{bom}{{{eol}  {entry}{eol}}}{eol}")
    open_brace, close_brace, members = _parse(body)
    matches = [m for m in members if m.key == key]
    if len(matches) > 1:
        raise SettingsError(f"duplicate {key!r} entries")
    if matches:
        m = matches[0]
        current = _string_value(body, m)
        if current == value:
            return PatchResult("already", text, previous=value)
        previous = body[m.val_start : m.val_end] if current is None else current
        new_body = f"{body[: m.val_start]}{json.dumps(value)}{body[m.val_end :]}"
        status: Status = "updated"
    else:
        previous = None
        new_body = _insert(body, open_brace, close_brace, members, entry, eol)
        status = "added"
    hits = [m for m in _parse(new_body)[2] if m.key == key]
    if len(hits) != 1 or _string_value(new_body, hits[0]) != value:
        raise SettingsError("could not apply the edit safely")
    return PatchResult(status, bom + new_body, previous=previous)


def vscode_settings_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "Code" / "User" / "settings.json"


def _atomic_write(target: Path, data: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_wrapper(wrapper: str, path: Path | None = None) -> WriteResult:
    path = path or vscode_settings_path()
    target = Path(os.path.realpath(path))
    if target.is_dir():
        raise SettingsError(f"{path} is a directory")
    if not target.exists():
        res = patch_jsonc("", SETTING_KEY, wrapper)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, res.text.encode(), 0o644)
        return WriteResult("created", path)
    raw = target.read_bytes()
    mode = stat.S_IMODE(target.stat().st_mode)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SettingsError(f"{path} is not valid UTF-8") from None
    res = patch_jsonc(text, SETTING_KEY, wrapper)
    if res.status == "already":
        return WriteResult("already", path, previous=res.previous)
    backup = target.with_name(target.name + ".bak")
    _atomic_write(backup, raw, mode)
    _atomic_write(target, res.text.encode(), mode)
    return WriteResult(res.status, path, backup=backup, previous=res.previous)
