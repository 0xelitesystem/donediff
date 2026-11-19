"""0.5.0 one read (Security, 2026-11-18): rows SEC-250..259 and the leftovers it filed.

Every value here is made up: no real run, no real path, no personal data.
"""

from __future__ import annotations

import json
import re
import time

import pytest

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
from donediff import cli, parser
from donediff.models import Verdict


def audit(target):
    return cli.run_audit(str(target), check_disk=False, agent="claude-code")


# -- SEC-258: the value counter runs in linear time ----------------------------

def _cut_line(quotes_kb: int = 8) -> str:
    """A Claude Code line cut mid-write inside its big string: code-like
    output (two quotes every 37 characters), then 1 MiB of plain text."""
    payload = ('print("value") # ' + "y" * 20) * (quotes_kb * 1024 // 2)
    line = json.dumps({"type": "user", "message": {"content": payload + "z" * (1 << 20)}})
    return line[: len(line) - 100]


def test_value_count_is_linear_on_a_line_cut_mid_string():
    cut = _cut_line()
    assert len(cut) > parser._COUNT_FROM and cut.count('"') > 8000
    for text in (cut, cut.encode()):
        started = time.perf_counter()
        assert not parser.too_many_values(text)
        assert time.perf_counter() - started < 2.0, type(text).__name__


def test_claude_code_line_cut_mid_write_is_skipped_quickly(tmp_path):
    rest = [assistant_tool("t1", "Bash", {"command": "pytest -q"}),
            tool_result("t1", "3 passed in 0.10s"),
            assistant_text("All tests pass.")]
    path = tmp_path / "cut.jsonl"
    path.write_text(_cut_line() + "\n" + "\n".join(json.dumps(r) for r in rest), encoding="utf-8")
    started = time.perf_counter()
    result = audit(path)
    assert time.perf_counter() - started < 5.0
    assert result.session.unrecognised == 1
    assert [f.verdict for f in result.findings] == [Verdict.VERIFIED]


_OLD_STRING = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"')  # the SEC-241 counter: linear on valid JSON


@pytest.mark.parametrize("doc", [
    {"a": [1, 2, {"b": "x,[{\\\"y"}], "c": "q\"[,{\\"},
    [{"k": "\\\\", "v": ",", "w": "\\\"[{\\\\\""}] * 50,
    {"text": "a, b, [c], {d}, \\\"e\\\", é中, \\u0022 ," * 40, "n": [[], {}, [{}], None]},
])
def test_value_count_is_unchanged_on_valid_json(doc, monkeypatch):
    text = json.dumps(doc)
    bare = _OLD_STRING.sub("", text)
    count = bare.count(",") + bare.count("[") + bare.count("{")
    monkeypatch.setattr(parser, "_COUNT_FROM", 0)
    for sample in (text, text.encode()):
        monkeypatch.setattr(parser, "MAX_VALUES", count)
        assert not parser.too_many_values(sample)
        monkeypatch.setattr(parser, "MAX_VALUES", count - 1)
        assert parser.too_many_values(sample)
