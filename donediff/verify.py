"""Verify claims against ground truth.

Ground truth, in order of strength:
1. Tool-call results inside the transcripts: exit codes and output of
   the commands the agent, or an agent it delegated to, actually ran.
2. The filesystem and git state of the project right now.

Each claim gets one of four verdicts:
- VERIFIED:     a relevant check ran before the claim, succeeded, and no
                code was edited between the check and the claim.
- STALE:        the check succeeded, but code was edited afterwards and
                success was claimed without re-running it.
- UNVERIFIED:   nothing that could back the claim ran before it.
- CONTRADICTED: the most recent relevant check before the claim failed,
                or the claimed artifact does not exist.

Ordering across transcripts. Main-session events are ordered by their
position in the main transcript. A run in a delegated transcript backs
a claim only when its result came back before the claim (by timestamp)
and its agent had finished before the claim, so a check the main agent
could not yet know about never backs what it said. An edit is judged by
its own time only: an edit made before the claim by an agent that was
still working counts toward STALE, because the code had changed.

Background runs count from their completion notice, not from their
launch. A run whose exit code a pager or filter hid backs a claim only
when its output shows a pass marker; otherwise the claim is UNVERIFIED.

Relayed claims. A claim made in the same turn that a delegated result
came back in (or one that says "the agent reports...") passes on that
agent's report. Only the delivered agents, or every agent of a delivered
workflow, plus the main session, can back it; with no command result
there it is UNVERIFIED with its own reason.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import replace
from pathlib import Path

from .evidence import CHECK_TYPES, commit_subjects, masked_exit, stage_types
from .gitstate import GitError, GitRepo
from .models import (
    EDIT, NOTEBOOK_EDIT, WRITE, Claim, ClaimType, Event, EventKind, Finding, Session, Source, Verdict,
)
from .parser import epoch
from .redact import redact_secrets
from .shell import base_name, confined, is_remote_path, norm_path, run_directory, windows_drive

RELAYED_REASON = "relayed from an agent's report, no command result seen"
MASKED_REASON = "exit code hidden by a pipe and output does not show the result"

# Wording that passes on another agent's report.
_RELAY_WORDING = re.compile(
    r"\b(?:sub-?agents?|agents?|workflows?|workers?|verifiers?|reviewers?|auditors?)"
    r"\s+(?:says?|said|reports?|reported|claims?|claimed|confirms?|confirmed|returned)\b"
    r"|\baccording\s+to\s+the\s+(?:sub-?agent|agent|workflow|worker)",
    re.IGNORECASE,
)


def _command_failed(event: Event) -> bool:
    if event.is_error or (event.exit_code or 0) != 0:
        return True
    return event.failure_hint


def _short(command: str, limit: int = 80) -> str:
    command = " ".join(redact_secrets(command).split())
    return command if len(command) <= limit else command[: limit - 1] + "…"


def _where(event: Event) -> str:
    return "main" if event.source is None else event.source.describe()


def _how_masked(event: Event) -> str | None:
    return masked_exit(event.command) if event.command else None


def _inconclusive(event: Event) -> str | None:
    """The reason, when a pipe or a later command hid the exit code and the
    output shows no pass marker; else None."""
    how = _how_masked(event)
    if how is None or event.pass_hint:
        return None
    if how.startswith("| "):
        return MASKED_REASON
    return f"exit code hidden by `{how}` and output does not show the result"


def _no_result(run: Event) -> str:
    """The log ends (or moves on) before this run's result: no proof either way."""
    return f"no result for this run in the log: `{_short(run.command)}`"


def _no_proof(run: Event) -> str | None:
    """Why a run that did not fail is still no proof that it passed, else None:
    its exit code is hidden, the log has a status only, or it has no explicit
    success record (evidence_of_success)."""
    reason = _inconclusive(run)
    if reason:
        return f"{reason}: `{_short(run.command)}`"
    if run.exit_code is None and run.status:
        # A reader with no exit code for this run (Codex, opencode tool parts)
        # gives its status; a status alone is no proof of success. The wording
        # says "status", never "exit code" (CEO rule, D087).
        return (f"the log has a status only ({run.status}), no proof of success: "
                f"`{_short(run.command)}`")
    if not evidence_of_success(run)[0]:
        return f"the run {_write_why(run)}: `{_short(run.command)}`"
    return None


def _masked_note(event: Event) -> str:
    """Note how the run's result was read when it was not a plain exit code."""
    how = _how_masked(event)
    truncated = ", output cut at 64 KiB in the log" if event.output_truncated else ""
    if how is not None:
        where = "a pipe" if how.startswith("| ") else f"`{how}`"
        return f", exit code hidden by {where}, output shows a pass{truncated}"
    if event.background:
        return f", ran in the background, from its completion notice{truncated}"
    return truncated


