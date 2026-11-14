"""Which commands count as evidence, and how a run's result is read.

A command counts as evidence for a claim type when one of its pipeline
stages starts a matching program: `cd app && npm test` runs the tests,
`grep -rn pytest src` does not. Patterns are matched against the stage
after shell.normalize_stage has dropped env assignments, wrappers and
the directory part of the program path.
"""

from __future__ import annotations

import re

from .models import ClaimType
from .shell import FILTERS, normalize_stage, program, split_statements

COMMAND_EVIDENCE: dict[ClaimType, re.Pattern] = {
    ClaimType.TESTS_PASS: re.compile(
        r"pytest\b|python\s+-m\s+(?:pytest|unittest)\b"
        r"|npm\s+(?:run\s+)?test\b|npm\s+t\b|(?:npx|yarn|pnpm|bun)\s+(?:run\s+)?"
        r"(?:test|jest|vitest|mocha|playwright\s+test)\b"
        r"|node\s+(?:\S+\s+)*?--test\b|deno\s+test\b|bun\s+test\b"
        r"|jest\b|vitest\b|mocha\b|go\s+test\b|cargo\s+(?:test|nextest)\b"
        r"|rspec\b|phpunit\b|dotnet\s+test\b|mvn\s+test\b|tox\b|nox\b"
        r"|gradlew?\s+test\b|mix\s+test\b|swift\s+test\b|ctest\b",
        re.IGNORECASE,
    ),
    ClaimType.BUILD_OK: re.compile(
        r"npm\s+run\s+build\b|(?:npx|yarn|pnpm|bun)\s+(?:run\s+)?build\b"
        r"|node\s+(?:\S*[\\/])?build\.[cm]?[jt]s\b"
        r"|cargo\s+build\b|go\s+build\b|make\b|dotnet\s+build\b"
        r"|mvn\s+(?:package|compile|install)\b|gradlew?\s+(?:build|assemble)\b"
        r"|vite\s+build\b|webpack\b|tsc\b.*(?:-b|--build)"
        r"|docker\s+build\b|python\s+-m\s+build\b",
        re.IGNORECASE,
    ),
    ClaimType.LINT_OK: re.compile(
        r"eslint\b|ruff\s+(?:check|format)\b|flake8\b|pylint\b"
        r"|golangci-lint\b|cargo\s+clippy\b|npm\s+run\s+lint\b"
        r"|(?:npx|yarn|pnpm)\s+(?:run\s+)?(?:lint|eslint)\b|biome\s+(?:check|lint)\b"
        r"|python\s+-m\s+(?:ruff|flake8|pylint)\b",
        re.IGNORECASE,
    ),
    ClaimType.TYPECHECK_OK: re.compile(
        r"mypy\b|pyright\b|tsc\b|npm\s+run\s+type-?check\b"
        r"|(?:npx|yarn|pnpm)\s+(?:run\s+)?(?:type-?check|tsc)\b|ty\s+check\b"
        r"|python\s+-m\s+mypy\b",
        re.IGNORECASE,
    ),
    ClaimType.COMMIT_MADE: re.compile(r"git\s+commit\b", re.IGNORECASE),
    ClaimType.PUSHED: re.compile(r"git\s+push\b", re.IGNORECASE),
    # Deploy-type commands: the whole list, kept short on purpose.
    ClaimType.DEPLOYED: re.compile(
        r"(?:(?:npx|bunx|pnpm\s+dlx|yarn)\s+)?"
        r"(?:wrangler\s+(?:pages\s+)?deploy\b|wrangler\s+publish\b"
        r"|vercel\s+(?:deploy\b|--prod\b)|netlify\s+deploy\b)"
        r"|gh\s+release\s+create\b"
        r"|(?:python\s+-m\s+)?twine\s+upload\b"
        r"|(?:npm|pnpm|yarn)\s+publish\b"
        r"|(?:rsync|scp)\b.*\s['\"]?(?:[^\s@:'\"]+@)?[A-Za-z0-9][\w.-]+:",  # to a host
        re.IGNORECASE,
    ),
}

# Checks whose result says something about the code: tests, builds, lint,
# type checks. Masking their exit code is a gaming signal.
CHECK_TYPES = frozenset({
    ClaimType.TESTS_PASS, ClaimType.BUILD_OK,
    ClaimType.LINT_OK, ClaimType.TYPECHECK_OK,
})

