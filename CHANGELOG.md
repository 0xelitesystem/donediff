# Changelog

## 0.5.0 (unreleased)

donediff now reads more coding agents than Claude Code.

### Added

- Transcript readers, picked with `--agent` (`auto` by default, which keeps
  Claude Code first):
  - Codex CLI: rollout files under `$CODEX_HOME` or `~/.codex/sessions`
    (`.jsonl.zst` too on Python 3.14 and later), including the code mode of
    Codex CLI 0.169, where the model calls one `exec` tool with a script.
    The script is read as text and never run; only plain
    `tools.<name>(...)` calls are taken, and any other use is counted as an
    unrecognised record.
  - opencode: its SQLite database (`$OPENCODE_DB`, or `opencode/opencode.db`
    under `$XDG_DATA_HOME` or `~/.local/share`), both table layouts.
  - Gemini CLI: chat files under `~/.gemini/tmp`, replayed following Gemini
    CLI's own loading rules (rewinds and patches included), and the older
    one-file `.json` sessions.
  - Qwen Code: chat files under `~/.qwen/projects` (and the older
    `tmp/<id>/chats`), following the active history by Qwen Code's own
    resume rules.
  - Cline: session files under `~/.cline/data/sessions`.
  - Goose: its `sessions.db`, schema version 18 only.
  - A reader listed here is built from that agent's published code and file
    formats and tested on sample sessions in that shape. It is not a
    statement that donediff has been tested on real runs of that agent.
- An exit code is taken only from the field where each agent records one.
  Where an agent logs a status and no exit code, the finding says "the log
  has a status only" and never "exit code".
- The JSON report's `scope` object has `unrecognised_records`: the number of
  records a reader could not read and skipped, the same count the terminal
  and Markdown scope line already showed.

### Changed

- One evidence rule for every reader: a run, a write or an edit backs a
  claim only when the log holds an explicit success record for it (an exit
  code of 0 read from a structured field, or the agent's own success
  status). Stricter than 0.4.0:
  - a shell run with no result in the log is UNVERIFIED, never "exit 0";
  - a task-done claim after a run with no result, a status-only run or a
    declined run is UNVERIFIED;
  - a commit id printed by a run with no success record backs nothing;
  - a shell write still running, or with no success record, is no evidence
    that the file was written;
  - skipped, timed out, not recorded, still waiting and rewound calls are no
    evidence;
  - a Codex patch counts only when Codex recorded it as applied; otherwise
    its outcome is unknown;
  - a printed "exit code 0" on a failed Claude Code result is not taken.
- A command the user declined, cancelled or the agent skipped never ran, so
  it proves nothing either way: a "tests pass" claim after it is UNVERIFIED,
  no longer CONTRADICTED. CONTRADICTED needs a real non-zero exit or failure
  output. (Goose logs no record of a cancel, so a cancelled Goose command
  still reads as the error it logged.)
- Gaming signals on a rewound call end with "(rewound)".
- JSON and Markdown reports show a home folder as `~` (for example
  `/home/<name>/project` becomes `~/project`) in the transcript path, the
  project folder, evidence, gaming-signal text and each claim's quote,
  sentence and detail, so a report attached to a CI run or an issue does not
  show the home folder in those fields. Other text, such as a folder name or a
  name inside a command, is not changed: read a report before you share it.
  The JSON keys are the same; only
  these values are shorter. `--full-paths` keeps the full paths. The terminal
  report is unchanged.
- `list` prints a heading row (ID MODIFIED SIZE PROJECT); scripts that parse
  it should skip line 1.

### Fixed

- An id prefix matching more than one session is refused with the matches
  listed (exit 2) instead of auditing one of them.
- A report file that cannot be written exits 2 with one line (was a
  traceback and exit 1).
- Unreadable lines count in `unrecognised_records`, and a file that is not a
  transcript says so.
- `--main-only` is reported only when it was passed.
- `--limit` and `--fail-under` values out of range are refused.
- SEC-203: a relative `$OPENCODE_DB` joined onto a network `$XDG_DATA_HOME`
  is refused; the final path is checked, not only its parts.
- SEC-204: opening an opencode database in WAL mode no longer creates files
  beside it.
- SEC-207: a log line holding deeply nested JSON no longer crashes the
  audit; it is counted as unreadable.