class Timeline:
    """Every evidence-bearing event of a session, in one time order."""

    def __init__(self, session: Session):
        self.session = session
        self._runs: dict[ClaimType, tuple[list[tuple], list[Event]]] = {}
        entries: dict[ClaimType, list[tuple[tuple, Event]]] = {}
        edits: list[tuple[tuple, Event]] = []
        rewound: dict[ClaimType, list[tuple[tuple, Event]]] = {}
        for key, event in self._all_tool_calls():
            for claim_type in event.check_types:
                # A rewound run is never evidence for a claim, only a cap on one.
                (rewound if event.rewound else entries).setdefault(
                    claim_type, []).append((key, event))
            if event.is_file_edit():
                edits.append((key, event))
        for claim_type, items in entries.items():
            items.sort(key=lambda pair: pair[0])
            self._runs[claim_type] = ([k for k, _ in items], [e for _, e in items])
        self._rewound = {t: sorted(items, key=lambda pair: pair[0])
                         for t, items in rewound.items()}
        edits.sort(key=lambda pair: pair[0])
        self._edit_keys = [k for k, _ in edits]
        self._edits = [e for _, e in edits]
        # (index, line) of each prompt; older callers may set only prompt_starts.
        self._prompts = sorted(session.prompt_marks or
                               [(i, -1) for i in session.prompt_starts])

    def _all_tool_calls(self):
        for event in self.session.events:
            if event.kind is EventKind.TOOL_CALL:
                order = event.index if event.done_order is None else event.done_order
                yield (event.t, 0, order), event
        for n, source in enumerate(self.session.sources):
            for seq, event in enumerate(source.events):
                yield (event.t, 1, n, seq), event

    # -- the claim's view of the timeline ---------------------------------

    def claim_key(self, claim: Claim) -> tuple:
        event = self.session.events[claim.event_index]
        return (event.t, 0, claim.event_index)

    def relay_keys(self, claim: Claim) -> set[tuple[str, str]]:
        """Agents and runs whose results came back in the claim's turn.

        A delivery and the next prompt can share an event index (nothing
        happened between them), so they are ordered by line number: a
        delivery recorded before the prompt belongs to the previous turn.
        """
        pos = bisect.bisect_right(self._prompts, (claim.event_index, float("inf")))
        start = self._prompts[pos - 1] if pos else (-1, -1)
        return {d.key for d in self.session.deliveries
                if start < (d.index, d.seq) and d.index <= claim.event_index}

    def eligible(self, event: Event, claim_key: tuple,
                 restrict: set[tuple[str, str]] | None) -> bool:
        source: Source | None = event.source
        if source is None:
            return True
        if source.finished is None or source.finished > claim_key[0]:
            return False
        return restrict is None or source.matches(restrict)

    def runs_before(self, claim_type: ClaimType, claim_key: tuple,
                    restrict, after: tuple | None = None) -> list[Event]:
        """Eligible runs of a type with after < key < claim, newest first."""
        keys, events = self._runs.get(claim_type, ([], []))
        hi = bisect.bisect_left(keys, claim_key)
        lo = bisect.bisect_right(keys, after) if after is not None else 0
        return [events[i] for i in range(hi - 1, lo - 1, -1)
                if self.eligible(events[i], claim_key, restrict)]

    def last_run(self, claim_type, claim_key, restrict) -> tuple[tuple, Event] | None:
        keys, events = self._runs.get(claim_type, ([], []))
        for i in range(bisect.bisect_left(keys, claim_key) - 1, -1, -1):
            if self.eligible(events[i], claim_key, restrict):
                return keys[i], events[i]
        return None

    def edits_between(self, start: tuple, claim_key: tuple, restrict,
                      within: str | None = None) -> list[Event]:
        """Eligible edits after `start` and before the claim.

        With `within` (a folder from shell.norm_path), only edits to files
        inside it count; an edit whose path cannot be read counts.
        """
        lo = bisect.bisect_right(self._edit_keys, start)
        hi = bisect.bisect_left(self._edit_keys, claim_key)
        return [self._edits[i] for i in range(lo, hi)
                if _inside(self._edits[i], within)]

    def rewound_failure(self, claim_type: ClaimType, after: tuple,
                        claim_key: tuple) -> Event | None:
        """The newest failed run a rewind left behind, after `after` and
        before the claim (SEC-210)."""
        for key, event in reversed(self._rewound.get(claim_type, [])):
            if after < key < claim_key and event.result_seen and _command_failed(event):
                return event
        return None

    def last_edit(self, claim_key, restrict) -> tuple[tuple, Event] | None:
        """The newest edit before the claim, by anyone: whether its agent had
        finished, or was the one the claim relays, does not matter for
        whether the code changed."""
        hi = bisect.bisect_left(self._edit_keys, claim_key)
        return (self._edit_keys[hi - 1], self._edits[hi - 1]) if hi else None

    def writes_before(self, claim_key, restrict) -> list[Event]:
        hi = bisect.bisect_left(self._edit_keys, claim_key)
        return [self._edits[i] for i in range(hi)
                if self.eligible(self._edits[i], claim_key, restrict)]


