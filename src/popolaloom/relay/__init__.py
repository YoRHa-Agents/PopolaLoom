"""Relay safety helpers (v0.8.8 T2.3.3) — audit log + secret pre-flight.

This package bundles the two reusable safety primitives consumed by the
v0.8.8 ``popola relay`` CLI (T2.2.1). They are extracted into their own
package so they are testable in isolation and so future relay-adjacent
features (HITL Lark fan-out, archive GC, watchdog) can compose them
without taking on the full CLI dependency surface.

Public surface
==============

- :class:`popolaloom.relay.audit.RelayAuditWriter` — append-only NDJSON
  audit log writer with ``0o600`` file mode, ``0o700`` parent dir mode,
  ``O_APPEND`` atomicity, and ``fsync`` durability per
  ``relay-auto-safety.md`` §4 (mitigation M2).
- :func:`popolaloom.relay.secrets.scan_envelope` — secret pre-flight
  scanner returning a list of :class:`popolaloom.relay.secrets.Finding`
  per ``relay-auto-safety.md`` §5 (mitigation M3). Uses
  ``detect-secrets`` as the primary engine (lazy-imported via the
  ``relay-secrets`` optional-dependency extra) and falls back to a
  built-in regex + Shannon-entropy catalogue when the optional
  dependency is unavailable. The fallback path emits an explicit
  ``WARNING`` log per the workspace No-Silent-Failures rule.

Both modules are isolated from the rest of the codebase (no daemon
state, no RPC types) so they can be imported eagerly by short-lived CLI
processes without dragging in the heavier daemon graph.
"""

from __future__ import annotations

from popolaloom.relay.audit import (
    DEFAULT_AUDIT_ROOT,
    RelayAuditWriter,
)
from popolaloom.relay.secrets import (
    Finding,
    scan_envelope,
)

__all__ = [
    "DEFAULT_AUDIT_ROOT",
    "Finding",
    "RelayAuditWriter",
    "scan_envelope",
]
