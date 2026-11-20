"""0.5.0 fix round 4, verdict engine (Security round-3 read V, 2026-11-19).

Item (A): a command after `if`, `while`, `until` or `coproc` is in command position, so a
trap there is seen (README: a trap whose handler might exit 0 leaves the claim UNVERIFIED).

Every value here is made up: no real run, no real path, no personal data.
"""

from __future__ import annotations

import pytest

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
from donediff import cli
from donediff.evidence import zero_exit_trap
from donediff.models import Verdict

_QUIET = "python -m pytest -q >/dev/null 2>&1"


def _verdict(tmp_path, command, output="", claim="All tests pass."):
    result = cli.run_audit(str(write_jsonl(tmp_path / "s.jsonl", [
        assistant_tool("t1", "Bash", {"command": command}),
        tool_result("t1", output),
        assistant_text(claim),
    ])), check_disk=False, agent="claude-code")
    (finding,) = result.findings
    return finding


@pytest.mark.parametrize("trap", [
    "if trap 'exit 0' EXIT; then :; fi",
    "while trap 'exit 0' EXIT; do break; done",
    "until trap 'exit 0' EXIT; do :; done",
    "coproc trap 'exit 0' EXIT",
])
def test_r4_a_trap_after_a_compound_keyword_is_seen(tmp_path, trap):
    command = trap + "; " + _QUIET
    assert zero_exit_trap(command)
    assert _verdict(tmp_path, command, "5 passed").verdict is Verdict.UNVERIFIED


@pytest.mark.parametrize("command", [
    "python -m pytest -q && echo ok",
    "python -m pytest -q",
])
def test_r4_a_plain_checks_still_verify(tmp_path, command):
    assert not zero_exit_trap(command)
    assert _verdict(tmp_path, command, "5 passed").verdict is Verdict.VERIFIED


# -- SEC-312 (NEW3-V-2): a comment after the status exit keeps the status ----------

@pytest.mark.parametrize("command", [
    "python -m pytest -q; exit $?  # done",
    "python -m pytest -q; exit $?   #",
    "python -m pytest -q | tee log; exit ${PIPESTATUS[0]} # keep the check's status",
])
def test_sec312_status_exit_with_a_trailing_comment_verifies(tmp_path, command):
    assert _verdict(tmp_path, command, "5 passed").verdict is Verdict.VERIFIED


def test_sec312_a_hash_glued_to_the_status_is_not_a_comment(tmp_path):
    command = "python -m pytest -q; exit $?#x"  # one word: bash exits 2 or 1#x, not the check's status
    assert _verdict(tmp_path, command, "5 passed").verdict is Verdict.UNVERIFIED
