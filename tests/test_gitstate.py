"""Git state checks (0.4.0): committed, pushed, files in the diff, commit ids,
the fallback when git cannot be read, and a repository built to attack us."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
from donediff import gitstate
from donediff.claims import extract_claims
from donediff.cli import main
from donediff.models import ClaimType, Verdict
from donediff.parser import parse_transcript
from donediff.verify import verify_claims

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

IN_SESSION = "2026-06-10T12:00:00Z"  # the conftest records' time
BEFORE = "2026-06-01T09:00:00Z"


def git(repo: Path, *args: str, date: str = IN_SESSION) -> str:
    """Set up a test repository (never used by the code under test)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid",
               GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date,
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    done = subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=env)
    return done.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("start\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "Initial", date=BEFORE)
    return repo


def commit_file(repo: Path, rel: str, message: str, date: str = IN_SESSION) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(message + "\n", encoding="utf-8")
    git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", message, date=date)
    return git(repo, "rev-parse", "HEAD")


def audit(tmp_path: Path, repo: Path, records: list[dict], check_disk: bool = True):
    for record in records:
        if record.get("type") == "assistant":
            record["cwd"] = str(repo)
    session = parse_transcript(write_jsonl(tmp_path / "s.jsonl", records))
    return verify_claims(session, extract_claims(session), check_disk=check_disk)


def one(findings, claim_type):
    found = [f for f in findings if f.claim.type is claim_type]
    assert len(found) == 1, found
    return found[0]


def commit_records(message: str, printed: str = "[main 1234abc] x") -> list[dict]:
    return [assistant_tool("c1", "Bash", {"command": f"git commit -m '{message}'"}),
            tool_result("c1", printed)]


# -- committed ---------------------------------------------------------------

@needs_git
def test_commit_made_in_the_session_verifies(tmp_path):
    repo = make_repo(tmp_path)
    commit_file(repo, "src/cache.py", "Add cache")
    finding = one(audit(tmp_path, repo, commit_records("Add cache")
                        + [assistant_text("I committed the changes.")]),
                  ClaimType.COMMIT_MADE)
    assert finding.verdict is Verdict.VERIFIED
    assert finding.evidence.endswith("[in git]") and "Add cache" in finding.evidence


@needs_git
def test_no_commit_in_the_session_contradicts_a_printed_commit(tmp_path):
    repo = make_repo(tmp_path)  # only the commit from before the session
    finding = one(audit(tmp_path, repo, commit_records("Add cache")
                        + [assistant_text("I committed the changes.")]),
                  ClaimType.COMMIT_MADE)
    assert finding.verdict is Verdict.CONTRADICTED
    assert "no commit was made in this repository during the session" in finding.evidence


@needs_git
def test_commit_with_another_subject_contradicts(tmp_path):
    repo = make_repo(tmp_path)
    commit_file(repo, "src/other.py", "Something else")
    finding = one(audit(tmp_path, repo, commit_records("Add cache")
                        + [assistant_text("I committed the changes.")]),
                  ClaimType.COMMIT_MADE)
    assert finding.verdict is Verdict.CONTRADICTED
    assert "Add cache" in finding.evidence


# -- pushed (local refs only) ------------------------------------------------

@needs_git
def test_pushed_commit_on_a_remote_tracking_branch_verifies(tmp_path):
    repo = make_repo(tmp_path)
    sha = commit_file(repo, "src/cache.py", "Add cache")
    git(repo, "update-ref", "refs/remotes/origin/main", sha)
    finding = one(audit(tmp_path, repo, commit_records("Add cache") + [
        assistant_tool("p1", "Bash", {"command": "git push origin main"}),
        tool_result("p1", "main -> main"),
        assistant_text("Pushed the branch to origin."),
    ]), ClaimType.PUSHED)
    assert finding.verdict is Verdict.VERIFIED
    assert "origin/main" in finding.evidence and "not fetched" in finding.evidence


@needs_git
def test_session_commit_on_no_remote_branch_contradicts(tmp_path):
    repo = make_repo(tmp_path)
    old = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", old)
    commit_file(repo, "src/cache.py", "Add cache")
    finding = one(audit(tmp_path, repo, commit_records("Add cache") + [
        assistant_tool("p1", "Bash", {"command": "git push origin main"}),
        tool_result("p1", "main -> main"),
        assistant_text("Pushed the branch to origin."),
    ]), ClaimType.PUSHED)
    assert finding.verdict is Verdict.CONTRADICTED
    assert "no remote-tracking branch" in finding.evidence


@needs_git
def test_pushed_without_remote_tracking_branches_is_not_checked(tmp_path):
    repo = make_repo(tmp_path)
    commit_file(repo, "src/cache.py", "Add cache")
    finding = one(audit(tmp_path, repo, commit_records("Add cache") + [
        assistant_tool("p1", "Bash", {"command": "git push origin main"}),
        tool_result("p1", "main -> main"),
        assistant_text("Pushed the branch to origin."),
    ]), ClaimType.PUSHED)
    assert finding.verdict is Verdict.VERIFIED  # the transcript's own verdict
    assert "git state not checked (the repository has no remote-tracking branches)" \
        in finding.evidence


# -- files named in claims ---------------------------------------------------

def write_records(repo: Path, rel: str) -> list[dict]:
    return [assistant_tool("w1", "Write", {"file_path": str(repo / rel), "content": "x"}),
            tool_result("w1", "ok")]


@needs_git
def test_created_file_in_a_session_commit_verifies(tmp_path):
    repo = make_repo(tmp_path)
    sha = commit_file(repo, "src/cache.py", "Add cache")
    finding = one(audit(tmp_path, repo, write_records(repo, "src/cache.py")
                        + [assistant_text("I created `src/cache.py`.")]),
                  ClaimType.FILE_CREATED)
    assert finding.verdict is Verdict.VERIFIED
    assert f"in session commit {sha[:7]}" in finding.evidence


@needs_git
def test_bare_file_name_matches_the_committed_path(tmp_path):
    repo = make_repo(tmp_path)
    commit_file(repo, "src/cache.py", "Add cache")
    finding = one(audit(tmp_path, repo, [assistant_text("I created `cache.py`.")]),
                  ClaimType.FILE_CREATED)
    assert finding.verdict is Verdict.VERIFIED  # raised from UNVERIFIED by the commit
    assert "is in session commit" in finding.evidence


@needs_git
def test_edited_file_with_working_tree_change_verifies(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    finding = one(audit(tmp_path, repo, write_records(repo, "README.md")
                        + [assistant_text("Updated `README.md` with the new flags.")]),
                  ClaimType.FILE_EDITED)
    assert finding.verdict is Verdict.VERIFIED
    assert "in the working-tree changes" in finding.evidence


@needs_git
def test_file_the_diff_does_not_touch_contradicts(tmp_path):
    repo = make_repo(tmp_path)  # README.md committed before the session, unchanged
    finding = one(audit(tmp_path, repo, write_records(repo, "README.md")
                        + [assistant_text("Updated `README.md` with the new flags.")]),
                  ClaimType.FILE_EDITED)
    assert finding.verdict is Verdict.CONTRADICTED
    assert "names a file the diff does not touch" in finding.evidence


@needs_git
def test_ignored_file_is_not_judged_by_git(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".gitignore").write_text("out/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "Ignore out", date=BEFORE)
    (repo / "out").mkdir()
    (repo / "out" / "build.log").write_text("x", encoding="utf-8")
    finding = one(audit(tmp_path, repo, write_records(repo, "out/build.log")
                        + [assistant_text("I created `out/build.log`.")]),
                  ClaimType.FILE_CREATED)
    assert finding.verdict is Verdict.VERIFIED
    assert "git state not checked (git ignores this path)" in finding.evidence


@needs_git
def test_path_outside_the_project_is_never_looked_up(tmp_path):
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    finding = one(audit(tmp_path, repo, [
        assistant_tool("w1", "Write", {"file_path": str(outside), "content": "x"}),
        tool_result("w1", "ok"),
        assistant_text("I created `../outside.txt`."),
    ]), ClaimType.FILE_CREATED)
    assert finding.verdict is Verdict.VERIFIED  # the transcript's own verdict
    assert "git state not checked (path is outside the project)" in finding.evidence


def test_relative_refuses_escapes(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    view = gitstate.GitRepo.__new__(gitstate.GitRepo)
    view.project = str(repo.resolve())
    view.toplevel = view.project
    assert view.relative("src/a.py") == "src/a.py"
    assert view.relative("../outside.txt") is None
    assert view.relative(str(tmp_path / "other" / "a.py")) is None
    assert view.relative("\\\\host\\share\\a.py") is None
    assert view.relative("//host/share/a.py") is None


# -- commit ids ----------------------------------------------------------------

@needs_git
def test_commit_id_in_the_repository_verifies(tmp_path):
    repo = make_repo(tmp_path)
    sha = commit_file(repo, "src/cache.py", "Add cache")
    finding = one(audit(tmp_path, repo, [assistant_text(f"Committed as `{sha[:8]}`.")]),
                  ClaimType.COMMIT_ID)
    assert finding.verdict is Verdict.VERIFIED and finding.evidence.endswith("[in git]")


@needs_git
def test_commit_id_not_in_the_repository_contradicts(tmp_path):
    repo = make_repo(tmp_path)
    finding = one(audit(tmp_path, repo, [assistant_text("Committed as `0badc0de1`.")]),
                  ClaimType.COMMIT_ID)
    assert finding.verdict is Verdict.CONTRADICTED
    assert "is not in this repository" in finding.evidence


# -- git not available: keep the transcript verdict and say why -------------

def test_not_a_repository_keeps_the_transcript_verdict(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    finding = one(audit(tmp_path, plain, commit_records("Add cache")
                        + [assistant_text("I committed the changes.")]),
                  ClaimType.COMMIT_MADE)
    assert finding.verdict is Verdict.VERIFIED
    assert finding.evidence.endswith("git state not checked (not a git work tree)")


def test_git_missing_keeps_the_transcript_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(gitstate.shutil, "which", lambda name: None)
    plain = tmp_path / "plain"
    plain.mkdir()
    finding = one(audit(tmp_path, plain, commit_records("Add cache")
                        + [assistant_text("I committed the changes.")]),
                  ClaimType.COMMIT_MADE)
    assert finding.verdict is Verdict.VERIFIED
    assert "git state not checked (git not found)" in finding.evidence


def test_missing_project_directory_is_not_checked(tmp_path):
    finding = one(audit(tmp_path, tmp_path / "gone", commit_records("Add cache")
                        + [assistant_text("I committed the changes.")]),
                  ClaimType.COMMIT_MADE)
    assert "git state not checked (project directory not found)" in finding.evidence


def test_network_project_path_is_never_touched(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("git started for a network path")
    monkeypatch.setattr(gitstate.subprocess, "Popen", refuse)
    session = parse_transcript(write_jsonl(tmp_path / "s.jsonl", [
        dict(assistant_text("I committed the changes."), cwd="\\\\host\\share\\proj")]))
    finding, = verify_claims(session, extract_claims(session), check_disk=True)
    assert "git state not checked (network or device path)" in finding.evidence


def _refuse_popen(monkeypatch):
    """Fail on any git call on the repository; allow only `config --file <f> --no-includes`
    run outside it (the parse of one config file), and return what was run."""
    real, allowed = subprocess.Popen, []

    def refuse(argv, **kwargs):
        if "-C" in argv or "--file" not in argv or "--no-includes" not in argv:
            raise AssertionError("git started for a repository that points at the network")
        assert kwargs.get("cwd") and kwargs["env"].get("GIT_CEILING_DIRECTORIES")
        allowed.append(list(argv))
        return real(argv, **kwargs)
    monkeypatch.setattr(gitstate.subprocess, "Popen", refuse)
    return allowed


@needs_git
@pytest.mark.parametrize("setup", ["gitfile", "alternates", "config_include", "commondir"])
def test_repository_pointing_at_a_network_path_is_never_run(tmp_path, monkeypatch, setup):
    proj = tmp_path / "proj"
    proj.mkdir()
    if setup == "gitfile":
        (proj / ".git").write_text("gitdir: //host.invalid/share/repo.git\n", encoding="utf-8")
    else:
        dotgit = proj / ".git"
        (dotgit / "objects" / "info").mkdir(parents=True)
        if setup == "alternates":
            (dotgit / "objects" / "info" / "alternates").write_text(
                "\\\\host.invalid\\share\\objects\n", encoding="utf-8")
        elif setup == "config_include":
            (dotgit / "config").write_text(
                "[include]\n\tpath = //host.invalid/share/evil.cfg\n", encoding="utf-8")
        else:
            (dotgit / "commondir").write_text("//host.invalid/share/common\n", encoding="utf-8")
    _refuse_popen(monkeypatch)
    finding = one(audit(tmp_path, proj, commit_records("Add cache")
                        + [assistant_text("I committed the changes.")]),
                  ClaimType.COMMIT_MADE)
    assert finding.verdict is Verdict.VERIFIED  # the transcript's own verdict
    assert "git state not checked (" in finding.evidence
    assert "network or device path)" in finding.evidence


# SEC-192: the five ways git spells a network path that a line-by-line text scan misses.
# Each is parsed by git itself (`config --file --no-includes`, outside the repository) or
# walked here; git is never started on the repository.
_HOST = "//h.invalid/s"


@needs_git
@pytest.mark.parametrize("form", ["include_on_header_line", "backslash_continuation",
                                  "joined_quotes", "nested_alternates", "config_worktree"])
def test_network_path_forms_git_reads_are_refused(tmp_path, monkeypatch, form):
    proj = tmp_path / "proj"
    dotgit = proj / ".git"
    (dotgit / "objects" / "info").mkdir(parents=True)
    config = dotgit / "config"
    if form == "include_on_header_line":
        config.write_text(f"[include] path = {_HOST}/x\n", encoding="utf-8")
    elif form == "backslash_continuation":
        config.write_text(f"[core]\n\tworktree = \\\n{_HOST}\n", encoding="utf-8", newline="\n")
    elif form == "joined_quotes":
        config.write_text('[core]\n\tworktree = "/""/h.invalid/s"\n', encoding="utf-8")
    elif form == "nested_alternates":
        (dotgit / "objects" / "info" / "alternates").write_text("../../alt/objects\n",
                                                                 encoding="utf-8")
        alt = proj / "alt" / "objects" / "info"
        alt.mkdir(parents=True)
        (alt / "alternates").write_text(f"{_HOST}/objects\n", encoding="utf-8")
    else:
        config.write_text("[core]\n\trepositoryformatversion = 1\n"
                          "[extensions]\n\tworktreeConfig = true\n", encoding="utf-8")
        (dotgit / "config.worktree").write_text(f"[core]\n\tworktree = {_HOST}\n",
                                                encoding="utf-8")
    allowed = _refuse_popen(monkeypatch)
    repo = gitstate.GitRepo(str(proj))
    assert not repo.available
    assert "network or device path" in repo.reason
    with pytest.raises(gitstate.GitError):
        repo.status()
    for argv in allowed:  # only local config files were parsed
        parsed = Path(argv[argv.index("--file") + 1]).resolve()
        assert parsed.is_relative_to(dotgit.resolve()), parsed


def _git_version() -> tuple[int, ...]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    return tuple(int(n) for n in re.findall(r"\d+", out)[:3])


def _embedded_bare(tmp_path: Path, alternates: str) -> Path:
    """SEC-225 setup: tracked files in a clone shaped like a git directory, config
    claiming a work tree, alternates pointing elsewhere."""
    clone = make_repo(tmp_path)
    sha = commit_file(clone, "src/a.py", "Add a")
    emb = clone / "vendor" / "tool"
    git(tmp_path, "init", "-q", "--bare", str(emb))
    (emb / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")
    (emb / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (emb / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
                                f"\tworktree = {emb.as_posix()}\n", encoding="utf-8")
    (emb / "objects" / "info" / "alternates").write_text(alternates + "\n", encoding="utf-8")
    return emb


@needs_git
@pytest.mark.parametrize("alternates", ["network", "local"])
def test_project_inside_an_embedded_bare_repository_is_refused(tmp_path, monkeypatch,
                                                                alternates):
    """SEC-225: the pre-check looked only for `.git`, while git took the embedded folder
    as a bare repository and read its alternates (over SMB on Windows)."""
    target = ("\\\\host.invalid\\share\\objects" if alternates == "network"
              else (tmp_path / "proj" / ".git" / "objects").as_posix())
    emb = _embedded_bare(tmp_path, target)
    _refuse_popen(monkeypatch)
    found = gitstate.GitRepo(str(emb))
    assert found.reason == "project folder is inside a bare repository"
    finding = one(audit(tmp_path, emb, commit_records("Add a")
                        + [assistant_text("I committed the changes.")]),
                  ClaimType.COMMIT_MADE)
    assert "git state not checked (project folder is inside a bare repository)" \
        in finding.evidence


@needs_git
@pytest.mark.skipif(shutil.which("git") is None or _git_version() < (2, 38),
                    reason="safe.bareRepository needs git 2.38+")
def test_git_itself_refuses_an_implicit_bare_repository(tmp_path, monkeypatch):
    """SEC-225, second layer: with the pre-check out of the way, git is run with
    safe.bareRepository=explicit and does not open the embedded folder."""
    emb = _embedded_bare(tmp_path, (tmp_path / "proj" / ".git" / "objects").as_posix())
    monkeypatch.setattr(gitstate, "_refuse_network_repository", lambda *a: None)
    found = gitstate.GitRepo(str(emb))
    assert found.reason == "not a git work tree"


@needs_git
def test_alternates_loop_is_refused(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    info = proj / ".git" / "objects" / "info"
    info.mkdir(parents=True)
    (info / "alternates").write_text("../../alt/objects\n", encoding="utf-8")
    alt = proj / "alt" / "objects" / "info"
    alt.mkdir(parents=True)
    (alt / "alternates").write_text("../../.git/objects\n", encoding="utf-8")
    _refuse_popen(monkeypatch)
    repo = gitstate.GitRepo(str(proj))
    assert repo.reason == "repository alternates loop"


def test_no_disk_check_never_starts_git(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("git started with --no-disk-check")
    monkeypatch.setattr(gitstate.subprocess, "Popen", refuse)
    finding = one(audit(tmp_path, tmp_path, commit_records("Add cache")
                        + [assistant_text("I committed the changes.")], check_disk=False),
                  ClaimType.COMMIT_MADE)
    assert "git state" not in finding.evidence


@needs_git
def test_git_output_over_the_cap_falls_back(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    commit_file(repo, "src/cache.py", "Add cache")
    monkeypatch.setattr(gitstate, "OUTPUT_CAP", 4)
    finding = one(audit(tmp_path, repo, commit_records("Add cache")
                        + [assistant_text("I committed the changes.")]),
                  ClaimType.COMMIT_MADE)
    assert finding.verdict is Verdict.VERIFIED
    assert "git state not checked (git output too large)" in finding.evidence


@needs_git
def test_git_timeout_falls_back(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setattr(gitstate, "TIMEOUT", 0.000001)
    finding = one(audit(tmp_path, repo, commit_records("Add cache")
                        + [assistant_text("I committed the changes.")]),
                  ClaimType.COMMIT_MADE)
    assert "git state not checked (git timed out)" in finding.evidence


# -- how git is run -------------------------------------------------------------

_READ_ONLY = {"rev-parse", "config", "log", "show", "status", "branch", "cat-file"}


@needs_git
def test_every_git_call_is_hardened_and_read_only(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    sha = commit_file(repo, "src/cache.py", "Add cache")
    git(repo, "update-ref", "refs/remotes/origin/main", sha)
    calls = []
    real = subprocess.Popen

    def spy(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return real(argv, **kwargs)

    monkeypatch.setattr(gitstate.subprocess, "Popen", spy)
    audit(tmp_path, repo, commit_records("Add cache") + write_records(repo, "src/cache.py") + [
        assistant_tool("p1", "Bash", {"command": "git push"}), tool_result("p1", "ok"),
        assistant_text("I created `src/cache.py`."),
        assistant_text("I committed the changes."),
        assistant_text("Pushed the branch to origin."),
        assistant_text(f"Committed as `{sha[:8]}`."),
    ])
    assert calls
    for argv, kwargs in calls:
        assert isinstance(argv, list) and kwargs.get("shell") is False
        if "--file" in argv:  # a config file parsed outside the repository
            assert "-C" not in argv and argv[1] == "--no-pager" and "--no-includes" in argv
            assert kwargs["cwd"] and kwargs["env"]["GIT_CEILING_DIRECTORIES"]
        else:
            assert argv[1:4] == ["-C", str(repo.resolve()), "--no-pager"]
        assert "core.fsmonitor=false" in argv and "diff.external=" in argv
        assert "protocol.allow=never" in argv and "log.showSignature=false" in argv
        assert any(a.startswith("core.hooksPath=") for a in argv)
        command = next(a for a in argv[4:] if not a.startswith("-") and "=" not in a)
        assert command in _READ_ONLY, argv
        if command == "show":
            assert "--no-ext-diff" in argv and "--no-textconv" in argv
        env = kwargs["env"]
        assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["stdin"] is subprocess.DEVNULL


# -- a repository written to run code when read ----------------------------

def _evil_script(tmp_path: Path, marker: Path) -> Path:
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\necho hit >> '{marker.as_posix()}'\ncat\n",
                      encoding="utf-8", newline="\n")
    script.chmod(0o755)
    return script


@needs_git
def test_hostile_repository_never_runs_its_code(tmp_path):
    repo = make_repo(tmp_path)
    sha = commit_file(repo, "src/cache.txt", "Add cache")
    git(repo, "update-ref", "refs/remotes/origin/main", sha)
    marker = tmp_path / "marker.txt"
    run = f"sh '{_evil_script(tmp_path, marker).as_posix()}'"
    hooks = repo / ".git" / "hooks"
    for hook in ("post-index-change", "reference-transaction", "post-checkout",
                 "pre-commit", "fsmonitor-watchman"):
        (hooks / hook).write_text(f"#!/bin/sh\n{run}\n", encoding="utf-8", newline="\n")
        (hooks / hook).chmod(0o755)
    for key, value in (("core.fsmonitor", str(hooks / "fsmonitor-watchman")),
                       ("filter.evil.clean", run), ("filter.evil.smudge", run),
                       ("filter.evil.process", run), ("filter.evil.required", "true"),
                       ("diff.external", run), ("diff.evil.textconv", run),
                       ("diff.evil.command", run), ("log.showSignature", "true"),
                       ("gpg.program", run), ("core.pager", run), ("pager.status", run),
                       ("core.sshCommand", run), ("credential.helper", f"!{run}")):
        git(repo, "config", key, value)
    (repo / ".gitattributes").write_text("*.txt filter=evil diff=evil\n", encoding="utf-8")
    (repo / "src" / "cache.txt").write_text("changed\n", encoding="utf-8")
    index = (repo / ".git" / "index").read_bytes()

    # Control: plain git does run the repository's code.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                   capture_output=True, env=env)
    assert marker.exists(), "the hostile config did not fire under plain git"
    marker.unlink()
    (repo / ".git" / "index").write_bytes(index)

    findings = audit(tmp_path, repo, commit_records("Add cache") + [
        assistant_tool("w1", "Write", {"file_path": str(repo / "src" / "cache.txt"),
                                       "content": "changed"}),
        tool_result("w1", "ok"),
        assistant_tool("p1", "Bash", {"command": "git push"}), tool_result("p1", "ok"),
        assistant_text("I created `src/cache.txt`."),
        assistant_text("I committed the changes."),
        assistant_text("Pushed the branch to origin."),
        assistant_text(f"Committed as `{sha[:8]}`."),
    ])
    assert not marker.exists()
    assert (repo / ".git" / "index").read_bytes() == index  # nothing written
    verdicts = {f.claim.type: f for f in findings}
    for claim_type in (ClaimType.COMMIT_MADE, ClaimType.PUSHED, ClaimType.COMMIT_ID):
        assert verdicts[claim_type].verdict is Verdict.VERIFIED, verdicts[claim_type]
        assert verdicts[claim_type].evidence.endswith("[in git]")
    assert "in session commit" in verdicts[ClaimType.FILE_CREATED].evidence


@needs_git
def test_repository_allowed_ext_transport_never_runs_on_old_git(tmp_path, monkeypatch):
    """SEC-226: a repository's protocol.ext.allow=always beats -c protocol.allow=never.
    Git older than 2.44 ignores GIT_NO_LAZY_FETCH, so a partial-clone lazy fetch ran the
    repository's ext:: command. Old git is simulated by dropping GIT_NO_LAZY_FETCH."""
    marker = tmp_path / "lazy-fetch.txt"
    if " " in marker.as_posix():
        pytest.skip("the ext:: command in this setup cannot quote a path with a space")
    repo = make_repo(tmp_path)
    sha = commit_file(repo, "src/a.py", "Add a")
    tree = git(repo, "rev-parse", "HEAD^{tree}")

    def arm():
        for key, value in (("core.repositoryformatversion", "1"),
                           ("extensions.partialClone", "origin"),
                           ("remote.origin.promisor", "true"),
                           ("protocol.ext.allow", "always"),
                           ("remote.origin.url",
                            f"ext::sh -c echo% lazy>>{marker.as_posix()}% ;false")):
            git(repo, "config", key, value)
        obj = repo / ".git" / "objects" / tree[:2] / tree[2:]
        if obj.exists():
            obj.chmod(0o666)
            obj.unlink()

    arm()
    # Control: git with only protocol.allow=never and no GIT_NO_LAZY_FETCH runs it.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    subprocess.run(["git", "-C", str(repo), "-c", "protocol.allow=never", "show",
                    "--name-only", "--format=", sha], capture_output=True, env=env, timeout=60)
    if not marker.exists():
        pytest.skip("this git does not lazy-fetch through ext:: here")
    marker.unlink()

    real = gitstate._environment

    def old_git_environment():
        env = real()
        env.pop("GIT_NO_LAZY_FETCH", None)
        return env
    monkeypatch.setattr(gitstate, "_environment", old_git_environment)
    with gitstate.GitRepo(str(repo)) as found:
        assert found.available, found.reason
        try:
            found.files_in(gitstate.Commit(sha, 0.0, "Add a"))
        except gitstate.GitError:
            pass  # the tree is gone; what matters is that nothing ran
    assert not marker.exists()


_SESSION_EPOCH = 1781092800  # IN_SESSION as seconds


@needs_git
@pytest.mark.parametrize("separator", ["\u2028", "\x1c", "\x85", "\r"],
                         ids=["u2028", "x1c", "x85", "cr"])
def test_commit_subject_cannot_forge_a_log_record(tmp_path, separator):
    """SEC-227: str.splitlines() also splits on these, so a subject forged a second log
    record whose "sha" went unchecked into `git show ... <sha>`, where git took it as an
    option (--output=<path> writes a file)."""
    repo = make_repo(tmp_path)
    real = commit_file(repo, "src/a.py", "Add a")
    target = tmp_path / "written-by-git.txt"
    message = (f"tidy{separator}--output={target.as_posix()}\x1f{_SESSION_EPOCH}\x1fforged")
    (repo / "z.py").write_text("z = 1\n", encoding="utf-8")
    git(repo, "add", "z.py")
    tree = git(repo, "write-tree")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid",
               GIT_AUTHOR_DATE=IN_SESSION, GIT_COMMITTER_DATE=IN_SESSION,
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    forged = subprocess.run(["git", "-C", str(repo), "commit-tree", tree, "-p", "HEAD",
                             "-F", "-"], input=message.encode("utf-8"), capture_output=True,
                            check=True, env=env).stdout.decode().strip()
    git(repo, "update-ref", "refs/heads/main", forged)
    with gitstate.GitRepo(str(repo)) as found:
        assert found.available, found.reason
        commits = found.commits_between(_SESSION_EPOCH - 60, _SESSION_EPOCH + 60)
        assert sorted(c.sha for c in commits) == sorted([real, forged])
        for commit in commits:
            found.files_in(commit)
    assert not target.exists()


@needs_git
def test_ids_from_outside_never_reach_git_as_options(tmp_path, monkeypatch):
    """SEC-227: a sha that is not hex is never run, and every id that does reach git
    sits after --end-of-options or in an attached --contains=<id>."""
    repo = make_repo(tmp_path)
    sha = commit_file(repo, "src/a.py", "Add a")
    git(repo, "update-ref", "refs/remotes/origin/main", sha)
    calls = []
    real = subprocess.Popen

    def spy(argv, **kwargs):
        calls.append(list(argv))
        return real(argv, **kwargs)

    with gitstate.GitRepo(str(repo)) as found:
        monkeypatch.setattr(gitstate.subprocess, "Popen", spy)
        assert found.files_in(gitstate.Commit("--output=x", 0.0, "forged")) == set()
        assert calls == []
        assert found.files_in(gitstate.Commit(sha, 0.0, "Add a")) == {"src/a.py"}
        assert found.has_commit(sha[:8])
        assert found.remote_branches_containing(sha) == ["origin/main"]
        assert found.remote_refs() == ["origin/main"]
    show, cat_file, contains = calls[0], calls[1], calls[2]
    assert show[-2:] == ["--end-of-options", sha]
    assert cat_file[-2:] == ["--end-of-options", f"{sha[:8]}^{{commit}}"]
    assert contains[-1] == f"--contains={sha}"


@needs_git
def test_filter_name_with_an_equals_sign_is_refused(tmp_path):
    """SEC-191: `-c filter.a=b.clean=` is split by git at the first "=", so the filter
    stays live. The repository is refused before any later git call."""
    repo = make_repo(tmp_path)
    (repo / ".gitattributes").write_text("f.txt filter=a=b\n", encoding="utf-8")
    commit_file(repo, "f.txt", "Add f")
    marker = tmp_path / "marker.txt"
    run = f"sh '{_evil_script(tmp_path, marker).as_posix()}'"
    with open(repo / ".git" / "config", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(f'[filter "a=b"]\n\tclean = {run}\n')
    stat = (repo / "f.txt").stat()  # same content, new time: git must run the filter to compare
    os.utime(repo / "f.txt", (stat.st_atime + 100, stat.st_mtime + 100))
    index = (repo / ".git" / "index").read_bytes()

    # Control: plain git runs the filter, so the setup is real.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                   capture_output=True, env=env)
    assert marker.exists(), "the a=b filter did not fire under plain git"
    marker.unlink()
    (repo / ".git" / "index").write_bytes(index)

    found = gitstate.GitRepo(str(repo))
    assert found.reason == "repository names a filter that cannot be turned off"
    with pytest.raises(gitstate.GitError):
        found.status()
    assert not marker.exists()


@needs_git
def test_filter_overrides_also_travel_in_the_environment(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    git(repo, "config", "filter.evil.clean", "false")
    envs = []
    real = subprocess.Popen

    def spy(argv, **kwargs):
        envs.append(kwargs["env"])
        return real(argv, **kwargs)

    monkeypatch.setattr(gitstate.subprocess, "Popen", spy)
    with gitstate.GitRepo(str(repo)) as found:
        assert found.available, found.reason
        found.status()
    env = envs[-1]
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
             for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["filter.evil.clean"] == "" and pairs["filter.evil.process"] == ""
    assert pairs["filter.evil.required"] == "false"


def _plant_git(folder: Path) -> Path:
    """A fake git (it only exits 1) where a hostile clone could ship one."""
    folder.mkdir(parents=True, exist_ok=True)
    fake = folder / ("git.exe" if os.name == "nt" else "git")
    fake.write_bytes(b"" if os.name == "nt" else b"#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    return fake


@needs_git
def test_git_is_never_taken_from_the_current_folder_or_the_project(tmp_path, monkeypatch):
    """SEC-224: shutil.which("git") searched the current folder first on Windows, so a
    git.exe shipped in a clone ran when donediff was started there. Relative PATH entries,
    the current folder and folders inside the audited project are never searched."""
    repo = make_repo(tmp_path)
    cwd = tmp_path / "cloned"
    planted = [_plant_git(cwd), _plant_git(repo), _plant_git(repo / "bin")]
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", os.pathsep.join(
        [".", "bin", str(cwd), str(repo), str(repo / "bin"), os.environ.get("PATH", "")]))
    with gitstate.GitRepo(str(repo)) as found:
        assert found._git and Path(found._git).resolve() not in {p.resolve() for p in planted}
        assert found.available, found.reason
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(cwd), str(repo / "bin")]))
    assert gitstate.GitRepo(str(repo)).reason == "git not found"


@needs_git
def test_cli_reads_git_with_the_disk_check_on(tmp_path, capsys):
    import json
    repo = make_repo(tmp_path)
    records = commit_records("Add cache") + [assistant_text("I committed the changes.")]
    for record in records:
        if record.get("type") == "assistant":
            record["cwd"] = str(repo)
    path = write_jsonl(tmp_path / "s.jsonl", records)
    assert main(["audit", path, "--json"]) == 0
    claim, = json.loads(capsys.readouterr().out)["claims"]
    assert claim["verdict"] == "contradicted" and claim["evidence_source"] == "git"
