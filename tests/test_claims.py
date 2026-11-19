from donediff.claims import extract_claims
from donediff.models import ClaimType, Event, EventKind, Session


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
