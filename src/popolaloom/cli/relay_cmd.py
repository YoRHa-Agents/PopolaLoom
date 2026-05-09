"""``popola relay`` subcommand — v0.8.8 T2.2.1 (Q-C-4 偏离默认).

Cross-PR / cross-agent handoff primitive. Default mode is ``auto``
(deviates from the safe default of ``manual_confirm``); the deviation
is gated by **5 mandatory release-gate mitigations** enforced by the
spec ``relay-auto-safety.md`` §10:

- **M1** repo allowlist enforcement (``[cloud.relay] repo_allowlist``,
  default ``[]`` BLOCKS all relays);
- **M2** append-only audit log via :class:`RelayAuditWriter` at
  ``.local/.agent/archive/relay/<source_task_id>.jsonl`` (mode
  ``0o600``, parent dir ``0o700``); a row is written **before** the
  cloud POST so a mid-flight crash leaves a forensic trail;
- **M3** secret-redaction pre-flight via
  :func:`scan_envelope`; runs **before** the allowlist gate so
  even an out-of-allowlist target with an embedded secret still
  emits a ``rejected_secret_detected`` row (no leak via stderr or
  audit text);
- **M4** RELEASE_NOTES callout (T2.3.2 ownership; not wired here);
- **M5** CI isolation tests at ``tests/cli/test_relay_safety.py``
  (T2.2.1 ownership) — five named tests + six parametrized M3 cases.

Order of operations (per ``relay-auto-safety.md`` §3.3 / brief AC (c)):

1. CLI parse + config load.
2. Resolve source ``task_a`` via daemon HTTP ``POST /relay/dispatch``
   (read-side RPC: returns ``cursor_agent_id`` / ``repo_url`` /
   ``summary`` / ``model``).
3. Build the relay envelope locally.
4. **Secret scan FIRST** (M3) — a hit short-circuits to
   ``outcome="rejected_secret_detected"`` + exit ``1``.
5. Allowlist gate (M1) — out-of-allowlist target with no
   ``--confirm-allowlist`` → ``rejected_allowlist`` + exit ``1``.
6. Dry-run check (``--dry-run``) — write
   ``mode="dry-run"`` + ``outcome="dry_run_passed"`` row + exit ``0``.
7. Idempotency check (sweep prior audit rows for matching
   ``(source_task, target_repo, idempotency_key)`` within
   ``[cloud.relay] idempotency_window_s`` → reuse existing
   ``target_task``).
8. Audit row pre-flight (``outcome="dispatch_inflight"``).
9. Cloud POST via :meth:`CloudCursorClient._retrying_request`
   (the v0.8.8 quota-aware wrapper — honors
   ``[cloud.backoff]`` + ``Retry-After``).
10. Final audit row (``outcome="dispatched"`` on success / one of
    the ``cloud_*_error`` outcomes on failure).
11. Print task_b id to stdout (or full JSON under ``--json``).

Exit codes (per ``relay-primitive.md`` §8 + brief AC (e)):

| Code | Class                  |
| ---- | ---------------------- |
| ``0``   | success / dry-run / idempotent skip |
| ``1``   | policy denied (allowlist / secret / payload) |
| ``2``   | invalid CLI args |
| ``75``  | cloud API error (5xx / network / rate-limit exhausted) |
| ``77``  | cloud auth error (401/403) |
| ``78``  | cloud feature unavailable / GitHub-app missing |
| ``100`` | cloud not found (agent deleted between dispatch and relay) |
| ``102`` | cloud conflict (``409 agent_busy`` when ``mode="fail_fast"``) |
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import typer

from popolaloom.adapters.cursor_cloud import (
    CloudCursorClient,
    CursorCloudAuthError,
    CursorCloudConflictError,
    CursorCloudError,
    CursorCloudFeatureUnavailableError,
    CursorCloudNotFoundError,
    CursorCloudRateLimitError,
    GithubAppMissingError,
    GithubAppPermissionError,
)
from popolaloom.relay.audit import DEFAULT_AUDIT_ROOT, RelayAuditWriter
from popolaloom.relay.secrets import Finding, scan_envelope

if TYPE_CHECKING:
    from popolaloom.daemon.main import CloudRelayConfig

logger = logging.getLogger(__name__)

__all__ = ["app", "relay_command"]


_GITHUB_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/?$")
"""Validation regex for ``--target-repo`` (per ``relay-primitive.md`` §2.3)."""

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
"""Validation regex for ``--idempotency-key`` (per spec §2.4 step 6)."""


# ── exit code mapping ────────────────────────────────────────────────────


_EXIT_SUCCESS: int = 0
_EXIT_POLICY_DENIED: int = 1
_EXIT_INVALID_ARGS: int = 2
_EXIT_CLOUD_API_ERROR: int = 75
_EXIT_CLOUD_AUTH_ERROR: int = 77
_EXIT_CLOUD_FEATURE_UNAVAILABLE: int = 78
_EXIT_CLOUD_NOT_FOUND: int = 100
_EXIT_CLOUD_CONFLICT: int = 102


# ── helpers ──────────────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    """Return ISO-8601 UTC timestamp with microsecond precision (audit format)."""
    return datetime.now(UTC).isoformat()


def _resolve_actor() -> str:
    """Resolve the operator identity for the audit ``actor`` field.

    Priority order per ``relay-primitive.md`` §7.3:
    ``$POPOLA_ACTOR`` env → ``$USER`` env → ``"<unknown>"``.
    (Git config lookup deferred — adds shell complexity vs. value.)
    """
    actor = os.environ.get("POPOLA_ACTOR", "").strip()
    if actor:
        return actor
    actor = os.environ.get("USER", "").strip()
    if actor:
        return actor
    return "<unknown>"


def _canonical_org_repo(value: str) -> str | None:
    """Return canonical ``org/repo`` form or ``None`` on malformed input.

    Strips ``https://github.com/`` / ``https://gitlab.com/`` prefix +
    trailing ``.git`` + trailing ``/``, then verifies the result matches
    ``r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"``. Lowercase is NOT applied
    (GitHub repo names are case-preserving but case-insensitive lookup
    is up to the operator's allowlist taste — keep verbatim for now).
    """
    raw = value.strip()
    for prefix in ("https://github.com/", "https://gitlab.com/", "git@github.com:"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    if raw.endswith(".git"):
        raw = raw[: -len(".git")]
    if raw.endswith("/"):
        raw = raw[:-1]
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", raw):
        return None
    return raw


def _payload_sha256(payload: dict[str, Any]) -> str:
    """Return canonical-JSON sha256 hex digest (per audit §4.3)."""
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _format_finding_preview(findings: list[Finding]) -> str:
    """Render the first finding as a stderr-safe redacted preview."""
    if not findings:
        return ""
    f0 = findings[0]
    return (
        f"shape={f0.shape} location={f0.location} preview={f0.redacted_preview}"
    )


def _make_audit_row(
    *,
    source_task_id: str,
    source_repo: str | None,
    target_task_id: str | None,
    target_repo: str,
    actor: str,
    mode: str,
    outcome: str,
    payload_sha: str,
    idempotency_key: str,
    gate_decision: str,
    confirm_allowlist: bool,
    mode_source: str,
    model: str | None = None,
    cloud_error: dict[str, Any] | None = None,
    secret_detector: dict[str, Any] | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Construct the 14-key audit row per ``relay-auto-safety.md`` §4.2.

    Optional keys (``model`` / ``cloud_error`` / ``secret_detector`` /
    ``agent_id`` / ``run_id``) are included only when applicable so a
    forensic ``jq`` query against the corpus can ``select(.cloud_error
    != null)`` without noise.
    """
    row: dict[str, Any] = {
        "schema_version": "1",
        "timestamp": _utc_now_iso(),
        "source_task_id": source_task_id,
        "source_repo": source_repo,
        "target_task_id": target_task_id,
        "target_repo": target_repo,
        "actor": actor,
        "mode": mode,
        "outcome": outcome,
        "payload_sha256": payload_sha,
        "idempotency_key": idempotency_key,
        "gate_decision": gate_decision,
        "confirm_allowlist": confirm_allowlist,
        "mode_source": mode_source,
    }
    if model is not None:
        row["model"] = model
    if agent_id is not None:
        row["agent_id"] = agent_id
    if run_id is not None:
        row["run_id"] = run_id
    if cloud_error is not None:
        row["cloud_error"] = cloud_error
    if secret_detector is not None:
        row["secret_detector"] = secret_detector
    return row


def _audit_root_for(cfg: CloudRelayConfig) -> Path:
    """Resolve the audit root path (config override → default)."""
    if cfg.audit_root:
        return Path(cfg.audit_root)
    return DEFAULT_AUDIT_ROOT


def _scan_idempotent_row(
    audit_path: Path,
    *,
    source_task_id: str,
    target_repo: str,
    idempotency_key: str,
    window_s: int,
) -> dict[str, Any] | None:
    """Return the most-recent ``outcome="dispatched"`` row matching the
    idempotency tuple within the ``window_s`` deadline; else ``None``.

    AC (f): same ``(source_task, target_repo, idempotency_key)`` within
    ``[cloud.relay] idempotency_window_s`` returns existing
    ``target_task`` with ``outcome="dispatched_idempotent"``.
    """
    if not audit_path.is_file():
        return None
    cutoff = datetime.now(UTC).timestamp() - max(0, window_s)
    matches: list[dict[str, Any]] = []
    try:
        with audit_path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("source_task_id") != source_task_id:
                    continue
                if row.get("target_repo") != target_repo:
                    continue
                if row.get("idempotency_key") != idempotency_key:
                    continue
                if row.get("outcome") != "dispatched":
                    continue
                ts_str = row.get("timestamp")
                if not isinstance(ts_str, str):
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str).timestamp()
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                matches.append(row)
    except OSError:
        return None
    if not matches:
        return None
    return matches[-1]


