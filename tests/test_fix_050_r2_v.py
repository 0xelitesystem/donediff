"""0.5.0 fix round 2, unit V (Security re-read V, 2026-11-19): the verdict engine.

Every command below exits 0 in bash when the check fails, unless a test says the
status is kept. Every value here is made up: no real run, no real path, no personal data.
"""

from __future__ import annotations

import pytest

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
from donediff import cli
from donediff.evidence import masking
from donediff.models import Verdict

_QUIET = "python -m pytest -q >/dev/null 2>&1"


def audit(target):
    return cli.run_audit(str(target), check_disk=False, agent="claude-code")


def _one_run(tmp_path, command, output="", claim="All tests pass.", is_error=False):
    return write_jsonl(tmp_path / "s.jsonl", [
        assistant_tool("t1", "Bash", {"command": command}),
        tool_result("t1", output, is_error=is_error),
        assistant_text(claim),
    ])


def _kinds(command: str) -> list[str]:
    return [kind for kind, _, _ in masking(command)]


def _verdict(tmp_path, command, output="", claim="All tests pass."):
    result = audit(_one_run(tmp_path, command, output, claim))
    (finding,) = result.findings
    return finding, {s.kind for s in result.gaming_signals}


# -- V-1: `& wait` keeps the status only as the call's last statement ---------

@pytest.mark.parametrize("command", [
    _QUIET + " & wait $! || true",
    _QUIET + " & wait $!; true",
    _QUIET + " & true & wait $!",
    _QUIET + " & wait -f",
])
def test_v1_a_wait_that_does_not_return_the_check_status_is_a_mask(tmp_path, command):
    assert "masked_exit_code" in _kinds(command)
    finding, _ = _verdict(tmp_path, command)
    assert finding.verdict is Verdict.UNVERIFIED


@pytest.mark.parametrize("command", [
    _QUIET + " && echo ok & wait $!",
    _QUIET + " & wait %1",
    _QUIET + " & wait -n",
    _QUIET + " & wait ${!}",
])
def test_v1_a_wait_for_the_check_job_keeps_its_status(command):
    assert _kinds(command) == []


# -- V-2: a COMMAND_OK claim reads the trap too --------------------------------

_HELLO = "python hello.py"
_WORKED = "`python hello.py` worked."


@pytest.mark.parametrize("command", [
    "trap 'exit 0' EXIT; " + _HELLO,
    "bash -c \"trap 'exit 0' EXIT; " + _HELLO + "\"",
    "trap 'exit 0' ERR; " + _HELLO,
    "trap { exit 0 }; " + _HELLO,
])
def test_v2_a_command_claim_after_a_zero_exit_trap_is_unverified(tmp_path, command):
    finding, _ = _verdict(tmp_path, command, claim=_WORKED)
    assert finding.verdict is Verdict.UNVERIFIED
    assert "hidden by `trap" in finding.evidence


@pytest.mark.parametrize("command", [
    _HELLO,
    "trap 'echo bye' EXIT; " + _HELLO,
    "trap 'rc=$?; rm -f t.txt; exit $rc' EXIT; " + _HELLO,
])
def test_v2_a_command_claim_without_a_zero_exit_trap_still_verifies(tmp_path, command):
    finding, _ = _verdict(tmp_path, command, claim=_WORKED)
    assert finding.verdict is Verdict.VERIFIED


# -- V-3: the status-kept reader -------------------------------------------------

@pytest.mark.parametrize("command", [
    _QUIET + "; [ $? -ne 0 ] && exit",
    _QUIET + "; if [ $? -ne 0 ]; then exit; fi",
    _QUIET + "; if [ $? -ne 0 ]; then exit \"0\"; fi",
    _QUIET + "; rc=$?; rc=0; exit $rc",
    _QUIET + "; rc=$?; unset rc; exit $rc",
    _QUIET + "; rc=$?; exit $RC",
    _QUIET + "; rc=$?; exit ${rc:+0}",
    _QUIET + "; [ $? -eq 1 ] || exit 1",
    _QUIET + "; [ $? -ne 0 ] || exit 1",
    _QUIET + "; if [ $? -eq 0 ]; then exit 1; fi",
])
def test_v3_a_status_test_that_exits_zero_on_failure_keeps_nothing(tmp_path, command):
    assert _kinds(command)
    finding, _ = _verdict(tmp_path, command)
    assert finding.verdict is Verdict.UNVERIFIED


@pytest.mark.parametrize("command", [
    _QUIET + "; [ $? -ne 0 ] && exit 1",
    _QUIET + "; [ \"$?\" != 0 ] && exit 2",
    _QUIET + "; [ $? -eq 0 ] || exit",
    _QUIET + "; [ $? -eq 0 ] || exit 1",
    _QUIET + "; [ $? -ne 0 ] && { echo failed; exit 1; }",
    _QUIET + "; [ $? -ne 0 ] && echo failed && exit 1",
    _QUIET + "; if [ $? -ne 0 ]; then echo failed; exit 1; fi",
    _QUIET + "; if [ $? -eq 0 ]; then echo ok; else exit 1; fi",
    _QUIET + "; rc=$?; exit $rc",
    _QUIET + "; rc=$?; exit \"${rc}\"",
    "python -m pytest -q; if ($LASTEXITCODE -ne 0) { exit 1 }",
    "python -m pytest -q; if (-not $?) { exit 1 }",
])
def test_v3_a_status_test_that_exits_with_a_failure_keeps_the_status(command):
    assert _kinds(command) == []