def _inside(edit: Event, folder: str | None) -> bool:
    if folder is None:
        return True
    path = norm_path(edit.file_path, norm_path(edit.cwd) if edit.cwd else None)
    return path is None or path == folder or path.startswith(folder.rstrip("/") + "/")


class _Context:
    """What one claim may be checked against."""

    def __init__(self, timeline: Timeline, claim: Claim):
        self.key = timeline.claim_key(claim)
        delivered = timeline.relay_keys(claim)
        self.restrict = delivered or None
        self.relayed = bool(delivered) or bool(
            _RELAY_WORDING.search(claim.sentence or claim.quote))


def _unverified(claim: Claim, ctx: _Context, reason: str) -> Finding:
    return Finding(claim=claim, verdict=Verdict.UNVERIFIED,
                   evidence=RELAYED_REASON if ctx.relayed else reason,
                   relayed=ctx.relayed)


def _found(claim: Claim, ctx: _Context, verdict: Verdict, evidence: str,
           event: Event) -> Finding:
    where = _where(event)
    return Finding(
        claim=claim, verdict=verdict, evidence=f"{evidence} [in {where}]",
        evidence_index=event.index if event.source is None else None,
        evidence_source=where, relayed=ctx.relayed,
    )


def _note(finding: Finding, text: str) -> Finding:
    """The finding with `text` added at the end of its evidence."""
    return replace(finding, evidence=f"{finding.evidence}; {text}")


_NO_RUN_REASON = {
    ClaimType.DEPLOYED: "no deploy command (wrangler, vercel, netlify, gh release, "
                        "twine, npm publish, rsync or scp to a host) ran before the claim",
}


def _verify_command_claim(timeline: Timeline, claim: Claim, ctx: _Context,
                          evidence_type: ClaimType | None = None) -> Finding:
    kind = evidence_type or claim.type
    last = timeline.last_run(kind, ctx.key, ctx.restrict)
    if last is None:
        return _unverified(claim, ctx, _NO_RUN_REASON.get(
            kind, "no command that could back this claim was run before it"))
    run_key, run = last
    if evidence_of_success(run)[1] == NO_RESULT:
        return _found(claim, ctx, Verdict.UNVERIFIED, _no_result(run), run)
    if _command_failed(run):
        if run.is_error or (run.exit_code or 0) != 0:
            reason = (f"exit {run.exit_code}" if run.exit_code is not None
                      else f"status {run.status}" if run.status else "exit ?")
        elif run.exit_code is None and run.status:
            reason = "output reports failures, the log has a status only"
        else:
            reason = "output reports failures despite exit 0"
        return _found(claim, ctx, Verdict.CONTRADICTED,
                      f"most recent relevant run failed ({reason}): "
                      f"`{_short(run.command)}`", run)
    doubt = _no_proof(run)
    if doubt:
        return _found(claim, ctx, Verdict.UNVERIFIED, doubt, run)
    if kind in CHECK_TYPES:
        # Only edits inside the folder the check ran in make it stale: a
        # parallel agent working on another repository does not.
        folder = run_directory(run.command, run.cwd,
                               lambda stage: kind in stage_types(stage))
        edits_after = timeline.edits_between(run_key, ctx.key, ctx.restrict, folder)
        if edits_after:
            files = {base_name(e.file_path) for e in edits_after if e.file_path}
            listed = ", ".join(sorted(files)[:3]) or f"{len(edits_after)} files"
            return _found(claim, ctx, Verdict.STALE,
                          f"`{_short(run.command)}` passed, but {listed} "
                          f"edited afterwards with no re-run before the claim", run)
    return _rewound_cap(timeline, kind, run_key, ctx, claim, _found(
        claim, ctx, Verdict.VERIFIED,
        f"`{_short(run.command)}` succeeded (exit "
        f"{run.exit_code if run.exit_code is not None else 0}"
        f"{_masked_note(run)})", run))


