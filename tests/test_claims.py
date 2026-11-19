import pytest

from donediff.claims import extract_claims
from donediff.models import ClaimType, Event, EventKind, Session, Verdict
from donediff.verify import verify_claims


def _session_with_text(text: str) -> Session:
    return Session(path="x", events=[
        Event(kind=EventKind.TEXT, index=0, text=text),
    ])


def _types(text: str) -> list[ClaimType]:
    return [c.type for c in extract_claims(_session_with_text(text))]


def test_extracts_tests_pass():
    assert ClaimType.TESTS_PASS in _types("All tests pass. Ready for review.")
    assert ClaimType.TESTS_PASS in _types("The test suite passes cleanly.")


def test_negation_is_not_a_claim():
    assert _types("The tests don't pass yet.") == []
    assert _types("3 tests failed after my change.") == []


def test_future_and_conditional_are_not_claims():
    assert _types("Once the tests pass, we can merge.") == []
    assert _types("The tests should pass after this fix.") == []


def test_extracts_file_created_with_path():
    claims = extract_claims(_session_with_text("Created `src/utils/cache.py` for the new layer."))
    assert claims[0].type is ClaimType.FILE_CREATED
    assert claims[0].detail == "src/utils/cache.py"


def test_extracts_commit_and_push():
    assert ClaimType.COMMIT_MADE in _types("I committed the changes.")
    assert ClaimType.PUSHED in _types("Pushed everything to the remote.")


def test_extracts_task_done():
    assert ClaimType.TASK_DONE in _types("The fix is complete.")
    assert ClaimType.TASK_DONE in _types("Fixed the bug in the date parser.")


def test_dedupes_repeats_within_event():
    text = "All tests pass. Yes, all tests pass."
    assert _types(text).count(ClaimType.TESTS_PASS) == 1


# Codex CLI names the files it touched as markdown links. The line below is a
# real Codex CLI 0.169.0 closing line (2026-11-19 trial run), account name
# replaced with "dev".
CODEX_CLOSING = ("Created [hello.py](C:/Users/dev/AppData/Local/Temp/codex-trial2/hello.py). "
                 "Running `python hello.py` worked. It printed `hello` and exited with code 0.")


def _file_claims(text: str):
    return [c for c in extract_claims(_session_with_text(text))
            if c.type in (ClaimType.FILE_CREATED, ClaimType.FILE_EDITED)]


def test_markdown_link_file_claim_reads_the_link_target():
    claims = _file_claims(CODEX_CLOSING)
    assert [(c.type, c.detail) for c in claims] == [
        (ClaimType.FILE_CREATED, "C:/Users/dev/AppData/Local/Temp/codex-trial2/hello.py")]
    edited = _file_claims("Updated [cache.py](src/utils/cache.py) to drop the lock.")
    assert [(c.type, c.detail) for c in edited] == [(ClaimType.FILE_EDITED, "src/utils/cache.py")]
    ticked = _file_claims("Created [`app.py`](app.py).")
    assert [c.detail for c in ticked] == ["app.py"]


def test_markdown_link_that_names_no_local_file_is_no_claim():
    for text in (
        "Created [hello.py](https://example.invalid/hello.py).",   # a URL is never a file
        "Created [hello.py](file:///C:/w/hello.py).",              # nor a file: URL
        "Created [hello.py](mailto:hello.py).",
        "Created [hello.py](other.py).",                           # text and target differ
        "Created [hello.py](src/hello.py#L3).",                    # an anchor: which file is a guess
        "Created [the script](hello.py).",                         # link text names no file
    ):
        assert _file_claims(text) == [], text


def test_markdown_link_obeys_the_disqualifiers():
    for text in (
        "I will create [hello.py](hello.py) next.",
        "If I created [hello.py](hello.py), it is in the folder.",
        "I didn't create [hello.py](hello.py).",
        "Creating [hello.py](hello.py) failed.",
    ):
        assert _file_claims(text) == [], text


def test_markdown_link_path_keeps_its_underscores():
    claims = _file_claims("Created [hello.py](C:/w/_build/hello_.py_/hello.py).")
    assert [c.detail for c in claims] == ["C:/w/_build/hello_.py_/hello.py"]


