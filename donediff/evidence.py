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
from .shell import command_words, normalize_stage, program, split_statements, wrapper_inner

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
# SEC-285: mocha's "N failing", and a capital FAIL at a line start followed
# by ":", a space or the line end (go test "--- FAIL:" and "FAIL pkg", jest
# "FAIL src/a.test.js"); FAIL_FAST=0 or "Failure" in prose do not match.
FAILURE_IN_OUTPUT = re.compile(
    r"\b([1-9]\d*)\s+fail(?:ed|ures?|ing)\b"
    r"|(?-i:\bFAILED\b)|\bTests?\s+failed\b|\bBUILD\s+FAILED\b"
    r"|test result: FAILED"
    r"|^\S*\s*fail\s+[1-9]\d*\s*$"
    r"|(?-i:^(?:--- )?FAIL(?::|\s|$))",
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

MAX_DIGITS = 18  # significant digits in a count or exit code read from a log


def log_int(text: str, overlong: int | None = None) -> int | None:
    """int() of a digit run (an optional leading "-") taken from a log.

    A run with more than MAX_DIGITS significant digits gives `overlong`
    instead: no test count or exit code is that long, and Python refuses
    to convert over 4300 digits at all (SEC-228). Exit-code callers pass a
    failure code, since such a number is not 0; count callers drop it.
    """
    sign = -1 if text.startswith("-") else 1
    digits = text.lstrip("-").lstrip("0") or "0"
    if len(digits) > MAX_DIGITS:
        return overlong
    return sign * int(digits)


_INTMAX = 2 ** 63  # bash reads an exit or return code as intmax_t


def exit_literal_fails(code: str) -> bool:
    """True when `exit <code>` (or return) with this literal fails, read the
    way bash reads it (SEC-252): sign and leading zeros dropped, up to 19
    digits parsed, and a value inside [-2^63, 2^63 - 1] fails when it is not
    0 mod 256 (bash exits n & 255). Longer or outside that range: bash
    refuses the number, a failure. `exit 1152921504606846976` (2^60) exits 0."""
    sign = -1 if code.startswith("-") else 1
    digits = code.lstrip("-").lstrip("0") or "0"
    if len(digits) > 19:
        return True
    value = sign * int(digits)
    return not -_INTMAX <= value < _INTMAX or value % 256 != 0


# What a runner prints when it ran no test at all (SEC-222).
_RAN_NOTHING = re.compile(
    r"\bno tests ran\b|\bcollected 0 items\b|^Ran 0 tests\b|\bTests are skipped\b"
    r"|\bNo tests found\b|\bNo tests were found\b|\bno tests to run\b"
    # SEC-253: dotnet test with a filter that matches nothing; pytest when
    # every test was deselected (the summary line starts with the count).
    r"|\bNo test matches the given testcase filter\b|\bdeselecting all items\b"
    r"|^[=\s]*\d+ deselected\b",
    re.IGNORECASE | re.MULTILINE)
_GO_OK_LINE = re.compile(r"^ok\s+\S+.*$", re.MULTILINE)
# Maven Surefire and JUnit: "Tests run: N, Failures: ..." (SEC-253).
_TESTS_RUN = re.compile(r"\bTests run: (\d+),")


def ran_nothing(output: str) -> bool:
    """True when the output says the runner ran no test (go: every package
    line says [no tests to run]; Maven: every "Tests run:" count is 0)."""
    if not output:
        return False
    go_ok = _GO_OK_LINE.findall(output)
    if go_ok:
        return all("[no tests to run]" in line for line in go_ok)
    runs = _TESTS_RUN.findall(output)
    if runs:
        return not any(n.strip("0") for n in runs)
    return bool(_RAN_NOTHING.search(output))


def passed_counts(output: str) -> frozenset[int]:
    """Counts of passing tests the output reports as a total: the last one
    shown, and for cargo (one result line per test binary) their sum.
    {0} when the output says no test ran at all (SEC-222)."""
    if not output:
        return frozenset()
    found = [log_int(next(g for g in m.groups() if g)) for m in _PASSED_COUNT.finditer(output)]
    if not any(found) and ran_nothing(output):
        return frozenset({0})
    if not found or found[-1] is None:
        return frozenset()  # no count, or an overlong last one: no count to match
    totals = {found[-1]}
    cargo = [log_int(n) for n in _CARGO_RESULT.findall(output)]
    if len(cargo) > 1 and None not in cargo:
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
    # SEC-222: forms that plan, list or skip instead of running the tests.
    "--setup-plan", "--setup-only", "--fixtures", "--fixtures-per-test",
    "--markers", "--list", "-list", "--show-only", "-DskipTests",
    "-DskipTests=true", "-Dmaven.test.skip", "-Dmaven.test.skip=true",
    # SEC-253: npm runs nothing and exits 0 when the script is missing.
    "--if-present",
})
# SEC-253: subcommands that list the tests instead of running them.
_LIST_SUBCOMMAND = frozenset({"vitest", "nextest"})
# ctest -N lists the tests without running them.
_CTEST_LIST = frozenset({"-N", "--show-only"})
# Option variables that change what a runner does without showing in its argv.
_OPTION_ENV = re.compile(
    r"(?:\$env:)?\b(?:PYTEST_ADDOPTS|MAVEN_OPTS|MAVEN_ARGS|GRADLE_OPTS|NODE_OPTIONS)"
    r"\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)", re.IGNORECASE)