def _map_cloud_exception(exc: CursorCloudError) -> tuple[int, str]:
    """Map a :class:`CursorCloudError` subclass to ``(exit_code, outcome)``."""
    if isinstance(exc, CursorCloudAuthError):
        return _EXIT_CLOUD_AUTH_ERROR, "cloud_auth_error"
    if isinstance(exc, CursorCloudNotFoundError):
        return _EXIT_CLOUD_NOT_FOUND, "cloud_run_not_found"
    if isinstance(
        exc,
        (CursorCloudFeatureUnavailableError, GithubAppMissingError, GithubAppPermissionError),
    ):
        return _EXIT_CLOUD_FEATURE_UNAVAILABLE, "cloud_feature_unavailable"
    if isinstance(exc, CursorCloudConflictError):
        return _EXIT_CLOUD_CONFLICT, "cloud_conflict"
    if isinstance(exc, CursorCloudRateLimitError):
        return _EXIT_CLOUD_API_ERROR, "cloud_api_error"
    return _EXIT_CLOUD_API_ERROR, "cloud_api_error"


def _socket_path() -> Path:
    """Resolve the daemon UDS path (mirrors ``cli/main.py::_socket_path``)."""
    home = os.environ.get("POPOLA_HOME")
    base = Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    return base / "popolad.sock"


