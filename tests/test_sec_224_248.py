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


# -- SEC-227: a commit subject that forges a log record and injects an option ----

_SEPARATORS = {"u2028": "\u2028", "u2029": "\u2029", "x1c": "\x1c", "x1d": "\x1d",
               "x1e": "\x1e", "x85": "\x85", "cr": "\r", "x0b": "\x0b", "x0c": "\x0c",
               "lf": "\n"}
_EPOCH_IN_SESSION = "1781092800"  # 2026-06-10T12:00:00Z


def _commit_raw_message(repo: Path, message: str) -> str:
    """A commit whose message is kept byte for byte (commit-tree, no cleanup)."""
    (repo / "z.py").write_text("z = 1\n", encoding="utf-8")
    git(repo, "add", "z.py")
    tree = git(repo, "write-tree")
    done = subprocess.run([REAL_GIT, "-C", str(repo), "commit-tree", tree, "-p", "HEAD",
                           "-F", "-"], input=message.encode("utf-8"), capture_output=True,
                          check=True, env=_git_env())
    sha = done.stdout.decode().strip()
    git(repo, "update-ref", "refs/heads/main", sha)
    return sha


@needs_git
@pytest.mark.parametrize("sep", sorted(_SEPARATORS))
def test_subject_cannot_forge_a_commit_or_inject_an_option(tmp_path, sep):
    repo = make_repo(tmp_path)
    target = tmp_path / f"written-by-git-{sep}.txt"
    forged = f"--output={target.as_posix()}"
    subject = f"tidy{_SEPARATORS[sep]}{forged}\x1f{_EPOCH_IN_SESSION}\x1fforged"
    sha = _commit_raw_message(repo, subject)
    start, end = 1781092800 - 3600, 1781092800 + 3600
    with gitstate.GitRepo(str(repo)) as found:
        assert found.available, found.reason
        commits = found.commits_between(start, end)
        for commit in commits:
            found.files_in(commit)
        found.files_in(gitstate.Commit(forged, 1781092800.0, "x"))  # never reaches argv
    assert not target.exists()
    real = git(repo, "rev-list", "--all").split()
    assert sha in real and len(real) == 2
    assert sorted(c.sha for c in commits) == sorted(real)  # no forged record
    assert next(c for c in commits if c.sha == sha).subject.startswith("tidy")
    assert all(gitstate._FULL_SHA.fullmatch(c.sha) for c in commits)


