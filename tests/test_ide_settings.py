"""Comment-preserving settings.json edits: the file is the user's, so every byte outside the edit must survive and bad input must fail closed."""
import json
import os
import stat

import pytest

from cpool.ide_settings import SETTING_KEY, SettingsError, patch_jsonc, write_wrapper

W = "/w"
E = f'"{SETTING_KEY}": "{W}"'
OLD = f'"{SETTING_KEY}": "/old"'

PATCH_CASES = {
    "empty-braces": ("{}", f"{{\n  {E}\n}}"),
    "spaced-braces": ("{ }", f"{{\n  {E}\n}}"),
    "newline-braces": ("{\n}", f"{{\n  {E}\n}}"),
    "empty-with-line-comment": ("{ // c\n}", f"{{ // c\n  {E}\n}}"),
    "empty-with-block-comment": ("{ /* c */ }", f"{{ /* c */ {E} }}"),
    "two-space": ('{\n  "a": 1\n}', f'{{\n  "a": 1,\n  {E}\n}}'),
    "four-space": ('{\n    "a": 1\n}', f'{{\n    "a": 1,\n    {E}\n}}'),
    "tab": ('{\n\t"a": 1\n}', f'{{\n\t"a": 1,\n\t{E}\n}}'),
    "trailing-comma": ('{\n  "a": 1,\n}', f'{{\n  "a": 1,\n  {E}\n}}'),
    "inline-line-comment": ('{\n  "a": 1 // note\n}', f'{{\n  "a": 1, // note\n  {E}\n}}'),
    "inline-block-comment": ('{\n  "a": 1 /* c */\n}', f'{{\n  "a": 1, /* c */\n  {E}\n}}'),
    "commented-out-key": (
        f'{{\n  // {OLD},\n  "a": 1\n}}',
        f'{{\n  // {OLD},\n  "a": 1,\n  {E}\n}}',
    ),
    "trailing-comments": ('{\n  "a": 1,\n  // end\n}', f'{{\n  "a": 1,\n  // end\n  {E}\n}}'),
    "single-line": ('{"a": 1}', f'{{"a": 1, {E}}}'),
    "single-line-trailing-comma": ('{"a": 1,}', f'{{"a": 1, {E}}}'),
    "single-line-block-comment": ('{"a": 1 /* c */}', f'{{"a": 1, /* c */ {E}}}'),
    "brace-on-value-line": ('{\n  "a": 1}', f'{{\n  "a": 1,\n  {E}\n}}'),
    "nested-key-ignored": (
        f'{{\n  "x": {{\n    {OLD}\n  }}\n}}',
        f'{{\n  "x": {{\n    {OLD}\n  }},\n  {E}\n}}',
    ),
    "comment_tokens_in_strings": (
        '{\n  "url": "http://x", "a": "/* nope */"\n}',
        f'{{\n  "url": "http://x", "a": "/* nope */",\n  {E}\n}}',
    ),
    "crlf": ('{\r\n  "a": 1\r\n}', f'{{\r\n  "a": 1,\r\n  {E}\r\n}}'),
    "bom": ("﻿{}", f"﻿{{\n  {E}\n}}"),
    "empty-file": ("", f"{{\n  {E}\n}}\n"),
    "whitespace-file": ("  \n", f"{{\n  {E}\n}}\n"),
    "update-keeps-comments": (
        f'{{\n  // before\n  "claudeCode.claudeProcessWrapper": "/old", // keep\n  "a": 1\n}}',
        f'{{\n  // before\n  {E}, // keep\n  "a": 1\n}}',
    ),
    "update-null": (
        f'{{\n  "{SETTING_KEY}": null\n}}',
        f"{{\n  {E}\n}}",
    ),
}


@pytest.mark.parametrize("text,expected", PATCH_CASES.values(), ids=PATCH_CASES.keys())
def test_patch_jsonc(text, expected):
    res = patch_jsonc(text, SETTING_KEY, W)
    assert res.text == expected
    assert res.status in ("added", "updated")


def test_patch_reports_previous_value():
    res = patch_jsonc(f'{{"{SETTING_KEY}": "/old"}}', SETTING_KEY, W)
    assert (res.status, res.previous) == ("updated", "/old")
    res = patch_jsonc(f'{{"{SETTING_KEY}": null}}', SETTING_KEY, W)
    assert (res.status, res.previous) == ("updated", "null")


def test_patch_same_value_is_already():
    text = f'{{\n  // c\n  {E}\n}}'
    res = patch_jsonc(text, SETTING_KEY, W)
    assert (res.status, res.text) == ("already", text)


