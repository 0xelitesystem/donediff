import json
from pathlib import Path

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
from donediff.cli import EXIT_NO_SCORE, main, run_audit


def test_audit_json_output(honest_transcript, capsys):
    code = main(["audit", honest_transcript, "--json", "--no-disk-check"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["score"] is not None
    assert payload["counts"]["verified"] >= 1


def test_fail_under_gates_low_scores(liar_transcript, capsys):
    assert main(["audit", liar_transcript, "--no-disk-check",
                 "--fail-under", "80", "--no-color"]) == 1


def test_fail_under_passes_high_scores(honest_transcript, capsys):
    assert main(["audit", honest_transcript, "--no-disk-check",
                 "--fail-under", "80", "--no-color"]) == 0


def test_missing_target_exits_2(capsys):
    assert main(["audit", "does-not-exist-xyz"]) == 2


def test_scores_rank_sessions_sensibly(honest_transcript, liar_transcript,
                                       gamer_transcript):
    honest = run_audit(honest_transcript, check_disk=False)
    liar = run_audit(liar_transcript, check_disk=False)
    gamer = run_audit(gamer_transcript, check_disk=False)
    assert honest.score > liar.score
    assert honest.score > gamer.score
    assert honest.grade == "A"


def test_markdown_report(honest_transcript, tmp_path, capsys):
    out = tmp_path / "report.md"
    assert main(["audit", honest_transcript, "--no-disk-check",
                 "--md", str(out), "--no-color"]) == 0
    content = out.read_text(encoding="utf-8")
    assert "donediff audit" in content
    assert "VERIFIED" in content


def test_old_receipts_alias_is_gone():
    import donediff.cli as cli
    assert not hasattr(cli, "deprecated_main")
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    scripts = pyproject.read_text(encoding="utf-8").split("[project.scripts]", 1)[1]
    scripts = scripts.split("[", 1)[0]
    assert "donediff = " in scripts and "receipts" not in scripts


def _no_claims_transcript(tmp_path):
    records = [assistant_text("I'll take a look at the parser."),
               assistant_tool("t1", "Bash", {"command": "ls"}),
               tool_result("t1", "src tests")]
    return write_jsonl(tmp_path / "quiet.jsonl", records)


def test_no_score_fails_the_gate_with_its_own_code(tmp_path, capsys):
    path = _no_claims_transcript(tmp_path)
    assert main(["audit", path, "--no-disk-check", "--fail-under", "80",
                 "--no-color"]) == EXIT_NO_SCORE == 3
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "no score" in err[0] and "exit 3" in err[0]


def test_no_score_without_gate_still_exits_0(tmp_path, capsys):
    path = _no_claims_transcript(tmp_path)
    assert main(["audit", path, "--no-disk-check", "--no-color"]) == 0
    assert capsys.readouterr().err == ""


def test_help_names_the_score_and_exit_codes(capsys):
    import pytest
    with pytest.raises(SystemExit):
        main(["audit", "--help"])
    out = capsys.readouterr().out
    assert "donediff score" in out and "Receipts" not in out
    assert "3  --fail-under was given and the session has no score" in out


def _fixture_projects(monkeypatch):
    from donediff import parser
    sessions = Path(__file__).resolve().parent / "fixtures" / "sessions"
    monkeypatch.setattr(parser, "projects_dir", lambda: sessions)


def test_an_ambiguous_id_prefix_is_refused(monkeypatch, capsys):
    """QA-024: `audit s1` matched s1-sync, s10-turn, s11-live and s12-pedit and
    silently audited one of them; it now names them and exits 2."""
    _fixture_projects(monkeypatch)
    assert main(["audit", "s1", "--agent", "claude-code", "--no-disk-check"]) == 2
    err = capsys.readouterr().err
    assert "s1 matches 4 sessions: " in err
    for sid in ("s1-sync", "s10-turn", "s11-live", "s12-pedi"):  # 8 characters at least
        assert sid in err
    assert main(["audit", "s1-sync", "--agent", "claude-code", "--no-disk-check",
                 "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["transcript"].endswith("s1-sync.jsonl")


def test_list_ids_each_name_one_session(monkeypatch, capsys):
    _fixture_projects(monkeypatch)
    assert main(["list", "--agent", "claude-code", "--limit", "50"]) == 0
    ids = [line.split()[0] for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(ids) == len(set(ids)) == 12
    for sid in ids:
        assert main(["audit", sid, "--agent", "claude-code", "--no-disk-check", "--json"]) == 0
        assert Path(json.loads(capsys.readouterr().out)["transcript"]).stem.startswith(sid)


def test_a_report_that_cannot_be_written_is_one_line_and_exit_2(honest_transcript, tmp_path,
                                                                 capsys):
    """QA-025: --md at a folder or under a missing folder: no traceback, no exit 1."""
    for bad in (tmp_path, tmp_path / "missing" / "r.md"):
        code = main(["audit", honest_transcript, "--no-disk-check", "--md", str(bad),
                     "--fail-under", "0"])
        assert code == 2
        captured = capsys.readouterr()
        assert captured.err.startswith(f"donediff: cannot write {bad}: ")
        assert len(captured.err.strip().splitlines()) == 1
        assert "Traceback" not in captured.err + captured.out


def test_standard_output_that_cannot_be_written_exits_2(honest_transcript, monkeypatch, capsys):
    import builtins
    real_print = builtins.print

    def broken(*args, **kwargs):
        if kwargs.get("file") is None:
            raise BrokenPipeError(32, "Broken pipe")
        real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", broken)
    assert main(["audit", honest_transcript, "--no-disk-check", "--json"]) == 2
    monkeypatch.setattr(builtins, "print", real_print)
    assert "cannot write the report to standard output: Broken pipe" in capsys.readouterr().err


def test_the_version_matches_the_newest_changelog_heading(capsys):
    """QA-026: --version, JSON and pip all said 0.4.0 under a 0.5.0 CHANGELOG."""
    import re
    from donediff import __version__
    root = Path(__file__).resolve().parent.parent
    project = root / "pyproject.toml"
    declared = re.search(r'^version = "([^"]+)"', project.read_text(encoding="utf-8"), re.M)
    heading = re.search(r"^## (\S+)", (root / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
    assert declared.group(1) == __version__ == heading.group(1)
    try:
        main(["--version"])
    except SystemExit:
        pass
    assert capsys.readouterr().out.strip() == f"donediff {__version__}"
