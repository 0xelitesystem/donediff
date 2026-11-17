"""Extract checkable claims from the agent's prose.

A claim is a sentence in an assistant text block that asserts something
about the state of the world: tests pass, the build is clean, a file was
created, work was committed. Hedged, negated, future, or conditional
sentences ("once tests pass...", "tests should pass") are not claims.
"""

from __future__ import annotations

import re

from .evidence import log_int
from .models import Claim, ClaimType, EventKind, Session
from .redact import redact_secrets

# Sentences containing these are aspirational or negative, not assertions.
_DISQUALIFIERS = re.compile(
    r"\b(?:will|would|should|could|may|might|once|if|unless|until|when|"
    r"whenever|after|let'?s|going to|need(?:s)? to|have to|to make|"
    r"don'?t|doesn'?t|didn'?t|aren'?t|isn'?t|wasn'?t|not|no longer|"
    r"fail(?:s|ed|ing)?|broken|can'?t|cannot|try|attempt)\b",
    re.IGNORECASE,
)

# A path named in a claim: in backticks, or bare with a file extension.
_PATH = (r"(?:`([^`\s]+)`"
         r"|((?:[A-Za-z]:)?[\w.\\/-]*[\w-]\.[A-Za-z0-9]{1,10})(?![\w\\/]))")

# The claim-word table: every phrasing donediff reads as a claim, in one
# place, tried in order. Each phrasing here has a test in test_claims.py.
# The groups capture the claim's detail (see _detail).
_PATTERNS: list[tuple[ClaimType, re.Pattern]] = [
    (ClaimType.TEST_COUNT, re.compile(
        r"\b(\d+)\s+of\s+(\d+)\s+(?:tests?\s+)?(?:pass(?:es|ed|ing)?|green|succeed(?:s|ed)?)\b"
        r"|\b(\d+)\s*/\s*(\d+)\s+tests?\b",
        re.IGNORECASE,
    )),
    (ClaimType.TESTS_PASS, re.compile(
        r"\b(?:all\s+)?(?:\d+\s+)?tests?\s+(?:are\s+|now\s+|still\s+)?"
        r"(?:pass(?:es|ing|ed)?|green|succeed(?:s|ed)?)\b"
        r"|\btest\s+suite\s+(?:passes|is\s+green|succeeds)\b"
        r"|\b(?:\d+)\s+passed\b.*\b0\s+failed\b",
        re.IGNORECASE,
    )),
    (ClaimType.BUILD_OK, re.compile(
        r"\bbuild\s+(?:is\s+|now\s+)?(?:pass(?:es|ing|ed)?|succeed(?:s|ed)?|"
        r"clean|green|works|working|successful)\b"
        r"|\b(?:compiles?|compiled)\s+(?:cleanly|successfully|without\s+errors?)\b",
        re.IGNORECASE,
    )),
    (ClaimType.LINT_OK, re.compile(
        r"\blint(?:er|ing)?\s+(?:is\s+|now\s+)?(?:pass(?:es|ing|ed)?|clean|green)\b"
        r"|\bno\s+lint(?:er|ing)?\s+(?:errors?|warnings?|issues?)\b",
        re.IGNORECASE,
    )),
    (ClaimType.TYPECHECK_OK, re.compile(
        r"\btype[\s-]?check(?:s|ing|er)?\s+(?:is\s+|now\s+)?(?:pass(?:es|ing|ed)?|clean|green)\b"
        r"|\bno\s+type\s+errors?\b"
        r"|\b(?:mypy|pyright|tsc)\s+(?:is\s+|now\s+)?(?:passes|clean|green|happy)\b",
        re.IGNORECASE,
    )),
    (ClaimType.FILE_CREATED, re.compile(
        r"\bcreated\s+(?:the\s+(?:file\s+)?|a\s+(?:new\s+)?(?:file\s+)?|file\s+)?"
        r"`([^`\s]+\.[A-Za-z0-9]{1,10})`",
        re.IGNORECASE,
    )),
    (ClaimType.FILE_EDITED, re.compile(
        r"\b(?:edited|updated|modified)\s+(?:the\s+(?:file\s+)?|file\s+)?"
        r"`([^`\s]+\.[A-Za-z0-9]{1,10})`",
        re.IGNORECASE,
    )),
    (ClaimType.PATH_WRITTEN, re.compile(
        r"\breport\s+(?:is\s+)?(?:at|in)\s+" + _PATH
        + r"|\bsaved\s+(?:it\s+|them\s+|(?:the|a)\s+\w+\s+)?(?:to|at|as)\s+" + _PATH
        + r"|\bwrote\s+(?:it\s+|them\s+|(?:the|a)\s+\w+\s+)?(?:to\s+)?" + _PATH,
        re.IGNORECASE,
    )),
    (ClaimType.COMMIT_ID, re.compile(
        r"\bcommit(?:ted)?(?:\s+(?:as|in|at))?(?:\s+(?:id|hash|sha))?[\s:]+[`(]?"
        r"([0-9a-fA-F]{7,40})\b",
        re.IGNORECASE,
    )),
    (ClaimType.DEPLOYED, re.compile(
        r"\bdeployed\s+(?:it|this|that|them|everything|the\s+\w+|to\s+\w+|v?\d[\w.]*)"
        r"|\b(?:has|have)\s+been\s+deployed\b|\b(?:is|are)\s+(?:now\s+)?deployed\b"
        r"|\bshipped\s+to\s+production\b"
        r"|\b(?:is|are)\s+(?:now\s+)?live\b",
        re.IGNORECASE,
    )),
    (ClaimType.COMMIT_MADE, re.compile(
        r"\bcommit(?:ted)?\s+(?:the\s+|all\s+|these\s+|those\s+)?(?:changes?|files?|work|everything|it)\b"
        r"|\bchanges?\s+(?:are\s+|have\s+been\s+|were\s+)?committed\b"
        r"|\bcreated\s+(?:a\s+|the\s+)?commit\b",
        re.IGNORECASE,
    )),
    (ClaimType.PUSHED, re.compile(
        r"\bpushed\s+(?:the\s+|to\s+|everything|changes?|it|commits?|branch)\b"
        r"|\bchanges?\s+(?:are\s+|have\s+been\s+|were\s+)?pushed\b",
        re.IGNORECASE,
    )),
    (ClaimType.TASK_DONE, re.compile(
        r"\b(?:task|fix|feature|implementation|migration|refactor)\s+"
        r"(?:is\s+)?(?:complete|done|finished|working)\b"
        r"|\b(?:everything|all)\s+(?:is\s+|now\s+)?(?:working|done|complete|in\s+place)\b"
        r"|\bbug\s+(?:is\s+)?fixed\b|\bfixed\s+the\s+(?:bug|issue|problem|error)\b",
        re.IGNORECASE,
    )),
]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
# Markdown noise we strip before matching: bold/italics, checkmarks, bullets.
_MD_NOISE = re.compile(r"[*_#>]|^\s*[-•✅✓✔☑]+\s*", re.MULTILINE)


