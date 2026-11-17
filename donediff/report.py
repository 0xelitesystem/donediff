"""Render an AuditResult: ANSI terminal report, Markdown, or JSON."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from . import __version__
from .models import AuditResult, Finding, GamingSeverity, GamingSignal, Session, Verdict
from .redact import redact_secrets

# Control characters that a terminal would act on instead of showing:
# every C0 (newline and tab too: each rendered field is one line, so a
# newline in a file name or cwd from the log cannot forge a report line,
# SEC-237), DEL, C1, and bidi overrides/isolates.
_UNSAFE_CHARS = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)


def _escape_char(match: re.Match) -> str:
    code = ord(match.group())
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def _safe(text: str) -> str:
    """Show transcript text verbatim, with control codes made visible and inert."""
    return _UNSAFE_CHARS.sub(_escape_char, text)


# A home folder at the start of a path: <drive>:\\Users\\<name> (either slash),
# the same drive seen from WSL (/mnt/<drive>/Users/<name>), MSYS or Git Bash
# (/<drive>/Users/<name>) and Cygwin (/cygdrive/<drive>/Users/<name>),
# /home/<name> or /Users/<name>; "Users" and the drive in any case. Matched
# by pattern, not only against this machine's home: the cwd and commands come
# from the log, which may have been written on another machine (SEC-220).
_HOME_PREFIX = re.compile(
    r"(?<![\w.\\/:-])(?:[a-z]:[\\/]+users[\\/]+|(?:/mnt|/cygdrive)?/[a-z]/users/"
    r"|/home/|/users/)[^\\/\s\"'`|<>]+", re.IGNORECASE)


def _own_home() -> str:
    try:
        return os.path.expanduser("~")
    except (KeyError, RuntimeError):
        return ""


def home_as_tilde(text: str) -> str:
    """The text with each home-folder prefix shown as ~, so a JSON or Markdown
    report shared in CI or an issue does not show the home folder (SEC-220)."""
    if not text:
        return text
    home = _own_home().rstrip("\\/")
    if len(home) > 3:  # this machine's home, wherever it lives, in either slash style
        for form in {home, home.replace("\\", "/")}:
            text = re.sub(re.escape(form) + r"(?=[\\/]|$)", "~", text,
                          flags=re.IGNORECASE)
    return _HOME_PREFIX.sub("~", text)


def _shown(text: str, full_paths: bool) -> str:
    """Text from the log as a report shows it: credentials masked (SEC-235),
    home folders as ~ unless full_paths."""
    if not isinstance(text, str):
        return text
    text = redact_secrets(text)
    return text if full_paths else home_as_tilde(text)


_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_CYAN = "\x1b[36m"

_VERDICT_STYLE = {
    Verdict.VERIFIED: (_GREEN, "✓", "VERIFIED"),
    Verdict.STALE: (_YELLOW, "◐", "STALE"),
    Verdict.UNVERIFIED: (_YELLOW, "?", "UNVERIFIED"),
    Verdict.CONTRADICTED: (_RED, "✗", "CONTRADICTED"),
}

_SEVERITY_STYLE = {
    GamingSeverity.HIGH: (_RED, "HIGH"),
    GamingSeverity.MEDIUM: (_YELLOW, "MED"),
    GamingSeverity.LOW: (_DIM, "LOW"),
}


def scope_line(session: Session) -> str:
    """Which transcripts the evidence came from, and lines skipped as unreadable."""
    line = _scope(session)
    if session.unrecognised:
        line += f"; {session.unrecognised} unrecognised record(s) skipped"
    return line


def _scope(session: Session) -> str:
    if session.main_only:
        return "evidence from the main transcript only (--main-only)"
    scan = session.scan
    if not session.sources:
        return "evidence from the main transcript; no sub-agent or workflow transcripts found"
    return (f"evidence from main + {scan.subagents} sub-agent and "
            f"{scan.workflow_agents} workflow-agent transcripts "
            f"({scan.bytes / 1_048_576:.1f} MB read in {scan.seconds:.1f} s)")


def _signal_text(signal: GamingSignal) -> str:
    if signal.source == "main":
        return signal.description
    return f"[{signal.source}] {signal.description}"


_SHOWN_PER_KIND = 5


def _shown_signals(signals: list[GamingSignal]):
    """Signals grouped by kind, at most a few of each; (None, n) marks n hidden."""
    kinds: dict[str, list[GamingSignal]] = {}
    for signal in signals:
        kinds.setdefault(signal.kind, []).append(signal)
    for group in kinds.values():
        for signal in group[:_SHOWN_PER_KIND]:
            yield signal, 0
        if len(group) > _SHOWN_PER_KIND:
            yield None, len(group) - _SHOWN_PER_KIND


def _colors_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _paint(text: str, *styles: str, enabled: bool = True) -> str:
    if not enabled or not styles:
        return text
    return "".join(styles) + text + _RESET


def render_terminal(result: AuditResult, color: bool | None = None) -> str:
    color = _colors_enabled() if color is None else color
    session = result.session
    lines: list[str] = []
    out = lines.append

    title = _safe(redact_secrets(session.slug or Path(session.path).stem))
    out("")
    out(_paint("  donediff", _BOLD, _CYAN, enabled=color)
        + _paint(", claims vs. reality", _DIM, enabled=color))
    out(_paint(f"  session {title} · {len(session.events)} events"
               + (f" · {_safe(redact_secrets(session.cwd))}" if session.cwd else ""),
               _DIM, enabled=color))
    out(_paint(f"  {_safe(scope_line(session))}", _DIM, enabled=color))
    out("")

    if result.score is None:
        out("  no checkable claims found in this session, nothing to audit.")
        out("")
        return "\n".join(lines)

    score_style = _GREEN if result.score >= 80 else (
        _YELLOW if result.score >= 60 else _RED)
    out(f"  {_paint('DONEDIFF SCORE', _BOLD, enabled=color)}  "
        + _paint(f"{result.score}/100 ({result.grade})", _BOLD, score_style,
                 enabled=color))
    counts = result.counts()
    out(_paint(
        f"  {counts['verified']} verified · {counts['stale']} stale · "
        f"{counts['unverified']} unverified · {counts['contradicted']} contradicted"
        + (f" · {len(result.gaming_signals)} gaming signal(s)"
           if result.gaming_signals else ""),
        _DIM, enabled=color))
    out("")

    out(_paint("  CLAIMS", _BOLD, enabled=color))
    for finding in result.findings:
        style, symbol, label = _VERDICT_STYLE[finding.verdict]
        out(f"  {_paint(symbol + ' ' + label.ljust(12), style, enabled=color)}"
            f" “{_safe(redact_secrets(finding.claim.quote)[:110])}”")
        out(_paint(f"    └─ {_safe(redact_secrets(finding.evidence))}", _DIM, enabled=color))
    out("")

    if result.gaming_signals:
        out(_paint("  GAMING SIGNALS", _BOLD, enabled=color))
        for signal, more in _shown_signals(result.gaming_signals):
            if signal is None:
                out(_paint(f"    ... and {more} more of this kind (all in --json)",
                           _DIM, enabled=color))
                continue
            style, label = _SEVERITY_STYLE[signal.severity]
            out(f"  {_paint('⚠ ' + label.ljust(5), style, enabled=color)}"
                f" {_safe(redact_secrets(_signal_text(signal)))}")
        out("")

    return "\n".join(lines)


def _finding_dict(finding: Finding, full_paths: bool = False) -> dict:
    return {
        "type": finding.claim.type.value,
        "quote": _shown(finding.claim.quote, full_paths),
        "sentence": _shown(finding.claim.sentence or finding.claim.quote, full_paths),
        "detail": _shown(finding.claim.detail, full_paths),
        "verdict": finding.verdict.value,
        "evidence": _shown(finding.evidence, full_paths),
        "claim_event": finding.claim.event_index,
        "evidence_event": finding.evidence_index,
        "evidence_source": finding.evidence_source,
        "relayed": finding.relayed,
    }


def render_json(result: AuditResult, full_paths: bool = False) -> str:
    """The JSON report. Home folders in paths and quoted commands show as ~
    unless full_paths (--full-paths); the keys are the same either way."""
    scan = result.session.scan
    return json.dumps({
        "version": __version__,
        "transcript": _shown(result.session.path, full_paths),
        "session_id": result.session.session_id,
        "cwd": _shown(result.session.cwd, full_paths),
        "scope": {
            "main_only": result.session.main_only,
            "subagent_transcripts": scan.subagents,
            "workflow_agent_transcripts": scan.workflow_agents,
            "unrecognised_records": result.session.unrecognised,
            "bytes": scan.bytes,
            "seconds": round(scan.seconds, 3),
        },
        "score": result.score,
        "grade": result.grade,
        "counts": result.counts(),
        "claims": [_finding_dict(f, full_paths) for f in result.findings],
        "gaming_signals": [{
            "kind": s.kind,
            "severity": s.severity.value,
            "description": _shown(s.description, full_paths),
            "event": s.event_index,
            "source": s.source,
        } for s in result.gaming_signals],
    }, indent=2)


def render_markdown(result: AuditResult, full_paths: bool = False) -> str:
    """The Markdown report; home folders show as ~ unless full_paths, as in JSON."""
    lines = [
        "# donediff audit",
        "",
        f"- **Transcript:** `{_safe(Path(result.session.path).name)}`",
        f"- **Project:** `{_safe(_shown(result.session.cwd, full_paths) or 'unknown')}`",
        f"- **Score:** {result.score if result.score is not None else 'n/a'}"
        f"/100 ({result.grade})",
        f"- **Scope:** {_safe(scope_line(result.session))}",
        "",
        "## Claims",
        "",
        "| Verdict | Claim | Evidence |",
        "|---|---|---|",
    ]
    for finding in result.findings:
        _, symbol, label = _VERDICT_STYLE[finding.verdict]
        quote = _safe(_shown(finding.claim.quote, full_paths)).replace("|", "\\|")
        evidence = _safe(_shown(finding.evidence, full_paths)).replace("|", "\\|")
        lines.append(f"| {symbol} {label} | {quote} | {evidence} |")
    if result.gaming_signals:
        lines += ["", "## Gaming signals", ""]
        for signal, more in _shown_signals(result.gaming_signals):
            if signal is None:
                lines.append(f"- ... and {more} more of this kind (all in the JSON report)")
                continue
            text = _safe(_shown(_signal_text(signal), full_paths))
            lines.append(f"- **{signal.severity.value.upper()}**: {text}")
    lines.append("")
    return "\n".join(lines)
