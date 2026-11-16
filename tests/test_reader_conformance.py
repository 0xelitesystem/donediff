"""Reader conformance (D087): one evidence rule, held by every registered reader.

Success comes only from an explicit success record. For every reader in
donediff.readers.READERS, a builder (tests/conformance/<reader>.py) writes a
synthetic session in each shape: no result, skipped or declined, cancelled,
failed, a fake "exit code 0" in printed text, a rewound call, and one real
success. A file claim ("I created `x.py`") and a test claim ("All tests
pass.") after any bad shape must never be VERIFIED, with and without the disk
check (the file is on disk, so only the log can keep it from verifying); after
the real success both must be VERIFIED, so this test can fail. A shape a
reader cannot express is listed in its builder's CANNOT with a one-line reason
and shows as a skip with that reason. A registered reader with no builder fails.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from donediff import cli
from donediff.models import ClaimType, EventKind, Verdict
from donediff.readers import READERS
from donediff.verify import NO_EVIDENCE_STATUS, SUCCESS_STATUS, evidence_of_success

CONFORMANCE = Path(__file__).resolve().parent / "conformance"
sys.path.insert(0, str(CONFORMANCE))

from _common import (BAD_SHAPES, CANCELLED, CLAIMS, FAILED, FILE, SHAPES,  # noqa: E402
                     SKIPPED, SUCCESS, TARGET, TESTS)

CLAIM_TYPES = {FILE: (ClaimType.FILE_CREATED, ClaimType.FILE_EDITED),
               TESTS: (ClaimType.TESTS_PASS,)}
NAMES = [r.name for r in READERS]


def builder(agent: str):
    """The reader's conformance builder; None when it has none."""
    path = CONFORMANCE / f"{agent.replace('-', '_')}.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"conformance_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDERS = {name: builder(name) for name in NAMES}


def _cases():
    for name in NAMES:
        module = BUILDERS[name]
        cannot = getattr(module, "CANNOT", {}) if module else {}
        for shape in SHAPES:
            for claim in CLAIMS:
                for disk in (False, True):
                    marks = ()
                    if (shape, claim) in cannot:
                        marks = pytest.mark.skip(reason=f"{name} cannot express "
                                                        f"{shape}/{claim}: {cannot[shape, claim]}")
                    yield pytest.param(name, shape, claim, disk, marks=marks,
                                       id=f"{name}-{shape}-{claim}-"
                                          f"{'disk' if disk else 'no-disk-check'}")


def _audit(tmp_path, name: str, shape: str, claim: str, disk: bool):
    module = BUILDERS[name]
    if module is None:
        pytest.fail(f"registered reader {name!r} has no conformance builder "
                    f"(tests/conformance/{name.replace('-', '_')}.py)")
    project = tmp_path / "project"
    project.mkdir()
    (project / TARGET).write_text("x = 1\n", encoding="utf-8")  # on disk: only the log decides
    target = module.build(project, shape, claim)
    return cli.run_audit(str(target), check_disk=disk, agent=module.AGENT)


@pytest.mark.parametrize("name", NAMES)
def test_every_registered_reader_has_a_builder(name):
    module = BUILDERS[name]
    assert module is not None, (f"registered reader {name!r} has no conformance builder "
                                f"(tests/conformance/{name.replace('-', '_')}.py)")
    assert module.AGENT == name
    for (shape, claim), reason in module.CANNOT.items():
        assert shape in SHAPES and claim in CLAIMS, (shape, claim)
        assert reason.strip() and "\n" not in reason, "one line, with its source"
    # the real success must be buildable for at least the file claim
    assert (SUCCESS, FILE) not in module.CANNOT


@pytest.mark.parametrize("name,shape,claim,disk", list(_cases()))
def test_reader_conformance(tmp_path, name, shape, claim, disk):
    result = _audit(tmp_path, name, shape, claim, disk)
    found = [f for f in result.findings if f.claim.type in CLAIM_TYPES[claim]]
    assert len(found) == 1, [(f.claim.type, f.verdict) for f in result.findings]
    finding = found[0]
    # The contract every reader keeps: result_seen is tracked, never None.
    calls = [e for e in result.session.events if e.kind is EventKind.TOOL_CALL]
    assert all(e.result_seen is not None for e in calls), "a reader left result_seen None"
    if shape == SUCCESS:
        assert finding.verdict is Verdict.VERIFIED, finding.evidence
    else:
        assert shape in BAD_SHAPES
        assert finding.verdict is not Verdict.VERIFIED, finding.evidence
        assert "succeeded (exit 0)" not in finding.evidence


def _not_run_cases():
    """(reader, shape) for the shapes where a command did not run, and the failed
    shape, each with the test claim (Agent Security ruling (b), 2026-11-16)."""
    for name in NAMES:
        module = BUILDERS[name]
        cannot = getattr(module, "CANNOT", {}) if module else {}
        unclear = getattr(module, "RUN_UNCLEAR", {}) if module else {}
        for shape in (SKIPPED, CANCELLED, FAILED):
            marks = ()
            if (shape, TESTS) in cannot:
                marks = pytest.mark.skip(reason=f"{name} cannot express {shape}/{TESTS}: "
                                                f"{cannot[shape, TESTS]}")
            elif shape in unclear:
                marks = pytest.mark.skip(reason=f"{name} cannot tell {shape} from a failure: "
                                                f"{unclear[shape]}")
            yield pytest.param(name, shape, marks=marks, id=f"{name}-{shape}")


