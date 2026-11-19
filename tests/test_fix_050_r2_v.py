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
