# donediff audit

- **Transcript:** `s12-pedit.jsonl`
- **Project:** `/work/app`
- **Score:** 50/100 (F)
- **Scope:** evidence from main + 1 sub-agent and 0 workflow-agent transcripts (0.0 MB read in 0.0 s)

## Claims

| Verdict | Claim | Evidence |
|---|---|---|
| ◐ STALE | All tests pass. | `pytest -q` passed, but core.py edited afterwards with no re-run before the claim [in main] |
