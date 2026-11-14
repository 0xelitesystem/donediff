"""Paths named in a transcript are untrusted: disk and git checks stay inside the project folder,
and nothing that points at a network or device path is ever touched."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
from donediff import gitstate
from donediff.claims import extract_claims
from donediff.models import ClaimType, Verdict
from donediff.parser import parse_transcript
from donediff.shell import confined
from donediff.verify import verify_claims


def test_confined_keeps_a_path_inside(tmp_path):
    (tmp_path / "proj" / "src").mkdir(parents=True)
    assert confined("src/a.py", tmp_path / "proj") == (tmp_path / "proj" / "src" / "a.py").resolve()
    assert confined(tmp_path / "proj" / "src", tmp_path / "proj") == (tmp_path / "proj" / "src").resolve()


def test_confined_refuses_paths_that_leave_the_project(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    root = tmp_path / "proj"
    assert confined("../outside.txt", root) is None
    assert confined(str(tmp_path / "outside.txt"), root) is None
    assert confined("src/../../outside.txt", root) is None


@pytest.mark.parametrize("path", ["\\\\host\\share\\a.txt", "//host/share/a.txt", "\\??\\C:\\a.txt"])
def test_confined_refuses_network_and_device_paths(tmp_path, path):
    assert confined(path, tmp_path) is None
    assert confined("a.txt", path) is None


def test_confined_refuses_a_symlink_that_leads_outside(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (tmp_path / "secret").mkdir()
    try:
        os.symlink(tmp_path / "secret", root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    assert confined("link/anything.txt", root) is None


def audit(tmp_path: Path, project: Path, file_path: str, claim: str):
    records = [assistant_tool("w1", "Write", {"file_path": file_path, "content": "x"}),
               tool_result("w1", "ok"), assistant_text(claim)]
    for record in records:
        if record.get("type") == "assistant":
            record["cwd"] = str(project)
    session = parse_transcript(write_jsonl(tmp_path / "s.jsonl", records))
    found = [f for f in verify_claims(session, extract_claims(session), check_disk=True)
             if f.claim.type is ClaimType.FILE_CREATED]
    assert len(found) == 1, found
    return found[0]


def test_disk_check_inside_the_project_still_works(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "notes.txt").write_text("x", encoding="utf-8")
    finding = audit(tmp_path, project, str(project / "notes.txt"), "I created `notes.txt`.")
    assert finding.verdict is Verdict.VERIFIED
    assert "exists on disk" in finding.evidence


def test_disk_check_does_not_look_outside_the_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "notes.txt").write_text("x", encoding="utf-8")
    finding = audit(tmp_path, project, str(tmp_path / "elsewhere" / "notes.txt"),
                    "I created `notes.txt`.")
    assert "outside the project folder" in finding.evidence
    assert "exists on disk" not in finding.evidence  # the outside file was never looked at


def test_disk_check_never_touches_a_network_path(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    touched = []
    real_exists = Path.exists

    def watch(self, *args, **kwargs):
        if str(self).replace("/", "\\").startswith("\\\\"):
            touched.append(str(self))
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", watch)
    finding = audit(tmp_path, project, "\\\\host\\share\\notes.txt", "I created `notes.txt`.")
    assert touched == []
    assert "network or device path" in finding.evidence


def make_dotgit(project: Path, config: str) -> Path:
    gitdir = project / ".git"
    gitdir.mkdir(parents=True)
    (gitdir / "config").write_text(config, encoding="utf-8")
    return gitdir


def test_network_path_in_an_included_config_is_refused(tmp_path):
    gitdir = make_dotgit(tmp_path / "proj", "[core]\n\tbare = false\n[include]\n\tpath = extra.cfg\n")
    (gitdir / "extra.cfg").write_text("[core]\n\tworktree = //host/share/x\n", encoding="utf-8")
    with pytest.raises(gitstate.GitError, match="network or device path"):
        gitstate._refuse_network_repository(tmp_path / "proj")


def test_network_path_in_an_includeif_chain_is_refused(tmp_path):
    gitdir = make_dotgit(tmp_path / "proj", '[includeIf "gitdir:**"]\n\tpath = one.cfg\n')
    (gitdir / "one.cfg").write_text("[include]\n\tpath = two.cfg\n", encoding="utf-8")
    (gitdir / "two.cfg").write_text("[remote \"o\"]\n\turl = \\\\host\\share\\repo\n", encoding="utf-8")
    with pytest.raises(gitstate.GitError, match="network or device path"):
        gitstate._refuse_network_repository(tmp_path / "proj")


def test_an_include_loop_ends_and_submodule_paths_are_not_followed(tmp_path):
    submodules = "".join(f'[submodule "m{i}"]\n\tpath = lib/m{i}\n' for i in range(15))
    gitdir = make_dotgit(tmp_path / "proj", submodules + "[include]\n\tpath = a.cfg\n")
    (gitdir / "a.cfg").write_text("[include]\n\tpath = b.cfg\n", encoding="utf-8")
    (gitdir / "b.cfg").write_text("[include]\n\tpath = a.cfg\n", encoding="utf-8")
    gitstate._refuse_network_repository(tmp_path / "proj")  # no error: nothing remote, loop ends


def test_too_many_included_files_are_refused(tmp_path):
    gitdir = make_dotgit(tmp_path / "proj", "[include]\n\tpath = c0.cfg\n")
    for i in range(12):
        (gitdir / f"c{i}.cfg").write_text(f"[include]\n\tpath = c{i + 1}.cfg\n", encoding="utf-8")
    with pytest.raises(gitstate.GitError, match="too many files"):
        gitstate._refuse_network_repository(tmp_path / "proj")