def test_markdown_link_file_claim_verifies_against_the_write(tmp_path):
    from donediff.verify import verify_claims
    from donediff.models import Verdict
    (tmp_path / "hello.py").write_text('print("hello")\n')
    target = (tmp_path / "hello.py").as_posix()
    session = Session(path="x", cwd=str(tmp_path), events=[
        Event(kind=EventKind.TOOL_CALL, index=0, tool_name="apply_patch", action="write",
              tool_id="c1", tool_input={"file_path": "hello.py", "content": 'print("hello")\n'},
              status="completed", result_seen=True, cwd=str(tmp_path)),
        Event(kind=EventKind.TEXT, index=1, text=f"Created [hello.py]({target})."),
    ])
    claims = extract_claims(session)
    findings = verify_claims(session, claims, check_disk=True)
    assert [f.verdict for f in findings] == [Verdict.VERIFIED]
    (tmp_path / "hello.py").unlink()
    findings = verify_claims(session, claims, check_disk=True)
    assert [f.verdict for f in findings] == [Verdict.CONTRADICTED]


# -- COMMAND_OK: "`X` worked" (CEO ruling (b), 2026-11-19) ------------------------
# Real closing lines from the 2026-11-19 trial runs: Goose 1.59.2 names the
# script it ran; opencode 1.19.5 names no command or script at all.
GOOSE_CLOSING = 'The `hello.py` script ran successfully, and it printed "hello" as expected.'
OPENCODE_CLOSING = 'The Python script ran successfully and printed "hello".'


def _found(text: str):
    return [(c.type, c.detail) for c in extract_claims(_session_with_text(text))]


def test_command_ok_reads_the_named_command():
    assert _found(CODEX_CLOSING) == [
        (ClaimType.FILE_CREATED, "C:/Users/dev/AppData/Local/Temp/codex-trial2/hello.py"),
        (ClaimType.COMMAND_OK, "python hello.py")]
    assert _found("`npm test` exited with code 0.") == [(ClaimType.COMMAND_OK, "npm test")]


def test_command_ok_reads_a_named_script_never_an_unnamed_one():
    assert _found(GOOSE_CLOSING) == [(ClaimType.COMMAND_OK, "hello.py")]
    assert _found(OPENCODE_CLOSING) == []  # no claim of any type


@pytest.mark.parametrize("text", [
    "`python hello.py` should work.",
    "`python hello.py` will run.",
    "If `python hello.py` ran successfully, the file is there.",
    "`python hello.py` didn't work.",
    "`python hello.py` did not run successfully.",
    "Try running `python hello.py`.",
    "Did `python hello.py` work?",
    "`python hello.py` worked?",
    "It printed `hello` and exited with code 0.",
    "`python hello.py` succeeded.",                         # not in the phrase list
    "`python hello.py` works.",                             # present tense
    "`python hello.py` exited with code 0 after a retry.",  # a disqualifier
    "`python hello.py` exited with code 01.",
    "` ` worked.",                                          # names nothing
])
def test_command_ok_needs_the_fixed_past_tense_phrase(text):
    assert ClaimType.COMMAND_OK not in _types(text)


def _shell(command: str, exit_code: int | None = 0, status: str = "completed", **kw) -> Event:
    kw.setdefault("is_error", exit_code not in (None, 0))
    return Event(kind=EventKind.TOOL_CALL, index=0, tool_name="shell", action="shell",
                 tool_input={"command": command}, exit_code=exit_code, status=status,
                 result_seen=kw.pop("result_seen", True), **kw)


def _verdict(runs, claim: str = "Running `python hello.py` worked.", after=()):
    events = [*runs, Event(kind=EventKind.TEXT, index=0, text=claim), *after]
    for index, event in enumerate(events):
        event.index = index
    session = Session(path="x", events=events)
    claims = [c for c in extract_claims(session) if c.type is ClaimType.COMMAND_OK]
    assert len(claims) == 1
    (finding,) = verify_claims(session, claims, check_disk=False)
    return finding


def test_command_ok_exit_0_is_verified():
    finding = _verdict([_shell("python hello.py")])
    assert finding.verdict is Verdict.VERIFIED, finding.evidence


def test_command_ok_named_script_is_verified_by_its_interpreter_run():
    finding = _verdict([_shell("python hello.py")], GOOSE_CLOSING)
    assert finding.verdict is Verdict.VERIFIED, finding.evidence


def test_command_ok_last_failed_run_after_a_pass_is_contradicted():
    finding = _verdict([_shell("python hello.py"), _shell("python hello.py", 1, "failed")])
    assert finding.verdict is Verdict.CONTRADICTED, finding.evidence


def test_command_ok_earlier_failure_then_pass_is_verified():
    finding = _verdict([_shell("python hello.py", 1, "failed"), _shell("python hello.py")])
    assert finding.verdict is Verdict.VERIFIED, finding.evidence