def _sentences(text: str) -> list[str]:
    cleaned = _MD_NOISE.sub(" ", text)
    return [s.strip() for s in _SENTENCE_SPLIT.split(cleaned) if s.strip()]


def extract_claims(session: Session) -> list[Claim]:
    claims: list[Claim] = []
    for event in session.events:
        if event.kind is not EventKind.TEXT:
            continue
        # Masked on the raw text, before markdown noise is stripped: a quote
        # is shown in every report (SEC-235).
        for sentence in _sentences(redact_secrets(event.text)):
            if len(sentence) > 400:
                continue  # code blocks / pasted output, not prose
            if _DISQUALIFIERS.search(sentence):
                continue
            claims.extend(_sentence_claims(sentence, event.index))
    return _dedupe(claims)


def _detail(claim_type: ClaimType, match: re.Match) -> str | None:
    """The claim's detail from its match, or None when it is no claim."""
    groups = [g for g in match.groups() if g]
    if claim_type is ClaimType.TEST_COUNT:
        passed, total = groups[0], groups[1]
        # "38 of 40 pass" admits failures; only "N of N" claims all pass.
        count, of = log_int(passed), log_int(total)
        return passed if count is not None and count == of and count > 0 else None
    if claim_type is ClaimType.COMMIT_ID:
        # A digit sets an id apart from a hex-letter word ("defaced").
        return groups[0].lower() if any(c.isdigit() for c in groups[0]) else None
    return groups[0] if groups else ""


def _sentence_claims(sentence: str, index: int) -> list[Claim]:
    found: list[tuple[ClaimType, str, str]] = []
    for claim_type, pattern in _PATTERNS:
        match = pattern.search(sentence)
        if not match:
            continue
        detail = _detail(claim_type, match)
        if detail is None:
            continue
        found.append((claim_type, match.group(0).strip(), detail))
        # no break: "tests pass and I committed" is two claims
    if any(t is ClaimType.TEST_COUNT for t, _, _ in found):
        # "12 of 12 tests pass" is one claim, checked with its count.
        found = [f for f in found if f[0] is not ClaimType.TESTS_PASS]
    several = len(found) > 1
    # A sentence holding two claims is quoted by the phrase each matched,
    # so a finding shows what it judged, not its neighbour's words.
    return [Claim(type=claim_type,
                  quote=(phrase if several else sentence)[:200],
                  event_index=index,
                  detail=detail,
                  sentence=sentence[:400] if several else "")
            for claim_type, phrase, detail in found]


def _dedupe(claims: list[Claim]) -> list[Claim]:
    """Drop repeats of the same claim type within the same text event."""
    seen: set[tuple] = set()
    unique: list[Claim] = []
    for claim in claims:
        key = (claim.type, claim.event_index, claim.detail)
        if key in seen:
            continue
        seen.add(key)
        unique.append(claim)
    return unique