# npm's if-present set through its config variable (SEC-253 leftover), `$env:` too.
_NPM_IF_PRESENT = re.compile(r"\bnpm_config_if_present\s*=\s*[\"']?(?!false\b|0\b)[^\s\"';]",
                             re.IGNORECASE)
# --exit-zero (ruff, flake8, pylint): findings no longer fail the run.
_EXIT_ZERO_FLAG = re.compile(r"(?:^|\s)--exit-zero\b")
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
# The real status is kept only when the command EXITS with it (SEC-221):
# `exit $?`, `exit ${PIPESTATUS[0]}`, `exit $LASTEXITCODE`, `exit $rc`
# after `rc=$?`. Printing it (`echo $?`) keeps nothing: the call's status
# is then echo's.
_STATUS_EXIT = re.compile(
    r"\bexit\s+\"?\$\{?(?:\?|PIPESTATUS\[\d+\]|LASTEXITCODE)\}?\"?(?=[\s;)}]|$)",
    re.IGNORECASE)
# A test of the status (`[ $? -ne 0 ]`, `if ($LASTEXITCODE -ne 0)`,
# `if (-not $?)`): it keeps the status only when a failing exit follows.
_STATUS_REF = re.compile(r"\$\{?(?:\?|PIPESTATUS\b|LASTEXITCODE\b)", re.IGNORECASE)
_STATUS_SAVED = re.compile(r"\b([A-Za-z_]\w*)=\"?\$\{?(?:\?|PIPESTATUS\[\d+\])", re.IGNORECASE)
_EXIT_WORD = re.compile(r"^(?:exit|return)(?:\s+(?:/b\s+)?(\S+?))?[\s;)}]*$", re.IGNORECASE)
# `wait <pid>` returns the background job's status; a bare `wait` returns 0,
# and so does `wait -f` with no id (V-1).
_WAIT_FOR_JOB = re.compile(r"^wait\s+(?:\$!|\$\{!\}|%[%+1]|-n)\s*$", re.IGNORECASE)
# Words that close a block; they do not set the status themselves.
_BLOCK_CLOSERS = frozenset({"done", "fi", "esac", "}", ")", "end"})


