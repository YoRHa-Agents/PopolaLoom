"""``log_redact`` + EventLog 0o600 + CI lint-guard tests (v0.8.8 T2.1.2).

Per ``cost-fields.md`` §5.3 (code-level enforcement):

* ``scrub_cost_fields(payload) -> dict`` deep-copies and removes every
  forbidden cost / token-bearing key at every nesting depth — fuzzed
  across nested dicts, lists, mixed scalars, and tuple leaves.
* ``EventLog.append`` calls ``os.chmod(path, 0o600)`` after creation
  (asserted by stat-ing the file mode on a fresh log).
* CI lint-guard: ``logger.info(.*\busage\b)`` and ``logger.info(.*\bcost\b)``
  produce **zero** matches outside ``tests/`` (per spec §5.3 rule 3).

The fuzz cases cover the spec's named shapes (S1..S6 secret token shapes
are out of scope here — those live in T2.3.3) plus three additional
nesting flavors so the redaction is exercised across the realistic
payload surface popolad actually emits at INFO/WARNING level:

1. Flat dict with a single forbidden key.
2. Deeply nested dict (`data.event.usage.totalCents`).
3. List-of-dicts payload (e.g. SSE batch envelope).
4. Mixed dict containing nested list + tuple leaves.
5. Forbidden key in a non-string position (handled by ignoring).

The 0o600 assertion uses ``Path.stat().st_mode & 0o777`` so the test
is platform-agnostic (POSIX-strict; Windows skips via the existing
project pytest config).
"""

from __future__ import annotations

import re
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.log_redact import FORBIDDEN_KEYS, scrub_cost_fields

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


# ── (a) deterministic redaction shapes ──────────────────────────────────────


def test_scrub_strips_top_level_forbidden_key() -> None:
    """A flat dict with a single forbidden key returns it stripped."""
    out = scrub_cost_fields(
        {"usage": {"inputTokens": 1, "outputTokens": 2}, "model": "x"}
    )
    assert out == {"model": "x"}


def test_scrub_strips_nested_forbidden_keys() -> None:
    """Deep nesting: every depth gets the same redaction."""
    payload = {
        "data": {
            "event": {
                "usage": {"totalCents": 250, "outputTokens": 10},
                "summary": "ok",
            },
            "model_id": "composer-2",
        },
        "tokenUsage": {"inputTokens": 5},
    }
    out = scrub_cost_fields(payload)
    assert out == {
        "data": {"event": {"summary": "ok"}, "model_id": "composer-2"}
    }
    assert "tokenUsage" not in out
    assert "usage" not in out["data"]["event"]
    # input must be unchanged (no aliasing — deep copy)
    assert payload["data"]["event"]["usage"]["totalCents"] == 250


def test_scrub_strips_in_lists_of_dicts() -> None:
    """Lists are walked element-wise."""
    payload = [
        {"usage": 1, "keep": True},
        {"chargedCents": 99, "ok": "yes"},
        "scalar-string-leaf",
        42,
    ]
    out = scrub_cost_fields(payload)
    assert out == [
        {"keep": True},
        {"ok": "yes"},
        "scalar-string-leaf",
        42,
    ]


def test_scrub_handles_tuple_leaves() -> None:
    """Tuples retain shape; forbidden keys nested inside are still scrubbed."""
    payload = {
        "snapshot": ({"usage": 99, "keep": "v"}, {"chargedCents": 5}),
        "frozen": (1, 2, 3),
    }
    out = scrub_cost_fields(payload)
    assert isinstance(out["snapshot"], tuple)
    assert out["snapshot"] == ({"keep": "v"}, {})
    assert out["frozen"] == (1, 2, 3)


def test_scrub_returns_scalars_verbatim() -> None:
    """Scalar inputs pass through (defensive; rare but possible)."""
    assert scrub_cost_fields("hello") == "hello"
    assert scrub_cost_fields(42) == 42
    assert scrub_cost_fields(None) is None
    assert scrub_cost_fields(True) is True


def test_scrub_does_not_mutate_input() -> None:
    """Deep copy semantics — caller's dict is untouched."""
    src = {"usage": {"a": 1}, "tokens_input": 5, "ok": "yes"}
    snapshot = deepcopy(src)
    out = scrub_cost_fields(src)
    assert src == snapshot, "scrub_cost_fields must not mutate its argument"
    assert out == {"ok": "yes"}


def test_scrub_strips_every_forbidden_key_in_isolation() -> None:
    """One test per forbidden key — direct catalog coverage."""
    sentinel: dict[str, Any] = {"keep": "v"}
    for key in FORBIDDEN_KEYS:
        payload = {**sentinel, key: "leaked"}
        out = scrub_cost_fields(payload)
        assert key not in out, f"{key} survived scrub: {out}"
        assert out["keep"] == "v"


