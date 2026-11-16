# Third-party notices

donediff is under the MIT licence (see LICENSE), except for the files listed here.

- `tests/fixtures/qwen/` holds one Qwen Code test file under the Apache License 2.0; see the notice and licence in
  that folder. Attribution: contract-chat-records.jsonl: Qwen Code contract test fixture, copied unmodified from
  github.com/QwenLM/qwen-code (commit 516e963f7a401053c18c23b32b007ec1fb70848f), Apache License 2.0; Copyright 2025
  Google LLC; Copyright 2025 Qwen. Licensed fixture, not a real run.
- `tests/fixtures/cline/` holds one Cline test file under the Apache License 2.0; see the notice and licence in that
  folder. Attribution: session.messages.json: Cline test fixture, copied unmodified from github.com/cline/cline
  (commit 146894b6a42a7a554c6ff37f7bb33fe5122cc33d), Apache License 2.0; Copyright 2026 Cline Bot Inc. Licensed
  fixture, not a real run.
- `tests/fixtures/goose/make_goose.py` contains three SQL table definitions (schema_version, sessions, messages)
  adapted from Goose, github.com/aaif-goose/goose (commit d8de0e789f1931cd54d6764dee931166b6c091ca,
  crates/goose/src/session/session_manager.rs), Apache License 2.0; see the licence in that folder. Changed:
  reformatted, IF NOT EXISTS removed. The rest of that file and every value it writes are donediff's own and
  synthetic, not a real run.
