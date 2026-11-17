"""Read a project's git state, read-only, for the checks that need it.

Used only when the disk check is on and the session's project directory
is a git work tree. The repository is not trusted: its config, hooks
and attributes may have been written to run code. So every call:

- is an argv list, never a shell, with `git -C <dir> --no-pager`, and
  git is taken from an absolute PATH entry, never from the current
  folder or the project (`find_git`);
- turns off what config could make a read run a program: fsmonitor,
  hooks (pointed at an empty folder), external diff, signature checks
  (gpg), every clean/smudge/process filter the repository names, and
  every network transport (`protocol.allow=never`, no lazy fetch);
  filter overrides also go through GIT_CONFIG_COUNT, and a filter name
  that cannot be overridden (one with "=") makes the repository refused;
- runs a read-only command: rev-parse, config --get-regexp, log,
  show --name-only, status --porcelain, branch -r, cat-file -e, and
  `config --file <f> --no-includes --list` outside the repository to
  parse its config files before git is run on it;
- has a timeout and a cap on how much output is read;
- runs without a terminal prompt, pager or editor, and with
  GIT_OPTIONAL_LOCKS=0 so `status` does not rewrite the index.

Remote state is read from local refs only. Nothing is ever fetched.
"""

from __future__ import annotations

import os
import re
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
        # An empty allow list beats every protocol.<name>.allow in the repository's
        # config (git 2.6+). protocol.allow=never alone loses to a repository's
        # protocol.ext.allow=always, and git before 2.44 has no GIT_NO_LAZY_FETCH.
        "GIT_ALLOW_PROTOCOL": "",
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


MAX_CONFIG_FILES = 10  # config files read per git directory, includes counted
MAX_ALTERNATE_DEPTH = 5  # git follows nested alternates this deep


def _local_target(path: Path) -> Path:
    """`path`, refused when it, or the link it is, names a network or device path."""
    if is_remote_path(path):
        raise GitError("repository points at a network or device path")
    try:
        if os.path.islink(path) and is_remote_path(os.readlink(path)):
            raise GitError("repository points at a network or device path")
    except OSError:
        pass
    return path


def _remote_value(value: str) -> bool:
    """A config value, or any line or trimmed form of it, is a network or device path."""
    return any(is_remote_path(part) or is_remote_path(part.strip())
               for part in (value, *value.splitlines()))


def _unquote_alternate(line: str) -> str:
    """An alternates entry as git reads it (C-style quotes when it starts with ")."""
    if not line.startswith('"'):
        return line
    body = line[1:-1] if line.endswith('"') and len(line) > 1 else line[1:]
    return re.sub(r"\\([0-7]{1,3}|.)",
                  lambda m: chr(int(m.group(1), 8)) if m.group(1)[0] in "01234567"
                  else {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), body)


def _looks_like_git_dir(folder: Path) -> bool:
    """A folder git would take as a git directory: HEAD plus objects/ or commondir."""
    try:
        return os.path.isfile(folder / "HEAD") and (os.path.isdir(folder / "objects")
                                                   or os.path.isfile(folder / "commondir"))
    except (OSError, ValueError):
        return False


