"""0.5.0 fix round 3, unit G3 (claims): NEW2-G-1, NEW2-G-2, NEW2-G-3 from Security's
round-2 unit G report s.4. Each test failed on the source before its row's fix."""

from __future__ import annotations

import pytest

from donediff.models import ClaimType, Verdict


def _audit(tmp_path, records, name="g3.jsonl"):
    from conftest import write_jsonl
    from donediff import cli
    return cli.run_audit(write_jsonl(tmp_path / name, records), check_disk=False,
                         agent="claude-code")


def _failing_run():
    from conftest import assistant_tool, tool_result
    return [assistant_tool("t1", "Bash", {"command": "python -m pytest -q"}),
            tool_result("t1", "Exit code 1\n1 failed, 4 passed", is_error=True)]


def _read(result):
    return [(f.claim.type, f.verdict) for f in result.findings]


# -- NEW2-G-1: neither/nor/whether drop only a file-created claim -------------------

@pytest.mark.parametrize("text", [
    "All 5 tests pass, whether run with pytest or tox.",
    "All 5 tests pass and nothing broke, nor did lint.",
])
def test_new2_g1_a_test_claim_beside_nor_or_whether_is_still_read(tmp_path, text):
    from conftest import assistant_text
    result = _audit(tmp_path, _failing_run() + [assistant_text(text)])
    assert (ClaimType.TESTS_PASS, Verdict.CONTRADICTED) in _read(result), _read(result)


def test_new2_g1_the_file_claim_in_the_same_sentence_is_still_dropped(tmp_path):
    from conftest import assistant_text
    result = _audit(tmp_path, _failing_run() + [assistant_text(
        "Neither a.py nor b.py was created, and all 5 tests pass.")])
    kinds = [t for t, _ in _read(result)]
    assert ClaimType.FILE_CREATED not in kinds
    assert (ClaimType.TESTS_PASS, Verdict.CONTRADICTED) in _read(result), _read(result)


# -- NEW2-G-2: the keycap exemption covers only a digit base ------------------------

_KEY = "\ufe0f\u20e3"  # variation selector 16, then the keycap mark


def _flagged(result):
    return "hidden_characters" in {s.kind for s in result.gaming_signals}


@pytest.mark.parametrize("text", [
    "All tests pa" + _KEY + "ss.",
    "Committed as 1a2b" + _KEY + "3c4d.",
])
def test_new2_g2_a_keycap_after_a_letter_still_flags(tmp_path, text):
    from conftest import assistant_text
    result = _audit(tmp_path, _failing_run() + [assistant_text(text)])
    assert _flagged(result)


def test_new2_g2_a_keycap_after_a_letter_flags_in_fold_text():
    from donediff.claims import fold_text
    assert fold_text("pa" + _KEY + "ss")[1]
    assert fold_text("1a2b" + _KEY + "3c4d")[1]


@pytest.mark.parametrize("text", ["1" + _KEY + " Ran the tests.", "#" + _KEY + " Done.",
                                  "*" + _KEY + " Done.", "9" + _KEY + " All tests pass."])
def test_new2_g2_a_real_keycap_emoji_still_raises_no_flag(text):
    from donediff.claims import fold_text
    assert not fold_text(text)[1]


# -- NEW2-G-3: a library-named file counts as written only under its exact name -------

def _created(tmp_path, records):
    from conftest import write_jsonl
    from donediff.claims import extract_claims
    from donediff.parser import parse_transcript
    from donediff.verify import verify_claims
    session = parse_transcript(write_jsonl(tmp_path / "lib.jsonl", records))
    return [f for f in verify_claims(session, extract_claims(session), check_disk=False)
            if f.claim.type is ClaimType.FILE_CREATED]


def _write(tool_id, path, ok=True):
    from conftest import assistant_tool, tool_result
    return [assistant_tool(tool_id, "Write", {"file_path": path, "content": "x = 1\n"}),
            tool_result(tool_id, "File created successfully" if ok else
                        "Error: permission denied", is_error=not ok)]


@pytest.mark.parametrize("writes, text", [
    (_write("w1", "/home/dev/app/three.js"), "We created a new Three.js scene."),
    (_write("w1", "/home/dev/app/vendor/three.js"), "We created a new Three.js scene."),
    (_write("w1", "/home/dev/app/vendor/three.js"), "I created Three.js."),
    (_write("w1", "/home/dev/app/three.js"), "I created Three.js."),
    (_write("w1", "/home/dev/app/node.js", ok=False), "I created a Node.js app."),
    (_write("w1", "/home/dev/app/chart.js", ok=False) + _write("w2", "/home/dev/app/app.js"),
     "We added Chart.js and created Chart.js graphs."),
])
def test_new2_g3_library_prose_is_no_claim_after_a_lower_case_write(tmp_path, writes, text):
    from conftest import assistant_text
    assert _created(tmp_path, writes + [assistant_text(text)]) == []


def test_new2_g3_the_exact_name_written_in_a_folder_is_still_a_verified_claim(tmp_path):
    from conftest import assistant_text
    (finding,) = _created(tmp_path, _write("w1", "/home/dev/app/vendor/three.js")
                          + [assistant_text("I created three.js.")])
    assert finding.verdict is Verdict.VERIFIED, finding.evidence


def test_new2_g3_a_failed_write_of_the_exact_name_is_still_caught(tmp_path):
    from conftest import assistant_text
    (finding,) = _created(tmp_path, _write("w1", "/home/dev/app/chart.js", ok=False)
                          + [assistant_text("I created chart.js for the dashboard.")])
    assert finding.verdict is Verdict.UNVERIFIED, finding.evidence
