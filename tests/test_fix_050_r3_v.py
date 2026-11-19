"""0.5.0 fix round 3, unit V3 (Security re-read V round 2, 2026-11-19): the verdict
engine, FAIL CLOSED. A false UNVERIFIED is acceptable; a false VERIFIED is not.

Every command below exits 0 in bash when the check fails, unless a test says the
status is kept. Every value here is made up: no real run, no real path, no personal data.
"""

from __future__ import annotations

import pytest

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
from donediff import cli
from donediff.evidence import masking, zero_exit_trap
from donediff.models import Verdict

_QUIET = "python -m pytest -q >/dev/null 2>&1"
_HELLO = "python hello.py"
_WORKED = "`python hello.py` worked."


def audit(target):
    return cli.run_audit(str(target), check_disk=False, agent="claude-code")


def _verdict(tmp_path, command, output="", claim="All tests pass."):
    result = audit(write_jsonl(tmp_path / "s.jsonl", [
        assistant_tool("t1", "Bash", {"command": command}),
        tool_result("t1", output),
        assistant_text(claim),
    ]))
    (finding,) = result.findings
    return finding, {s.kind for s in result.gaming_signals}


def _kinds(command: str) -> list[str]:
    return [kind for kind, _, _ in masking(command)]


# -- Row 1, SEC-291 (V-2): a trap in command position whose handler is not -----
# -- provably status-keeping is a mask, for a named command and for a check ----

_SEC291_TRAPS = [
    "\\trap 'exit 0' EXIT",
    "'trap' 'exit 0' EXIT",
    "tr''ap 'exit 0' EXIT",
    "case x in x) trap 'exit 0' EXIT;; esac",
    "x=1 trap 'exit 0' EXIT",
    "time trap 'exit 0' EXIT",
    "t='exit 0'; trap \"$t\" EXIT",
    "trap 'true; exit $?' EXIT",
    "trap 'echo bye; exit' EXIT",
    "trap 'rc=$?; rc=0; exit $rc' EXIT",
    "trap 'rc=$?; read rc; exit $rc' EXIT",
    "trap \"exit $?\" EXIT",
    "trap 'exit '$? EXIT",
    "trap 'case 1 in 1) exit 0;; esac' EXIT",
    "trap 'source ./t.sh' EXIT",
    "trap 'eval exit 0' EXIT",
]


@pytest.mark.parametrize("trap", _SEC291_TRAPS)
def test_sec291_r3_a_command_claim_after_an_unproven_trap_is_unverified(tmp_path, trap):
    command = trap + "; " + _HELLO
    assert zero_exit_trap(command)
    finding, _ = _verdict(tmp_path, command, claim=_WORKED)
    assert finding.verdict is Verdict.UNVERIFIED
    assert "hidden by `trap" in finding.evidence


@pytest.mark.parametrize("trap", _SEC291_TRAPS)
def test_sec291_r3_a_check_after_an_unproven_trap_is_a_swallowed_failure(tmp_path, trap):
    command = trap + "; " + _QUIET
    assert "swallowed_failure" in _kinds(command)
    finding, signals = _verdict(tmp_path, command, "5 passed")
    assert finding.verdict is Verdict.UNVERIFIED
    assert "swallowed_failure" in signals


@pytest.mark.parametrize("trap", [
    "trap 'rm -f tmp.txt' EXIT",
    "trap 'echo bye' EXIT",
    "trap 'exit $?' EXIT",
    "trap 'exit' EXIT",
    "trap 'exit $LASTEXITCODE' EXIT",
    "trap 'rc=$?; echo \"status $rc\"; exit \"$rc\"' EXIT",
    "trap 'rc=$?; rm -f t.txt; exit ${rc}' EXIT",
    "trap 'exit 3' EXIT",
    "echo \"trap 'exit 0' EXIT\"",
    "grep -n trap notes.txt",
])
def test_sec291_r3_a_trap_that_provably_keeps_the_status_is_no_mask(tmp_path, trap):
    assert zero_exit_trap(trap + "; " + _HELLO) is None
    assert _kinds(trap + "; python -m pytest -q") == []
    finding, _ = _verdict(tmp_path, trap + "; " + _HELLO, claim=_WORKED)
    assert finding.verdict is Verdict.VERIFIED


# -- Row 2, SEC-251: generated text run in this shell, and a trap on any signal --

_SEC251_FORMS = [
    "source <(echo \"trap 'exit 0' EXIT\")",
    ". <(echo \"trap 'exit 0' EXIT\")",
    "eval \"$(echo \"trap 'exit 0' EXIT\")\"",
    "eval `echo trap true EXIT`",
    "c=\"trap 'exit 0' EXIT\"; eval \"$c\"",
    "source /dev/stdin <<< \"trap 'exit 0' EXIT\"",
    "if true; then source <(echo x); fi",
    "t=trap; $t 'exit 0' EXIT",
    "trap 'exit 0' INT TERM",
    "command trap 'exit 0' INT",
    "trap 'exit 0' USR1",
    "trap 'exit 0' $SIG",
    "trap 'exit 0' RETURN",
]


@pytest.mark.parametrize("form", _SEC251_FORMS)
def test_sec251_r3_generated_text_or_a_trap_on_any_signal_is_a_mask(tmp_path, form):
    command = form + "; " + _QUIET
    assert "swallowed_failure" in _kinds(command)
    finding, signals = _verdict(tmp_path, command, "5 passed")
    assert finding.verdict is Verdict.UNVERIFIED
    assert "swallowed_failure" in signals