def _refuse_network_repository(project: Path, read_config) -> None:
    """Refuse a repository whose files point git at a network or device path.

    A `.git` file (`gitdir: //host/share`), a worktree's `commondir`, an
    object `alternates` list (and the alternates' own alternates) or any
    value in the repository's config, `config.worktree` and every file
    they include (`include.path`, `core.worktree`) can send git to
    \\\\host\\share, and on Windows touching that sends the user's
    credentials to the host. Config files are parsed by git itself
    (`read_config`: `git config --file <f> --no-includes --list`, run
    outside the repository), so every way git spells a value is seen;
    includes are followed here. Git is not run on the repository on a hit.
    """
    dotgit = None
    for d in (project, *project.parents):
        if os.path.lexists(d / ".git"):
            dotgit = d / ".git"
            break
        # Git also opens a folder that IS a git directory as a bare repository, and a
        # clone can ship one as ordinary tracked files. Its config and alternates are
        # not the ones checked here, so it is refused (git is also run with
        # safe.bareRepository=explicit).
        if _looks_like_git_dir(d):
            raise GitError("project folder is inside a bare repository")
    if dotgit is None:
        return  # git will say it is not a work tree
    gitdirs = [_local_target(dotgit)]
    if dotgit.is_file():
        match = re.match(r"gitdir:\s*(.+)", _read_small(dotgit).strip())
        if not match:
            return
        target = match.group(1).strip()
        if is_remote_path(target):
            raise GitError("network or device path")
        gitdirs = [_local_target(dotgit.parent / _windows_path(target))]
    common = _read_small(_local_target(gitdirs[0] / "commondir")).strip()
    if common:
        if is_remote_path(common):
            raise GitError("network or device path")
        gitdirs.append(_local_target(gitdirs[0] / _windows_path(common)))
    for gitdir in gitdirs:
        _refuse_network_alternates(gitdir / "objects")
        # config.worktree is read whatever extensions.worktreeConfig says: reading costs nothing.
        pending = [gitdir / "config.worktree", gitdir / "config"]
        seen: set[str] = set()
        while pending:
            config = _local_target(pending.pop())
            key = os.path.normcase(os.path.abspath(config))
            if key in seen or not config.is_file():
                continue
            if len(seen) >= MAX_CONFIG_FILES:
                raise GitError("repository config includes too many files")
            seen.add(key)
            for name, value in read_config(config):
                if _remote_value(value):
                    raise GitError("repository points at a network or device path")
                name = name.lower()
                if value and (name == "include.path" or (name.startswith("includeif.")
                                                         and name.endswith(".path"))):
                    target = os.path.expanduser(value) if value.startswith("~") else value
                    pending.append(config.parent / _windows_path(target))


def _refuse_network_alternates(objects: Path) -> None:
    """Walk `objects/info/alternates` the way git does, nested, with a loop guard."""
    queue: list[tuple[Path, tuple[str, ...]]] = [(objects, ())]
    seen: set[str] = set()
    while queue:
        directory, chain = queue.pop()
        key = os.path.normcase(os.path.abspath(directory))
        if key in chain:
            raise GitError("repository alternates loop")
        if key in seen:
            continue
        seen.add(key)
        chain, depth = (*chain, key), len(chain)
        text = _read_small(_local_target(directory / "info" / "alternates"))
        for raw in text.splitlines():
            entry = _unquote_alternate(raw.strip())
            if not entry or entry.startswith("#"):
                continue
            if is_remote_path(entry):
                raise GitError("repository points at a network or device path")
            if depth + 1 > MAX_ALTERNATE_DEPTH:
                raise GitError("repository alternates nest too deep")
            queue.append((_local_target(directory / _windows_path(entry)), chain))