def _read_only(norm: str) -> bool:
    words = [w.strip("\"'") for w in norm.split()]
    if any(w in _READ_ONLY_FLAGS for w in words[1:]):
        return True
    # --if-present=true (any value but false or 0) is the flag (SEC-253 leftover).
    if any(w.lower().startswith("--if-present=") and w[13:].lower() not in ("false", "0")
           for w in words[1:]):
        return True
    if words and words[0] == "ctest" and any(w in _CTEST_LIST for w in words[1:]):
        return True
    # gradle test -x test (--exclude-task test): the test task is excluded.
    if any(a in ("-x", "--exclude-task") and b.lstrip(":") == "test"
           for a, b in zip(words, words[1:])) or "--exclude-task=test" in words:
        return True
    # vitest list, cargo nextest list (SEC-253).
    if any(re.split(r"[\\/]", a)[-1] in _LIST_SUBCOMMAND and b == "list"
           for a, b in zip(words, words[1:])):
        return True
    # go test -c compiles the test binary and runs nothing (SEC-253).
    if words[:2] == ["go", "test"] and any(w in ("-c", "-c=true") for w in words[2:]):
        return True
    return bool(words) and words[0] in _MAKE_PROGRAMS and any(
        w in _MAKE_DRY_RUN for w in words[1:])


def stage_types(stage: str) -> frozenset[ClaimType]:
    norm = normalize_stage(stage)
    if not norm:
        return frozenset()
    found = frozenset(t for t, pattern in COMMAND_EVIDENCE.items()
                      if pattern.match(norm))
    if found & CHECK_TYPES and (_read_only(norm) or _env_read_only(stage)):
        found -= CHECK_TYPES
    return found


def _env_read_only(text: str) -> bool:
    """True when an option variable set in `text` (PYTEST_ADDOPTS=--co ...)
    holds a flag that makes the runner list or skip instead of run."""
    return bool(_NPM_IF_PRESENT.search(text)) or any(
        _read_only("x " + m.group(1).strip("\"'")) for m in _OPTION_ENV.finditer(text))


def check_types(command: str) -> frozenset[ClaimType]:
    """Claim types this command can back, judged stage by stage. An option
    variable set anywhere in the command (`export PYTEST_ADDOPTS=--co; pytest`)
    that makes the runner list or skip drops the check types (SEC-222)."""
    if not command:
        return frozenset()
    found: set[ClaimType] = set()
    for statement in split_statements(command):
        for stage in statement.stages:
            if stage:
                found |= stage_types(stage)
    if found & CHECK_TYPES and _env_read_only(command):
        found -= CHECK_TYPES
    return frozenset(found)


def _has_check(stages: list[str]) -> bool:
    return any(stage_types(s) & CHECK_TYPES for s in stages if s)


def _failing_stage(stage: str) -> bool:
    """True for a stage that always fails: false, throw ..., a bare exit or
    return, or exit/return with a literal code that is non-zero mod 256.
    A word inside an argument (`echo false`) or a computed code is not."""
    norm = normalize_stage(stage).rstrip(" ;)}")
    low = norm.lower()
    if low in ("false", "$false") or low == "throw" or low.startswith("throw "):
        return True
    match = _EXIT_WORD.match(norm)
    if match is None:
        return False
    code = match.group(1)
    return code is None or (re.fullmatch(r"-?\d+", code) is not None
                            and exit_literal_fails(code))


def _fails_again(statements) -> bool:
    """True when a `||` tail still fails: an exit or throw that is reached
    (not the right side of another `||`), or a false that sets the status."""
    for i, st in enumerate(statements):
        stage = next((s for s in reversed(st.stages) if s), "")
        if not stage or not _failing_stage(stage):
            continue
        if i and statements[i - 1].op == "||":
            continue  # `true || exit 1`: only runs when the left side failed
        word = normalize_stage(stage).split(" ", 1)[0].lower()
        if word in ("exit", "return", "throw") or i == len(statements) - 1:
            return True
    return False


def _status_kept(after: list[str], ops: list[str] | None = None) -> bool:
    """True when the statements after a check exit with its status. `ops` are
    the operators that follow each statement (`&&`, `||`, `;` ...)."""
    ops = ops or [""] * len(after)
    saved: set[str] = set()
    for i, text in enumerate(after):
        if _STATUS_EXIT.search(text):
            return True
        # Bash names are case-sensitive; `rc=0` or `unset rc` drops a saved status (V-3).
        saved -= {n for n in saved if re.search(
            r"(?:^|[\s;])(?:" + re.escape(n) + r"=(?!\"?\$\{?(?:\?|PIPESTATUS))|unset\s+"
            + re.escape(n) + r"\b)", text)}
        saved.update(m.group(1) for m in _STATUS_SAVED.finditer(text))
        for name in saved:
            if re.search(r"\bexit\s+\"?\$(?:" + re.escape(name) + r"|\{" + re.escape(name)
                         + r"\})\"?(?=[\s;)}]|$)", text):
                return True
        if _STATUS_REF.search(text) and _test_exits_on_failure(after, ops, i):
            return True
    return False


