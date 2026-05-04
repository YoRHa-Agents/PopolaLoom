"""Real CLI smoke tests (testing-matrix.md §1.3 + roadmap v0.2.2 §3.3).

These tests exercise the actual ``cursor-agent`` / ``claude`` /
``codex`` binaries against a real popolad daemon.  They are gated by
``@pytest.mark.real_cli`` and skip automatically when the binary is
not on ``$PATH`` (``shutil.which`` returns None) so they're safe to
collect in default CI lanes.
"""

from __future__ import annotations
