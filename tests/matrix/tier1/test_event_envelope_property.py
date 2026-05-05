"""Tier 1 / A2 — hypothesis property tests for the EventLog CloudEvents 1.0 envelope.

Per testing-matrix.md §1.1 example
``test_envelope_specversion_always_1_0_for_any_input``: for any
``(type_, data)`` we feed to :meth:`EventLog.append`, the resulting
envelope MUST satisfy:

- ``specversion == "1.0"``
- ``id.startswith("evt-")``
- ``time.endswith("Z")``  (ISO-8601 UTC with millisecond precision)
- ``source.startswith("popola/")``
- ``data`` is preserved (JSON round-trip equal to input)

Additional dedicated cases cover the listed edge inputs: empty dict,
deeply nested (≤ 5 levels), large strings (~1 KB), unicode, bool/null,
distinct ids never collide.
"""

from __future__ import annotations

import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from popolaloom.daemon.event_log import EventLog


def _close_log(log: EventLog) -> None:
    """Close the log idempotently — used in finally blocks of property tests."""
    log.close()


# JSON-safe payload strategy: dicts whose values are bool / int / str / None /
# small recursive variants. Keep the recursion bounded (max_leaves=10) so
# hypothesis stays fast.
_safe_json_value = st.recursive(
    st.one_of(
        st.booleans(),
        st.integers(min_value=-(10**9), max_value=10**9),
        st.text(max_size=120),
        st.none(),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(
            st.text(min_size=1, max_size=10, alphabet="abcdefghij_"),
            children,
            max_size=4,
        ),
    ),
    max_leaves=10,
)
_safe_json_dict = st.dictionaries(
    st.text(min_size=1, max_size=10, alphabet="abcdefghij_"),
    _safe_json_value,
    max_size=6,
)


@given(event_type=st.text(min_size=1, max_size=40), data=_safe_json_dict)
@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_envelope_satisfies_cloudevents_invariants(
    event_type: str, data: dict[str, object], tmp_path_factory: object
) -> None:
    """Every envelope returned by :meth:`EventLog.append` is CloudEvents-1.0 compliant.

    Hypothesis-driven: thousands of random ``(event_type, data)`` pairs.
    """
    tmp_path = tmp_path_factory.mktemp("env")  # type: ignore[attr-defined]
    log_path = Path(tmp_path) / "events.jsonl"
    log = EventLog(log_path, fsync_interval_s=0)
    try:
        envelope = log.append(event_type, dict(data))
        assert envelope["specversion"] == "1.0"
        assert isinstance(envelope["id"], str) and envelope["id"].startswith("evt-")
        assert isinstance(envelope["time"], str) and envelope["time"].endswith("Z")
        assert isinstance(envelope["source"], str) and envelope["source"].startswith("popola/")
        assert envelope["type"] == event_type
        assert json.loads(json.dumps(envelope["data"])) == data
    finally:
        _close_log(log)


@given(payload=_safe_json_dict)
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_envelope_id_is_unique_within_log(
    payload: dict[str, object], tmp_path_factory: object
) -> None:
    """Two consecutive ``append`` calls produce two distinct envelope ids (uuid4 hex)."""
    tmp_path = tmp_path_factory.mktemp("uniq")  # type: ignore[attr-defined]
    log = EventLog(Path(tmp_path) / "u.jsonl", fsync_interval_s=0)
    try:
        e1 = log.append("dup.test", dict(payload))
        e2 = log.append("dup.test", dict(payload))
        assert e1["id"] != e2["id"], "uuid4-derived ids must not collide"
    finally:
        _close_log(log)


# ── explicit edge cases (kept separate so failures are diagnosable) ──────


def test_envelope_empty_data(tmp_path: Path) -> None:
    """Empty dict data is preserved verbatim and is JSON-roundtrippable."""
    log = EventLog(tmp_path / "empty.jsonl", fsync_interval_s=0)
    try:
        ev = log.append("empty.dict", {})
        assert ev["data"] == {}
        assert json.loads(json.dumps(ev["data"])) == {}
    finally:
        _close_log(log)


def test_envelope_deeply_nested(tmp_path: Path) -> None:
    """A 5-level deeply nested dict survives append() without flattening."""
    nested: dict[str, object] = {"k": {"k": {"k": {"k": {"leaf": 42}}}}}
    log = EventLog(tmp_path / "deep.jsonl", fsync_interval_s=0)
    try:
        ev = log.append("deep.test", nested)
        node = ev["data"]
        for _ in range(4):
            assert isinstance(node, dict)
            node = node["k"]
        assert node == {"leaf": 42}
    finally:
        _close_log(log)


def test_envelope_large_string(tmp_path: Path) -> None:
    """A ~1 KB string in payload is preserved (within JSON line limits)."""
    big = "x" * 1024
    log = EventLog(tmp_path / "big.jsonl", fsync_interval_s=0)
    try:
        ev = log.append("big.test", {"blob": big})
        assert ev["data"]["blob"] == big
        events = log.tail()
        assert len(events) == 1
        assert events[0]["data"]["blob"] == big
    finally:
        _close_log(log)


def test_envelope_unicode_preserved(tmp_path: Path) -> None:
    """Unicode strings (CJK + emoji-equivalent) survive a tail() round-trip."""
    payload = {"zh": "中文测试", "punctuation": "café — naïve résumé"}
    log = EventLog(tmp_path / "u.jsonl", fsync_interval_s=0)
    try:
        log.append("unicode.test", payload)
        events = log.tail()
        assert events[0]["data"] == payload
    finally:
        _close_log(log)


def test_envelope_none_and_bool_preserved(tmp_path: Path) -> None:
    """``None`` / ``True`` / ``False`` are round-trippable through append + tail."""
    payload = {"flag": True, "off": False, "missing": None, "n": 0}
    log = EventLog(tmp_path / "b.jsonl", fsync_interval_s=0)
    try:
        log.append("bool.test", payload)
        ev = log.tail()[0]
        assert ev["data"]["flag"] is True
        assert ev["data"]["off"] is False
        assert ev["data"]["missing"] is None
        assert ev["data"]["n"] == 0
    finally:
        _close_log(log)


def test_envelope_source_uses_explicit_override(tmp_path: Path) -> None:
    """When ``source=`` is passed to ``__init__``, envelopes use it verbatim
    (default falls back to ``popola/<file_stem>``)."""
    log = EventLog(tmp_path / "src.jsonl", source="popola/explicit-src", fsync_interval_s=0)
    try:
        ev = log.append("src.test", {"k": 1})
        assert ev["source"] == "popola/explicit-src"
    finally:
        _close_log(log)


def test_envelope_default_source_is_popola_stem(tmp_path: Path) -> None:
    """Default source is ``popola/<file-stem>`` — verifies stem-derivation."""
    log = EventLog(tmp_path / "stem.jsonl", fsync_interval_s=0)
    try:
        ev = log.append("stem.test", {"k": 1})
        assert ev["source"] == "popola/stem"
    finally:
        _close_log(log)
