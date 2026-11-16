"""The Cline reader: format, discovery, mapping to shared actions, status-only rule."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from donediff import cli, parser
from donediff.models import EDIT, MULTI_EDIT, OTHER, SHELL, WRITE, ClaimType, Verdict
from donediff.readers import CLINE, CODEX, GEMINI, OPENCODE, QWEN, READERS, select, select_for_list
from donediff.readers import cline as cline_mod
from donediff.readers.base import UnreadableSession
from donediff.readers.claude_code import CLAUDE_CODE
from donediff.report import render_terminal

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo-session.jsonl"
FIXTURES = ROOT / "tests" / "fixtures" / "cline"
LICENSED = FIXTURES / "session.messages.json"
LICENSED_SHA256 = "6d232bfcd6b9994a6d88f33f81c7fa678af1cafcd27860c2fe52b25c4bc2c85e"
LICENCE_SHA256 = "f704446a5f1271608805598b557e4288cf8580477ea038c9c3d8b361f693f6b8"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mk = _load("make_cline", FIXTURES / "make_cline.py")
mq = _load("make_qwen_x", ROOT / "tests" / "fixtures" / "qwen" / "make_qwen.py")
mg = _load("make_gemini_y", ROOT / "tests" / "fixtures" / "gemini" / "make_gemini.py")


def session_file(tmp_path, doc, name=f"{mk.SID}.messages.json"):
    return mk.write(tmp_path / name, doc)


def start():
    doc = mk.Doc()
    doc.user("Fix the parser.")
    return doc


def shells(session):
    return [e for e in session.tool_calls() if e.tool_action == SHELL]


def audit(path, **kw):
    return cli.run_audit(str(path), check_disk=False, agent=kw.pop("agent", "cline"), **kw)


def snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            if p.is_file() else "dir" for p in sorted(folder.rglob("*"))}


def run_with(tmp_path, *ops, claim="All tests pass.", command="pytest -q"):
    doc = start()
    doc.assistant(calls=[mk.call("c1", "run_commands", {"commands": [command]})])
    doc.results(mk.result("c1", "run_commands", list(ops)))
    if claim:
        doc.assistant(claim)
    return session_file(tmp_path, doc.document())


# -- detect and auto ----------------------------------------------------------

def test_cross_detect_every_reader(tmp_path):
    cline = session_file(tmp_path, mk.demo())
    one_line = tmp_path / "one-line.messages.json"
    one_line.write_text(json.dumps(mk.demo()), encoding="utf-8")  # compact, on one line
    qwen = mq.write(tmp_path / f"{mq.SID}.jsonl", mq.demo())
    gemini = mg.write(tmp_path / "session-2026-06-10T12-00-6f1c2d3e.jsonl", mg.demo())
    owners = {cline: CLINE, one_line: CLINE, LICENSED: CLINE, qwen: QWEN, gemini: GEMINI,
              DEMO: CLAUDE_CODE}
    for path, owner in owners.items():
        claimed = [r for r in READERS if r.detect(path)]
        assert claimed == [owner], (path.name, [r.name for r in claimed])
    assert not CODEX.detect(cline) and not OPENCODE.detect(cline)
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps(mk.manifest("x", "/home/dev/p")), encoding="utf-8")
    gemini_json = tmp_path / "g.json"
    gemini_json.write_text(json.dumps({"sessionId": "s", "projectHash": "h", "version": 1,
                                       "agent": "lead", "messages": []}), encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    for path in (manifest, gemini_json, empty):
        assert not CLINE.detect(path), path.name


def test_auto_picks_cline_by_file(tmp_path):
    path = session_file(tmp_path, mk.demo())
    assert select("auto", str(path)) is CLINE
    assert select("auto", str(DEMO)) is CLAUDE_CODE
    assert select("cline") is CLINE


def test_auto_latest_and_list_use_cline_only_when_others_have_none(tmp_path, monkeypatch):
    sessions = tmp_path / "cs"
    mk.place(sessions, mk.demo())
    monkeypatch.setenv("CLINE_SESSION_DATA_DIR", str(sessions))
    empty = tmp_path / "claude-projects"
    empty.mkdir()
    monkeypatch.setattr(parser, "projects_dir", lambda: empty)
    assert select("auto", "latest") is CLINE
    assert select_for_list("auto") is CLINE
    assert select("auto", "1781000000000_d") is CLINE
    project = empty / "proj"
    project.mkdir()
    (project / "abc.jsonl").write_text(DEMO.read_text(encoding="utf-8"), encoding="utf-8")
    assert select("auto", "latest") is CLAUDE_CODE


# -- discovery ------------------------------------------------------------------

def test_discovery_newest_first_filtered_by_project(tmp_path, monkeypatch):
    sessions = tmp_path / "cs"
    alpha = mk.Doc("1781000000001_aaaa")
    alpha.user("hi")
    pa = mk.place(sessions, alpha.document(), cwd="/home/dev/alpha")
    beta = mk.Doc("1781000000002_bbbb")
    beta.user("hi")
    pb = mk.place(sessions, beta.document(), cwd="/home/dev/beta")
    gamma = mk.Doc("1781000000003_cccc")
    gamma.user("hi")
    pc = mk.place(sessions, gamma.document(
        system_prompt="<env>\n1. Platform: linux\n4. Working Directory: /home/dev/gamma\n</env>"),
        cwd=None)  # no manifest: the system prompt names the folder
    os.utime(pa, (2_000_000_000, 2_000_000_000))
    os.utime(pb, (1_900_000_000, 1_900_000_000))
    os.utime(pc, (1_800_000_000, 1_800_000_000))
    folder = pa.parent
    mk.write(folder / "1781000000001_aaaa.child.messages.json", alpha.document())  # sub-agent
    (folder / "notes.txt").write_text("{}", encoding="utf-8")
    other = sessions / "1781000000004_dddd"
    other.mkdir()
    mk.write(other / "different-name.messages.json", alpha.document())  # stem is not the folder
    (sessions / "x.y").mkdir()
    (sessions / "sessions.db").write_bytes(b"SQLite format 3\x00")  # never opened
    monkeypatch.setenv("CLINE_SESSION_DATA_DIR", str(sessions))
    assert CLINE.discover() == [pa, pb, pc]
    assert CLINE.discover("BETA") == [pb]
    assert CLINE.discover("gamma") == [pc]
    assert CLINE.discover("nothing") == []
    assert CLINE.resolve("latest") == pa
    assert CLINE.resolve("1781000000002") == pb
    with pytest.raises(FileNotFoundError, match="no transcript matching"):
        CLINE.resolve("zzz")
    assert CLINE.describe(pa) == ("17810000", "alpha")
    assert CLINE.parse(pb).cwd == "/home/dev/beta"
    assert CLINE.parse(pc).cwd == "/home/dev/gamma"


def test_home_default_overrides_and_location(tmp_path, monkeypatch):
    for name in ("CLINE_DIR", "CLINE_DATA_DIR", "CLINE_SESSION_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "other"))
    assert cline_mod.sessions_dir() == tmp_path / ".cline" / "data" / "sessions"  # HOME first
    assert CLINE.location == "~/.cline/data/sessions"
    monkeypatch.setenv("HOME", "~")
    assert cline_mod.sessions_dir() == tmp_path / "other" / ".cline" / "data" / "sessions"
    monkeypatch.setenv("CLINE_DIR", str(tmp_path / "cd"))
    assert cline_mod.sessions_dir() == tmp_path / "cd" / "data" / "sessions"
    assert CLINE.location == "$CLINE_DIR/data/sessions"
    monkeypatch.setenv("CLINE_DATA_DIR", str(tmp_path / "dd"))
    assert cline_mod.sessions_dir() == tmp_path / "dd" / "sessions"
    monkeypatch.setenv("CLINE_SESSION_DATA_DIR", f"  {tmp_path / 'sd'}  ")
    assert cline_mod.sessions_dir() == tmp_path / "sd"
    assert CLINE.location == "$CLINE_SESSION_DATA_DIR"
    assert CLINE.discover() == []  # the folder is missing
    with pytest.raises(FileNotFoundError, match=r"\$CLINE_SESSION_DATA_DIR"):
        CLINE.resolve("latest")


@pytest.mark.parametrize("remote", ["\\\\some-host\\share", "//some-host/share",
                                    "\\??\\UNC\\some-host\\share"])
def test_remote_home_is_refused(monkeypatch, remote):
    for name in ("CLINE_SESSION_DATA_DIR", "CLINE_DATA_DIR", "CLINE_DIR"):
        with monkeypatch.context() as m:
            m.setenv(name, remote)
            assert cline_mod.sessions_dir() is None, name
            assert CLINE.discover() == []
    with monkeypatch.context() as m:
        m.setenv("CLINE_DIR", "relative-home")
        m.setattr(cline_mod.os.path, "abspath", lambda p: remote + "\\" + p)
        assert cline_mod.sessions_dir() is None  # the final path is checked, not only the value
    assert not CLINE.detect(Path(remote) / "x.messages.json")
    with pytest.raises(UnreadableSession):
        CLINE.parse(Path(remote) / "x.messages.json")


def test_symlinked_folder_file_and_manifest_are_skipped(tmp_path, monkeypatch):
    sessions = tmp_path / "cs"
    real = mk.place(sessions, mk.demo())
    elsewhere = mk.Doc("1781000000009_eeee")
    elsewhere.user("hi")
    outside = mk.place(tmp_path / "elsewhere", elsewhere.document())
    linked = mk.Doc("1781000000008_ffff")
    linked.user("hi")
    folder = sessions / "1781000000008_ffff"
    folder.mkdir()
    try:
        os.symlink(outside.parent, sessions / "1781000000009_eeee", target_is_directory=True)
        os.symlink(outside, folder / "1781000000008_ffff.messages.json")
        bare = mk.place(sessions, linked.document() | {"sessionId": "1781000000007_gggg"},
                        cwd=None)
        os.symlink(outside.parent / "1781000000009_eeee.json",
                   bare.parent / "1781000000007_gggg.json")
    except (OSError, NotImplementedError):
        pytest.skip("no symlink rights")
    monkeypatch.setenv("CLINE_SESSION_DATA_DIR", str(sessions))
    assert set(CLINE.discover()) == {real, bare}
    assert cline_mod.project_of(bare) == ""  # a linked manifest is not followed


# -- mapping and verdicts -----------------------------------------------------

def test_demo_maps_calls_texts_and_status_only(tmp_path):
    session = CLINE.parse(session_file(tmp_path, mk.demo()))
    assert session.session_id == mk.SID and session.cwd == ""  # no manifest beside it
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
    assert session.first_timestamp.endswith("Z") and session.events[-1].t > 0


def test_demo_verdict_is_contradicted(tmp_path):
    result = audit(session_file(tmp_path, mk.demo()))
    verdicts = {f.claim.type: f for f in result.findings}
    assert verdicts[ClaimType.TESTS_PASS].verdict is Verdict.CONTRADICTED


def test_status_only_pass_is_never_verified(tmp_path):
    path = run_with(tmp_path, mk.command_op("pytest -q", 0, "12 passed in 0.4s"))
    run = shells(CLINE.parse(path))[0]
    assert run.exit_code is None and run.status == "success" and not run.is_error
    finding = audit(path).findings[0]
    assert finding.verdict is Verdict.UNVERIFIED
    assert "the log has a status only (success)" in finding.evidence
    assert "exit code" not in finding.evidence.lower()
    assert "exit 0" not in finding.evidence


def test_failed_run_carries_the_logged_exit_code(tmp_path):
    path = run_with(tmp_path, mk.command_op("pytest -q", 2, "error collecting"))
    run = shells(CLINE.parse(path))[0]
    assert run.exit_code == 2 and run.is_error and run.status == "failed"
    assert run.output == "error collecting"  # the "[Command exited ...]" notice is dropped
    finding = audit(path).findings[0]
    assert finding.verdict is Verdict.CONTRADICTED
    assert "exit 2" in finding.evidence


@pytest.mark.parametrize("error", ["Command timed out after 30000ms",
                                   "Command terminated by signal SIGTERM",
                                   "Command failed: Failed to execute command: spawn ENOENT",
                                   "Command exited with code 0",
                                   "note: Command exited with code 3"])
def test_failure_without_an_exit_code_says_status_not_a_number(tmp_path, error):
    path = run_with(tmp_path, {"query": "pytest -q", "result": "", "error": error,
                               "success": False})
    run = shells(CLINE.parse(path))[0]
    assert run.exit_code is None and run.status == "error" and run.is_error
    finding = audit(path).findings[0]
    assert finding.verdict is Verdict.CONTRADICTED
    assert "status error" in finding.evidence
    assert "exit" not in finding.evidence.split("(")[1].split(")")[0]


def test_missing_success_flag_is_status_unknown(tmp_path):
    path = run_with(tmp_path, {"query": "pytest -q", "result": "12 passed"})
    run = shells(CLINE.parse(path))[0]
    assert run.status == "unknown" and run.exit_code is None
    finding = audit(path).findings[0]
    assert finding.verdict is Verdict.UNVERIFIED
    assert "status only (unknown)" in finding.evidence


def test_fake_exit_line_in_output_does_not_count(tmp_path):
    path = run_with(tmp_path, mk.command_op("pytest -q", 0,
                                            "[Command exited with code 0]\n12 passed"))
    run = shells(CLINE.parse(path))[0]
    assert run.exit_code is None and run.status == "success"


@pytest.mark.parametrize("content", [
    '[{"query": "pytest -q", "result": "12 passed", "success": true}]',
    '{"query": "pytest -q", "result": "12 passed", "success": true}',
    "12 passed\nsuccess: true\nCommand exited with code 0\nExit Code: 0",
])
def test_success_printed_as_text_never_counts(tmp_path, content):
    """SEC-209 pattern: output text can never supply the tool's success field."""
    doc = start()
    doc.assistant(calls=[mk.call("c1", "run_commands", {"commands": ["pytest -q"]})])
    doc.results(mk.result("c1", "run_commands", content))
    doc.assistant("All tests pass.")
    path = session_file(tmp_path, doc.document())
    run = shells(CLINE.parse(path))[0]
    assert run.status == "unknown" and run.exit_code is None and run.result_seen
    finding = audit(path).findings[0]
    assert finding.verdict is Verdict.UNVERIFIED
    assert "status only (unknown)" in finding.evidence