# The status compared with a literal: `[ $? -ne 0 ]`, `if ($LASTEXITCODE -ne 0)` (V-3).
_STATUS_TEST = re.compile(
    r"\$\{?(?:\?|PIPESTATUS\[\d+\]|LASTEXITCODE)\}?[\"']?\s*(-eq|-ne|-gt|-lt|-ge|-le|==|!=|=)"
    r"\s*[\"']?(-?\d+)\b", re.IGNORECASE)
_ELSE = re.compile(r"(?:^|[\s;}])(?:else|elif)\b", re.IGNORECASE)
_GROUP_CLOSER = {"{": "}", "(": ")"}


def _test_exits_on_failure(after: list[str], ops: list[str], i: int) -> bool:
    """True when the status test at after[i] runs a failing exit when the check
    failed. Only `$?` compared with 0 is read: `-ne|!=|-gt 0` then `&&` or
    `then` exit N, `-eq|==|= 0` then `||` or `else` exit N. A test of another
    value (`[ $? -eq 1 ] || exit 1`) keeps nothing. With no comparison
    (`if (-not $?) { exit 1 }`), any failing exit after it counts."""
    text = after[i]
    test = _STATUS_TEST.search(text)
    if test is None:
        return any(_fails_in(t) for t in after[i:])
    op, value = test.group(1).lower(), test.group(2)
    if int(value) != 0 or op in ("-lt", "-ge", "-le"):
        return False
    on_true = op in ("-ne", "!=", "-gt")
    parts = _ELSE.split(text[test.end():], maxsplit=1)
    branches: dict[bool, list[tuple[str, bool]]] = {True: [(parts[0], False)], False: []}
    if len(parts) > 1:
        branches[False].append((parts[1], False))
    if re.match(r"\s*(?:if|elif)\b", text, re.IGNORECASE) and not re.search(r"(?<!\$)\{", text):
        side = True  # bash if: the then branch runs to else/elif/fi
        for later in after[i + 1:]:
            word = later.strip().split(" ", 1)[0].lower()
            if word in ("fi", "fi;"):
                break
            if word in ("else", "elif"):
                side = False
            branches[side].append((later, False))
    elif ops[i] in ("&&", "||"):
        side, j = ops[i] == "&&", i + 1
        closer = _GROUP_CLOSER.get(after[j].lstrip()[:1]) if j < len(after) else None
        while j < len(after):
            branches[side].append((after[j], ops[i] == "||" and j == i + 1))
            if closer:
                if after[j].rstrip().endswith(closer):
                    break
            elif ops[j] != "&&":
                break
            j += 1
    return any(_fails_in(t, after_or) for t, after_or in branches[on_true])


# exit/return/throw/false in command position (after ; { ( & | then else do
# or at the start), outside quotes: `if [ $? -ne 0 ]; then exit 1; fi`.
_EXIT_IN_TEXT = re.compile(
    r"(?:^|[;{(&|]|\b(?:then|else|do)\b)\s*"
    r"(?:(?:exit|return)\b(?:\s+(?:/b\s+)?([^\s;)}]+))?(?=[\s;)}]|$)|(throw|false)\b)",
    re.IGNORECASE)
_QUOTED_TEXT = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'")


# `exit "0"` is `exit 0`: unquote a quoted literal code before quotes are blanked (V-3).
_QUOTED_CODE = re.compile(r"(\b(?:exit|return)\s+(?:/b\s+)?)([\"'])(-?\d+)\2", re.IGNORECASE)


