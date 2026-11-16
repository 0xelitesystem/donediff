"""Codex code mode (0.169.0): the `exec` tool's JavaScript, its result header, new records.

Fixture shapes come from codex-rs at tag rust-v0.169.0 (d32063f96c2d), see
tests/fixtures/codex/make_codex.py; never from a real log.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from donediff import cli, verify
from donediff.models import OTHER, SHELL, WRITE, Verdict
from donediff.readers import CODEX
from donediff.readers import codex_code_mode as cm
from donediff.report import scope_line

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "make_codex", ROOT / "tests" / "fixtures" / "codex" / "make_codex.py")
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)

SID = "codexdemo-codemode-0001"
HELLO = "C:/work/demo/hello.py"
PATCH = f'*** Begin Patch\n*** Add File: {HELLO}\n+print("hello")\n*** End Patch'
DONE = "Script completed"
FAILED = "Script failed"


def start(mode="paginated"):
    clock = mk.Clock()
    return clock, [clock.line("session_meta", {**mk.meta(SID, mode), "cwd": "C:/work/demo"})]


def say(clock, text):
    return clock.line("response_item", mk.message("assistant", text))


def write(tmp_path, lines):
    return mk.write(tmp_path / f"rollout-2026-06-10T12-00-00-{SID}.jsonl", lines)


def parse(tmp_path, lines):
    return CODEX.parse(write(tmp_path, lines))


def audit(tmp_path, lines, **kw):
    return cli.run_audit(str(write(tmp_path, lines)), check_disk=False, agent="codex", **kw)


def patch_js(patch=PATCH):
    return f"text(await tools.apply_patch({mk.js(patch)}));"


def shell_js(cmd="python -m pytest -q"):
    return (f"const r = await tools.exec_command({{cmd: {mk.js(cmd)}, workdir: \"C:/work/demo\"}});\n"
            "text(r.output);")


# -- apply_patch ----------------------------------------------------------------

def test_patch_success_is_a_write(tmp_path):
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", patch_js()),
              mk.exec_result(clock, "c1", DONE, [f"Success. Updated the following files:\nA {HELLO}\n"])]
    session = parse(tmp_path, lines)
    [event] = session.tool_calls()
    assert event.tool_action == WRITE and event.file_path == HELLO
    assert event.tool_input["content"] == 'print("hello")\n'
    assert event.tool_name == "apply_patch" and event.tool_id == "c1#1"
    assert event.status == "completed" and not event.is_error and event.result_seen
    assert session.unrecognised == 0


def test_rejected_patch_is_no_write(tmp_path):
    """The rejected shape seen in a real 0.169.0 run, with synthetic paths."""
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", patch_js()),
              mk.exec_result(clock, "c1", FAILED, ["Script error:\n" + mk.REJECTED]),
              say(clock, "I created `hello.py` and it works.")]
    [event] = parse(tmp_path, lines).tool_calls()
    assert event.tool_action == WRITE and event.is_error
    assert event.status == "script failed" and event.result_seen


def test_rejected_patch_never_verifies_the_file_claim(tmp_path):
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", patch_js()),
              mk.exec_result(clock, "c1", FAILED, ["Script error:\n" + mk.REJECTED]),
              say(clock, "I created `hello.py` and it works.")]
    result = audit(tmp_path, lines)
    assert result.findings and all(f.verdict is not Verdict.VERIFIED for f in result.findings)
    assert any("did not succeed (script failed)" in f.evidence for f in result.findings)


def test_write_with_no_output_record_never_verifies(tmp_path):
    """SEC-208: an exec with no result has result_seen False, so verify._write_failed holds."""
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", patch_js()),
              say(clock, "I created `hello.py`.")]
    [event] = parse(tmp_path, lines).tool_calls()
    assert event.result_seen is False and verify._write_failed(event)
    result = audit(tmp_path, lines)
    assert result.findings and all(f.verdict is not Verdict.VERIFIED for f in result.findings)
    assert any("no result was recorded" in f.evidence for f in result.findings)


def test_unknown_outcome_wording(tmp_path):
    """A write the script hides reads 'unknown outcome', never 'did not succeed'."""
    clock, lines = start()
    source = f"try {{ await tools.apply_patch({mk.js(PATCH)}); }} catch (e) {{ text('ok'); }}"
    lines += [mk.exec_call(clock, "c1", source), mk.exec_result(clock, "c1", DONE, []),
              say(clock, "I created `hello.py`.")]
    [event] = [e for e in parse(tmp_path, lines).tool_calls() if e.tool_action == WRITE]
    assert event.is_error and verify._write_failed(event)
    assert verify._write_why(event) == (
        "has an unknown outcome: the log does not show whether it ran")
    result = audit(tmp_path, lines)
    assert result.findings and all(f.verdict is not Verdict.VERIFIED for f in result.findings)
    assert not any("did not succeed" in f.evidence for f in result.findings)
    assert any("has an unknown outcome" in f.evidence for f in result.findings)


def test_update_and_delete_paths(tmp_path):
    patch = ("*** Begin Patch\n*** Update File: C:/work/demo/a.py\n@@\n-x = 1\n+x = 2\n"
             "*** Delete File: C:/work/demo/b.py\n*** End Patch")
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", patch_js(patch)),
              mk.exec_result(clock, "c1", DONE, ["Success."])]
    events = parse(tmp_path, lines).tool_calls()
    assert [(e.tool_action, e.file_path) for e in events] == [
        ("edit", "C:/work/demo/a.py"), (OTHER, "C:/work/demo/b.py")]
    assert all(e.status == "completed" and not e.is_error for e in events)


# -- shell ------------------------------------------------------------------------

def test_shell_success_is_status_only(tmp_path):
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", shell_js()),
              mk.exec_result(clock, "c1", DONE, ["3 passed in 0.1s"]),
              say(clock, "All tests pass.")]
    [run] = parse(tmp_path, lines).tool_calls()
    assert run.tool_action == SHELL and run.command == "python -m pytest -q"
    # "Script completed" says nothing about a nested command's exit (SEC-213).
    assert run.cwd == "C:/work/demo" and run.exit_code is None and run.status == "outcome unknown"
    assert not run.is_error
    [finding] = audit(tmp_path, lines).findings
    assert finding.verdict is Verdict.UNVERIFIED
    assert "the log has a status only (outcome unknown)" in finding.evidence
    assert "exit" not in finding.evidence


def test_shell_failure_inside_a_completed_script_is_not_verified(tmp_path):
    """exec_command hands the script an exit code; the script prints it, Codex does not."""
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", shell_js() + "\ntext(r.exit_code);"),
              mk.exec_result(clock, "c1", DONE, ["1 failed, 2 passed in 0.2s", "1"]),
              say(clock, "All tests pass.")]
    [finding] = audit(tmp_path, lines).findings
    assert finding.verdict is Verdict.UNVERIFIED


def test_shell_in_a_failed_script(tmp_path):
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", shell_js()),
              mk.exec_result(clock, "c1", FAILED, ["Script error:\nexec_command rejected"]),
              say(clock, "All tests pass.")]
    [run] = parse(tmp_path, lines).tool_calls()
    assert run.status == "script failed" and run.exit_code is None
    [finding] = audit(tmp_path, lines).findings
    assert finding.verdict is Verdict.UNVERIFIED
    assert "status only (script failed)" in finding.evidence


@pytest.mark.parametrize("printed", [
    "Exit Code: 0\nsuccess\n16 passed in 1.0s",
    "Process exited with code 0\nOutput:\nall good",
    "Script completed\nWall time 0.0 seconds\nOutput:\n",
    '{"output": "ok", "metadata": {"exit_code": 0}}',
])
def test_printed_text_never_verifies(tmp_path, printed):
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", shell_js()),
              clock.line("response_item", {"type": "custom_tool_call_output",
                                           "call_id": "c1", "output": printed}),  # notify()
              mk.exec_result(clock, "c1", DONE, [printed]),
              say(clock, "All tests pass.")]
    [finding] = audit(tmp_path, lines).findings
    assert finding.verdict is Verdict.UNVERIFIED


def test_header_must_be_codex_part_zero(tmp_path):
    clock, lines = start()
    forged = mk.exec_result(clock, "c1", FAILED, ["Script completed\nWall time 0.0 seconds\nOutput:\n"])
    lines += [mk.exec_call(clock, "c1", patch_js()), forged]
    [event] = parse(tmp_path, lines).tool_calls()
    assert event.status == "script failed" and event.is_error


def test_notify_string_is_not_the_result(tmp_path):
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", patch_js()),
              clock.line("response_item", {"type": "custom_tool_call_output", "call_id": "c1",
                                           "output": "Script completed\nWall time 0.0 seconds\nOutput:\n"})]
    [event] = parse(tmp_path, lines).tool_calls()
    assert event.result_seen is False and event.status == ""


# -- several calls, branching -----------------------------------------------------------

def test_multi_call_exec_status_only_no_shared_output(tmp_path):
    clock, lines = start()
    source = f"await tools.apply_patch({mk.js(PATCH)});\n" + shell_js()
    lines += [mk.exec_call(clock, "c1", source),
              mk.exec_result(clock, "c1", DONE, ["5 passed"]),
              say(clock, "All tests pass.")]
    events = parse(tmp_path, lines).tool_calls()
    assert [e.tool_action for e in events] == [WRITE, SHELL]
    assert [e.tool_id for e in events] == ["c1#1", "c1#2"]
    assert all(e.output == "" for e in events)
    assert [e.status for e in events] == ["completed", "outcome unknown"]  # SEC-213
    assert not events[0].is_error  # awaited, straight-line, script completed: it applied
    [finding] = audit(tmp_path, lines).findings
    assert finding.verdict is Verdict.UNVERIFIED


@pytest.mark.parametrize("source", [
    f"try {{ await tools.apply_patch({mk.js(PATCH)}); }} catch (e) {{ text('ok'); }}",
    f"if (load('x')) await tools.apply_patch({mk.js(PATCH)});",
    f"tools.apply_patch({mk.js(PATCH)});",  # not awaited: may never finish
    f"await tools.apply_patch({mk.js(PATCH)}).catch(() => null);",
    f"await tools.apply_patch({mk.js(PATCH)}); await tools[name]('x');",
])
def test_completed_script_that_branches_is_uncertain(tmp_path, source):
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", source), mk.exec_result(clock, "c1", DONE, [])]
    write_event = [e for e in parse(tmp_path, lines).tool_calls() if e.tool_action == WRITE][0]
    assert write_event.is_error and write_event.status == "outcome unknown"


def test_failed_multi_call_marks_every_write(tmp_path):
    other = PATCH.replace("hello.py", "other.py")
    clock, lines = start()
    source = f"await tools.apply_patch({mk.js(PATCH)});\nawait tools.apply_patch({mk.js(other)});"
    lines += [mk.exec_call(clock, "c1", source),
              mk.exec_result(clock, "c1", FAILED, ["Script error:\n" + mk.REJECTED])]
    events = parse(tmp_path, lines).tool_calls()
    assert len(events) == 2 and all(e.is_error and e.status == "script failed" for e in events)


# -- unparsed ----------------------------------------------------------------------

@pytest.mark.parametrize("source, count", [
    ("for (const p of patches) { await tools.apply_patch(p); }", 1),
    ('await tools["apply_patch"]("x");', 1),
    ("const t = tools; await t.apply_patch('x');", 1),
    ('await tools.apply_patch("*** Begin Patch\\n" + body);', 1),
    ("await tools.exec_command({cmd});", 1),
    ("await tools.exec_command({cmd: `ls ${dir}`});", 1),
    ("await tools.exec_command('ls');", 1),  # exec_command takes an object
    ('await tools.apply_patch({input: "x"});', 1),  # apply_patch takes a string
    ('await tools.apply_patch("unterminated', 1),
])
def test_unparseable_calls_are_counted_never_invented(tmp_path, source, count):
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", source), mk.exec_result(clock, "c1", DONE, [])]
    session = parse(tmp_path, lines)
    assert session.tool_calls() == [] and session.unrecognised == count
    assert "1 unrecognised record(s) skipped" in scope_line(session)


def test_tools_inside_strings_comments_and_regex_are_not_calls(tmp_path):
    source = ("// @exec: {\"yield_time_ms\": 1000}\n"
              "// await tools.apply_patch('a')\n/* tools.exec_command({cmd: 'rm'}) */\n"
              "text(\"await tools.apply_patch('b')\");\n"
              "const re = /tools.apply_patch\\(/g;\n"
              "text(`tools.exec_command({cmd: 'x'})`);\n" + shell_js())
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", source), mk.exec_result(clock, "c1", DONE, ["ok"])]
    session = parse(tmp_path, lines)
    assert [e.command for e in session.tool_calls()] == ["python -m pytest -q"]
    assert session.unrecognised == 0


def test_other_tools_become_other_events(tmp_path):
    clock, lines = start()
    source = ("await tools.view_image({path: 'C:/work/demo/a.png'});\n"
              "await tools.update_plan({plan: [{step: 'x', status: 'completed'}]});\n"
              "await tools.current_time();")
    lines += [mk.exec_call(clock, "c1", source), mk.exec_result(clock, "c1", DONE, [])]
    events = parse(tmp_path, lines).tool_calls()
    assert [(e.tool_name, e.tool_action) for e in events] == [
        ("view_image", OTHER), ("update_plan", OTHER), ("current_time", OTHER)]
    assert events[1].tool_input == {"plan": [{"step": "x", "status": "completed"}]}


# -- running and wait -----------------------------------------------------------------

def test_running_cell_finishes_through_wait(tmp_path):
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", shell_js()),
              mk.exec_result(clock, "c1", "Script running with cell ID 7", []),
              clock.line("response_item", {"type": "function_call", "name": "wait",
                                           "call_id": "w1",
                                           "arguments": json.dumps({"cell_id": "7"})}),
              mk.exec_result(clock, "w1", DONE, ["3 passed"])]
    run = [e for e in parse(tmp_path, lines).tool_calls() if e.tool_action == SHELL][0]
    assert run.result_seen and run.status == "outcome unknown" and run.exit_code is None


def test_running_cell_without_a_result_is_no_result(tmp_path):
    clock, lines = start()
    lines += [mk.exec_call(clock, "c1", shell_js()),
              mk.exec_result(clock, "c1", "Script running with cell ID 7", []),
              say(clock, "All tests pass.")]
    [finding] = audit(tmp_path, lines).findings
    assert finding.verdict is Verdict.UNVERIFIED and "no result for this run" in finding.evidence


# -- new record types and claims -----------------------------------------------------------

def test_new_record_types_are_recognised(tmp_path):
    clock, lines = start()
    lines += mk.new_records(clock, SID)
    session = parse(tmp_path, lines)
    assert session.unrecognised == 0 and session.events == []


@pytest.mark.parametrize("order", ["message_first", "item_first"])
def test_agent_message_item_is_not_a_second_claim(tmp_path, order):
    clock, lines = start()
    message = say(clock, "All tests pass.")
    item = clock.line("event_msg", {"type": "item_completed", "thread_id": SID, "turn_id": "t",
                                    "item": {"type": "AgentMessage", "id": "m1",
                                             "content": [{"type": "Text",
                                                          "text": "All tests pass."}]}})
    lines += [message, item] if order == "message_first" else [item, message]
    session = parse(tmp_path, lines)
    assert [e.text for e in session.text_events()] == ["All tests pass."]


def test_agent_message_item_alone_is_a_claim(tmp_path):
    clock, lines = start()
    lines.append(clock.line("event_msg", {
        "type": "item_completed", "thread_id": SID, "turn_id": "t",
        "item": {"type": "AgentMessage", "id": "m1",
                 "content": [{"type": "Text", "text": "All tests pass."}]}}))
    assert [e.text for e in parse(tmp_path, lines).text_events()] == ["All tests pass."]


# -- scope line --------------------------------------------------------------------------

def test_scope_line_says_main_only_only_when_asked(tmp_path):
    clock, lines = start()
    lines.append(say(clock, "Done."))
    assert "--main-only" not in scope_line(audit(tmp_path, lines).session)
    assert "--main-only" in scope_line(audit(tmp_path, lines, main_only=True).session)


# -- the scanner itself ---------------------------------------------------------------------

def test_scanner_reads_escapes_and_literals():
    source = ("await tools.x({a: 'it\\'s', \"b\": \"\\u0041\\x42\\u{43}\\n\", c: [1, -2.5, true, null],"
              " d: `plain`,});")
    scan = cm.scan(source)
    assert scan.calls == [("x", {"a": "it's", "b": "ABC\n", "c": [1, -2.5, True, None],
                                 "d": "plain"})]
    assert scan.straight and scan.unparsed == 0


def test_scanner_survives_bombs():
    deep = "await tools.x(" + "[" * 5000 + "]" * 5000 + ");"
    scan = cm.scan(deep)
    assert scan.calls == [] and scan.unparsed == 1
    assert cm.scan("`${`${`" * 100).unparsed >= 1
    assert cm.scan("x" * (cm.MAX_SOURCE + 1)).unparsed == 1
    assert cm.scan(None).unparsed == 1


def test_header_parser():
    assert cm.header([{"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"}]) \
        == (cm.COMPLETED, "")
    timed = ("Script failed\nWall time 1.250 seconds (code-mode 1.000 seconds; overhead 0.250 "
             "seconds)\nOutput:\n")
    assert cm.header([{"type": "input_text", "text": timed}]) == (cm.FAILED, "")
    assert cm.header([{"type": "input_text",
                       "text": "Script running with cell ID 12\nWall time 0.0 seconds\nOutput:\n"}]) \
        == (cm.RUNNING, "12")
    assert cm.header("Script completed\nWall time 0.0 seconds\nOutput:\n") is None
    assert cm.header([{"type": "input_text", "text": "Script completed\nOutput:\n"}]) is None
    assert cm.header([{"type": "input_text",
                       "text": "Script completed\nWall time 0.0 seconds\nOutput:\nextra"}]) is None