def make_sync_client(socket_path: Path | None = None) -> httpx.Client:
    """Construct a sync httpx client pointed at the popolad UDS.

    Tests monkey-patch this to inject a fake daemon transport — see
    ``tests/cli/test_relay_safety.py``.
    """
    sock = socket_path or _socket_path()
    transport = httpx.HTTPTransport(uds=str(sock))
    return httpx.Client(
        transport=transport,
        base_url="http://popolad",
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0),
    )


def _build_cloud_client(api_key: str) -> CloudCursorClient:
    """Construct a :class:`CloudCursorClient` from ``api_key``.

    Indirection point so tests can monkey-patch this function with a
    factory that injects an :class:`httpx.MockTransport` per the brief
    ("Use httpx.MockTransport for cloud calls in tests").
    """
    return CloudCursorClient(api_key)


# ── envelope construction ────────────────────────────────────────────────


def _build_prompt_body(
    *,
    message_prefix: str,
    pr_url: str | None,
    summary: str,
    source_task_id: str,
) -> str:
    """Assemble the final relay prompt per ``relay-primitive.md`` §5.3.

    Format:

        {prefix}\\n\\nFollow-up to: {prUrl or '(no PR opened)'}\\n\\n
        Context:\\n{summary[:4000]}

    The 4000-char summary truncation prevents prompt bloat / token-cost
    surprises (per spec §5.2 row "summary_for_context"); the full
    ``raw_summary_sha256`` is recorded in the audit row for forensic
    recovery.
    """
    prefix = (message_prefix or f"Follow-up relay from {source_task_id}").strip()
    pr_label = pr_url if pr_url else "(no PR opened by source run)"
    truncated_summary = summary[:4000] if summary else ""
    return (
        f"{prefix}\n\n"
        f"Follow-up to: {pr_label}\n\n"
        f"Context:\n{truncated_summary}"
    )


def _build_envelope(
    *,
    source_task_id: str,
    target_repo: str,
    prompt_body: str,
    summary: str,
    pr_url: str | None,
    model: str,
) -> dict[str, Any]:
    """Build the relay envelope dict for secret scanning + audit hash.

    The envelope shape mirrors ``relay-primitive.md`` §5.3 and feeds
    :func:`scan_envelope` (M3); the audit row's ``payload_sha256``
    derives from the same envelope so an operator can grep both
    surfaces for the same hash.
    """
    repos: list[dict[str, str]] = [{"url": target_repo, "ref": "main"}]
    if pr_url:
        repos[0]["prUrl"] = pr_url
    envelope: dict[str, Any] = {
        "schema_version": "1",
        "source_task_id": source_task_id,
        "prompt": prompt_body,
        "summary": summary,
        "repos": repos,
        "model": model,
        "auto_create_pr": False,
    }
    return envelope