def _fails_in(text: str, after_or: bool = False) -> bool:
    """True when `text` runs a failing exit (inside an if block too). A bare
    exit returns the status of the test before it, so it fails only as the
    first command after `||` (`after_or`)."""
    blank = _QUOTED_TEXT.sub(" ", _QUOTED_CODE.sub(r"\1\3", text))
    for match in _EXIT_IN_TEXT.finditer(blank):
        if match.group(2):
            return True
        code = match.group(1)
        if code is None:
            if after_or and match.start() == 0:
                return True
        elif re.fullmatch(r"-?\d+", code) and exit_literal_fails(code):
            return True
    return False


_STATUS_CODES = frozenset({"$?", "${?}", "$LASTEXITCODE", "${LASTEXITCODE}"})


def _exits_zero(text: str) -> bool:
    """True when `text` may exit 0 (SEC-251 leftover): exit or return with a
    literal code that is 0 mod 256 (quoted too), with any code other than a
    literal, `$?` or a variable saved from `$?` in `text` (`exit +0`,
    `exit $((0))`), or exec."""
    text = re.sub(r"(\b(?:exit|return)\s+(?:/b\s+)?)([\"'])([^\"'\s]+)\2", r"\1\3", text,
                  flags=re.IGNORECASE)
    blank = _QUOTED_TEXT.sub(" ", text)
    if re.search(r"(?:^|[;{(&|]|\b(?:then|else|do)\b)\s*exec\b", blank, re.IGNORECASE):
        return True
    kept = {m.group(1) for m in _STATUS_SAVED.finditer(text)}
    kept_codes = _STATUS_CODES | {"$" + k for k in kept} | {"${" + k + "}" for k in kept}
    for match in _EXIT_IN_TEXT.finditer(blank):
        code = match.group(1)
        if code is None:
            continue
        if re.fullmatch(r"-?\d+", code):
            if not exit_literal_fails(code):
                return True
        elif code.strip('"') not in kept_codes:
            return True
    return False


# A trap whose handler is not provably status-keeping makes the call exit
# with whatever the handler says, whatever the check returned (SEC-251,
# SEC-291, fail closed): bash `trap 'exit 0' EXIT` (or 0) wherever it stands,
# ERR or DEBUG when set before the check, and PowerShell's `trap { exit 0 }`,
# which covers its whole scope.
_SHELL_WORD = re.compile(r"""(?:"(?:[^"\\]|\\.)*"?|'[^']*'?|\\.|[^\s"'\\])+""")
# Words after which the next word is still in command position.
_CMD_PREFIX = frozenset({"then", "do", "else", "elif", "time", "builtin", "command", "exec",
                         "nohup", "env", "sudo", "nice", "!", "{", "(", ""})
_ASSIGN_WORD = re.compile(r"[A-Za-z_]\w*(?:\[[^\]]*\])?\+?=")
_FUNC_DEF = re.compile(r"(?:^|[\s;{])(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)")


def _dequote(word: str) -> str:
    """A word as bash runs it: quotes and backslashes dropped (`\\trap`, `'trap'`, `tr''ap`)."""
    return re.sub(r"\$(?=')|[\"'\\]", "", word)


def _commands(text: str):
    """(word, raw word, text after it) for each word of `text` in command
    position, quotes and backslashes dropped: at the start, after `(`, `{` or
    a case pattern `x)`, and after words that only start the next one (then,
    time, builtin, command, an assignment, an option of those)."""
    at_cmd = True
    for match in _SHELL_WORD.finditer(text):
        raw = match.group()
        core = raw.lstrip("({!&")
        word = _dequote(core)
        if at_cmd or core != raw:
            yield word, core, text[match.end():]
            at_cmd = (word.lower() in _CMD_PREFIX or bool(_ASSIGN_WORD.match(core))
                      or word.startswith("-"))
        else:
            at_cmd = False
        if raw.endswith(("(", ")", "{")):
            at_cmd = True


def _trap_text(text: str, _depth: int = 0) -> str | None:
    """`trap ...` from the first trap in command position in `text` (also
    `\\trap`, `'trap'`, `x=1 trap`, `case x in x) trap`, and inside `eval`),
    else None (SEC-291)."""
    for word, _, rest in _commands(text):
        low = word.lower()
        if low == "trap":
            return "trap" + rest
        if low == "eval" and _depth < 2:
            inner = " ".join(command_words(rest))
            for st in split_statements(inner):
                found = _trap_text(_text(st), _depth + 1)
                if found:
                    return found
            return None
    return None


