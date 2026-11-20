"""0.5.0 fix round 4 (Security round-3 read, 2026-11-19): Gemini and Qwen readers.

SEC-308 (NEW3-I-1): text parts and functionResponses that hold nothing are not held item by item.
SEC-309 (NEW3-I-2): a tool call with no name is no tool call: it is counted unrecognised, not an Event.

Every value here is made up: no real run, no real path, no personal data.
"""

from __future__ import annotations

import gc
import importlib.util
import tracemalloc
from pathlib import Path

import pytest

from donediff.readers import GEMINI, QWEN

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
PADN = 50_000


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mk_gemini = _load("make_gemini_050r4", FIXTURES / "gemini" / "make_gemini.py")
mk_qwen = _load("make_qwen_050r4", FIXTURES / "qwen" / "make_qwen.py")


def _peak(reader, path):
    gc.collect()
    tracemalloc.start()
    try:
        session = reader.parse(path)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return session, peak


def _gemini_session(tmp_path, n_calls, kind):
    clock = mk_gemini.Clock()
    records = [mk_gemini.meta(), mk_gemini.user(clock, "u1", "Do it.")]
    for i in range(n_calls):
        cid = f"c{i}"
        call = mk_gemini.call(clock, cid, "google_web_search", {"query": "x"}, "success",
                              mk_gemini.output(cid, "google_web_search", "ok"))
        record = mk_gemini.gemini(clock, f"g{i}", "", [call])
        if kind == "txtparts":
            record["content"] = [{"text": ""} for _ in range(PADN)]
        elif kind == "frempty":
            call["result"] = call["result"] + [{"functionResponse": {}} for _ in range(PADN)]
        elif kind == "rtext":
            call["result"] = call["result"] + [{"text": ""} for _ in range(PADN)]
        elif kind == "calls":
            record["toolCalls"] = [call] + [{} for _ in range(PADN)]
        records.append(record)
    folder = tmp_path / f"g{kind}{n_calls}"
    folder.mkdir()
    return mk_gemini.write(folder / f"session-2026-06-10T12-00-{n_calls:08x}.jsonl", records)


def _qwen_session(tmp_path, n_calls, kind):
    chat = mk_qwen.Chat()
    chat.user("Do it.")
    for i in range(n_calls):
        cid = f"c{i}"
        record = chat.assistant(calls=[mk_qwen.call(cid, "web_search", {"query": "x"})])
        result = chat.result(cid, "web_search", output="ok")
        if kind == "txtparts":
            record["message"]["parts"] += [{"text": ""} for _ in range(PADN)]
        elif kind == "frempty":
            result["message"]["parts"] += [{"functionResponse": {}} for _ in range(PADN)]
        elif kind == "userfr":
            user = chat.user("next")
            user["message"]["parts"] += [{"functionResponse": {}} for _ in range(PADN)]
        elif kind == "thought":
            record["message"]["parts"] += [{"text": "x", "thought": True} for _ in range(PADN)]
        elif kind == "fcempty":
            record["message"]["parts"] += [{"functionCall": {}} for _ in range(PADN)]
    folder = tmp_path / f"q{kind}{n_calls}"
    folder.mkdir()
    return mk_qwen.write(folder / f"{mk_qwen.SID}.jsonl", chat.records)


def _flat(reader, make, tmp_path, kind):
    small, peak_small = _peak(reader, make(tmp_path, 2, kind))
    big, peak_big = _peak(reader, make(tmp_path, 8, kind))
    assert len(small.tool_calls()) == 2 and len(big.tool_calls()) == 8
    assert all(e.result_seen for e in big.tool_calls())
    assert peak_big < 1.5 * peak_small, (peak_small, peak_big)
    return big


# -- SEC-308 (NEW3-I-1) -------------------------------------------------------------

@pytest.mark.parametrize("kind", ["txtparts", "frempty", "rtext"])
def test_sec308_gemini_peak_stays_flat_for_empty_parts(tmp_path, kind):
    _flat(GEMINI, _gemini_session, tmp_path, kind)


@pytest.mark.parametrize("kind", ["txtparts", "frempty", "userfr", "thought"])
def test_sec308_qwen_peak_stays_flat_for_empty_parts(tmp_path, kind):
    _flat(QWEN, _qwen_session, tmp_path, kind)


def test_sec308_gemini_text_and_results_read_as_before(tmp_path):
    clock = mk_gemini.Clock()
    records = [mk_gemini.meta(),
               {**mk_gemini.user(clock, "u1", ""), "content": [{"text": "Fix"}, "", {"text": "it."}]},
               mk_gemini.gemini(clock, "g1", "", [
                   mk_gemini.call(clock, "e1", "replace", {"file_path": "app.py"}, "error",
                                  mk_gemini.error("e1", "replace", "no match")),
                   mk_gemini.call(clock, "w1", "write_file",
                                  {"file_path": "app.py", "content": "x = 1\n"}, "success",
                                  [{"text": "a"}, {"text": ""}, {"functionResponse": {}},
                                   {"functionResponse": {"response": {"output": "b"}}}])])]
    records[2]["content"] = [{"text": "plan", "thought": True}, {"text": "Done"}, {"text": "now."}]
    path = mk_gemini.write(tmp_path / "session-2026-06-10T12-00-0000aa01.jsonl", records)
    session = GEMINI.parse(path)
    texts = [e.text for e in session.events if e.text]
    assert texts == ["Done\nnow."]
    calls = session.tool_calls()
    assert [c.is_error for c in calls] == [True, False]
    assert calls[0].output == "no match"
    assert calls[1].output == "a\nb"
    assert session.prompt_starts == [0]


def test_sec308_qwen_text_runs_read_as_before(tmp_path):
    chat = mk_qwen.Chat()
    user = chat.user("Fix")
    user["message"]["parts"] += [{"text": ""}, {"text": "it."}]
    record = chat.assistant(calls=[mk_qwen.call("c1", "web_search", {"query": "x"})])
    record["message"]["parts"] = ([{"text": "a"}, {"text": "why", "thought": True}, {"text": "b"}]
                                  + record["message"]["parts"] + [{"text": "c"}, {"text": ""}])
    result = chat.result("c1", "web_search", output="ok")
    result["message"]["parts"] += [{"functionResponse": {}}, {"functionResponse": {"id": 5}}]
    path = mk_qwen.write(tmp_path / f"{mk_qwen.SID}.jsonl", chat.records)
    session = QWEN.parse(path)
    texts = [e.text for e in session.events if e.text]
    assert texts == ["a\nb", "c"]
    calls = session.tool_calls()
    assert len(calls) == 1 and calls[0].result_seen and calls[0].output == "ok"
    assert session.prompt_starts == [0]
