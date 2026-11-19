"""0.5.0 fix round 3, unit I (Security re-read round 2, 2026-11-19): readers and redaction.

Every value here is made up: no real run, no real path, no personal data. Token-like
strings are built from parts at run time.
"""

from __future__ import annotations

import gc
import importlib.util
import time
import tracemalloc
from pathlib import Path

import pytest

from donediff.readers import GEMINI, QWEN
from donediff.readers.base import UnreadableSession

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mk_gemini = _load("make_gemini_050r3", FIXTURES / "gemini" / "make_gemini.py")
mk_qwen = _load("make_qwen_050r3", FIXTURES / "qwen" / "make_qwen.py")


def _peak(reader, path):
    gc.collect()
    tracemalloc.start()
    try:
        session = reader.parse(path)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return session, peak


# -- NEW2-I-1: the Gemini and Qwen readers hold no padding anywhere --------------

def _pad():
    return [{} for _ in range(50_000)]  # ~0.2 MiB of JSON, about 4 MB once parsed


def _gemini_session(tmp_path, n_calls, where):
    clock = mk_gemini.Clock()
    records = [mk_gemini.meta(), mk_gemini.user(clock, "u1", "Find it.")]
    for i in range(n_calls):
        cid = f"c{i}"
        name, args = "google_web_search", {"query": "x"}
        result = mk_gemini.output(cid, name, "ok")
        if where == "read_key":  # a modelled tool's read key holding a list
            name, args = "write_file", {"file_path": f"f{i}.py", "content": _pad()}
        elif where == "dir_key":
            name, args = "run_shell_command", {"command": "ls", "dir_path": _pad()}
        elif where == "result":
            result = [{"functionResponse": {"id": cid, "name": name,
                                            "response": {"output": "ok", "pad": _pad()}}}]
        call = mk_gemini.call(clock, cid, name, args, "success", result)
        if where == "display":
            call["resultDisplay"] = _pad()
        record = mk_gemini.gemini(clock, f"g{i}", "", [call])
        if where == "record":
            record["pad"] = _pad()
        elif where == "content":
            record["content"] = [{"text": "Looking."}] + _pad()
        records.append(record)
        if where == "patch":  # a $patch result is held like any other
            records.append({"$patch": {"id": f"g{i}", "toolCalls": [
                {"id": cid, "result": [{"functionResponse": {
                    "id": cid, "name": name, "response": {"output": "ok", "pad": _pad()}}}]}]}})
        elif where == "meta":
            records.append({"$set": {"lastUpdated": "x", f"pad{i}": _pad()}})
    folder = tmp_path / f"g{where}{n_calls}"
    folder.mkdir()
    return mk_gemini.write(folder / f"session-2026-06-10T12-00-{n_calls:08x}.jsonl", records)


def _qwen_session(tmp_path, n_calls, where):
    chat = mk_qwen.Chat()
    user = chat.user("Find it.")
    if where == "parts":
        user["message"]["parts"] += _pad()
    for i in range(n_calls):
        cid = f"c{i}"
        name, args = "web_search", {"query": "x"}
        if where == "read_key":
            name, args = "write_file", {"file_path": f"f{i}.py", "content": _pad()}
        elif where == "dir_key":
            name, args = "run_shell_command", {"command": "ls", "directory": _pad()}
        record = chat.assistant(calls=[mk_qwen.call(cid, name, args)])
        if where == "record":
            record["pad"] = _pad()
        result = chat.result(cid, name, output="ok")
        if where == "result":
            result["message"]["parts"][0]["functionResponse"]["response"]["pad"] = _pad()
        elif where == "tool_call_result":
            result["toolCallResult"]["pad"] = _pad()
        elif where == "parts":
            result["message"]["parts"] += _pad()
    folder = tmp_path / f"q{where}{n_calls}"
    folder.mkdir()
    return mk_qwen.write(folder / f"{mk_qwen.SID}.jsonl", chat.records)


