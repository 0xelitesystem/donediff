"""donediff: audit what your coding agent claimed against what it did.

Usage:
  donediff audit <transcript.jsonl | session-id-prefix | latest> [options]
  donediff list [--project NAME] [--limit N]

Options:
  --main-only       read only the main transcript (v0.1 behaviour); by default
                    the session's sub-agent and workflow transcripts count too
  --project NAME    only consider transcripts whose project folder matches
  --json            emit machine-readable JSON instead of the terminal report
  --md FILE         also write a Markdown report to FILE
  --no-disk-check   skip filesystem and git checks (transcript evidence only)
  --fail-under N    exit 1 if the donediff score is below N, exit 3 if the
                    session has no score (CI gate)
  --no-color        disable ANSI colors
  --agent NAME      transcript reader: auto (default), claude-code, codex,
                    opencode, gemini, qwen, cline or goose (opencode and goose
                    target: a session id, the database path, or the database
                    path + "#<session id>")

Exit codes:
  0  audit ran (and, with --fail-under, the score is N or more)
  1  the score is below --fail-under N
  2  transcript not found or not a transcript, bad arguments, or a report
     could not be written
  3  --fail-under was given and the session has no score (no checkable claims)
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .claims import extract_claims
from .gaming import detect_gaming
from .models import AuditResult
from .readers import (AGENT_CHOICES, AmbiguousSession, NotATranscript, UnreadableSession,
                      not_a_transcript, select, select_for_list, short_ids)
from .report import render_json, render_markdown, render_terminal
from .score import score_audit
from .verify import verify_claims


EXIT_BELOW = 1  # score below --fail-under
_UNSAFE_ID = re.compile(r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
EXIT_NO_SCORE = 3  # --fail-under given and the session has no score

_EXIT_CODES = """exit codes:
  0  audit ran (and, with --fail-under, the score is N or more)
  1  the score is below --fail-under N
  2  transcript not found or not a transcript, bad arguments, or a report
     could not be written
  3  --fail-under was given and the session has no score (no checkable claims)"""


def run_audit(target: str, project: str | None = None, check_disk: bool = True,
              main_only: bool = False, agent: str = "auto") -> AuditResult:
    """Library entry point: audit a transcript and return the result.

    Unless main_only, the session's sub-agent and workflow transcripts are
    discovered next to it and their command results count as evidence.
    `agent` picks the transcript reader (donediff.readers).
    """
    started = time.perf_counter()
    reader = select(agent, target, project)
    path = reader.resolve(target, project)
    session = reader.parse(path)
    if not session.events and not reader.detect(path):
        raise not_a_transcript(reader, path, session.unrecognised, agent)
    session.main_only = main_only  # the scope says --main-only only when it was given
    session.scan.files = 1
    try:
        session.scan.bytes = path.stat().st_size
    except OSError:
        pass
    if not main_only:
        reader.attach_sources(session)
        session.scan.files += len(session.sources)
    session.scan.seconds = time.perf_counter() - started
    claims = extract_claims(session)
    result = AuditResult(
        session=session,
        findings=verify_claims(session, claims, check_disk=check_disk),
        gaming_signals=detect_gaming(session),
    )
    return score_audit(result)


def _cannot_write(where: str, exc: OSError) -> int:
    reason = exc.strerror or exc.__class__.__name__
    try:
        print(f"donediff: cannot write {where}: {reason}", file=sys.stderr)
    except OSError:
        pass
    return 2


def _cmd_audit(args: argparse.Namespace) -> int:
    try:
        result = run_audit(args.target, args.project, check_disk=not args.no_disk_check,
                           main_only=args.main_only, agent=args.agent)
    except (FileNotFoundError, UnreadableSession, AmbiguousSession, NotATranscript) as exc:
        print(f"donediff: {exc}", file=sys.stderr)
        return 2

    # A report that cannot be written is one plain line and exit 2, never a
    # traceback and never exit 1, which means "below --fail-under" (QA-025).
    if args.md:
        try:
            Path(args.md).write_text(render_markdown(result), encoding="utf-8")
        except OSError as exc:
            return _cannot_write(args.md, exc)
    try:
        if args.json:
            print(render_json(result))
        else:
            color = False if args.no_color else None
            print(render_terminal(result, color=color))
        if args.md and not args.json:
            print(f"  markdown report written to {args.md}\n")
        sys.stdout.flush()
    except OSError as exc:
        return _cannot_write("the report to standard output", exc)

    if args.fail_under is not None:
        if result.score is None:
            print(f"donediff: no score (no checkable claims), which fails "
                  f"--fail-under {args.fail_under} (exit {EXIT_NO_SCORE})", file=sys.stderr)
            return EXIT_NO_SCORE
        if result.score < args.fail_under:
            return EXIT_BELOW
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    reader = select_for_list(args.agent, args.project)
    found = reader.discover(args.project)
    transcripts = found[: args.limit]
    if not transcripts:
        print(f"no transcripts found under {reader.location}", file=sys.stderr)
        return 2
    # ids long enough that each names one session (QA-024), so `audit <id>` gets that row
    ids = [_UNSAFE_ID.sub("?", i) for i in short_ids(found, transcripts, reader.session_keys,
                                                  getattr(reader, "id_width", 8))]
    width = max(len(i) for i in ids)
    for path, short_id in zip(transcripts, ids):
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        size_kb = path.stat().st_size // 1024
        _, project = reader.describe(path)
        print(f"{short_id.ljust(width)}  {mtime:%Y-%m-%d %H:%M}  {size_kb:>6} KB  "
              f"{project}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="donediff",
        description="Audit what your coding agent claimed against what it actually did.",
    )
    parser.add_argument("--version", action="version", version=f"donediff {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="audit one session transcript",
                           epilog=_EXIT_CODES,
                           formatter_class=argparse.RawDescriptionHelpFormatter)
    audit.add_argument("target", help="transcript path, session-id prefix, or 'latest'")
    audit.add_argument("--project", help="filter session discovery by project name")
    audit.add_argument("--main-only", action="store_true",
                       help="ignore sub-agent and workflow transcripts (v0.1 behaviour)")
    audit.add_argument("--json", action="store_true", help="JSON output")
    audit.add_argument("--md", metavar="FILE", help="write Markdown report to FILE")
    audit.add_argument("--no-disk-check", action="store_true",
                       help="skip filesystem and git verification")
    audit.add_argument("--fail-under", type=int, metavar="N",
                       help="exit 1 if the donediff score is below N, "
                            "exit 3 if the session has no score (for CI)")
    audit.add_argument("--no-color", action="store_true", help="plain output")
    audit.add_argument("--agent", choices=AGENT_CHOICES, default="auto",
                       help="which coding agent wrote the transcript (default: auto)")
    audit.set_defaults(func=_cmd_audit)

    lst = sub.add_parser("list", help="list recent session transcripts")
    lst.add_argument("--project", help="filter by project folder name")
    lst.add_argument("--limit", type=int, default=15)
    lst.add_argument("--agent", choices=AGENT_CHOICES, default="auto",
                     help="which coding agent's sessions to list (default: auto)")
    lst.set_defaults(func=_cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows consoles often default to cp1252, which can't print ✓/✗/⚠.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