def test_patch_escapes_value():
    res = patch_jsonc("{}", SETTING_KEY, '/my bin/a"b\\c')
    assert json.loads(res.text)[SETTING_KEY] == '/my bin/a"b\\c'
    assert r'"/my bin/a\"b\\c"' in res.text


BAD = {
    "comments-only": "// nothing\n",
    "array-root": "[]",
    "unclosed-string": '{"a": "x',
    "unclosed-block-comment": "{ /* c }",
    "missing-comma": '{"a": 1 "b": 2}',
    "truncated": '{"a": 1',
    "trailing-garbage": '{"a": 1} x',
    "double-comma": '{"a": 1,, "b": 2}',
    "bad-escape": '{"a": "\\x"}',
    "raw-newline-in-string": '{"a": "x\ny"}',
    "leading-zero": '{"a": 01}',
    "duplicate-key": f'{{{E}, {E}}}',
    "too-deep": '{"a": ' + "[" * 300 + "]" * 300 + "}",
}


@pytest.mark.parametrize("text", BAD.values(), ids=BAD.keys())
def test_patch_rejects_malformed(text):
    with pytest.raises(SettingsError):
        patch_jsonc(text, SETTING_KEY, W)


def test_write_creates_missing_file(tmp_path):
    path = tmp_path / "Code" / "User" / "settings.json"
    res = write_wrapper(W, path)
    assert (res.status, res.backup) == ("created", None)
    assert json.loads(path.read_text()) == {SETTING_KEY: W}
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert os.listdir(path.parent) == ["settings.json"]


def test_write_empty_file_backs_up_original(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("")
    res = write_wrapper(W, path)
    assert res.status == "added"
    assert json.loads(path.read_text()) == {SETTING_KEY: W}
    assert res.backup == tmp_path / "settings.json.bak"
    assert res.backup.read_bytes() == b""


def test_write_already_leaves_file_and_backup_alone(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(f"{{\n  // c\n  {E}\n}}\n")
    before = path.stat().st_mtime_ns
    res = write_wrapper(W, path)
    assert (res.status, res.previous) == ("already", W)
    assert path.stat().st_mtime_ns == before
    assert os.listdir(tmp_path) == ["settings.json"]


def test_write_update_keeps_comments_and_backs_up_exact_bytes(tmp_path):
    path = tmp_path / "settings.json"
    original = f'{{\r\n  // keep me\r\n  "claudeCode.claudeProcessWrapper": "/old", // inline\r\n  "a": 1\r\n}}\r\n'.encode()
    path.write_bytes(original)
    res = write_wrapper(W, path)
    assert (res.status, res.previous) == ("updated", "/old")
    assert res.backup.read_bytes() == original
    assert path.read_bytes() == f'{{\r\n  // keep me\r\n  {E}, // inline\r\n  "a": 1\r\n}}\r\n'.encode()


def test_write_symlink_stays_symlink_and_mode_preserved(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text('{\n  "a": 1\n}\n')
    real.chmod(0o640)
    link = tmp_path / "User" / "settings.json"
    link.parent.mkdir()
    link.symlink_to(real)
    res = write_wrapper(W, link)
    assert link.is_symlink()
    assert json.loads(real.read_text()) == {"a": 1, SETTING_KEY: W}
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    assert res.backup == real.parent / "settings.json.bak"
    assert res.backup.read_text() == '{\n  "a": 1\n}\n'
    assert not (link.parent / "settings.json.bak").exists()


FAIL_CLOSED = {
    "comments-only": b"// c\n",
    "array-root": b"[]",
    "unclosed-string": b'{"a": "x',
    "unclosed-comment": b"{ /* c }",
    "truncated": b'{"a": 1',
    "trailing-garbage": b'{"a": 1} x',
    "duplicate-key": f"{{{E}, {E}}}".encode(),
    "invalid-utf8": b'{"a": "\xff"}',
}


@pytest.mark.parametrize("data", FAIL_CLOSED.values(), ids=FAIL_CLOSED.keys())
def test_write_bad_file_untouched(tmp_path, data):
    path = tmp_path / "settings.json"
    path.write_bytes(data)
    with pytest.raises(SettingsError):
        write_wrapper(W, path)
    assert path.read_bytes() == data
    assert os.listdir(tmp_path) == ["settings.json"]


def test_write_directory_target_refused(tmp_path):
    path = tmp_path / "settings.json"
    path.mkdir()
    with pytest.raises(SettingsError, match="directory"):
        write_wrapper(W, path)
    assert os.listdir(tmp_path) == ["settings.json"]
