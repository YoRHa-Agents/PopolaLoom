"""Tier 4 — Structured / langgraph 真子图 (slow + real_graph).

Per testing-matrix.md §1.4 + §0.1 — Tier 4 cases use **real**
``langgraph`` SqliteSaver (no mocking the subgraph) + interrupt/resume
+ thread_id isolation.  Mock CLI (``tests/fixtures/mock_cli/``) is the
**only** thing mocked — every subgraph node, checkpointer, and graph
DAG is real.

All cases here are double-marked ``@pytest.mark.slow @pytest.mark.real_graph``
so the default lane skips them; the weekly cron runs the slow lane.
"""
