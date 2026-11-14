"""Golden outputs: a refactor must not change a byte of what a user sees.

The expected files are made by tests/golden/make_golden.py and committed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden"
_spec = importlib.util.spec_from_file_location("make_golden", GOLDEN / "make_golden.py")
make_golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_golden)

EXPECTED = make_golden.expected()


def test_coverage_input_matches_generator():
    committed = (GOLDEN / "inputs" / "coverage.jsonl").read_text(encoding="utf-8")
    import json
    assert [json.loads(line) for line in committed.splitlines() if line] == make_golden.coverage_records()


def test_no_missing_or_extra_golden_files():
    on_disk = {p.name for p in (GOLDEN / "outputs").iterdir()}
    assert on_disk == set(EXPECTED)


def test_coverage_session_hits_every_checked_tool_name():
    names = {r["message"]["content"][0].get("name") for r in make_golden.coverage_records()}
    assert {"PowerShell", "Write", "MultiEdit", "NotebookEdit", "Edit"} <= names


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_golden_output_unchanged(name):
    committed = (GOLDEN / "outputs" / name).read_text(encoding="utf-8")
    assert EXPECTED[name] == committed, f"{name} changed; rerun make_golden.py only if intended"