def test_block_error_wins_over_a_success_entry(tmp_path):
    doc = start()
    doc.assistant(calls=[mk.call("c1", "run_commands", {"commands": ["pytest -q"]})])
    doc.results(mk.result("c1", "run_commands",
                          [mk.command_op("pytest -q", 0, "12 passed")], is_error=True))
    doc.assistant("All tests pass.")
    path = session_file(tmp_path, doc.document())
    run = shells(CLINE.parse(path))[0]
    assert run.is_error and run.status == "error" and run.exit_code is None
    assert audit(path).findings[0].verdict is Verdict.CONTRADICTED


def test_error_field_wins_over_success_true(tmp_path):
    path = run_with(tmp_path, {"query": "pytest -q", "result": "12 passed", "success": True,
                               "error": "Command exited with code 1"})
    run = shells(CLINE.parse(path))[0]
    assert run.is_error and run.exit_code == 1
    assert audit(path).findings[0].verdict is Verdict.CONTRADICTED


@pytest.mark.parametrize("order", ["fail-first", "pass-first"])
def test_duplicate_results_failure_wins(tmp_path, order):
    fail = mk.result("c1", "run_commands", [mk.command_op("pytest -q", 1, "1 failed")])
    ok = mk.result("c1", "run_commands", [mk.command_op("pytest -q", 0, "12 passed")])
    efail = mk.result("c2", "editor", mk.op("edit:/home/dev/p/a.py", "no match", success=False))
    eok = mk.result("c2", "editor", mk.op("edit:/home/dev/p/a.py", "Edited"))
    doc = start()
    doc.assistant(calls=[mk.call("c1", "run_commands", {"commands": ["pytest -q"]}),
                         mk.call("c2", "editor", {"path": "/home/dev/p/a.py", "old_text": "a",
                                                  "new_text": "b"})])
    first, second = ((fail, efail), (ok, eok)) if order == "fail-first" else ((ok, eok),
                                                                            (fail, efail))
    doc.results(*first)
    doc.results(*second)
    run, edit = CLINE.parse(session_file(tmp_path, doc.document())).tool_calls()
    assert run.is_error and run.exit_code == 1
    assert edit.is_error and edit.status == "error"


