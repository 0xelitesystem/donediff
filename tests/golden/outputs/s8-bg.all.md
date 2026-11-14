# donediff audit

- **Transcript:** `s8-bg.jsonl`
- **Project:** `/work/app`
- **Score:** 42/100 (F)
- **Scope:** evidence from the main transcript; no sub-agent or workflow transcripts found

## Claims

| Verdict | Claim | Evidence |
|---|---|---|
| ? UNVERIFIED | All tests pass. | no command that could back this claim was run before it |
| ✓ VERIFIED | The tests pass now. | `npm test` succeeded (exit 0, ran in the background, from its completion notice) [in main] |
| ✗ CONTRADICTED | Tests pass. | most recent relevant run failed (exit 1): `pytest -q` [in main] |
