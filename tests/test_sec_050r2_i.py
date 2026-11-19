"""0.5.0 fix round 2, unit I (Security re-read, 2026-11-19): input, output and claims rows.

Every value here is made up: no real run, no real path, no personal data. Token-like
strings are built from parts at run time.
"""

from __future__ import annotations

import gc
import importlib.util
import json
import tracemalloc
from pathlib import Path

from donediff.models import OTHER
from donediff.readers import CODEX, GEMINI, QWEN

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mk_codex = _load("make_codex_050r2", FIXTURES / "codex" / "make_codex.py")
mk_gemini = _load("make_gemini_050r2", FIXTURES / "gemini" / "make_gemini.py")
mk_qwen = _load("make_qwen_050r2", FIXTURES / "qwen" / "make_qwen.py")


# -- I-1: the Codex, Gemini and Qwen readers keep only the input the engine reads --

_PADDED = {"query": "x", "description": "d" * 50, "file_path": "notes.md",
           "pad": [{} for _ in range(1000)]}


def _codex_rollout(tmp_path, n_calls, args):
    clock = mk_codex.Clock()
    lines = [clock.line("session_meta", {**mk_codex.meta("s1", "legacy"), "cwd": mk_codex.CWD})]
    for i in range(n_calls):
        lines.append(clock.line("response_item", {
            "type": "function_call", "name": "mcp__search__find", "call_id": f"c{i}",
            "arguments": json.dumps(args)}))
        lines.append(clock.line("response_item", {
            "type": "function_call_output", "call_id": f"c{i}", "output": "ok"}))
    path = tmp_path / "rollout-2026-06-10T12-00-00-codexdemo-pad-0001.jsonl"
    return mk_codex.write(path, lines)


def test_i1_codex_unmodelled_tool_keeps_only_read_keys(tmp_path):
    (event,) = CODEX.parse(_codex_rollout(tmp_path, 1, _PADDED)).tool_calls()
    assert event.tool_action == OTHER and event.tool_name == "mcp__search__find"
    assert event.tool_input == {"file_path": "notes.md"}


def test_i1_codex_padding_in_an_unmodelled_tool_is_not_kept(tmp_path):
    pad = {"query": "x", "pad": [{} for _ in range(300_000)]}  # ~0.9 MiB a line, all read
    path = _codex_rollout(tmp_path, 4, pad)
    gc.collect()
    tracemalloc.start()
    try:
        session = CODEX.parse(path)
        gc.collect()
        kept = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    assert len(session.tool_calls()) == 4
    assert all(e.tool_input == {} for e in session.tool_calls())
    assert kept < 2 * path.stat().st_size, kept  # was ~25x


def test_i1_gemini_unmodelled_tool_keeps_only_read_keys(tmp_path):
    clock = mk_gemini.Clock()
    records = [mk_gemini.meta(), mk_gemini.user(clock, "u1", "Find it."),
               mk_gemini.gemini(clock, "g1", "", [
                   mk_gemini.call(clock, "c1", "google_web_search", _PADDED, "success",
                                  mk_gemini.output("c1", "google_web_search", "ok"))])]
    path = mk_gemini.write(tmp_path / "session-2026-06-10T12-00-6f1c2d3e.jsonl", records)
    (event,) = GEMINI.parse(path).tool_calls()
    assert event.tool_action == OTHER and event.result_seen
    assert event.tool_input == {"file_path": "notes.md"}


def test_i1_qwen_unmodelled_tool_keeps_only_read_keys(tmp_path):
    chat = mk_qwen.Chat()
    chat.user("Find it.")
    chat.assistant(calls=[mk_qwen.call("c1", "web_search", _PADDED)])
    chat.result("c1", "web_search", output="ok")
    path = mk_qwen.write(tmp_path / f"{mk_qwen.SID}.jsonl", chat.records)
    (event,) = QWEN.parse(path).tool_calls()
    assert event.tool_action == OTHER and event.result_seen
    assert event.tool_input == {"file_path": "notes.md"}