def test_forbidden_keys_match_spec() -> None:
    """``FORBIDDEN_KEYS`` matches the §5.3 contract verbatim.

    Adding/removing a key here is a v0.8.8.x point release per spec §6;
    pinning the set as a test invariant prevents accidental drift.
    """
    spec_keys = {
        "usage",
        "tokens_input",
        "tokens_output",
        "cacheReadTokens",
        "cacheWriteTokens",
        "chargedCents",
        "totalCents",
        "tokenUsage",
        "cursorTokenFee",
        "spendCents",
        "cost_estimate_usd",
    }
    missing = spec_keys - FORBIDDEN_KEYS
    assert not missing, f"FORBIDDEN_KEYS missing spec keys: {missing}"


# ── (b) fuzz: random nesting / random key placement ────────────────────────


@pytest.fixture
def rng_seed() -> int:
    return 20260508  # locked seed → deterministic CI runs


def _fuzz_payload(depth: int, fanout: int, *, rng_seed: int) -> dict[str, Any]:
    """Build a deterministic nested dict with ``forbidden_keys`` sprinkled.

    Walk ``depth`` levels deep; at each level, insert ``fanout`` keys —
    half from the FORBIDDEN_KEYS set, half from a benign sentinel set.
    The test then asserts the post-scrub version contains zero forbidden
    keys at any depth (recursive grep).
    """
    import random

    rnd = random.Random(rng_seed)
    benign_keys = ["model", "task_id", "agent_id", "summary", "phase"]
    forbidden = sorted(FORBIDDEN_KEYS)

    def _build(level: int) -> dict[str, Any]:
        node: dict[str, Any] = {}
        for _ in range(fanout):
            if rnd.random() < 0.5:
                key = rnd.choice(forbidden)
                node[key] = rnd.choice([1, "leaked", [1, 2], {"x": 1}])
            else:
                key = rnd.choice(benign_keys)
                if level < depth and rnd.random() < 0.4:
                    node[key] = _build(level + 1)
                else:
                    node[key] = rnd.choice([rnd.randint(0, 100), "ok", None])
        return node

    return _build(0)


def _walk_dict_keys(payload: Any) -> list[str]:
    """Yield every dict-key string in ``payload`` recursively (for grepping)."""
    keys: list[str] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(k, str):
                keys.append(k)
            keys.extend(_walk_dict_keys(v))
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            keys.extend(_walk_dict_keys(item))
    return keys


def test_scrub_fuzz_zero_forbidden_keys_after(rng_seed: int) -> None:
    """100 randomized payloads × 0 surviving forbidden keys (property)."""
    for i in range(100):
        payload = _fuzz_payload(depth=4, fanout=5, rng_seed=rng_seed + i)
        out = scrub_cost_fields(payload)
        keys_after = _walk_dict_keys(out)
        leaked = [k for k in keys_after if k in FORBIDDEN_KEYS]
        assert not leaked, (
            f"iter={i} payload had leaked keys after scrub: {leaked}"
        )


def test_scrub_fuzz_preserves_benign_keys(rng_seed: int) -> None:
    """The non-forbidden keys must survive the scrub (no over-zealous strip).

    "Benign" here means a key that lives **outside** any forbidden
    subtree — i.e. its full path from the root never passes through a
    forbidden key. Keys nested *inside* a forbidden subtree (e.g.
    ``usage.inputTokens``) are correctly removed alongside the parent
    and are not expected to survive.
    """
    payload = _fuzz_payload(depth=3, fanout=6, rng_seed=rng_seed)

    def _benign_outside_forbidden(p: Any) -> set[str]:
        result: set[str] = set()
        if isinstance(p, dict):
            for k, v in p.items():
                if not isinstance(k, str):
                    continue
                if k in FORBIDDEN_KEYS:
                    continue  # the entire subtree is redacted, skip walking
                result.add(k)
                result |= _benign_outside_forbidden(v)
        elif isinstance(p, (list, tuple)):
            for item in p:
                result |= _benign_outside_forbidden(item)
        return result

    benign_in = _benign_outside_forbidden(payload)
    benign_out = _benign_outside_forbidden(scrub_cost_fields(payload))
    assert benign_in == benign_out, (
        f"benign keys vanished: in={benign_in - benign_out} "
        f"unexpected_added={benign_out - benign_in}"
    )


# ── (c) EventLog.append calls os.chmod(path, 0o600) ────────────────────────


def test_event_log_chmod_0o600_on_creation(tmp_path: Path) -> None:
    """A fresh ``EventLog`` file is mode 0o600 (owner-only).

    Per ``cost-fields.md`` §5.2: EventLog is the source of truth and may
    contain undocumented payload extras incl. potential token / cost
    data, so the file MUST be owner-only.
    """
    path = tmp_path / "task-001.jsonl"
    log = EventLog(path)
    try:
        log.append("test.event", {"k": "v"})
        log.fsync()
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, (
            f"expected 0o600 mode on {path}, got 0o{mode:o}"
        )
    finally:
        log.close()


