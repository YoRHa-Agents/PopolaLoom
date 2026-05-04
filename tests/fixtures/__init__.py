"""Shared test fixtures package (testing-matrix.md §5).

Houses cross-tier fixtures that need their own module for size /
clarity. The exposed ``real_popolad`` fixture (v0.2.2 Stage 3 / Tier 3
introduction) is also re-exported via :file:`tests/matrix/conftest.py`
so any test under ``tests/matrix/**`` can request it without an
explicit import.
"""

from __future__ import annotations
