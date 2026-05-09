"""Cloud marker-payload redaction tests (v0.9.2+).

Pins the contract that ``redact_cloud_marker_cmd`` strips ``api_key``
from the JSON marker built by
:meth:`popolaloom.adapters.cursor_cloud.CursorCloudAdapter.build_command`,
without disturbing other fields. The redaction is the security backstop
when a test or operator passes ``--cli-flag api_key=...`` — the
unredacted value reaches the supervisor for cloud spawn but the
persisted ``handle.cmd`` / NDJSON / ArkTower row see the placeholder.

Cross-reference: ``credentials.py`` REDACTION_PLACEHOLDER /
``server.py::_redact_cmd_for_persistence``.
"""

from __future__ import annotations

import json
from pathlib import Path

from popolaloom.adapters.cursor_cloud import (
    CLOUD_BUILD_COMMAND_MARKER,
    CursorCloudAdapter,
    redact_cloud_marker_cmd,
)
from popolaloom.daemon.server import _redact_cmd_for_persistence

# ── happy path: marker JSON with api_key ────────────────────────────────


def test_redact_replaces_api_key_in_marker_payload() -> None:
    adapter = CursorCloudAdapter()
    cmd = adapter.build_command(
        "fix the bug",
        cwd=Path("/tmp/repo"),
        extra={
            "repo_url": "https://github.com/o/r",
            "api_key": "cr_super_secret_key",
        },
    )

    redacted = redact_cloud_marker_cmd(cmd)
    assert redacted[:2] == CLOUD_BUILD_COMMAND_MARKER

    payload = json.loads(redacted[2])
    extra = payload["extra"]
    assert "cr_super_secret_key" not in redacted[2]
    assert extra["api_key"] == "<REDACTED:CURSOR_API_KEY>"
    # Other fields preserved verbatim.
    assert extra["repo_url"] == "https://github.com/o/r"
    assert payload["prompt"] == "fix the bug"


def test_redact_preserves_marker_when_no_api_key() -> None:
    adapter = CursorCloudAdapter()
    cmd = adapter.build_command(
        "design caching",
        extra={"repo_url": "https://github.com/o/r"},
    )
    redacted = redact_cloud_marker_cmd(cmd)
    assert redacted == cmd


def test_redact_passes_through_non_cloud_cmd() -> None:
    """Vanilla cursor-agent / claude / codex argv pass through unchanged."""
    cmd = ["/usr/local/bin/cursor-agent", "-p", "--trust", "fix bug"]
    assert redact_cloud_marker_cmd(cmd) == cmd


def test_redact_passes_through_malformed_marker() -> None:
    """A broken JSON payload is left as-is (no exception)."""
    cmd = [*CLOUD_BUILD_COMMAND_MARKER, "{not valid json"]
    out = redact_cloud_marker_cmd(cmd)
    assert out == cmd


def test_redact_returns_fresh_list_not_in_place_mutation() -> None:
    payload = '{"extra": {"api_key": "cr_x"}, "prompt": "p", "cwd": null}'
    cmd = [*CLOUD_BUILD_COMMAND_MARKER, payload]
    out = redact_cloud_marker_cmd(cmd)
    assert out is not cmd
    # Original input untouched
    assert "cr_x" in cmd[2]


# ── server-side wrapper ─────────────────────────────────────────────────


def test_server_redact_helper_round_trip() -> None:
    """``_redact_cmd_for_persistence`` is just the cursor_cloud helper."""
    adapter = CursorCloudAdapter()
    cmd = adapter.build_command(
        "round trip",
        extra={
            "repo_url": "https://github.com/o/r",
            "api_key": "cr_redact_me",
        },
    )
    out = _redact_cmd_for_persistence(cmd)
    assert "cr_redact_me" not in json.dumps(out)


def test_server_redact_helper_passthrough_for_non_cloud() -> None:
    cmd = ["python", "-c", "print('hi')"]
    assert _redact_cmd_for_persistence(cmd) == cmd
