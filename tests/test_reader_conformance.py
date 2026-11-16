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

from _common import (BAD_SHAPES, CLAIMS, FILE, SHAPES, SUCCESS, TARGET,  # noqa: E402
                     TESTS)

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