@pytest.mark.parametrize("form", _SEC251_FORMS)
def test_sec251_r3_a_command_claim_after_generated_text_is_unverified(tmp_path, form):
    command = form + "; " + _HELLO
    assert zero_exit_trap(command)
    finding, _ = _verdict(tmp_path, command, claim=_WORKED)
    assert finding.verdict is Verdict.UNVERIFIED
    assert "hidden by `" in finding.evidence


@pytest.mark.parametrize("form", [
    "source .venv/bin/activate",
    ". ./env.sh",
    "eval \"trap 'rm -f t.txt' EXIT\"",
    "echo \"source <(x)\"",
    "trap 'exit 1' INT",
    "trap '' INT",
    "trap - INT",
    "$PYTHON -m pip --version",
])
def test_sec251_r3_literal_text_and_harmless_traps_are_no_mask(tmp_path, form):
    assert _kinds(form + "; python -m pytest -q") == []
    assert zero_exit_trap(form + "; " + _HELLO) is None


# -- Row 3, SEC-290 (V-1): a wait that does not return the check's own status ----

@pytest.mark.parametrize("command", [
    "true & " + _QUIET + " & wait %1",
    _QUIET + " & wait $! &",
    _QUIET + " & wait $!&",
    "true & sleep 1; " + _QUIET + " & wait -n",
    "sleep 1 & " + _QUIET + " & wait -n",
])
def test_sec290_r3_a_wait_for_another_job_or_in_the_background_is_a_mask(tmp_path, command):
    assert "masked_exit_code" in _kinds(command)
    finding, _ = _verdict(tmp_path, command)
    assert finding.verdict is Verdict.UNVERIFIED


@pytest.mark.parametrize("command", [
    _QUIET + " & wait $!",
    _QUIET + " & wait %1",
    _QUIET + " & wait -n",
    "true & " + _QUIET + " & wait $!",
    "true & " + _QUIET + " & wait %%",
])
def test_sec290_r3_a_wait_for_the_check_job_keeps_its_status(command):
    assert _kinds(command) == []


# -- Row 4, SEC-292 (V-3): a status read keeps the status only in a canonical form

@pytest.mark.parametrize("tail", [
    "; [ $? -eq 0 -o 1 -eq 1 ] || exit 1",
    "; if [ $? -ne 0 -a 1 = 2 ]; then exit 1; fi",
    "; if [ $? -ne 0 ]; then if false; then exit 1; fi; fi",
    "; if [ $? -ne 0 ]; then true || exit 1; fi",
    "; rc=$?; echo \"exit $rc\"",
    "; rc=$?; if false; then exit $rc; fi",
    "; echo hi; exit $?",
    "; echo \"exit $?\"",
    " || echo \"exit $?\"",
    " || true; exit $?",
    "; echo hi; [ $? -ne 0 ] && exit 1",
    "; [ $? -ne 0 ] && exit 1 &",
    " & if [ $? -ne 0 ]; then exit 1; fi",
    "; [ $? -ne 0 ] && { if false; then exit 1; fi; }",
    "; [ $? -ne 0 ] && false && exit 1",
    "; [ $? -ne 0 ] && exit $?",
    "; rc=$?; true || exit $rc",
    "; rc=$?; exit 0; exit $rc",
    "; rc=$?; f(){ exit $rc; }",
    "; echo hi; rc=$?; exit $rc",
])
def test_sec292_r3_a_status_read_outside_the_canonical_forms_keeps_nothing(tmp_path, tail):
    command = _QUIET + tail
    assert _kinds(command)
    finding, _ = _verdict(tmp_path, command)
    assert finding.verdict is Verdict.UNVERIFIED


@pytest.mark.parametrize("tail", [
    "; rc=$?; if [ $rc -ne 0 ]; then exit $rc; fi",
    "; rc=$?; [ $rc -ne 0 ] && exit $rc",
    "; rc=$?; echo done; exit $rc",
    "; if [ $? -ne 0 ]; then echo failed; exit 1; fi",
    "; exit $?",
    " | tail -3; exit ${PIPESTATUS[0]}",
    " || exit $?",
    "; test $? -eq 0 || exit 1",
    "; [[ $? -ne 0 ]] && exit 1",
])
def test_sec292_r3_the_canonical_status_forms_keep_the_status(tail):
    assert _kinds(_QUIET + tail) == []


# -- Row 5, SEC-250: only a statement that provably prints nothing lets a marker count

@pytest.mark.parametrize("before", [
    "pushd '5 passed'",
    "set -x; X='5 passed'",
    "X='5 passed'; set",
    "export X='5 passed'; export -p",
    "source ./m.sh",
    "x=$(echo${IFS}5${IFS}passed>&2)",
    "cd -",
    "export X=$(cat notes.txt)",
    "cd app | echo '5 passed'",
    "popd",
])
def test_sec250_r3_a_marker_may_come_from_a_statement_that_prints(tmp_path, before):
    command = before + "; " + _QUIET + " | tail -1"
    finding, signals = _verdict(tmp_path, command, "5 passed")
    assert finding.verdict is Verdict.UNVERIFIED
    assert "in the same call, so a pass marker in the output is no proof" in finding.evidence
    assert "masked_exit_code" in signals


@pytest.mark.parametrize("before", [
    "cd app",
    "set -euo pipefail",
    "set -e",
    "export CI=1",
    "unset CI",
    "CI=1",
    "X='5 passed'",
    "source .venv/bin/activate",
    ". venv/Scripts/activate",
])
def test_sec250_r3_silent_statements_still_let_a_marker_verify(tmp_path, before):
    finding, _ = _verdict(tmp_path, before + "; python -m pytest -q 2>&1 | tail -3",
                          "12 passed in 0.2s")
    assert finding.verdict is Verdict.VERIFIED
