"""The Qwen Code reader: format, discovery, mapping to shared actions, exit-code rule."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

from donediff import cli, parser
from donediff.models import EDIT, OTHER, SHELL, WRITE, ClaimType, Verdict
from donediff.readers import CODEX, GEMINI, OPENCODE, QWEN, READERS, select, select_for_list
from donediff.readers import qwen as qwen_mod
from donediff.readers.base import UnreadableSession
from donediff.readers.claude_code import CLAUDE_CODE
from donediff.report import render_terminal

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo-session.jsonl"
FIXTURES = ROOT / "tests" / "fixtures" / "qwen"
CONTRACT = FIXTURES / "contract-chat-records.jsonl"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mk = _load("make_qwen", FIXTURES / "make_qwen.py")
mg = _load("make_gemini_x", ROOT / "tests" / "fixtures" / "gemini" / "make_gemini.py")


def session_file(tmp_path, records, name=f"{mk.SID}.jsonl"):
    return mk.write(tmp_path / name, records)


def start():
    chat = mk.Chat()
    chat.user("Fix the parser.")
    return chat


def shells(session):
    return [e for e in session.tool_calls() if e.tool_action == SHELL]


def audit(path, **kw):
    return cli.run_audit(str(path), check_disk=False, agent=kw.pop("agent", "qwen"), **kw)


def snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            if p.is_file() else "dir" for p in sorted(folder.rglob("*"))}


# -- detect and auto ----------------------------------------------------------

def test_cross_detect_every_reader(tmp_path):
    qwen = session_file(tmp_path, mk.demo())
    gemini = mg.write(tmp_path / "session-2026-06-10T12-00-6f1c2d3e.jsonl", mg.demo())
    owners = {
        qwen: QWEN, gemini: GEMINI, DEMO: CLAUDE_CODE,
        CONTRACT: QWEN,
    }
    for path, owner in owners.items():
        claimed = [r for r in READERS if r.detect(path)]
        assert claimed == [owner], (path.name, [r.name for r in claimed])
    assert not CODEX.detect(qwen) and not OPENCODE.detect(qwen)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert not QWEN.detect(empty)


def test_claude_shaped_record_with_uuid_is_not_qwen():
    claude = {"uuid": "u1", "parentUuid": None, "sessionId": "s", "type": "user",
              "isSidechain": False, "userType": "external",
              "message": {"role": "user", "content": "hi"}}
    assert not qwen_mod.is_qwen_record(claude)
    no_parts = {"uuid": "u1", "parentUuid": None, "sessionId": "s", "type": "user",
                "message": {"role": "user", "content": "hi"}}
    assert not qwen_mod.is_qwen_record(no_parts)


def test_auto_picks_qwen_by_file(tmp_path):
    path = session_file(tmp_path, mk.demo())
    assert select("auto", str(path)) is QWEN
    assert select("auto", str(DEMO)) is CLAUDE_CODE
    assert select("qwen") is QWEN


def test_auto_latest_and_list_use_qwen_only_when_others_have_none(tmp_path, monkeypatch):
    home = tmp_path / "qh"
    mk.place(home, mk.demo())
    monkeypatch.setenv("QWEN_HOME", str(home))
    empty = tmp_path / "claude-projects"
    empty.mkdir()
    monkeypatch.setattr(parser, "projects_dir", lambda: empty)
    assert select("auto", "latest") is QWEN
    assert select_for_list("auto") is QWEN
    assert select("auto", "7a2b3c") is QWEN
    project = empty / "proj"
    project.mkdir()
    (project / "abc.jsonl").write_text(DEMO.read_text(encoding="utf-8"), encoding="utf-8")
    assert select("auto", "latest") is CLAUDE_CODE


# -- discovery ------------------------------------------------------------------

def test_discovery_newest_first_filtered_by_project(tmp_path, monkeypatch):
    home = tmp_path / "qh"
    alpha = mk.Chat("aaaa1111-0000", "/home/dev/alpha")
    alpha.user("hi")
    pa = mk.place(home, alpha.records, session_id="aaaa1111-0000", cwd="/home/dev/alpha")
    beta = mk.Chat("bbbb2222-0000", "/home/dev/beta")
    beta.user("hi")
    pb = mk.place(home, beta.records, session_id="bbbb2222-0000", cwd="/home/dev/beta")
    legacy = mk.Chat("cccc3333-0000", "/home/dev/gamma")
    legacy.user("hi")
    pc = mk.place(home, legacy.records, session_id="cccc3333-0000", group="tmp",
                  folder="f" * 64)
    os.utime(pa, (2_000_000_000, 2_000_000_000))
    os.utime(pb, (1_900_000_000, 1_900_000_000))
    os.utime(pc, (1_800_000_000, 1_800_000_000))
    chats = pa.parent
    (chats / "notes.txt").write_text("{}", encoding="utf-8")  # not a session name
    (chats / "x.y.jsonl").write_text("{}", encoding="utf-8")  # a dot in the stem: not an id
    (chats / "sub").mkdir()
    mk.write(chats / "sub" / "dddd4444.jsonl", alpha.records)  # nested: not read
    monkeypatch.setenv("QWEN_HOME", str(home))
    assert QWEN.discover() == [pa, pb, pc]
    assert QWEN.discover("BETA") == [pb]
    assert QWEN.discover("gamma") == [pc]  # matched on the recorded cwd
    assert QWEN.discover("nothing") == []
    assert QWEN.resolve("latest") == pa
    assert QWEN.resolve("bbbb") == pb
    with pytest.raises(FileNotFoundError, match="no transcript matching"):
        QWEN.resolve("zzz")
    assert QWEN.describe(pa) == ("aaaa1111", "alpha")
    assert QWEN.parse(pb).cwd == "/home/dev/beta"


def test_home_default_runtime_override_and_bad_values(tmp_path, monkeypatch):
    monkeypatch.delenv("QWEN_HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert qwen_mod.qwen_dir() == tmp_path / ".qwen"
    assert QWEN.location == "~/.qwen/projects"
    runtime = tmp_path / "rt"
    runtime.mkdir()
    monkeypatch.setenv("QWEN_HOME", str(tmp_path / "other"))
    monkeypatch.setenv("QWEN_RUNTIME_DIR", str(runtime))
    assert qwen_mod.qwen_dir() == runtime  # the runtime folder wins
    assert QWEN.location == "$QWEN_RUNTIME_DIR/projects"
    monkeypatch.setenv("QWEN_RUNTIME_DIR", "~/rt")
    assert qwen_mod.qwen_dir() == runtime
    monkeypatch.delenv("QWEN_RUNTIME_DIR")
    assert QWEN.discover() == []  # QWEN_HOME is missing
    with pytest.raises(FileNotFoundError, match=r"\$QWEN_HOME"):
        QWEN.resolve("latest")


@pytest.mark.parametrize("remote", ["\\\\some-host\\share", "//some-host/share",
                                    "\\??\\UNC\\some-host\\share"])
def test_remote_home_is_refused(monkeypatch, remote):
    monkeypatch.setenv("QWEN_HOME", remote)
    assert qwen_mod.qwen_dir() is None
    monkeypatch.setenv("QWEN_RUNTIME_DIR", remote)
    assert qwen_mod.qwen_dir() is None
    monkeypatch.delenv("QWEN_RUNTIME_DIR")
    with monkeypatch.context() as m:
        m.setenv("QWEN_HOME", "relative-home")
        m.setattr(qwen_mod.os.path, "abspath", lambda p: remote + "\\" + p)
        assert qwen_mod.qwen_dir() is None  # the final path is checked, not only the value
    assert QWEN.discover() == []
    assert not QWEN.detect(Path(remote) / "x.jsonl")
    with pytest.raises(UnreadableSession):
        QWEN.parse(Path(remote) / "x.jsonl")


def test_symlinked_project_folder_and_file_are_skipped(tmp_path, monkeypatch):
    home = tmp_path / "qh"
    real = mk.place(home, mk.demo())
    outside = mk.place(tmp_path / "elsewhere", mk.demo(), folder="outside")
    try:
        os.symlink(outside.parent.parent, home / "projects" / "linked",
                   target_is_directory=True)
        os.symlink(real, real.parent / "linked00.jsonl")
    except (OSError, NotImplementedError):
        pytest.skip("no symlink rights")
    monkeypatch.setenv("QWEN_HOME", str(home))
    assert QWEN.discover() == [real]


# -- mapping and verdicts -----------------------------------------------------

def test_demo_maps_calls_texts_and_exit_codes(tmp_path):
    session = QWEN.parse(session_file(tmp_path, mk.demo()))
    assert session.session_id == mk.SID and session.cwd == mk.CWD
    assert [e.text for e in session.text_events()] == [mk.OPENING, mk.CLOSING]
    assert len(session.prompt_starts) == 1
    runs = shells(session)
    assert [e.command for e in runs] == [r[0] for r in mk.RUNS]
    assert [e.exit_code for e in runs] == [1, 0, 0]
    assert [e.status for e in runs] == ["failed", "completed", "completed"]
    assert [e.output for e in runs] == [r[2] for r in mk.RUNS]
    assert all(e.result_seen for e in session.tool_calls())
    assert [e.tool_action for e in session.tool_calls()] == [EDIT, SHELL, EDIT, SHELL, SHELL]
    assert session.unrecognised == 0
    assert [e.index for e in session.events] == list(range(len(session.events)))


def test_demo_verdicts(tmp_path):
    result = audit(session_file(tmp_path, mk.demo()))
    verdicts = {f.claim.type: f for f in result.findings}
    assert verdicts[ClaimType.TESTS_PASS].verdict is Verdict.CONTRADICTED


def test_logged_exit_zero_is_verified(tmp_path):
    chat = start()
    mk.shell(chat, "c1", "pytest -q", 0, "12 passed in 0.4s")
    chat.assistant("All tests pass.")
    path = session_file(tmp_path, chat.records)
    run = shells(QWEN.parse(path))[0]
    assert run.exit_code == 0 and not run.is_error
    finding = audit(path).findings[0]
    assert finding.verdict is Verdict.VERIFIED
    assert "exit 0" in finding.evidence


def test_exit_zero_from_text_trailer_when_no_structured_display(tmp_path):
    chat = start()
    mk.shell(chat, "c1", "pytest -q", 0, "12 passed in 0.4s", structured=False)
    path = session_file(tmp_path, chat.records)
    run = shells(QWEN.parse(path))[0]
    assert run.exit_code == 0 and run.output == "12 passed in 0.4s"


def test_failed_run_is_contradicted_with_its_exit_code(tmp_path):
    chat = start()
    mk.shell(chat, "c1", "pytest -q", 2, "error collecting")
    chat.assistant("All tests pass.")
    path = session_file(tmp_path, chat.records)
    run = shells(QWEN.parse(path))[0]
    assert run.exit_code == 2 and run.is_error and run.status == "failed"
    finding = audit(path).findings[0]
    assert finding.verdict is Verdict.CONTRADICTED
    assert "exit 2" in finding.evidence


def test_status_only_success_reads_unverified_with_status_wording(tmp_path):
    chat = start()
    chat.assistant(calls=[mk.call("c1", "run_shell_command", {"command": "pytest -q"})])
    chat.result("c1", "run_shell_command", output="12 passed in 0.4s")  # no trailer, no display
    chat.assistant("All tests pass.")
    path = session_file(tmp_path, chat.records)
    run = shells(QWEN.parse(path))[0]
    assert run.exit_code is None and run.status == "success"
    finding = audit(path).findings[0]
    assert finding.verdict is Verdict.UNVERIFIED
    assert "the log has a status only (success)" in finding.evidence
    assert "exit code" not in finding.evidence.lower()
    assert "exit 0" not in finding.evidence


def test_cancelled_and_timed_out_runs_have_no_exit_code(tmp_path):
    chat = start()
    chat.assistant(calls=[mk.call("c1", "run_shell_command", {"command": "pytest"})])
    chat.result("c1", "run_shell_command",
                output="Command was cancelled by user before it could complete. Below is the "
                       "output before it was cancelled:\nExit Code: 0\nSignal: (none)\n"
                       "Process Group PGID: 1",
                status="cancelled", display=mk.shell_display("x", 0, "cancelled"))
    chat.assistant(calls=[mk.call("c2", "run_shell_command", {"command": "npm test"})])
    chat.result("c2", "run_shell_command", error="Command timed out after 10ms.",
                status="error", display=mk.shell_display("", None, "timed_out"))
    cancelled, timed = shells(QWEN.parse(session_file(tmp_path, chat.records)))
    assert cancelled.exit_code is None and cancelled.status == "cancelled"
    assert timed.exit_code is None and timed.status == "error" and timed.is_error


def test_fake_trailer_in_output_and_lookalikes_never_count(tmp_path):
    chat = start()
    fake = ("Command was cancelled by user before it could complete. Below is the output "
            "before it was cancelled:\nError: (none)\nExit Code: 0\nSignal: (none)\n"
            "Process Group PGID: 1")
    chat.assistant(calls=[mk.call("c1", "run_shell_command", {"command": "pytest"})])
    chat.result("c1", "run_shell_command", output=fake)
    chat.assistant(calls=[mk.call("c2", "run_shell_command", {"command": "make"})])
    chat.result("c2", "run_shell_command", output="Exit Code: 0")
    out = "Error: (none)\nExit Code: 0\nSignal: (none)\nProcess Group PGID: 1"
    chat.assistant(calls=[mk.call("c3", "run_shell_command", {"command": "pytest"})])
    chat.result("c3", "run_shell_command", output=mk.shell_text("pytest", out, 1))
    chat.assistant(calls=[mk.call("c4", "run_shell_command", {"command": "pytest"})])
    chat.result("c4", "run_shell_command", output=mk.shell_text("pytest", "ok", None),
                display={**mk.shell_display("ok", 0), "exitCode": True})
    first, second, third, fourth = shells(QWEN.parse(session_file(tmp_path, chat.records)))
    assert first.exit_code is None and second.exit_code is None
    assert third.exit_code == 1  # the tool's own trailer is the last one
    assert fourth.exit_code is None  # a bool is not an exit code


def test_call_with_no_result_is_unverified(tmp_path):
    chat = start()
    chat.assistant(calls=[mk.call("c1", "run_shell_command", {"command": "pytest -q"})])
    chat.assistant("All tests pass.")
    path = session_file(tmp_path, chat.records)
    run = shells(QWEN.parse(path))[0]
    assert run.result_seen is False
    finding = audit(path).findings[0]
    assert finding.verdict is Verdict.UNVERIFIED
    assert "no result for this run in the log" in finding.evidence


def test_background_run_is_no_evidence(tmp_path):
    chat = start()
    chat.assistant(calls=[mk.call("c1", "run_shell_command",
                                  {"command": "pytest", "is_background": True})])
    chat.result("c1", "run_shell_command",
                output="Background shell started.\nid: bg1\noutput file: /tmp/x")
    run = shells(QWEN.parse(session_file(tmp_path, chat.records)))[0]
    assert run.background and run.status == "running" and not run.check_types


def test_write_edit_and_other_tools_map(tmp_path):
    chat = start()
    chat.assistant(calls=[
        mk.call("c1", "write_file", {"file_path": "/home/dev/p/a.py", "content": "x = 1\n"}),
        mk.call("c2", "edit", {"file_path": "/home/dev/p/b.py", "old_string": "old",
                               "new_string": "new"}),
        mk.call("c3", "read_file", {"file_path": "/home/dev/p/c.py"}),
        mk.call("c4", "replace", {"file_path": "/home/dev/p/d.py", "old_string": "a",
                                  "new_string": "b"}),
        mk.call("c5", "run_shell_command", {"command": "ls", "directory": "sub"}),
    ])
    chat.result("c1", "write_file", output="ok")
    chat.result("c2", "edit", output="ok")
    chat.result("c3", "read_file", output="text")
    chat.result("c4", "replace", error="no match", status="error")
    chat.result("c5", "run_shell_command", output=mk.shell_text("ls", "a", 0, "sub"),
                display=mk.shell_display("a", 0))
    calls = QWEN.parse(session_file(tmp_path, chat.records)).tool_calls()
    assert [e.tool_action for e in calls] == [WRITE, EDIT, OTHER, EDIT, SHELL]
    assert calls[0].file_path == "/home/dev/p/a.py" and calls[0].is_file_edit()
    assert calls[1].tool_input["new_string"] == "new" and not calls[1].is_error
    assert calls[2].tool_name == "read_file" and not calls[2].is_file_edit()
    assert calls[3].is_error
    assert calls[4].output == "a" and calls[4].cwd == f"{mk.CWD}/sub"


def test_rewound_branch_is_dropped_and_notifications_are_not_prompts(tmp_path):
    chat = start()
    chat.assistant("First answer.")
    keep = chat.last
    chat.user("A prompt that gets rewound.")
    chat.assistant("Dropped by the rewind.")
    chat.last = keep  # rewindRecording re-roots the chain
    chat.system("rewind", {"targetTurnIndex": 1})
    note = chat.user("<notification>task done</notification>")
    note["subtype"] = "notification"
    chat.user("Second prompt.")
    chat.assistant("Second answer.")
    session = QWEN.parse(session_file(tmp_path, chat.records))
    assert [e.text for e in session.text_events()] == ["First answer.", "Second answer."]
    assert len(session.prompt_starts) == 2
    assert session.unrecognised == 0


def test_malformed_and_unknown_records_are_skipped_and_counted(tmp_path):
    chat = start()
    chat.assistant("Still read.")
    records = list(chat.records)
    records.insert(1, {"something": "else"})
    records.insert(1, {"uuid": "x1", "parentUuid": None, "sessionId": mk.SID,
                       "type": "brand-new"})
    path = session_file(tmp_path, records)
    with open(path, "ab") as fh:
        fh.write(b"{not json\n[1, 2]\n")
    # the malformed lines come last, so the chain still starts from the last good record
    session = QWEN.parse(path)
    assert [e.text for e in session.text_events()] == ["Still read."]
    assert session.unrecognised == 4
    report = render_terminal(audit(path), color=False)
    assert "4 unrecognised record(s) skipped" in report


def test_huge_line_is_skipped(tmp_path, monkeypatch):
    chat = start()
    chat.assistant("x" * 5000)
    big = chat.last
    chat.assistant("Short.")
    chat.records[-1]["parentUuid"] = chat.records[0]["uuid"]
    path = session_file(tmp_path, chat.records)
    monkeypatch.setattr(qwen_mod, "MAX_LINE", 1000)
    session = QWEN.parse(path)
    assert big and [e.text for e in session.text_events()] == ["Short."]
    assert session.unrecognised == 1


def test_parent_cycle_does_not_loop(tmp_path):
    chat = start()
    chat.assistant("Looped.")
    chat.records[0]["parentUuid"] = chat.records[-1]["uuid"]
    session = QWEN.parse(session_file(tmp_path, chat.records))
    assert [e.text for e in session.text_events()] == ["Looped."]


def test_licensed_contract_fixture_parses():
    """Licensed fixture, not a real run: Qwen Code's own contract test case (Apache-2.0)."""
    assert (FIXTURES / "CONTRACT-FIXTURE-NOTICE.txt").is_file()
    assert (FIXTURES / "LICENSE-qwen-code.txt").is_file()
    session = QWEN.parse(CONTRACT)
    assert session.session_id == "session-test" and session.cwd == "/workspace/project"
    assert [e.text for e in session.text_events()] == ["The contract is stable."]
    assert len(session.prompt_starts) == 1
    (call,) = session.tool_calls()
    assert call.tool_name == "read_file" and call.tool_action == OTHER
    assert call.result_seen and call.status == "success" and not call.is_error
    assert call.output == "hidden raw output"
    assert session.unrecognised == 0


def test_shell_text_parser():
    assert qwen_mod.shell_text(mk.shell_text("c", "out", 3)) == (3, "out")
    assert qwen_mod.shell_text(mk.shell_text("c", "", 0)) == (0, "")
    assert qwen_mod.shell_text(mk.shell_text("c", "x", None)) == (None, "x")
    assert qwen_mod.shell_text("Output: x\nExit Code: 1") == (None, "Output: x\nExit Code: 1")


# -- read only ----------------------------------------------------------------

def test_audit_detect_and_list_change_nothing(tmp_path, monkeypatch, capsys):
    home = tmp_path / "qh"
    path = mk.place(home, mk.demo())
    monkeypatch.setenv("QWEN_HOME", str(home))
    before = snapshot(home)
    mtime = path.stat().st_mtime_ns
    assert QWEN.detect(path)
    audit(path)
    cli.run_audit("latest", check_disk=False, agent="qwen")
    assert cli.main(["list", "--agent", "qwen"]) == 0
    assert "7a2b3c4d" in capsys.readouterr().out
    assert snapshot(home) == before
    assert path.stat().st_mtime_ns == mtime