_is_remote_path = is_remote_path  # kept under its old name for callers and tests


_SKIP_REMOTE = "network or device path"
_SKIP_OUTSIDE = "outside the project folder"


def _confine(path, base, root: str, skipped: set[str]) -> Path | None:
    """`path` (joined to `base` when relative) if it is safe to check on disk, else None.

    Safe means inside the session's project folder `root` (symlinks followed) and never a
    network or device path. The reason for each refusal goes into `skipped`.
    """
    if not path:
        return None
    path, base = windows_drive(str(path)), windows_drive(str(base)) if base else None
    full = Path(path) if Path(path).is_absolute() or not base else Path(base) / path
    if _is_remote_path(path) or _is_remote_path(base or "") or _is_remote_path(full) \
            or _is_remote_path(root):
        skipped.add(_SKIP_REMOTE)
        return None
    inside = confined(full, root)
    if inside is None:
        skipped.add(_SKIP_OUTSIDE)
    return inside


def _local_candidates(session: Session, claim: Claim,
                      writes: list[Event]) -> tuple[list[Path], set[str]]:
    """Paths safe to check on disk, and why any were skipped (empty when none were)."""
    skipped: set[str] = set()
    candidates = [_confine(claim.detail, session.cwd, session.cwd, skipped)]
    candidates += [_confine(w.file_path, None, session.cwd, skipped) for w in writes if w.file_path]
    return [p for p in candidates if p is not None], skipped


def _not_checked(skipped: set[str]) -> str:
    return "disk not checked (" + "; ".join(sorted(skipped)) + ")"


# -- the one evidence rule (D087) ---------------------------------------------
#
# Success comes ONLY from an explicit success record. SEC-197, 208, 209, 211,
# 212, 213 and 214 were one bug class, "something that did not succeed reads
# as success", fixed per reader; every evidence decision below now goes
# through `evidence_of_success`, and tests/test_reader_conformance.py holds
# every registered reader to it.

# How a reader marks a call that ended badly or never ran (SEC-208, SEC-214):
# Gemini and Qwen say cancelled or error, Codex failed or declined, OpenCode
# error, Goose declined (a chat-mode skip) or timeout.
_FAILED_STATUS = frozenset({"cancelled", "canceled", "error", "failed", "rejected",
                            "declined", "skipped", "timeout"})
# A result the log holds but that does not say whether the call worked: Codex
# code mode "outcome unknown", Cline "unknown" (SEC-212, is_error False),
# Codex "not recorded" (a shell result with no exit code and no success flag).
_UNKNOWN_STATUS = frozenset({"outcome unknown", "unknown", "not recorded"})
# A Codex code-mode script that failed or was stopped: a command nested in it
# keeps is_error False (a test claim stays "status only"), but it is no
# evidence that a file it would write was written (SEC-213).
_SCRIPT_ENDED = frozenset({"script failed", "script terminated"})
# Left behind by a rewind (SEC-210), or a pass logged before rows Goose
# deleted (goose._REWOUND, SEC-215 class).
_REWOUND_STATUS = frozenset({"rewound", "completed, later log rows were deleted"})
# Still waiting: Gemini and OpenCode words for a call with no result yet, and
# a run that went on in the background (SEC-208).
_WAITING_STATUS = frozenset({"running", "pending", "streaming", "validating", "scheduled",
                             "executing", "awaiting_approval"})
NO_EVIDENCE_STATUS = (_FAILED_STATUS | _UNKNOWN_STATUS | _SCRIPT_ENDED
                      | _REWOUND_STATUS | _WAITING_STATUS)
# The words a reader writes when the agent's own record says the call worked.
SUCCESS_STATUS = frozenset({"completed", "success"})

# Why a call is no evidence of success (the second value of evidence_of_success).
NO_RESULT, REWOUND, FAILED, UNKNOWN, WAITING, NO_RECORD = (
    "no result", "rewound", "failed", "unknown", "waiting", "no success record")


