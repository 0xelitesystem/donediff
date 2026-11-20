"""0.5.0 QA re-check of 763a1e2 (2026-11-19): QA-032 and QA-033, the verdict engine,
FAIL CLOSED. A false UNVERIFIED is acceptable; a false VERIFIED is not.

Every value here is made up: no real run, no real path, no personal data.
"""

from __future__ import annotations

import pytest

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
from donediff import cli
from donediff.evidence import masking, zero_exit_trap
from donediff.models import Verdict

_QUIET = "python -m pytest -q"
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


# -- QA-032: PowerShell code built at run time is a mask, like bash eval/source --

_QA032_FORMS = [
    "iex 'function global:python {\"5 passed\"}'",
    "IEX 'function global:python {1}'",
    "Invoke-Expression 'function global:python {\"5 passed\"}'",
    "invoke-expression $code",
    "(& conda 'shell.powershell' 'hook') | Out-String | Invoke-Expression",
    "(& conda 'shell.powershell' 'hook') | Out-String | iex",
    "& ([scriptblock]::Create('function global:python {1}'))",
    ". ([ScriptBlock]::Create($s))",
    "$sb = [scriptblock] :: Create($s); . $sb",
    "$ExecutionContext.InvokeCommand.InvokeScript('function global:python {1}')",
    ". $ExecutionContext.InvokeCommand.NewScriptBlock($s)",
]


@pytest.mark.parametrize("form", _QA032_FORMS)
def test_qa032_powershell_code_built_at_run_time_is_a_mask(tmp_path, form):
    command = form + "; " + _QUIET
    assert "swallowed_failure" in _kinds(command)
    finding, signals = _verdict(tmp_path, command, "5 passed")
    assert finding.verdict is Verdict.UNVERIFIED
    assert "swallowed_failure" in signals


@pytest.mark.parametrize("form", _QA032_FORMS)
def test_qa032_a_command_claim_after_powershell_run_time_code_is_unverified(tmp_path, form):
    command = form + "; " + _HELLO
    assert zero_exit_trap(command)
    finding, _ = _verdict(tmp_path, command, claim=_WORKED)
    assert finding.verdict is Verdict.UNVERIFIED


@pytest.mark.parametrize("form", [
    "Write-Host 'iex is only a word here'",
    "echo \"Invoke-Expression\"",
    "$iex = 1",
    "Get-Help about_Script_Blocks",
    "Set-Location app",
])
def test_qa032_quoted_words_and_plain_powershell_are_no_mask(tmp_path, form):
    assert _kinds(form + "; " + _QUIET) == []
    assert zero_exit_trap(form + "; " + _HELLO) is None


# -- QA-033: a status read keeps the check's status only when it names the -----
# -- check's own stage ----------------------------------------------------------

_QA033_FORMS = [
    "pytest | head -1; exit ${PIPESTATUS[1]}",
    "pytest | head -1 | cat; exit ${PIPESTATUS[2]}",
    "pytest | head -1 | cat; exit ${PIPESTATUS[1]}",
    "pytest | head -1; rc=${PIPESTATUS[1]}; exit $rc",
    "pytest | head -1; [ ${PIPESTATUS[1]} -ne 0 ] && exit 1",
    "pytest | head -1; if [ ${PIPESTATUS[1]} -ne 0 ]; then exit 1; fi",
    "pytest | head -1; exit $?",
    "pytest | head -1; exit ${?}",
    "pytest | head -1; rc=$?; exit $rc",
    "pytest | head -1; [ $? -ne 0 ] && exit 1",
    "pytest | findstr passed; exit $LASTEXITCODE",
    "pytest | Select-Object -Last 5 | findstr passed; if ($LASTEXITCODE -ne 0) { exit 1 }",
]


@pytest.mark.parametrize("command", _QA033_FORMS)
def test_qa033_a_status_read_of_another_stage_is_a_mask(tmp_path, command):
    assert "masked_exit_code" in _kinds(command)
    finding, signals = _verdict(tmp_path, command, "1 failed, 4 passed")
    assert finding.verdict is not Verdict.VERIFIED
    assert "masked_exit_code" in signals


@pytest.mark.parametrize("command", [
    "pytest | head -1; exit ${PIPESTATUS[0]}",
    "pytest | head -1 | cat; exit ${PIPESTATUS[0]}",
    "pytest | head -1; rc=${PIPESTATUS[0]}; exit $rc",
    "pytest | head -1; [ ${PIPESTATUS[0]} -ne 0 ] && exit 1",
    "cat cfg | pytest; exit ${PIPESTATUS[1]}",
    "cat cfg | pytest; exit $?",
    "set -o pipefail; pytest | head -1; exit $?",
    "pytest; exit ${PIPESTATUS[0]}",
    "pytest; exit $?",
    "npm test | Select-Object -Last 5; if ($LASTEXITCODE -ne 0) { exit 1 }",
    "npm test | Where-Object { $_ } | Select-Object -Last 5; exit $LASTEXITCODE",
    "npm test; exit $LASTEXITCODE",
])
def test_qa033_a_status_read_of_the_checks_own_stage_is_kept(command):
    assert "masked_exit_code" not in _kinds(command)
