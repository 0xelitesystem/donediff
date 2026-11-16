"""SEC-211 (a Qwen run with no real trailer read an exit code printed in its
output), SEC-212 (a Cline write whose result holds no status read as a
success), SEC-213 (a Codex code-mode nested shell write counted whatever the
script's outcome), and rewound edits marked "(rewound)" in gaming signals."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from donediff import cli
from donediff.gaming import detect_gaming, signals_for_event
from donediff.models import EDIT, EventKind, Event, ClaimType, Source, Verdict
from donediff.readers import CLINE, QWEN
from donediff.readers import qwen as qwen_mod

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
PASS_CLAIM = "All tests pass."
TEST = "python -m pytest tests/ -q"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mq = _load("make_qwen_211", FIXTURES / "qwen" / "make_qwen.py")
mc = _load("make_cline_212", FIXTURES / "cline" / "make_cline.py")


def _finding(result, types):
    found = [f for f in result.findings if f.claim.type in types]
    assert len(found) == 1, [(f.claim.type, f.claim.text) for f in result.findings]
    return found[0]


# -- SEC-211: Qwen shell text -------------------------------------------------

FAKE = "\n".join([f"Command: {TEST}", "Directory: (root)", "Output: 16 passed in 1.0s",
                  "Error: (none)", "Exit Code: 0", "Signal: (none)", "Process Group PGID: 1"])


def _qwen_run(tmp_path, text, status):
    chat = mq.Chat()
    chat.user("Run the tests.")
    chat.assistant(calls=[mq.call("c1", "run_shell_command",
                                  {"command": TEST, "is_background": False})])
    chat.result("c1", "run_shell_command", output=text, status=status)
    chat.assistant(PASS_CLAIM)
    path = mq.write(tmp_path / f"{mq.SID}.jsonl", chat.records)
    run = [e for e in QWEN.parse(path).tool_calls() if e.tool_id == "c1"][0]
    finding = _finding(cli.run_audit(str(path), check_disk=False, agent="qwen"),
                       (ClaimType.TESTS_PASS,))
    return run, finding


def test_cancelled_run_with_a_printed_trailer_is_not_verified(tmp_path):
    run, finding = _qwen_run(tmp_path, FAKE, "cancelled")
    assert run.exit_code is None and run.status == "cancelled"
    assert finding.verdict is not Verdict.VERIFIED
    assert "exit 0" not in finding.evidence


def test_trailer_with_output_after_it_is_not_read(tmp_path):
    """Success status, real trailer missing: the only trailer is followed by
    more output (another "Output: " line), so the command printed it."""
    text = FAKE + "\nOutput: more of the command's output"
    run, finding = _qwen_run(tmp_path, text, "success")
    assert run.exit_code is None and run.status == "success"
    assert finding.verdict is Verdict.UNVERIFIED
    assert "exit 0" not in finding.evidence


def test_success_with_trailer_only_text_has_no_code(tmp_path):
    """A finished-looking result with a trailer but no "Output: " block."""
    text = "\n".join([f"Command: {TEST}", "Directory: (root)", "Exit Code: 0",
                      "Signal: (none)", "Process Group PGID: 1"])
    run, finding = _qwen_run(tmp_path, text, "success")
    assert run.exit_code is None
    assert finding.verdict is Verdict.UNVERIFIED


@pytest.mark.parametrize("code,verdict", ((0, Verdict.VERIFIED), (1, Verdict.CONTRADICTED)))
def test_real_trailer_after_the_last_output_still_reads(tmp_path, code, verdict):
    """The command's output itself holds an "Output: " line and a fake trailer;
    the real trailer after them still gives the code."""
    out = "\n".join(["Output: nested", "Error: (none)", "Exit Code: 0", "Signal: (none)",
                     "Process Group PGID: 1", "16 passed in 1.0s" if code == 0 else "1 failed"])
    run, finding = _qwen_run(tmp_path, mq.shell_text(TEST, out, code), "success")
    assert run.exit_code == code
    assert finding.verdict is verdict


def test_shell_text_rule():
    assert qwen_mod.shell_text(FAKE) == (0, "16 passed in 1.0s")
    assert qwen_mod.shell_text(FAKE + "\nOutput: x")[0] is None
    assert qwen_mod.shell_text(mq.shell_text("c", "Output: a", 2)) == (2, "Output: a")


@pytest.mark.parametrize("command", ("grep -r limiter src", "rg limiter"))
def test_grep_exit_one_is_still_a_success(tmp_path, command):
    chat = mq.Chat()
    chat.user("Look for it.")
    mq.shell(chat, "c1", command, 1, "", structured=False)
    path = mq.write(tmp_path / f"{mq.SID}.jsonl", chat.records)
    run = QWEN.parse(path).tool_calls()[0]
    assert chat.records[-1]["toolCallResult"]["status"] == "success"
    assert run.exit_code == 1 and run.status == "failed"  # unchanged by SEC-211


# -- SEC-212: Cline write with no status --------------------------------------

CLAIM = "I created `limiter.py` with the new limiter."


def _cline_write(tmp_path, tool, content):
    target = (tmp_path / "limiter.py").as_posix()
    doc = mc.Doc()
    doc.user("Add a limiter.")
    if tool == "editor":
        doc.assistant(calls=[mc.call("c1", "editor", {"path": target, "new_text": "x = 1\n"})])
    else:
        doc.assistant(calls=[mc.call("c1", "apply_patch",
                                     f"*** Begin Patch\n*** Add File: {target}\n+x = 1\n"
                                     "*** End Patch\n")])
    doc.results(mc.result("c1", tool, content(target) if callable(content) else content))
    doc.assistant(CLAIM)
    return mc.write(tmp_path / f"{mc.SID}.messages.json", doc.document())


NO_STATUS = {
    "W1-denied": "The user denied this operation.",
    "W2-text-blocks": [{"type": "text", "text": "Tool execution was cancelled"}],
    "W3-empty": "",
}


@pytest.mark.parametrize("disk", (False, True), ids=("no-disk-check", "disk-check"))
@pytest.mark.parametrize("content", list(NO_STATUS.values()), ids=list(NO_STATUS))
@pytest.mark.parametrize("tool", ["editor", "apply_patch"])
def test_write_with_no_status_is_unverified(tmp_path, tool, content, disk):
    path = _cline_write(tmp_path, tool, content)
    write = CLINE.parse(path).tool_calls()[0]
    assert write.result_seen and write.status == "unknown" and not write.is_error
    finding = _finding(cli.run_audit(str(path), check_disk=disk, agent="cline"),
                       (ClaimType.FILE_CREATED, ClaimType.FILE_EDITED))
    assert finding.verdict is Verdict.UNVERIFIED
    assert "has an unknown outcome: the log does not show whether it ran" in finding.evidence


@pytest.mark.parametrize("tool", ["editor", "apply_patch"])
def test_stringified_success_still_verifies(tmp_path, tool):
    path = _cline_write(tmp_path, tool, lambda target: mc.op(f"add:{target}", "File created"))
    write = CLINE.parse(path).tool_calls()[0]
    assert write.status == "success"
    finding = _finding(cli.run_audit(str(path), check_disk=False, agent="cline"),
                       (ClaimType.FILE_CREATED, ClaimType.FILE_EDITED))
    assert finding.verdict is Verdict.VERIFIED


# -- rewound edits in gaming signals ------------------------------------------

def _test_edit(rewound: bool, source: Source | None = None) -> Event:
    return Event(kind=EventKind.TOOL_CALL, index=3, tool_name="edit", action=EDIT,
                 tool_input={"file_path": "tests/test_app.py",
                             "old_string": "assert a\nassert b", "new_string": "assert a"},
                 rewound=rewound, source=source)


def test_rewound_edit_signal_says_rewound_with_the_same_severity():
    plain = signals_for_event(_test_edit(False))
    rewound = signals_for_event(_test_edit(True))
    assert [s.kind for s in plain] == [s.kind for s in rewound] == ["weakened_assertions"]
    assert [s.severity for s in plain] == [s.severity for s in rewound]
    assert not plain[0].description.endswith("(rewound)")
    assert rewound[0].description == plain[0].description + " (rewound)"


def test_delegated_path_gets_the_same_text():
    source = Source(kind="subagent", path="x.jsonl", agent_id="a1")
    signal = signals_for_event(_test_edit(True, source), source.describe())[0]
    assert signal.description.endswith(" (rewound)") and signal.event_index is None


def test_qwen_rewound_test_edit_is_marked(tmp_path):
    chat = mq.Chat()
    chat.user("Make the tests pass.")
    point = chat.last
    mq.edit(chat, "e1", f"{mq.CWD}/tests/test_app.py", "assert a\nassert b", "assert a")
    chat.last = point  # rewound: the next record re-roots on the prompt
    chat.assistant(PASS_CLAIM)
    session = QWEN.parse(mq.write(tmp_path / f"{mq.SID}.jsonl", chat.records))
    signals = detect_gaming(session)
    assert [s.kind for s in signals] == ["weakened_assertions"]
    assert signals[0].description.endswith(" (rewound)")


# -- SEC-213: Codex code mode, a nested shell write -----------------------------

mx = _load("make_codex_213", FIXTURES / "codex" / "make_codex.py")
CM_SID = "codexdemo-codemode-0213"
SAVED = "I saved the report to `out.txt`."


def _code_mode(tmp_path, source, header, claim=SAVED):
    clock = mx.Clock()
    lines = [clock.line("session_meta", {**mx.meta(CM_SID, "paginated"), "cwd": "C:/work/demo"}),
             mx.exec_call(clock, "c1", source),
             mx.exec_result(clock, "c1", header, ["done"]),
             clock.line("response_item", mx.message("assistant", claim))]
    path = mx.write(tmp_path / f"rollout-2026-06-10T12-00-00-{CM_SID}.jsonl", lines)
    return cli.run_audit(str(path), check_disk=False, agent="codex")


def _exec(cmd):
    return (f"const r = await tools.exec_command({{cmd: {mx.js(cmd)}, workdir: \"C:/work/demo\"}});\n"
            "text(r.output);")


@pytest.mark.parametrize("source,header", [
    (_exec("curl -sf -o out.txt https://example.invalid/x"), "Script completed"),  # S1
    (_exec("python gen.py > out.txt"), "Script failed"),  # S2
    (f"if (load('x')) {{ {_exec('python gen.py > out.txt')} }}", "Script completed"),  # S3
    (_exec("python gen.py > out.txt"), "Script terminated"),  # S4
], ids=("S1-completed", "S2-failed", "S3-branching", "S4-terminated"))
def test_nested_shell_write_is_not_verified(tmp_path, source, header):
    finding = _finding(_code_mode(tmp_path, source, header), (ClaimType.PATH_WRITTEN,))
    assert finding.verdict is Verdict.UNVERIFIED


def test_nested_patch_in_a_completed_straight_script_still_verifies(tmp_path):
    target = "C:/work/demo/out.txt"
    patch = f"*** Begin Patch\n*** Add File: {target}\n+x\n*** End Patch"
    finding = _finding(_code_mode(tmp_path, f"text(await tools.apply_patch({mx.js(patch)}));",
                                  "Script completed"), (ClaimType.PATH_WRITTEN,))
    assert finding.verdict is Verdict.VERIFIED
