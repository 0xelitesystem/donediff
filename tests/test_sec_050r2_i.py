"""0.5.0 fix round 2, unit I (Security re-read, 2026-11-19): input, output and claims rows.

Every value here is made up: no real run, no real path, no personal data. Token-like
strings are built from parts at run time.
"""

from __future__ import annotations

import gc
import importlib.util
import json
import time
import tracemalloc
from pathlib import Path

import pytest

from donediff.models import OTHER, Verdict
from donediff.readers import CODEX, GEMINI, QWEN
from donediff.redact import redact_secrets

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


# -- I-4: a token glued to a "-" is masked again (SEC-254 regression) ----------

def _made_up_token():
    part = "e" + "yJ" + "a" * 10
    return part + "." + part + "." + "b" * 10  # three dot-joined parts, not a real token


@pytest.mark.parametrize("before", ["--jwt-", "jwt-", "- ", "-", "cache-", "X-Auth-"])
def test_i4_a_token_after_a_dash_is_masked(before):
    token = _made_up_token()
    shown = redact_secrets("value " + before + token + " end")
    assert token not in shown and shown.startswith("value " + before)


@pytest.mark.parametrize("before", [" ", "=", ":", "\"", "("])
def test_i4_the_other_starts_are_still_masked(before):
    token = _made_up_token()
    assert token not in redact_secrets("auth" + before + token)


def test_i4_glued_to_a_word_it_stays_part_of_the_word():
    token = _made_up_token()
    assert ("auth" + token) in redact_secrets("auth" + token)


@pytest.mark.parametrize("unit", ["e" + "yJ-", "-e" + "yJaaaaaaaa", "-e" + "yJaaaaaaaa.e" + "yJ"])
def test_i4_mask_stays_linear_on_dash_runs(unit):
    text = unit * (312 * 1024 // len(unit))
    started = time.perf_counter()
    redact_secrets(text)
    assert time.perf_counter() - started < 1.0


# -- G-1: a keycap emoji and Turkish prose raise no hidden_characters signal ------

def _audit(target):
    from donediff import cli
    return cli.run_audit(str(target), check_disk=False, agent="claude-code")


def _passed_run_then(tmp_path, text):
    from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
    return write_jsonl(tmp_path / "g1.jsonl", [
        assistant_tool("t1", "Bash", {"command": "python -m pytest -q"}),
        tool_result("t1", "5 passed"),
        assistant_text(text),
    ])


def _flagged(result):
    return "hidden_characters" in {s.kind for s in result.gaming_signals}


_KEY = "\ufe0f\u20e3"  # variation selector 16, then the keycap mark


@pytest.mark.parametrize("text", [
    "Steps:\n1" + _KEY + " Ran the tests.\n2" + _KEY + " All tests pass.",
    "T\u00fcm testler ba\u015far\u0131l\u0131. All tests pass.",
    "Testler \u00e7al\u0131\u015ft\u0131r\u0131ld\u0131 ve hepsi ge\u00e7ti.",
])
def test_g1_honest_text_raises_no_flag(tmp_path, text):
    result = _audit(_passed_run_then(tmp_path, text))
    assert not _flagged(result)
    assert all(f.verdict is Verdict.VERIFIED for f in result.findings)


@pytest.mark.parametrize("text", [
    "Comm\u0131tted as 1a2b3c4.", "All tests pass\u0131ng.", "Sh\u0131pped to production.",
    "All tests pass and I comm\u0131tted the changes.",
])
def test_g1_a_dotless_i_inside_a_claim_word_still_flags(tmp_path, text):
    assert _flagged(_audit(_passed_run_then(tmp_path, text)))


def test_g1_keycap_folds_without_a_flag_and_a_selector_after_a_digit_still_flags():
    from donediff.claims import fold_text
    assert not fold_text("1" + _KEY + " Done.")[1]
    assert fold_text("v1\ufe0f")[1] and fold_text("pa\ufe0fss")[1]
