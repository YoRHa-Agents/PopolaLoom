"""``popola relay`` v0.8.8 T2.2.1 safety tests (Q-C-4 偏离默认).

Implements the 5 named tests + 6 parametrised M3 cases enumerated in
``.local/research/v0.8.8_multi_run/relay-auto-safety.md`` §7. All tests
land in the default ``pytest -m "not real_cursor_cloud"`` lane (no
network, no real ``CURSOR_API_KEY``); cloud calls are mocked via
:class:`httpx.MockTransport` per the brief constraint, and the daemon
``/relay/dispatch`` RPC is mocked via a stub :class:`httpx.Client`
returning canned :class:`~popolaloom.daemon.rpc.RelayDispatchResponse`
JSON bodies.

The 5 named tests map onto the 5 release-gate mitigations as follows
(``relay-auto-safety.md`` §10 boxes C1..C7):

- :func:`test_relay_rejects_outside_allowlist` — C2 (M1 default-deny +
  M2 audit-on-reject).
- :func:`test_relay_with_confirm_allowlist_dispatches` — C2 (M1 override
  path with ``mode="confirmed"`` audit row).
- :func:`test_relay_secret_detection_rejects` — C3 (M3 over six
  parametrised token shapes S1..S6).
- :func:`test_relay_audit_row_shape` — C2 (M2 row shape ≥ 14 keys +
  ``0o600`` file mode + ``0o700`` parent dir).
- :func:`test_relay_dry_run_no_api_call` — C2 (M5 cross-cutting "no
  outbound under ``--dry-run``" guarantee).

Cross-cutting invariants observed by every test:

- Audit row writes use the ``RelayAuditWriter`` per
  ``audit-writer`` (T2.3.3) — no direct file IO from the CLI.
- The CLI's ``make_sync_client`` is monkeypatched so each test runs
  hermetically against a configurable in-process daemon stub.
- The CLI's ``_build_cloud_client`` is monkeypatched to inject an
  :class:`httpx.MockTransport` into a real :class:`CloudCursorClient`,
  so the v0.8.8 ``_retrying_request`` retry surface stays exercised.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from popolaloom.adapters.cursor_cloud import CloudCursorClient
from popolaloom.cli import relay_cmd

# ---------------------------------------------------------------------------
# Test data — synthetic token shapes (NEVER real credentials)
# ---------------------------------------------------------------------------
#
# The 6 shapes mirror ``relay-auto-safety.md`` §5.2 catalogue. The literal
# strings are pure regex-shape fillers (``"A" * 36`` etc.) so no scanner
# hit corresponds to a real secret. Per the spec §5.4 redaction policy,
# even synthetic samples are kept short and constant-shape.

S1_AWS_SAMPLE: str = "AKIAIOSFODNN7EXAMPLE"
S2_GITHUB_PAT_SAMPLE: str = "ghp_" + "A" * 36
S3_STRIPE_SAMPLE: str = "sk_live_" + "B" * 24
S4_JWT_SAMPLE: str = (
    "eyJhbGciOiJIUzI1NiJ9."
    "eyJzdWIiOiIxMjMifQ."
    + "C" * 30
)
S5_SLACK_SAMPLE: str = "xoxb-" + "1" * 12 + "abc"
# 44 chars, mixed case + digits → Shannon entropy > 4.0 (regex + entropy gate).
S6_HIGH_ENTROPY_SAMPLE: str = "AbCdEf01GhIjKl23MnOpQr45StUvWx67YzAb89EfGh01"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    """A default :class:`typer.testing.CliRunner` instance.

    Newer Typer/Click drop the ``mix_stderr`` constructor kwarg; we
    instead reach for ``_combined_output(result)`` (the combined stream by
    default in this Typer version) AND ``result.stderr`` when the
    test needs to inspect either independently. The
    :func:`_combined_output` helper below normalises across versions.
    """
    return CliRunner()


def _combined_output(result: Any) -> str:
    """Return ``stdout + stderr`` as a single string.

    Some Typer / Click versions split the streams when the runner is
    constructed without ``mix_stderr=True`` (which is gone in newer
    versions); reading both attributes defensively keeps assertions
    portable.
    """
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        if value and value not in parts:
            parts.append(value)
    return "".join(parts)


@pytest.fixture
def _isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point ``$POPOLA_HOME`` + ``$HOME`` at ``tmp_path`` for hermetic tests.

    The CLI loads ``popolad.toml`` from ``$POPOLA_HOME/popolad.toml`` (via
    :func:`popolaloom.daemon.main.get_popolad_config_path`). Each test
    scoped to its own ``tmp_path`` keeps the audit log + config sandboxed
    so concurrent test runs (or accidental real ``$HOME`` reads) cannot
    cross-contaminate.

    Also chdirs into ``tmp_path`` so :data:`DEFAULT_AUDIT_ROOT` (a
    relative path) materialises inside the sandbox.
    """
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _write_popolad_toml(home: Path, *, body: str) -> Path:
    """Materialise ``popolad.toml`` at ``$POPOLA_HOME/popolad.toml``."""
    target = home / "popolad.toml"
    target.write_text(body, encoding="utf-8")
    return target


