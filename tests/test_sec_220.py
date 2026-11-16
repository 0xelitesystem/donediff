"""SEC-220: JSON and Markdown reports show home folders as ~ (--full-paths keeps them).

The user name in a home folder is an identity line once a report is attached to
a CI run or an issue. Synthetic paths only; "alice" is a made-up user.
"""

from __future__ import annotations

import json

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
from donediff.cli import main
from donediff.report import home_as_tilde

B = chr(92)
WIN_HOME = B.join(["C:", "Users", "alice"])
WIN_CWD = WIN_HOME + B + "proj"
COMMAND = "cd /home/alice/proj && python -m pytest -q"


def _session(tmp_path, cwd: str = WIN_CWD) -> str:
    records = [assistant_tool("t1", "Bash", {"command": COMMAND}),
               tool_result("t1", "16 passed in 1.0s"),
               assistant_text("All tests pass.")]
    for record in records:
        record["cwd"] = cwd
    return write_jsonl(tmp_path / "s.jsonl", records)


def test_home_prefixes_become_tilde():
    assert home_as_tilde(WIN_CWD) == "~" + B + "proj"
    assert home_as_tilde("C:/Users/alice/proj") == "~/proj"
    assert home_as_tilde("/home/alice/proj") == "~/proj"
    assert home_as_tilde("/Users/alice/proj") == "~/proj"
    assert home_as_tilde(COMMAND) == "cd ~/proj && python -m pytest -q"


def test_a_path_outside_a_home_is_unchanged():
    for text in ("/work/proj", "/srv/home/alice/x", "D:" + B + "builds" + B + "x", "relative/home/x"):
        assert home_as_tilde(text) == text


def test_the_running_users_home_becomes_tilde_wherever_it_is(monkeypatch):
    monkeypatch.setattr("donediff.report._own_home", lambda: "/data/profiles/alice")
    assert home_as_tilde("/data/profiles/alice/proj") == "~/proj"
    assert home_as_tilde("/data/profiles/alicex/proj") == "/data/profiles/alicex/proj"


def test_json_and_markdown_show_no_user_name(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("donediff.report._own_home", lambda: str(tmp_path))
    target = _session(tmp_path)
    md = tmp_path / "r.md"
    assert main(["audit", target, "--no-disk-check", "--json", "--md", str(md)]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["cwd"] == "~" + B + "proj"
    assert data["transcript"] == "~" + B + "s.jsonl"
    assert set(data) >= {"transcript", "cwd", "scope", "claims"}  # the keys stay
    text = md.read_text(encoding="utf-8")
    assert "- **Project:** `~" + B + "proj`" in text
    for report in (out, text):
        assert "alice" not in report and str(tmp_path) not in report


def test_full_paths_keeps_them(tmp_path, capsys):
    target = _session(tmp_path)
    md = tmp_path / "r.md"
    assert main(["audit", target, "--no-disk-check", "--json", "--md", str(md),
                 "--full-paths"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["cwd"] == WIN_CWD and data["transcript"] == target
    assert WIN_CWD in md.read_text(encoding="utf-8")


def test_the_terminal_report_is_unchanged(tmp_path, capsys):
    assert main(["audit", _session(tmp_path), "--no-disk-check", "--no-color"]) == 0
    assert WIN_CWD in capsys.readouterr().out
