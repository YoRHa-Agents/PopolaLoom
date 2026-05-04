"""tests/matrix — v0.2.1 5-tier testing matrix expansion (Tier 1 + Tier 2).

See ``.local/memory/specs/popolaloom/testing-matrix.md`` §2.1 for the
canonical directory layout. ``tier1/`` holds Simple unit-level cases;
``tier2/`` holds Medium integration-level cases.

Both tier subdirectories own their own ``__init__.py`` so pytest collects
them recursively without polluting the top-level ``tests/`` namespace.
"""
