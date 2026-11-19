"""0.5.0 one read (Security, 2026-11-18): rows SEC-250..259 and the leftovers it filed.

Every value here is made up: no real run, no real path, no personal data.
"""

from __future__ import annotations

import json
import re
import time

import pytest

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
from donediff import cli, parser
from donediff.evidence import masking
from donediff.models import Verdict


def audit(target):
    return cli.run_audit(str(target), check_disk=False, agent="claude-code")


# -- SEC-258: the value counter runs in linear time ----------------------------

def _cut_line(quotes_kb: int = 8) -> str:
    """A Claude Code line cut mid-write inside its big string: code-like
    output (two quotes every 37 characters), then 1 MiB of plain text."""
    payload = ('print("value") # ' + "y" * 20) * (quotes_kb * 1024 // 2)
    line = json.dumps({"type": "user", "message": {"content": payload + "z" * (1 << 20)}})
    return line[: len(line) - 100]


def test_value_count_is_linear_on_a_line_cut_mid_string():
    cut = _cut_line()
    assert len(cut) > parser._COUNT_FROM and cut.count('"') > 8000
    for text in (cut, cut.encode()):
        started = time.perf_counter()
        assert not parser.too_many_values(text)
        assert time.perf_counter() - started < 2.0, type(text).__name__


def test_claude_code_line_cut_mid_write_is_skipped_quickly(tmp_path):
    rest = [assistant_tool("t1", "Bash", {"command": "pytest -q"}),
            tool_result("t1", "3 passed in 0.10s"),
            assistant_text("All tests pass.")]
    path = tmp_path / "cut.jsonl"
    path.write_text(_cut_line() + "\n" + "\n".join(json.dumps(r) for r in rest), encoding="utf-8")
    started = time.perf_counter()
    result = audit(path)
    assert time.perf_counter() - started < 5.0
    assert result.session.unrecognised == 1
    assert [f.verdict for f in result.findings] == [Verdict.VERIFIED]


_OLD_STRING = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"')  # the SEC-241 counter: linear on valid JSON


@pytest.mark.parametrize("doc", [
    {"a": [1, 2, {"b": "x,[{\\\"y"}], "c": "q\"[,{\\"},
    [{"k": "\\\\", "v": ",", "w": "\\\"[{\\\\\""}] * 50,
    {"text": "a, b, [c], {d}, \\\"e\\\", é中, \\u0022 ," * 40, "n": [[], {}, [{}], None]},
])
def test_value_count_is_unchanged_on_valid_json(doc, monkeypatch):
    text = json.dumps(doc)
    bare = _OLD_STRING.sub("", text)
    count = bare.count(",") + bare.count("[") + bare.count("{")
    monkeypatch.setattr(parser, "_COUNT_FROM", 0)
    for sample in (text, text.encode()):
        monkeypatch.setattr(parser, "MAX_VALUES", count)
        assert not parser.too_many_values(sample)
        monkeypatch.setattr(parser, "MAX_VALUES", count - 1)
        assert parser.too_many_values(sample)


# -- SEC-250 + SEC-244: when a pass marker may lift a hidden exit code ---------

def _one_run(tmp_path, command, output, claim="All tests pass."):
    return write_jsonl(tmp_path / "s.jsonl", [
        assistant_tool("t1", "Bash", {"command": command}),
        tool_result("t1", output),
        assistant_text(claim),
    ])


_QUIET = "python -m pytest -q >/dev/null 2>&1"


@pytest.mark.parametrize("command, output", [
    (_QUIET + " || echo '5 passed'", "5 passed"),
    (_QUIET + " || echo ok", "ok"),
    (_QUIET + " && echo ok || echo ok", "ok"),
    (_QUIET + "; echo '5 passed in 0.1s'", "5 passed in 0.1s"),
    (_QUIET + "; echo OK", "OK"),
    (_QUIET + " || true; echo '5 passed'", "5 passed"),
    (_QUIET + " || printf '5 passed\\n'", "5 passed"),
    (_QUIET + " & echo '5 passed'", "5 passed"),
    ("! " + _QUIET + "; echo '5 passed'", "5 passed"),
    ("python -m pytest -q; git status --short", "12 passed in 0.2s"),
    ("python -m pytest -q; exit 0", "12 passed in 0.2s"),
    ("! python -m pytest -q", "12 passed in 0.2s"),
])
def test_sec250_a_pass_marker_never_lifts_an_or_semicolon_amp_or_bang_mask(tmp_path, command, output):
    (finding,) = audit(_one_run(tmp_path, command, output)).findings
    assert finding.verdict is Verdict.UNVERIFIED
    assert "a pass marker in the output is no proof" in finding.evidence


@pytest.mark.parametrize("command, output, rewriter", [
    ("python -m pytest -q 2>&1 | grep -o '[0-9]* passed' | tail -1", "5 passed", "grep"),
    ("python -m pytest -q 2>&1 | sed 's/failed/passed/' | head -5", "5 passed in 0.1s", "sed"),
    ("python -m pytest -q 2>&1 | awk '{print \"5 passed\"}' | cat", "5 passed", "awk"),
    ("python -m pytest -q 2>&1 | grep passed | tee o.txt", "5 passed", "grep"),
    ('bash -c "python -m pytest -q 2>&1 | grep passed | tail -1"', "5 passed", "grep"),
])
def test_sec244_every_stage_after_the_check_must_be_a_verbatim_filter(tmp_path, command, output,
                                                                      rewriter):
    (finding,) = audit(_one_run(tmp_path, command, output)).findings
    assert finding.verdict is Verdict.UNVERIFIED
    assert f"a pipe through `{rewriter}`" in finding.evidence


@pytest.mark.parametrize("command", [
    "python -m pytest -q 2>&1 | tee log.txt | tail -3",
    "python -m pytest -q | cat | head -5",
    'bash -c "python -m pytest -q | tail -3"',
    "cd app && python -m pytest -q 2>&1 | tail -3",
])
def test_verbatim_chains_still_verify_on_a_pass_marker(tmp_path, command):
    (finding,) = audit(_one_run(tmp_path, command, "12 passed in 0.2s")).findings
    assert finding.verdict is Verdict.VERIFIED
    assert "exit code hidden by a pipe, output shows a pass" in finding.evidence


def test_a_masked_run_without_a_marker_keeps_its_reason(tmp_path):
    (finding,) = audit(_one_run(tmp_path, _QUIET + " || true", "")).findings
    assert finding.verdict is Verdict.UNVERIFIED
    assert "exit code hidden by `|| true` and output does not show the result" in finding.evidence


# -- SEC-251: traps that exit 0, and shells started through launchers ----------

def _kinds(command: str) -> list[str]:
    return [kind for kind, _, _ in masking(command)]


@pytest.mark.parametrize("command", [
    "trap 'exit 0' EXIT; " + _QUIET,
    'trap "exit 0" 0; ' + _QUIET,
    "trap -- 'echo bye; exit 0' EXIT; " + _QUIET,
    "trap 'exit 0' ERR; " + _QUIET,
    _QUIET + "; trap 'exit 0' EXIT",
    "trap { exit 0 }; python -m pytest -q",
    "trap {exit 0}; python -m pytest -q",
    "trap { Write-Host bye; exit 0 }; python -m pytest -q",
    "trap [System.Exception] { exit 0 }; python -m pytest -q",
    "bash -c \"trap 'exit 0' EXIT; " + _QUIET + "\"",
])
def test_sec251_a_trap_that_exits_zero_swallows_the_failure(tmp_path, command):
    assert "swallowed_failure" in _kinds(command)
    result = audit(_one_run(tmp_path, command, "5 passed"))
    assert result.findings[0].verdict is Verdict.UNVERIFIED
    assert "swallowed_failure" in {s.kind for s in result.gaming_signals}


@pytest.mark.parametrize("command", [
    "trap 'echo cleanup' EXIT; python -m pytest -q",
    "trap 'rc=$?; rm -f t.txt; exit $rc' EXIT; python -m pytest -q",
    "trap 'exit 1' EXIT; python -m pytest -q",
    "trap 'exit 0' INT TERM; python -m pytest -q",
    "trap 'exit' EXIT; python -m pytest -q",
    "trap { Write-Host failed; exit 1 }; python -m pytest -q",
])
def test_sec251_other_traps_are_no_mask(command):
    assert _kinds(command) == []


_SWALLOW = "'" + _QUIET + " || true'"


@pytest.mark.parametrize("command", [
    "timeout 60 bash -c " + _SWALLOW,
    "timeout -s KILL 60 bash -c " + _SWALLOW,
    "gtimeout 60 bash -c " + _SWALLOW,
    "/usr/bin/timeout 60 bash -c " + _SWALLOW,
    "nohup bash -c " + _SWALLOW,
    "stdbuf -oL bash -c " + _SWALLOW,
    "stdbuf -o L bash -c " + _SWALLOW,
    "echo x | xargs sh -c " + _SWALLOW,
    "echo x | xargs -n 1 -I {} sh -c " + _SWALLOW,
])
def test_sec251_masking_reads_a_shell_started_through_a_launcher(command):
    assert "swallowed_failure" in _kinds(command)


def test_sec251_timeout_wrapped_swallow_is_unverified(tmp_path):
    result = audit(_one_run(tmp_path, "timeout 60 bash -c " + _SWALLOW, "5 passed"))
    assert result.findings[0].verdict is Verdict.UNVERIFIED
    assert "swallowed_failure" in {s.kind for s in result.gaming_signals}


def test_named_status_keeps_the_plain_wrapper_form():
    # COMMAND_OK: xargs does not hand on a command's own status, so named_status
    # does not read through launchers (unchanged by SEC-251).
    from donediff.shell import named_status, starts_command
    target = lambda stage: starts_command(stage, ["python", "hello.py"])  # noqa: E731
    assert named_status("echo x | xargs sh -c 'python hello.py'", target) is None
    assert named_status("timeout 60 bash -c 'python hello.py'", target) is None
    assert named_status("bash -c 'python hello.py'", target) == (True, True, "")


# -- SEC-252: an exit literal reads as the shell reads it ----------------------

@pytest.mark.parametrize("code", [str(2 ** 60), str(2 ** 62), "-" + str(2 ** 63), "0" * 30 + str(2 ** 60)])
def test_sec252_a_19_digit_exit_that_is_0_mod_256_swallows_the_failure(tmp_path, code):
    command = _QUIET + " || exit " + code
    assert "swallowed_failure" in _kinds(command)
    result = audit(_one_run(tmp_path, command, ""))
    assert result.findings[0].verdict is Verdict.UNVERIFIED
    assert "swallowed_failure" in {s.kind for s in result.gaming_signals}


@pytest.mark.parametrize("code", [str(2 ** 60 + 1), str(2 ** 63), "-" + str(2 ** 63 + 256),
                                  "9" * 20, "1" + "0" * 5000])
def test_sec252_other_long_exits_still_fail(code):
    # 2^60 + 1 exits 1; 2^63 and beyond are outside bash's range: it refuses the number.
    assert _kinds(_QUIET + " || exit " + code) == []


def test_sec252_exit_literal_rule():
    from donediff.evidence import exit_literal_fails
    assert not exit_literal_fails("0") and not exit_literal_fails("256") and not exit_literal_fails("-256")
    assert not exit_literal_fails(str(2 ** 63 - 256)) and not exit_literal_fails("-" + str(2 ** 63))
    assert exit_literal_fails("1") and exit_literal_fails("-1") and exit_literal_fails(str(2 ** 63 - 1))
    assert exit_literal_fails(str(2 ** 63)) and exit_literal_fails("1" + "0" * 19)


def test_sec252_a_trap_exit_of_2_pow_60_swallows_too():
    assert "swallowed_failure" in _kinds("trap 'exit " + str(2 ** 60) + "' EXIT; " + _QUIET)


# -- SEC-253: commands that run no test, and output that says none ran ---------

@pytest.mark.parametrize("command", [
    "npx vitest list", "pnpm vitest list", "vitest list --json", "cargo nextest list",
    "go test -c ./pkg", "go test -c -o /dev/null ./pkg", "./gradlew test --exclude-task test",
    "gradle test --exclude-task=test", "npm test --if-present", "npm run test --if-present",
])
def test_sec253_forms_that_run_no_test_back_no_test_claim(tmp_path, command):
    from donediff.evidence import check_types
    assert not check_types(command)
    (finding,) = audit(_one_run(tmp_path, command, "a.test.ts > adds")).findings
    assert finding.verdict is Verdict.UNVERIFIED


@pytest.mark.parametrize("command, output", [
    ("mvn test -Dtest=NoSuchTest -Dsurefire.failIfNoSpecifiedTests=false",
     "[INFO] Tests run: 0, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS"),
    ("dotnet test --filter FullyQualifiedName=None",
     "No test matches the given testcase filter `FullyQualifiedName=None` in /p/bin/t.dll"),
    ("python -m pytest -q --lf --lfnf=none",
     "run-last-failure: no previously failed tests, deselecting all items.\n6 deselected in 0.01s"),
    ("python -m pytest -q -k zzz", "6 deselected in 0.01s"),
    ("python -m pytest -k zzz", "==================== 6 deselected in 0.01s ===================="),
])
def test_sec253_output_that_says_no_test_ran_is_unverified(tmp_path, command, output):
    (finding,) = audit(_one_run(tmp_path, command, output)).findings
    assert finding.verdict is Verdict.UNVERIFIED
    assert "says no test ran" in finding.evidence


@pytest.mark.parametrize("command, output", [
    ("npx vitest run", "Test Files  1 passed (1)\n     Tests  3 passed (3)"),
    ("cargo nextest run", "Summary [0.1s] 3 tests run: 3 passed, 0 skipped"),
    ("go test ./pkg", "ok  \tm/pkg\t0.01s"),
    ("mvn test", "[INFO] Tests run: 0, Failures: 0, Errors: 0, Skipped: 0, in a.EmptyTest\n"
                 "[INFO] Tests run: 4, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS"),
    ("python -m pytest -q -k add", "3 passed, 6 deselected in 0.01s"),
    ("pytest -c pytest.ini", "3 passed in 0.01s"),
])
def test_sec253_runs_that_ran_tests_still_verify(tmp_path, command, output):
    (finding,) = audit(_one_run(tmp_path, command, output)).findings
    assert finding.verdict is Verdict.VERIFIED


# -- SEC-231: a Claude Code event keeps only the tool input the engine reads ----

def _padded_calls(tmp_path, n=4):
    """n Bash calls whose input holds ~0.9 MiB of empty objects next to the
    command: under the 1 MiB value-count threshold, so every line is read."""
    pad = "[" + "{}," * 300_000 + "{}]"
    path = tmp_path / "pad.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(n):
            line = json.dumps(assistant_tool(f"t{i}", "Bash", {"command": "pytest -q"}))
            fh.write(line.replace('"command": "pytest -q"', '"command": "pytest -q", "pad": ' + pad) + "\n")
            fh.write(json.dumps(tool_result(f"t{i}", "3 passed")) + "\n")
        fh.write(json.dumps(assistant_text("All tests pass.")) + "\n")
    return path


def test_sec231_padding_in_a_tool_input_is_not_kept(tmp_path):
    import gc
    import tracemalloc
    path = _padded_calls(tmp_path)
    gc.collect()
    tracemalloc.start()
    try:
        session = parser.parse_transcript(path)
        gc.collect()
        kept = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    calls = [e for e in session.events if e.tool_input]
    assert len(calls) == 4 and all(e.tool_input == {"command": "pytest -q"} for e in calls)
    assert kept < 2 * path.stat().st_size, kept  # was ~25x


def test_sec231_kept_input_holds_what_the_engine_reads():
    raw = {"command": "pytest", "file_path": "a.py", "content": "x", "old_string": "a",
           "new_string": "b", "description": "d" * 50, "timeout": 5,
           "edits": [{"old_string": "1", "new_string": "2", "replace_all": True}, {}, "x",
                     {"old_string": "", "new_string": ""}, {"new_string": 3}]}
    assert parser.kept_input(raw) == {
        "command": "pytest", "file_path": "a.py", "content": "x", "old_string": "a",
        "new_string": "b", "edits": [{"old_string": "1", "new_string": "2"}, {"new_string": "3"}]}
    assert parser.kept_input(None) == {} and parser.kept_input(["command"]) == {}
    assert parser.kept_input({"command": 7}) == {"command": "7"}
