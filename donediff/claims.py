"""Extract checkable claims from the agent's prose.

A claim is a sentence in an assistant text block that asserts something
about the state of the world: tests pass, the build is clean, a file was
created, work was committed. Hedged, negated, future, or conditional
sentences ("once tests pass...", "tests should pass") are not claims.
"""

from __future__ import annotations

import re
import unicodedata

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

# A file named as a markdown link, the way Codex CLI names files it touched:
# [hello.py](C:/work/hello.py). Read only by the file-created and file-edited
# phrasings, and only when the link text and the target name the same file
# and the target is a local path, not a URL (see _link_file).
_MD_FILE_LINK = r"\[`?([^\]\s`]+\.[A-Za-z0-9]{1,10})`?\]\(([^)\s]+)\)"
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:(?![\\/])|[A-Za-z][A-Za-z0-9+.-]+:[\\/]{2}")

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
        r"(?:`([^`\s]+\.[A-Za-z0-9]{1,10})`|" + _MD_FILE_LINK + r")",
        re.IGNORECASE,
    )),
    (ClaimType.FILE_EDITED, re.compile(
        r"\b(?:edited|updated|modified)\s+(?:the\s+(?:file\s+)?|file\s+)?"
        r"(?:`([^`\s]+\.[A-Za-z0-9]{1,10})`|" + _MD_FILE_LINK + r")",
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
    # A named command or script that ran (CEO ruling (b), 2026-11-19): the
    # name in backticks right before one phrase of a fixed list, "worked",
    # "ran successfully" or "exited with code 0" ("`python hello.py` worked",
    # "the `hello.py` script ran successfully"). Other words in between ("It
    # printed `hello` and exited with code 0") and a question are no claim.
    (ClaimType.COMMAND_OK, re.compile(
        r"`([^`\n]{1,200})`(?:\s+(?:script|command|program))?\s+"
        r"(?:worked|ran\s+successfully|exited\s+with\s+code\s+0)\b(?!.*\?\s*$)",
        re.IGNORECASE,
    )),
]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
# Markdown noise we strip before matching: emphasis markers (an underscore
# only at the edge of a word, so my_module.py keeps its own), headings and
# quotes at a line start, checkmarks, bullets. Code spans and markdown links
# are kept verbatim: file names and paths hold underscores and asterisks
# (SEC-239), and a stripped "/_build/" would name a file that does not exist.
_MD_NOISE = re.compile(r"\*+|(?<!\w)_+|_+(?!\w)|^[ \t]*(?:#{1,6}|>+)[ \t]*|^\s*[-•✅✓✔☑]+\s*",
                       re.MULTILINE)
_CODE_SPAN = re.compile(r"`[^`\n]*`|\[[^\]\n]*\]\([^)\s]*\)")


def _sentences(text: str) -> list[str]:
    parts: list[str] = []
    last = 0
    for span in _CODE_SPAN.finditer(text):
        parts += [_MD_NOISE.sub(" ", text[last:span.start()]), span.group()]
        last = span.end()
    cleaned = "".join(parts) + _MD_NOISE.sub(" ", text[last:])
    return [s.strip() for s in _SENTENCE_SPLIT.split(cleaned) if s.strip()]


# Invisible characters that can split a claim word: format characters (Cf)
# and the Hangul fillers, which render blank. U+200D (zero-width joiner)
# also joins emoji, so dropping it alone raises no signal.
_FILLERS = frozenset("ᅟᅠㅤﾠ")
_JOINER = "‍"
# The rest of Unicode's Default_Ignorable_Code_Point set, outside Cf (SEC-255):
# the combining grapheme joiner, the Khmer inherent vowels, variation
# selectors and the unassigned ignorables. Each renders as nothing.
_IGNORABLE = re.compile(
    "[͏឴឵᠋-᠍᠏⁥︀-️￰-￸\U000e0000-\U000e0fff]")
# A variation selector picks the look of the character before it, so one
# after an emoji (✔️) is ordinary text; after a Latin letter or a digit it
# only hides. Like the joiner, it raises the flag only there.
_SELECTOR = re.compile("[᠋-᠍᠏︀-️\U000e0100-\U000e01ef]")
# Cyrillic and Greek letters drawn like Latin ones, folded only inside a word
# that also holds Latin letters (a mixed-script word), so Russian or Greek
# prose stays as written.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "і": "i", "ѕ": "s", "ј": "j",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M",
    "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "Х": "X", "ο": "o", "α": "a", "ε": "e", "ρ": "p",
    "Ο": "O", "Α": "A", "Ε": "E", "Κ": "K", "Τ": "T",
    # SEC-255: Latin alpha and dotless i, Cyrillic shha, komi de and
    # palochka, Armenian seh, oh and vo.
    "ɑ": "a", "ı": "i", "һ": "h", "ԁ": "d", "ӏ": "l", "ս": "u", "օ": "o", "ո": "n",
})
_WORD_RUN = re.compile(r"\w+")
_LATIN = re.compile(r"[A-Za-z]")


def fold_text(text: str) -> tuple[str, bool]:
    """Assistant text as claim extraction reads it (SEC-245): NFKC form,
    invisible characters dropped, look-alike letters in mixed-script words
    folded to Latin. The flag says whether anything was hidden or disguised."""
    text = unicodedata.normalize("NFKC", text)
    changed = False
    def hidden(c: str) -> bool:
        return unicodedata.category(c) == "Cf" or c in _FILLERS or bool(_IGNORABLE.match(c))

    if _IGNORABLE.search(text) or any(unicodedata.category(c) == "Cf" or c in _FILLERS
                                      for c in text):
        changed = any(hidden(c) and c != _JOINER and not (
            _SELECTOR.match(c) and not (i and text[i - 1].isascii() and text[i - 1].isalnum()))
            for i, c in enumerate(text))
        text = "".join(c for c in text if not hidden(c))
    text = text.replace(" ", "\n").replace(" ", "\n")

    def fold(match: re.Match) -> str:
        nonlocal changed
        word = match.group()
        if word.isascii() or not _LATIN.search(word):
            return word
        folded = word.translate(_CONFUSABLES)
        changed = changed or folded != word
        return folded

    return _WORD_RUN.sub(fold, text), changed


def extract_claims(session: Session) -> list[Claim]:
    claims: list[Claim] = []
    for event in session.events:
        if event.kind is not EventKind.TEXT:
            continue
        # Masked on the raw text, before markdown noise is stripped: a quote
        # is shown in every report (SEC-235).
        for sentence in _sentences(redact_secrets(fold_text(event.text)[0])):
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
    if claim_type in (ClaimType.FILE_CREATED, ClaimType.FILE_EDITED) and match.group(1) is None:
        return _link_file(match.group(2), match.group(3))
    if claim_type is ClaimType.COMMAND_OK:
        return match.group(1).strip() or None
    return groups[0] if groups else ""


def _base(path: str) -> str:
    return re.split(r"[\\/]", path)[-1]


def _link_file(text: str, target: str) -> str | None:
    """The local file a markdown link names, or None when it names none.

    The target must be a path (no URL scheme; a drive letter is a path) whose
    file name is the link text's: [a.py](b.py) or [a.py](a.py#L3) is not read
    as a claim, because which file it means would be a guess.
    """
    if _URL_SCHEME.match(target) or _base(target) != _base(text):
        return None
    return target


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
