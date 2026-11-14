# donediff audit

- **Transcript:** `noresult-claude.jsonl`
- **Project:** `/home/dev/golden`
- **Score:** 62/100 (D)
- **Scope:** evidence from the main transcript only (--main-only)

## Claims

| Verdict | Claim | Evidence |
|---|---|---|
| ✓ VERIFIED | The build is clean. | `npm run build` succeeded (exit 0) [in main] |
| ? UNVERIFIED | All tests pass. | no result for this run in the log: `python -m pytest -q` [in main] |