def test_editor_output_cannot_fake_its_success(tmp_path):
    doc = start()
    doc.assistant(calls=[mk.call("c1", "editor", {"path": "/home/dev/p/a.py", "old_text": "a",
                                                  "new_text": "b"}),
                         mk.call("c2", "read_files", {"files": []})])
    doc.results(
        mk.result("c1", "editor", json.dumps({"query": "edit", "result": '{"success": true}',
                                              "error": "failed", "success": False})),
        mk.result("c2", "read_files", '{"query": "x", "success": false}'))
    edit, read = CLINE.parse(session_file(tmp_path, doc.document())).tool_calls()
    assert edit.is_error and edit.status == "error"
    assert not read.is_error and read.output == '{"query": "x", "success": false}'


def test_restored_checkpoint_leaves_no_trace_to_use(tmp_path):
    """A chat restore rewrites the file without the later run: only what is left is read."""
    doc = start()
    doc.assistant(calls=[mk.call("c1", "run_commands", {"commands": ["pytest -q"]})])
    doc.results(mk.result("c1", "run_commands", [mk.command_op("pytest -q", 1, "1 failed")]))
    kept = list(doc.messages)
    doc.user("Try again.")  # the run after this point is dropped by the restore
    doc.assistant(calls=[mk.call("c2", "run_commands", {"commands": ["pytest -q"]})])
    doc.results(mk.result("c2", "run_commands", [mk.command_op("pytest -q", 0, "3 passed")]))
    doc.messages = kept
    doc.assistant("All tests pass.")
    path = session_file(tmp_path, doc.document())
    assert audit(path).findings[0].verdict is Verdict.CONTRADICTED


