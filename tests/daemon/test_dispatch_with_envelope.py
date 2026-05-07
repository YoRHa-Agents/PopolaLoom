"""Tests for ``Popolad.dispatch_with_envelope`` — v0.7.2 E3 internal-unification entry.

v0.7.2 (Stage v0.8.0 patch 2, per design D-080 Q5=E3): the file-backed
:class:`HandoffEnvelope` is the single source of truth for dispatch payload.
:meth:`Popolad.dispatch_task` is now a thin wrapper that builds an envelope
from kwargs and delegates to :meth:`Popolad.dispatch_with_envelope`.

This file covers the new method's invariants (type validation, envelope file
write, env-overlay merging, handoff_root resolution); ``test_dispatch_chain_integration.py``
+ existing daemon tests cover the kwargs-style ``dispatch_task`` surface and
prove backward compat (E3 internal-unification preserves public signatures).
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from popolaloom.daemon import Popolad
from popolaloom.handoff import HandoffEnvelope, generate_handoff_id


def _no_op_adapter(
    cli: str,
    prompt: str,
    cwd: Path | None,
    extra: dict[str, Any] | None = None,
) -> list[str]:
    """Adapter that spawns a fast Python subprocess (exits 0 immediately).

    Returns a 4-arg signature so :meth:`Popolad._call_adapter` takes the
    canonical strict path (extras would be passed if present).
    """
    return [sys.executable, "-c", "import sys; sys.exit(0)"]


def _build_envelope(target_cli: str = "testcli", prompt: str = "do work") -> HandoffEnvelope:
    return HandoffEnvelope(
        handoff_id=generate_handoff_id(target_cli, prompt),
        created_at=datetime.now(UTC),
        target_cli=target_cli,
        prompt=prompt,
    )


# ── 1: type validation ────────────────────────────────────────────────────


def test_dispatch_with_envelope_rejects_non_envelope(tmp_path: Path) -> None:
    """Non-HandoffEnvelope arg → TypeError (No Silent Failures)."""
    popolad = Popolad(events_dir=tmp_path, adapter=_no_op_adapter, use_graph=False)
    with pytest.raises(TypeError, match="HandoffEnvelope"):
        popolad.dispatch_with_envelope("not an envelope")  # type: ignore[arg-type]


def test_dispatch_with_envelope_rejects_dict(tmp_path: Path) -> None:
    """A raw dict is not a HandoffEnvelope — must raise."""
    popolad = Popolad(events_dir=tmp_path, adapter=_no_op_adapter, use_graph=False)
    payload = {"target_cli": "cursor", "prompt": "x"}
    with pytest.raises(TypeError, match="HandoffEnvelope"):
        popolad.dispatch_with_envelope(payload)  # type: ignore[arg-type]


# ── 2: envelope file is written ──────────────────────────────────────────


def test_dispatch_with_envelope_writes_envelope_file(
    tmp_path: Path, _handoff_dir_session: Path
) -> None:
    """Active envelope file lands at <handoff_root>/<handoff_id>.md."""
    popolad = Popolad(events_dir=tmp_path, adapter=_no_op_adapter, use_graph=False)
    env = _build_envelope("cursor", "fix the bug")

    popolad.dispatch_with_envelope(env)

    expected_path = _handoff_dir_session / f"{env.handoff_id}.md"
    assert expected_path.is_file(), f"envelope file missing at {expected_path}"
    content = expected_path.read_text()
    assert content.startswith("---\n"), "envelope is not Markdown front-matter"
    assert env.prompt in content, "prompt missing from envelope body"
    assert env.handoff_id in content, "handoff_id missing from envelope front-matter"


def test_dispatch_with_envelope_writes_envelope_at_explicit_root(tmp_path: Path) -> None:
    """``handoff_root`` arg overrides $POPOLA_HANDOFF_DIR + default."""
    custom_root = tmp_path / "custom_handoff"
    popolad = Popolad(events_dir=tmp_path, adapter=_no_op_adapter, use_graph=False)
    env = _build_envelope("cursor", "explicit root")

    popolad.dispatch_with_envelope(env, handoff_root=custom_root)

    assert (custom_root / f"{env.handoff_id}.md").is_file()


def test_dispatch_with_envelope_uses_pola_handoff_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``handoff_root=None`` falls back to ``$POPOLA_HANDOFF_DIR``."""
    env_dir = tmp_path / "env_root"
    monkeypatch.setenv("POPOLA_HANDOFF_DIR", str(env_dir))

    popolad = Popolad(events_dir=tmp_path, adapter=_no_op_adapter, use_graph=False)
    env = _build_envelope("cursor", "env-resolved root")

    popolad.dispatch_with_envelope(env)

    assert (env_dir / f"{env.handoff_id}.md").is_file()


# ── 3: env overlay injection ─────────────────────────────────────────────


