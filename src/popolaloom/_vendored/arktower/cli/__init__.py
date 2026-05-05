"""Vendored CLI dependency-wiring helpers (subset).

Only :mod:`popolaloom._vendored.arktower.cli.deps` is vendored, and only
its :func:`migrations_dir` helper — the only symbol PopolaLoom uses from
``arktower.cli`` (per
:func:`popolaloom.daemon.repository._arktower_migrations_dir`).
"""