_STATUS_WORDS = frozenset({"$?", "${?}", "$LASTEXITCODE", "${LASTEXITCODE}"})


def _handler_keeps(handler: str, literal: bool, funcs: set[str]) -> bool:
    """True only when a bash trap handler provably keeps the check's status
    (fail closed, SEC-291): every exit in it has a failing literal code, or is
    bare or `exit $?` as its first command, or `exit $rc` after `rc=$?` as its
    first command with rc never set again. `$?` read later is another
    command's 0; in a handler that is not single-quoted it is expanded when
    the trap is set. Nothing in it may run text or a function the engine
    cannot read (`$t`, eval, source, exec, a function of the call)."""
    statements = [_text(st) for st in split_statements(handler) if any(st.stages)]
    saved = None
    first = re.fullmatch(r"\s*([A-Za-z_]\w*)=(\"?)\$\{?\?\}?\2\s*", statements[0]) \
        if statements else None
    if first and literal:
        name = first.group(1)
        rest = re.sub(r"\$(?:" + name + r"\b|\{" + name + r"\})", " ", handler)
        if len(re.findall(r"(?<![\w$])" + name + r"\b", rest)) == 1:
            saved = name
    for k, text in enumerate(statements):
        for word, _, rest in _commands(text):
            low = word.lower()
            if low.startswith("$") or "`" in low or low in ("eval", "source", ".", "exec", "trap") \
                    or word in funcs:
                return False
            if low not in ("exit", "return", "logout"):
                continue
            args = command_words(rest)
            code = args[0] if args else ""
            code = code if code.startswith("${") else code.rstrip(";)}")
            if not code:
                ok = k == 0
            elif re.fullmatch(r"-?\d+", code):
                ok = exit_literal_fails(code)
            elif code in _STATUS_WORDS:
                ok = literal and k == 0
            else:
                ok = saved is not None and code in ("$" + saved, "${" + saved + "}")
            if not ok:
                return False
    return True
_TRAP_PS = re.compile(r"^trap\b(?:\s*\[[^\]]*\])?\s*\{(.*)\}\s*$", re.IGNORECASE | re.DOTALL)
_TRAP_BEFORE = frozenset({"err", "sigerr", "debug", "sigdebug", "return", "sigreturn"})
# Signal names a trap can be given; a command named by a variable with one of
# them as an argument (`$t 'exit 0' EXIT`) may be a trap.
_TRAP_SIGNAL_WORD = re.compile(r"(?:SIG)?(?:EXIT|ERR|DEBUG|RETURN)|0", re.IGNORECASE)


def _runs_generated_text(text: str) -> bool:
    """True when `text` runs shell text the engine cannot read in this shell,
    which may set a trap or anything else (fail closed, SEC-251 ruling (b)):
    source or `.` of `<(..)`, `$(..)`, a backtick, /dev/stdin and the like or a
    redirect, eval of anything not literal, or a command named by a variable
    given a trap signal name."""
    for word, _, rest in _commands(text):
        low = word.lower()
        if low in ("source", ".") and re.search(r"<|\$\(|`|/dev/|/proc/", rest):
            return True
        if low == "eval" and re.search(r"[$`<]", rest):
            return True
        args = _SHELL_WORD.findall(rest)
        if (low.startswith("$") or "`" in low) and len(args) >= 2 \
                and _TRAP_SIGNAL_WORD.fullmatch(_dequote(args[-1])):
            return True
    return False