def evidence_of_success(event: Event) -> tuple[bool, str]:
    """The one evidence rule: (True, "") only when the log holds an explicit
    success record for this call, else (False, why).

    Success needs all of: the result was seen (result_seen not False); the
    call was not rewound; its status is not in NO_EVIDENCE_STATUS; it is not
    an error; and success is recorded, as an exit code of 0 (which a reader
    sets only from a structured field, see models.Event) or a status in
    SUCCESS_STATUS. Missing result, skipped, declined, cancelled, unknown,
    status-only failure words, an exit code claimed only by printed text and
    rewound all mean no evidence. A run whose exit code is missing still needs
    it for a check claim ("status only", in _verify_command_claim).

    A hand-built event (result_seen None, no reader) is taken as given: no
    error and no exit code read as exit 0, the shape tests write by hand.
    """
    status = (event.status or "").lower()
    if status in _UNKNOWN_STATUS:
        return False, UNKNOWN
    if (event.is_error or status in _FAILED_STATUS | _SCRIPT_ENDED
            or event.exit_code not in (None, 0)):
        return False, FAILED
    if event.rewound or status in _REWOUND_STATUS:
        return False, REWOUND
    if event.result_seen is False:
        return False, NO_RESULT
    if status in _WAITING_STATUS:
        return False, WAITING
    if event.exit_code == 0 or status in SUCCESS_STATUS:
        return True, ""
    if event.result_seen is None and not status:
        return True, ""  # hand-built: no reader, nothing to doubt
    return False, NO_RECORD


def _write_why(event: Event) -> str:
    """Why `evidence_of_success` is False, as the end of a sentence."""
    why = evidence_of_success(event)[1]
    if why == UNKNOWN:
        return "has an unknown outcome: the log does not show whether it ran"
    if why == FAILED:
        return f"did not succeed ({event.status or 'error'})"
    if why == REWOUND:
        return "was rewound"
    if why == WAITING:
        return f"did not finish ({event.status})"
    if why == NO_RECORD:
        return "has no success record in the log"
    return "did not finish: no result was recorded"


def _failed_write(claim: Claim, ctx: _Context, writes: list[Event], name: str) -> Finding:
    last = writes[-1]
    return _found(claim, ctx, Verdict.UNVERIFIED,
                  f"the {last.tool_name} call for `{name}` {_write_why(last)}", last)


def _rewound_cap(timeline: Timeline, kind: ClaimType, run_key: tuple, ctx: _Context,
                 claim: Claim, finding: Finding) -> Finding:
    """A VERIFIED finding capped at UNVERIFIED when a later failed run of the
    same kind was rewound before the claim (SEC-210)."""
    if finding.verdict is not Verdict.VERIFIED:
        return finding
    failed = timeline.rewound_failure(kind, run_key, ctx.key)
    if failed is None:
        return finding
    how = (f"exit {failed.exit_code}" if failed.exit_code is not None
           else f"status {failed.status or 'error'}")
    return _found(claim, ctx, Verdict.UNVERIFIED,
                  f"a later run was rewound ({how}): `{_short(failed.command)}`", failed)


def _matching_writes(timeline: Timeline, claim: Claim, ctx: _Context) -> list[Event]:
    """Write/Edit calls before the claim to a file with the claimed name."""
    name = base_name(claim.detail)
    return [
        e for e in timeline.writes_before(ctx.key, ctx.restrict)
        if e.tool_action in (WRITE, EDIT, NOTEBOOK_EDIT)
        and base_name(e.file_path) == name
    ]


def _verify_file_created(timeline: Timeline, claim: Claim, ctx: _Context,
                         check_disk: bool) -> Finding:
    """A file the claim says was created or edited: written in-session, on disk."""
    session = timeline.session
    name = base_name(claim.detail)
    writes = _matching_writes(timeline, claim, ctx)
    if not writes:
        return _unverified(claim, ctx,
                           f"no Write/Edit call for `{name}` appears before the claim")
    done = [w for w in writes if evidence_of_success(w)[0]]
    if not done:
        return _failed_write(claim, ctx, writes, name)
    writes = done
    last = writes[-1]
    if check_disk and session.cwd:
        candidates, skipped = _local_candidates(session, claim, writes)
        on_disk = any(p.exists() for p in candidates)
        if not on_disk and skipped:
            return _found(claim, ctx, Verdict.VERIFIED,
                          f"{last.tool_name} call for `{name}` found; {_not_checked(skipped)}", last)
        if not on_disk:
            return _found(claim, ctx, Verdict.CONTRADICTED,
                          f"`{claim.detail}` was written in-session but is not on disk now",
                          last)
    return _found(claim, ctx, Verdict.VERIFIED,
                  f"{last.tool_name} call for `{name}` found"
                  + (" and file exists on disk" if check_disk else ""), last)


