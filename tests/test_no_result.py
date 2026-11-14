"""SEC-197: a shell run with no result in the log is never VERIFIED or "exit 0".

Cases A and B are Codex shapes, CC is the Claude Code shape (a tool call whose
result never arrives). Controls C and D keep their verdicts.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl

from donediff import cli
from donediff.cli import run_audit
from donediff.models import ClaimType, Verdict

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "make_codex", ROOT / "tests" / "fixtures" / "codex" / "make_codex.py")
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)

CLAIM = "All tests pass."


def _rollout(tmp_path, call_records):
    clock = mk.Clock()
    records = [clock.line("session_meta", mk.meta("noresult-0001", "legacy"))]
    records += [clock.line(rtype, payload) for rtype, payload in call_records]
    records.append(clock.line("response_item", mk.message("assistant", CLAIM)))
    return str(mk.write(tmp_path / "rollout-2026-06-10T12-00-00-noresult-0001.jsonl", records))


def _exec_call(call_id="c1"):
    return ("response_item", {"type": "function_call", "name": "exec_command", "call_id": call_id,
                              "arguments": json.dumps({"cmd": "pytest -q"})})


def _tests_finding(path, agent):
    result = run_audit(path, check_disk=False, agent=agent)
    found = [f for f in result.findings if f.claim.type == ClaimType.TESTS_PASS]
    assert len(found) == 1
    return found[0], result


def test_a_codex_exec_command_without_output_is_unverified(tmp_path):
    finding, _ = _tests_finding(_rollout(tmp_path, [_exec_call()]), "codex")
    assert finding.verdict == Verdict.UNVERIFIED
    assert "no result for this run in the log" in finding.evidence
    assert "exit 0" not in finding.evidence


def test_b_codex_local_shell_call_without_status_is_unverified(tmp_path):
    call = ("response_item", {"type": "local_shell_call", "call_id": "c1",
                              "action": {"type": "exec", "command": ["pytest", "-q"]}})
    finding, _ = _tests_finding(_rollout(tmp_path, [call]), "codex")
    assert finding.verdict == Verdict.UNVERIFIED
    assert "no result for this run in the log" in finding.evidence


def test_cc_claude_code_tool_call_without_result_is_unverified(tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [
        assistant_tool("t1", "Bash", {"command": "pytest -q"}),
        assistant_text(CLAIM),
    ])
    finding, _ = _tests_finding(path, "claude-code")
    assert finding.verdict == Verdict.UNVERIFIED
    assert "no result for this run in the log" in finding.evidence
    assert "exit 0" not in finding.evidence


def test_control_c_codex_exit_zero_header_is_verified(tmp_path):
    output = ("response_item", {"type": "function_call_output", "call_id": "c1",
                                "output": mk.exec_output(0, "3 passed in 0.1s")})
    finding, _ = _tests_finding(_rollout(tmp_path, [_exec_call(), output]), "codex")
    assert finding.verdict == Verdict.VERIFIED


def test_control_d_codex_status_completed_without_output_stays_unverified(tmp_path):
    call = ("response_item", {"type": "local_shell_call", "call_id": "c1", "status": "completed",
                              "action": {"type": "exec", "command": ["pytest", "-q"]}})
    finding, _ = _tests_finding(_rollout(tmp_path, [call]), "codex")
    assert finding.verdict == Verdict.UNVERIFIED
    assert "no exit code in the log (status completed)" in finding.evidence


def test_control_claude_code_result_present_is_verified(tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [
        assistant_tool("t1", "Bash", {"command": "pytest -q"}),
        tool_result("t1", "3 passed in 0.1s"),
        assistant_text(CLAIM),
    ])
    finding, _ = _tests_finding(path, "claude-code")
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
