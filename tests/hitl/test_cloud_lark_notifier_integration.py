"""B2 wiring regression — production notifier renders the v1 card.

Per REVIEW.md finding **B2** of
`.local/.agent/active/v0.8.7-cloud-hitl-prod/REVIEW.md`: the production
:class:`~popolaloom.hitl.cloud_bridge._DefaultCloudLarkNotifier` was
falling through to the legacy v0.5 generic HITL card builder. v0.8.7
B2 wiring binds the bridge's :class:`HITLStore` to the notifier so the
v1 ``cloud_hitl_request_card_v1`` template (per
``lark-card-spec.md`` §2.3) is the **production** card payload. This
test asserts that ``send_lark_card`` is invoked with a ``card_payload``
kwarg whose ``card_metadata.template_version == "v1"`` — i.e., the v1
versioned envelope from
:func:`popolaloom.lark.cloud_hitl_card.build_cloud_hitl_card`.
"""

from __future__ import annotations

from importlib import resources
from importlib import resources
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from popolaloom.hitl.cloud_bridge import (
    CloudHITLBridge,
    _DefaultCloudLarkNotifier,
)
from popolaloom.hitl.sync import HITLStore
from popolaloom.lark.cloud_hitl_card import (
    CARD_METADATA_KEYS,
    CARD_TEMPLATE_VERSION,
)

_MIGRATIONS = ("006_popola_hitl.sql", "007_popola_hitl_metadata.sql")


def _apply_migrations(conn: sqlite3.Connection) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for name in _MIGRATIONS:
        sql = (Path(resources.files("popolaloom.migrations")) / name).read_text(encoding="utf-8")
        conn.executescript(sql)
    conn.commit()


@pytest.fixture()
def hitl_store(tmp_path: Path) -> HITLStore:
    db_path = tmp_path / "b2.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    return HITLStore(conn)


def test_default_notifier_renders_v1_card_payload(
    hitl_store: HITLStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2 — ``_DefaultCloudLarkNotifier`` builds the v1 card and pins
    it via ``card_payload=`` so the legacy generic template never wins.

    Captures the keyword args passed to
    :func:`popolaloom.hitl.renderers.lark.send_lark_card` so the test
    can assert (a) ``card_payload`` is non-None, (b) it carries the
    full v1 ``card_metadata`` allowlist, and (c) the
    ``template_version`` is the v1 sentinel — proving the production
    notifier uses :func:`build_cloud_hitl_card` (not the v0.5 builder).
    """
    captured: dict[str, Any] = {}

    def _fake_send(prompt: Any, *args: Any, **kwargs: Any) -> Any:
        captured["prompt"] = prompt
        captured["args"] = args
        captured["kwargs"] = dict(kwargs)
        return None

    monkeypatch.setattr(
        "popolaloom.hitl.renderers.lark.send_lark_card", _fake_send
    )

    notifier = _DefaultCloudLarkNotifier(store=hitl_store)
    bridge = CloudHITLBridge(hitl_store, notifier)
    bridge.submit_request(
        task_id="b2-test",
        cursor_agent_id="agent-b2",
        cursor_run_id="run-b2",
        prompt_title="Approve deploy?",
        prompt_body="Approve deploy to prod-us-east-1?",
        options=[{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
        idempotency_key="b2-key-aaaa",
    )

    assert "kwargs" in captured, (
        "send_lark_card was never invoked — the notifier short-circuited "
        "before reaching the renderer."
    )
    payload = captured["kwargs"].get("card_payload")
    assert payload is not None, (
        "B2 regression: card_payload kwarg is None — the production "
        "notifier fell through to the legacy generic v0.5 builder."
    )
    metadata = payload["card_metadata"]
    assert set(metadata.keys()) == set(CARD_METADATA_KEYS), (
        f"v1 card_metadata key set mismatch — got {sorted(metadata.keys())}; "
        f"expected {sorted(CARD_METADATA_KEYS)}"
    )
    assert metadata["template_version"] == CARD_TEMPLATE_VERSION
    assert metadata["template_id"] == "cloud_hitl_request_card_v1"
    assert metadata["cursor_agent_id"] == "agent-b2"
    assert metadata["cursor_run_id"] == "run-b2"


def test_default_notifier_falls_back_when_store_missing_row(
    hitl_store: HITLStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2 defensive path — a missing row falls back to the legacy v0.5
    card body so the human still gets *some* notification (No Silent
    Failures: the fallback is logged explicitly).
    """
    captured: dict[str, Any] = {}

    def _fake_send(prompt: Any, *args: Any, **kwargs: Any) -> Any:
        captured["kwargs"] = dict(kwargs)
        return None

    monkeypatch.setattr(
        "popolaloom.hitl.renderers.lark.send_lark_card", _fake_send
    )

    notifier = _DefaultCloudLarkNotifier(store=hitl_store)
    from popolaloom.hitl import HITLOption, HITLPrompt

    prompt = HITLPrompt(
        trigger="approval",
        why="ctx",
        what="missing-row?",
        options=[
            HITLOption(id="yes", label="Yes"),
            HITLOption(id="no", label="No"),
        ],
        default_option_id="yes",
        channels=["lark", "mcp"],
        deadline_seconds=600,
    )
    notifier.send_hitl_card(prompt, hitl_id="missing-hitl-id")
    assert captured.get("kwargs", {}).get("card_payload") is None, (
        "fallback path must NOT pin a card_payload — let the legacy "
        "build_card_payload handle the missing-row branch."
    )
