"""Read a project's git state, read-only, for the checks that need it.

Used only when the disk check is on and the session's project directory
is a git work tree. The repository is not trusted: its config, hooks
and attributes may have been written to run code. So every call:

- is an argv list, never a shell, with `git -C <dir> --no-pager`;
- turns off what config could make a read run a program: fsmonitor,
  hooks (pointed at an empty folder), external diff, signature checks
  (gpg), every clean/smudge/process filter the repository names, and
  every network transport (`protocol.allow=never`, no lazy fetch);
- runs a read-only command: rev-parse, config --get-regexp, log,
  show --name-only, status --porcelain, branch -r, cat-file -e;
- has a timeout and a cap on how much output is read;
- runs without a terminal prompt, pager or editor, and with
  GIT_OPTIONAL_LOCKS=0 so `status` does not rewrite the index.

Remote state is read from local refs only. Nothing is ever fetched.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .shell import confined, is_remote_path

TIMEOUT = 10.0  # seconds per git call
OUTPUT_CAP = 4 * 1024 * 1024  # bytes read from one call; more is an error
MAX_COMMITS = 200  # session commits read with show --name-only

_HEX_ID = re.compile(r"[0-9a-fA-F]{7,40}")
_MSYS_DRIVE = re.compile(r"^/([A-Za-z])(?=/|$)")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class GitError(Exception):
    """Git could not be read; the message is the reason shown to the user."""


@dataclass
class Commit:
    sha: str
    time: float  # committer time, seconds since the epoch
    subject: str

    @property
    def short(self) -> str:
        return self.sha[:7]


def _environment() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_EDITOR": ":",
        "LC_ALL": "C",
    })
    return env


def _windows_path(path: str) -> str:
    """/c/Users/x (Git Bash form) as C:/Users/x on Windows; else unchanged."""
    if os.name == "nt":
        return _MSYS_DRIVE.sub(lambda m: m.group(1) + ":", path)
    return path


def _same(a: str, b: str) -> bool:
    return os.path.normcase(a) == os.path.normcase(b)


def _read_small(path: Path) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(65536)
    except OSError:
        return ""


def _refuse_network_repository(project: Path) -> None:
    """Refuse a repository whose files point git at a network or device path.

    A `.git` file (`gitdir: //host/share`), a worktree's `commondir`, an
    object `alternates` list or any value in the repository's config
    (`include.path`, `core.worktree`) can send git to \\\\host\\share, and on
    Windows touching that sends the user's credentials to the host. These
    are read as plain text before git starts; git is not run on a hit.
    """
    dotgit = next((d / ".git" for d in (project, *project.parents)
                   if os.path.lexists(d / ".git")), None)
    if dotgit is None:
        return  # git will say it is not a work tree
    gitdirs = [dotgit]
    if dotgit.is_file():
        match = re.match(r"gitdir:\s*(.+)", _read_small(dotgit).strip())
        if not match:
            return
        target = match.group(1).strip()
        if is_remote_path(target):
            raise GitError("network or device path")
        gitdirs = [dotgit.parent / _windows_path(target)]
    common = _read_small(gitdirs[0] / "commondir").strip()
    if common:
        if is_remote_path(common):
            raise GitError("network or device path")
        gitdirs.append(gitdirs[0] / common)
    for gitdir in gitdirs:
        alternates = _read_small(gitdir / "objects" / "info" / "alternates")
        values = [line.strip() for line in alternates.splitlines()]
        # The config and every local file it includes ([include] / [includeIf] path = ...),
        # followed as plain text, so an included file cannot point git at a network path either.
        pending, seen = [gitdir / "config"], set()
        while pending and len(seen) < 10:
            config = pending.pop()
            key = os.path.normcase(os.path.abspath(config))
            if key in seen:
                continue
            seen.add(key)
            section = ""
            for line in _read_small(config).splitlines():
                stripped = line.strip()
                if stripped.startswith("["):
                    section = stripped[1:].split("]", 1)[0].split(None, 1)[0].strip("\"").lower()
                    continue
                if "=" not in line:
                    continue
                name, value = (part.strip() for part in line.split("=", 1))
                value = value.strip("\"'")
                values.append(value)
                if section in ("include", "includeif") and name.lower() == "path" and value \
                        and not is_remote_path(value) and not value.startswith("~"):
                    pending.append(config.parent / _windows_path(value))
        if pending:
            raise GitError("repository config includes too many files")
        if any(is_remote_path(v) for v in values if v):
            raise GitError("repository points at a network or device path")


class GitRepo:
    """The git state of one project directory, read on demand and cached.

    Never raises from the constructor: when git cannot be read, `reason`
    says why and every check must fall back to the transcript.
    """

    def __init__(self, project_dir: str):
        self.reason = ""
        self.project = ""
        self.toplevel = ""
        self._git = ""
        self._hooks: tempfile.TemporaryDirectory | None = None
        self._filters: list[str] = []
        self._has_head = False
        self._commits: dict[tuple[int, int], list[Commit]] = {}
        self._files: dict[str, set[str]] = {}
        self._status: tuple[set[str], list[str]] | None = None
        self._remote_refs: list[str] | None = None
        try:
            self._open(project_dir)
        except GitError as exc:
            self.reason = str(exc)
            self.close()

    @property
    def available(self) -> bool:
        return not self.reason

    def close(self) -> None:
        if self._hooks is not None:
            self._hooks.cleanup()
            self._hooks = None

    def __enter__(self) -> "GitRepo":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- running git --------------------------------------------------------

    def _open(self, project_dir: str) -> None:
        if not project_dir:
            raise GitError("no project directory in the transcript")
        if is_remote_path(project_dir):
            raise GitError("network or device path")
        path = Path(_windows_path(project_dir))
        if not path.is_dir():
            raise GitError("project directory not found")
        self.project = str(path.resolve())
        _refuse_network_repository(Path(self.project))
        self._git = shutil.which("git") or ""
        if not self._git:
            raise GitError("git not found")
        self._hooks = tempfile.TemporaryDirectory(prefix="donediff-nohooks-")
        code, out = self._run(["rev-parse", "--is-inside-work-tree", "--show-toplevel"],
                              check=False)
        lines = out.splitlines()
        if code != 0 or not lines or lines[0].strip() != "true" or len(lines) < 2:
            raise GitError("not a git work tree")
        self.toplevel = str(Path(_windows_path(lines[1].strip())).resolve())
        # Names of every filter the config defines, so each can be turned off.
        code, out = self._run(["config", "--name-only", "--get-regexp", r"^filter\."],
                              check=False)
        names = set()
        for line in out.splitlines():
            parts = line.strip().split(".")
            if len(parts) >= 3:
                names.add(".".join(parts[1:-1]))
        self._filters = sorted(names)
        code, _ = self._run(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], check=False)
        self._has_head = code == 0

    def _config_args(self) -> list[str]:
        hooks = self._hooks.name if self._hooks is not None else os.devnull
        args = [
            "-c", "core.fsmonitor=false",
            "-c", f"core.hooksPath={hooks}",
            "-c", "diff.external=",
            "-c", "log.showSignature=false",
            "-c", "protocol.allow=never",
        ]
        for name in self._filters:
            for key in ("clean", "smudge", "process"):
                args += ["-c", f"filter.{name}.{key}="]
            args += ["-c", f"filter.{name}.required=false"]
        return args

    def _run(self, args: list[str], check: bool = True) -> tuple[int, str]:
        argv = [self._git, "-C", self.project, "--no-pager", *self._config_args(), *args]
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=_environment(), shell=False,
                creationflags=_NO_WINDOW,
            )
        except OSError:
            raise GitError("git could not start") from None
        deadline = time.monotonic() + TIMEOUT
        box: dict[str, bytes] = {}

        def read() -> None:
            box["data"] = proc.stdout.read(OUTPUT_CAP + 1)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(TIMEOUT)
        if reader.is_alive():
            proc.kill()
            raise GitError("git timed out")
        data = box.get("data", b"")
        if len(data) > OUTPUT_CAP:
            proc.kill()
            raise GitError("git output too large")
        try:
            code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            raise GitError("git timed out") from None
        finally:
            proc.stdout.close()
        if check and code != 0:
            raise GitError(f"git {args[0]} failed")
        return code, data.decode("utf-8", errors="replace")

    # -- what the checks read -----------------------------------------------

    def commits_between(self, start: float, end: float) -> list[Commit]:
        """Commits on local branches and HEAD whose committer time is in
        [start, end], newest first, at most MAX_COMMITS."""
        key = (int(start), int(end) + 1)
        if key not in self._commits:
            found: list[Commit] = []
            if self._has_head:
                _, out = self._run(["log", "--branches", "HEAD", "--no-show-signature",
                                    "--date-order", f"--since=@{key[0]}",
                                    f"--until=@{key[1]}", f"--max-count={MAX_COMMITS}",
                                    "--format=%H%x1f%ct%x1f%s"])
                for line in out.splitlines():
                    parts = line.split("\x1f", 2)
                    if len(parts) == 3 and parts[1].isdigit() and \
                            start <= float(parts[1]) <= end:
                        found.append(Commit(parts[0], float(parts[1]), parts[2]))
            self._commits[key] = found
        return self._commits[key]

    def files_in(self, commit: Commit) -> set[str]:
        """Paths (repository-relative, forward slashes) the commit changed."""
        if commit.sha not in self._files:
            _, out = self._run(["show", "--no-ext-diff", "--no-textconv",
                                "--no-show-signature", "--no-renames",
                                "--diff-merges=first-parent", "--name-only",
                                "--format=", "-z", commit.sha])
            self._files[commit.sha] = {p for p in out.split("\0") if p.strip()}
        return self._files[commit.sha]

    def status(self) -> tuple[set[str], list[str]]:
        """(changed paths, ignored paths) in the working tree and index.

        Ignored folders end in "/". Submodules are not entered.
        """
        if self._status is None:
            _, out = self._run(["status", "--porcelain=v1", "-z", "--untracked-files=all",
                                "--ignored=matching", "--ignore-submodules=all"])
            changed: set[str] = set()
            ignored: list[str] = []
            tokens = out.split("\0")
            i = 0
            while i < len(tokens):
                entry = tokens[i]
                i += 1
                if len(entry) < 4:
                    continue
                code, path = entry[:2], entry[3:]
                if code == "!!":
                    ignored.append(path)
                    continue
                changed.add(path)
                if code[0] in "RC" and i < len(tokens):
                    changed.add(tokens[i])  # the rename's old path
                    i += 1
            self._status = (changed, ignored)
        return self._status

    def remote_refs(self) -> list[str]:
        if self._remote_refs is None:
            _, out = self._run(["branch", "-r", "--no-column", "--format=%(refname:short)"])
            self._remote_refs = [r.strip() for r in out.splitlines() if r.strip()]
        return self._remote_refs

    def remote_branches_containing(self, sha: str) -> list[str]:
        """Remote-tracking branches (local refs, never fetched) holding the commit."""
        if not _HEX_ID.fullmatch(sha):
            return []
        _, out = self._run(["branch", "-r", "--no-column", "--format=%(refname:short)",
                            "--contains", sha])
        return [r.strip() for r in out.splitlines() if r.strip()]

    def has_commit(self, sha: str) -> bool:
        if not _HEX_ID.fullmatch(sha):
            return False
        code, _ = self._run(["cat-file", "-e", f"{sha}^{{commit}}"], check=False)
        return code == 0

    # -- paths ----------------------------------------------------------------

    def relative(self, claimed: str) -> str | None:
        """The claimed path relative to the repository root, or None.

        The path is resolved against the project directory, symlinks
        followed, and refused when it lands outside the project: no
        path named in a transcript can make us look elsewhere.
        """
        resolved = confined(claimed, self.project)
        if resolved is None:
            return None
        try:
            return resolved.relative_to(Path(self.toplevel)).as_posix()
        except ValueError:
            return None

    def where_touched(self, rels: list[str], commits: list[Commit],
                      name: str | None = None) -> tuple[str, str]:
        """How the session touched any of the repository-relative paths.

        With `name` (the claim named a bare file name), a path in the
        project with that file name counts too. Returns ("commit", short
        id), ("working", "") for an uncommitted change, ("ignored", "")
        when git ignores the path, or ("none", "").
        """
        def hit(path: str) -> bool:
            if any(_same(rel, path) for rel in rels):
                return True
            return name is not None and _same(path.rsplit("/", 1)[-1], name) \
                and self._in_project(path)

        for commit in commits:
            if any(hit(p) for p in self.files_in(commit)):
                return "commit", commit.short
        changed, ignored = self.status()
        if any(hit(p) for p in changed):
            return "working", ""
        for path in ignored:
            for rel in rels:
                if _same(rel, path) or (path.endswith("/") and os.path.normcase(rel)
                                        .startswith(os.path.normcase(path))):
                    return "ignored", ""
        return "none", ""

    def _in_project(self, rel: str) -> bool:
        """A repository-relative path lies inside the project directory."""
        try:
            prefix = Path(self.project).relative_to(Path(self.toplevel)).as_posix()
        except ValueError:
            return False
        if prefix in ("", "."):
            return True
        return os.path.normcase(rel).startswith(os.path.normcase(prefix.rstrip("/") + "/"))