def _flat(reader, make, tmp_path, where):
    small, peak_small = _peak(reader, make(tmp_path, 2, where))
    big_path = make(tmp_path, 8, where)
    big, peak_big = _peak(reader, big_path)
    assert len(small.tool_calls()) == 2 and len(big.tool_calls()) == 8
    assert all(e.result_seen for e in big.tool_calls())
    # held padding grows the peak with the number of calls (was ~3.6x from 2 to 8)
    assert peak_big < 1.5 * peak_small, (peak_small, peak_big)
    return big


@pytest.mark.parametrize("where", ["read_key", "dir_key", "result", "display", "record",
                                   "content", "patch", "meta"])
def test_new2_i1_gemini_peak_stays_flat_wherever_the_padding_sits(tmp_path, where):
    session = _flat(GEMINI, _gemini_session, tmp_path, where)
    assert session.session_id == mk_gemini.SID


@pytest.mark.parametrize("where", ["read_key", "dir_key", "result", "tool_call_result",
                                   "record", "parts"])
def test_new2_i1_qwen_peak_stays_flat_wherever_the_padding_sits(tmp_path, where):
    session = _flat(QWEN, _qwen_session, tmp_path, where)
    assert session.session_id == mk_qwen.SID


def test_new2_i1_gemini_lean_replay_still_reads_every_field(tmp_path):
    clock = mk_gemini.Clock()
    records = [mk_gemini.meta(), mk_gemini.user(clock, "u1", "Fix it."),
               mk_gemini.gemini(clock, "g1", "Writing.", [
                   mk_gemini.call(clock, "w1", "write_file",
                                  {"file_path": "app.py", "content": "x = 1\n"}, "success",
                                  mk_gemini.output("w1", "write_file", "ok")),
                   mk_gemini.shell_call(clock, "s1", "python -m pytest -q", 1, "1 failed"),
                   mk_gemini.call(clock, "e1", "replace", {"file_path": "app.py"}, "error",
                                  mk_gemini.error("e1", "replace", "no match"))]),
               {"$patch": {"id": "g1", "toolCalls": [
                   {"id": "w1", "result": mk_gemini.output("w1", "write_file", "patched")}]}}]
    path = mk_gemini.write(tmp_path / "session-2026-06-10T12-00-6f1c2d3e.jsonl", records)
    session = GEMINI.parse(path)
    write, shell, edit = session.tool_calls()
    assert write.tool_input == {"file_path": "app.py", "content": "x = 1\n"}
    assert write.output == "patched" and not write.is_error
    assert shell.command == "python -m pytest -q" and shell.exit_code == 1
    assert edit.is_error and edit.output == "no match"
    assert [e.text for e in session.events if e.text] == ["Writing."]
    assert session.prompt_starts == [0] and session.first_timestamp


def test_new2_i1_qwen_lean_records_still_read_every_field(tmp_path):
    chat = mk_qwen.Chat()
    chat.user("Fix it.")
    chat.system("notification")
    note = chat.base("user")
    note["subtype"] = "cron"
    note["message"] = {"role": "user", "parts": [{"text": "a cron prompt"}]}
    mk_qwen.shell(chat, "s1", "python -m pytest -q", 0, "3 passed")
    chat.assistant(text="Done.", calls=[mk_qwen.call("b1", "run_shell_command",
                                                     {"command": "npm start", "is_background": True})])
    chat.result("b1", "run_shell_command", output="Background shell started.")
    chat.assistant(calls=[mk_qwen.call("x1", "web_search", {"query": "x"})])
    result = chat.result("x1", "web_search", output="ok", status="")
    result["message"]["parts"][0]["functionResponse"]["response"] = {"error": {"code": 1}}
    path = mk_qwen.write(tmp_path / f"{mk_qwen.SID}.jsonl", chat.records)
    session = QWEN.parse(path)
    shell, background, other = session.tool_calls()
    assert shell.exit_code == 0 and shell.status == "completed" and "3 passed" in shell.output
    assert background.background and background.status == "running"
    assert other.status == "error" and other.is_error  # a non-text error still names the status
    assert len(session.prompt_starts) == 1 and session.cwd == mk_qwen.CWD