def test_call_with_no_result_is_unverified(tmp_path):
    doc = start()
    doc.assistant(calls=[mk.call("c1", "run_commands", {"commands": ["pytest -q"]})])
    doc.assistant("All tests pass.")
    path = session_file(tmp_path, doc.document())
    run = shells(CLINE.parse(path))[0]
    assert run.result_seen is False
    finding = audit(path).findings[0]
    assert finding.verdict is Verdict.UNVERIFIED
    assert "no result for this run in the log" in finding.evidence


def test_several_commands_in_one_call_pair_by_position(tmp_path):
    doc = start()
    doc.assistant(calls=[mk.call("c1", "run_commands", {"commands": [
        "ruff check .", {"command": "pytest", "args": ["-q", "tests"]}]})])
    doc.results(mk.result("c1", "run_commands", [
        mk.command_op("ruff check .", 0, "All checks passed!"),
        mk.command_op("pytest -q tests", 1, "1 failed")]))
    ruff, tests = shells(CLINE.parse(session_file(tmp_path, doc.document())))
    assert ruff.command == "ruff check ." and ruff.status == "success"
    assert tests.command == "pytest -q tests" and tests.exit_code == 1
    assert ruff.tool_id == tests.tool_id == "c1"


def test_count_mismatch_pairs_only_exact_queries(tmp_path):
    doc = start()
    doc.assistant(calls=[mk.call("c1", "run_commands",
                                 {"commands": ["cat <<EOF > a.txt", "hi", "EOF", "pytest"]})])
    doc.results(mk.result("c1", "run_commands", [
        mk.command_op("cat <<EOF > a.txt\nhi\nEOF", 0, ""),
        mk.command_op("pytest", 0, "3 passed")]))
    runs = shells(CLINE.parse(session_file(tmp_path, doc.document())))
    assert [r.result_seen for r in runs] == [False, False, False, True]
    assert runs[-1].output == "3 passed"