def test_dispatch_with_envelope_injects_handoff_env_vars(tmp_path: Path) -> None:
    """Spawned process sees ``POPOLA_HANDOFF_FILE`` + ``POPOLA_HANDOFF_ID``."""
    seen_envs: list[dict[str, str] | None] = []

    class CapturingSupervisor:
        def __init__(self) -> None:
            self.state_store = None

        def spawn(
            self,
            *,
            task_id: str,
            cmd: list[str],
            cwd: Path | None,
            env: dict[str, str] | None,
            event_log: Any,
            on_exit: Any = None,
        ) -> int:
            seen_envs.append(dict(env) if env is not None else None)
            return 12345

    popolad = Popolad(
        events_dir=tmp_path, adapter=_no_op_adapter, use_graph=False
    )
    popolad._supervisor = CapturingSupervisor()  # type: ignore[assignment]

    env = _build_envelope("cursor", "see env vars")
    popolad.dispatch_with_envelope(env)

    assert len(seen_envs) == 1
    captured = seen_envs[0]
    assert captured is not None
    assert "POPOLA_HANDOFF_FILE" in captured
    assert "POPOLA_HANDOFF_ID" in captured
    assert captured["POPOLA_HANDOFF_ID"] == env.handoff_id
    handoff_path = Path(captured["POPOLA_HANDOFF_FILE"])
    assert handoff_path.is_absolute()
    assert handoff_path.is_file()


def test_dispatch_with_envelope_overlay_overrides_caller_env(tmp_path: Path) -> None:
    """Overlay (POPOLA_HANDOFF_*) always wins over caller-provided base_env keys.

    Rationale: prevent handoff impersonation. A caller passing
    ``base_env={"POPOLA_HANDOFF_FILE": "/tmp/fake"}`` must NOT see that fake
    path leak into the spawn — popolad rewrites it to the real envelope path.
    """
    seen_envs: list[dict[str, str] | None] = []

    class CapturingSupervisor:
        def __init__(self) -> None:
            self.state_store = None

        def spawn(
            self,
            *,
            task_id: str,
            cmd: list[str],
            cwd: Path | None,
            env: dict[str, str] | None,
            event_log: Any,
            on_exit: Any = None,
        ) -> int:
            seen_envs.append(dict(env) if env is not None else None)
            return 12345

    popolad = Popolad(
        events_dir=tmp_path, adapter=_no_op_adapter, use_graph=False
    )
    popolad._supervisor = CapturingSupervisor()  # type: ignore[assignment]

    env = _build_envelope("cursor", "no impersonation")
    fake_base = {
        "POPOLA_HANDOFF_FILE": "/tmp/attacker-controlled.md",
        "POPOLA_HANDOFF_ID": "attacker-fake-12345678",
        "USER_PROVIDED": "kept",
    }

    popolad.dispatch_with_envelope(env, base_env=fake_base)

    captured = seen_envs[0]
    assert captured is not None
    assert captured["POPOLA_HANDOFF_FILE"] != "/tmp/attacker-controlled.md"
    assert captured["POPOLA_HANDOFF_ID"] == env.handoff_id
    assert captured["USER_PROVIDED"] == "kept"


def test_dispatch_with_envelope_uses_os_environ_when_base_env_none(tmp_path: Path) -> None:
    """When ``base_env=None``, the merged env starts from ``os.environ``."""
    seen_envs: list[dict[str, str] | None] = []

    class CapturingSupervisor:
        def __init__(self) -> None:
            self.state_store = None

        def spawn(
            self,
            *,
            task_id: str,
            cmd: list[str],
            cwd: Path | None,
            env: dict[str, str] | None,
            event_log: Any,
            on_exit: Any = None,
        ) -> int:
            seen_envs.append(dict(env) if env is not None else None)
            return 12345

    popolad = Popolad(
        events_dir=tmp_path, adapter=_no_op_adapter, use_graph=False
    )
    popolad._supervisor = CapturingSupervisor()  # type: ignore[assignment]

    env = _build_envelope("cursor", "default env base")
    popolad.dispatch_with_envelope(env)

    captured = seen_envs[0]
    assert captured is not None
    # PATH must survive (sanity check that os.environ leaked through)
    assert "PATH" in captured, "os.environ['PATH'] not propagated"
    if "HOME" in os.environ:
        assert captured.get("HOME") == os.environ["HOME"]


# ── 4: returns task_id distinct from handoff_id ──────────────────────────


def test_dispatch_with_envelope_returns_popola_task_id(tmp_path: Path) -> None:
    """Returned id is the popola-internal task_id, not the envelope handoff_id."""
    popolad = Popolad(events_dir=tmp_path, adapter=_no_op_adapter, use_graph=False)
    env = _build_envelope("cursor", "task vs handoff id")

    task_id = popolad.dispatch_with_envelope(env)

    assert task_id != env.handoff_id
    assert task_id.startswith("cursor-")


# ── 5: dispatch_task delegates through dispatch_with_envelope (E3) ───────


def test_dispatch_task_writes_envelope_file_via_e3(
    tmp_path: Path, _handoff_dir_session: Path
) -> None:
    """``dispatch_task(prompt=...)`` builds an envelope + writes the file (E3)."""
    popolad = Popolad(events_dir=tmp_path, adapter=_no_op_adapter, use_graph=False)

    popolad.dispatch_task(cli="cursor", prompt="check E3 wiring")

    files = list(_handoff_dir_session.glob("cursor-*.md"))
    assert len(files) >= 1, "no envelope file written by dispatch_task E3 path"


