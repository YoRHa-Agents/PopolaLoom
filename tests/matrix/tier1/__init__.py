"""tests/matrix/tier1 — Tier 1 (Simple, unit-level) per testing-matrix.md §1.1.

Each module is < 200 LOC and covers a focused contract:

- ``test_state_fsm_property``: hypothesis state-machine FSM invariants on
  :class:`popolaloom.daemon.state.StateStore`.
- ``test_event_envelope_property``: hypothesis property tests of the
  CloudEvents 1.0 envelope produced by :meth:`EventLog.append`.
- ``test_adapter_combinatorial``: parametrized 3-adapter × extras × cwd
  matrix asserting :meth:`build_command` determinism.
- ``test_pydantic_state_schema``: validation rules of
  :class:`popolaloom.daemon.graph.TaskState`.
- ``test_adapter_facade``: registry primitives + ``build_command``
  facade + ``is_available`` shutil.which gating.
"""
