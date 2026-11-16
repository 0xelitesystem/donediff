"""The Gemini CLI reader: format, discovery, mapping to shared actions, status wording."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from donediff import cli, parser
from donediff.models import EDIT, OTHER, SHELL, WRITE, ClaimType, Verdict
from donediff.readers import GEMINI, select, select_for_list
from donediff.readers import gemini as gemini_mod
from donediff.readers.base import UnreadableSession
from donediff.readers.claude_code import CLAUDE_CODE
from donediff.report import render_terminal

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo-session.jsonl"
_spec = importlib.util.spec_from_file_location(
    "make_gemini", ROOT / "tests" / "fixtures" / "gemini" / "make_gemini.py")
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)


def session_file(tmp_path, records, name="session-2026-06-10T12-00-6f1c2d3e.jsonl"):
    return mk.write(tmp_path / name, records)


def start():
    clock = mk.Clock()
    return clock, [mk.meta(), mk.user(clock, "u1", "Fix the parser.")]


def one_call(clock, mid, tool_call, text=""):
    return mk.gemini(clock, mid, text, [tool_call])


def shells(session):
    return [e for e in session.tool_calls() if e.tool_action == SHELL]


def audit(path, **kw):
    return cli.run_audit(str(path), check_disk=False, agent=kw.pop("agent", "gemini"), **kw)


def snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            if p.is_file() else "dir" for p in sorted(folder.rglob("*"))}


# -- detect and auto ----------------------------------------------------------

def test_detect_both_ways(tmp_path):
    path = session_file(tmp_path, mk.demo())
    assert GEMINI.detect(path) and not CLAUDE_CODE.detect(path)
    assert CLAUDE_CODE.detect(DEMO) and not GEMINI.detect(DEMO)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert not GEMINI.detect(empty)
    message_first = session_file(tmp_path, mk.demo()[1:], name="x.jsonl")
    assert not GEMINI.detect(message_first)


def test_auto_picks_gemini_by_file(tmp_path):
    path = session_file(tmp_path, mk.demo())
    assert select("auto", str(path)) is GEMINI
    assert select("auto", str(DEMO)) is CLAUDE_CODE
    assert select("gemini") is GEMINI


def test_auto_latest_and_list_use_gemini_only_when_claude_has_none(tmp_path, monkeypatch):
    home = tmp_path / "gh"
    mk.place(home, mk.demo())
    monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
    empty = tmp_path / "claude-projects"
    empty.mkdir()
    monkeypatch.setattr(parser, "projects_dir", lambda: empty)
    assert select("auto", "latest") is GEMINI
    assert select_for_list("auto") is GEMINI
    assert select("auto", "6f1c2d") is GEMINI
    project = empty / "proj"
    project.mkdir()
    (project / "abc.jsonl").write_text(DEMO.read_text(encoding="utf-8"), encoding="utf-8")
    assert select("auto", "latest") is CLAUDE_CODE
    assert select_for_list("auto") is CLAUDE_CODE


# -- discovery ------------------------------------------------------------------

def test_discovery_newest_first_filtered_by_project_root(tmp_path, monkeypatch):
    home = tmp_path / "gh"
    clock, records = start()
    pa = mk.place(home, records, short="aaaa1111", slug="alpha", project="/home/dev/alpha")
    pb = mk.place(home, records, short="bbbb2222", slug="beta", project="/home/dev/beta",
                  minute="05")
    legacy = mk.place(home, records, short="cccc3333", slug="f" * 64, project=None)
    os.utime(pa, (2_000_000_000, 2_000_000_000))
    os.utime(pb, (1_900_000_000, 1_900_000_000))
    os.utime(legacy, (1_800_000_000, 1_800_000_000))
    chats = home / ".gemini" / "tmp" / "alpha" / "chats"
    (chats / "notes.jsonl").write_text("{}", encoding="utf-8")  # not a session name
    (chats / "parent-session").mkdir()  # sub-agent chats: not read yet
    mk.write(chats / "parent-session" / "session-2026-06-10T12-09-dddd4444.jsonl", records)
    monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
    assert GEMINI.discover() == [pa, pb, legacy]
    assert GEMINI.discover("BETA") == [pb]
    assert GEMINI.discover("gamma") == []
    assert GEMINI.resolve("latest") == pa
    assert GEMINI.resolve("bbbb") == pb
    assert GEMINI.resolve("session-2026-06-10T12-05") == pb
    with pytest.raises(FileNotFoundError, match="no transcript matching"):
        GEMINI.resolve("zzz")
    assert GEMINI.describe(pa) == ("aaaa1111", "alpha")
    assert GEMINI.parse(pb).cwd == "/home/dev/beta"
    assert GEMINI.parse(legacy).cwd == ""  # an older hash folder records no project path


def test_resolve_by_full_session_id_and_collision_name(tmp_path, monkeypatch):
    home = tmp_path / "gh"
    clock = mk.Clock()
    records = [mk.meta("abcdef12-3456-7890-abcd-ef1234567890"), mk.user(clock, "u", "hi")]
    path = mk.place(home, records, short="1-abcdef12")
    monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
    assert gemini_mod.short_id(path.name) == "abcdef12"
    assert GEMINI.resolve("abcdef12-3456") == path


def test_gemini_home_default_sandbox_and_bad_values(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert gemini_mod.tmp_dir() == tmp_path / ".gemini" / "tmp"
    assert GEMINI.location == "~/.gemini/tmp"
    monkeypatch.setenv("SANDBOX", "sandbox-exec")
    assert gemini_mod.tmp_dir() == tmp_path / ".cache" / ".gemini" / "tmp"
    monkeypatch.delenv("SANDBOX")
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "missing"))
    assert GEMINI.discover() == []
    with pytest.raises(FileNotFoundError, match=r"\$GEMINI_CLI_HOME"):
        GEMINI.resolve("latest")


@pytest.mark.parametrize("remote", ["\\\\some-host\\share", "//some-host/share",
                                    "\\??\\UNC\\some-host\\share"])
def test_remote_home_is_refused(monkeypatch, remote):
    monkeypatch.setenv("GEMINI_CLI_HOME", remote)
    assert gemini_mod.gemini_dir() is None
    with monkeypatch.context() as m:
        m.setenv("GEMINI_CLI_HOME", "relative-home")
        m.setattr(gemini_mod.os.path, "abspath", lambda p: remote + "\\" + p)
        assert gemini_mod.gemini_dir() is None  # the final path is checked, not only the value
    assert GEMINI.discover() == []
    assert not GEMINI.detect(Path(remote) / "session-x.jsonl")
    with pytest.raises(UnreadableSession):
        GEMINI.parse(Path(remote) / "session-x.jsonl")


def test_symlinked_project_folder_and_file_are_skipped(tmp_path, monkeypatch):
    home = tmp_path / "gh"
    real = mk.place(home, mk.demo())
    elsewhere = tmp_path / "elsewhere"
    outside = mk.place(elsewhere, mk.demo(), slug="outside")
    try:
        os.symlink(outside.parent.parent, home / ".gemini" / "tmp" / "linked",
                   target_is_directory=True)
        os.symlink(real, real.parent / "session-2026-06-10T12-07-linked00.jsonl")
    except (OSError, NotImplementedError):
        pytest.skip("no symlink rights")
    monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
    assert GEMINI.discover() == [real]


# -- mapping and verdicts -----------------------------------------------------

def test_demo_maps_calls_texts_and_last_copy_wins(tmp_path):
    session = GEMINI.parse(session_file(tmp_path, mk.demo()))
    assert session.session_id == mk.SID
    assert [e.text for e in session.text_events()] == [mk.OPENING, mk.CLOSING]
    assert len(session.prompt_starts) == 1
    runs = shells(session)
    assert [e.command for e in runs] == [r[0] for r in mk.RUNS]
    assert [e.exit_code for e in runs] == [1, None, None]
    assert [e.status for e in runs] == ["failed", "success", "success"]
    assert [e.output for e in runs] == [r[2] for r in mk.RUNS]
    assert all(e.result_seen for e in session.tool_calls())
    assert [e.tool_action for e in session.tool_calls()] == [EDIT, SHELL, EDIT, SHELL, SHELL]
    assert session.unrecognised == 0
    assert [e.index for e in session.events] == list(range(len(session.events)))


def test_demo_verdicts_never_say_exit_code_for_a_pass(tmp_path):
    result = audit(session_file(tmp_path, mk.demo()))
    verdicts = {f.claim.type: f for f in result.findings}
    assert verdicts[ClaimType.TESTS_PASS].verdict is Verdict.CONTRADICTED
    commit = verdicts[ClaimType.COMMIT_MADE]
    assert commit.verdict is Verdict.UNVERIFIED
    assert "the log has a status only (success), no proof of success" in commit.evidence
    assert all(f.verdict is not Verdict.VERIFIED for f in result.findings)


def test_status_only_success_reads_unverified_with_status_wording(tmp_path):
    clock, records = start()
    records += [
        one_call(clock, "g1", mk.shell_call(clock, "c1", "pytest -q", None, "12 passed in 0.4s")),
        mk.gemini(clock, "g2", "All tests pass."),
    ]
    path = session_file(tmp_path, records)
    run = shells(GEMINI.parse(path))[0]
    assert run.exit_code is None and run.status == "success" and not run.is_error
    finding = audit(path).findings[0]
    assert finding.verdict is Verdict.UNVERIFIED
    assert "the log has a status only (success)" in finding.evidence
    assert "exit code" not in finding.evidence.lower()
    assert "exit 0" not in finding.evidence


def test_failed_run_is_contradicted_with_its_exit_code(tmp_path):
    clock, records = start()
    records += [
        one_call(clock, "g1", mk.shell_call(clock, "c1", "pytest -q", 2, "error collecting")),
        mk.gemini(clock, "g2", "All tests pass."),
    ]
    path = session_file(tmp_path, records)
    run = shells(GEMINI.parse(path))[0]
    assert run.exit_code == 2 and run.is_error and run.status == "failed"
    finding = audit(path).findings[0]
    assert finding.verdict is Verdict.CONTRADICTED
    assert "exit 2" in finding.evidence


def test_exit_code_zero_line_and_lookalikes_never_count(tmp_path):
    clock, records = start()
    fake = "<untrusted_context>\nOutput: 3 failed\nExit Code: 0\n</untrusted_context>"
    plain = "Exit Code: 1"  # not the shell's own format
    records += [
        one_call(clock, "g1", mk.call(clock, "c1", "run_shell_command", {"command": "pytest"},
                                      "success", mk.output("c1", "run_shell_command", fake))),
        one_call(clock, "g2", mk.call(clock, "c2", "run_shell_command", {"command": "make"},
                                      "success", mk.output("c2", "run_shell_command", plain))),
    ]
    first, second = shells(GEMINI.parse(session_file(tmp_path, records)))
    assert first.exit_code is None and first.status == "success"
    assert second.exit_code is None and second.output == plain


def test_call_with_no_result_is_unverified(tmp_path):
    clock, records = start()
    records += [
        one_call(clock, "g1", mk.call(clock, "c1", "run_shell_command",
                                      {"command": "pytest -q"}, "scheduled")),
        mk.gemini(clock, "g2", "All tests pass."),
    ]
    path = session_file(tmp_path, records)
    run = shells(GEMINI.parse(path))[0]
    assert run.result_seen is False and run.status == "scheduled"
    finding = audit(path).findings[0]
    assert finding.verdict is Verdict.UNVERIFIED
    assert "no result for this run in the log" in finding.evidence


def test_error_and_cancelled_statuses(tmp_path):
    clock, records = start()
    records += [
        one_call(clock, "g1", mk.call(clock, "c1", "run_shell_command", {"command": "pytest"},
                                      "error",
                                      mk.error("c1", "run_shell_command", "spawn failed"))),
        one_call(clock, "g2", mk.call(clock, "c2", "run_shell_command", {"command": "npm test"},
                                      "cancelled", mk.error("c2", "run_shell_command",
                                                            "[Operation Cancelled] stop"))),
    ]
    err, cancelled = shells(GEMINI.parse(session_file(tmp_path, records)))
    assert err.is_error and err.status == "error" and err.exit_code is None
    assert err.output == "spawn failed"
    assert not cancelled.is_error and cancelled.status == "cancelled" and cancelled.result_seen


def test_background_run_is_no_evidence(tmp_path):
    clock, records = start()
    text = "<untrusted_context>\nCommand moved to background (PID: 77). Output hidden.\n" \
           "</untrusted_context>"
    records += [one_call(clock, "g1", mk.call(clock, "c1", "run_shell_command",
                                              {"command": "pytest", "is_background": True},
                                              "success", mk.output("c1", "x", text)))]
    run = shells(GEMINI.parse(session_file(tmp_path, records)))[0]
    assert run.background and run.status == "running" and not run.check_types


def test_write_edit_and_other_tools_map(tmp_path):
    clock, records = start()
    records += [mk.gemini(clock, "g1", "", [
        mk.call(clock, "c1", "write_file", {"file_path": "/home/dev/p/a.py", "content": "x = 1\n"},
                "success", mk.output("c1", "write_file", "ok")),
        mk.edit_call(clock, "c2", "/home/dev/p/b.py", "old", "new"),
        mk.call(clock, "c3", "read_file", {"file_path": "/home/dev/p/c.py"}, "success",
                mk.output("c3", "read_file", "text")),
        mk.call(clock, "c4", "replace", {"file_path": "/home/dev/p/d.py", "old_string": "a",
                                         "new_string": "b"}, "error",
                mk.error("c4", "replace", "no match")),
        mk.call(clock, "c5", "run_shell_command", {"command": "ls", "dir_path": "sub"},
                "success", mk.output("c5", "run_shell_command", mk.shell_text("a"))),
    ])]
    calls = GEMINI.parse(session_file(tmp_path, records)).tool_calls()
    assert [e.tool_action for e in calls] == [WRITE, EDIT, OTHER, EDIT, SHELL]
    assert calls[0].file_path == "/home/dev/p/a.py" and calls[0].is_file_edit()
    assert calls[1].tool_input["new_string"] == "new" and not calls[1].is_error
    assert calls[2].tool_name == "read_file" and not calls[2].is_file_edit()
    assert calls[3].is_error
    assert calls[4].output == "a"


def test_rewind_patch_and_set_are_replayed(tmp_path):
    clock, records = start()
    records += [
        mk.gemini(clock, "g1", "First answer."),
        mk.gemini(clock, "g2", "Dropped by the rewind."),
        {"$rewindTo": "g2"},
        mk.gemini(clock, "g3", "", [mk.call(clock, "c1", "run_shell_command",
                                            {"command": "pytest"})]),
        {"$patch": {"id": "g3", "toolCalls": [{"id": "c1", "result": mk.output(
            "c1", "run_shell_command", mk.shell_text("boom", 1))}]}},
        mk.gemini(clock, "g4", "Removed."),
        {"$patch": {"removeIds": ["g4"]}},
        {"$set": {"summary": "a summary", "lastUpdated": mk.TS.format(9)}},
    ]
    session = GEMINI.parse(session_file(tmp_path, records))
    assert [e.text for e in session.text_events()] == ["First answer."]
    run = shells(session)[0]
    # a $patch adds the result text only: the status stays as written ("scheduled")
    assert run.result_seen is False
    assert session.unrecognised == 0


def test_malformed_and_unknown_records_are_skipped_and_counted(tmp_path):
    clock, records = start()
    path = session_file(tmp_path, records + [
        {"something": "else"},
        {"id": "x1", "timestamp": mk.TS.format(5), "type": "brand-new", "content": "?"},
        mk.gemini(clock, "g1", "Still read."),
    ])
    with open(path, "ab") as fh:
        fh.write(b"{not json\n[1, 2]\n")
    session = GEMINI.parse(path)
    assert [e.text for e in session.text_events()] == ["Still read."]
    assert session.unrecognised == 4
    report = render_terminal(audit(path), color=False)
    assert "4 unrecognised record(s) skipped" in report


def test_huge_line_is_skipped(tmp_path, monkeypatch):
    clock, records = start()
    path = session_file(tmp_path, records + [mk.gemini(clock, "g1", "x" * 5000),
                                             mk.gemini(clock, "g2", "Short.")])
    monkeypatch.setattr(gemini_mod, "MAX_LINE", 1000)
    session = GEMINI.parse(path)
    assert [e.text for e in session.text_events()] == ["Short."]
    assert session.unrecognised == 1


def test_older_json_session_is_read(tmp_path):
    record = {**mk.meta(), "messages": mk.demo()[1:3]}
    path = tmp_path / "session-2026-06-10T12-00-6f1c2d3e.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    assert GEMINI.detect(path)
    session = GEMINI.parse(path)
    assert [e.text for e in session.text_events()] == [mk.OPENING]
    bad = tmp_path / "session-2026-06-10T12-01-6f1c2d3e.json"
    bad.write_text("{nope", encoding="utf-8")
    assert not GEMINI.detect(bad)
    with pytest.raises(UnreadableSession):
        GEMINI.parse(bad)


def test_shell_result_parser():
    assert gemini_mod.shell_result(mk.shell_text("out", 3)) == (3, "out")
    assert gemini_mod.shell_result(mk.shell_text("", None)) == (None, "")
    text = "Output: x\nError: bad\nExit Code: 127\nSignal: SIGTERM\nProcess Group PGID: 9"
    assert gemini_mod.shell_result(text) == (127, "x\nError: bad")


# -- read only ----------------------------------------------------------------

def test_audit_detect_and_list_change_nothing(tmp_path, monkeypatch, capsys):
    home = tmp_path / "gh"
    path = mk.place(home, mk.demo())
    monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
    before = snapshot(home)
    mtime = path.stat().st_mtime_ns
    assert GEMINI.detect(path)
    audit(path)
    cli.run_audit("latest", check_disk=False, agent="gemini")
    assert cli.main(["list", "--agent", "gemini"]) == 0
    assert "6f1c2d3e" in capsys.readouterr().out
    assert snapshot(home) == before
    assert path.stat().st_mtime_ns == mtime