@pytest.mark.parametrize("name,shape", list(_not_run_cases()))
def test_a_command_that_did_not_run_is_unverified(tmp_path, name, shape):
    """A declined, skipped or cancelled command proves nothing either way: a
    "tests pass" claim after it is exactly UNVERIFIED, never CONTRADICTED. A
    real failure stays CONTRADICTED."""
    result = _audit(tmp_path, name, shape, TESTS, False)
    found = [f for f in result.findings if f.claim.type is ClaimType.TESTS_PASS]
    assert len(found) == 1, [(f.claim.type, f.verdict) for f in result.findings]
    expected = Verdict.CONTRADICTED if shape == FAILED else Verdict.UNVERIFIED
    assert found[0].verdict is expected, found[0].evidence
    if shape != FAILED:
        runs = [e for e in result.session.events
                if e.kind is EventKind.TOOL_CALL and e.check_types]
        assert runs and all(not e.is_error and e.exit_code is None and not e.failure_hint
                            and e.status in NO_EVIDENCE_STATUS for e in runs), \
            [(e.is_error, e.exit_code, e.status) for e in runs]


def test_run_unclear_reasons_are_one_line():
    for name in NAMES:
        for shape, reason in getattr(BUILDERS[name], "RUN_UNCLEAR", {}).items():
            assert shape in (SKIPPED, CANCELLED), (name, shape)
            assert reason.strip() and "\n" not in reason, (name, shape)


def test_the_rule_sets_do_not_overlap():
    assert not NO_EVIDENCE_STATUS & SUCCESS_STATUS


def test_goose_rewound_marker_is_no_evidence():
    """Goose's marker for a pass logged before deleted rows stays in the set."""
    from donediff.readers import goose
    assert goose._REWOUND in NO_EVIDENCE_STATUS


@pytest.mark.parametrize("status", sorted(NO_EVIDENCE_STATUS))
def test_no_evidence_status_never_succeeds(status):
    from donediff.models import Event, EventKind as K
    for exit_code in (None, 0):
        event = Event(kind=K.TOOL_CALL, index=0, tool_name="Write", status=status,
                      result_seen=True, exit_code=exit_code)
        assert evidence_of_success(event)[0] is False


@pytest.mark.parametrize("name", NAMES)
def test_main_only_is_reported_only_when_given(tmp_path, name):
    """QA-023: every reader, and any new one, says --main-only only when it was passed."""
    import json

    from donediff.report import render_json, scope_line
    module = BUILDERS[name]
    project = tmp_path / "project"
    project.mkdir()
    target = module.build(project, SUCCESS, FILE)
    default = cli.run_audit(str(target), check_disk=False, agent=module.AGENT)
    assert default.session.main_only is False
    assert "--main-only" not in scope_line(default.session)
    assert json.loads(render_json(default))["scope"]["main_only"] is False
    asked = cli.run_audit(str(target), check_disk=False, agent=module.AGENT, main_only=True)
    assert asked.session.main_only is True
    assert "--main-only" in scope_line(asked.session)


_PREFIX = "abcd1234"
_IDS = (_PREFIX + "_1", _PREFIX + "_1x9")  # the first id is also a prefix of the second


def _two_sessions(name: str, tmp_path, monkeypatch) -> list:
    """Two sessions whose ids share a prefix, as the reader's discover() returns them."""
    from donediff.readers import cline, goose, opencode
    files = {
        "claude-code": lambda i: tmp_path / f"{i}.jsonl",
        "qwen": lambda i: tmp_path / f"{i}.jsonl",
        "codex": lambda i: tmp_path / f"rollout-2026-06-10T12-00-00-{i}.jsonl",
        "gemini": lambda i: tmp_path / f"session-2026-06-10T12-00-{i}.json",
        "cline": lambda i: tmp_path / i / f"{i}{cline._SUFFIX}",
        "opencode": lambda i: opencode.session_path(tmp_path / "opencode.db", i, "v2"),
        "goose": lambda i: goose.session_path(tmp_path / "sessions.db", i),
    }
    assert name in files, f"registered reader {name!r} has no two-session case here (QA-024)"
    sessions = [files[name](i) for i in _IDS]
    reader = next(r for r in READERS if r.name == name)
    monkeypatch.setattr(reader, "discover", lambda project_filter=None: list(sessions))
    if name == "goose":  # Goose lists ids straight from its database
        monkeypatch.setattr(goose, "database_path", lambda: tmp_path / "sessions.db")
        monkeypatch.setattr(goose, "_usable", lambda db: True)
        monkeypatch.setattr(reader, "_list", lambda *a, **k: list(sessions))
    return reader, sessions


@pytest.mark.parametrize("name", NAMES)
def test_session_ids_resolve_through_one_rule(tmp_path, monkeypatch, capsys, name):
    """QA-024: an exact id wins, a unique prefix resolves, an ambiguous prefix is
    refused (exit 2, naming the matches), and `list` ids each name one session."""
    from donediff.readers import AmbiguousSession, short_ids
    reader, sessions = _two_sessions(name, tmp_path, monkeypatch)
    assert reader.resolve(_IDS[0]) is sessions[0]  # exact, though it prefixes the other
    assert reader.resolve(_IDS[1]) is sessions[1]
    assert reader.resolve(_IDS[1][:-1]) is sessions[1]  # a prefix of one id only
    with pytest.raises(AmbiguousSession) as refused:
        reader.resolve(_PREFIX)
    assert str(refused.value) == f"{_PREFIX} matches 2 sessions: {_IDS[0]}, {_IDS[1][:-1]}"
    assert cli.main(["audit", _PREFIX, "--agent", name]) == 2
    assert f"{_PREFIX} matches 2 sessions" in capsys.readouterr().err
    ids = short_ids(sessions, sessions, reader.session_keys)
    assert len(set(ids)) == len(ids)
    assert all(reader.resolve(i) is s for i, s in zip(ids, sessions))
