"""0.4.0 claim words: deployed / live, N of N pass, commit ids, report paths,
edited files, and the phrase a finding quotes."""

from __future__ import annotations

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
from donediff.claims import extract_claims
from donediff.models import ClaimType, Event, EventKind, Session, Verdict
from donediff.parser import parse_transcript
from donediff.verify import verify_claims


def _claims(text: str):
    return extract_claims(Session(path="x", events=[
        Event(kind=EventKind.TEXT, index=0, text=text)]))


def _types(text: str) -> list[ClaimType]:
    return [c.type for c in _claims(text)]


def _audit(tmp_path, records, check_disk=False, cwd=None, name="s.jsonl"):
    if cwd is not None:
        for record in records:
            if record.get("type") == "assistant":
                record["cwd"] = cwd
    session = parse_transcript(write_jsonl(tmp_path / name, records))
    return verify_claims(session, extract_claims(session), check_disk=check_disk)


def _one(findings, claim_type):
    found = [f for f in findings if f.claim.type is claim_type]
    assert len(found) == 1, found
    return found[0]


# -- extraction ----------------------------------------------------------------

def test_deploy_phrasings_are_claims():
    for text in ("I deployed the site to Cloudflare Pages.",
                 "Deployed to production.",
                 "The new build has been deployed.",
                 "The docs site is live at the new address.",
                 "It is now live.",
                 "The fix shipped to production."):
        assert ClaimType.DEPLOYED in _types(text), text


def test_deploy_hedges_and_plans_are_not_claims():
    assert _types("Once it is deployed we can test.") == []
    assert _types("It will be live tomorrow.") == []
    assert _types("The site is not live yet.") == []


def test_test_count_phrasings():
    claim, = _claims("12 of 12 tests passed.")
    assert claim.type is ClaimType.TEST_COUNT and claim.detail == "12"
    claim, = _claims("All 40/40 tests pass now.")
    assert claim.type is ClaimType.TEST_COUNT and claim.detail == "40"
    claim, = _claims("That makes 7 of 7 pass.")
    assert claim.type is ClaimType.TEST_COUNT and claim.detail == "7"


def test_partial_count_is_not_an_all_pass_claim():
    assert ClaimType.TEST_COUNT not in _types("38 of 40 tests pass.")


def test_commit_id_phrasings():
    for text, sha in (("Committed as `a1b2c3d`.", "a1b2c3d"),
                      ("The change is in commit 9f8e7d6c5b4a.", "9f8e7d6c5b4a"),
                      ("Commit: 0123abc.", "0123abc")):
        ids = [c.detail for c in _claims(text) if c.type is ClaimType.COMMIT_ID]
        assert ids == [sha], text


def test_hex_word_without_digit_is_not_a_commit_id():
    assert ClaimType.COMMIT_ID not in _types("Committed defaced files.")


def test_report_path_phrasings():
    for text, path in (("The report is at `out/audit.md`.", "out/audit.md"),
                       ("Saved the summary to docs/summary.md.", "docs/summary.md"),
                       ("I wrote `notes/plan.txt`.", "notes/plan.txt"),
                       ("Saved it to C:\\work\\out\\r.json.", "C:\\work\\out\\r.json")):
        paths = [c.detail for c in _claims(text) if c.type is ClaimType.PATH_WRITTEN]
        assert paths == [path], text


def test_edited_file_phrasing():
    claim, = _claims("Updated `README.md` with the new flags.")
    assert claim.type is ClaimType.FILE_EDITED and claim.detail == "README.md"


# -- backlog: a finding quotes the matched phrase ------------------------------

def test_two_claims_in_one_sentence_quote_their_own_phrases():
    claims = _claims("All tests pass and I committed the changes.")
    quotes = {c.type: c.quote for c in claims}
    assert quotes[ClaimType.TESTS_PASS] == "All tests pass"
    assert quotes[ClaimType.COMMIT_MADE] == "committed the changes"
    assert all(c.sentence == "All tests pass and I committed the changes." for c in claims)


