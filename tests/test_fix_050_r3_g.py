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
