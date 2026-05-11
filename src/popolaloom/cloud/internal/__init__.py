"""popolaloom.cloud.internal — EXPERIMENTAL Connect-RPC adapter (path-B, v1.0.0 GA).

Per :file:`.local/.agent/active/v1.0.0-ga/DECISIONS.md` Q-22 (LOCKED), this
package is **EXPERIMENTAL** and is NOT part of the v1.x SemVer stability
surface. It speaks Cursor's reverse-engineered Connect-RPC protocol against
``https://api2.cursor.sh/aiserver.v1.BackgroundComposerService/*`` to
unlock the advanced dispatch controls (``--mode``, ``--max-mode``,
``--effort``, ``--time-budget``, ``--long-running``,
``--auto-proceed-after-plan``) that the public REST ``POST /v1/agents``
schema does NOT accept.

Stability commitment: NONE. The protocol surface is undocumented and may
change without notice when Cursor updates their internal services. The
``--auth-mode=session-jwt`` flag is **opt-in default-OFF** per Q-13; the
default REST path (``--auth-mode=rest``, the v0.10.0 baseline) remains
the stable surface.

When the path-B path stops working (e.g. Cursor changes the wire format),
the dispatch raises :class:`CursorCloudInternalError` with a hint pointing
at:

1. ``--auth-mode=rest`` to fall back to the REST path (which loses the
   path-B-only flags but at least dispatches).
2. ``BL-v1.x-rpc-protobuf`` (tracked in ``.local/feedbacks/TRACKER.md``)
   for the migration to a Cursor-supported wire format if/when one ships.

Module map:

- :mod:`.jwt_auth`              — JWT loader / validator / refresh helper.
- :mod:`.cursor_cloud_internal` — Connect-RPC client; one method
  :meth:`CursorCloudInternalClient.start_background_composer_from_snapshot`
  covering the 74-field ``StartBackgroundComposerFromSnapshotRequest``.
- :mod:`.flags`                 — Pure flag-shape helpers (``--mode`` /
  ``--time-budget`` parsers, preset expansion).
"""

from __future__ import annotations

__all__: list[str] = [
    "CursorCloudInternalClient",
    "CursorCloudInternalError",
    "JWTAuthError",
    "JWTBundle",
    "load_jwt_bundle",
]

from popolaloom.cloud.internal.cursor_cloud_internal import (
    CursorCloudInternalClient,
    CursorCloudInternalError,
)
from popolaloom.cloud.internal.jwt_auth import (
    JWTAuthError,
    JWTBundle,
    load_jwt_bundle,
)