def test_one_claim_keeps_the_whole_sentence():
    claim, = _claims("All tests pass, ready for review.")
    assert claim.quote == "All tests pass, ready for review."


# -- verification: deployed --------------------------------------------------

def test_deploy_claim_verified_by_a_deploy_run(tmp_path):
    findings = _audit(tmp_path, [
        assistant_tool("t1", "Bash", {"command": "npx wrangler pages deploy site"}),
        tool_result("t1", "Deployment complete"),
        assistant_text("I deployed the site to Cloudflare Pages."),
    ])
    assert _one(findings, ClaimType.DEPLOYED).verdict is Verdict.VERIFIED


def test_deploy_claim_without_a_deploy_run_is_unverified(tmp_path):
    findings = _audit(tmp_path, [
        assistant_tool("t1", "Bash", {"command": "npm run build"}),
        tool_result("t1", "built in 2s"),
        assistant_text("The site is live."),
    ])
    finding = _one(findings, ClaimType.DEPLOYED)
    assert finding.verdict is Verdict.UNVERIFIED
    assert "no deploy command" in finding.evidence


def test_deploy_claim_after_a_failed_deploy_is_contradicted(tmp_path):
    findings = _audit(tmp_path, [
        assistant_tool("t1", "Bash", {"command": "gh release create v1.0.0 dist/app.zip"}),
        tool_result("t1", "HTTP 422: Validation Failed", is_error=True),
        assistant_text("Shipped to production."),
    ])
    assert _one(findings, ClaimType.DEPLOYED).verdict is Verdict.CONTRADICTED


def test_deploy_command_table():
    from donediff.evidence import check_types
    for command in ("wrangler deploy", "npx vercel --prod", "netlify deploy --prod",
                    "gh release create v1", "python -m twine upload dist/*",
                    "npm publish", "rsync -avz site/ deploy@example.invalid:/var/www",
                    "scp build.zip host.example.invalid:/srv"):
        assert ClaimType.DEPLOYED in check_types(command), command
    for command in ("rsync -av site/ backup/", "scp a.txt C:\\temp", "npm run build"):
        assert ClaimType.DEPLOYED not in check_types(command), command


# -- verification: N of N pass -----------------------------------------------

def test_count_claim_matching_output_is_verified(tmp_path):
    findings = _audit(tmp_path, [
        assistant_tool("t1", "Bash", {"command": "pytest -q"}),
        tool_result("t1", "12 passed in 0.40s"),
        assistant_text("12 of 12 tests passed."),
    ])
    finding = _one(findings, ClaimType.TEST_COUNT)
    assert finding.verdict is Verdict.VERIFIED
    assert "output shows 12 passed" in finding.evidence
    assert not [f for f in findings if f.claim.type is ClaimType.TESTS_PASS]


def test_count_claim_with_a_different_count_is_contradicted(tmp_path):
    findings = _audit(tmp_path, [
        assistant_tool("t1", "Bash", {"command": "npm test"}),
        tool_result("t1", "Tests:       9 passed, 9 total"),
        assistant_text("All 12/12 tests pass."),
    ])
    finding = _one(findings, ClaimType.TEST_COUNT)
    assert finding.verdict is Verdict.CONTRADICTED
    assert "shows 9 passed" in finding.evidence


def test_count_claim_without_a_count_in_output_is_unverified(tmp_path):
    findings = _audit(tmp_path, [
        assistant_tool("t1", "Bash", {"command": "go test ./..."}),
        tool_result("t1", "ok  \texample/pkg\t0.2s"),
        assistant_text("5 of 5 tests pass."),
    ])
    assert _one(findings, ClaimType.TEST_COUNT).verdict is Verdict.UNVERIFIED


# -- verification: commit id from the transcript ------------------------------