@pytest.mark.parametrize("run,why", [
    (lambda: _shell("python hello.py", None), "status only (completed)"),
    (lambda: _shell("python hello.py", None, "outcome unknown"), "status only (outcome unknown)"),
    (lambda: _shell("python hello.py | tail -5"), "hidden by `| tail`"),
    (lambda: _shell("python hello.py", None, "", output="Process exited with code 0"),
     "no success record"),                                     # an exit code printed only
    (lambda: _shell("python hello.py", failure_hint=True), "output reports a failure"),
    (lambda: _shell("python hello.py", None, "", result_seen=False), "no result"),
])
def test_command_ok_without_a_structured_exit_0_is_unverified(run, why):
    event = run()
    finding = _verdict([event])
    assert finding.verdict is Verdict.UNVERIFIED and why in finding.evidence, finding.evidence
    if event.exit_code is None:
        assert "exit code" not in finding.evidence  # a status is never called one (D087)


@pytest.mark.parametrize("status", ["declined", "skipped", "cancelled"])
def test_command_ok_after_a_call_that_did_not_run_is_unverified(status):
    run = _shell("python hello.py", None, "")
    run.mark_not_run(status)
    finding = _verdict([run])
    assert finding.verdict is Verdict.UNVERIFIED and "did not run" in finding.evidence


@pytest.mark.parametrize("command,claim", [
    ("python hello.pyc", GOOSE_CLOSING),
    ("python ./hello.py", GOOSE_CLOSING),
    ("python hello.py.bak", GOOSE_CLOSING),
    ("python3 hello.py", "Running `python hello.py` worked."),
    ("cat hello.py", GOOSE_CLOSING),                       # names the script, does not run it
    ('echo "python hello.py"', "Running `python hello.py` worked."),
])
def test_command_ok_needs_a_run_of_exactly_that_command(command, claim):
    finding = _verdict([_shell(command)], claim)
    assert finding.verdict is Verdict.UNVERIFIED and "no run of" in finding.evidence


def test_command_ok_a_later_mention_is_not_a_later_run():
    finding = _verdict([_shell("python hello.py", 1, "failed"), _shell("cat hello.py")],
                       GOOSE_CLOSING)
    assert finding.verdict is Verdict.CONTRADICTED, finding.evidence


def test_command_ok_run_after_the_claim_does_not_count():
    finding = _verdict([], after=[_shell("python hello.py")])
    assert finding.verdict is Verdict.UNVERIFIED and "no run of" in finding.evidence


def test_command_ok_rewound_run_never_verifies():
    alone = _verdict([_shell("python hello.py", rewound=True)])
    assert alone.verdict is Verdict.UNVERIFIED and "was rewound" in alone.evidence, alone.evidence
    capped = _verdict([_shell("python hello.py"),
                       _shell("python hello.py", 1, "failed", rewound=True)])
    assert capped.verdict is Verdict.UNVERIFIED and "rewound" in capped.evidence


@pytest.mark.parametrize("command,exit_code,verdict,why", [
    ("cd app && python hello.py", 0, Verdict.VERIFIED, "succeeded (exit 0)"),
    ('bash -c "python hello.py"', 0, Verdict.VERIFIED, "succeeded (exit 0)"),
    ("python hello.py && echo done", 0, Verdict.VERIFIED, "succeeded (exit 0)"),
    ("cd app; python hello.py", 1, Verdict.CONTRADICTED, "most recent run failed (exit 1)"),
    ("python hello.py; echo done", 0, Verdict.UNVERIFIED, "hidden by `; echo`"),
    ("python hello.py && echo a; echo b", 0, Verdict.UNVERIFIED, "hidden by `; echo`"),
    ("true || python hello.py", 0, Verdict.UNVERIFIED, "hidden by `true ||`"),  # ran only if true failed
    ("! python hello.py", 0, Verdict.UNVERIFIED, "status negated"),
    ("python hello.py &", 0, Verdict.UNVERIFIED, "(background)"),
    ('bash -c "python hello.py | tail -5"', 0, Verdict.UNVERIFIED, "hidden by `| tail`"),
    ("python hello.py && git commit -m x", 1, Verdict.UNVERIFIED, "may be what failed"),
    ("cd app && python hello.py", 1, Verdict.UNVERIFIED, "may be what failed"),  # cd may have failed
])
def test_command_ok_reads_only_its_own_exit_code(command, exit_code, verdict, why):
    finding = _verdict([_shell(command, exit_code, "completed" if exit_code == 0 else "failed")])
    assert finding.verdict is verdict and why in finding.evidence, finding.evidence
