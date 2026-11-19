"""0.5.0 fix round 2, unit Q (QA re-check of 2b1391a, 2026-11-19): QA-029, QA-031, QA-030.

Every value here is made up: no real run, no real path, no personal data.
"""

from __future__ import annotations

import gc
import importlib.util
import tracemalloc
from pathlib import Path

from donediff.models import OTHER
from donediff.readers import GEMINI, QWEN

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mk_gemini = _load("make_gemini_050r2q", FIXTURES / "gemini" / "make_gemini.py")
mk_qwen = _load("make_qwen_050r2q", FIXTURES / "qwen" / "make_qwen.py")


# -- QA-029 (SEC-293 follow-up): the Gemini and Qwen readers hold no padding while replaying --

def _pad():
    return {"query": "x", "pad": [{} for _ in range(50_000)]}  # ~0.2 MiB a line


def _gemini_session(tmp_path, n_calls):
    clock = mk_gemini.Clock()
    records = [mk_gemini.meta(), mk_gemini.user(clock, "u1", "Find it.")]
    for i in range(n_calls):
        records.append(mk_gemini.gemini(clock, f"g{i}", "", [
            mk_gemini.call(clock, f"c{i}", "google_web_search", _pad(), "success",
                           mk_gemini.output(f"c{i}", "google_web_search", "ok"))]))
    return mk_gemini.write(tmp_path / f"session-2026-06-10T12-00-{n_calls:08x}.jsonl", records)


def _qwen_session(tmp_path, n_calls):
    chat = mk_qwen.Chat()
    chat.user("Find it.")
    for i in range(n_calls):
        chat.assistant(calls=[mk_qwen.call(f"c{i}", "web_search", _pad())])
        chat.result(f"c{i}", "web_search", output="ok")
    folder = tmp_path / f"q{n_calls}"
    folder.mkdir()
    return mk_qwen.write(folder / f"{mk_qwen.SID}.jsonl", chat.records)


def _peak(reader, path):
    gc.collect()
    tracemalloc.start()
    try:
        session = reader.parse(path)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return session, peak


def _flat(reader, make, tmp_path):
    small, peak_small = _peak(reader, make(tmp_path, 2))
    big_path = make(tmp_path, 8)
    big, peak_big = _peak(reader, big_path)
    assert len(small.tool_calls()) == 2 and len(big.tool_calls()) == 8
    assert all(e.tool_action == OTHER and e.tool_input == {} and e.result_seen
               for e in big.tool_calls())
    # held padding grows the peak with the number of calls (was ~4x from 2 to 8 calls)
    assert peak_big < 1.5 * peak_small, (peak_small, peak_big)
    assert peak_big < 6 * big_path.stat().st_size, (peak_big, big_path.stat().st_size)


def test_qa029_gemini_peak_stays_flat_as_padded_calls_grow(tmp_path):
    _flat(GEMINI, _gemini_session, tmp_path)


def test_qa029_qwen_peak_stays_flat_as_padded_calls_grow(tmp_path):
    _flat(QWEN, _qwen_session, tmp_path)