def test_commit_id_printed_by_a_commit_command_is_verified(tmp_path):
    findings = _audit(tmp_path, [
        assistant_tool("t1", "Bash", {"command": "git commit -m 'Add cache'"}),
        tool_result("t1", "[main 4e5f6a7] Add cache\n 1 file changed"),
        assistant_text("Committed as `4e5f6a7`."),
    ])
    assert _one(findings, ClaimType.COMMIT_ID).verdict is Verdict.VERIFIED


def test_commit_id_nobody_printed_is_unverified(tmp_path):
    findings = _audit(tmp_path, [
        assistant_tool("t1", "Bash", {"command": "git commit -m 'Add cache'"}),
        tool_result("t1", "[main 4e5f6a7] Add cache"),
        assistant_text("Committed as `1a2b3c4`."),
    ])
    assert _one(findings, ClaimType.COMMIT_ID).verdict is Verdict.UNVERIFIED


# -- verification: report at a path -------------------------------------------

def test_report_path_written_and_on_disk_is_verified(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "audit.md").write_text("ok", encoding="utf-8")
    findings = _audit(tmp_path, [
        assistant_tool("t1", "Write", {"file_path": str(tmp_path / "out" / "audit.md"),
                                       "content": "ok"}),
        tool_result("t1", "ok"),
        assistant_text("The report is at `out/audit.md`."),
    ], check_disk=True, cwd=str(tmp_path))
    finding = _one(findings, ClaimType.PATH_WRITTEN)
    assert finding.verdict is Verdict.VERIFIED and "exists on disk" in finding.evidence


def test_report_path_written_by_shell_redirect_is_verified(tmp_path):
    (tmp_path / "summary.txt").write_text("ok", encoding="utf-8")
    findings = _audit(tmp_path, [
        assistant_tool("t1", "Bash", {"command": "python make_summary.py > summary.txt"}),
        tool_result("t1", ""),
        assistant_text("Saved the summary to summary.txt."),
    ], check_disk=True, cwd=str(tmp_path))
    assert _one(findings, ClaimType.PATH_WRITTEN).verdict is Verdict.VERIFIED


def test_report_path_never_written_is_unverified(tmp_path):
    findings = _audit(tmp_path, [assistant_text("The report is at `out/audit.md`.")])
    assert _one(findings, ClaimType.PATH_WRITTEN).verdict is Verdict.UNVERIFIED


def test_report_path_written_but_missing_is_contradicted(tmp_path):
    findings = _audit(tmp_path, [
        assistant_tool("t1", "Write", {"file_path": str(tmp_path / "gone.md"), "content": ""}),
        tool_result("t1", "ok"),
        assistant_text("I wrote `gone.md`."),
    ], check_disk=True, cwd=str(tmp_path))
    assert _one(findings, ClaimType.PATH_WRITTEN).verdict is Verdict.CONTRADICTED


def test_report_path_written_without_disk_check_is_verified(tmp_path):
    findings = _audit(tmp_path, [
        assistant_tool("t1", "Write", {"file_path": str(tmp_path / "gone.md"), "content": ""}),
        tool_result("t1", "ok"),
        assistant_text("I wrote `gone.md`."),
    ], check_disk=False, cwd=str(tmp_path))
    assert _one(findings, ClaimType.PATH_WRITTEN).verdict is Verdict.VERIFIED


# -- the commit message parser -------------------------------------------------

def test_commit_subjects_from_common_forms():
    from donediff.evidence import commit_subjects
    assert commit_subjects('git commit -m "Add cache layer"') == ["Add cache layer"]
    assert commit_subjects("git add . && git commit -am 'Fix: date parse'") == ["Fix: date parse"]
    heredoc = ("git commit -m \"$(cat <<'EOF'\nAdd retry logic\n\nLonger body.\nEOF\n)\"")
    assert commit_subjects(heredoc) == ["Add retry logic"]
    assert commit_subjects("git commit -F msg.txt") == []
    assert commit_subjects("grep -m 3 x f && git status") == []
