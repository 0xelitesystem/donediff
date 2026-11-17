"""SEC-224..227, 247, 248: running git on an untrusted repository (0.5.0 deep pen test).

Synthetic repositories only. Every hostile payload only appends to a marker file
under tmp_path, and each test proves its setup is live with a plain-git control
where one is cheap to run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from donediff import gitstate

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows executable lookup")

IN_SESSION = "2026-06-10T12:00:00Z"


def _git_env(date: str = IN_SESSION) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid",
               GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date,
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


REAL_GIT = shutil.which("git")


def git(repo: Path, *args: str) -> str:
    """Set up a test repository (never used by the code under test)."""
    done = subprocess.run([REAL_GIT, "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=_git_env())
    return done.stdout.strip()


def make_repo(tmp_path: Path, name: str = "proj") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("start\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "Initial")
    return repo


def _spy_popen(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = subprocess.Popen

    def spy(argv, **kwargs):
        calls.append(list(argv))
        return real(argv, **kwargs)
    monkeypatch.setattr(gitstate.subprocess, "Popen", spy)
    return calls


# -- SEC-224: a git executable inside the folder donediff runs from ------------

def _plant(folder: Path, name: str) -> Path:
    path = folder / name
    path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


@needs_git
def test_git_in_the_current_folder_is_never_run(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    planted = _plant(repo, "git.exe" if os.name == "nt" else "git")
    monkeypatch.chdir(repo)
    # "." on PATH is how POSIX would search the current folder; Windows does it anyway.
    monkeypatch.setenv("PATH", os.pathsep.join([".", "bin", os.environ["PATH"]]))
    calls = _spy_popen(monkeypatch)
    with gitstate.GitRepo(str(repo)) as found:
        assert found.available, found.reason
    assert calls
    for argv in calls:
        assert os.path.isabs(argv[0])
        assert Path(argv[0]).resolve() != planted.resolve()
        assert not Path(argv[0]).resolve().is_relative_to(repo.resolve())


@needs_git
def test_absolute_path_entry_naming_the_project_or_cwd_is_skipped(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    planted = _plant(repo, "git.exe" if os.name == "nt" else "git")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("PATH", os.pathsep.join([str(repo), str(elsewhere),
                                                os.environ["PATH"]]))
    _plant(elsewhere, "git.exe" if os.name == "nt" else "git")
    found = gitstate.find_git((str(repo),))
    assert found and Path(found).resolve() != planted.resolve()
    assert Path(found).parent.resolve() not in (repo.resolve(), elsewhere.resolve())


@windows_only
def test_only_git_exe_is_taken_on_windows(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("git.bat", "git.cmd", "git.com", "git"):
        _plant(scripts, name)
    monkeypatch.setenv("PATH", str(scripts))
    assert gitstate.find_git() == ""


def test_relative_and_network_path_entries_are_skipped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _plant(tmp_path, "git.exe" if os.name == "nt" else "git")
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "\\\\host.invalid\\share\\bin"]))
    assert gitstate.find_git() == ""
