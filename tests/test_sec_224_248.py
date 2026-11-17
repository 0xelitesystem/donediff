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


# -- SEC-225: an embedded bare repository (tracked files shaped like a git dir) ----

def _refuse_repo_popen(monkeypatch) -> None:
    """Fail on any git call on the repository (config --file parses outside it are fine)."""
    real = subprocess.Popen

    def refuse(argv, **kwargs):
        if "-C" in argv:
            raise AssertionError("git started on a bare repository inside the project")
        return real(argv, **kwargs)
    monkeypatch.setattr(gitstate.subprocess, "Popen", refuse)


@needs_git
@pytest.mark.parametrize("shape", ["objects", "commondir"])
def test_project_inside_an_embedded_bare_repository_is_refused(tmp_path, monkeypatch, shape):
    clone = make_repo(tmp_path)
    emb = clone / "vendor" / "tool"
    if shape == "objects":
        (emb / "objects" / "info").mkdir(parents=True)
        (emb / "refs" / "heads").mkdir(parents=True)
        (emb / "objects" / "info" / "alternates").write_text(
            "//host.invalid/share/objects\n", encoding="utf-8")
    else:
        emb.mkdir(parents=True)
        (emb / "commondir").write_text("../../.git\n", encoding="utf-8")
    (emb / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (emb / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    inner = emb / "refs"
    inner.mkdir(exist_ok=True)
    _refuse_repo_popen(monkeypatch)
    for project in (emb, inner):  # the bare folder itself and a folder inside it
        found = gitstate.GitRepo(str(project))
        assert found.reason == "project folder is inside a bare repository"
        with pytest.raises(gitstate.GitError):
            found.status()


@needs_git
def test_every_git_call_refuses_implicit_bare_repositories(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    calls = _spy_popen(monkeypatch)
    with gitstate.GitRepo(str(repo)) as found:
        assert found.available, found.reason
        found.status()
    assert calls
    for argv in calls:
        assert "safe.bareRepository=explicit" in argv, argv


# -- SEC-226: a repository's protocol.ext.allow=always and a lazy fetch ----------

def _lazy_fetch_repo(tmp_path: Path, marker: Path) -> tuple[Path, str]:
    """A partial clone whose promisor remote is an ext:: command, with the session
    commit's tree deleted so reading the commit's files needs a lazy fetch."""
    repo = make_repo(tmp_path)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "a.py")
    git(repo, "commit", "-q", "-m", "Add a")
    sha = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    for key, value in (("core.repositoryformatversion", "1"),
                       ("extensions.partialClone", "origin"),
                       ("remote.origin.promisor", "true"),
                       ("protocol.ext.allow", "always"),
                       ("remote.origin.url",
                        f"ext::sh -c echo% lazy>>{marker.as_posix()}% ;false")):
        git(repo, "config", key, value)
    obj = repo / ".git" / "objects" / tree[:2] / tree[2:]
    obj.chmod(0o666)
    obj.unlink()
    return repo, sha


def test_no_protocol_is_allowed_whatever_the_repository_says():
    assert gitstate._environment()["GIT_ALLOW_PROTOCOL"] == ""


@needs_git
def test_lazy_fetch_through_a_repository_allowed_ext_remote_never_runs(tmp_path, monkeypatch):
    marker = tmp_path / "lazy-fetch.txt"
    repo, sha = _lazy_fetch_repo(tmp_path, marker)

    # Control: what git before 2.44 does with protocol.allow=never and no GIT_NO_LAZY_FETCH.
    subprocess.run([REAL_GIT, "-C", str(repo), "--no-pager", "-c", "protocol.allow=never",
                    "show", "--name-only", "--format=", sha],
                   env=_git_env(), capture_output=True, timeout=60)
    if not marker.exists():
        pytest.skip("this git does not lazy-fetch through ext:: (setup not live)")
    marker.unlink()

    # The same environment as git before 2.44: GIT_NO_LAZY_FETCH is not known.
    real_env = gitstate._environment

    def old_git_env():
        env = real_env()
        env.pop("GIT_NO_LAZY_FETCH", None)
        return env
    monkeypatch.setattr(gitstate, "_environment", old_git_env)
    with gitstate.GitRepo(str(repo)) as found:
        assert found.available, found.reason
        commit = gitstate.Commit(sha, 0.0, "Add a")
        with pytest.raises(gitstate.GitError):
            found.files_in(commit)
    assert not marker.exists()