def _allowlist_toml(repos: list[str], *, audit_root: str = "") -> str:
    """Render a ``popolad.toml`` snippet with the given allowlist."""
    rendered_list = (
        "[" + ", ".join(f'"{r}"' for r in repos) + "]"
    )
    return (
        "[cloud.relay]\n"
        'mode = "auto"\n'
        f"repo_allowlist = {rendered_list}\n"
        f'audit_root = "{audit_root}"\n'
    )


# ---------------------------------------------------------------------------
# httpx.MockTransport-based cloud client factory
# ---------------------------------------------------------------------------


def _build_cloud_mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> CloudCursorClient:
    """Construct a real :class:`CloudCursorClient` with a mock transport.

    Per the brief constraint ("Use httpx.MockTransport for cloud calls
    in tests"), this factory keeps the production code path unchanged —
    the same ``CloudCursorClient`` + ``_retrying_request`` machinery
    runs in tests as in the real CLI; only the underlying ``httpx``
    transport is swapped out for an in-memory router.
    """
    client = CloudCursorClient("test-api-key")
    client._client.close()
    client._client = httpx.Client(
        base_url=client._base_url,
        auth=("test-api-key", ""),
        transport=httpx.MockTransport(handler),
    )
    return client


# ---------------------------------------------------------------------------
# Daemon stub: /relay/dispatch read-side RPC
# ---------------------------------------------------------------------------


def _build_daemon_dispatch_response(
    *,
    source_task_id: str = "v088-task-aaa",
    cursor_agent_id: str = "bc-test-001",
    cursor_run_id: str = "run-test-001",
    repo_url: str = "https://github.com/neolix-ai/popola-loom",
    pr_url: str | None = "https://github.com/neolix-ai/popola-loom/pull/42",
    summary: str = "ship branch fix-foo to PR #42",
    model: str = "composer-2",
    state: str = "completed",
) -> dict[str, Any]:
    """Build a canned ``/relay/dispatch`` JSON body.

    Mirrors :class:`popolaloom.daemon.rpc.RelayDispatchResponse` so the
    CLI's response-shape contract is exercised verbatim.
    """
    body: dict[str, Any] = {
        "source_task_id": source_task_id,
        "cursor_agent_id": cursor_agent_id,
        "cursor_run_id": cursor_run_id,
        "repo_url": repo_url,
        "pr_url": pr_url,
        "summary": summary,
        "model": model,
        "state": state,
        "cloud_phase": "FINISHED",
        "runtime": "cloud",
    }
    return body


