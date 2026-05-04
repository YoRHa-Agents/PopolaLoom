"""tests/matrix/tier2 — Tier 2 (Medium, integration-level) per testing-matrix.md §1.2.

Each module is < 200 LOC and exercises 2-3 modules together with mocked
subprocess / HTTP / time:

- ``test_supervisor_failure_paths``: mocked Popen failure modes asserting
  state transitions to FAILED + NDJSON emits ``task.failed``.
- ``test_dispatch_chain_integration``: in-process Popolad facade dispatch
  chain (legacy + graph paths) + adapter-failure handling.
- ``test_cli_httpx_mock_daemon``: typer CliRunner + monkey-patched
  httpx client returning canned responses for the 5 daemon endpoints.
- ``test_freezegun_time_handling``: time-locked envelopes / handles /
  probe uptime via :mod:`freezegun`.
- ``test_event_log_buffered_invariants``: concurrent append + close
  durability contracts of the buffered EventLog.
"""