@pytest.mark.parametrize("tool_input", [["pytest -q"], "pytest -q", {"command": "pytest -q"},
                                        {"cmd": "pytest -q"}, {"commands": "pytest -q"},
                                        [{"command": "pytest", "args": ["-q"]}],
                                        {"command": "pytest", "args": ["-q"]}])
def test_every_run_commands_input_shape(tmp_path, tool_input):
    doc = start()
    doc.assistant(calls=[mk.call("c1", "run_commands", tool_input)])
    run = shells(CLINE.parse(session_file(tmp_path, doc.document())))[0]
    assert run.command == "pytest -q"


def test_whole_call_error_marks_every_command(tmp_path):
    doc = start()
    doc.assistant(calls=[mk.call("c1", "run_commands", {"commands": ["a", "b"]})])
    doc.results(mk.result("c1", "run_commands", "Tool execution failed", is_error=True))
    runs = shells(CLINE.parse(session_file(tmp_path, doc.document())))
    assert all(r.result_seen and r.is_error and r.status == "error" for r in runs)
    assert all(r.exit_code is None for r in runs)


def test_write_edit_patch_and_other_tools_map(tmp_path):
    patch = ("*** Begin Patch\n*** Add File: src/new.py\n+x = 1\n"
             "*** Update File: src/old.py\n@@\n-a = 1\n+a = 2\n"
             "*** Delete File: src/gone.py\n*** End Patch")
    doc = start()
    doc.assistant(calls=[
        mk.call("c1", "editor", {"path": "/home/dev/p/a.py", "new_text": "x = 1\n"}),
        mk.call("c2", "editor", {"path": "/home/dev/p/b.py", "old_text": "old",
                                 "new_text": "new"}),
        mk.call("c3", "editor", {"path": "/home/dev/p/c.py", "new_text": "y\n",
                                 "insert_line": 3, "old_text": None}),
        mk.call("c4", "apply_patch", {"input": patch}),
        mk.call("c5", "read_files", {"files": [{"path": "/home/dev/p/a.py"}]}),
        mk.call("c6", "apply_patch", patch),
        mk.call("c7", "editor", {"path": "/home/dev/p/d.py", "old_text": "q",
                                 "new_text": "r"}),
    ])
    doc.results(
        mk.result("c1", "editor", mk.op("edit:/home/dev/p/a.py", "File created")),
        mk.result("c2", "editor", mk.op("edit:/home/dev/p/b.py", "Edited")),
        mk.result("c3", "editor", mk.op("insert:/home/dev/p/c.py", "Inserted")),
        mk.result("c4", "apply_patch", mk.op("apply_patch", "Done")),
        mk.result("c5", "read_files", [{"query": "/home/dev/p/a.py", "result": "x = 1",
                                        "success": True}]),
        mk.result("c7", "editor", mk.op("edit:/home/dev/p/d.py",
                                        "Editor operation failed: no match", success=False)),
    )
    calls = CLINE.parse(session_file(tmp_path, doc.document())).tool_calls()
    assert [e.tool_action for e in calls] == [WRITE, EDIT, EDIT, WRITE, EDIT, OTHER, OTHER,
                                              WRITE, EDIT, OTHER, EDIT]
    assert calls[0].file_path == "/home/dev/p/a.py" and calls[0].is_file_edit()
    assert calls[0].tool_input["content"] == "x = 1\n"
    assert calls[1].tool_input["old_string"] == "old" and not calls[1].is_error
    assert calls[2].tool_input == {"file_path": "/home/dev/p/c.py", "old_string": "",
                                   "new_string": "y\n"}
    assert calls[3].file_path == "src/new.py" and calls[4].tool_input["new_string"] == "a = 2"
    assert calls[5].file_path == "src/gone.py" and calls[5].status == "success"
    assert calls[6].tool_name == "read_files" and calls[6].output == "x = 1"
    assert calls[7].file_path == "src/new.py" and calls[7].result_seen is False
    assert calls[10].is_error and calls[10].status == "error"
    assert "no match" in calls[10].output