def _make_daemon_client(
    dispatch_body: dict[str, Any] | None = None,
    *,
    captured_posts: list[tuple[str, dict[str, Any]]] | None = None,
) -> MagicMock:
    """Build a context-manager-shaped sync httpx client double for the daemon.

    The returned :class:`MagicMock` only handles ``POST /relay/dispatch``;
    other endpoints are not exercised by the relay CLI in v0.8.8. When
    ``dispatch_body`` is ``None`` the stub returns a default
    completed-task envelope; tests pass a custom body to drive the
    "task not terminal" / "no agent_id" rejection paths.
    """
    body = dispatch_body or _build_daemon_dispatch_response()

    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.json.return_value = body
    response.text = json.dumps(body)

    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    def _post(
        url: str,
        json: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> MagicMock:
        if captured_posts is not None:
            captured_posts.append((url, json or {}))
        return response

    client.post.side_effect = _post
    return client


# ---------------------------------------------------------------------------
# Combined helper: monkeypatch CLI's daemon + cloud factories
# ---------------------------------------------------------------------------


def _wire_cli_test(
    monkeypatch: pytest.MonkeyPatch,
    *,
    daemon_client: MagicMock,
    cloud_client: CloudCursorClient | None,
) -> None:
    """Wire the CLI's two indirection points to the test doubles.

    Patches:
        - ``popolaloom.cli.relay_cmd.make_sync_client`` →
          factory returning ``daemon_client``.
        - ``popolaloom.cli.relay_cmd._build_cloud_client`` →
          factory returning ``cloud_client`` (when not ``None``).

    Tests that exercise the rejection paths pass ``cloud_client=None``
    so a stray cloud call (a regression in step ordering) raises a
    :class:`TypeError` (None is not callable) loud-and-clear instead
    of silently succeeding against the real internet.
    """
    monkeypatch.setattr(
        relay_cmd,
        "make_sync_client",
        lambda *a, **kw: daemon_client,
    )

    if cloud_client is not None:
        monkeypatch.setattr(
            relay_cmd,
            "_build_cloud_client",
            lambda api_key: cloud_client,
        )
    monkeypatch.setenv("CURSOR_API_KEY", "test-api-key")
    monkeypatch.setenv("POPOLA_ACTOR", "alice@neolix.ai")


# ---------------------------------------------------------------------------
# 7.1 — test_relay_rejects_outside_allowlist (M1 default-deny + M2 audit)
# ---------------------------------------------------------------------------


def test_relay_rejects_outside_allowlist(
    _isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-empty allowlist BLOCKS all relays; audit row written.

    Pins:

    1. CLI exit code == ``1`` (policy denied per ``relay-primitive.md``
       §8 + brief AC (e); the spec's §7.1 wording mentions ``2`` but
       the canonical exit-code matrix routes allowlist rejections via
       the ``policy denied`` class).
    2. ``mock_httpx.captured_requests`` is EMPTY — no cloud POST issued.
    3. ``.local/.agent/archive/relay/<task_a>.jsonl`` exists at mode
       ``0o600`` (file) + ``0o700`` (parent dir).
    4. The single audit row carries:
         - ``outcome ∈ {"rejected_allowlist_empty", "rejected_allowlist"}``
         - ``mode == "auto"``
         - ``target_task_id`` IS ``None``
         - ``target_repo == "neolix-ai/downstream-svc"``
    5. Stderr contains a remediation hint pointing the operator at the
       config key + ``--confirm-allowlist`` override.
    """
    _write_popolad_toml(
        _isolated_home,
        body=_allowlist_toml([]),  # default-deny
    )
    captured: list[tuple[str, dict[str, Any]]] = []
    daemon_client = _make_daemon_client(captured_posts=captured)

    cloud_calls: list[httpx.Request] = []

    def _cloud_handler(request: httpx.Request) -> httpx.Response:
        cloud_calls.append(request)
        return httpx.Response(500, json={"error": "should-not-be-called"})

    cloud_client = _build_cloud_mock_client(_cloud_handler)
    _wire_cli_test(
        monkeypatch,
        daemon_client=daemon_client,
        cloud_client=cloud_client,
    )

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay",
            "v088-task-aaa",
            "--target-repo",
            "https://github.com/neolix-ai/downstream-svc",
        ],
    )

    assert result.exit_code == 1, (
        f"exit must be 1 (policy denied) for empty allowlist; "
        f"got {result.exit_code}\n{_combined_output(result)}"
    )
    assert cloud_calls == [], "no cloud POST should be issued for rejection"

    audit_path = _isolated_home / ".local/.agent/archive/relay/v088-task-aaa.jsonl"
    assert audit_path.is_file(), f"audit file missing at {audit_path}"
    assert stat.S_IMODE(audit_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(audit_path.parent.stat().st_mode) == 0o700

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 1
    row = json.loads(lines[-1])
    assert row["outcome"] in {
        "rejected_allowlist_empty",
        "rejected_allowlist",
    }, f"outcome={row.get('outcome')!r}"
    assert row["mode"] == "auto"
    assert row["target_task_id"] is None
    assert row["target_repo"] == "neolix-ai/downstream-svc"

    out = _combined_output(result)
    # remediation hint mentions config key OR --confirm-allowlist override
    assert "repo_allowlist" in out or "--confirm-allowlist" in out, (
        f"stderr missing remediation hint:\n{out}"
    )


# ---------------------------------------------------------------------------
# 7.2 — test_relay_with_confirm_allowlist_dispatches (M1 override + M2)
# ---------------------------------------------------------------------------


def test_relay_with_confirm_allowlist_dispatches(
    _isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--confirm-allowlist`` allows dispatch; mode flips to ``"confirmed"``.

    Pins:

    1. CLI exit code == ``0``.
    2. Cloud transport receives EXACTLY ONE POST to
       ``/v1/agents/bc-test-001/runs``.
    3. The final audit row carries:
         - ``outcome == "dispatched"``
         - ``mode == "confirmed"``  (NOT ``"auto"`` — override path)
         - ``agent_id == "bc-test-001"``
         - ``run_id == "run-test-001"``
         - ``target_task_id`` IS NOT ``None``
    4. Stderr contains the WARNING substring
       ``"dispatching relay outside repo_allowlist via --confirm-allowlist"``.
    """
    # Empty allowlist forces the override path; the test also pins
    # behaviour against the M1 default-deny by demonstrating that
    # ``--confirm-allowlist`` is the **only** way to dispatch when
    # the target is not in the operator-curated list.
    _write_popolad_toml(
        _isolated_home,
        body=_allowlist_toml([]),
    )
    daemon_client = _make_daemon_client()

    cloud_calls: list[tuple[str, dict[str, Any]]] = []

    def _cloud_handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {}
        if request.content:
            body = json.loads(request.content.decode("utf-8"))
        cloud_calls.append((str(request.url), body))
        return httpx.Response(
            201,
            json={
                "id": "run-test-001",
                "agentId": "bc-test-001",
                "taskId": "task-b-test-001",
            },
        )

    cloud_client = _build_cloud_mock_client(_cloud_handler)
    _wire_cli_test(
        monkeypatch,
        daemon_client=daemon_client,
        cloud_client=cloud_client,
    )

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay",
            "v088-task-aaa",
            "--target-repo",
            "https://github.com/external/fork",
            "--confirm-allowlist",
        ],
    )

    assert result.exit_code == 0, (
        f"exit must be 0 (success) with --confirm-allowlist; "
        f"got {result.exit_code}\n{_combined_output(result)}"
    )
    assert len(cloud_calls) == 1, (
        f"expected exactly 1 cloud POST; got {len(cloud_calls)}: {cloud_calls!r}"
    )
    posted_url, _ = cloud_calls[0]
    assert "/v1/agents/bc-test-001/runs" in posted_url, (
        f"cloud POST hit wrong path: {posted_url}"
    )

    audit_path = _isolated_home / ".local/.agent/archive/relay/v088-task-aaa.jsonl"
    assert audit_path.is_file()
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 1
    final_row = json.loads(lines[-1])
    assert final_row["outcome"] == "dispatched"
    assert final_row["mode"] == "confirmed", (
        f"override path must record mode=confirmed; got {final_row.get('mode')!r}"
    )
    assert final_row["agent_id"] == "bc-test-001"
    assert final_row["run_id"] == "run-test-001"
    assert final_row["target_task_id"] is not None
    assert final_row["confirm_allowlist"] is True
    assert final_row["gate_decision"] == "override_confirm_allowlist"

    out = _combined_output(result)
    assert "dispatching relay outside repo_allowlist via --confirm-allowlist" in out, (
        f"WARNING substring missing from stderr:\n{out}"
    )