# "N failed" with N > 0, or framework-specific hard failure markers.
# A bare FAILED counts only in capitals (pytest's marker): a passing cargo
# run prints "0 failed", which must not read as a failure.
FAILURE_IN_OUTPUT = re.compile(
    r"\b([1-9]\d*)\s+fail(?:ed|ures?)\b"
    r"|(?-i:\bFAILED\b)|\bTests?\s+failed\b|\bBUILD\s+FAILED\b"
    r"|test result: FAILED"
    r"|^\S*\s*fail\s+[1-9]\d*\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# A positive pass marker: what a passing run prints, framework by
# framework. A run whose exit code a pipe hid backs a claim only when its
# output shows one of these (and no failure marker).
PASS_IN_OUTPUT = re.compile(
    r"\b[1-9]\d*\s+(?:passed|passing)\b"                 # pytest, jest, vitest, mocha
    r"|^\S*\s*fail\s+0\s*$"                              # node --test, TAP: "# fail 0"
    r"|test result: ok\b"                                # cargo test
    r"|^OK(?:\s+\(.*\))?\s*$"                            # unittest, phpunit
    r"|^ok\s+\S+|^PASS\s*$"                              # go test
    r"|\bPassed!|\b\d+ examples?, 0 failures\b"          # dotnet test, rspec
    r"|\bAll checks passed\b|\bSuccess: no issues found\b"  # ruff, mypy
    r"|\bFound 0 errors\b|\b0 errors?, 0 warnings?\b"    # tsc, pyright
    r"|\bBUILD SUCCESS(?:FUL)?\b|\bBuild succeeded\b|\bbuilt in \d"
    r"|\bcompiled successfully\b|\bSuccessfully built\b|^\s*Finished\b",
    re.IGNORECASE | re.MULTILINE,
)

# How many tests a run's output says passed: pytest, jest, vitest, cargo
# ("N passed"), mocha ("N passing"), node --test and TAP ("# pass N"),
# unittest ("Ran N tests").
_PASSED_COUNT = re.compile(
    r"\b(\d+)\s+(?:passed|passing)\b"
    r"|^\S*\s*pass\s+(\d+)\s*$"
    r"|^Ran\s+(\d+)\s+tests?\b",
    re.IGNORECASE | re.MULTILINE,
)
_CARGO_RESULT = re.compile(r"test result: ok\. (\d+) passed", re.IGNORECASE)


def passed_counts(output: str) -> frozenset[int]:
    """Counts of passing tests the output reports as a total: the last one
    shown, and for cargo (one result line per test binary) their sum."""
    if not output:
        return frozenset()
    found = [int(next(g for g in m.groups() if g)) for m in _PASSED_COUNT.finditer(output)]
    if not found:
        return frozenset()
    totals = {found[-1]}
    cargo = [int(n) for n in _CARGO_RESULT.findall(output)]
    if len(cargo) > 1:
        totals.add(sum(cargo))
    return frozenset(totals)


# `-m "msg"`, `-am 'msg'`, `--message=msg` in a git commit command.
_COMMIT_MESSAGE = re.compile(
    r"""(?:^|\s)(?:-[A-Za-z]*m|--message)(?:=|\s+)"""
    r"""(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|([^\s"']+))""",
    re.DOTALL,
)
_GIT_COMMIT = re.compile(r"\bgit(?:\s+-[Cc]\s+\S+)*\s+commit\b")
_HEREDOC_START = re.compile(r"^\$\(\s*cat\s+<<-?\s*['\"]?(\w+)['\"]?\s*\n", re.DOTALL)


def commit_subjects(command: str) -> list[str]:
    """First lines of the messages a `git commit` command gives with -m.

    Handles the quoted forms and the `-m "$(cat <<'EOF' ... EOF)"` form.
    An -F file or an editor message is unknown and gives nothing.
    """
    subjects: list[str] = []
    start = _GIT_COMMIT.search(command)
    if start is None:
        return subjects
    for match in _COMMIT_MESSAGE.finditer(command, start.end()):
        double, single, bare = match.groups()
        text = single if single is not None else (bare if bare is not None else
                                                   re.sub(r"\\(.)", r"\1", double))
        heredoc = _HEREDOC_START.match(text)
        if heredoc:
            body = text[heredoc.end():]
            lines = [ln.strip() for ln in body.splitlines()]
            text = next((ln for ln in lines if ln and ln != heredoc.group(1)), "")
        elif "$(" in text or "`" in text:
            continue  # built by the shell at run time: not known
        subject = " ".join(text.strip().splitlines()[0].split()) if text.strip() else ""
        if subject:
            subjects.append(subject)
    return subjects


# Flags that make a check program print something and exit without
# running the check: a version or help probe, a listing, a dry run.
_READ_ONLY_FLAGS = frozenset({
    "--version", "-V", "--help", "-h", "--collect-only", "--co",
    "--listTests", "--list-tests", "--dry-run", "--no-run", "--showConfig",
})
_MAKE_DRY_RUN = frozenset({"-n", "--just-print", "--recon"})
_MAKE_PROGRAMS = frozenset({"make", "gmake", "nmake", "mingw32-make", "ninja"})

_SWALLOW_TAIL = re.compile(
    r"(?:true|:|\$true|exit\s+0|cmd\s+/c\s+exit\s+0)", re.IGNORECASE)
_EXIT_ZERO = re.compile(r"exit\s+0", re.IGNORECASE)
_PASS_WITH_NO_TESTS = re.compile(r"--passWithNoTests\b")
# Turning pipefail ON (set -o pipefail, set -euo pipefail), not +o.
_PIPEFAIL_ON = re.compile(r"^set\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*o\s+pipefail\b")
# `set -e` (or -eu, -euo ...): a failing statement stops the script.
_ERREXIT_ON = re.compile(r"^set\s+(?:-[A-Za-z]+\s+)*-[A-Za-df-z]*e")
# Reading the real status after the pipeline or statement.
_STATUS_READ = re.compile(
    r"\$\{?PIPESTATUS\b|\bexit\s+\$LASTEXITCODE\b|\bif\s*\(\s*\$LASTEXITCODE\b"
    r"|\$\?", re.IGNORECASE)
# What follows `||` and still fails: exit with a non-zero or no code,
# return non-zero, false, throw.
_FAILS_AGAIN = re.compile(
    r"\b(?:exit|return)\b(?!\s+(?:/b\s+)?0\b)|\bfalse\b|\bthrow\b", re.IGNORECASE)
# Words that close a block; they do not set the status themselves.
_BLOCK_CLOSERS = frozenset({"done", "fi", "esac", "}", ")", "end"})


def _read_only(norm: str) -> bool:
    words = [w.strip("\"'") for w in norm.split()]
    if any(w in _READ_ONLY_FLAGS for w in words[1:]):
        return True
    return bool(words) and words[0] in _MAKE_PROGRAMS and any(
        w in _MAKE_DRY_RUN for w in words[1:])


def stage_types(stage: str) -> frozenset[ClaimType]:
    norm = normalize_stage(stage)
    if not norm:
        return frozenset()
    found = frozenset(t for t, pattern in COMMAND_EVIDENCE.items()
                      if pattern.match(norm))
    if found & CHECK_TYPES and _read_only(norm):
        found -= CHECK_TYPES
    return found


def check_types(command: str) -> frozenset[ClaimType]:
    """Claim types this command can back, judged stage by stage."""
    if not command:
        return frozenset()
    found: set[ClaimType] = set()
    for statement in split_statements(command):
        for stage in statement.stages:
            if stage:
                found |= stage_types(stage)
    return frozenset(found)


def _has_check(stages: list[str]) -> bool:
    return any(stage_types(s) & CHECK_TYPES for s in stages if s)


def _text(statement) -> str:
    return " | ".join(s for s in statement.stages if s)


def _sets_status(statement) -> bool:
    """False for a bare block closer (`done`, `fi`, `}`), which only ends a block."""
    stages = [s for s in statement.stages if s]
    return not (len(stages) == 1 and stages[0].strip() in _BLOCK_CLOSERS)


def masking(command: str) -> list[tuple[str, str, str]]:
    """Ways this command hides the exit status of a test, build or check.

    Returns (kind, how, snippet) triples, where snippet is the part of the
    command that does it. kind is "masked_exit_code" for a pipeline
    whose last stage is a pager or filter, "swallowed_failure" for
    `|| <anything that does not fail again>`, for a check followed by `;`
    or a newline and then only other commands, for `; exit 0` and for
    --passWithNoTests. The real status counts as kept when pipefail (or,
    for a sequence, `set -e`) is turned on before the check, or
    PIPESTATUS, `$?` or `$LASTEXITCODE` is read after it. Read-only
    diagnostics (`grep x f | head`, `ls d || true`, `pytest --version |
    head`) are not reported.
    """
    found: list[tuple[str, str, str]] = []
    statements = [st for st in split_statements(command)
                  if any(s for s in st.stages)]
    # Only on a stage that runs a check: not in a here-doc body, a quoted
    # script or a string literal that merely contains the flag.
    for st in statements:
        hit = next((s for s in st.stages
                    if s and _PASS_WITH_NO_TESTS.search(s) and _has_check([s])), None)
        if hit is not None:
            found.append(("swallowed_failure", "--passWithNoTests", hit))
            break
    norms = [normalize_stage(st.stages[0]) for st in statements]
    texts = [_text(st) for st in statements]
    checks = [_has_check(st.stages) for st in statements]
    # Stages of the current &&/|| chain, so `cd x && pytest || true` counts.
    chain: list[str] = []
    chain_start = 0
    for pos, statement in enumerate(statements):
        stages = [s for s in statement.stages if s]
        before, after = norms[:pos], texts[pos + 1:]
        pipefail = any(_PIPEFAIL_ON.match(n) for n in before)
        errexit = any(_ERREXIT_ON.match(n) for n in before)
        read_after = any(_STATUS_READ.search(t) for t in after)
        prev_op = statements[pos - 1].op if pos else ""
        if prev_op == "||" and _has_check(chain):
            # Everything from here to the end of the &&/|| chain runs only
            # when the check failed, and its status replaces the check's.
            tail_end = pos
            while (tail_end < len(statements) - 1
                   and statements[tail_end].op in ("&&", "||")):
                tail_end += 1
            tail = statements[pos:tail_end + 1]
            rest = command[statement.start:]
            if not (any(checks[pos:tail_end + 1]) or _FAILS_AGAIN.search(rest)
                    or read_after or _STATUS_READ.search(texts[pos])):
                is_swallow = (len(stages) == 1 and _SWALLOW_TAIL.fullmatch(
                    normalize_stage(stages[0]) or ""))
                how = "|| " + (stages[0].strip() if is_swallow else
                               " ".join(_text(st) for st in tail))
                found.append(("swallowed_failure", how[:60],
                              command[chain_start:statement.end]))
        if (checks[pos] and statement.op in (";", "\n") and not errexit
                and not read_after):
            later = [i for i in range(pos + 1, len(statements))
                     if _sets_status(statements[i])]
            if later and not any(checks[i] for i in later):
                # The call's status is the last command's, not the check's.
                # `; exit 0` says so on purpose; anything else loses it the
                # way a pager does, and the output still shows the result.
                last = statements[later[-1]]
                if _EXIT_ZERO.fullmatch(texts[later[-1]].strip()):
                    found.append(("swallowed_failure", "; exit 0",
                                  command[statement.start:last.end]))
                else:
                    how = "; " + (program(last.stages[0]) or texts[later[-1]].strip())
                    found.append(("masked_exit_code", how[:60],
                                  command[statement.start:last.end]))
        if len(stages) > 1 and not pipefail:
            last_prog = program(stages[-1])
            if last_prog in FILTERS and _has_check(stages[:-1]) and not read_after:
                found.append(("masked_exit_code", "| " + last_prog,
                              command[statement.start:statement.end]))
        if stages[0].lstrip()[:1] in ("(", "{"):
            # A group starts here: a `||` inside it applies to the group's
            # own commands, not to the chain before it.
            chain = []
        if not chain:
            chain_start = statement.start
        chain = (chain + stages) if statement.op in ("&&", "||") else []
    return found


def masked_exit(command: str) -> str | None:
    """How a check's exit status in `command` is hidden (`| tail`, `; git`,
    `|| true` ...), or None when the call's status is the check's own."""
    found = masking(command)
    return found[0][1] if found else None