- SEC-208: a write or edit that failed, or that has no recorded result, no
  longer backs a "created" or "report at" claim. It still makes an earlier
  check stale.
- SEC-209: a Qwen Code run that did not pass never reads as passing, even
  when the command printed its own exit line.
- SEC-210: a Qwen Code rewind no longer hides a later edit or failure; a
  rewound run or write is never evidence.
- SEC-211: Qwen Code exit codes are no longer read from text for a cancelled
  run.
- SEC-212: a Cline edit or patch with no recorded outcome is unknown, not a
  success.
- SEC-213: a command inside a Codex code-mode script has an unknown outcome,
  and a write in a script that failed or was stopped is no evidence.
- SEC-214: a Goose tool call skipped in chat mode never ran: it is declined,
  not a success.
- SEC-215: when Goose rewrites a whole conversation, no pass logged before
  the rewrite is proof.
- SEC-216: a relative or drive-relative `APPDATA` or `XDG_DATA_HOME` is
  ignored by the Goose and opencode readers, as if unset, and a data folder
  that is still not absolute (a relative home) is refused, so neither reads
  from the working folder.
- SEC-217: Goose metadata over a size cap is skipped and counted, and one
  session's read is bounded; no pass logged past the bound is proof.
- SEC-218: a Goose or opencode database with both side files (`-wal` and
  `-shm`) is read from a private temporary copy, so nothing beside the
  original is ever written.
- SEC-219: a Goose tool name with no extension prefix is not taken as the
  developer extension's shell, write or edit.
- SEC-218 follow-up: the Goose and opencode readers choose how to open a
  database from the side files present: a `-wal` with a `-shm` is read from
  a private copy and a lone `-wal` is refused, whatever the database header
  says.
- SEC-220 follow-up: home folders seen from WSL, MSYS and Cygwin
  (`/mnt/<drive>/Users/<name>`, `/<drive>/Users/<name>`,
  `/cygdrive/<drive>/Users/<name>`), and `users` in any case, are also shown
  as `~`.
- SEC-221: the hidden exit code check sees negation, any last pipe stage
  that is not a check, `echo $?`, wrapped shells, a background `&`,
  `exit 256`, `exit 00`, `exit $((0))` and words inside `||` tails.
- SEC-222: list, plan and skip forms (`setup-plan`, fixtures,
  `-DskipTests`, `-x test`, `ctest -N`, `-- --list`, option variables) are
  not checks; output saying no test ran is no proof; `--exit-zero` is a
  swallowed failure.
- SEC-223: a file claim that names a folder needs a write to that path; a
  missing path inside the project is not verified by a write elsewhere.
- SEC-228: a number read from a log or a claim is capped at 18 significant
  digits, so one over 4300 digits no longer crashes a reader; an overlong
  exit code reads as a failure, an overlong count as no count.
- SEC-229: the Claude Code reader replaces invalid UTF-8 instead of stopping
  with a traceback; the bad line is counted as unreadable.
- SEC-230: Goose and opencode refuse a session table that is virtual or has
  a generated column; size caps count bytes; opencode has Goose's caps
  (16 MiB per row, 500,000 rows and 128 MiB per session).
- SEC-231: in the Claude Code reader a transcript line over 32 MiB is read
  past in pieces and counted as unrecognised; a transcript over 512 MiB is
  refused, and a sub-agent transcript over it is counted and not read.
- SEC-232: in a background command's completion notice any non-zero exit
  code wins, and a failed notice never reads as exit 0.
- SEC-233: writes in a shell command are read stage by stage with quoted
  text and comments blanked; `-o` counts as an output file only for programs
  that take one.
- SEC-234: Goose skip text counts only with no error flag and exit 0; skip
  or decline text with failures in the output is a failed run.
- SEC-235: claim text is masked for secrets before claims are read from it,
  and every report masks quotes, evidence, signals, the working folder and
  the session slug.
- SEC-236: masking also covers Stripe test and webhook keys, Slack app,
  refresh and webhook URLs, Google OAuth, JWT, npm, Hugging Face and
  SendGrid tokens, and PEM private key bodies; its limits are documented.
- SEC-237: a newline or tab from the log is shown escaped in terminal and
  Markdown reports.
- SEC-238: Markdown report text is inert outside code spans (no raw HTML,
  autolinks, links or images, reference names included); code spans are
  fenced past any backtick; pipes are escaped with their backslashes.
