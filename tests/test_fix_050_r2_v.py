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
