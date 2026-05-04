"""HITL channel renderers — v0.3.0 Stage F4.B.

Per spec §3.4 + roadmap §12, every :class:`popolaloom.hitl.HITLPrompt`
fans out to ≥ 2 channels. Each channel module under this package
exposes a small surface:

- ``render_<channel>(prompt: HITLPrompt) -> <ChannelPayload>`` — pure
  rendering (no side-effects); easy to snapshot-test.
- ``parse_reply(<channel_event>) -> HITLReply`` — reverse mapping from
  the channel's reply payload to a uniform reply structure.
- (Optional) ``dispatch_<channel>(...)`` — actually sends the rendered
  payload via subprocess / API call. Errors propagate; never silenced
  (workspace rule "No Silent Failures").

Channels implemented:

- ``lark`` — :mod:`popolaloom.hitl.renderers.lark`
- ``ide``  — :mod:`popolaloom.hitl.renderers.ide`
- ``cli``  — :mod:`popolaloom.hitl.renderers.cli`
- ``mcp``  — :mod:`popolaloom.hitl.renderers.mcp`
- ``web``  — :mod:`popolaloom.hitl.renderers.web` (NiceGUI form stub
  for v0.3.0; full page deferred to v0.4.0)

The :class:`HITLReply` envelope (re-exported from :mod:`popolaloom.hitl`)
is the cross-renderer reply contract;
:meth:`popolaloom.hitl.sync.HITLStore.mark_answered` accepts it.
"""

from __future__ import annotations

# Re-export the canonical HITLReply + HITLChannel(Tag) so renderers and
# :mod:`popolaloom.hitl.sync` can `from popolaloom.hitl.renderers import
# HITLReply, HITLChannelTag`. The schemas live in the parent module to
# avoid circular imports between renderers and the core schemas.
from popolaloom.hitl import HITLChannel, HITLReply
from popolaloom.hitl.renderers import cli, ide, lark, mcp, web

# ``HITLChannelTag`` is a v0.3.0 F4 alias for :data:`HITLChannel` to
# keep import sites readable when the value is being used as a reply
# channel "tag" (e.g. in :class:`popolaloom.hitl.sync.HITLStore`).
HITLChannelTag = HITLChannel

__all__ = [
    "HITLChannel",
    "HITLChannelTag",
    "HITLReply",
    "cli",
    "ide",
    "lark",
    "mcp",
    "web",
]
