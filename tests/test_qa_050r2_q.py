"""0.5.0 fix round 2, unit Q (QA re-check of 2b1391a, 2026-11-19): QA-029, QA-031, QA-030.

Every value here is made up: no real run, no real path, no personal data.
"""

from __future__ import annotations

import gc
import importlib.util
import tracemalloc
from pathlib import Path

from donediff.models import OTHER, Verdict
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


# -- QA-031: a successful MultiEdit backs an edit claim, like Edit ------------------

def _file_claim(tmp_path, tool, tool_input, result="ok", is_error=False,
                text="I edited `app.py`."):
    from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
    from donediff.claims import extract_claims
    from donediff.models import ClaimType
    from donediff.parser import parse_transcript
    from donediff.verify import verify_claims

    records = [assistant_tool("t1", tool, tool_input),
               tool_result("t1", result, is_error=is_error),
               assistant_text(text)]
    session = parse_transcript(write_jsonl(tmp_path / f"{tool}.jsonl", records))
    findings = [f for f in verify_claims(session, extract_claims(session), check_disk=False)
                if f.claim.type in (ClaimType.FILE_CREATED, ClaimType.FILE_EDITED)]
    assert len(findings) == 1, findings
    return findings[0]


_PAIR = {"old_string": "a = 1", "new_string": "a = 2"}


def test_qa031_multiedit_backs_an_edit_claim(tmp_path):
    finding = _file_claim(tmp_path, "MultiEdit",
                          {"file_path": "/home/dev/app/app.py", "edits": [_PAIR]})
    assert finding.verdict is Verdict.VERIFIED, finding.evidence
    assert "MultiEdit call for `app.py` found" in finding.evidence


def test_qa031_edit_control_still_verified(tmp_path):
    finding = _file_claim(tmp_path, "Edit", {"file_path": "/home/dev/app/app.py", **_PAIR})
    assert finding.verdict is Verdict.VERIFIED, finding.evidence


def test_qa031_failed_multiedit_backs_nothing(tmp_path):
    finding = _file_claim(tmp_path, "MultiEdit",
                          {"file_path": "/home/dev/app/app.py", "edits": [_PAIR]},
                          result="String to replace not found in file.", is_error=True)
    assert finding.verdict is Verdict.UNVERIFIED, finding.evidence
    assert "MultiEdit call for `app.py` did not succeed" in finding.evidence


def test_qa031_multiedit_to_another_file_backs_nothing(tmp_path):
    finding = _file_claim(tmp_path, "MultiEdit",
                          {"file_path": "/home/dev/app/other.py", "edits": [_PAIR]})
    assert finding.verdict is Verdict.UNVERIFIED, finding.evidence
