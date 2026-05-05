"""Tier 5 — Project / end-to-end self-evolution dry-run (e2e + nightly).

Per testing-matrix.md §1.5 + §0.1 — Tier 5 cases run a full self-
evolution round through:

- Real ``python -m popolaloom.daemon`` subprocess (via the
  ``real_popolad`` fixture).
- Real ArkTower SQLite + LangGraph SqliteSaver.
- **Mock CLI** (``tests/fixtures/mock_cli/``) — no real LLM calls;
  the mocks emit the devola-flow 3-section L3 contract so the
  inner-gate parser sees a passing composite_score.

All cases here are double-marked ``@pytest.mark.e2e @pytest.mark.nightly``
so the default lane skips them; nightly cron runs them.
"""