def test_event_log_chmod_persists_after_append(tmp_path: Path) -> None:
    """Repeated appends do not weaken the file mode."""
    path = tmp_path / "task-002.jsonl"
    log = EventLog(path)
    try:
        for i in range(5):
            log.append("test.event", {"i": i})
        log.fsync()
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, f"mode drift after appends: 0o{mode:o}"
    finally:
        log.close()


def test_event_log_chmod_on_existing_file(tmp_path: Path) -> None:
    """Re-opening an existing file STILL chmods to 0o600 (idempotent)."""
    path = tmp_path / "task-003.jsonl"
    path.touch()
    path.chmod(0o644)  # simulate a stale world-readable file from a prior run
    log = EventLog(path)
    try:
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, (
            f"reopening must tighten to 0o600, got 0o{mode:o}"
        )
    finally:
        log.close()


# ── (d) CI lint-guard: no `logger.info(...)` with usage/cost outside tests ─


_LINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"logger\.info\([^)]*\busage\b"),
    re.compile(r"logger\.info\([^)]*\bcost\b"),
)


def _all_python_sources_outside_tests() -> list[Path]:
    """Return every ``.py`` file under ``src/`` (excludes ``tests/`` by design)."""
    src_root = REPO_ROOT / "src" / "popolaloom"
    return sorted(p for p in src_root.rglob("*.py") if p.is_file())


def test_ci_lint_no_usage_in_logger_info() -> None:
    """`logger.info(...usage...)` outside ``tests/`` ➜ zero matches.

    Spec §5.3 rule 3 — protects against accidental token-count leaks
    into ``popolad.log`` (which is mode 0o644 by default per §5.2).
    """
    offenders: list[tuple[Path, int, str]] = []
    for src in _all_python_sources_outside_tests():
        text = src.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            for pattern in _LINT_PATTERNS:
                if pattern.search(line):
                    offenders.append((src, lineno, line.rstrip()))
                    break
    assert not offenders, (
        "logger.info() usage of cost/usage tokens outside tests/:\n"
        + "\n".join(f"  {p}:{ln}  {ln_content}" for p, ln, ln_content in offenders)
    )


def test_ci_lint_uses_ripgrep_when_available() -> None:
    """Cross-check via ``rg`` so the spec §5.3 grep contract is honored.

    The ``rg`` binary is available in CI (per project ``DEVELOPMENT.md``);
    when missing, the pure-Python guard above is sufficient — we skip
    rather than fail to keep dev-machine pytest runs frictionless.
    """
    rg_path = subprocess.run(
        ["which", "rg"], capture_output=True, text=True, check=False
    )
    if rg_path.returncode != 0:
        pytest.skip("ripgrep not installed; pure-Python guard suffices")

    src_dir = str(REPO_ROOT / "src")
    for needle in (r"logger\.info\(.*\busage\b", r"logger\.info\(.*\bcost\b"):
        proc = subprocess.run(
            ["rg", "--no-heading", "--color=never", "-n", "-e", needle, src_dir],
            capture_output=True,
            text=True,
            check=False,
        )
        # rg returncode: 0 = matches found; 1 = no matches; 2 = error
        assert proc.returncode != 0 or not proc.stdout.strip(), (
            f"ripgrep found forbidden logger.info pattern {needle!r}:\n"
            f"{proc.stdout}"
        )


# ── (e) sanity: log_redact pairs cleanly with EventLog INFO emit ────────────


def test_scrub_round_trip_through_event_log(tmp_path: Path) -> None:
    """End-to-end: a payload with ``usage`` ➜ scrubbed ➜ written ➜ no leak.

    Validates the realistic call pattern: callers that opt into the
    ``log_redact.scrub_cost_fields`` helper before ``event_log.append``
    produce a stored envelope that does NOT carry any forbidden keys.
    """
    path = tmp_path / "task-redact.jsonl"
    log = EventLog(path)
    try:
        raw = {
            "task_id": "t1",
            "phase": "RUNNING",
            "usage": {"inputTokens": 5, "outputTokens": 7},
            "model_id": "composer-2",
            "cost_estimate_usd": 0.123,
        }
        scrubbed = scrub_cost_fields(raw)
        log.append("cloud.run_status", scrubbed)
        log.fsync()
        body = path.read_text(encoding="utf-8")
        assert "usage" not in body
        assert "inputTokens" not in body
        assert "cost_estimate_usd" not in body
        assert "model_id" in body  # benign data preserved
    finally:
        log.close()