def _verify_task_done(timeline: Timeline, claim: Claim, ctx: _Context) -> Finding:
    """'Done/fixed/working' is only as good as the checks run after the last edit."""
    last_edit = timeline.last_edit(ctx.key, ctx.restrict)
    if last_edit is None:
        return _unverified(claim, ctx,
                           "no file edits precede this claim; nothing to check it against")
    edit_key = last_edit[0]
    for claim_type in (ClaimType.TESTS_PASS, ClaimType.BUILD_OK, ClaimType.TYPECHECK_OK):
        runs = timeline.runs_before(claim_type, ctx.key, ctx.restrict, after=edit_key)
        if runs:
            run = runs[-1]  # the first check after the final edit
            if evidence_of_success(run)[1] == NO_RESULT:
                return _found(claim, ctx, Verdict.UNVERIFIED, _no_result(run), run)
            if _command_failed(run):
                return _found(claim, ctx, Verdict.CONTRADICTED,
                              f"check after final edit failed: `{_short(run.command)}`", run)
            doubt = _no_proof(run)
            if doubt:
                return _found(claim, ctx, Verdict.UNVERIFIED, doubt, run)
            return _rewound_cap(timeline, claim_type, edit_key, ctx, claim, _found(
                claim, ctx, Verdict.VERIFIED,
                f"verified after final edit by `{_short(run.command)}`", run))
    return _unverified(claim, ctx,
                       "no test/build/typecheck ran between the final edit and this claim")


def _verify_test_count(timeline: Timeline, claim: Claim, ctx: _Context) -> Finding:
    """'N of N pass': a passing test run before it whose output shows N passed."""
    finding = _verify_command_claim(timeline, claim, ctx, ClaimType.TESTS_PASS)
    if finding.verdict not in (Verdict.VERIFIED, Verdict.STALE):
        return finding
    _, run = timeline.last_run(ClaimType.TESTS_PASS, ctx.key, ctx.restrict)
    claimed = int(claim.detail)
    if not run.passed_counts:
        return _found(claim, ctx, Verdict.UNVERIFIED,
                      f"the run's output shows no test count to match {claimed}: "
                      f"`{_short(run.command)}`", run)
    if claimed not in run.passed_counts:
        shown = " or ".join(str(n) for n in sorted(run.passed_counts))
        return _found(claim, ctx, Verdict.CONTRADICTED,
                      f"claimed {claimed} passing, the run's output shows {shown} "
                      f"passed: `{_short(run.command)}`", run)
    return _note(finding, f"output shows {claimed} passed")


def _ids_match(claimed: str, printed: str) -> bool:
    printed = printed.lower()
    return printed.startswith(claimed) or claimed.startswith(printed)


_PRINTED_ID = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)


def _verify_commit_id(timeline: Timeline, claim: Claim, ctx: _Context) -> Finding:
    """A commit id named in a claim, from the transcript: a commit command printed it."""
    for run in timeline.runs_before(ClaimType.COMMIT_MADE, ctx.key, None):
        if _command_failed(run) or not evidence_of_success(run)[0]:
            continue
        if any(_ids_match(claim.detail, m) for m in _PRINTED_ID.findall(run.output[:4000])):
            return _found(claim, ctx, Verdict.VERIFIED,
                          f"`{_short(run.command)}` printed commit {claim.detail[:12]}", run)
    return _unverified(claim, ctx,
                       f"no commit command in the transcript printed {claim.detail[:12]}")


# A shell command writing a file: redirection, -o/--output, PowerShell writers, tee.
_SHELL_WRITE = re.compile(
    r"(?:>>?|\s-o|\s--output(?:=|\s)|\s--out(?:=|\s)"
    r"|\bOut-File(?:\s+-FilePath)?|\b(?:Set|Add)-Content(?:\s+-Path)?|\btee(?:\s+-a)?)"
    r"\s*[\"']?([^\s\"'|;&<>]+)",
    re.IGNORECASE,
)


def _same_file(path: str, base: str, claimed: str, claimed_base: str) -> bool:
    a = norm_path(path, norm_path(base) if base else None)
    b = norm_path(claimed, norm_path(claimed_base) if claimed_base else None)
    if a is not None and b is not None:
        return a == b
    return Path(path.replace("\\", "/")).name.lower() == \
        Path(claimed.replace("\\", "/")).name.lower()