def test_dispatch_task_envelope_filename_matches_slug(
    tmp_path: Path, _handoff_dir_session: Path
) -> None:
    """The envelope filename is ``<cli>-<slug>-<8hex>.md``."""
    popolad = Popolad(events_dir=tmp_path, adapter=_no_op_adapter, use_graph=False)

    popolad.dispatch_task(cli="claude", prompt="refactor module X")

    files = list(_handoff_dir_session.glob("claude-refactor-*.md"))
    assert len(files) >= 1, "expected slug-prefixed file claude-refactor-*.md"


def test_dispatch_task_user_extra_reaches_adapter_unmodified(tmp_path: Path) -> None:
    """User-provided ``extra`` reaches adapter unchanged.

    Internal handoff bookkeeping (envelope path, id) flows via env vars,
    NOT via extra dict — so test fixtures asserting exact extra equality
    still pass post-E3.
    """
    seen_extras: list[dict[str, Any] | None] = []

    def capture_adapter(
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        seen_extras.append(dict(extra) if extra is not None else None)
        return [sys.executable, "-c", "import sys; sys.exit(0)"]

    popolad = Popolad(events_dir=tmp_path, adapter=capture_adapter, use_graph=False)
    popolad.dispatch_task(cli="cli2", prompt="p", extra={"foo": True, "n": 3})

    assert len(seen_extras) == 1
    assert seen_extras[0] == {"foo": True, "n": 3}, (
        "extra dict modified by E3 path; internal bookkeeping leaked into adapter input"
    )


def test_dispatch_task_extra_none_passes_none(tmp_path: Path) -> None:
    """When user passes no extra, adapter sees ``extra=None`` (legacy 3-arg path)."""
    seen_extras: list[dict[str, Any] | None] = []

    def capture_adapter(
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        seen_extras.append(dict(extra) if extra is not None else None)
        return [sys.executable, "-c", "import sys; sys.exit(0)"]

    popolad = Popolad(events_dir=tmp_path, adapter=capture_adapter, use_graph=False)
    popolad.dispatch_task(cli="cli3", prompt="p")

    assert len(seen_extras) == 1
    assert seen_extras[0] is None, "extra should be None when user passed nothing"


# ── 6: C5 flag-channel injection (v0.7.2 双通道) ─────────────────────────


def _spawnable_adapter(
    cli: str,
    prompt: str,
    cwd: Path | None,
    extra: dict[str, Any] | None = None,
) -> list[str]:
    """Adapter returning a real spawnable cmd (sys.executable + exit 0)."""
    return [sys.executable, "-c", "import sys; sys.exit(0)"]


def test_handoff_flag_injection_off_by_default(tmp_path: Path) -> None:
    """``--popola-handoff-file`` is NOT injected without explicit opt-in.

    Vanilla cursor-agent / claude / codex don't recognise the flag yet;
    auto-injecting would break their argv parsing. The env-var channel
    (``POPOLA_HANDOFF_FILE``) is the primary delivery; the flag is opt-in
    forward-compat for sub-CLIs that gain native support later.
    """
    popolad = Popolad(events_dir=tmp_path, adapter=_spawnable_adapter, use_graph=False)
    popolad.dispatch_task(
        cli="fakecli", prompt="no flag opt-in", extra={"foo": 1}
    )

    handle = popolad._state.list_all()[0]
    assert "--popola-handoff-file" not in handle.cmd, (
        f"flag injected without opt-in: {handle.cmd}"
    )


def test_handoff_flag_injection_when_opted_in(tmp_path: Path) -> None:
    """``extra["popola_handoff_flag"] = True`` opt-in causes flag append."""
    popolad = Popolad(events_dir=tmp_path, adapter=_spawnable_adapter, use_graph=False)
    popolad.dispatch_task(
        cli="fakecli",
        prompt="opted in",
        extra={"popola_handoff_flag": True},
    )

    handle = popolad._state.list_all()[0]
    cmd = handle.cmd
    assert "--popola-handoff-file" in cmd, f"flag missing in cmd: {cmd}"

    flag_idx = cmd.index("--popola-handoff-file")
    handoff_path_arg = cmd[flag_idx + 1]
    assert Path(handoff_path_arg).is_file(), (
        f"flag value {handoff_path_arg!r} should point to the active envelope"
    )


def test_handoff_flag_falsy_optin_does_not_inject(tmp_path: Path) -> None:
    """``popola_handoff_flag=False`` (or 0 / "") should NOT inject."""
    popolad = Popolad(events_dir=tmp_path, adapter=_spawnable_adapter, use_graph=False)
    for falsy in (False, 0, ""):
        popolad.dispatch_task(
            cli="fakecli",
            prompt=f"falsy={falsy!r}",
            extra={"popola_handoff_flag": falsy},
        )

    handles = popolad._state.list_all()
    for handle in handles:
        assert "--popola-handoff-file" not in handle.cmd, (
            f"falsy opt-in {handle.task_id} should not inject flag: {handle.cmd}"
        )
