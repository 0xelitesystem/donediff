"""The Codex CLI reader: format, discovery, mapping to shared actions, parity, red team."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

from donediff import cli, parser
from donediff.models import EDIT, MULTI_EDIT, OTHER, SHELL, WRITE, ClaimType, Verdict
from donediff.readers import CODEX, select, select_for_list
from donediff.readers import codex as codex_mod
from donediff.readers.base import UnreadableSession
from donediff.readers.claude_code import CLAUDE_CODE
from donediff.report import render_markdown, render_terminal
from donediff.shell import confined

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo-session.jsonl"
_spec = importlib.util.spec_from_file_location(
    "make_codex", ROOT / "tests" / "fixtures" / "codex" / "make_codex.py")
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)

NAME = "rollout-2026-06-10T12-00-00-{}.jsonl"
HAS_ZSTD = sys.version_info >= (3, 14)


def rollout(tmp_path, records, sid="codexdemo-legacy-0001", folder=None, compress=False):
    path = (folder or tmp_path) / (NAME.format(sid) + (".zst" if compress else ""))
    return mk.write(path, records, compress=compress)


def session_lines(sid="s1", cwd=mk.CWD, mode="legacy"):
    clock = mk.Clock()
    return clock, [clock.line("session_meta", {**mk.meta(sid, mode), "cwd": cwd})]


def call(clock, name, args, call_id):
    return clock.line("response_item", {"type": "function_call", "name": name,
                                        "call_id": call_id, "arguments": json.dumps(args)})


def out(clock, call_id, text, kind="function_call_output"):
    return clock.line("response_item", {"type": kind, "call_id": call_id, "output": text})


def say(clock, text):
    return clock.line("response_item", mk.message("assistant", text))


def shells(session):
    return [e for e in session.tool_calls() if e.tool_action == SHELL]


def verdicts(result):
    return [(f.claim.type, f.verdict) for f in result.findings]


# -- detect and auto ----------------------------------------------------------

def test_detect_both_ways(tmp_path):
    codex = rollout(tmp_path, mk.legacy_demo())
    assert CODEX.detect(codex) and not CLAUDE_CODE.detect(codex)
    assert CLAUDE_CODE.detect(DEMO) and not CODEX.detect(DEMO)
    assert CODEX.detect(tmp_path / (NAME.format("x") + ".zst"))  # by name, unread
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert not CODEX.detect(empty) and not CLAUDE_CODE.detect(empty)


def test_auto_picks_by_file_and_falls_back_to_claude(tmp_path):
    codex = rollout(tmp_path, mk.paginated_demo(), sid="codexdemo-paged-0001")
    assert select("auto", str(codex)) is CODEX
    assert select("auto", str(DEMO)) is CLAUDE_CODE
    junk = tmp_path / "junk.jsonl"
    junk.write_text("not json\n", encoding="utf-8")
    assert select("auto", str(junk)) is CLAUDE_CODE
    assert select("codex") is CODEX and select("auto") is CLAUDE_CODE


def test_auto_latest_and_list_use_codex_only_when_claude_has_none(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    rollout(tmp_path, mk.legacy_demo(), folder=home / "sessions" / "2026" / "06" / "10")
    monkeypatch.setenv("CODEX_HOME", str(home))
    empty = tmp_path / "claude-projects"
    empty.mkdir()
    monkeypatch.setattr(parser, "projects_dir", lambda: empty)
    assert select("auto", "latest") is CODEX
    assert select_for_list("auto") is CODEX
    assert select("auto", "codexdemo-leg") is CODEX
    project = empty / "proj"
    project.mkdir()
    (project / "abc.jsonl").write_text(DEMO.read_text(encoding="utf-8"), encoding="utf-8")
    assert select("auto", "latest") is CLAUDE_CODE
    assert select_for_list("auto") is CLAUDE_CODE
    assert select("auto", "codexdemo-leg") is CODEX  # no Claude id matches
    assert select("auto", "nothing-matches") is CLAUDE_CODE


# -- discovery ------------------------------------------------------------------

def _day(home, y, m, d):
    return home / "sessions" / y / m / d


def test_discovery_newest_first_filtered_by_recorded_cwd(tmp_path, monkeypatch):
    home = tmp_path / "ch"
    clock, a = session_lines("aaa", cwd="/work/alpha")
    clock, b = session_lines("bbb", cwd="/work/beta")
    pa = rollout(tmp_path, a, sid="aaa", folder=_day(home, "2026", "06", "10"))
    pb = rollout(tmp_path, b, sid="bbb", folder=_day(home, "2026", "06", "11"))
    os.utime(pa, (2_000_000_000, 2_000_000_000))
    os.utime(pb, (1_900_000_000, 1_900_000_000))
    (home / "sessions" / "notes").mkdir()
    rollout(tmp_path, a, sid="ccc", folder=home / "sessions" / "notes")  # not a year
    (_day(home, "2026", "06", "10") / "other.jsonl").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert CODEX.discover() == [pa, pb]
    assert CODEX.discover("BETA") == [pb]
    assert CODEX.discover("gamma") == []
    assert CODEX.resolve("latest") == pa
    assert CODEX.resolve("bb") == pb
    with pytest.raises(FileNotFoundError, match="no transcript matching"):
        CODEX.resolve("zzz")
    assert CODEX.describe(pa) == ("aaa", "alpha")


def test_codex_home_default_and_bad_values(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert codex_mod.sessions_dir() == tmp_path / ".codex" / "sessions"
    assert CODEX.location == "~/.codex/sessions"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    assert CODEX.discover() == []
    with pytest.raises(FileNotFoundError, match=r"\$CODEX_HOME/sessions"):
        CODEX.resolve("latest")
    monkeypatch.setenv("CODEX_HOME", "\\\\some-host\\share")
    assert codex_mod.codex_home() is None


def test_symlinked_session_folder_and_file_are_skipped(tmp_path, monkeypatch):
    home = tmp_path / "ch"
    real = rollout(tmp_path, mk.legacy_demo(), folder=_day(home, "2026", "06", "10"))
    elsewhere = tmp_path / "elsewhere"
    rollout(tmp_path, mk.legacy_demo(), sid="outside", folder=elsewhere)
    try:
        os.symlink(elsewhere, _day(home, "2026", "06", "11"), target_is_directory=True)
        os.symlink(real, _day(home, "2026", "06", "10") / NAME.format("linked"))
    except (OSError, NotImplementedError):
        pytest.skip("no symlink rights")
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert CODEX.discover() == [real]


# -- mapping --------------------------------------------------------------------

@pytest.mark.parametrize("make", [mk.legacy_demo, mk.paginated_demo])
def test_calls_pair_with_outputs_and_texts_are_not_doubled(tmp_path, make):
    session = CODEX.parse(rollout(tmp_path, make()))
    assert session.session_id.startswith("codexdemo-")
    assert session.cwd == mk.CWD and session.git_branch == "main"
    assert [e.text for e in session.text_events()] == [mk.OPENING, mk.CLOSING]
    assert len(session.prompt_starts) == 1  # the typed prompt, not the context notes
    runs = shells(session)
    assert [e.command for e in runs] == [r[0] for r in mk.RUNS]
    assert [e.exit_code for e in runs] == [1, 0, 0]
    assert [e.output.strip() for e in runs] == [r[2] for r in mk.RUNS]
    assert all(e.cwd == mk.CWD for e in runs)
    assert session.unrecognised == 0
    assert [e.index for e in session.events] == list(range(len(session.events)))


def test_no_exit_code_gives_status_never_a_made_up_code(tmp_path):
    clock, lines = session_lines()
    lines += [
        call(clock, "exec_command", {"cmd": "pytest -q"}, "c1"),
        out(clock, "c1", "12 passed in 0.4s"),  # no header: no exit code
        say(clock, "All tests pass."),
    ]
    path = rollout(tmp_path, lines)
    run = shells(CODEX.parse(path))[0]
    assert run.exit_code is None and run.status == "not recorded" and not run.is_error
    result = cli.run_audit(str(path), check_disk=False, agent="codex")
    finding = result.findings[0]
    assert finding.verdict is Verdict.UNVERIFIED
    assert "the log has a status only (not recorded)" in finding.evidence
    assert "exit 0" not in finding.evidence


def test_local_shell_call_status_and_failed_status(tmp_path):
    clock, lines = session_lines(mode="paginated")
    lines += [
        clock.line("response_item", {"type": "local_shell_call", "call_id": "c1",
                                     "status": "completed",
                                     "action": {"type": "exec", "command": ["pytest", "-q"],
                                                "working_directory": "sub"}}),
        out(clock, "c1", "ok"),
        say(clock, "Tests pass."),
        call(clock, "exec_command", {"cmd": "npm test"}, "c2"),
        clock.line("event_msg", {"type": "item_completed", "thread_id": "s1", "turn_id": "t",
                                 "item": {"type": "CommandExecution", "id": "c2",
                                          "status": "failed", "command": [], "cwd": "",
                                          "parsed_cmd": [], "source": "agent"}}),
        say(clock, "All tests pass now."),
    ]
    path = rollout(tmp_path, lines)
    session = CODEX.parse(path)
    first, second = shells(session)
    assert first.command == "pytest -q" and first.status == "completed"
    assert first.exit_code is None and first.cwd == mk.CWD + "/sub"
    assert second.exit_code is None and second.status == "failed" and second.is_error
    result = cli.run_audit(str(path), check_disk=False, agent="codex")
    assert [f.verdict for f in result.findings] == [Verdict.UNVERIFIED, Verdict.CONTRADICTED]
    assert "status failed" in result.findings[1].evidence


def test_running_command_counts_once_write_stdin_reports_its_exit(tmp_path):
    clock, lines = session_lines()
    lines += [
        call(clock, "exec_command", {"cmd": "pytest -q"}, "c1"),
        out(clock, "c1", mk.exec_output(None, "collecting", running="7")),
        say(clock, "Tests pass."),  # before the run finished: no evidence yet
        call(clock, "write_stdin", {"session_id": 7, "chars": ""}, "c2"),
        out(clock, "c2", mk.exec_output(0, "12 passed in 3.0s")),
        say(clock, "Tests pass."),
    ]
    path = rollout(tmp_path, lines)
    run = shells(CODEX.parse(path))[0]
    assert run.background and run.exit_code == 0 and "12 passed" in run.output
    result = cli.run_audit(str(path), check_disk=False, agent="codex")
    assert [f.verdict for f in result.findings] == [Verdict.UNVERIFIED, Verdict.VERIFIED]


def test_item_after_write_stdin_completion_keeps_the_evidence(tmp_path):
    clock, lines = session_lines(mode="paginated")
    lines += [
        call(clock, "exec_command", {"cmd": "pytest -q"}, "c1"),
        out(clock, "c1", mk.exec_output(None, "collecting", running="7")),
        call(clock, "write_stdin", {"session_id": 7}, "c2"),
        out(clock, "c2", mk.exec_output(0, "12 passed in 3.0s")),
        clock.line("event_msg", {"type": "item_completed", "thread_id": "s1", "turn_id": "t",
                                 "item": {"type": "CommandExecution", "id": "c1",
                                          "status": "completed", "exit_code": 0,
                                          "aggregated_output": "12 passed in 3.0s\n"}}),
        say(clock, "All tests pass."),
    ]
    path = rollout(tmp_path, lines)
    run = shells(CODEX.parse(path))[0]
    assert run.check_types and run.exit_code == 0
    finding = cli.run_audit(str(path), check_disk=False, agent="codex").findings[0]
    assert finding.verdict is Verdict.VERIFIED


def test_paginated_item_completes_a_running_command_and_notes_truncation(tmp_path):
    clock, lines = session_lines(mode="paginated")
    big = "x\n... command output truncated for persistence ...\n12 passed in 9.0s\n"
    lines += [
        call(clock, "exec_command", {"cmd": "pytest -q"}, "c1"),
        out(clock, "c1", mk.exec_output(None, "", running="3")),
        clock.line("event_msg", {"type": "item_completed", "thread_id": "s1", "turn_id": "t",
                                 "item": {"type": "CommandExecution", "id": "c1",
                                          "status": "completed", "exit_code": 0,
                                          "aggregated_output": big, "command": [],
                                          "cwd": "file:///work/acme-api", "parsed_cmd": [],
                                          "source": "agent"}}),
        say(clock, "All tests pass."),
    ]
    path = rollout(tmp_path, lines)
    run = shells(CODEX.parse(path))[0]
    assert run.exit_code == 0 and run.output_truncated
    finding = cli.run_audit(str(path), check_disk=False, agent="codex").findings[0]
    assert finding.verdict is Verdict.VERIFIED
    assert "output cut at 64 KiB in the log" in finding.evidence


def test_apply_patch_becomes_write_and_edit_pairs(tmp_path):
    patch = ("*** Begin Patch\n"
             "*** Add File: src/new.py\n+def f():\n+    return 1\n"
             "*** Update File: src/old.py\n@@ def g():\n-    return 1\n+    return 2\n"
             " # end\n"
             "*** Update File: src/two.py\n*** Move to: src/moved.py\n@@\n-a = 1\n+a = 2\n"
             "@@\n-b = 1\n+b = 2\n"
             "*** Delete File: src/gone.py\n"
             "*** End Patch\n")
    clock, lines = session_lines()
    lines += [
        clock.line("response_item", {"type": "custom_tool_call", "call_id": "p1",
                                     "name": "apply_patch", "input": patch}),
        out(clock, "p1", "Success.", kind="custom_tool_call_output"),
        call(clock, "apply_patch", {"input": patch.replace("new.py", "fn.py")}, "p2"),
        call(clock, "shell", {"command": ["apply_patch", patch]}, "p3"),
    ]
    session = CODEX.parse(rollout(tmp_path, lines))
    calls = session.tool_calls()
    assert [e.tool_action for e in calls[:4]] == [WRITE, EDIT, MULTI_EDIT, OTHER]
    add, edit, multi, gone = calls[:4]
    assert add.file_path == "src/new.py" and add.tool_input["content"] == "def f():\n    return 1\n"
    assert edit.tool_input == {"file_path": "src/old.py", "old_string": "    return 1\n# end",
                               "new_string": "    return 2\n# end"}
    assert multi.file_path == "src/moved.py"
    assert multi.tool_input["edits"] == [{"old_string": "a = 1", "new_string": "a = 2"},
                                         {"old_string": "b = 1", "new_string": "b = 2"}]
    assert gone.file_path == "src/gone.py" and not gone.is_file_edit()
    assert all(e.output == "Success." for e in calls[:4])
    assert calls[4].file_path == "src/fn.py" and calls[8].tool_action == WRITE
    assert len(calls) == 12 and not shells(session)


def test_patch_failure_marks_the_edits(tmp_path):
    clock, lines = session_lines()
    lines += mk.patch_call(clock, "p1", mk.PATCH_1, "x") + [
        clock.line("event_msg", {"type": "patch_apply_end", "call_id": "p1", "stdout": "",
                                 "stderr": "", "success": False, "status": "failed"})]
    edit = CODEX.parse(rollout(tmp_path, lines)).tool_calls()[0]
    assert edit.tool_action == EDIT and edit.is_error


# -- parity with the Claude Code demo -------------------------------------------

@pytest.mark.parametrize("make", [mk.legacy_demo, mk.paginated_demo])
def test_same_claims_same_verdicts_as_the_claude_demo(tmp_path, make):
    claude = cli.run_audit(str(DEMO), check_disk=False, agent="claude-code")
    codex = cli.run_audit(str(rollout(tmp_path, make())), check_disk=False)  # auto
    assert verdicts(codex) == verdicts(claude)
    assert verdicts(claude)  # the demo has claims
    assert sorted(s.kind for s in codex.gaming_signals) == \
        sorted(s.kind for s in claude.gaming_signals)
    assert codex.score == claude.score and codex.grade == claude.grade


@pytest.mark.skipif(not HAS_ZSTD, reason="compression.zstd needs Python 3.14")
def test_compressed_rollout_reads_like_the_plain_one(tmp_path):
    plain = rollout(tmp_path, mk.legacy_demo())
    packed = rollout(tmp_path / "z", mk.legacy_demo(), compress=True)
    assert packed.name.endswith(".jsonl.zst") and CODEX.detect(packed)
    a = cli.run_audit(str(plain), check_disk=False, agent="codex")
    b = cli.run_audit(str(packed), check_disk=False)
    assert verdicts(a) == verdicts(b) and a.score == b.score


def test_compressed_rollout_below_314_says_so(tmp_path, monkeypatch, capsys):
    packed = tmp_path / (NAME.format("old") + ".zst")
    packed.write_bytes(b"\x28\xb5\x2f\xfd")
    monkeypatch.setattr(codex_mod, "sys", types.SimpleNamespace(version_info=(3, 10)))
    with pytest.raises(UnreadableSession, match="needs Python 3.14"):
        CODEX.parse(packed)
    assert cli.main(["audit", str(packed), "--agent", "codex"]) == 2
    assert "compressed session: needs Python 3.14 to read" in capsys.readouterr().err
    assert CODEX.describe(packed) == ("old", "(compressed)")


# -- red team -------------------------------------------------------------------

def test_malformed_and_unknown_records_are_skipped_and_counted(tmp_path):
    clock, lines = session_lines()
    path = rollout(tmp_path, lines + [say(clock, "Hello.")])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{not json\n[1, 2]\n\"text\"\n")
        fh.write(json.dumps({"type": "brand_new_record", "payload": {}}) + "\n")
        fh.write(json.dumps({"type": "response_item", "payload": {"type": "hologram"}}) + "\n")
        fh.write(json.dumps({"type": "response_item", "payload": "not a dict"}) + "\n")
        fh.write(json.dumps({"type": "event_msg", "payload": {"type": "anything"}}) + "\n")
        fh.write(json.dumps({"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "nobody", "output": 5}}) + "\n")
    session = CODEX.parse(path)
    assert session.unrecognised == 6
    assert [e.text for e in session.text_events()] == ["Hello."]
    result = cli.run_audit(str(path), check_disk=False, agent="codex")
    assert "6 unrecognised record(s) skipped" in render_terminal(result, color=False)
    assert "6 unrecognised record(s) skipped" in render_markdown(result)


def test_huge_line_is_skipped_without_reading_it_whole(tmp_path, monkeypatch):
    clock, lines = session_lines()
    path = rollout(tmp_path, lines + [say(clock, "x" * 5000), say(clock, "After.")])
    monkeypatch.setattr(codex_mod, "MAX_LINE", 1000)
    session = CODEX.parse(path)
    assert session.unrecognised == 1
    assert [e.text for e in session.text_events()] == ["After."]
    monkeypatch.undo()
    big = rollout(tmp_path / "b", lines + [say(clock, "y" * 3_000_000)])
    assert len(CODEX.parse(big).text_events()[0].text) == 3_000_000


@pytest.mark.parametrize("cwd", [
    "\\\\evil-host\\share\\repo", "//evil-host/share/repo", "file://evil-host/share/repo",
    "/work/acme-api/../../../etc", "/work/a\x00b", "/work/\x1b[31mred\x1b[0m",
])
def test_path_tricks_in_cwd_never_reach_the_disk_unconfined(tmp_path, cwd):
    clock, lines = session_lines(cwd=cwd)
    lines.append(say(clock, "I created src/app.py and it works."))
    path = rollout(tmp_path, lines)
    session = CODEX.parse(path)
    assert "\x00" not in session.cwd
    result = cli.run_audit(str(path), check_disk=True, agent="codex")
    assert all(f.verdict is not Verdict.VERIFIED for f in result.findings)
    assert "\x1b" not in render_terminal(result, color=False)
    if session.cwd.startswith(("\\\\", "//")):
        assert confined("src/app.py", session.cwd) is None


def test_clean_path_handles_uris():
    assert codex_mod.clean_path("file:///C:/work/x%20y") == "C:/work/x y"
    assert codex_mod.clean_path("file:///work/x") == "/work/x"
    assert codex_mod.clean_path("file://host/share/x") == "//host/share/x"
    assert codex_mod.clean_path("file:///%00/bad/path/x") == ""
    assert codex_mod.clean_path(5) == ""


# -- the command line -----------------------------------------------------------

def test_cli_list_and_audit_with_agent_codex(tmp_path, monkeypatch, capsys):
    home = tmp_path / "ch"
    rollout(tmp_path, mk.paginated_demo(), sid="codexdemo-paged-0001",
            folder=_day(home, "2026", "06", "10"))
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert cli.main(["list", "--agent", "codex"]) == 0
    heading, line = capsys.readouterr().out.strip().splitlines()  # column headings first
    assert heading.split() == ["ID", "MODIFIED", "SIZE", "PROJECT"]
    assert line.startswith("codexdem") and line.endswith("acme-api")
    assert cli.main(["audit", "latest", "--agent", "codex", "--json", "--no-disk-check"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["session_id"] == "codexdemo-paged-0001"
    assert [c["verdict"] for c in data["claims"]] == \
        [v.value for _, v in verdicts(cli.run_audit(str(DEMO), check_disk=False))]
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    assert cli.main(["list", "--agent", "codex"]) == 2
    assert "no transcripts found under $CODEX_HOME/sessions" in capsys.readouterr().err


def test_claim_types_in_the_demo_are_the_expected_ones(tmp_path):
    types_seen = {f.claim.type for f in cli.run_audit(
        str(rollout(tmp_path, mk.legacy_demo())), check_disk=False).findings}
    assert {ClaimType.TESTS_PASS, ClaimType.COMMIT_MADE} <= types_seen