def _verify_path_written(timeline: Timeline, claim: Claim, ctx: _Context,
                         check_disk: bool) -> Finding:
    """'Report at / saved to / wrote <path>': written in-session, and on disk."""
    session = timeline.session
    target = claim.detail
    writes = [e for e in timeline.writes_before(ctx.key, ctx.restrict)
              if e.file_path and _same_file(e.file_path, e.cwd or session.cwd,
                                            target, session.cwd)]
    shell_paths: list[tuple[Event, str]] = []
    unfinished: list[Event] = []  # shell writes the log does not show succeeding (SEC-208)
    for event in session.events[:claim.event_index]:
        if event.kind is EventKind.TOOL_CALL and event.command and not _command_failed(event):
            for path in _SHELL_WRITE.findall(event.command):
                if _same_file(path, event.cwd or session.cwd, target, session.cwd):
                    if not evidence_of_success(event)[0]:
                        unfinished.append(event)
                    else:
                        shell_paths.append((event, path))
    if not writes and not shell_paths:
        if unfinished:
            last = unfinished[-1]
            return _found(claim, ctx, Verdict.UNVERIFIED,
                          f"`{_short(last.command)}` writes `{target}`, but the run"
                          f" {_write_why(last)}", last)
        return _unverified(claim, ctx, f"no write to `{target}` appears before the claim")
    done = [w for w in writes if evidence_of_success(w)[0]]
    if writes and not done and not shell_paths:
        return _failed_write(claim, ctx, writes, target)
    writes = done
    last = writes[-1] if writes else shell_paths[-1][0]
    how = (f"{last.tool_name} call for `{target}` found" if writes
           else f"`{_short(last.command)}` wrote `{target}`")
    if check_disk and session.cwd:
        candidates, skipped = _local_candidates(session, claim, writes)
        for event, path in shell_paths:
            inside = _confine(path, event.cwd or session.cwd, session.cwd, skipped)
            if inside is not None:
                candidates.append(inside)
        if not any(p.exists() for p in candidates):
            if skipped:
                return _found(claim, ctx, Verdict.VERIFIED, f"{how}; {_not_checked(skipped)}", last)
            return _found(claim, ctx, Verdict.CONTRADICTED,
                          f"`{target}` was written in-session but is not on disk now", last)
        return _found(claim, ctx, Verdict.VERIFIED, f"{how} and it exists on disk", last)
    return _found(claim, ctx, Verdict.VERIFIED, how, last)


# -- git state ---------------------------------------------------------------

_GIT_TYPES = frozenset({
    ClaimType.COMMIT_MADE, ClaimType.PUSHED, ClaimType.FILE_CREATED,
    ClaimType.FILE_EDITED, ClaimType.COMMIT_ID,
})
# Seconds around the session's first and last record (and the claim) that
# still count as inside it: commit times come from the same clock, but a
# commit is stamped a moment before its result is logged.
_WINDOW_SLACK = 120.0


def _git_finding(claim: Claim, ctx: _Context, verdict: Verdict, evidence: str) -> Finding:
    return Finding(claim=claim, verdict=verdict, evidence=f"{evidence} [in git]",
                   evidence_source="git", relayed=ctx.relayed)


def _subject_text(subject: str) -> str:
    return _short(subject, 60).replace("`", "'")


