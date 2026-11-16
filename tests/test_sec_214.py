"""SEC-214: a tool call Goose skipped in chat mode (answered as a success,
the tool never ran) read as a successful write or run."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from donediff import cli
from donediff.models import ClaimType, Verdict
from donediff.readers import GOOSE
from donediff.readers import goose as gs

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "make_goose_214", ROOT / "tests" / "fixtures" / "goose" / "make_goose.py")
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)

# The prefix of Goose's CHAT_MODE_TOOL_SKIPPED_RESPONSE (block/goose d8de0e7,
# ops_remaining_tool_calls.rs:87-92); the rest of the text here is synthetic.
PREFIX = "Let the user know the tool call was skipped in goose chat mode"
SKIPPED = {"content": [{"type": "text", "text": PREFIX + ". The tool did not run."}],
           "isError": False}


def _session(tmp_path, build):
    b = mk.Builder(tmp_path / "sessions.db").session("20260610_7")
    b.user("Please do the task.")
    build(b)
    return b.close()


def _finding(result, types):
    found = [f for f in result.findings if f.claim.type in types]
    assert len(found) == 1, [(f.claim.type, f.claim.text) for f in result.findings]
    return found[0]


def test_prefix_matches_the_goose_constant():
    """Guard: the reader's prefix moves only with this pinned text."""
    assert gs._SKIPPED == PREFIX


@pytest.mark.parametrize("disk", (False, True), ids=("no-disk-check", "disk-check"))
def test_skipped_write_is_unverified(tmp_path, disk):
    target = (tmp_path / "limiter.py").as_posix()
    db = _session(tmp_path, lambda b: (
        b.tool("developer__write", {"path": target, "content": "x = 1\n"}, SKIPPED),
        b.say("I created `limiter.py` with the new limiter.")))
    write = GOOSE.parse(GOOSE.resolve(db)).tool_calls()[0]
    assert write.result_seen and write.is_error and write.status == "declined"
    finding = _finding(cli.run_audit(db, check_disk=disk, agent="goose"),
                       (ClaimType.FILE_CREATED, ClaimType.FILE_EDITED))
    assert finding.verdict is Verdict.UNVERIFIED
    assert "did not succeed (declined)" in finding.evidence


@pytest.mark.parametrize("disk", (False, True), ids=("no-disk-check", "disk-check"))
def test_skipped_edit_is_unverified(tmp_path, disk):
    target = (tmp_path / "limiter.py").as_posix()
    db = _session(tmp_path, lambda b: (
        b.tool("developer__edit", {"path": target, "before": "a = 1", "after": "a = 2"},
               SKIPPED),
        b.say("I updated `limiter.py` to raise the limit.")))
    edit = GOOSE.parse(GOOSE.resolve(db)).tool_calls()[0]
    assert edit.result_seen and edit.is_error and edit.status == "declined"
    finding = _finding(cli.run_audit(db, check_disk=disk, agent="goose"),
                       (ClaimType.FILE_CREATED, ClaimType.FILE_EDITED))
    assert finding.verdict is Verdict.UNVERIFIED


@pytest.mark.parametrize("disk", (False, True), ids=("no-disk-check", "disk-check"))
def test_skipped_shell_is_unverified(tmp_path, disk):
    text = PREFIX + ". 16 passed in 1.0s"
    db = _session(tmp_path, lambda b: (
        b.tool("developer__shell", {"command": "pytest -q"},
               {"content": [{"type": "text", "text": text}], "isError": False,
                "structuredContent": {"stdout": "16 passed", "stderr": "", "exit_code": 0}}),
        b.say("All tests pass.")))
    run = GOOSE.parse(GOOSE.resolve(db)).tool_calls()[0]
    assert run.exit_code is None and not run.pass_hint and run.status == "declined"
    finding = _finding(cli.run_audit(db, check_disk=disk, agent="goose"),
                       (ClaimType.TESTS_PASS,))
    assert finding.verdict is Verdict.UNVERIFIED
    assert "exit 0" not in finding.evidence


def test_ordinary_success_still_verifies(tmp_path):
    target = (tmp_path / "limiter.py").as_posix()
    db = _session(tmp_path, lambda b: (
        b.write(target, "x = 1\n"), b.say("I created `limiter.py` with the new limiter.")))
    finding = _finding(cli.run_audit(db, check_disk=False, agent="goose"),
                       (ClaimType.FILE_CREATED, ClaimType.FILE_EDITED))
    assert finding.verdict is Verdict.VERIFIED
