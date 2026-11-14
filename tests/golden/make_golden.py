"""Record the golden outputs that prove a refactor changes nothing a user sees.

Run `python tests/golden/make_golden.py` ONLY when an output change is intended;
tests/test_golden.py fails if any output differs from the committed files.

Corpus: the demo session, every fixture session except the secret one, and
inputs/coverage.jsonl, a made-up session that exercises every tool name the
checks look at (Bash is covered by the demo; this adds PowerShell, Write,
MultiEdit, NotebookEdit and Edit on a test file). Each session is audited with
and without --main-only, with no disk check, and rendered as terminal (with
colour), JSON and Markdown. Timing, byte counts, the transcript path and the
version are normalised so the files do not depend on the machine or release.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

from donediff import __version__  # noqa: E402
from donediff.cli import run_audit  # noqa: E402
from donediff.report import render_json, render_markdown, render_terminal  # noqa: E402

INPUTS = HERE / "inputs"
OUTPUTS = HERE / "outputs"
SKIP = {"s7-secret.jsonl"}  # key-shaped fixture: never rendered into a committed file


def _record(kind: str, content: list, extra: dict | None = None) -> dict:
    record = {"type": kind, "timestamp": "2026-06-10T12:00:00.000Z", "sessionId": "golden-coverage",
              "cwd": "/home/dev/golden", "message": {"role": kind, "content": content}}
    record.update(extra or {})
    return record


def _text(text: str) -> dict:
    return _record("assistant", [{"type": "text", "text": text}])


def _tool(tool_id: str, name: str, tool_input: dict) -> dict:
    return _record("assistant", [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}])


def _result(tool_id: str, content: str, is_error: bool = False) -> dict:
    return _record("user", [{"type": "tool_result", "tool_use_id": tool_id, "content": content,
                             "is_error": is_error}],
                   {"toolUseResult": (f"Error: Exit code 1\n{content}" if is_error
                                      else {"stdout": content, "stderr": "", "interrupted": False})})


def coverage_records() -> list[dict]:
    return [
        _text("Starting on the cache feature."),
        _tool("t1", "PowerShell", {"command": "python -m pytest -q"}),
        _result("t1", "3 passed in 0.10s"),
        _tool("t2", "Write", {"file_path": "/home/dev/golden/src/cache.py", "content": "CACHE = {}\n"}),
        _result("t2", "File created successfully at: /home/dev/golden/src/cache.py"),
        _tool("t3", "Write", {"file_path": "/home/dev/golden/tests/test_old.py", "content": ""}),
        _result("t3", "File written"),
        _tool("t4", "MultiEdit", {"file_path": "/home/dev/golden/tests/test_api.py", "edits": [
            {"old_string": "assert a == 1\n    assert b == 2", "new_string": "assert a == 1"},
            {"old_string": "def test_x():", "new_string": "@pytest.mark.skip\ndef test_x():"}]}),
        _result("t4", "Applied 2 edits"),
        _tool("t5", "Edit", {"file_path": "/home/dev/golden/tests/test_cache.py",
                             "old_string": "assert get() == 1", "new_string": "pass"}),
        _result("t5", "The file has been updated"),
        _tool("t6", "NotebookEdit", {"notebook_path": "/home/dev/golden/nb.ipynb", "new_source": "print(1)"}),
        _result("t6", "Updated cell"),
        _tool("t7", "PowerShell", {"command": "git commit -am 'Add cache' --no-verify"}),
        _result("t7", "[main 1a2b3c4] Add cache"),
        _tool("t8", "PowerShell", {"command": "npm run build"}),
        _result("t8", "Build failed: 1 error", is_error=True),
        _text("Created cache.py. All tests pass. The build is clean. I committed the changes. Done."),
    ]


def noresult_claude_records() -> list[dict]:
    """SEC-197: a run whose result never reached the log must not read as passed."""
    return [
        _text("Running the checks."),
        _tool("n1", "Bash", {"command": "npm run build"}),
        _result("n1", "built in 1.2s"),
        _tool("n2", "Bash", {"command": "python -m pytest -q"}),
        _text("The build is clean. All tests pass."),
    ]


NO_RESULT_INPUTS = {"noresult-claude.jsonl": noresult_claude_records}


def corpus() -> list[Path]:
    sessions = sorted(p for p in (REPO / "tests" / "fixtures" / "sessions" / "proj").glob("*.jsonl")
                      if p.name not in SKIP)
    return [REPO / "examples" / "demo-session.jsonl", INPUTS / "coverage.jsonl",
            *(INPUTS / name for name in NO_RESULT_INPUTS), *sessions]


def render(path: Path, main_only: bool) -> dict[str, str]:
    result = run_audit(str(path), check_disk=False, main_only=main_only)
    result.session.path = path.relative_to(REPO).as_posix()
    result.session.scan.seconds = 0.0
    result.session.scan.bytes = 0
    outputs = {"txt": render_terminal(result, color=True), "json": render_json(result),
               "md": render_markdown(result)}
    return {k: v.replace(__version__, "<version>") for k, v in outputs.items()}


def expected() -> dict[str, str]:
    """Every golden file name -> its content, computed now."""
    files: dict[str, str] = {}
    for path in corpus():
        for main_only in (False, True):
            mode = "main" if main_only else "all"
            for ext, text in render(path, main_only).items():
                files[f"{path.stem}.{mode}.{ext}"] = text
    return files


def write_inputs() -> None:
    INPUTS.mkdir(parents=True, exist_ok=True)
    (INPUTS / "coverage.jsonl").write_text("\n".join(json.dumps(r) for r in coverage_records()) + "\n",
                                          encoding="utf-8", newline="\n")
    for name, records in NO_RESULT_INPUTS.items():
        (INPUTS / name).write_text("\n".join(json.dumps(r) for r in records()) + "\n",
                                   encoding="utf-8", newline="\n")


if __name__ == "__main__":
    write_inputs()
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    files = expected()
    for name, text in files.items():
        (OUTPUTS / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"golden: {len(files)} files written")
