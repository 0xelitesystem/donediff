"""SEC-197: a shell run with no result in the log is never VERIFIED or "exit 0".

A tool call whose result never arrives (the session was cut off or killed)
reads UNVERIFIED and counts against the score; a run with a result keeps its
verdict.
"""

from __future__ import annotations

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl

from donediff import cli
from donediff.cli import run_audit
from donediff.models import ClaimType, Verdict

CLAIM = "All tests pass."


def _tests_finding(path):
    result = run_audit(path, check_disk=False)
    found = [f for f in result.findings if f.claim.type == ClaimType.TESTS_PASS]
    assert len(found) == 1
    return found[0], result


def test_tool_call_without_result_is_unverified(tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [
        assistant_tool("t1", "Bash", {"command": "pytest -q"}),
        assistant_text(CLAIM),
    ])
    finding, _ = _tests_finding(path)
    assert finding.verdict == Verdict.UNVERIFIED
    assert "no result for this run in the log" in finding.evidence
    assert "exit 0" not in finding.evidence


def test_control_result_present_is_verified(tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [
        assistant_tool("t1", "Bash", {"command": "pytest -q"}),
        tool_result("t1", "3 passed in 0.1s"),
        assistant_text(CLAIM),
    ])
    finding, _ = _tests_finding(path)
    assert finding.verdict == Verdict.VERIFIED


def test_missing_result_counts_against_fail_under(tmp_path, capsys):
    with_result = write_jsonl(tmp_path / "ok.jsonl", [
        assistant_tool("t1", "Bash", {"command": "pytest -q"}),
        tool_result("t1", "3 passed in 0.1s"),
        assistant_text(CLAIM),
    ])
    without = write_jsonl(tmp_path / "cut.jsonl", [
        assistant_tool("t1", "Bash", {"command": "pytest -q"}),
        assistant_text(CLAIM),
    ])
    ok = run_audit(with_result, check_disk=False)
    cut = run_audit(without, check_disk=False)
    assert cut.score < ok.score
    assert cli.main(["audit", without, "--no-disk-check", "--fail-under", str(ok.score)]) != 0
    capsys.readouterr()