def find_git(avoid: tuple[str, ...] = ()) -> str:
    """The git executable from an absolute PATH entry, or "".

    shutil.which("git") on Windows looks in the current folder before PATH
    (unless NoDefaultCurrentDirectoryInExePath is set), so a git.exe shipped
    in a cloned repository would run when donediff is started from it. Here
    the current folder is never searched: a relative PATH entry (".", "bin"),
    a network one, or one that names the current folder or a folder in
    `avoid` (the project) is skipped. On Windows only git.exe is taken: a
    git.bat or git.cmd would run through cmd.exe.
    """
    skip = set()
    for folder in (os.getcwd(), *avoid):
        try:
            skip.add(os.path.normcase(os.path.realpath(folder)))
        except (OSError, ValueError):
            pass
    name = "git.exe" if os.name == "nt" else "git"
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry) or is_remote_path(entry):
            continue
        try:
            if os.path.normcase(os.path.realpath(entry)) in skip:
                continue
            candidate = os.path.join(entry, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return os.path.abspath(candidate)
        except (OSError, ValueError):
            continue
    return ""


def _hardening_args(hooks: str) -> list[str]:
    """The -c settings every git call carries (filters are added per repository)."""
    return [
        "-c", "core.fsmonitor=false",
        "-c", f"core.hooksPath={hooks}",
        "-c", "diff.external=",
        "-c", "log.showSignature=false",
        "-c", "protocol.allow=never",
        "-c", "safe.bareRepository=explicit",
    ]


def _stop(proc: subprocess.Popen) -> None:
    """Kill and reap, so no git process is left holding a folder we remove."""
    proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _run_git(argv: list[str], env: dict[str, str], cwd: str | None) -> tuple[int, str]:
    """Run one git argv (never a shell) with the timeout and output cap."""
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, shell=False, cwd=cwd,
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
    try:
        if reader.is_alive():
            _stop(proc)
            raise GitError("git timed out")
        data = box.get("data", b"")
        if len(data) > OUTPUT_CAP:
            _stop(proc)
            raise GitError("git output too large")
        try:
            code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _stop(proc)
            raise GitError("git timed out") from None
    finally:
        proc.stdout.close()
    return code, data.decode("utf-8", errors="replace")


def read_config_file(git: str, scratch: str, config: Path) -> list[tuple[str, str]]:
    """(name, value) pairs of one config file, parsed by git, includes not followed.

    `git config --file <f> --no-includes --list -z`, run in `scratch` (an empty
    folder) with repository discovery stopped above it and the same argv and
    environment hardening as every other call, so only this one local file
    is read and no repository is opened.
    """
    env = _environment()
    env["GIT_CEILING_DIRECTORIES"] = str(Path(scratch).parent)
    argv = [git, "--no-pager", *_hardening_args(scratch),
            "config", "--file", str(config), "--no-includes", "--list", "-z"]
    code, out = _run_git(argv, env, scratch)
    if code != 0:
        raise GitError("repository config could not be read")
    pairs = []
    for entry in out.split("\0"):
        if entry:
            name, _, value = entry.partition("\n")
            pairs.append((name, value))
    return pairs


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
        self._git = find_git((self.project,))
        if not self._git:
            raise GitError("git not found")
        self._hooks = tempfile.TemporaryDirectory(prefix="donediff-nohooks-",
                                                  ignore_cleanup_errors=True)
        _refuse_network_repository(Path(self.project), self._read_config_file)
        code, out = self._run(["rev-parse", "--is-inside-work-tree", "--show-toplevel"],
                              check=False)
        lines = out.splitlines()
        if code != 0 or not lines or lines[0].strip() != "true" or len(lines) < 2:
            raise GitError("not a git work tree")
        self.toplevel = str(Path(_windows_path(lines[1].strip())).resolve())
        # Names of every filter the config defines, so each can be turned off.
        code, out = self._run(["config", "-z", "--name-only", "--get-regexp", r"^filter\."],
                              check=False)
        names = set()
        for entry in out.split("\0"):
            first, _, rest = entry.strip("\n").partition(".")
            if first.lower() == "filter" and "." in rest:
                names.add(rest.rsplit(".", 1)[0])
        # git splits `-c key=value` at the first "=", so a filter named "a=b" cannot be
        # turned off with -c, and older git ignores the GIT_CONFIG_COUNT form. Refuse it.
        if any("=" in name or "\n" in name or "\0" in name for name in names):
            raise GitError("repository names a filter that cannot be turned off")
        self._filters = sorted(names)
        code, _ = self._run(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], check=False)
        self._has_head = code == 0

    def _read_config_file(self, config: Path) -> list[tuple[str, str]]:
        if self._hooks is None:
            raise GitError("git could not start")
        return read_config_file(self._git, self._hooks.name, config)

    def _config_args(self) -> list[str]:
        hooks = self._hooks.name if self._hooks is not None else os.devnull
        args = _hardening_args(hooks)
        for name in self._filters:
            for key in ("clean", "smudge", "process"):
                args += ["-c", f"filter.{name}.{key}="]
            args += ["-c", f"filter.{name}.required=false"]
        return args

    def _filter_environment(self) -> dict[str, str]:
        """The filter overrides again as GIT_CONFIG_COUNT/KEY/VALUE (git 2.31+):
        key and value travel apart, so no name can be split into another key."""
        pairs = [(f"filter.{name}.{key}", "") for name in self._filters
                 for key in ("clean", "smudge", "process")]
        pairs += [(f"filter.{name}.required", "false") for name in self._filters]
        env = {"GIT_CONFIG_COUNT": str(len(pairs))} if pairs else {}
        for i, (key, value) in enumerate(pairs):
            env[f"GIT_CONFIG_KEY_{i}"] = key
            env[f"GIT_CONFIG_VALUE_{i}"] = value
        return env

    def _run(self, args: list[str], check: bool = True) -> tuple[int, str]:
        if self.reason or not self._git:
            # A repository refused at open is never run again, by any later call.
            raise GitError(self.reason or "git not found")
        env = _environment()
        env.update(self._filter_environment())
        argv = [self._git, "-C", self.project, "--no-pager", *self._config_args(), *args]
        code, out = _run_git(argv, env, None)
        if check and code != 0:
            raise GitError(f"git {args[0]} failed")
        return code, out

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