# ── main relay command ───────────────────────────────────────────────────


def relay_command(  # noqa: C901, PLR0912, PLR0913, PLR0915 — the policy gate is intentionally
    # linear-and-explicit (one branch per spec §3.3 step) so a security
    # reviewer can trace the order of operations top-to-bottom; refactoring
    # into per-step helpers would hide the cross-cutting audit emission.
    task_a: str = typer.Argument(
        ...,
        help="popola task id of the COMPLETED source run.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Build envelope + run policy gate + write mode='dry-run' audit "
            "row; emit ZERO outbound HTTP. Mutex with --no-confirm."
        ),
    ),
    no_confirm: bool = typer.Option(
        False,
        "--no-confirm",
        help=(
            "Per-invocation opt-in to the Q-C-4 deviated default (relevant "
            "only when [cloud.relay] mode='confirm'). Mutex with --dry-run."
        ),
    ),
    target_repo: str = typer.Option(
        "",
        "--target-repo",
        help=(
            "Override target repo URL "
            "(default: extracted from task_a's source repo)."
        ),
    ),
    confirm_allowlist: bool = typer.Option(
        False,
        "--confirm-allowlist",
        help=(
            "Required when target repo is NOT in [cloud.relay] repo_allowlist; "
            "audit row records the override."
        ),
    ),
    message: str | None = typer.Option(
        None,
        "--message",
        help=(
            "Custom prompt prefix for run_b "
            "(default: 'Follow-up relay from <task_a>'). "
            "Empty string is rejected (exit 2)."
        ),
    ),
    idempotency_key: str = typer.Option(
        "",
        "--idempotency-key",
        help=(
            "Stable token (8..64 chars [A-Za-z0-9_-]) suppresses double-"
            "dispatch on operator retry; default = sha256(canonical_payload)[:16]."
        ),
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit dispatch summary as a single JSON object on stdout.",
    ),
) -> None:
    """v0.8.8 ``popola relay <task_a>`` — cross-PR / cross-agent handoff.

    Per the Q-C-4 deviation lock, the default is ``auto`` (no flag
    needed). The full policy gate (M1+M3) runs even on the deviated
    path so a fresh install with empty ``repo_allowlist`` cannot relay
    anywhere accidentally.
    """
    if not task_a:
        typer.echo("error: missing task_a (use `popola relay <task_a>`)", err=True)
        raise typer.Exit(code=_EXIT_INVALID_ARGS)

    # ── §2.4 step 3: --dry-run ⊕ --no-confirm mutex ──
    if dry_run and no_confirm:
        typer.echo(
            "error: --dry-run and --no-confirm are mutually exclusive",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)

    # ── §2.4 step 4: --message non-empty when supplied ──
    # Typer treats `--message ""` as supplying an empty string; we
    # reject explicitly per spec (an unset --message stays None and
    # falls back to the default prefix).
    if message is not None and message == "":
        typer.echo("error: --message must be non-empty when supplied", err=True)
        raise typer.Exit(code=_EXIT_INVALID_ARGS)

    # ── §2.4 step 5: --target-repo regex ──
    target_repo_canonical: str | None = None
    if target_repo:
        m = _GITHUB_URL_RE.fullmatch(target_repo)
        if m is None:
            typer.echo(
                f"error: --target-repo must match {_GITHUB_URL_RE.pattern}; "
                f"got {target_repo!r}",
                err=True,
            )
            raise typer.Exit(code=_EXIT_INVALID_ARGS)
        target_repo_canonical = f"{m.group(1)}/{m.group(2).rstrip('/')}"

    # ── §2.4 step 6: --idempotency-key regex ──
    if idempotency_key and not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
        typer.echo(
            f"error: --idempotency-key must match {_IDEMPOTENCY_KEY_RE.pattern}; "
            f"got {idempotency_key!r}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)

    # ── §2.4 step 7: load [cloud.relay] config ──
    from popolaloom.daemon.main import load_popolad_config

    try:
        popolad_config = load_popolad_config()
    except (ValueError, OSError) as exc:
        typer.echo(f"error: popolad.toml invalid: {exc}", err=True)
        raise typer.Exit(code=_EXIT_INVALID_ARGS) from exc
    relay_cfg: CloudRelayConfig = popolad_config.cloud.relay

    # ── §2.4 steps 1-2: resolve task_a + terminal-state gate via daemon ──
    try:
        with make_sync_client() as client:
            r = client.post(
                "/relay/dispatch",
                json={"source_task_id": task_a},
            )
    except httpx.ConnectError as exc:
        typer.echo(
            "error: popolad not running, run `popola popolad start` to start it",
            err=True,
        )
        logger.debug("daemon connect error: %r", exc)
        raise typer.Exit(code=_EXIT_INVALID_ARGS) from exc

    if r.status_code == 404:
        typer.echo(f"error: task_a not found: {task_a}", err=True)
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    if r.status_code == 400:
        detail = ""
        try:
            detail = (r.json() or {}).get("detail", "") or ""
        except (ValueError, json.JSONDecodeError):
            detail = r.text
        typer.echo(f"error: {detail}", err=True)
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    if r.status_code != 200:
        typer.echo(
            f"error: /relay/dispatch unexpected status {r.status_code}: {r.text}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)

    src_info = r.json() or {}
    cursor_agent_id = src_info.get("cursor_agent_id")
    if not isinstance(cursor_agent_id, str) or not cursor_agent_id:
        typer.echo(
            f"error: task_a has no cursor_agent_id (runtime={src_info.get('runtime')})",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)

    src_repo_url = src_info.get("repo_url") or ""
    src_pr_url = src_info.get("pr_url")
    src_summary = src_info.get("summary") or ""
    src_model = src_info.get("model") or ""

    # ── target_repo resolution + canonicalisation ──
    if target_repo_canonical is None:
        if not src_repo_url:
            typer.echo(
                "error: source task has no repo_url and --target-repo not set",
                err=True,
            )
            raise typer.Exit(code=_EXIT_INVALID_ARGS)
        target_repo_canonical = _canonical_org_repo(src_repo_url)
        if target_repo_canonical is None:
            typer.echo(
                f"error: could not canonicalise source repo url {src_repo_url!r}",
                err=True,
            )
            raise typer.Exit(code=_EXIT_INVALID_ARGS)

    source_repo_canonical = (
        _canonical_org_repo(src_repo_url) if src_repo_url else None
    )

    # ── §3.1 confirm prompt (mode=confirm + no --no-confirm) ──
    mode_source = "config"
    if relay_cfg.mode == "confirm" and not dry_run:
        if not no_confirm:
            if not sys.stderr.isatty():
                typer.echo(
                    'error: confirmation required but stdin is not a TTY; '
                    'pass --no-confirm or set [cloud.relay] mode = "auto"',
                    err=True,
                )
                raise typer.Exit(code=_EXIT_INVALID_ARGS)
        else:
            mode_source = "flag"

    if relay_cfg.mode == "auto" and no_confirm:
        mode_source = "flag"

    # ── envelope + payload construction ──
    actor = _resolve_actor()
    final_message = message if message is not None else ""
    prompt_body = _build_prompt_body(
        message_prefix=final_message,
        pr_url=src_pr_url if isinstance(src_pr_url, str) else None,
        summary=src_summary,
        source_task_id=task_a,
    )
    envelope = _build_envelope(
        source_task_id=task_a,
        target_repo=f"https://github.com/{target_repo_canonical}",
        prompt_body=prompt_body,
        summary=src_summary,
        pr_url=src_pr_url if isinstance(src_pr_url, str) else None,
        model=src_model,
    )
    payload_for_hash = {
        "prompt": prompt_body,
        "target_repo": target_repo_canonical,
        "model": src_model,
        "source_task": task_a,
    }
    payload_sha = _payload_sha256(payload_for_hash)
    resolved_idempotency_key = idempotency_key or payload_sha[:16]

    audit_root = _audit_root_for(relay_cfg)
    audit_writer = RelayAuditWriter(audit_root)

    # ── prompt-size cap check ──
    if len(prompt_body.encode("utf-8")) > relay_cfg.prompt_size_cap_bytes:
        if relay_cfg.dry_run_emits_audit or not dry_run:
            try:
                audit_writer.append(
                    _make_audit_row(
                        source_task_id=task_a,
                        source_repo=source_repo_canonical,
                        target_task_id=None,
                        target_repo=target_repo_canonical,
                        actor=actor,
                        mode="dry-run" if dry_run else "auto",
                        outcome="rejected_payload_too_large",
                        payload_sha=payload_sha,
                        idempotency_key=resolved_idempotency_key,
                        gate_decision="skipped_for_dry_run" if dry_run else "in_allowlist",
                        confirm_allowlist=confirm_allowlist,
                        mode_source=mode_source,
                        model=src_model or None,
                    )
                )
            except OSError as exc:
                logger.warning("audit write failed (payload_too_large path): %s", exc)
        typer.echo(
            f"error: relay prompt size "
            f"{len(prompt_body.encode('utf-8'))} bytes exceeds "
            f"[cloud.relay] prompt_size_cap_bytes={relay_cfg.prompt_size_cap_bytes}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_POLICY_DENIED)

    # ── §3.3 step 3: secret scan FIRST (M3) ──
    if relay_cfg.secret_scan_enabled:
        findings: list[Finding] = scan_envelope(envelope)
        if findings:
            f0 = findings[0]
            preview = _format_finding_preview(findings)
            secret_detector_meta: dict[str, Any] = {
                "shape": f0.shape,
                "location": f0.location,
                "redacted_preview": f0.redacted_preview,
                "count": len(findings),
            }
            try:
                audit_writer.append(
                    _make_audit_row(
                        source_task_id=task_a,
                        source_repo=source_repo_canonical,
                        target_task_id=None,
                        target_repo=target_repo_canonical,
                        actor=actor,
                        mode="dry-run" if dry_run else "auto",
                        outcome="rejected_secret_detected",
                        payload_sha=payload_sha,
                        idempotency_key=resolved_idempotency_key,
                        gate_decision="skipped_for_dry_run"
                        if dry_run
                        else "rejected_allowlist_empty",
                        confirm_allowlist=confirm_allowlist,
                        mode_source=mode_source,
                        model=src_model or None,
                        secret_detector=secret_detector_meta,
                    )
                )
            except OSError as exc:
                logger.warning("audit write failed (secret-detected path): %s", exc)
            typer.echo(
                f"ERROR: relay aborted — embedded secret detected: {preview}",
                err=True,
            )
            raise typer.Exit(code=_EXIT_POLICY_DENIED)

    # ── §3.3 step 4: allowlist gate (M1) ──
    in_allowlist = target_repo_canonical in tuple(relay_cfg.repo_allowlist)
    if not in_allowlist and not confirm_allowlist:
        rejection_outcome = (
            "rejected_allowlist_empty"
            if not relay_cfg.repo_allowlist
            else "rejected_allowlist"
        )
        rejection_gate = (
            "rejected_allowlist_empty"
            if not relay_cfg.repo_allowlist
            else "rejected_allowlist"
        )
        try:
            audit_writer.append(
                _make_audit_row(
                    source_task_id=task_a,
                    source_repo=source_repo_canonical,
                    target_task_id=None,
                    target_repo=target_repo_canonical,
                    actor=actor,
                    mode="dry-run" if dry_run else "auto",
                    outcome=rejection_outcome,
                    payload_sha=payload_sha,
                    idempotency_key=resolved_idempotency_key,
                    gate_decision=rejection_gate,
                    confirm_allowlist=confirm_allowlist,
                    mode_source=mode_source,
                    model=src_model or None,
                )
            )
        except OSError as exc:
            logger.warning("audit write failed (allowlist-rejected path): %s", exc)
        if not relay_cfg.repo_allowlist:
            typer.echo(
                f"error: target repo {target_repo_canonical!r} rejected — "
                f"[cloud.relay] repo_allowlist is empty (default-deny). "
                f"Set [cloud.relay] repo_allowlist or pass --confirm-allowlist.",
                err=True,
            )
        else:
            allowlist_preview = list(relay_cfg.repo_allowlist)[:5]
            typer.echo(
                f"error: target repo {target_repo_canonical!r} not in "
                f"[cloud.relay] repo_allowlist (first 5: {allowlist_preview}). "
                f"Set [cloud.relay] repo_allowlist or pass --confirm-allowlist.",
                err=True,
            )
        raise typer.Exit(code=_EXIT_POLICY_DENIED)

    if confirm_allowlist:
        gate_decision = (
            "in_allowlist" if in_allowlist else "override_confirm_allowlist"
        )
    else:
        gate_decision = "in_allowlist"

    if confirm_allowlist and not in_allowlist:
        typer.echo(
            f"WARNING: dispatching relay outside repo_allowlist via "
            f"--confirm-allowlist (target={target_repo_canonical}); "
            f"audit row recorded at "
            f"{audit_writer.path_for(task_a)}",
            err=True,
        )

    # ── §3.3 step 5: dry-run check (audit row + exit 0) ──
    if dry_run:
        if relay_cfg.dry_run_emits_audit:
            try:
                audit_writer.append(
                    _make_audit_row(
                        source_task_id=task_a,
                        source_repo=source_repo_canonical,
                        target_task_id=None,
                        target_repo=target_repo_canonical,
                        actor=actor,
                        mode="dry-run",
                        outcome="dry_run_passed",
                        payload_sha=payload_sha,
                        idempotency_key=resolved_idempotency_key,
                        gate_decision="skipped_for_dry_run",
                        confirm_allowlist=confirm_allowlist,
                        mode_source=mode_source,
                        model=src_model or None,
                    )
                )
            except OSError as exc:
                logger.warning("audit write failed (dry-run path): %s", exc)
        if json_out:
            typer.echo(
                json.dumps(
                    {
                        "mode": "dry-run",
                        "outcome": "dry_run_passed",
                        "source_task": task_a,
                        "source_repo": source_repo_canonical,
                        "target_repo": target_repo_canonical,
                        "target_task": None,
                        "model": src_model,
                        "prompt_sha256": payload_sha,
                        "audit_path": str(audit_writer.path_for(task_a)),
                        "dispatched_at": None,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            typer.echo(
                f"[DRY-RUN] would relay {task_a} → "
                f"https://github.com/{target_repo_canonical}\n"
                f"  payload_sha256={payload_sha}\n"
                f"  audit={audit_writer.path_for(task_a)}"
            )
        raise typer.Exit(code=_EXIT_SUCCESS)

    # ── §3.3 idempotency check (AC f) ──
    audit_path = audit_writer.path_for(task_a)
    prior = _scan_idempotent_row(
        audit_path,
        source_task_id=task_a,
        target_repo=target_repo_canonical,
        idempotency_key=resolved_idempotency_key,
        window_s=relay_cfg.idempotency_window_s,
    )
    if prior is not None:
        prior_target = prior.get("target_task_id")
        prior_agent = prior.get("agent_id")
        prior_run = prior.get("run_id")
        try:
            audit_writer.append(
                _make_audit_row(
                    source_task_id=task_a,
                    source_repo=source_repo_canonical,
                    target_task_id=prior_target,
                    target_repo=target_repo_canonical,
                    actor=actor,
                    mode="auto" if relay_cfg.mode == "auto" else "confirmed",
                    outcome="dispatched_idempotent",
                    payload_sha=payload_sha,
                    idempotency_key=resolved_idempotency_key,
                    gate_decision=gate_decision,
                    confirm_allowlist=confirm_allowlist,
                    mode_source=mode_source,
                    model=src_model or None,
                    agent_id=prior_agent if isinstance(prior_agent, str) else None,
                    run_id=prior_run if isinstance(prior_run, str) else None,
                )
            )
        except OSError as exc:
            logger.warning("audit write failed (idempotent path): %s", exc)
        if json_out:
            typer.echo(
                json.dumps(
                    {
                        "outcome": "dispatched_idempotent",
                        "source_task": task_a,
                        "target_task": prior_target,
                        "agent_id": prior_agent,
                        "run_id": prior_run,
                        "audit_path": str(audit_path),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            typer.echo(
                f"DISPATCHED-IDEMPOTENT {prior_target} (audit={audit_path})"
            )
        raise typer.Exit(code=_EXIT_SUCCESS)

    # ── §3.3 step 6: audit pre-flight + cloud POST + final audit ──
    final_mode = "confirmed" if confirm_allowlist and not in_allowlist else "auto"
    if relay_cfg.mode == "confirm" and not no_confirm:
        final_mode = "confirmed"

    try:
        audit_writer.append(
            _make_audit_row(
                source_task_id=task_a,
                source_repo=source_repo_canonical,
                target_task_id=None,
                target_repo=target_repo_canonical,
                actor=actor,
                mode=final_mode,
                outcome="dispatch_inflight",
                payload_sha=payload_sha,
                idempotency_key=resolved_idempotency_key,
                gate_decision=gate_decision,
                confirm_allowlist=confirm_allowlist,
                mode_source=mode_source,
                model=src_model or None,
            )
        )
    except OSError as exc:
        typer.echo(
            f"error: failed to write pre-flight audit row: {exc}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_CLOUD_API_ERROR) from exc

    # v0.9.2: route through the credential resolver so relay accepts
    # OS-keyring-stored credentials in addition to the historical
    # CURSOR_API_KEY env path. The audit row records "missing_api_key"
    # uniformly when no slot answers (the audit log never sees the
    # secret value either way).
    from popolaloom.credentials import resolve_cursor_api_key

    api_key = resolve_cursor_api_key()
    if not api_key:
        try:
            audit_writer.append(
                _make_audit_row(
                    source_task_id=task_a,
                    source_repo=source_repo_canonical,
                    target_task_id=None,
                    target_repo=target_repo_canonical,
                    actor=actor,
                    mode=final_mode,
                    outcome="cloud_auth_error",
                    payload_sha=payload_sha,
                    idempotency_key=resolved_idempotency_key,
                    gate_decision=gate_decision,
                    confirm_allowlist=confirm_allowlist,
                    mode_source=mode_source,
                    model=src_model or None,
                    cloud_error={
                        "status_code": None,
                        "error_code": "missing_api_key",
                        "error_message_first_500": (
                            "no Cursor API key configured "
                            "(set CURSOR_API_KEY env or run "
                            "`popola auth cursor set`)"
                        ),
                    },
                )
            )
        except OSError as exc:
            logger.warning("audit write failed (no-api-key path): %s", exc)
        typer.echo(
            "error: no Cursor API key configured "
            "(set CURSOR_API_KEY env or run `popola auth cursor set`)",
            err=True,
        )
        raise typer.Exit(code=_EXIT_CLOUD_AUTH_ERROR)

    cloud_payload = {
        "prompt": {"text": prompt_body},
        "model": {"id": src_model} if src_model else {},
    }
    if not src_model:
        cloud_payload.pop("model", None)

    cloud_client = _build_cloud_client(api_key)
    try:
        response = cloud_client._retrying_request(
            "POST",
            f"/v1/agents/{cursor_agent_id}/runs",
            json_body=cloud_payload,
            backoff_config=popolad_config.cloud.backoff,
        )
    except CursorCloudError as exc:
        exit_code, outcome = _map_cloud_exception(exc)
        try:
            audit_writer.append(
                _make_audit_row(
                    source_task_id=task_a,
                    source_repo=source_repo_canonical,
                    target_task_id=None,
                    target_repo=target_repo_canonical,
                    actor=actor,
                    mode=final_mode,
                    outcome=outcome,
                    payload_sha=payload_sha,
                    idempotency_key=resolved_idempotency_key,
                    gate_decision=gate_decision,
                    confirm_allowlist=confirm_allowlist,
                    mode_source=mode_source,
                    model=src_model or None,
                    cloud_error={
                        "status_code": exc.status_code,
                        "error_code": type(exc).__name__,
                        "error_message_first_500": str(exc)[:500],
                    },
                )
            )
        except OSError as os_exc:
            logger.warning("audit write failed (cloud-error path): %s", os_exc)
        typer.echo(f"error: cloud dispatch failed: {exc}", err=True)
        raise typer.Exit(code=exit_code) from exc
    finally:
        try:
            cloud_client.close()
        except Exception:  # noqa: BLE001 — close failure is non-fatal
            logger.debug("cloud client close failed", exc_info=True)

    new_run_id = response.get("id")
    new_agent_id = response.get("agentId") or response.get("agent_id") or cursor_agent_id
    target_task_id = response.get("taskId") or response.get("task_id") or new_run_id

    try:
        audit_writer.append(
            _make_audit_row(
                source_task_id=task_a,
                source_repo=source_repo_canonical,
                target_task_id=target_task_id if isinstance(target_task_id, str) else None,
                target_repo=target_repo_canonical,
                actor=actor,
                mode=final_mode,
                outcome="dispatched",
                payload_sha=payload_sha,
                idempotency_key=resolved_idempotency_key,
                gate_decision=gate_decision,
                confirm_allowlist=confirm_allowlist,
                mode_source=mode_source,
                model=src_model or None,
                agent_id=new_agent_id if isinstance(new_agent_id, str) else None,
                run_id=new_run_id if isinstance(new_run_id, str) else None,
            )
        )
    except OSError as exc:
        logger.warning("audit write failed (dispatched path): %s", exc)

    if json_out:
        typer.echo(
            json.dumps(
                {
                    "mode": final_mode,
                    "outcome": "dispatched",
                    "source_task": task_a,
                    "source_repo": source_repo_canonical,
                    "target_repo": target_repo_canonical,
                    "target_task": target_task_id,
                    "agent_id": new_agent_id,
                    "run_id": new_run_id,
                    "model": src_model,
                    "prompt_sha256": payload_sha,
                    "audit_path": str(audit_writer.path_for(task_a)),
                    "dispatched_at": _utc_now_iso(),
                },
                ensure_ascii=False,
            )
        )
    else:
        typer.echo(
            f"DISPATCHED {target_task_id} → https://github.com/{target_repo_canonical}\n"
            f"  audit={audit_writer.path_for(task_a)}"
        )
    raise typer.Exit(code=_EXIT_SUCCESS)


# A test-only sub-app exposing ``relay_command`` as the bare invocation —
# allows ``CliRunner.invoke(app, [task_a, ...])`` to drive the command in
# unit tests without depending on the full ``cli/main.py`` registration.
# In production ``cli/main.py`` registers ``relay_command`` directly on
# the parent app via ``app.command(name="relay")(relay_command)`` so the
# user-facing surface is ``popola relay <task_a>`` (no extra verb).
app = typer.Typer(
    name="relay",
    help=(
        "v0.8.8 cross-PR relay (Q-C-4 偏离默认: defaults to AUTO; "
        "5 mitigations enforced — see relay-auto-safety.md §10)."
    ),
    no_args_is_help=False,
    add_completion=False,
)
app.command(name="relay")(relay_command)