@needs_git
def test_files_in_puts_end_of_options_before_the_commit(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    sha = git(repo, "rev-parse", "HEAD")
    calls = _spy_popen(monkeypatch)
    with gitstate.GitRepo(str(repo)) as found:
        assert found.files_in(gitstate.Commit(sha, 0.0, "Initial")) == {"README.md"}
        found.has_commit(sha[:8])
        found.commits_between(0, 2 ** 31)
    shows = [a for a in calls if "show" in a or "cat-file" in a or "log" in a]
    assert len(shows) == 3
    for argv in shows:
        assert argv[-2] == "--end-of-options", argv


@needs_git
def test_remote_ref_name_with_a_line_separator_stays_one_name(tmp_path):
    repo = make_repo(tmp_path)
    sha = git(repo, "rev-parse", "HEAD")
    name = "origin/x\u2028origin/main"
    try:
        git(repo, "update-ref", f"refs/remotes/{name}", sha)
    except subprocess.CalledProcessError:
        pytest.skip("this file system cannot hold the ref name")
    with gitstate.GitRepo(str(repo)) as found:
        assert found.remote_refs() == [name]
        assert found.remote_branches_containing(sha) == [name]


@needs_git
def test_project_folder_name_with_a_line_separator_is_the_toplevel(tmp_path):
    try:
        base = tmp_path / "a\u2028b"
        base.mkdir()
    except OSError:
        pytest.skip("this file system cannot hold the folder name")
    repo = make_repo(base)
    with gitstate.GitRepo(str(repo)) as found:
        assert found.available, found.reason
        assert Path(found.toplevel) == repo.resolve()


# -- SEC-247: confined() reads a link's target before anything follows it --------

from donediff import shell  # noqa: E402

_LINK = "lnk-to-net"
_NET_TARGET = r"\\host.invalid\share" if os.name == "nt" else "/net/host.invalid/share"


def _no_resolve_through_the_link(monkeypatch) -> None:
    real = Path.resolve

    def resolve(self, *args, **kwargs):
        if _LINK in str(self):
            raise AssertionError(f"resolve() followed the link: {self}")
        return real(self, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)


@pytest.mark.parametrize("target", [_NET_TARGET, "//host.invalid/share",
                                    r"\\?\UNC\host.invalid\share"])
def test_link_to_a_network_target_is_refused_before_it_is_followed(tmp_path, monkeypatch,
                                                                    target):
    root = tmp_path / "proj"
    root.mkdir()
    link = root / _LINK
    real_islink, real_readlink = os.path.islink, os.readlink
    monkeypatch.setattr(shell.os.path, "islink",
                        lambda p: str(p) == str(link) or real_islink(p))
    monkeypatch.setattr(shell.os, "readlink",
                        lambda p: target if str(p) == str(link) else real_readlink(p))
    _no_resolve_through_the_link(monkeypatch)
    assert shell.confined(f"{_LINK}/notes.txt", str(root)) is None
    assert shell.confined(str(link / "notes.txt"), str(root)) is None
    assert shell.confined("notes.txt", str(link)) is None  # the root itself
    assert shell.confined("ok.txt", str(root)) == (root / "ok.txt").resolve()


def test_link_chain_ending_at_a_network_target_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    first, second = root / _LINK, root / f"{_LINK}-2"
    targets = {str(first): f"{_LINK}-2", str(second): _NET_TARGET}
    real_islink, real_readlink = os.path.islink, os.readlink
    monkeypatch.setattr(shell.os.path, "islink",
                        lambda p: str(p) in targets or real_islink(p))
    monkeypatch.setattr(shell.os, "readlink",
                        lambda p: targets.get(str(p)) or real_readlink(p))
    _no_resolve_through_the_link(monkeypatch)
    assert shell.confined(f"{_LINK}/notes.txt", str(root)) is None


def test_real_symlink_to_a_network_target_is_never_followed(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    try:
        os.symlink(_NET_TARGET, root / _LINK, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    _no_resolve_through_the_link(monkeypatch)
    assert shell.confined(f"{_LINK}/notes.txt", str(root)) is None


def test_local_symlink_inside_the_project_still_resolves(tmp_path):
    root = tmp_path / "proj"
    (root / "real").mkdir(parents=True)
    try:
        os.symlink(root / "real", root / "alias", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    assert shell.confined("alias/x.txt", str(root)) == (root / "real" / "x.txt").resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX automount folders")
@pytest.mark.parametrize("path", ["/net/host.invalid/x", "/Network/Servers/h/x", "/net"])
def test_posix_automount_paths_are_refused_lexically(tmp_path, monkeypatch, path):
    def resolve(self, *args, **kwargs):
        raise AssertionError("resolve() called for an automount path")
    real = Path.resolve
    monkeypatch.setattr(Path, "resolve",
                        lambda self, *a, **k: resolve(self) if str(self).startswith(("/net", "/Network"))
                        else real(self, *a, **k))
    assert shell.confined(path, str(tmp_path)) is None


# -- SEC-248: config-defined hooks (hook.<name>.command, git 2.50+) -------------

def _config_hook_repo(tmp_path: Path, marker: Path) -> Path:
    repo = make_repo(tmp_path)
    run = f"echo hit >> '{marker.as_posix()}'"
    for key, value in (("hook.ev.command", run), ("hook.ev.event", "post-index-change"),
                       ("hook.Two.Dots.command", run), ("hook.Two.Dots.event", "post-index-change")):
        git(repo, "config", key, value)
    return repo


def _stale_index(repo: Path) -> None:
    """Same content, new time: status must refresh the index (and so write it)."""
    stat = (repo / "README.md").stat()
    os.utime(repo / "README.md", (stat.st_atime + 100, stat.st_mtime + 100))


@needs_git
def test_config_hooks_are_turned_off_by_name(tmp_path, monkeypatch):
    marker = tmp_path / "hook-ran.txt"
    repo = _config_hook_repo(tmp_path, marker)
    calls = _spy_popen(monkeypatch)
    with gitstate.GitRepo(str(repo)) as found:
        assert found.available, found.reason
        found.status()
    status = next(argv for argv in calls if "status" in argv)
    assert "hook.ev.enabled=false" in status
    assert "hook.Two.Dots.enabled=false" in status


@needs_git
def test_config_hook_never_fires_even_when_git_writes_the_index(tmp_path, monkeypatch):
    marker = tmp_path / "hook-ran.txt"
    repo = _config_hook_repo(tmp_path, marker)
    _stale_index(repo)
    index = (repo / ".git" / "index").read_bytes()

    # Control: plain git status writes the index and fires the config hook.
    subprocess.run([REAL_GIT, "-C", str(repo), "status", "--porcelain"],
                   env=_git_env(), capture_output=True, timeout=60)
    if not marker.exists():
        pytest.skip("this git has no config-defined hooks (git < 2.50)")
    marker.unlink()
    (repo / ".git" / "index").write_bytes(index)
    _stale_index(repo)

    # Without GIT_OPTIONAL_LOCKS=0 git writes the index; the hook must still not fire.
    real_env = gitstate._environment

    def writing_env():
        env = real_env()
        env.pop("GIT_OPTIONAL_LOCKS", None)
        return env
    monkeypatch.setattr(gitstate, "_environment", writing_env)
    with gitstate.GitRepo(str(repo)) as found:
        assert found.available, found.reason
        found.status()
    assert not marker.exists()


@needs_git
def test_hook_name_that_cannot_be_turned_off_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    with open(repo / ".git" / "config", "a", encoding="utf-8", newline="\n") as fh:
        fh.write('[hook "a=b"]\n\tcommand = false\n\tevent = post-index-change\n')
    found = gitstate.GitRepo(str(repo))
    assert found.reason == "repository names a hook that cannot be turned off"