# -- SEC-251 leftover: trap forms ------------------------------------------------

@pytest.mark.parametrize("command", [
    "trap 'exit \"0\"' EXIT; " + _QUIET,
    "trap 'exit +0' EXIT; " + _QUIET,
    "trap 'exit $((0))' EXIT; " + _QUIET,
    "trap exit\\ 0 EXIT; " + _QUIET,
    "trap $'exit 0' EXIT; " + _QUIET,
    "f(){ trap 'exit 0' EXIT; }; f; " + _QUIET,
    "if true; then trap 'exit 0' EXIT; fi; " + _QUIET,
    "builtin trap 'exit 0' EXIT; " + _QUIET,
    "command trap 'exit 0' EXIT; " + _QUIET,
    "eval \"trap 'exit 0' EXIT\"; " + _QUIET,
    "g(){ exit 0; }; trap g EXIT; " + _QUIET,
    "trap 'exec true' EXIT; " + _QUIET,
    "trap 'x=0; exit \"$x\"' EXIT; " + _QUIET,
])
def test_sec251_leftover_trap_forms_swallow_the_failure(tmp_path, command):
    assert "swallowed_failure" in _kinds(command)
    finding, signals = _verdict(tmp_path, command, "5 passed")
    assert finding.verdict is Verdict.UNVERIFIED
    assert "swallowed_failure" in signals


@pytest.mark.parametrize("command", [
    "trap 'rc=$?; exit \"$rc\"' EXIT; python -m pytest -q",
    "trap 'exit $?' EXIT; python -m pytest -q",
    "trap { exit $LASTEXITCODE }; python -m pytest -q",
    "trap \"echo done\" EXIT; python -m pytest -q",
    "trap exit\\ 1 EXIT; python -m pytest -q",
    "command trap 'exit 0' INT; python -m pytest -q",
    "echo \"trap 'exit 0' EXIT\"; python -m pytest -q",
])
def test_sec251_leftover_traps_that_keep_the_status_are_no_mask(command):
    assert _kinds(command) == []


# -- SEC-250 leftover: a marker printed by another statement before the pipe ----

@pytest.mark.parametrize("command", [
    "echo '5 passed'; " + _QUIET + " | tail -1",
    "echo '5 passed' && " + _QUIET + " | tail -1",
    "echo '5 passed' & " + _QUIET + " | tail -1",
    "printf '5 passed\\n'; python -m pytest -q 2>&1 | tail -1",
])
def test_sec250_leftover_a_marker_from_another_statement_is_no_proof(tmp_path, command):
    finding, signals = _verdict(tmp_path, command, "5 passed")
    assert finding.verdict is Verdict.UNVERIFIED
    assert "in the same call, so a pass marker in the output is no proof" in finding.evidence
    assert "masked_exit_code" in signals


@pytest.mark.parametrize("command", [
    "cd app && python -m pytest -q 2>&1 | tail -3",
    "export CI=1; python -m pytest -q 2>&1 | tail -3",
    "CI=1; python -m pytest -q 2>&1 | tail -3",
    "source .venv/bin/activate && python -m pytest -q 2>&1 | tail -3",
    "bash -c \"python -m pytest -q | tail -3\"",
    "python -m pytest -q 2>&1 | tee log.txt | tail -3",
])
def test_sec250_leftover_silent_statements_still_let_a_marker_verify(tmp_path, command):
    finding, _ = _verdict(tmp_path, command, "12 passed in 0.2s")
    assert finding.verdict is Verdict.VERIFIED


# -- SEC-244 leftover: a filter is judged by what it is given, not its name -----

@pytest.mark.parametrize("command, output, name", [
    (_QUIET + " | cat <<< '5 passed'", "5 passed", "cat"),
    (_QUIET + " | cat - <(echo '5 passed')", "5 passed", "cat"),
    (_QUIET + " | tee >(echo '5 passed')", "5 passed", "tee"),
    ("python -m pytest -q 2>&1 | tail -c 18", "5 passed in 0.12s", "tail"),
    ("python -m pytest -q 2>&1 | head --bytes 40", "5 passed in 0.12s", "head"),
    ("python -m pytest -q 2>&1 | tail -c18", "5 passed in 0.12s", "tail"),
    ("python -m pytest -q 2>&1 | cat old-log.txt", "5 passed in 0.12s", "cat"),
])
def test_sec244_leftover_a_filter_with_input_of_its_own_or_a_byte_cut_is_no_proof(
        tmp_path, command, output, name):
    finding, _ = _verdict(tmp_path, command, output)
    assert finding.verdict is Verdict.UNVERIFIED
    assert f"a pipe through `{name}`" in finding.evidence


@pytest.mark.parametrize("command", [
    "python -m pytest -q 2>&1 | tail -n 3",
    "python -m pytest -q 2>&1 | tail --lines=3",
    "python -m pytest -q 2>&1 | head -n +20",
    "python -m pytest -q 2>&1 | cat -",
    "python -m pytest -q | Select-Object -Last 5",
])
def test_sec244_leftover_line_cuts_still_verify(tmp_path, command):
    finding, _ = _verdict(tmp_path, command, "12 passed in 0.2s")
    assert finding.verdict is Verdict.VERIFIED