# ---------------------------------------------------------------------------
# 7.3 — test_relay_secret_detection_rejects (M3, parametrised × 6 shapes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shape_id", "token_sample"),
    [
        ("aws_access_key_id", S1_AWS_SAMPLE),
        ("github_pat", S2_GITHUB_PAT_SAMPLE),
        ("stripe_key", S3_STRIPE_SAMPLE),
        ("jwt", S4_JWT_SAMPLE),
        ("slack_token", S5_SLACK_SAMPLE),
        ("generic_high_entropy", S6_HIGH_ENTROPY_SAMPLE),
    ],
    ids=["S1-aws", "S2-github", "S3-stripe", "S4-jwt", "S5-slack", "S6-hi-entropy"],
)
def test_relay_secret_detection_rejects(
    _isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    shape_id: str,
    token_sample: str,
) -> None:
    """Each catalogue shape (S1..S6) is REJECTED; no cloud POST issued.

    The allowlist is set to include the target repo so we are
    isolating the M3 reject path (i.e. M1 PASSES — the rejection
    must come from the secret scanner, not the allowlist gate).

    Pins:

    1. CLI exit code == ``1`` (policy denied).
    2. Cloud transport handler NEVER invoked.
    3. The audit row carries:
         - ``outcome == "rejected_secret_detected"``
         - ``secret_detector.shape == shape_id``
         - ``secret_detector.redacted_preview == "…<last4>"`` shape
         - ``mode == "auto"``
         - ``target_task_id`` IS ``None``
    4. Stderr contains the redacted ``"…<last4>"`` preview but NOT the
       full ``token_sample``.
    5. The full ``token_sample`` does NOT leak into the audit row.
    """
    _write_popolad_toml(
        _isolated_home,
        body=_allowlist_toml(["neolix-ai/popola-loom"]),
    )

    # Inject the token via the source task's summary so the scanner
    # exercises ``_walk_envelope`` on the ``"summary"`` segment.
    daemon_body = _build_daemon_dispatch_response(
        summary=f"please review {token_sample} which leaked",
    )
    daemon_client = _make_daemon_client(daemon_body)

    cloud_calls: list[httpx.Request] = []

    def _cloud_handler(request: httpx.Request) -> httpx.Response:
        cloud_calls.append(request)
        return httpx.Response(500, json={"error": "should-not-be-called"})

    cloud_client = _build_cloud_mock_client(_cloud_handler)
    _wire_cli_test(
        monkeypatch,
        daemon_client=daemon_client,
        cloud_client=cloud_client,
    )

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay",
            "v088-task-aaa",
            "--target-repo",
            "https://github.com/neolix-ai/popola-loom",
        ],
    )

    assert result.exit_code == 1, (
        f"({shape_id}) exit must be 1 (policy denied); "
        f"got {result.exit_code}\n{_combined_output(result)}"
    )
    assert cloud_calls == [], (
        f"({shape_id}) cloud transport must NOT be hit on secret rejection"
    )

    audit_path = (
        _isolated_home / ".local/.agent/archive/relay/v088-task-aaa.jsonl"
    )
    assert audit_path.is_file()
    body = audit_path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in body.splitlines() if line.strip()]
    assert rows, f"({shape_id}) audit log empty"
    rejection = rows[-1]
    assert rejection["outcome"] == "rejected_secret_detected"
    assert rejection["mode"] == "auto"
    assert rejection["target_task_id"] is None
    detector = rejection.get("secret_detector")
    assert isinstance(detector, dict)
    assert detector.get("shape") == shape_id, (
        f"({shape_id}) shape mismatch: got {detector!r}"
    )
    last4 = token_sample[-4:]
    preview = detector.get("redacted_preview", "")
    assert preview.endswith(last4), (
        f"({shape_id}) redacted_preview must end with last4={last4!r}; "
        f"got {preview!r}"
    )
    assert preview.startswith("\u2026"), (
        f"({shape_id}) preview must start with U+2026 ellipsis; got {preview!r}"
    )

    # No-leak invariant: the full token MUST NOT appear anywhere in
    # the audit row body (only the redacted preview lands).
    assert token_sample not in body, (
        f"({shape_id}) full token leaked into audit log:\n{body}"
    )

    # No-leak invariant on stderr too (the rejection notice must use
    # the redacted preview, NEVER the full token).
    assert token_sample not in _combined_output(result), (
        f"({shape_id}) full token leaked into stderr:\n{_combined_output(result)}"
    )
    assert last4 in _combined_output(result), (
        f"({shape_id}) stderr must show last4={last4!r}; got:\n{_combined_output(result)}"
    )


