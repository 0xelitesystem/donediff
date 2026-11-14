# donediff audit

- **Transcript:** `s2-wf.jsonl`
- **Project:** `/work/app`
- **Score:** 92/100 (A)
- **Scope:** evidence from main + 0 sub-agent and 1 workflow-agent transcripts (0.0 MB read in 0.0 s)

## Claims

| Verdict | Claim | Evidence |
|---|---|---|
| ✓ VERIFIED | all 900 tests pass | `cd /work/app && npm test 2>&1 \| tail -3` succeeded (exit 0, exit code hidden by a pipe, output shows a pass) [in workflow wf_fx-001, agent b1build (build and test)] |
| ✓ VERIFIED | build is clean | `npm run build` succeeded (exit 0) [in workflow wf_fx-001, agent b1build (build and test)] |

## Gaming signals

- **MEDIUM**: [workflow wf_fx-001, agent b1build (build and test)] check piped into `tail`, so its exit code is lost: `npm test 2>&1 | tail -3`