def test_multi_hunk_patch_is_a_multi_edit(tmp_path):
    patch = ("*** Begin Patch\n*** Update File: a.py\n@@\n-a\n+b\n@@\n-c\n+d\n*** End Patch")
    doc = start()
    doc.assistant(calls=[mk.call("c1", "apply_patch", {"input": patch})])
    (call,) = CLINE.parse(session_file(tmp_path, doc.document())).tool_calls()
    assert call.tool_action == MULTI_EDIT and len(call.tool_input["edits"]) == 2


def test_unknown_messages_and_blocks_are_counted(tmp_path):
    doc = start()
    doc.assistant("Still read.", thinking="hidden")
    doc.messages[-1]["content"].append({"type": "brand_new_block", "x": 1})
    doc.messages.insert(1, {"id": "m", "role": "tool", "content": []})
    doc.messages.insert(1, "not a message")
    doc.messages.insert(1, {"id": "m2", "role": "user", "content": "string content"})
    path = session_file(tmp_path, doc.document())
    session = CLINE.parse(path)
    assert [e.text for e in session.text_events()] == ["Still read."]
    assert session.unrecognised == 4
    report = render_terminal(audit(path), color=False)
    assert "4 unrecognised record(s) skipped" in report


def test_tagged_user_text_and_tool_results_are_not_prompts(tmp_path):
    doc = start()
    doc.user("<system-reminder>context</system-reminder>")
    doc.assistant(calls=[mk.call("c1", "read_files", {"files": []})])
    doc.results(mk.result("c1", "read_files", []))
    doc.user("Second prompt.")
    session = CLINE.parse(session_file(tmp_path, doc.document()))
    assert len(session.prompt_starts) == 2


