"""SEC-215..219: Goose (and opencode) reader Lows from the D087 security read.

Every value here is made up: no real run, no real path, no personal data.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from donediff import cli
from donediff.models import SHELL, ClaimType, Verdict
from donediff.readers import goose as gs

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "make_goose_215", ROOT / "tests" / "fixtures" / "goose" / "make_goose.py")
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)


def audit(target, **kw):
    return cli.run_audit(str(target), check_disk=False, agent="goose", **kw)


def pass_finding(target):
    found = [f for f in audit(target).findings if f.claim.type is ClaimType.TESTS_PASS]
    assert len(found) == 1
    return found[0]


# -- SEC-215: a whole-conversation rewrite hides a later edit and failure ---------

def _rewrite(b, keep: int) -> None:
    """What Goose's replace_conversation_inner does: delete every row of the
    session, insert the kept ones again (same content and time, new ids)."""
    rows = b.conn.execute("SELECT role, content_json, created_timestamp, metadata_json "
                          "FROM messages WHERE session_id = ? ORDER BY id", (b.sid,)).fetchall()
    b.conn.execute("DELETE FROM messages WHERE session_id = ?", (b.sid,))
    for role, content, created, metadata in rows[:keep]:
        b.conn.execute("INSERT INTO messages (session_id, role, content_json, created_timestamp, "
                       "metadata_json) VALUES (?, ?, ?, ?, ?)",
                       (b.sid, role, content, created, metadata))


def _pass_edit_fail(b) -> None:
    b.user("Please do the task.")
    b.shell("pytest -q", "12 passed", 0)  # rows 2 (request) and 3 (result)
    b.edit(mk.CWD + "/app.py", "x = 1", "x = 2")
    b.shell("pytest -q", "1 failed", 1)


def _assert_rewound(db) -> None:
    result = audit(db)
    run = [e for e in result.session.tool_calls() if e.tool_action == SHELL][0]
    assert run.exit_code is None and run.status == gs._REWOUND
    finding = [f for f in result.findings if f.claim.type is ClaimType.TESTS_PASS][0]
    assert finding.verdict is Verdict.UNVERIFIED and "exit 0" not in finding.evidence


def test_rewritten_only_session_hides_no_failure(tmp_path):
    b = mk.Builder(tmp_path / "sessions.db").session("20260610_7")
    _pass_edit_fail(b)
    _rewrite(b, keep=3)  # user + the passing run; the edit and the failure are gone
    b.say("All tests pass.")
    _assert_rewound(b.close())


def test_rewritten_session_after_another_session_hides_no_failure(tmp_path):
    b = mk.Builder(tmp_path / "sessions.db").session("20260610_7")
    _pass_edit_fail(b)
    b.session("20260610_8").user("another session")  # its row sits right below the rewrite
    b.sid = "20260610_7"
    _rewrite(b, keep=3)
    b.say("All tests pass.")
    _assert_rewound(b.close())


def test_sessions_one_after_another_are_not_a_rewrite(tmp_path):
    b = mk.Builder(tmp_path / "sessions.db").session("20260610_6")
    b.user("first session")
    b.say("Hello.")
    b.session("20260610_7")
    b.user("Please do the task.")
    b.shell("pytest -q", "12 passed", 0)
    b.say("All tests pass.")
    assert pass_finding(b.close() + "#20260610_7").verdict is Verdict.VERIFIED