@pytest.mark.parametrize("reader, make", [(GEMINI, _gemini_session), (QWEN, _qwen_session)])
def test_new2_i1_a_session_file_over_the_cap_is_not_read(tmp_path, monkeypatch, reader, make):
    import sys
    path = make(tmp_path, 2, "none")
    module = sys.modules[type(reader).__module__]
    assert module.MAX_FILE == 512 * 1024 * 1024
    monkeypatch.setattr(module, "MAX_FILE", path.stat().st_size - 1)
    with pytest.raises(UnreadableSession, match="larger than donediff reads"):
        reader.parse(path)
    monkeypatch.setattr(module, "MAX_FILE", path.stat().st_size)
    assert len(reader.parse(path).tool_calls()) == 2



# -- NEW2-I-2: one edit-pair budget per session, in every reader that keeps pairs --

_ASSERT_PAIR = {"old_string": "assert a", "new_string": ""}  # drops an assert in a test file


def _write_lines(path, records):
    import json
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return path


def _multiedit(call_id, pairs):
    from conftest import assistant_tool, tool_result
    return [assistant_tool(call_id, "MultiEdit", {"file_path": "tests/test_a.py",
                                                  "edits": [_ASSERT_PAIR] * pairs}),
            tool_result(call_id, "ok")]


def _with_subagent(tmp_path, main_records, sub_records):
    from conftest import assistant_text
    main = _write_lines(tmp_path / "s1.jsonl", main_records + [assistant_text("Done.")])
    folder = tmp_path / "s1" / "subagents"
    folder.mkdir(parents=True)
    _write_lines(folder / "agent-a1.jsonl", sub_records)
    return main


def test_new2_i2_subagent_edit_pairs_have_a_budget(tmp_path, monkeypatch):
    from donediff import parser
    from donediff.delegated import attach_sources
    monkeypatch.setattr(parser, "_MAX_EDIT_PAIRS", 10_000)
    records = []
    for i in range(30):  # 60,000 assert-dropping pairs, 2,000 a call
        records += _multiedit(f"m{i}", 2_000)
    session = attach_sources(parser.parse_transcript(_with_subagent(tmp_path, [], records)))
    (source,) = session.sources
    signals = [s for s in source.gaming if s.kind != "hidden_characters"]
    assert 0 < len(signals) <= 10_000, len(signals)  # one signal a pair: was 60,000
    assert session.unrecognised == 25  # the 25 calls past the budget


def test_new2_i2_the_budget_is_shared_by_the_transcript_and_its_subagents(tmp_path, monkeypatch):
    from donediff import parser
    from donediff.delegated import attach_sources
    monkeypatch.setattr(parser, "_MAX_EDIT_PAIRS", 10_000)
    main = _with_subagent(tmp_path, _multiedit("m0", 8_000),
                          _multiedit("s0", 2_000) + _multiedit("s1", 2_000))
    session = attach_sources(parser.parse_transcript(main))
    assert session.unrecognised == 1 and session.edit_pairs == 12_000


def test_new2_i2_a_real_subagent_multiedit_still_raises_its_signal(tmp_path):
    from donediff import parser
    from donediff.delegated import attach_sources
    session = attach_sources(parser.parse_transcript(
        _with_subagent(tmp_path, [], _multiedit("s0", 1))))
    (source,) = session.sources
    assert len(source.gaming) == 1 and session.unrecognised == 0


def _codex_patches(tmp_path, hunks, calls):
    import json  # noqa: F401
    mk_codex = _load("make_codex_050r3", FIXTURES / "codex" / "make_codex.py")
    clock = mk_codex.Clock()
    lines = [clock.line("session_meta", {**mk_codex.meta("s1", "legacy"), "cwd": mk_codex.CWD})]
    body = "*** Begin Patch\n*** Update File: a.py\n" + "@@\n+a\n" * hunks + "*** End Patch\n"
    for i in range(calls):
        lines += mk_codex.patch_call(clock, f"p{i}", body, "a.py")
    path = tmp_path / "rollout-2026-06-10T12-00-00-codexdemo-hunk-0001.jsonl"
    return mk_codex.write(path, lines)


def test_new2_i2_codex_patch_hunks_have_a_budget(tmp_path, monkeypatch):
    from donediff import parser
    from donediff.readers import CODEX
    monkeypatch.setattr(parser, "_MAX_EDIT_PAIRS", 10)
    session = CODEX.parse(_codex_patches(tmp_path, 8, 3))
    kept = [len(e.tool_input.get("edits", ())) for e in session.tool_calls()]
    assert kept == [8, 0, 0] and session.unrecognised == 2
    assert all(e.file_path == "a.py" for e in session.tool_calls())


