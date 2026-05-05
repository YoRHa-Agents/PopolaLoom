# .agent/active/

In-flight changes managed by the `change-driven` workflow (shipped v8.2.6+; see
`workflow-system/agent/templates/builtin/change-driven.yaml` for the stage definition).
Each subfolder is `<change-id>/` with the per-change artifact set:

- `goal.md` — intent statement (<= 200 tokens, hard ceiling 400)
- `acceptance.md` — testable AC checklist (<= 400 tokens, hard ceiling 800)
- `spec.md` — OpenSpec-style ADDED/MODIFIED/REMOVED delta (<= 1500 tokens)
- `tasks.md` — implementation checklist (<= 800 tokens)
- `STATUS.yaml` — machine-readable state block (<= 100 tokens)
- `owned_files.txt` — ownership manifest (<= 50 tokens, max 6 paths)
- `learnings.jsonl` — per-change reflections (capped 50 KB)

S-8 invariant: L3 task agents inside this folder MUST NOT write outside
their `owned_files.txt` set (plus the change folder + handoff outbox).
See `.local/research/v8.3.0_design.md` Section 1.1 for the full layout.