@pytest.mark.parametrize("body", [b"{not json", b"[1, 2]", b'{"version": 2, "messages": []}',
                                  b"\x00\x01binary",
                                  pytest.param(b'{"a":' * 200_000 + b"1" + b"}" * 200_000,
                                               id="nested-too-deep")])
def test_malformed_file_is_one_unreadable_session(tmp_path, body, capsys):
    path = tmp_path / "bad.messages.json"
    path.write_bytes(body)
    assert not CLINE.detect(path)
    with pytest.raises(UnreadableSession):
        CLINE.parse(path)
    assert cli.main(["audit", str(path), "--agent", "cline", "--no-disk-check"]) != 0
    assert "bad.messages.json" in capsys.readouterr().err  # reported, not a crash


def test_oversized_file_is_refused(tmp_path, monkeypatch):
    path = session_file(tmp_path, mk.demo())
    monkeypatch.setattr(cline_mod, "MAX_FILE", 100)
    assert not CLINE.detect(path)
    with pytest.raises(UnreadableSession, match="not readable JSON"):
        CLINE.parse(path)


def test_bom_is_accepted(tmp_path):
    path = tmp_path / "bom.messages.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(mk.demo()).encode("utf-8"))
    assert CLINE.detect(path) and CLINE.parse(path).session_id == mk.SID


def test_licensed_fixture_parses():
    """Licensed fixture, not a real run: Cline's own published session fixture (Apache-2.0)."""
    assert (FIXTURES / "SESSION-FIXTURE-NOTICE.txt").is_file()
    assert hashlib.sha256(LICENSED.read_bytes()).hexdigest() == LICENSED_SHA256
    licence = FIXTURES / "LICENSE-cline.txt"
    assert hashlib.sha256(licence.read_bytes()).hexdigest() == LICENCE_SHA256
    session = CLINE.parse(LICENSED)
    assert session.session_id == "1784094124598_rruoq"
    assert session.cwd == "/Users/example/dev/cline"  # from the system prompt: no manifest copied
    assert len(session.prompt_starts) == 5
    calls = session.tool_calls()
    assert [c.tool_name for c in calls].count("editor") == 4
    assert [c.tool_name for c in calls].count("read_files") == 6
    assert all(c.result_seen for c in calls)
    assert all(c.tool_action == EDIT for c in calls if c.tool_name == "editor")
    (run,) = shells(session)
    assert run.command.startswith("grep -rn") and run.status == "success"
    assert run.exit_code is None and not run.is_error
    assert session.unrecognised == 0


# -- read only ----------------------------------------------------------------

def test_audit_detect_and_list_change_nothing(tmp_path, monkeypatch, capsys):
    sessions = tmp_path / "cs"
    path = mk.place(sessions, mk.demo())
    monkeypatch.setenv("CLINE_SESSION_DATA_DIR", str(sessions))
    before = snapshot(sessions)
    mtime = path.stat().st_mtime_ns
    assert CLINE.detect(path)
    audit(path)
    cli.run_audit("latest", check_disk=False, agent="cline")
    assert cli.main(["list", "--agent", "cline"]) == 0
    out = capsys.readouterr().out
    assert "17810000" in out and "acme-api" in out
    assert snapshot(sessions) == before
    assert path.stat().st_mtime_ns == mtime