def test_new2_i2_cline_patch_hunks_have_a_budget(tmp_path, monkeypatch):
    from donediff import parser
    from donediff.readers import CLINE
    monkeypatch.setattr(parser, "_MAX_EDIT_PAIRS", 10)
    mk_cline = _load("make_cline_050r3", FIXTURES / "cline" / "make_cline.py")
    body = "*** Begin Patch\n*** Update File: a.py\n" + "@@\n-a\n+b\n" * 8 + "*** End Patch"
    doc = mk_cline.Doc()
    doc.user("Fix it.")
    for i in range(3):
        doc.assistant(calls=[mk_cline.call(f"p{i}", "apply_patch", {"input": body})])
        doc.results(mk_cline.result(f"p{i}", "apply_patch", mk_cline.op("apply_patch", "Done")))
    doc.assistant("Done.")
    session = CLINE.parse(mk_cline.write(tmp_path / f"{mk_cline.SID}.messages.json",
                                         doc.document()))
    kept = [len(e.tool_input.get("edits", ())) for e in session.tool_calls()]
    assert kept == [8, 0, 0] and session.unrecognised == 2
    assert all(e.file_path == "a.py" for e in session.tool_calls())


def test_new2_i2_opencode_multiedit_pairs_have_a_budget(tmp_path, monkeypatch):
    from donediff import cli, parser
    monkeypatch.setattr(parser, "_MAX_EDIT_PAIRS", 10)
    mk_opencode = _load("make_opencode_050r3", FIXTURES / "opencode" / "make_opencode.py")
    builder = mk_opencode.Builder(tmp_path / "opencode.db").session(
        "ses_r3i20001", directory=str(tmp_path))
    builder.user("Fix it.")
    for _ in range(3):
        builder.tool("multiedit", {"filePath": "a.py",
                                   "edits": [{"oldString": "a", "newString": "b"}] * 8})
    builder.say("Done.")
    session = cli.run_audit(builder.close(), check_disk=False, agent="opencode").session
    kept = [len(e.tool_input.get("edits", ())) for e in session.tool_calls()]
    assert kept == [8, 0, 0] and session.unrecognised == 2


@pytest.mark.parametrize("reader", ["codex", "gemini", "qwen"])
def test_new2_i2_an_unmodelled_tool_keeps_no_edit_list(tmp_path, reader):
    import json
    args = {"file_path": "notes.md", "edits": [{"new_string": "1"}] * 50}
    if reader == "codex":
        mk_codex = _load("make_codex_050r3", FIXTURES / "codex" / "make_codex.py")
        clock = mk_codex.Clock()
        path = mk_codex.write(tmp_path / "rollout-2026-06-10T12-00-00-codexdemo-edit-0001.jsonl", [
            clock.line("session_meta", {**mk_codex.meta("s1", "legacy"), "cwd": mk_codex.CWD}),
            clock.line("response_item", {"type": "function_call", "name": "mcp__x__y",
                                         "call_id": "c1", "arguments": json.dumps(args)}),
            clock.line("response_item", {"type": "function_call_output", "call_id": "c1",
                                         "output": "ok"})])
        from donediff.readers import CODEX as chosen
    elif reader == "gemini":
        clock = mk_gemini.Clock()
        path = mk_gemini.write(tmp_path / "session-2026-06-10T12-00-6f1c2d3e.jsonl", [
            mk_gemini.meta(), mk_gemini.user(clock, "u1", "Go."),
            mk_gemini.gemini(clock, "g1", "", [mk_gemini.call(
                clock, "c1", "some_mcp_tool", args, "success",
                mk_gemini.output("c1", "some_mcp_tool", "ok"))])])
        chosen = GEMINI
    else:
        chat = mk_qwen.Chat()
        chat.user("Go.")
        chat.assistant(calls=[mk_qwen.call("c1", "some_mcp_tool", args)])
        chat.result("c1", "some_mcp_tool", output="ok")
        path = mk_qwen.write(tmp_path / f"{mk_qwen.SID}.jsonl", chat.records)
        chosen = QWEN
    (event,) = chosen.parse(path).tool_calls()
    assert event.tool_input == {"file_path": "notes.md"}  # the edits list is never read


