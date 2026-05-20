"""CursorAdapter — wraps ``agent agent --print`` for non-interactive run.

Source command derivation (出处:
``.local/memory/research/02-cli-capabilities.md`` §"Cursor Agent CLI"):

    agent agent --print --output-format text "<prompt>"

(legacy installs may still expose this as ``cursor-agent``; the resolver
tries both, preferring ``agent``)

Optional ``extra`` keys:

- ``output_format`` (str): ``"text"`` (default) or ``"stream-json"``.
  ``stream-json`` 让 ``Supervisor`` 端的 NDJSON 解析器逐行消费; 注意官方
  约束 — partial-output 事件必须按 ``timestamp_ms`` + ``model_call_id`` 去重
  (出处: 02 §"Cursor Agent CLI 调用形态")。
- ``cwd_flag`` (bool): 若 True 且 ``cwd`` 非 None, 注入 ``--cwd <cwd>``;
  否则由 ``Supervisor`` 通过 ``Popen(cwd=...)`` 控制工作目录。
- ``session_id`` (str): 追加 ``--session-id <chatId>``; 与 PopolaLoom
  "派发前 ``cursor-agent create-chat`` 预生成 chatId" 的语义对齐
  (出处: 02 §"Cursor Agent CLI 后台与会话")。
- ``cli_args`` (list[str] | str): v0.6.0 (Phase 2 step 1, L6.B closure)
  generic cursor-agent passthrough — accepts a list of strings (preferred,
  explicit token list) or a single string (whitespace-split via
  :func:`shlex.split` so quoted compound tokens survive). Each token
  is appended after the ``--print --output-format <fmt>`` core flags but
  BEFORE the ``<prompt>`` positional, so cursor-agent recognises them as
  flags rather than prompt content. Unblocks cursor-agent flags that
  PopolaLoom does not (and should not) hard-code one-by-one — e.g.
  ``--trust``, ``--no-color``, future cursor-agent additions. The
  legacy ``cmd_args`` key (used by SKILL.md v0.5.3 Workflow 4 example)
  is accepted as an alias for back-compat. The adapter remains PURE
  (no shell expansion, no env interpolation).
"""

from __future__ import annotations

import logging
import shlex
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ALLOWED_OUTPUT_FORMATS: tuple[str, ...] = ("text", "stream-json")

_DEFAULT_CURSOR_BINARIES: tuple[str, ...] = ("agent", "cursor-agent")
"""Resolution order for the local Cursor CLI binary.

v1.6.1 (``.local/feedbacks/feedback_for_v1.6.0.md``): the modern Cursor
install (2026.05.07+) ships ``agent`` as the canonical CLI name with
``cursor-agent`` kept as a symlink for backward compat. We prefer
``agent`` to match the upstream CLI's own error messages (e.g.
"Please run 'agent login'") and the worker-side resolver in
``cloud_worker_cmd._DEFAULT_AGENT_BINARIES``. Legacy installs that only
ship ``cursor-agent`` still work via fallback.
"""


