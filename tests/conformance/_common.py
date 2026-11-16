"""Shared names for the reader conformance builders (D087).

Every registered reader has a builder here, tests/conformance/<reader>.py
(the reader's name with "-" as "_"), with:

- AGENT: the reader's --agent name;
- CANNOT: {(shape, claim): one-line reason, from the agent's source or log
  format, why this reader cannot express that shape};
- build(project, shape, claim) -> the audit target: a synthetic session whose
  working folder is `project`, holding one tool call in the given shape and
  then the claim.

Every builder writes synthetic data only, never a real agent log.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# The shapes, in order. Each bad shape must never back a claim; "success" must.
NO_RESULT, SKIPPED, CANCELLED, FAILED, FAKE_EXIT, REWOUND, SUCCESS = SHAPES = (
    "no_result",   # the call is in the log, its result never is
    "skipped",     # skipped or declined: the tool never ran
    "cancelled",   # cancelled or interrupted
    "failed",      # ended with an error or a non-zero exit
    "fake_exit",   # no structured success; the printed text claims exit 0 / success
    "rewound",     # left behind by a rewind
    "success",     # a real success: the claim must verify
)
BAD_SHAPES = SHAPES[:-1]

FILE, TESTS = CLAIMS = ("file", "tests")
TARGET = "x.py"
CLAIM_TEXT = {FILE: f"I created `{TARGET}` with the change.", TESTS: "All tests pass."}
PROMPT = "Please make the change and run the tests."
TEST_CMD = "python -m pytest tests/ -q"
PASS_OUT = "16 passed in 1.0s"
CONTENT = "x = 1\n"


def load_fixture(reader: str, filename: str):
    """A fixture builder module (tests/fixtures/<reader>/<filename>), loaded by path."""
    path = FIXTURES / reader / filename
    spec = importlib.util.spec_from_file_location(f"conformance_fixture_{reader}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def target(project: Path) -> str:
    """The claimed file's full path, with forward slashes."""
    return (project / TARGET).as_posix()
