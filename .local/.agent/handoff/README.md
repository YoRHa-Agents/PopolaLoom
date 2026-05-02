# .agent/handoff/

Cross-agent handoff envelopes — append-only per Soul Rule S-9.

Naming: `<from>__<to>__<change-id>__<seq>.yaml`
- `seq` is a monotonic int starting at `0001` (zero-padded for sort-correct listing)
- Once an envelope file exists, it MUST NOT be modified or deleted
- New information goes in `seq + 1`

Schema shipped in v8.2.4 under `schemas/agent-workspace/handoff-envelope.yaml`.
Append-only enforcement: `tests/test_handoff_envelope_immutable.py` (CI lint)
plus the `lifecycle/check_envelope_append_only` hook (block in STRICT mode).