# -- NEW2-I-3: a token whose first part holds a "-", glued to a dash, is masked --

def _dash_token(first="a" * 6 + "-" + "a" * 7):
    """Three dot-joined parts, the first holding a "-": made up, not a real token."""
    start = "e" + "yJ"
    return start + first + "." + start + "b" * 14 + "." + "c" * 20


@pytest.mark.parametrize("before", ["saved jwt-", "-", "cli --jwt-", "a-b-", "--", "X-Auth-",
                                    "wrote cache-"])
def test_new2_i3_a_dash_glued_token_with_a_dash_in_part_one_is_masked(before):
    from donediff.redact import redact_secrets
    token = _dash_token()
    shown = redact_secrets(before + token + " end")
    assert token not in shown and "a" * 7 + "." not in shown, shown.replace(token, "<token>")
    assert shown.startswith(before) and shown.endswith(" end")


@pytest.mark.parametrize("first", ["a" * 14, "a" * 6 + "_" + "a" * 7, "-" + "a" * 13,
                                   "a" * 12 + "-a"])
def test_new2_i3_other_first_parts_glued_to_a_dash_are_masked(first):
    from donediff.redact import redact_secrets
    token = _dash_token(first)
    assert token not in redact_secrets("saved jwt-" + token)


def test_new2_i3_a_dashed_name_that_is_no_token_stays():
    from donediff.redact import redact_secrets
    for text in ("the my-eyJunk-file.eyJson.txt name", "branch fix-eyJ-parse.eyJ.x",
                 "a-b-c-d-e-f-g-h-i-j-k-l.m-n-o-p-q-r-s-t.u-v-w-x-y-z"):
        assert redact_secrets(text) == text


@pytest.mark.parametrize("unit", ["-e" + "yJ-", "-e" + "yJ" + "a" * 6 + "-" + "a" * 7 + ".",
                                  "-e" + "yJ" + "a-" * 6 + ".e" + "yJ" + "b" * 8 + ".-",
                                  "a" * 11 + "-"])
