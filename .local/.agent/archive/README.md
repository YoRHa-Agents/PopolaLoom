# .agent/archive/

Completed changes preserved with date prefix `<YYYY-MM-DD>-<change-id>/`.
Frozen at archive time + auto-generated `REPORT.md` summarising the change.

Archive is the read-mostly half of the lifecycle FSM
(see `.local/research/v8.3.0_design.md` Section 1.3). Source-of-truth specs
in `.local/memory/specs/` are mutated only after the change-gate composite
score >= 8.5 PASSES (W-3 / SI-3 for minor, >= 9.0 for major) per Rule A-4.
The mergeability check (shipped v8.2.5+) gates the merge.