class CursorAdapter:
    """Cursor Agent CLI (``agent``) command builder."""

    name: str = "cursor"
    binary: str = "agent"

    def __init__(self, binary: str | None = None) -> None:
        """Construct an adapter, optionally pinning the CLI binary name.

        Args:
            binary: Explicit cursor CLI binary spelling
                (``"agent"`` / ``"cursor-agent"`` / a custom path).  When
                ``None`` (default), the constructor probes PATH for each
                entry in :data:`_DEFAULT_CURSOR_BINARIES` in order and
                assigns the first hit to ``self.binary``.  When neither
                spelling is on PATH, the class default ``"agent"`` is
                kept so :func:`subprocess.Popen` later surfaces a
                recognisable ``FileNotFoundError`` against the canonical
                name (No Silent Failures).

        v1.6.1 (``feedback_for_v1.6.0.md`` Q-3 + Bugbot review of
        PR #39): the previous ``@classmethod _resolve_binary`` ignored
        per-instance ``binary`` overrides because it accessed
        ``cls.binary`` rather than ``self.binary``; the adapter
        combinatorial matrix's ``argv[0] == adapter.binary`` assertion
        broke on legacy hosts that only shipped ``cursor-agent``.  The
        fix pins ``self.binary`` to the resolved spelling at
        construction time (or to the explicit ``binary=`` override) so
        every call to :meth:`build_command` and :meth:`is_available`
        agrees with the documented contract that ``argv[0]`` mirrors
        ``adapter.binary``.

        Reading PATH at construction time is treated as pure here:
        no writes, no mutation of state outside ``self``, and no
        ``os.getcwd()`` call (that's the original purity invariant from
        the module docstring).  Callers that need a fully air-gapped
        adapter (zero PATH probes) pass an explicit ``binary=`` value.
        """
        if binary is not None:
            self.binary = binary
            return
        for candidate in _DEFAULT_CURSOR_BINARIES:
            if shutil.which(candidate) is not None:
                self.binary = candidate
                return
        # Neither spelling is on PATH; keep the class default so a later
        # subprocess.Popen surfaces FileNotFoundError against the
        # canonical name.

    @classmethod
    def _resolve_binary(cls) -> str:
        """Class-level resolver kept for backward compat with v1.6.1-pre callers.

        Most call sites should prefer :attr:`self.binary` (set at
        construction time by :meth:`__init__`).  This classmethod stays
        for the rare caller that wants the "first PATH hit OR class
        default" lookup without instantiating a full adapter.

        Tries each entry in :data:`_DEFAULT_CURSOR_BINARIES` in order
        and returns the first one that resolves on PATH.  Falls back to
        :attr:`cls.binary` (``"agent"``) when neither is on PATH.
        """
        for candidate in _DEFAULT_CURSOR_BINARIES:
            if shutil.which(candidate) is not None:
                return candidate
        return cls.binary

    def build_command(
        self,
        prompt: str,
        cwd: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        """Construct ``cursor-agent agent --print`` argv list (PURE).

        Args:
            prompt: 主提示词, 作为最后一个位置参数传给 ``cursor-agent``。
            cwd: 工作目录; 仅在 ``extra["cwd_flag"]`` 为真时下沉到 ``--cwd``。
            extra: 见模块 docstring 四个键 (``output_format`` / ``cwd_flag``
                / ``session_id`` / ``cli_args``).

        Returns:
            list[str]: 形如 ``[cursor-agent, agent, --print, --output-format,
            <fmt>, [--cwd, <cwd>,] [<cli_args...>,] <prompt>,
            [--session-id, <id>]]``。

        Raises:
            ValueError: 当 ``extra["output_format"]`` 不在白名单内, 或
                当 ``extra["cli_args"]``(或别名 ``cmd_args``)的类型不是
                ``list[str]`` / ``str`` (No Silent Failures)。
        """
        extra = extra or {}

        output_format = str(extra.get("output_format", "text"))
        if output_format not in _ALLOWED_OUTPUT_FORMATS:
            raise ValueError(
                f"cursor: output_format must be one of {_ALLOWED_OUTPUT_FORMATS!r}, "
                f"got {output_format!r}"
            )

        cmd: list[str] = [
            self.binary,
            "agent",
            "--print",
            "--output-format",
            output_format,
        ]

        if extra.get("cwd_flag"):
            if cwd is None:
                logger.warning(
                    "cursor: cwd_flag=True but cwd=None; skipping --cwd "
                    "(adapter is PURE — won't read os.getcwd())"
                )
            else:
                cmd.extend(["--cwd", str(cwd)])

        cli_args = extra.get("cli_args", extra.get("cmd_args"))
        if cli_args is not None:
            cmd.extend(_normalize_cli_args(cli_args))

        cmd.append(prompt)

        if "session_id" in extra:
            cmd.extend(["--session-id", str(extra["session_id"])])

        return cmd

    def is_available(self) -> bool:
        """Return True iff this adapter's pinned binary resolves on ``$PATH``.

        v1.6.1 (Bugbot review of PR #39): the previous implementation
        checked every entry in :data:`_DEFAULT_CURSOR_BINARIES` so an
        explicit ``CursorAdapter(binary="cursor-agent")`` override would
        report ``is_available() == True`` even when only the OTHER
        spelling (``"agent"``) was on PATH — and then
        :meth:`build_command` would emit ``["cursor-agent", ...]`` for
        :class:`subprocess.Popen` to raise ``FileNotFoundError``
        against. The fix probes the actual binary this adapter will
        invoke, so the availability check matches the binary the next
        ``build_command`` call will produce (No Silent Failures).
        """
        return shutil.which(self.binary) is not None


def _normalize_cli_args(value: Any) -> list[str]:
    """Coerce a ``cli_args`` extras value into a ``list[str]`` of argv tokens.

    v0.6.0 (Phase 2 step 1, L6.B closure) — generic cursor-agent
    passthrough. Accepts the two ergonomic shapes a ``--cli-flag`` user
    is likely to type:

    1. ``list[str]`` — the explicit, recommended form. Used by JSON
       payloads (``--cli-flag 'cli_args=["--trust", "--no-color"]'``)
       and by Python callers that want zero ambiguity. Validated to
       contain only strings (No Silent Failures: a stray int / dict /
       None silently flowing into argv would surface as a confusing
       cursor-agent crash later, so we raise here at the boundary).
    2. ``str`` — convenience form for the common single-flag /
       short-token case. Split via :func:`shlex.split` so quoted
       compound tokens survive intact (``'--name "alice bob"'`` →
       ``["--name", "alice bob"]``).

    Any other type raises :class:`ValueError` with a key-pinned message
    so the failure travels under the same name the user typed.

    Args:
        value: the raw ``extra["cli_args"]`` (or ``extra["cmd_args"]``
            alias) value as it arrived from
            :func:`popolaloom.cli.main._parse_cli_flags` (already JSON-
            decoded when possible) or from a direct adapter caller.

    Returns:
        list[str]: argv tokens, guaranteed every element is a non-None
        string. May be empty (if the user passed ``cli_args=[]`` or
        ``cli_args=""``) — caller is responsible for the no-op semantics.

    Raises:
        ValueError: when the value is neither a list nor a string, or
            when a list element is not a string.
    """
    if isinstance(value, list):
        if not all(isinstance(token, str) for token in value):
            raise ValueError(
                "cursor: cli_args list must contain only strings, "
                f"got {value!r}"
            )
        return list(value)
    if isinstance(value, str):
        return shlex.split(value)
    raise ValueError(
        "cursor: cli_args must be list[str] or str, "
        f"got {type(value).__name__}={value!r}"
    )
