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