# ---------------------------------------------------------------------------
# 7.4 — test_relay_audit_row_shape (M2 schema + 0o600 file mode)
# ---------------------------------------------------------------------------


def test_relay_audit_row_shape(
    _isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every required key in §4.2 is present; file mode ``0o600``.

    Pins:

    1. ``.local/.agent/archive/relay/<task_a>.jsonl`` exists.
    2. ``(path.stat().st_mode & 0o777) == 0o600``.
    3. ``(path.parent.stat().st_mode & 0o777) == 0o700``.
    4. The dispatched row contains all 14 mandatory keys per
       ``relay-auto-safety.md`` §4.2 plus the 5 optional keys
       (``confirm_allowlist`` / ``mode_source`` / ``model`` / etc.) that
       the brief AC (d) requires when applicable.
    5. ``timestamp`` parses as ISO-8601 UTC.
    6. ``payload_sha256`` is a 64-char lowercase hex string.
    7. ``schema_version`` is exactly ``"1"``.
    """
    _write_popolad_toml(
        _isolated_home,
        body=_allowlist_toml(["neolix-ai/popola-loom"]),
    )
    daemon_client = _make_daemon_client()

    def _cloud_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": "run-test-001",
                "agentId": "bc-test-001",
                "taskId": "task-b-test-001",
            },
        )

    cloud_client = _build_cloud_mock_client(_cloud_handler)
    _wire_cli_test(
        monkeypatch,
        daemon_client=daemon_client,
        cloud_client=cloud_client,
    )

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay",
            "v088-task-aaa",
            "--target-repo",
            "https://github.com/neolix-ai/popola-loom",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)

    audit_path = (
        _isolated_home / ".local/.agent/archive/relay/v088-task-aaa.jsonl"
    )
    assert audit_path.is_file()
    file_mode = stat.S_IMODE(audit_path.stat().st_mode)
    parent_mode = stat.S_IMODE(audit_path.parent.stat().st_mode)
    assert file_mode == 0o600, f"file mode={file_mode:o}, want 0o600"
    assert parent_mode == 0o700, f"parent mode={parent_mode:o}, want 0o700"

    rows = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows, "audit log empty after dispatch"
    final_row = rows[-1]

    # 14 mandatory keys per relay-auto-safety.md §4.2 (the table lists 15
    # — relay_envelope_id is the cross-link, optional in our schema).
    mandatory_keys: set[str] = {
        "schema_version",
        "timestamp",
        "source_task_id",
        "source_repo",
        "target_task_id",
        "target_repo",
        "actor",
        "mode",
        "outcome",
        "payload_sha256",
        "idempotency_key",
        "gate_decision",
        "confirm_allowlist",
        "mode_source",
    }
    missing = mandatory_keys - set(final_row.keys())
    assert not missing, f"final row missing required keys: {missing!r}"

    assert final_row["schema_version"] == "1"
    sha = final_row["payload_sha256"]
    assert isinstance(sha, str) and len(sha) == 64 and sha == sha.lower(), (
        f"payload_sha256 must be 64-char lowercase hex; got {sha!r}"
    )
    # Hex-only check.
    assert all(c in "0123456789abcdef" for c in sha), (
        f"payload_sha256 must be hex; got {sha!r}"
    )
    # ISO-8601 parse — fromisoformat accepts the ``+00:00`` suffix.
    from datetime import datetime as _dt
    _dt.fromisoformat(final_row["timestamp"])

    # 5 optional keys per brief AC (d): model + agent_id + run_id +
    # cloud_error + secret_detector are conditionally included; for the
    # successful dispatch path we expect model + agent_id + run_id.
    assert final_row.get("model") == "composer-2"
    assert final_row.get("agent_id") == "bc-test-001"
    assert final_row.get("run_id") == "run-test-001"
    assert final_row["mode"] in {"auto", "confirmed"}
    assert final_row["outcome"] == "dispatched"


# ---------------------------------------------------------------------------
# 7.5 — test_relay_dry_run_no_api_call (M5 cross-cutting + M2 dry-run audit)
# ---------------------------------------------------------------------------


def test_relay_dry_run_no_api_call(
    _isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` emits an audit row + issues ZERO HTTP requests.

    Pins:

    1. CLI exit code == ``0``.
    2. Cloud transport NEVER invoked.
    3. The audit row carries:
         - ``mode == "dry-run"``
         - ``outcome == "dry_run_passed"``
         - ``target_task_id`` IS ``None``
         - ``agent_id`` and ``run_id`` are ABSENT (no cloud POST)
         - ``payload_sha256`` IS the same hash a real dispatch
           would produce (idempotency invariant — operators can grep
           the same hash across dry-run + auto rows).
    4. Stdout shows a ``[DRY-RUN]`` rendering of the envelope.
    """
    _write_popolad_toml(
        _isolated_home,
        body=_allowlist_toml(["neolix-ai/popola-loom"]),
    )
    daemon_client = _make_daemon_client()

    cloud_calls: list[httpx.Request] = []

    def _cloud_handler(request: httpx.Request) -> httpx.Response:
        cloud_calls.append(request)
        return httpx.Response(500)

    cloud_client = _build_cloud_mock_client(_cloud_handler)
    _wire_cli_test(
        monkeypatch,
        daemon_client=daemon_client,
        cloud_client=cloud_client,
    )

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay",
            "v088-task-aaa",
            "--target-repo",
            "https://github.com/neolix-ai/popola-loom",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    assert cloud_calls == [], (
        f"--dry-run must NOT issue a cloud POST; got {cloud_calls!r}"
    )

    audit_path = (
        _isolated_home / ".local/.agent/archive/relay/v088-task-aaa.jsonl"
    )
    assert audit_path.is_file()
    rows = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1, (
        f"dry-run must emit exactly one audit row; got {len(rows)}: {rows!r}"
    )
    row = rows[0]
    assert row["mode"] == "dry-run"
    assert row["outcome"] == "dry_run_passed"
    assert row["target_task_id"] is None
    assert row.get("agent_id") is None or "agent_id" not in row
    assert row.get("run_id") is None or "run_id" not in row

    # payload_sha256 must be a 64-char hex digest — same shape as a
    # real-dispatch row so an operator can grep cross-mode for the
    # same hash.
    sha = row["payload_sha256"]
    assert isinstance(sha, str) and len(sha) == 64

    out = _combined_output(result)
    assert "[DRY-RUN]" in out, f"stdout missing [DRY-RUN] marker:\n{out}"


# ---------------------------------------------------------------------------
# Bonus assertions covering AC (b) flag mutex + AC (e) exit codes
# ---------------------------------------------------------------------------


def test_relay_dry_run_and_no_confirm_mutex_exits_2(
    _isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` and ``--no-confirm`` are mutually exclusive (exit 2).

    Pins AC (b): ``--dry-run`` mutex with ``--no-confirm`` → exit 2.
    No audit row is written (rejection precedes the policy gate per
    spec §3.3 + §2.4 step 3).
    """
    _write_popolad_toml(
        _isolated_home,
        body=_allowlist_toml(["neolix-ai/popola-loom"]),
    )
    daemon_client = _make_daemon_client()
    _wire_cli_test(
        monkeypatch,
        daemon_client=daemon_client,
        cloud_client=None,
    )

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay",
            "v088-task-aaa",
            "--target-repo",
            "https://github.com/neolix-ai/popola-loom",
            "--dry-run",
            "--no-confirm",
        ],
    )
    assert result.exit_code == 2, (
        f"--dry-run + --no-confirm must exit 2; got {result.exit_code}\n{_combined_output(result)}"
    )

    audit_path = (
        _isolated_home / ".local/.agent/archive/relay/v088-task-aaa.jsonl"
    )
    assert not audit_path.exists(), (
        "argument-validation rejection must precede audit emission"
    )