def _zero_exit_trap(texts: list[str], pos: int) -> str | None:
    """"anywhere" or "before" for a trap at statement `pos` whose handler exits
    0 (see above), else None. A PowerShell block cut at `;` is joined back."""
    if _runs_generated_text(texts[pos]):
        return "anywhere"
    text = _trap_text(texts[pos])
    if text is None:
        return None
    text = text.strip()
    block = text
    if re.match(r"^trap\b(?:\s*\[[^\]]*\])?\s*\{", text, re.IGNORECASE) and "}" not in text:
        end = pos
        while end < len(texts) - 1 and not texts[end].rstrip().endswith("}"):
            end += 1
        block = "; ".join([text] + [t.strip() for t in texts[pos + 1:end + 1]])
    match = _TRAP_PS.match(block)
    if match:
        return "anywhere" if _exits_zero(match.group(1)) else None
    words = _SHELL_WORD.findall(text)[1:]
    if words[:1] == ["--"]:
        words = words[1:]
    if len(words) < 2 or words[0].startswith("-"):
        return None  # trap -p, trap -l, trap - EXIT: nothing set
    # Any signal may come while the check runs (a child can signal the shell),
    # so only ERR, DEBUG and RETURN are limited to traps set before it (SEC-251).
    sigs = {_dequote(s).lower().strip(";") for s in words[1:]}
    reach = "before" if sigs <= _TRAP_BEFORE else "anywhere"
    handler, literal = words[0], False
    if re.fullmatch(r"\$?'[^']*'", handler):
        handler, literal = handler[handler.index("'") + 1:-1], True
    elif re.fullmatch(r"\"(?:[^\"\\]|\\.)*\"", handler):
        handler = re.sub(r"\\([\"\\$`])", r"\1", handler[1:-1])
    else:  # unquoted or mixed (`exit\ 0`, `'exit '$?`): expanded when the trap is set
        handler = re.sub(r"\\(.)|[\"']", lambda m: m.group(1) or "", handler)
    # A handler that calls a function defined in the call may exit 0 there.
    funcs = {m.group(1) for t in texts for m in _FUNC_DEF.finditer(t)}
    return reach if reach and not _handler_keeps(handler, literal, funcs) else None


def zero_exit_trap(command: str, _depth: int = 0) -> str | None:
    """The text of a trap in `command` whose handler may exit 0, whatever the
    call runs (a named command, not only a check), or None (V-2). Shells
    started through a wrapper (`bash -c "..."`) are read too."""
    statements = [st for st in split_statements(command) if any(s for s in st.stages)]
    texts = [_text(st) for st in statements]
    for pos in range(len(statements)):
        for stage in statements[pos].stages:
            inner = wrapper_inner(stage, launchers=True) if stage and _depth < 2 else None
            found = zero_exit_trap(inner, _depth + 1) if inner else None
            if found:
                return found
        if _zero_exit_trap(texts, pos):
            return (_trap_text(texts[pos]) or texts[pos]).strip()[:60]
    return None


def _text(statement) -> str:
    return " | ".join(s for s in statement.stages if s)


def _sets_status(statement) -> bool:
    """False for a bare block closer (`done`, `fi`, `}`), which only ends a block."""
    stages = [s for s in statement.stages if s]
    return not (len(stages) == 1 and stages[0].strip() in _BLOCK_CLOSERS)