class _GitCheck:
    """Checks a claim against the project's git state (local refs only)."""

    def __init__(self, session: Session):
        self.session = session
        self.repo = GitRepo(session.cwd)
        self.reason = self.repo.reason
        start, end = epoch(session.first_timestamp), epoch(session.last_timestamp)
        if not self.reason and (start is None or end is None):
            self.reason = "no timestamps in the transcript"
        self.start = (start or 0.0) - _WINDOW_SLACK
        self.end = (end or 0.0) + _WINDOW_SLACK

    def close(self) -> None:
        self.repo.close()

    def _session_commits(self, until: float):
        """Session commits made by `until`, newest first (one git log per audit)."""
        return [c for c in self.repo.commits_between(self.start, self.end) if c.time <= until]

    def check(self, timeline: Timeline, claim: Claim, ctx: _Context,
              finding: Finding) -> Finding:
        if self.reason:
            return _note(finding, f"git state not checked ({self.reason})")
        t = timeline.session.events[claim.event_index].t
        until = (t if t != float("-inf") else self.end) + _WINDOW_SLACK
        try:
            if claim.type is ClaimType.COMMIT_MADE:
                return self._committed(timeline, claim, ctx, until)
            if claim.type is ClaimType.PUSHED:
                return self._pushed(claim, ctx, finding, until)
            if claim.type is ClaimType.COMMIT_ID:
                return self._commit_id(claim, ctx)
            return self._file(timeline, claim, ctx, finding)
        except GitError as exc:
            self.reason = str(exc)
            return _note(finding, f"git state not checked ({self.reason})")

    def _committed(self, timeline, claim, ctx, until) -> Finding:
        commits = self._session_commits(until)
        if not commits:
            return _git_finding(claim, ctx, Verdict.CONTRADICTED,
                                "no commit was made in this repository during the session "
                                "(git log of local branches)")
        subjects = {s for run in timeline.runs_before(ClaimType.COMMIT_MADE, ctx.key, None)
                    for s in commit_subjects(run.command)}
        if subjects:
            match = next((c for c in commits if " ".join(c.subject.split()) in subjects), None)
            if match is None:
                wanted = _subject_text(sorted(subjects)[0])
                return _git_finding(claim, ctx, Verdict.CONTRADICTED,
                                    f"no commit made during the session has the subject "
                                    f"`{wanted}` that the transcript's git commit gave")
            return _git_finding(claim, ctx, Verdict.VERIFIED,
                                f"commit {match.short} `{_subject_text(match.subject)}` "
                                f"made during the session is in the repository")
        return _git_finding(claim, ctx, Verdict.VERIFIED,
                            f"commit {commits[0].short} made during the session "
                            f"is in the repository")

    def _pushed(self, claim, ctx, finding, until) -> Finding:
        commits = self._session_commits(until)
        if not commits:
            return _note(finding, "git state not checked (no commit made during the session)")
        if not self.repo.remote_refs():
            return _note(finding, "git state not checked "
                                  "(the repository has no remote-tracking branches)")
        newest = commits[0]
        refs = self.repo.remote_branches_containing(newest.sha)
        if refs:
            return _git_finding(claim, ctx, Verdict.VERIFIED,
                                f"commit {newest.short} is on {_short(refs[0], 60)} "
                                f"(local remote-tracking refs, not fetched)")
        return _git_finding(claim, ctx, Verdict.CONTRADICTED,
                            f"commit {newest.short} made during the session is on no "
                            f"remote-tracking branch (local refs, not fetched)")

    def _commit_id(self, claim, ctx) -> Finding:
        short = claim.detail[:12]
        if self.repo.has_commit(claim.detail):
            return _git_finding(claim, ctx, Verdict.VERIFIED,
                                f"commit {short} exists in the repository")
        return _git_finding(claim, ctx, Verdict.CONTRADICTED,
                            f"commit {short} is not in this repository")

    def _file(self, timeline, claim, ctx, finding) -> Finding:
        if finding.verdict is Verdict.CONTRADICTED:
            return finding  # already not on disk; git cannot add to that
        named = [claim.detail] + [w.file_path for w in _matching_writes(timeline, claim, ctx)]
        rels = [r for r in (self.repo.relative(p) for p in named) if r is not None]
        if not rels:
            return _note(finding, "git state not checked (path is outside the project)")
        bare = "/" not in claim.detail and "\\" not in claim.detail
        commits = self.repo.commits_between(self.start, self.end)
        how, short = self.repo.where_touched(rels, commits,
                                             name=Path(claim.detail).name if bare else None)
        if how == "commit":
            if finding.verdict is Verdict.VERIFIED:
                return _note(finding, f"in session commit {short}")
            return _git_finding(claim, ctx, Verdict.VERIFIED,
                                f"`{claim.detail}` is in session commit {short}")
        if how == "working":
            return _note(finding, "in the working-tree changes"
                         if finding.verdict is Verdict.VERIFIED
                         else "it has working-tree changes")
        if how == "ignored":
            return _note(finding, "git state not checked (git ignores this path)")
        return _git_finding(claim, ctx, Verdict.CONTRADICTED,
                            f"names a file the diff does not touch: `{claim.detail}` is in "
                            f"no session commit and has no working-tree change")


def verify_claims(session: Session, claims: list[Claim],
                  check_disk: bool = True) -> list[Finding]:
    timeline = Timeline(session)
    findings: list[Finding] = []
    git = (_GitCheck(session)
           if check_disk and any(c.type in _GIT_TYPES for c in claims) else None)
    try:
        for claim in claims:
            ctx = _Context(timeline, claim)
            if claim.type in (ClaimType.FILE_CREATED, ClaimType.FILE_EDITED):
                finding = _verify_file_created(timeline, claim, ctx, check_disk)
            elif claim.type is ClaimType.TASK_DONE:
                finding = _verify_task_done(timeline, claim, ctx)
            elif claim.type is ClaimType.TEST_COUNT:
                finding = _verify_test_count(timeline, claim, ctx)
            elif claim.type is ClaimType.COMMIT_ID:
                finding = _verify_commit_id(timeline, claim, ctx)
            elif claim.type is ClaimType.PATH_WRITTEN:
                finding = _verify_path_written(timeline, claim, ctx, check_disk)
            else:
                finding = _verify_command_claim(timeline, claim, ctx)
            if git is not None and claim.type in _GIT_TYPES:
                finding = git.check(timeline, claim, ctx, finding)
            findings.append(finding)
    finally:
        if git is not None:
            git.close()
    return findings