- SEC-239: Markdown noise is stripped only outside code spans and an
  underscore inside a word stays, so claims about `my_module.py`,
  `__init__.py` or `run_1.md` are no longer dropped.
- SEC-240: `--md` writes the report through a temporary file beside the
  target and shows text it cannot encode as `?`; a failed write leaves no
  file and exits 2 with one line.
- SEC-241: a JSON text over 1 MiB with more than 1,000,000 values is refused
  as unreadable.
- SEC-242: the Goose deleted-message check reads the message ids once, then
  searches each gap, instead of one table scan per gap.
- SEC-243: a run's first private database copy first removes copy folders a
  killed run left behind (only folders holding nothing but the copy and its
  side files, no links, untouched for an hour); on Windows with Python 3.10
  to 3.12 the copy folder is made owner-only, and no copy is made if that
  fails.
- SEC-244: behind a hidden exit code a pass marker counts only through
  filters that pass lines unchanged (`tail`, `head`, `tee` and the like),
  not `grep`, `sed` or `awk`.
- SEC-245: claims are read with invisible characters dropped and look-alike
  letters in mixed-script words folded, with a LOW `hidden_characters`
  signal; reports escape zero-width, filler, separator and tag characters.
- SEC-246: Claude Code decline text counts only with no exit code anywhere,
  no failing Bash record and no long output after it.
- SEC-247: path checks read every link's target before anything follows it;
  network targets and `/net` and `/Network` automount paths are refused.

### Security

- Every new reader opens agent logs read-only, starts no other program and
  makes no network call. Network and device paths are refused. The Codex,
  Gemini CLI, Qwen Code and Cline readers do not follow symlinks or Windows
  junctions in the agents' folders, and the opencode and Goose readers do not
  open a linked database or a database in a linked folder.
- SEC-224, SEC-225, SEC-226, SEC-227 and SEC-248, listed under 0.4.2, are
  also fixed in 0.5.0's code: git is started only from an absolute PATH entry
  that is not the current folder and does not lie inside the project or its
  work tree, never as a `.bat` or `.cmd`; a
  project inside a bare repository is refused and git runs with
  `safe.bareRepository=explicit`; every git transport is turned off; git's
  log and refs are read on NUL separators, with full commit ids and
  `--end-of-options`; config-defined hooks are turned off by name, and a
  hook name that cannot be turned off is refused. A commit id named in a
  claim may be 7 to 64 hex digits, so SHA-256 repositories are read.

## 0.4.2

### Security

Upgrade if you run donediff with the disk check on (the default) in a
folder you did not create, such as a cloned repository. In 0.4.1 and
earlier, a repository built to attack it could:

- On Windows, run a program named `git.exe` placed in the folder donediff
  was started from, instead of your installed git. donediff now starts only
  a git found in the absolute folders on your PATH.
- Make git read objects from a network share named by a bare repository
  hidden among the project's files. On Windows, that connection can send
  your Windows sign-in (NTLM) to that host. donediff now refuses a project
  inside a bare repository.
- With git older than 2.44, run a command named in the repository's own
  config through a lazy fetch, despite donediff's network setting. donediff
  now turns off every git transport with a setting the repository's config
  cannot override.
- Through a crafted commit message, pass git an option that writes a file
  anywhere you can write, or list a commit that does not exist as one made
  during the session. donediff now reads git's log with NUL separators,
  accepts only full commit ids, and passes no value read from the
  repository to git as an option.
- On git 2.50 or newer, config-defined hooks were not turned off (no
  command donediff runs fires one). donediff now turns them off by name.

## 0.4.1

### Fixed

- A command whose result never reached the transcript (the session was cut off
  or the run was killed) was marked VERIFIED as "succeeded (exit 0)". It is now
  UNVERIFIED with the reason "no result for this run in the log", so it lowers
  the score and counts against `--fail-under`. Runs that have a result keep
  their verdicts.

## 0.4.0

donediff now reads the project's git state.

### Added