def masking(command: str, _depth: int = 0) -> list[tuple[str, str, str]]:
    """Ways this command hides the exit status of a test, build or check.

    Returns (kind, how, snippet) triples, where snippet is the part of the
    command that does it. kind is "masked_exit_code" for a pipeline
    whose last stage is a pager or filter, "swallowed_failure" for
    `|| <anything that does not fail again>`, for a check followed by `;`
    or a newline and then only other commands, for `; exit 0`, for a trap
    whose handler exits 0 and for --passWithNoTests. The real status counts as kept when pipefail (or,
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
    for st in statements:
        hit = next((s for s in st.stages
                    if s and _EXIT_ZERO_FLAG.search(s) and _has_check([s])), None)
        if hit is not None:
            found.append(("swallowed_failure", "--exit-zero", hit))
            break
    norms = [normalize_stage(st.stages[0]) for st in statements]
    texts = [_text(st) for st in statements]
    checks = [_has_check(st.stages) for st in statements]
    for pos, statement in enumerate(statements):
        reach = _zero_exit_trap(texts, pos)
        if reach and any(checks if reach == "anywhere" else checks[pos + 1:]):
            found.append(("swallowed_failure", texts[pos].strip()[:60],
                          command[statement.start:statement.end]))
            break
    # Stages of the current &&/|| chain, so `cd x && pytest || true` counts.
    chain: list[str] = []
    chain_start = 0
    for pos, statement in enumerate(statements):
        stages = [s for s in statement.stages if s]
        before, after = norms[:pos], texts[pos + 1:]
        pipefail = any(_PIPEFAIL_ON.match(n) for n in before)
        errexit = any(_ERREXIT_ON.match(n) for n in before)
        read_after = _status_kept(after, [st.op for st in statements[pos + 1:]])
        prev_op = statements[pos - 1].op if pos else ""
        if prev_op == "||" and _has_check(chain):
            # Everything from here to the end of the &&/|| chain runs only
            # when the check failed, and its status replaces the check's.
            tail_end = pos
            while (tail_end < len(statements) - 1
                   and statements[tail_end].op in ("&&", "||")):
                tail_end += 1
            tail = statements[pos:tail_end + 1]
            closer = {"(": ")", "{": "}"}.get(stages[0].lstrip()[:1])
            if closer:
                # `|| (echo failed; exit 1)`: the group runs to its closer.
                end = pos
                while end < len(statements) - 1 and not texts[end].rstrip().endswith(closer):
                    end += 1
                tail = statements[pos:max(end, tail_end) + 1]
            if not (any(checks[pos:tail_end + 1]) or _fails_again(tail)
                    or read_after or _STATUS_EXIT.search(texts[pos])):
                is_swallow = (len(stages) == 1 and _SWALLOW_TAIL.fullmatch(
                    normalize_stage(stages[0]) or ""))
                how = "|| " + (stages[0].strip() if is_swallow else
                               " ".join(_text(st) for st in tail))
                found.append(("swallowed_failure", how[:60],
                              command[chain_start:statement.end]))
        # A failing check skips the rest of its && chain, so the chain's own
        # end decides (SEC-284): in `pytest && echo ok; true` the status
        # comes from `true`, in `pytest && echo ok & true` from `true` too.
        end = pos
        if checks[pos] and not read_after:
            while end < len(statements) - 1 and statements[end].op == "&&":
                end += 1
        # The wait keeps the status only as the call's last statement, right
        # after the check's `&` (V-1): `& wait $! || true`, `& wait $!; true`
        # and `& true & wait $!` (the other job) exit 0.
        waited = end + 2 == len(statements) and bool(_WAIT_FOR_JOB.match(norms[end + 1]))
        if checks[pos] and statements[end].op == "&" and not read_after and not waited:
            # A check sent to the background (`pytest & wait`), or cmd's
            # `a & b`: the call's status is whatever runs after it.
            nxt = next((program(st.stages[0]) for st in statements[end + 1:]
                        if st.stages and st.stages[0]), "")
            found.append(("masked_exit_code", "& " + (nxt or "(background)"),
                          command[statement.start:]))
        elif (checks[pos] and statements[end].op in (";", "\n") and not read_after
                and not (errexit and end == pos)):
            # set -e does not stop on a command inside a && chain other than
            # its last one, so it keeps the status only for the chain's end.
            later = [i for i in range(end + 1, len(statements))
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
            # Without pipefail the LAST stage's status is the pipeline's: any
            # last stage that is not itself a check hides the check's status
            # (a pager, a filter, `| true`, `| Out-Null`, `| xargs ...`).
            last_prog = program(stages[-1])
            if (not _has_check(stages[-1:]) and _has_check(stages[:-1])
                    and not read_after):
                found.append(("masked_exit_code", "| " + (last_prog or "?"),
                              command[statement.start:statement.end]))
        if stages[0].lstrip("({ \t").startswith("!") and _has_check(stages[:1]):
            # `! pytest`: the status is negated, a failing run reads exit 0.
            found.append(("swallowed_failure", "! (status negated)",
                          command[statement.start:statement.end]))
        for stage in stages:
            # bash -c "...", cmd /c "...": judge the inner command too.
            inner = wrapper_inner(stage, launchers=True)  # also timeout, stdbuf, xargs (SEC-251)
            if inner is not None and _depth < 2:
                found.extend(masking(inner, _depth + 1))
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
