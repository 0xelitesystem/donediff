"""Data models for donediff.

Everything the auditor reasons about is one of these shapes:
a Session is an ordered list of Events (assistant text or tool calls),
Claims are extracted from text events, and Findings are the verdicts.
A Session can also carry Sources: the transcripts of the sub-agents and
workflow agents it delegated to, which hold only the tool calls that
can count as evidence (checks, git commands, file writes).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class EventKind(enum.Enum):
    TEXT = "text"
    TOOL_CALL = "tool_call"


# What a tool call does, in words every reader shares. The checks read these,
# never an agent's own tool names. A reader sets Event.action and puts the
# inputs under the shared keys: "command" (shell), "file_path" (write and the
# edits), "content" (write), "old_string"/"new_string" (edit) or "edits", a list
# of those pairs (multi_edit). With no action set, Claude Code's names apply
# (the first reader's, and what hand-built test events use).
SHELL, WRITE, EDIT, MULTI_EDIT, NOTEBOOK_EDIT, OTHER = (
    "shell", "write", "edit", "multi_edit", "notebook_edit", "other")
CLAUDE_CODE_ACTIONS = {
    "Bash": SHELL, "PowerShell": SHELL, "Write": WRITE, "Edit": EDIT,
    "MultiEdit": MULTI_EDIT, "NotebookEdit": NOTEBOOK_EDIT,
}
FILE_ACTIONS = (WRITE, EDIT, MULTI_EDIT, NOTEBOOK_EDIT)


@dataclass
class Event:
    """One thing that happened in the session, in transcript order.

    The evidence contract every reader keeps (D087; verify.evidence_of_success
    reads it, tests/test_reader_conformance.py holds each reader to it):
    - result_seen: False when the call is made, True only when its result is in
      the log. A reader never leaves it None (None means a hand-built event).
    - exit_code: only from a structured field of the agent's log (a JSON exit
      code, or a header or trailer the agent writes in a fixed place that the
      command's own output cannot supply). A code read from printed text never
      makes a run a success: text may only add a failure. No code: None, never
      a made-up 0.
    - status: the agent's own word for how the call ended. Success is only
      "completed" or "success" from the agent's own record; the absence of a
      failure flag is not a success. An outcome the log does not show is
      "unknown"; a call that was skipped, declined or cancelled says so (with
      is_error for a write or edit).
    - rewound: True for a call a rewind left behind; it is never evidence.
    """

    kind: EventKind
    index: int  # position in the session event stream
    timestamp: str = ""
    is_sidechain: bool = False

    # TEXT events
    text: str = ""

    # TOOL_CALL events
    tool_name: str = ""  # the agent's own name for the tool, for display
    action: str = ""  # shared action (see CLAUDE_CODE_ACTIONS); empty = from tool_name
    tool_id: str = ""
    tool_input: dict = field(default_factory=dict)
    output: str = ""
    is_error: bool = False
    exit_code: int | None = None
    # How the run ended in words, for a reader whose log can lack an exit
    # code (Codex). With no exit code, a check reads this, never a made-up 0.
    status: str = ""
    # Whether the log holds a result for this call. Readers set False when the
    # call is made and True when its result (or completion notice) arrives;
    # None = not tracked (hand-built events). False never reads as a success.
    result_seen: bool | None = None
    output_truncated: bool = False  # the log cut the output short (Codex, 64 KiB)
    cwd: str = ""  # working directory the tool call started in

    # Ordering across transcripts. `t` is seconds since the epoch: for a
    # main-session event, the record time made non-decreasing; for an
    # event in a delegated transcript, the time its result came back.
    t: float = float("-inf")
    source: "Source | None" = None  # None means the main session
    failure_hint: bool = False  # output reported failures (kept when output is dropped)
    pass_hint: bool = False  # output shows a positive pass marker (kept likewise)
    passed_counts: frozenset = frozenset()  # passing-test totals the output shows (kept likewise)
    # A command that went to the background: its first result only says it
    # started. It counts once its completion notice is seen, with the exit
    # code and time from that notice; until then it is no evidence.
    background: bool = False
    done_order: float | None = None  # main ordering position of the notice
    check_types: frozenset = frozenset()  # ClaimTypes this command can back
    # Left behind by a rewind (Qwen): off the active history, after its branch
    # point. Evidence-only (SEC-210): an edit still makes a check stale, a
    # failed run caps a later claim at UNVERIFIED, and nothing here verifies.
    rewound: bool = False

    @property
    def tool_action(self) -> str:
        """The shared action: the reader's, else from Claude Code's tool name."""
        return self.action or CLAUDE_CODE_ACTIONS.get(self.tool_name, OTHER)

    @property
    def command(self) -> str:
        """Shell command for shell calls, else empty."""
        if self.tool_action == SHELL:
            return str(self.tool_input.get("command", ""))
        return ""

    @property
    def file_path(self) -> str:
        """Target path for file-mutating tools, else empty."""
        return str(self.tool_input.get("file_path", ""))

    def is_file_edit(self) -> bool:
        return self.tool_action in FILE_ACTIONS


@dataclass
class Source:
    """A delegated transcript: a sub-agent, or one agent of a workflow run."""

    kind: str  # "subagent" or "workflow_agent"
    path: str
    agent_id: str = ""
    run_id: str = ""
    label: str = ""  # the agent's description, redacted and trimmed
    finished: float | None = None  # time of the transcript's last record
    events: list[Event] = field(default_factory=list)
    gaming: list["GamingSignal"] = field(default_factory=list)
    size: int = 0

    def describe(self) -> str:
        where = (f"workflow {self.run_id}, agent {self.agent_id}"
                 if self.kind == "workflow_agent" else f"sub-agent {self.agent_id}")
        return where + (f" ({self.label})" if self.label else "")

    def matches(self, keys: set[tuple[str, str]]) -> bool:
        return (("agent", self.agent_id) in keys
                or (bool(self.run_id) and ("run", self.run_id) in keys))


@dataclass
class Delivery:
    """A delegated result reaching the main session (agent or workflow done)."""

    index: int  # number of main events before it
    key: tuple[str, str]  # ("agent", agentId) or ("run", runId)
    seq: int = 0  # line number in the main transcript, to order it against prompts


@dataclass
class ScanStats:
    files: int = 0
    subagents: int = 0
    workflow_agents: int = 0
    bytes: int = 0
    seconds: float = 0.0


@dataclass
class Session:
    """A parsed agent session transcript."""

    path: str
    session_id: str = ""
    cwd: str = ""
    git_branch: str = ""
    slug: str = ""
    version: str = ""
    events: list[Event] = field(default_factory=list)
    first_timestamp: str = ""
    last_timestamp: str = ""
    # Where each human prompt starts (index of the next event), and where
    # delegated results came back, for telling a relayed claim apart.
    prompt_starts: list[int] = field(default_factory=list)
    # (index, line number) of each human prompt; the line number orders a
    # prompt against a delivery recorded with the same index.
    prompt_marks: list[tuple[int, int]] = field(default_factory=list)
    deliveries: list[Delivery] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    main_only: bool = True
    scan: ScanStats = field(default_factory=ScanStats)
    unrecognised: int = 0  # log lines a reader could not read, skipped (shown in scope)

    def tool_calls(self) -> list[Event]:
        return [e for e in self.events if e.kind is EventKind.TOOL_CALL]

    def text_events(self) -> list[Event]:
        return [e for e in self.events if e.kind is EventKind.TEXT]


class ClaimType(enum.Enum):
    TESTS_PASS = "tests_pass"
    BUILD_OK = "build_ok"
    LINT_OK = "lint_ok"
    TYPECHECK_OK = "typecheck_ok"
    FILE_CREATED = "file_created"
    COMMIT_MADE = "commit_made"
    PUSHED = "pushed"
    TASK_DONE = "task_done"
    FILE_EDITED = "file_edited"
    DEPLOYED = "deployed"
    TEST_COUNT = "test_count"
    COMMIT_ID = "commit_id"
    PATH_WRITTEN = "path_written"


@dataclass
class Claim:
    """A checkable assertion the agent made in its prose."""

    type: ClaimType
    quote: str  # what the finding shows: the sentence, or the matched phrase
    event_index: int  # index of the text event containing it
    # FILE_CREATED / FILE_EDITED / PATH_WRITTEN: the path; COMMIT_ID: the id;
    # TEST_COUNT: the claimed number of passing tests.
    detail: str = ""
    sentence: str = ""  # the whole sentence, when it holds more than one claim


class Verdict(enum.Enum):
    VERIFIED = "verified"  # evidence in the transcript backs the claim
    STALE = "stale"  # evidence exists but code changed after it
    UNVERIFIED = "unverified"  # no evidence either way
    CONTRADICTED = "contradicted"  # evidence shows the claim is false


@dataclass
class Finding:
    """The verdict on one claim, with a pointer to the evidence."""

    claim: Claim
    verdict: Verdict
    evidence: str = ""  # human-readable: what we checked and what we saw
    evidence_index: int | None = None  # main event index of the deciding evidence
    evidence_source: str = ""  # where the deciding evidence ran: "main" or a sub-agent
    relayed: bool = False  # the claim passes on a delegated agent's report


class GamingSeverity(enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class GamingSignal:
    """A pattern that suggests the agent made checks pass by weakening them."""

    kind: str
    severity: GamingSeverity
    description: str
    event_index: int | None  # main event index, None in a delegated transcript
    source: str = "main"


@dataclass
class AuditResult:
    """Everything the audit produced for one session."""

    session: Session
    findings: list[Finding] = field(default_factory=list)
    gaming_signals: list[GamingSignal] = field(default_factory=list)
    score: int | None = None  # 0-100, None when there were no claims
    grade: str = ""

    def counts(self) -> dict[str, int]:
        c = {v.value: 0 for v in Verdict}
        for f in self.findings:
            c[f.verdict.value] += 1
        return c