- Git checks, on when the disk check is on and the session's project directory is
  a git work tree:
  - "committed": a commit made during the session exists on a local branch or
    HEAD, and when the transcript's `git commit -m` message is known, one has that
    subject. None found is CONTRADICTED.
  - "pushed": the session's newest commit is on a remote-tracking branch, read
    from local refs only. Nothing is fetched.
  - A file a claim says was created or edited is in a session commit
    (`git show --name-only`) or in the working-tree changes (`git status
    --porcelain`). In neither, the claim is CONTRADICTED ("names a file the diff
    does not touch").
  - A commit id in a claim is found with `git cat-file -e`, or the claim is
    CONTRADICTED.
  - When git is missing, the folder is not a repository or a git call fails, the
    claim keeps its transcript verdict and says `git state not checked (<reason>)`.
- Git is run read-only on a repository treated as untrusted: an argument list,
  `git -C <dir> --no-pager`, a timeout and an output cap on every call, and
  fsmonitor, hooks, external diff, textconv, signature checks, every
  clean/smudge filter the config names and all network transports turned off.
  Paths named in claims are resolved inside the project directory only.
- New claim words: "deployed", "is live", "shipped to production" (backed by a
  successful deploy command: wrangler, vercel, netlify, gh release create, twine
  upload, npm publish, rsync or scp to a host); "N of N pass" and "N/N tests"
  (the test run's output must show that count); a commit id after "commit" or
  "committed as" (found in git, or printed by a commit command in the
  transcript); "report at", "saved to" and "wrote" a path (written in the
  transcript, and on disk with the disk check on); "edited", "updated" and
  "modified" a file.
- `--fail-under N` now fails a session with no score: exit code 3 and a one-line
  reason on stderr. Without `--fail-under` the exit code is unchanged.

### Changed

- When one sentence holds two claims, each finding quotes the phrase it judged,
  not the whole sentence. The JSON report adds a `sentence` field with the full
  sentence.
- `--help` names the "donediff score" and lists the exit codes.
- The tool now starts one other program: `git`, for the checks above. With
  `--no-disk-check` it starts none.
- The file-on-disk check looks only inside the session's project folder (links
  followed). A path outside it, or a network or device path, is not checked and
  the finding says why. A repository whose config, or a file the config
  includes, points at a network path is not read with git.

### Removed

- The `receipts` command, deprecated in 0.3.0. Use `donediff`.

### Security

- Hostile repositories are refused before git reads them: a filter that cannot be
  turned off (a filter name holding `=`), and a config, included config,
  worktree config or alternates chain that points at a network or device path.

## 0.3.0

The tool is now called donediff (formerly agent-receipts).

### Changed

- Package, module and command renamed: `pip install donediff`, `import donediff`,
  `donediff audit` and `donediff list`. The repository moved to
  github.com/0xelitesystem/donediff; the old address redirects.
- The `receipts` command still works for this release only. It prints a one-line
  notice on stderr and then runs `donediff`.

### Removed

- Removed an unused internal helper; the tool starts no other program.

## 0.2.0

Sessions that delegate are now audited as a whole.

### Added

- `receipts audit` finds the session's sub-agent transcripts
  (`<session>/subagents/agent-*.jsonl`) and workflow agent transcripts
  (`<session>/subagents/workflows/wf_*/agent-*.jsonl`) next to the main
  transcript. A test, build, lint, type check, commit or file write in any
  of them counts as evidence, and every evidence line names where it ran:
  `[in main]`, `[in sub-agent <id> (<description>)]` or
  `[in workflow <run id>, agent <id> (<description>)]`.
- Honest ordering across files: delegated evidence counts only when its
  result came back before the claim and its agent had finished before the
  claim. A claim made in the turn that a delegated result came back in is
  checked against that agent (or every agent of that workflow) and the
  main session only.
- A claim that only passes on an agent's report ("the agent says 900 tests
  pass") with no command result behind it is UNVERIFIED with the reason
  "relayed from an agent's report, no command result seen". JSON findings
  carry `relayed` and `evidence_source`.
- `--main-only` keeps the v0.1 behaviour (main transcript only).
- New gaming signal `masked_exit_code` (MEDIUM): a test, build, lint or
  type check piped into a pager or filter (`| tail`, `| head`, `| grep`,
  `| Select-Object` and similar), which reports the filter's exit code
  instead of the check's, or followed by other commands, which report
  their own. Not raised when the command keeps the real status
  (`set -o pipefail` before it, `PIPESTATUS`, `$?` or `$LASTEXITCODE`
  read after it).
- Gaming signals are found in delegated transcripts too, labelled with
  where they happened.
- Test runners `node --test`, `deno test`, `bun test`, `mocha`, `tox`,
  `nox`, and `node <path>/build.mjs` as a build, are recognised.
- JSON report: `version`, `scope` (transcripts and bytes read, seconds),
  and `source` on each gaming signal.
- `benchmarks/bench_delegated.py`: builds a synthetic session of any size
  and times the audit.

### Changed

- Commands are read stage by stage (split at `&&`, `||`, `;`, `|` and
  newlines, outside quotes and here-doc bodies) and a stage counts only by
  the program it starts. `grep -rn pytest src` is no longer test evidence.
- `swallowed_failure` (`|| true`, `; exit 0`) is raised only when the
  command runs a test, build, lint or type check. Read-only diagnostics
  such as `ls dir || true` are no longer flagged.
- Each kind of gaming signal is charged once per audit in the score; every
  occurrence is still listed (terminal and Markdown show five per kind,
  JSON shows all).
- A passing run is STALE only for edits inside the folder it ran in (the
  call's working directory, followed through `cd`, `pushd` and
  `Set-Location`); edits to other repositories no longer count. When the
  folder cannot be told, every edit counts, as before.
- Evidence lines end with `[in <where>]`.

### Fixed before release (red-team review of the v0.2 branch)

- A shell command sent to the background counted as a passing run: its
  first result ("Command running in background with ID: ...") read as
  exit 0. It now counts only once its `<task-notification>` arrives, with
  the exit code and time from that notice, in the main session and in
  delegated transcripts alike. A notice that reports a failure
  contradicts the claim; a launch that never completes is no evidence.
  Background shell notices no longer mark a claim as relayed.
- A run whose exit code a pager or filter hid was VERIFIED whenever its
  filtered output showed no failure. It now needs a positive pass marker
  (`N passed`, `# fail 0`, `test result: ok` and similar); without one the
  claim is UNVERIFIED: "exit code hidden by a pipe and output does not show
  the result".
- A delegated result recorded as the last line of a turn counted as part
  of the next turn, which marked the next turn's claims as relayed and
  narrowed them to that agent. Prompts and deliveries are now ordered by
  their line in the main transcript.
- Any mention of `pipefail`, `PIPESTATUS` or `LASTEXITCODE` switched the
  masked-exit signal off, including `set +o pipefail` and an echo. Only
  pipefail turned on before the pipeline, or the status read after it,
  counts now.
- A check followed by `|| echo ...` or by `;` and other commands lost its
  status unnoticed. `|| <command that succeeds>` is now
  `swallowed_failure`; a check followed by non-check commands is
  `masked_exit_code` ("check followed by `git`"), with the same
  pass-marker rule for the verdict.
- Version probes, listings and dry runs (`pytest --version`,
  `--collect-only`, `make -n`, `tsc --version`) counted as test or build
  evidence and as masking. They are now neither.
- `py -3 -m pytest`, `python.exe -X utf8 -m pytest` and
  `timeout -s KILL 60 pytest` were not recognised as test runs.
- An agent whose last record had no timestamp, or was cut off, could count
  as finished before its later work. Its finish time is now the latest
  timestamp on any line, and a cut-off last line means not finished.
- An edit by an agent still running at claim time was ignored for
  staleness. Edits now count by their own time, so a parallel agent's edit
  before the claim makes an earlier pass STALE.
- Found while fixing the above: cargo's passing summary
  (`test result: ok. 9 passed; 0 failed`) read as a failure because a bare
  `FAILED` matched without regard to case; it now matches in capitals
  only. `--passWithNoTests` inside a here-doc or a quoted script, and a
  `||` inside a `( ... )` group, are no longer flagged.

### Performance

- Delegated transcripts are streamed line by line in binary and only lines
  that can matter are decoded; tool output is dropped once read. On the
  maintainer's Windows machine (warm file cache) a synthetic session of
  2,001 transcripts and 1.07 GB audits in 3.3 s with 12 MB of peak
  allocation (Python 3.14; 6.1 s with 3.10), and a real session of 657
  transcripts and 418 MB in 10.9 s.

## 0.1.0

First release: claim extraction, verification against the main transcript
and the filesystem, gaming signals, Receipts Score, terminal, Markdown and
JSON reports.