def test_new2_i3_mask_stays_linear_on_dash_runs(unit):
    from donediff.redact import redact_secrets
    text = unit * (1024 * 1024 // len(unit))
    started = time.perf_counter()
    redact_secrets(text)
    assert time.perf_counter() - started < 2.0


# -- NEW2-I-3b: a token glued to a word or "_" is masked (secrets fail closed) ---

@pytest.mark.parametrize("before", ["TOKEN_", "auth", "export JWT_", "Bearer_", "id9"])
@pytest.mark.parametrize("first", ["a" * 14, "a" * 6 + "-" + "a" * 7, "a" * 6 + "_" + "a" * 7])
def test_new2_i3b_a_token_glued_to_a_word_is_masked(before, first):
    from donediff.redact import redact_secrets
    token = _dash_token(first)
    shown = redact_secrets(before + token + " end")
    assert token not in shown and "b" * 14 not in shown and "c" * 20 not in shown
    assert shown.startswith(before) and shown.endswith(" end")


def test_new2_i3b_words_and_dotted_names_that_are_no_token_stay():
    from donediff.redact import redact_secrets
    start = "e" + "yJ"
    for text in ("TOKEN_VALUE auth_header heyJude keyJar", "my_module.sub_module.function_name",
                 "monkeyJumps.over.the_lazy_dog_today",            # part two is no "eyJ"
                 "TOKEN_" + start + "short." + start + "abcdefgh.cccccccc",   # part one too short
                 "auth" + start + "abcdefgh." + start + "short.cccccccc",     # part two too short
                 "auth" + start + "abcdefgh." + start + "abcdefgh.ccc",       # part three too short
                 "v1.2.3 release_notes.final.txt"):
        assert redact_secrets(text) == text, text


@pytest.mark.parametrize("unit", ["x" + "e" + "yJ", "_e" + "yJ" + "a" * 8 + ".",
                                  "Te" + "yJ" + "a" * 8 + ".e" + "yJ" + "b" * 8 + "._",
                                  "TOKEN_e" + "yJ" + "a" * 7 + ".e" + "yJ" + "b" * 8 + ".c" * 8])
def test_new2_i3b_mask_stays_linear_on_word_runs(unit):
    from donediff.redact import redact_secrets
    text = unit * (1024 * 1024 // len(unit))
    started = time.perf_counter()
    redact_secrets(text)
    assert time.perf_counter() - started < 2.0


# -- NEW2-I-4: Goose, OpenCode and Cline keep only the read keys of a tool they do not model --

_UNMODELLED = {"query": "x", "file_path": "notes.md", "edits": [{"new_string": "1"}] * 3}


def _kept(reader, path):
    """Bytes still held once the session is parsed (the session kept alive)."""
    gc.collect()
    tracemalloc.start()
    try:
        session = reader.parse(path)
        gc.collect()
        kept = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    return session, kept


def _goose_session(tmp_path, n_calls, where, args):
    mk_goose = _load("make_goose_050r3", FIXTURES / "goose" / "make_goose.py")
    from donediff.readers import GOOSE
    builder = mk_goose.Builder(tmp_path / f"goose-{where}-{n_calls}.db").session("20260610_1")
    builder.user("Find it.")
    for _ in range(n_calls):
        if where == "tool":  # a tool outside the developer extension
            builder.tool("search__find", dict(args))
        else:  # a developer text_editor command donediff does not model
            builder.tool("developer__text_editor", {"command": "view", "path": "a.py", **args})
    builder.say("Done.")
    return GOOSE, GOOSE.resolve(builder.close())


def _opencode_session(tmp_path, n_calls, where, args):
    mk_opencode = _load("make_opencode_050r3", FIXTURES / "opencode" / "make_opencode.py")
    from donediff.readers import OPENCODE
    builder = mk_opencode.Builder(tmp_path / f"opencode-{n_calls}.db").session(
        "ses_r3i40001", directory=str(tmp_path))
    builder.user("Find it.")
    for _ in range(n_calls):
        builder.tool("websearch", dict(args), output="ok")
    builder.say("Done.")
    return OPENCODE, OPENCODE.resolve(builder.close())


def _cline_session(tmp_path, n_calls, where, args):
    mk_cline = _load("make_cline_050r3", FIXTURES / "cline" / "make_cline.py")
    from donediff.readers import CLINE
    doc = mk_cline.Doc()
    doc.user("Find it.")
    name = "web_fetch" if where == "tool" else "run_commands"  # run_commands with no command
    for i in range(n_calls):
        doc.assistant(calls=[mk_cline.call(f"c{i}", name, dict(args))])
        doc.results(mk_cline.result(f"c{i}", name, "ok"))
    doc.assistant("Done.")
    return CLINE, mk_cline.write(tmp_path / f"{n_calls}" / f"{mk_cline.SID}.messages.json",
                                 doc.document())


_READERS_I4 = [(_goose_session, "tool"), (_goose_session, "text_editor"),
               (_opencode_session, "tool"), (_cline_session, "tool"), (_cline_session, "shell")]


@pytest.mark.parametrize("make, where", _READERS_I4)
def test_new2_i4_unmodelled_tool_keeps_only_read_keys(tmp_path, make, where):
    reader, path = make(tmp_path, 1, where, _UNMODELLED)
    calls = reader.parse(path).tool_calls()
    view = {"command": "view"} if where == "text_editor" else {}  # "command" is a read key
    assert len(calls) == 1 and calls[0].tool_input == {**view, "file_path": "notes.md"}


@pytest.mark.parametrize("make, where", _READERS_I4)
def test_new2_i4_padding_in_an_unmodelled_tool_is_not_kept(tmp_path, make, where):
    pad = {"query": "x", "pad": _pad()}  # ~4 MB once parsed, per call
    reader, path = make(tmp_path, 8, where, pad)
    session, kept = _kept(reader, path)
    assert len(session.tool_calls()) == 8
    assert kept < 2 * 1024 * 1024, kept  # 8 held pads were ~30 MB
    view = {"command": "view"} if where == "text_editor" else {}
    assert all(e.tool_input == view for e in session.tool_calls())
