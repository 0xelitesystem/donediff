# donediff

> Prove what your agent actually did.

Formerly agent-receipts. The old repository address redirects here. The old `receipts` command was removed in 0.4.0;
use `donediff`.

`donediff` audits AI coding agent sessions and verifies every claim the agent made against what it *actually did*. It parses the session transcript, extracts checkable claims ("tests pass", "I committed the changes", "created `cache.py`", "12 of 12 tests pass", "deployed"), and cross-checks each one against ground truth: the real exit codes of the commands the agent ran, the real edits it made, the real state of your filesystem, and, since 0.4.0, the real git state of the project (the commit exists, the named file is in the diff).

Since v0.2 that includes the work the agent delegated: the transcripts of its sub-agents and workflow agents are read too, and every piece of evidence names where it ran.

Zero dependencies. Pure Python stdlib; the git checks use your installed `git`, read-only, when there is one. Works offline, no API keys, nothing leaves your machine.

## The problem

Coding agents generate completion language as part of their output pattern, regardless of the actual state of the codebase. In the [2026 State of AI survey](https://2026.stateofai.dev/en-US/risks-pain-points/), "Hallucination & inaccuracies" ranked first among pain points: 3,899 of the 6,084 people who answered that question named it, and the specific failure mode everyone has a story about is the agent that says **"All tests pass"** while the suite is red, or quietly edits the *test* until it goes green.

It reads the session transcript and checks each done-claim against the commands and results the transcript records.

## What it catches

```
  donediff, claims vs. reality
  session demo-session  -  7 events  -  /home/dev/acme-api
  evidence from the main transcript; no sub-agent or workflow transcripts found

  DONEDIFF SCORE  0/100 (F)
  1 verified  -  0 stale  -  0 unverified  -  2 contradicted  -  3 gaming signal(s)

  CLAIMS
  ✗ CONTRADICTED "All tests pass."
    └─ most recent relevant run failed (output reports failures despite exit 0): `python -m pytest tests/ -q || true` [in main]
  ✓ VERIFIED     "committed the changes"
    └─ `git commit -am 'Add rate limiting' --no-verify` succeeded (exit 0) [in main]
  ✗ CONTRADICTED "feature is complete"
    └─ check after final edit failed: `python -m pytest tests/ -q || true` [in main]

  GAMING SIGNALS
  ⚠ MED   test_rate_limit.py: assertions reduced 2 -> 1 in one edit
  ⚠ HIGH  command masks its own failure: `python -m pytest tests/ -q || true`
  ⚠ HIGH  commit made with --no-verify (hooks bypassed)
```

That's a real audit of [`examples/demo-session.jsonl`](examples/demo-session.jsonl), an agent that hit 2 failing tests, weakened an assertion instead of fixing the code, masked the remaining failure with `|| true`, committed with `--no-verify`, and reported "All tests pass." Run it yourself:

```bash
donediff audit examples/demo-session.jsonl --no-disk-check
```

(The demo's project folder does not exist on your machine, so `--no-disk-check` keeps the output as shown; without it the commit line adds `git state not checked (project directory not found)`.)

## Install

```bash
pip install git+https://github.com/0xelitesystem/donediff
```

Python 3.10 or newer. No dependencies.

## Usage

```bash
# Audit your most recent Claude Code session
donediff audit latest

# Audit a specific session by id prefix, or any transcript path
donediff audit 8dcbd9b2
donediff audit ~/.claude/projects/<project>/<session>.jsonl

# List recent sessions across all projects
donediff list

# Machine-readable output / Markdown report
donediff audit latest --json
donediff audit latest --md report.md

# CI gate: fail the pipeline when the agent didn't back up its claims
donediff audit latest --fail-under 80

# Only the main transcript, ignoring sub-agents and workflows (v0.1 behaviour)
donediff audit latest --main-only
```

## Sessions that delegate

A session that hands work to sub-agents or workflows keeps their transcripts next to its own (`<session>/subagents/...`). `donediff audit` finds them automatically, and a test run inside any of them counts as evidence, with the evidence line naming where it ran:

```
  ✓ VERIFIED     "The build is clean and all 900 tests pass."
    └─ `npm run build` succeeded (exit 0) [in workflow wf_fx-001, agent b1build (build and test)]
  ? UNVERIFIED   "The agent says 900 tests pass."
    └─ relayed from an agent's report, no command result seen
```

Ordering stays honest: delegated evidence counts only if its result came back before the claim and the agent had finished before the claim. A claim that only passes on an agent's report, with no command result behind it anywhere, stays UNVERIFIED with its own reason. Try it on the synthetic fixtures:

```bash
donediff audit tests/fixtures/sessions/proj/s2-wf.jsonl
donediff audit tests/fixtures/sessions/proj/s4-relay.jsonl
```

The details (layout, ordering rules, relayed claims, performance numbers) are in [docs/delegation.md](docs/delegation.md).

## How verification works

Every claim gets one of four verdicts, decided by evidence in this order: the transcript's own tool-call results (exit codes, output), then your filesystem and the project's git state.

| Verdict | Meaning |
|---|---|
| ✓ **VERIFIED** | A relevant check ran before the claim, succeeded, and no code was edited between the check and the claim. |
| ◐ **STALE** | The check passed, but the agent edited code *afterwards* and claimed success without re-running it. |
| ? **UNVERIFIED** | The agent never ran anything that could back the claim. |
| ✗ **CONTRADICTED** | The most recent relevant check failed, or the claimed artifact doesn't exist. |

| Claim | Evidence required |
|---|---|
| "tests pass" | A test runner actually ran (pytest, jest, vitest, go test, cargo test, ...) and succeeded, *after* the last code edit |
| "build is clean" | A build command ran and succeeded |
| "lint/typecheck passes" | eslint/ruff/mypy/tsc/pyright ran and succeeded |
| "12 of 12 tests pass", "12/12 tests" | A passing test run whose output shows 12 passed; a different count is CONTRADICTED |
| "created / edited / updated `file.py`" | A Write/Edit call for that file exists in the transcript, and the file is on disk. With git: the file is in a commit made during the session or in the working-tree changes, or it is CONTRADICTED ("names a file the diff does not touch") |
| "committed" | The git command actually ran and didn't error. With git: a commit made during the session exists, and when the transcript's `git commit -m` message is known, one has that subject; none found is CONTRADICTED |
| "pushed" | The git command actually ran and didn't error. With git: the session's newest commit is on a remote-tracking branch (local refs only, never fetched) |
| "committed as `a1b2c3d`" (a commit id) | With git: `git cat-file` finds the commit, or it is CONTRADICTED. Without git: a commit command in the transcript printed that id |
| "deployed", "is live", "shipped to production" | A deploy command ran and succeeded: `wrangler deploy`, `vercel deploy`/`--prod`, `netlify deploy`, `gh release create`, `twine upload`, `npm publish`, `rsync`/`scp` to a host. None ran: UNVERIFIED |
| "report at / saved to / wrote `out/report.md`" | The path was written in the transcript (a Write/Edit call, a `>` redirect, `-o`, `Out-File`), and with the disk check on it exists |
| "done / fixed / working" | *Some* check (test/build/typecheck) ran after the final edit |

When one sentence holds two claims ("All tests pass and I committed the changes"), each finding quotes the phrase it judged, not the whole sentence.

The exit code is the primary signal, but output is parsed too, so `pytest || true` reporting `1 failed` is still caught as a failure. A command counts by the program each pipeline stage starts: `cd app && npm test | tail` runs the tests, `grep -rn pytest src` does not.

### Git state

With the disk check on (the default; `--no-disk-check` turns it off) and the session's project directory a git work tree, donediff reads the repository to check commit, push, commit-id and file claims. When git is missing, the folder is not a repository, or a git call fails, the claim keeps its transcript verdict and the evidence says `git state not checked (<reason>)`.

The repository is treated as untrusted. donediff runs `git -C <dir> --no-pager` as an argument list (no shell), only read-only commands (`rev-parse`, `config --get-regexp`, `log`, `show --name-only`, `status --porcelain`, `branch -r`, `cat-file -e`), each with a timeout and an output cap, and on every call turns off what a repository's config could use to run a program: fsmonitor, hooks, external diff and textconv, signature checks, every clean/smudge filter the config names, and all network transports. Nothing is fetched and the index is not rewritten. Paths named in claims are resolved inside the project directory only.

Transcript paths that point at a network share or device (`\\host\share`, `//host/share`) are never opened, so the disk check for those claims is reported as not checked. Reports mask common credential shapes (tokens, keys, passwords, authorization headers), but masking is pattern based: review a report before you share it.

### Gaming signals

Independent of claims, the auditor scans for changes that make checks pass by weakening them:

- **Weakened assertions**, an edit to a test file that removes more assertions than it adds
- **Added skips**, `@pytest.mark.skip`, `it.skip`, `#[ignore]`, `t.Skip()` added to an existing test
- **Swallowed failures**, `npm test || true`, `pytest; exit 0`, `--passWithNoTests` (only on a test, build, lint or type check; `ls dir || true` is not flagged)
- **Masked exit codes**, a check piped into a pager or filter (`pytest | tail`, `npm test | grep`, `| Select-Object`), or followed by other commands (`pytest; git status`), so the call reports another command's exit code; not raised when the real status is kept (`set -o pipefail` before it, `${PIPESTATUS[0]}`, `$?` or `exit $LASTEXITCODE` after it). Such a run backs a claim only when its output shows a pass marker (`5 passed`, `# fail 0`, `test result: ok`); otherwise the claim is UNVERIFIED. Version probes, listings and dry runs (`pytest --version`, `--collect-only`, `make -n`) are neither evidence nor flagged.
- **Bypassed hooks**, `git commit --no-verify`
- **Deleted/emptied test files**

These are signals, not convictions, every one points at the exact event so you can judge for yourself. Signals found in a sub-agent or workflow transcript say which one.

### donediff score

One number for how much of what the agent said it backed up: claims weighted by verdict (verified 1.0  -  stale 0.5  -  unverified 0.25  -  contradicted 0), gaming signals subtract on top (high -15  -  medium -8), each kind charged once per audit however often it repeats (every occurrence is still listed). `--fail-under N` turns it into a CI gate.

A session with no checkable claims has no score ("n/a"). With `--fail-under`, that fails the gate too: donediff exits 3 with a one-line reason on stderr, so a session that says nothing checkable cannot pass by silence. Without `--fail-under` the exit code stays 0.

| Exit code | Meaning |
|---|---|
| 0 | The audit ran (and, with `--fail-under N`, the score is N or more) |
| 1 | The score is below `--fail-under N` |
| 2 | Transcript not found, or bad arguments |
| 3 | `--fail-under` was given and the session has no score |

## Supported agents

- **Claude Code**, reads the JSONL transcripts in `~/.claude/projects/` directly. No setup.

The parser is isolated in [`donediff/parser.py`](donediff/parser.py); adapters for other agents that persist transcripts (Codex CLI, OpenCode, Gemini CLI) are the roadmap's top item. PRs welcome.

## Honest limitations

- Claim extraction is regex-based and English-only. It errs toward precision (hedged/conditional/negated sentences are excluded), but it will miss creatively-phrased claims and can still misread odd sentences.
- Projects with no test suite will show "done" claims as UNVERIFIED, that's accurate (there *were* no receipts), but it means the score is most meaningful on projects with checks the agent can run.
- A passing test run proves the suite passed, not that the suite is any good.
- Which run backs a claim is chosen by claim type and time, not by project: in a session that works on several repositories, a test run in one can back or contradict a claim about another. Relayed claims are narrowed to the agents whose results came back in that turn; other claims see every agent that had finished. Staleness is scoped: only edits inside the folder the check ran in make it stale.
- An agent counts as finished when its transcript has no later record, so an idle background agent looks finished. A test run meant to fail (mutation testing) looks like a failure.
- A shell command sent to the background counts only from its completion notice, which has an exit code but no output; a result read later with BashOutput or TaskOutput is not used.
- When a pipe or a later command hid a check's exit code, the claim needs a pass marker from a fixed list (`N passed`, `# fail 0`, `test result: ok`, `OK`, `ok pkg`, `Found 0 errors` and similar). A runner that prints none of them leaves the claim UNVERIFIED.
- The shell reader is a small scanner, not a shell: tests run inside a script (`bash ci.sh`), an alias or `eval` are not seen.
- Git is read only with the disk check on, and only the state it is in now, not at the moment of the claim. A "session commit" is one whose committer time falls inside the session's time window (with two minutes either side), on a local branch or HEAD.
- "Pushed" is judged from local remote-tracking refs only; donediff never fetches. A push by URL, or to a remote with no fetch refspec, does not update those refs, and a repository with none is reported as not checked.
- A file git ignores, or one outside the project directory, is not judged by git. A file named only by its name ("created `cache.py`") matches any path in the project with that name.
- The new claim words are a short English list ("deployed", "is live", "shipped to production", "N of N pass", "N/N tests", "committed as <id>", "report at / saved to / wrote <path>", "edited / updated / modified `<file>`"). The deploy check knows only the commands in its table; a deploy any other way stays UNVERIFIED.

## Part of the agent accountability suite

Five zero-dependency tools that audit a finished agent session from five angles:

- **donediff**, did the agent's claims ("tests pass") match reality?
- [agent-leaks](https://github.com/0xelitesystem/agent-leaks), did it leak secrets into the transcript?
- [agent-blast-radius](https://github.com/0xelitesystem/agent-blast-radius), what irreversible actions did it take?
- [agent-rules](https://github.com/0xelitesystem/agent-rules), did it follow your `CLAUDE.md`?
- [agent-cost](https://github.com/0xelitesystem/agent-cost), where did the tokens and money go?

## Roadmap

- [ ] Adapters: Codex CLI, OpenCode, Gemini CLI session formats
- [ ] `donediff watch`, live-tail the active session, flag claims as they happen
- [ ] Claude Code Stop-hook integration: auto-audit every session on completion
- [ ] Optional LLM-assisted claim extraction (`--llm`) for non-template phrasing
- [ ] Re-run mode: actually re-execute the detected test command now and compare

## Development

```bash
git clone https://github.com/0xelitesystem/donediff
cd donediff
pip install -e .[dev]
pytest

# time an audit of a synthetic 2,000-transcript, 1 GB session (delete the folder after)
python benchmarks/bench_delegated.py /tmp/donediff-bench --files 2000 --mb 1024
```

## More

Part of a catalog of single-file browser tools and plain-language references, all MIT licensed and dependency-free: [0xelitesystem.github.io](https://0xelitesystem.github.io/). Built by [elitesystem.ai](https://elitesystem.ai).

## License

[MIT](LICENSE)
