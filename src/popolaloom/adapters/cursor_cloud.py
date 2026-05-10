"""CursorCloudAdapter — wraps Cursor's Cloud Agent REST API (Option α).

Separate adapter (``--cli=cursor-cloud``) keeps local ``cursor-agent`` argv
building unchanged while exposing the cloud API as a sibling in the registry
(出处: ``.local/research/v0.8.5_cloud_agent/research.md`` §6 Option α).

Authentication follows Cursor Cloud Agents API: **HTTP Basic** with the API
key as username and an **empty password** (``httpx`` ``auth=(key, "")``).

:func:`CursorCloudAdapter.build_command` returns a **sentinel argv** (prefix
``CLOUD_BUILD_COMMAND_MARKER`` + JSON payload) — not a subprocess argv. Stage 2
Supervisor detects this marker and delegates to :class:`CloudCursorClient`
instead of ``Popen``.

:class:`CloudCursorClient` maps HTTP errors to :exc:`CursorCloudError`
subclasses; **stream_run** / follow-up runs are deferred to v0.8.6+ (SSE
/parser not in this module).

Further design notes: ``.local/research/v0.8.5_cloud_agent/research.md``.
REST field names: ``https://cursor.com/docs/cloud-agent/api/endpoints.md``.

v0.10.0 — Cloud Dispatch Clarity
================================

This module was pivoted in v0.10.0 (release ``v1.0.0-pre.1``) to align with
the live Cursor REST gateway schema as discovered by 22 successful 2xx
probes against ``api.cursor.com``. See
``.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md`` for the
full decision log; the rows below are the ones that landed in this file:

- **Q-1** (API-key class detection): :meth:`CloudCursorClient.me` calls
  ``GET /v1/me`` and inspects the personal-key marker fields
  (``userId | userFirstName | userLastName``) — these are runtime-additive
  fields not declared in the OpenAPI ``ApiKeyInfo`` schema. The result is
  purely informational; no code path branches on key class.
- **Q-2** (routing field shape): ``POST /v1/agents`` now emits
  ``env: {type: "cloud" | "pool" | "machine", name?: str}`` instead of the
  legacy ``usePrivateWorker:true + labels.worker:X`` shape — the gateway
  rejects the legacy shape with ``400 "Unrecognized key(s)"``. Personal AND
  service-account keys both accept ``env`` with no code-path divergence.
- **Q-3** (worker discovery): :meth:`CloudCursorClient.list_workers` calls
  ``GET /v0/private-workers`` (works for personal keys per probe PROBE_07/44).
- **Q-8** (branch handling): the OpenAPI-documented ``autoGenerateBranch``
  field is rejected by the runtime gateway; the accepted toggle is
  ``workOnCurrentBranch:true`` (PROBE_29). ``branchName`` aliases were
  never accepted; ``startingRef`` is the only branch pointer.
- **Q-9** (GitHub-App caveat handling): two-pronged refuse/catch UX for
  the missing-Cursor-GitHub-App failure mode on ``github.com`` URLs.
  The early-refuse path is a ``GET /v1/repositories`` pre-flight inside
  :meth:`CloudCursorClient.create_agent` (gated on the new
  ``skip_github_app_preflight`` kwarg) — when the response carries an
  empty ``items`` list and the dispatch target's host is ``github.com``,
  the call raises :class:`GithubAppMissingError` BEFORE the heavy
  ``POST /v1/agents`` runs. The late-catch path covers the gateway's
  three known 400 message variants via :data:`_ERROR_CATALOG` rules
  ``integration_github_app_branch_not_found`` (regex extended to
  match BOTH ``"Failed to verify existence of branch ... in repository"``
  AND ``"Failed to determine repository default branch"`` per
  research/02 §3.1), ``repository_required`` (caller forgot
  ``repos[]``), and ``pr_resolution_failed`` (``prUrl`` against an
  uninstalled-App repo — same actionable fix as the branch-validation
  variant). All four early/late surfaces emit the SAME bilingual hint
  pointing at ``https://cursor.com/integrations/github`` so operator
  UX is identical regardless of whether popola refuses up front or
  the gateway 400s downstream.
- **Q-11** (adapter API stability): :meth:`CloudCursorClient.create_agent`
  drops ``use_private_worker`` and ``labels`` kwargs; the typed
  ``env: AgentEnv | None`` parameter replaces them. Legacy extras
  (``use_private_worker`` / ``labels`` / ``worker_name`` / ``machine_name``)
  passed via ``--cli-flag`` translate to ``env={type:"machine", name:X}``
  with a one-time ``DeprecationWarning`` per call (one minor release of
  graceful migration; v1.1+ removes the alias path entirely).
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import threading
import time
import warnings
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

import httpx

if TYPE_CHECKING:
    from popolaloom.daemon.event_log import EventLog

logger = logging.getLogger(__name__)

CLOUD_BUILD_COMMAND_MARKER: list[str] = ["__cloud__", "cursor-cloud"]
CURSOR_API_BASE: str = "https://api.cursor.com"
DEFAULT_TIMEOUT_S: float = 60.0
DEFAULT_STREAM_TIMEOUT_S: float = 600.0
"""Default httpx timeout for ``GET .../stream`` SSE connections (10 min).

A long ceiling matches Cursor's stream retention; the actual loop wakes on
each frame so a short network read timeout would prematurely cut keepalives."""

_CURSOR_API_KEY_ENV: str = "CURSOR_API_KEY"

_PARSE_ERROR_EVENT: str = "parse_error"
"""Synthetic SSE event_type emitted by :func:`iter_events` for malformed
frames. Pump translates it to ``cloud.sse.parse_error`` per
``state-source-of-truth.md`` §5 failure mode #4."""

_SSE_RAW_CHUNK_CAP_BYTES: int = 4096
"""Cap on raw payload bytes embedded in ``cloud.sse.parse_error`` (per
``state-source-of-truth.md`` §5)."""


# ---------------------------------------------------------------------------
# v0.10.0 — typed schema for ``POST /v1/agents`` `env` field (Q-2 + Q-11).
#
# AgentEnv is a tagged-union TypedDict that mirrors the runtime Zod schema
# discovered during v0.10.0 R1 path-2 probes (research/01-path-2-live-probe.md
# §"Schema fully nailed down" L97-122).  All three discriminator values are
# accepted by the gateway; the ``name`` slot is required for ``machine`` and
# optional for ``pool``.  ``cloud`` is the gateway default when ``env`` is
# omitted entirely.
#
# WorkerInfo is the parsed row shape for ``GET /v0/private-workers`` (Q-3).
# Personal API keys can list their own workers via this endpoint per
# probe PROBE_07/44; the row maps the camelCase wire field names to
# snake_case for ergonomic Python consumers.
# ---------------------------------------------------------------------------


class AgentEnv(TypedDict, total=False):
    """Runtime schema for ``POST /v1/agents`` ``env`` field (Q-2).

    Discriminated union over ``type``; the gateway rejects any other
    discriminator value with ``400 "Invalid discriminator value"``.

    - ``type="cloud"``: Cursor-managed VM. ``name`` is ignored if set.
    - ``type="machine"``: My-Machines (self-hosted) worker;
      ``name`` is REQUIRED (omitting it returns ``400 "env.name is required
      when env.type is machine"``).
    - ``type="pool"``: Self-Hosted Pool worker (Enterprise);
      ``name`` is optional.
    """

    type: Literal["cloud", "pool", "machine"]
    name: str


class WorkerInfo(TypedDict, total=False):
    """Parsed worker row from ``GET /v0/private-workers`` (Q-3).

    The wire shape is ``{workerId, name, isInUse, activeBcId, repoUrl,
    userId}`` (camelCase); this TypedDict normalizes to snake_case for
    consistency with the rest of the popolaloom codebase. ``name`` keeps
    its wire spelling because it is already snake_case-compatible.
    """

    worker_id: str
    name: str
    is_in_use: bool
    active_bc_id: str | None
    repo_url: str | None
    user_id: int | None


class CursorCloudError(Exception):
    """Base exception for :class:`CloudCursorClient`.

    Subclasses populated by :func:`_map_http_error` carry bilingual user-facing
    hints (``hint_en`` / ``hint_zh``) and a ``cli_exit`` code per the v0.8.6
    error catalog (``.local/research/v0.8.6_sse/422-error-catalog.md`` §3).
    """

    hint_en: str = ""
    hint_zh: str = ""
    cli_exit: int = 1

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        is_retryable: bool = False,
        hint_en: str | None = None,
        hint_zh: str | None = None,
        cli_exit: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code: int | None = status_code
        self.is_retryable: bool = is_retryable
        if hint_en is not None:
            self.hint_en = hint_en
        if hint_zh is not None:
            self.hint_zh = hint_zh
        if cli_exit is not None:
            self.cli_exit = cli_exit


class CursorCloudAuthError(CursorCloudError):
    """401/403 — credentials invalid or insufficient permission."""

    cli_exit = 77


class CursorCloudConflictError(CursorCloudError):
    """409 — e.g. ``agent_busy`` / ``run_not_cancellable``; not retryable."""

    cli_exit = 102


# ---------------------------------------------------------------------------
# v0.8.6 — 422 / integration error catalog (T2.1.3, blueprint:
# ``.local/research/v0.8.6_sse/422-error-catalog.md`` §4 YAML).
#
# Single source of truth for HTTP error → exception subclass mapping plus
# bilingual operator hints. The selector :func:`_map_http_error` consults
# this dict in three priority phases:
#
#   1. ``error.code`` exact match (catalog entry's ``code`` list)
#   2. ``error.message`` regex match (catalog entry's ``message_pattern``)
#   3. HTTP status fallback (catalog entry's ``http``)
#
# Adding a new entry: append to ``_ERROR_CATALOG`` then declare the
# corresponding subclass below referencing ``_ERROR_CATALOG[<id>]`` for
# class-level ``hint_en`` / ``hint_zh`` / ``cli_exit``.
# ---------------------------------------------------------------------------

_ERROR_CATALOG: dict[str, dict[str, Any]] = {
    "unauthorized_invalid_key": {
        "http": 401,
        "code": ["unauthorized"],
        "message_pattern": None,
        "subclass": "CursorCloudAuthError",
        "retry": False,
        "cli_exit": 77,
        "hint_en": (
            "Your Cursor API key was rejected as invalid. Generate or rotate a key at "
            "https://cursor.com/dashboard/integrations and re-export CURSOR_API_KEY "
            "before re-running popola dispatch --cli=cursor-cloud."
        ),
        "hint_zh": (
            "Cursor API key 校验失败：可能未设置或格式错误。请在 "
            "https://cursor.com/dashboard/integrations 重新生成 / 轮换密钥，"
            "导出新的 CURSOR_API_KEY 后重试 popola dispatch --cli=cursor-cloud。"
        ),
    },
    "unauthorized_revoked": {
        "http": 401,
        "code": ["api_key_not_found"],
        "message_pattern": None,
        "subclass": "CursorCloudApiKeyRevokedError",
        "retry": False,
        "cli_exit": 77,
        "hint_en": (
            "The API key was revoked or never existed. Open "
            "https://cursor.com/dashboard/integrations to mint a fresh key — old "
            "keys can't be reactivated."
        ),
        "hint_zh": (
            "该 API key 已被吊销或从未存在。请到 https://cursor.com/dashboard/integrations "
            "申请新密钥；已删除的密钥无法恢复。"
        ),
    },
    "forbidden_plan_required": {
        "http": 403,
        "code": ["plan_required"],
        "message_pattern": None,
        "subclass": "CursorCloudPlanRequiredError",
        "retry": False,
        "cli_exit": 78,
        "hint_en": (
            "Cloud Agents require a paid Cursor plan; this account is on a free "
            "tier. Upgrade at https://cursor.com/pricing or use an account with "
            "paid access."
        ),
        "hint_zh": (
            "Cloud Agents 需要付费版 Cursor 套餐，当前账户为免费档。请到 "
            "https://cursor.com/pricing 升级，或切换到已付费账户。"
        ),
    },
    "forbidden_role": {
        "http": 403,
        "code": ["role_forbidden"],
        "message_pattern": None,
        "subclass": "CursorCloudAuthError",
        "retry": False,
        "cli_exit": 77,
        "hint_en": (
            "Your Cursor team role is not allowed to call this endpoint. Ask a "
            "team admin at https://cursor.com/dashboard to grant access or use a "
            "service-account API key."
        ),
        "hint_zh": (
            "该接口需要更高的团队角色权限。请联系团队管理员（"
            "https://cursor.com/dashboard）授予权限，或改用服务账号 API key。"
        ),
    },
    "forbidden_feature": {
        "http": 403,
        "code": ["feature_unavailable"],
        "message_pattern": None,
        "subclass": "CursorCloudFeatureUnavailableError",
        "retry": False,
        "cli_exit": 78,
        "hint_en": (
            "The requested cloud feature is not enabled for your team. Visit "
            "https://cursor.com/dashboard/cloud-agents (Team feature settings) to "
            "enable it, or remove the conflicting flag from extra."
        ),
        "hint_zh": (
            "该云功能未对你所在团队启用。请到 https://cursor.com/dashboard/cloud-agents"
            "（Team feature settings）开启，或在 extra 中去掉相关 flag。"
        ),
    },
    "not_found_agent_or_run": {
        "http": 404,
        "code": ["agent_not_found", "run_not_found"],
        "message_pattern": None,
        "subclass": "CursorCloudNotFoundError",
        "retry": False,
        "cli_exit": 100,
        "hint_en": (
            "Cursor cannot find this agent or run — it may have been deleted or "
            "belong to another account. Check https://cursor.com/agents for the "
            "latest IDs and re-run with the correct task_id."
        ),
        "hint_zh": (
            "Cursor 找不到对应的 agent / run，可能已被删除或属于其他账号。请到 "
            "https://cursor.com/agents 查最新 ID，并用正确的 task_id 重试。"
        ),
    },
    "conflict_agent_busy": {
        "http": 409,
        "code": ["agent_busy"],
        "message_pattern": None,
        "subclass": "CursorCloudConflictError",
        "retry": False,
        "cli_exit": 102,
        "hint_en": (
            "Another run on this agent is still active. Wait for it to finish, or "
            "cancel it via popola cancel <task_id> "
            "(POST https://api.cursor.com/v1/agents/{id}/runs/{runId}/cancel) before "
            "sending a follow-up."
        ),
        "hint_zh": (
            "该 agent 已有另一次 run 在跑。等待其结束，或先 popola cancel <task_id>"
            "（即 POST https://api.cursor.com/v1/agents/{id}/runs/{runId}/cancel）"
            "再发起 follow-up。"
        ),
    },
    "conflict_archived": {
        "http": 409,
        "code": ["agent_archived"],
        "message_pattern": None,
        "subclass": "CursorCloudConflictError",
        "retry": False,
        "cli_exit": 102,
        "hint_en": (
            "This agent is archived and cannot accept new runs. Unarchive it (POST "
            "https://api.cursor.com/v1/agents/{id}/unarchive) or create a new "
            "agent at https://cursor.com/agents."
        ),
        "hint_zh": (
            "该 agent 已归档，无法接收新 run。请先解除归档（POST "
            "https://api.cursor.com/v1/agents/{id}/unarchive），或在 "
            "https://cursor.com/agents 新建 agent。"
        ),
    },
    "conflict_not_cancellable": {
        "http": 409,
        "code": ["run_not_cancellable"],
        "message_pattern": None,
        "subclass": "CursorCloudConflictError",
        "retry": False,
        "cli_exit": 102,
        "hint_en": (
            "This run already reached a terminal state — there is nothing to "
            "cancel. Use popola status <task_id> to inspect the final result; see "
            "https://cursor.com/agents."
        ),
        "hint_zh": (
            "该 run 已经到达终态，无需取消。请用 popola status <task_id> 查看最终结果；"
            "完整对话仍可在 https://cursor.com/agents 查看。"
        ),
    },
    "stream_expired": {
        "http": 410,
        "code": ["stream_expired"],
        "message_pattern": None,
        "subclass": "CursorCloudStreamExpiredError",
        "retry": False,
        "cli_exit": 75,
        "hint_en": (
            "The live stream for this run has expired. Fetch terminal state via "
            "popola status <task_id> (GET "
            "https://api.cursor.com/v1/agents/{id}/runs/{runId}); the original "
            "conversation is still readable on https://cursor.com/agents."
        ),
        "hint_zh": (
            "该 run 的 SSE 流已过期。请用 popola status <task_id>（对应 GET "
            "https://api.cursor.com/v1/agents/{id}/runs/{runId}）拉终态；"
            "完整对话仍可在 https://cursor.com/agents 查看。"
        ),
    },
    "integration_repo_allowlist": {
        # observed 422; documented 400/403 (unverified — needs follow-up)
        "http": [422, 400, 403],
        "code": ["validation_error", "feature_unavailable", None],
        "message_pattern": (
            r"(?i)(allow.?list|allowed.?repositor|"
            r"repository.+not.+(configured|installed|allowed))"
        ),
        "subclass": "RepoAllowlistError",
        "retry": False,
        "cli_exit": 78,
        "hint_en": (
            "The Cursor GitHub App is not allow-listed for this repository. Open "
            "https://github.com/apps/cursor (or your org's Integrations page) and "
            "add the repo, then revisit https://cursor.com/dashboard/integrations "
            "to confirm the GitHub connection."
        ),
        "hint_zh": (
            "Cursor GitHub App 未对该仓库开通。请到 https://github.com/apps/cursor"
            "（或组织 Integrations 页）勾选目标仓库，再到 "
            "https://cursor.com/dashboard/integrations 确认连接已生效。"
        ),
    },
    "integration_github_app_missing": {
        # observed 422; documented 403 (unverified — needs follow-up)
        "http": [422, 403],
        "code": ["feature_unavailable", None],
        "message_pattern": (
            r"(?i)(github.?app.+(not.?installed|missing)|"
            r"install.+(cursor|github.?app))"
        ),
        "subclass": "GithubAppMissingError",
        "retry": False,
        "cli_exit": 78,
        "hint_en": (
            "The Cursor GitHub App is not installed on the owning organization. A "
            "GitHub org admin must install it from https://github.com/apps/cursor "
            "and grant repository access — see "
            "https://cursor.com/docs/integrations/github.md."
        ),
        "hint_zh": (
            "目标仓库所在 GitHub 组织尚未安装 Cursor GitHub App。需要 GitHub 组织管理员到 "
            "https://github.com/apps/cursor 安装并授予仓库权限（参考 "
            "https://cursor.com/docs/integrations/github.md）。"
        ),
    },
    "integration_github_app_perms": {
        # observed 422; documented 403 (unverified — needs follow-up)
        "http": [422, 403],
        "code": ["feature_unavailable", "role_forbidden", None],
        "message_pattern": (
            r"(?i)(permission.+(denied|insufficient)|"
            r"missing.+(write|push|pull.?request))"
        ),
        "subclass": "GithubAppPermissionError",
        "retry": False,
        "cli_exit": 78,
        "hint_en": (
            "The Cursor GitHub App is installed but missing required permissions "
            "on this repo. Re-install or 'Configure' the app at "
            "https://github.com/apps/cursor and accept the updated permissions — "
            "full list at https://cursor.com/docs/integrations/github.md#permissions."
        ),
        "hint_zh": (
            "Cursor GitHub App 已安装但缺少必需权限。请到 https://github.com/apps/cursor "
            "'Configure' 并同意最新权限；完整权限表见 "
            "https://cursor.com/docs/integrations/github.md#permissions。"
        ),
    },
    "integration_github_app_branch_not_found": {
        # v0.9.9 F4: Cursor REST misclassifies a missing GitHub App as a 400
        # ``validation_error`` whose message claims the *branch* cannot be
        # verified, even when the branch exists. Routed to
        # :class:`GithubAppMissingError` per Q-V099-7 so the operator hint
        # surfaces the App-install fix (or the ``auto_create_pr=false``
        # workaround) instead of the misleading "schema" hint emitted by
        # the sibling :data:`validation_request_body` entry.
        #
        # v0.10.0 (DECISIONS Q-9): regex EXTENDED to also match the second
        # missing-App message variant ``"Failed to determine repository
        # default branch"`` observed when no ``startingRef`` is provided
        # (research/02-path-1-visibility-probe.md §3.1 row 1). The
        # actionable fix is identical for both variants — install the
        # Cursor GitHub App — so a single catalog entry covers both.
        #
        # Position: BEFORE ``validation_request_body`` so :func:`_score_entry`
        # picks this entry on a regex match (+5 over the generic body hit).
        "http": 400,
        "code": ["validation_error"],
        "message_pattern": (
            r"(?i)(failed\s+to\s+verify\s+existence\s+of\s+branch.+in\s+repository|"
            r"failed\s+to\s+determine\s+repository\s+default\s+branch)"
        ),
        "subclass": "GithubAppMissingError",
        "retry": False,
        "cli_exit": 78,
        "hint_en": (
            "This 'branch not found' error from Cursor REST almost always means "
            "the Cursor GitHub App is not installed on the target org/repo "
            "(rather than a genuinely missing branch). Install the App at "
            "https://cursor.com/integrations/github (or visit "
            "https://github.com/apps/cursor) and grant write access to the "
            "repository, OR pass `auto_create_pr=false` to skip PR creation. "
            "If your branch genuinely does not exist, double-check the spelling "
            "— the `(?i)` regex matches both 'main' and 'Main' / smart quotes."
        ),
        "hint_zh": (
            "此 'branch not found' 错误几乎总是表示 Cursor GitHub App 未安装到目标 "
            "org/repo（而非分支真的不存在）。请到 "
            "https://cursor.com/integrations/github 安装 App 并授予写权限，或使用 "
            "`auto_create_pr=false` 跳过 PR 创建。若分支确实不存在，请检查拼写。"
        ),
    },
    "repository_required": {
        # v0.10.0 (DECISIONS Q-9 / research/02 §3.1 row "NEW codes observed"):
        # the gateway returns 400 + ``error.code = "repository_required"`` when
        # the dispatch payload is missing ``repos[]`` (or ``repos[0].url``)
        # entirely. Distinct from the generic body fallback because the fix
        # is "supply --repo-url" rather than "fix the URL/ref format" — the
        # ``cli_exit=2`` (CLI usage error) lets shell scripts branch on this
        # without parsing the hint text.
        #
        # Selector wins over :data:`validation_request_body` via the explicit
        # ``code`` list (+10) which scores higher than the generic entry's
        # baseline +1. Position: BEFORE ``validation_request_body`` so the
        # ordering is documented even though :func:`_score_entry` picks the
        # max regardless of dict order.
        "http": 400,
        "code": ["repository_required"],
        "message_pattern": None,
        "subclass": "CursorCloudValidationError",
        "retry": False,
        "cli_exit": 2,
        "hint_en": (
            "Cursor rejected the dispatch because no repository was specified. "
            "Set --repo-url (or --cli-flag repo_url=...) to point at the target "
            "GitHub repo, or configure a default repo at "
            "https://cursor.com/settings before re-running."
        ),
        "hint_zh": (
            "Cursor 拒绝调度：未指定仓库。请通过 --repo-url（或 --cli-flag "
            "repo_url=...）指定目标 GitHub 仓库，或到 "
            "https://cursor.com/settings 配置默认仓库后重试。"
        ),
    },
    "pr_resolution_failed": {
        # v0.10.0 (DECISIONS Q-9 / research/02 §3.1 row P/P2): the gateway
        # returns 400 + ``error.code = "pr_resolution_failed"`` when the
        # dispatch sets ``repos[0].prUrl`` AND the Cursor GitHub App is not
        # installed (or lacks PR-read permission) on the owning org. The
        # actionable fix is identical to
        # ``integration_github_app_branch_not_found`` — install / configure
        # the Cursor GitHub App — so we reuse :class:`GithubAppMissingError`
        # and ``cli_exit=78`` for ABI parity with the branch-validation
        # variant. Selector wins over :data:`validation_request_body` via
        # the explicit ``code`` list (+10).
        #
        # Position: BEFORE ``validation_request_body`` for the same
        # documentation-of-intent reason as ``repository_required`` above.
        "http": 400,
        "code": ["pr_resolution_failed"],
        "message_pattern": None,
        "subclass": "GithubAppMissingError",
        "retry": False,
        "cli_exit": 78,
        "hint_en": (
            "Cursor could not resolve the pull request — almost always because "
            "the Cursor GitHub App is not installed (or lacks PR-read "
            "permission) on the owning org/repo. Install or 'Configure' the "
            "App at https://cursor.com/integrations/github (or "
            "https://github.com/apps/cursor) and grant repository access; "
            "see https://cursor.com/docs/integrations/github.md."
        ),
        "hint_zh": (
            "Cursor 无法获取 PR 详情，通常是因为目标仓库所在 org 未安装 "
            "Cursor GitHub App（或缺少 PR 读取权限）。请到 "
            "https://cursor.com/integrations/github（或 "
            "https://github.com/apps/cursor）安装/配置 App 并授予仓库权限"
            "（参考 https://cursor.com/docs/integrations/github.md）。"
        ),
    },
    "validation_request_body": {
        "http": [400, 422],
        "code": ["validation_error", "missing_body"],
        "message_pattern": None,
        "subclass": "CursorCloudValidationError",
        "retry": False,
        "cli_exit": 64,
        "hint_en": (
            "Cursor rejected the request body — likely an invalid repos[0].url, "
            "prUrl, or startingRef. Verify the URL format and re-run; see "
            "https://cursor.com/docs/cloud-agent/api/endpoints.md#create-an-agent "
            "for the schema."
        ),
        "hint_zh": (
            "Cursor 拒绝了请求体：通常是 repos[0].url、prUrl 或 startingRef 格式不合法。请按 "
            "https://cursor.com/docs/cloud-agent/api/endpoints.md#create-an-agent "
            "的 schema 校正后重试。"
        ),
    },
    "rate_limit": {
        "http": 429,
        "code": ["rate_limit_exceeded"],
        "message_pattern": None,
        "subclass": "CursorCloudRateLimitError",
        "retry": True,
        "cli_exit": 75,
        "hint_en": (
            "Cursor rate-limited this client; immediate retry will fail. Honor the "
            "Retry-After header (default ~60 s) and reduce dispatch concurrency; "
            "see https://cursor.com/docs/api.md#rate-limits."
        ),
        "hint_zh": (
            "Cursor 已限流，立即重试仍会失败。请按 Retry-After 头部等待（默认 ~60 s），"
            "并降低调度并发；详见 https://cursor.com/docs/api.md#rate-limits。"
        ),
    },
    "backend_5xx": {
        "http": [500, 502, 503, 504],
        "code": ["internal_error", "upstream_error", None],
        "message_pattern": None,
        "subclass": "CursorCloudError",
        "retry": True,
        "cli_exit": 75,
        "hint_en": (
            "Cursor's backend is failing — try again in a minute. If the error "
            "persists, check https://status.cursor.com (unverified — needs "
            "follow-up) and report to background-agent-feedback@cursor.com with "
            "the request id."
        ),
        "hint_zh": (
            "Cursor 后端故障，请稍后（约 1 分钟）重试。若持续失败，请查看 "
            "https://status.cursor.com（未验证 — 需后续确认）并附 request id 反馈到 "
            "background-agent-feedback@cursor.com。"
        ),
    },
}


class CursorCloudApiKeyRevokedError(CursorCloudAuthError):
    """401 + ``api_key_not_found`` — key revoked or never existed."""

    hint_en = _ERROR_CATALOG["unauthorized_revoked"]["hint_en"]
    hint_zh = _ERROR_CATALOG["unauthorized_revoked"]["hint_zh"]
    cli_exit = _ERROR_CATALOG["unauthorized_revoked"]["cli_exit"]


class CursorCloudPlanRequiredError(CursorCloudError):
    """403 + ``plan_required`` — paid plan needed for cloud agents."""

    hint_en = _ERROR_CATALOG["forbidden_plan_required"]["hint_en"]
    hint_zh = _ERROR_CATALOG["forbidden_plan_required"]["hint_zh"]
    cli_exit = _ERROR_CATALOG["forbidden_plan_required"]["cli_exit"]


class CursorCloudFeatureUnavailableError(CursorCloudError):
    """403 + ``feature_unavailable`` — team feature flag not enabled."""

    hint_en = _ERROR_CATALOG["forbidden_feature"]["hint_en"]
    hint_zh = _ERROR_CATALOG["forbidden_feature"]["hint_zh"]
    cli_exit = _ERROR_CATALOG["forbidden_feature"]["cli_exit"]


class CursorCloudNotFoundError(CursorCloudError):
    """404 — agent / run not found."""

    hint_en = _ERROR_CATALOG["not_found_agent_or_run"]["hint_en"]
    hint_zh = _ERROR_CATALOG["not_found_agent_or_run"]["hint_zh"]
    cli_exit = _ERROR_CATALOG["not_found_agent_or_run"]["cli_exit"]


class CursorCloudStreamExpiredError(CursorCloudError):
    """410 + ``stream_expired`` — SSE retention window elapsed (T2.1.1)."""

    hint_en = _ERROR_CATALOG["stream_expired"]["hint_en"]
    hint_zh = _ERROR_CATALOG["stream_expired"]["hint_zh"]
    cli_exit = _ERROR_CATALOG["stream_expired"]["cli_exit"]


class CursorCloudStreamInvalidLastEventIdError(CursorCloudError):
    """410 + ``invalid_last_event_id`` — recoverable: drop saved id, reconnect (T2.1.1).

    Distinct from :class:`CursorCloudStreamExpiredError`: the latter is
    terminal for the stream channel (poller takes over per Q-A-4). This one
    only invalidates the resume cursor — caller should retry the same
    ``stream_run`` *without* a ``Last-Event-ID`` header. Intentionally NOT
    in :data:`_ERROR_CATALOG` to keep the 16-entry contract stable; hint
    text is hardcoded here.
    """

    cli_exit = 75
    hint_en = (
        "Cursor rejected the Last-Event-ID sent on resume; the saved cursor "
        "is no longer valid for this run. The SSE reader should drop the "
        "saved id and reconnect without the header — see "
        "https://cursor.com/docs/cloud-agent/api/endpoints.md for the "
        "resume contract."
    )
    hint_zh = (
        "断线续传时携带的 Last-Event-ID 已被 Cursor 拒绝，该游标对当前 run 已失效。"
        "SSE 读取器应丢弃已保存的 id 并不带头部重新连接；续传契约见 "
        "https://cursor.com/docs/cloud-agent/api/endpoints.md。"
    )


class RepoAllowlistError(CursorCloudError):
    """422 — repo not in Cursor GitHub App allow-list."""

    hint_en = _ERROR_CATALOG["integration_repo_allowlist"]["hint_en"]
    hint_zh = _ERROR_CATALOG["integration_repo_allowlist"]["hint_zh"]
    cli_exit = _ERROR_CATALOG["integration_repo_allowlist"]["cli_exit"]


class GithubAppMissingError(CursorCloudError):
    """422 — Cursor GitHub App not installed on owning org."""

    hint_en = _ERROR_CATALOG["integration_github_app_missing"]["hint_en"]
    hint_zh = _ERROR_CATALOG["integration_github_app_missing"]["hint_zh"]
    cli_exit = _ERROR_CATALOG["integration_github_app_missing"]["cli_exit"]


class GithubAppPermissionError(CursorCloudError):
    """422 — Cursor GitHub App lacks required repo permissions."""

    hint_en = _ERROR_CATALOG["integration_github_app_perms"]["hint_en"]
    hint_zh = _ERROR_CATALOG["integration_github_app_perms"]["hint_zh"]
    cli_exit = _ERROR_CATALOG["integration_github_app_perms"]["cli_exit"]


class CursorCloudValidationError(CursorCloudError):
    """400/422 — generic request-body validation failure (fallback for 422)."""

    hint_en = _ERROR_CATALOG["validation_request_body"]["hint_en"]
    hint_zh = _ERROR_CATALOG["validation_request_body"]["hint_zh"]
    cli_exit = _ERROR_CATALOG["validation_request_body"]["cli_exit"]


class CursorCloudRateLimitError(CursorCloudError):
    """429 — rate limit; retryable per ``Retry-After`` + :class:`BackoffConfig`."""

    hint_en = _ERROR_CATALOG["rate_limit"]["hint_en"]
    hint_zh = _ERROR_CATALOG["rate_limit"]["hint_zh"]
    cli_exit = _ERROR_CATALOG["rate_limit"]["cli_exit"]


_SUBCLASS_REGISTRY: dict[str, type[CursorCloudError]] = {
    "CursorCloudError": CursorCloudError,
    "CursorCloudAuthError": CursorCloudAuthError,
    "CursorCloudConflictError": CursorCloudConflictError,
    "CursorCloudApiKeyRevokedError": CursorCloudApiKeyRevokedError,
    "CursorCloudPlanRequiredError": CursorCloudPlanRequiredError,
    "CursorCloudFeatureUnavailableError": CursorCloudFeatureUnavailableError,
    "CursorCloudNotFoundError": CursorCloudNotFoundError,
    "CursorCloudStreamExpiredError": CursorCloudStreamExpiredError,
    "CursorCloudStreamInvalidLastEventIdError": CursorCloudStreamInvalidLastEventIdError,
    "RepoAllowlistError": RepoAllowlistError,
    "GithubAppMissingError": GithubAppMissingError,
    "GithubAppPermissionError": GithubAppPermissionError,
    "CursorCloudValidationError": CursorCloudValidationError,
    "CursorCloudRateLimitError": CursorCloudRateLimitError,
}


def _http_matches(entry: dict[str, Any], status: int) -> bool:
    http = entry.get("http")
    if isinstance(http, list):
        return status in http
    return http == status


def _normalize_code_list(raw: Any) -> list[str | None] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return cast(list[str | None], raw)
    return None


def _parse_error_body(response: httpx.Response) -> tuple[str | None, str | None]:
    """Extract ``(error.code, error.message)`` from a Cursor REST response.

    Tolerates both canonical ``{"error": {"code", "message"}}`` (Cloud Agents
    v1) and legacy flat ``{"error": "<title>", "message": "<text>"}`` shapes
    (per ``api.md``). Falls back to ``detail`` (gateway-style 422) when
    neither matches. Returns ``(None, None)`` on JSON parse failure or empty
    body.
    """
    try:
        data: Any = response.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None, None
    except Exception as exc:  # noqa: BLE001 — httpx may raise httpx-specific errors here
        logger.debug("cursor-cloud body json parse failed: %s", exc)
        return None, None

    if not isinstance(data, dict):
        return None, None

    err_obj = data.get("error")
    code: str | None = None
    message: str | None = None

    if isinstance(err_obj, dict):
        raw_code = err_obj.get("code")
        if isinstance(raw_code, str) and raw_code:
            code = raw_code
        raw_msg = err_obj.get("message")
        if isinstance(raw_msg, str) and raw_msg:
            message = raw_msg

    if message is None:
        for key in ("message", "detail", "error_description"):
            raw = data.get(key)
            if isinstance(raw, str) and raw:
                message = raw
                break

    if message is None and isinstance(err_obj, str) and err_obj:
        # Legacy ``{"error": "<Title>"}`` shape (per ``api.md``) — used only
        # when no richer field exists, since the title alone ("Unprocessable
        # Entity") is rarely actionable.
        message = err_obj

    return code, message


def _score_entry(
    entry: dict[str, Any],
    status: int,
    err_code: str | None,
    err_message: str | None,
) -> int | None:
    """Return a match score (higher = better fit) or ``None`` if incompatible.

    Scoring follows the catalog precedence (``error.code → error.message
    regex → HTTP status``):

    - **+1** baseline for any entry whose ``http`` matches.
    - **+10** when ``error.code`` is present in the entry's ``code`` list.
    - **+5** when ``error.message`` is matched by the entry's
      ``message_pattern`` regex.
    - Returns **None** when an explicit ``code`` constraint is set and the
      response carries a non-matching ``error.code`` (entry is not a candidate).
    - Returns **None** when an explicit ``message_pattern`` constraint is set,
      the response carries a non-empty message, and the regex does not match.
    """
    if not _http_matches(entry, status):
        return None

    score = 1

    code_list = _normalize_code_list(entry.get("code"))
    if code_list is not None and err_code is not None:
        if err_code in code_list:
            score += 10
        else:
            return None

    msg_pattern = entry.get("message_pattern")
    if isinstance(msg_pattern, str) and err_message is not None:
        if re.search(msg_pattern, err_message):
            score += 5
        else:
            return None

    return score


def _build_error(
    entry: dict[str, Any],
    status: int,
    response: httpx.Response,
) -> CursorCloudError:
    subclass_name = entry["subclass"]
    cls = _SUBCLASS_REGISTRY.get(subclass_name, CursorCloudError)
    detail = response.text[:500] if response.text else ""
    msg = (
        f"cursor-cloud HTTP {status}: {detail}"
        if detail
        else f"cursor-cloud HTTP {status}"
    )
    is_retryable = bool(entry.get("retry", False))
    return cls(
        msg,
        status_code=status,
        is_retryable=is_retryable,
        hint_en=entry["hint_en"],
        hint_zh=entry["hint_zh"],
        cli_exit=entry["cli_exit"],
    )


def _map_http_error(response: httpx.Response) -> CursorCloudError:
    """Map an HTTP error response to the most-specific :class:`CursorCloudError`.

    Selector precedence (per ``422-error-catalog.md`` §4 YAML header):

    1. ``error.code`` exact match against catalog ``code`` lists.
    2. ``error.message`` regex match against catalog ``message_pattern``.
    3. HTTP status fallback against catalog ``http``.

    On a 422 with no scoring match (no code, no regex hit), logs the body at
    ``WARNING`` and returns :class:`CursorCloudValidationError` rather than
    silently downgrading to the generic base — see workspace
    ``No Silent Failures`` rule.
    """
    status = response.status_code
    err_code, err_message = _parse_error_body(response)

    best_entry: dict[str, Any] | None = None
    best_score: int = 0
    for entry in _ERROR_CATALOG.values():
        score = _score_entry(entry, status, err_code, err_message)
        if score is None:
            continue
        if score > best_score:
            best_entry = entry
            best_score = score

    if best_entry is not None and best_score >= 5:
        return _build_error(best_entry, status, response)

    if status == 422:
        logger.warning(
            "cursor-cloud unrecognized 422 body (status=%s, code=%r, message=%r): %s",
            status,
            err_code,
            err_message,
            response.text[:1000] if response.text else "<empty>",
        )
        return _build_error(_ERROR_CATALOG["validation_request_body"], status, response)

    if best_entry is not None:
        return _build_error(best_entry, status, response)

    detail = response.text[:500] if response.text else ""
    msg = (
        f"cursor-cloud HTTP {status}: {detail}"
        if detail
        else f"cursor-cloud HTTP {status}"
    )
    is_retryable = status >= 500
    return CursorCloudError(msg, status_code=status, is_retryable=is_retryable)


# ---------------------------------------------------------------------------
# v0.8.6 — SSE wire parser (T2.1.1, blueprint:
# ``.local/research/v0.8.6_sse/sse-event-schema.md`` §3 mapping table).
#
# Standalone parser kept module-level so unit tests can drive it with
# in-memory line iterables without an httpx round-trip. Per
# ``state-source-of-truth.md`` §1.2 rule 4, malformed frames MUST surface
# as ``parse_error`` events (No-Silent-Failures) rather than be dropped.
# ---------------------------------------------------------------------------


def _emit_sse_frame(
    event: str | None,
    data_lines: list[str],
    sse_id: str | None,
) -> Iterator[tuple[str, dict[str, Any], str | None]]:
    """Yield exactly one parsed event for a completed SSE frame."""
    event_type = event if event is not None else "message"
    raw_data = "\n".join(data_lines) if data_lines else ""

    if not raw_data:
        yield event_type, {}, sse_id
        return

    try:
        parsed: Any = json.loads(raw_data)
    except (json.JSONDecodeError, ValueError) as exc:
        # No-Silent-Failures: emit a synthetic parse_error frame so pump
        # records a ``cloud.sse.parse_error`` envelope instead of dropping.
        cap = raw_data[:_SSE_RAW_CHUNK_CAP_BYTES]
        yield (
            _PARSE_ERROR_EVENT,
            {
                "raw_chunk_b64": base64.b64encode(cap.encode("utf-8")).decode("ascii"),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "event_attempt": event_type,
            },
            sse_id,
        )
        return

    if isinstance(parsed, dict):
        yield event_type, cast(dict[str, Any], parsed), sse_id
    else:
        yield event_type, {"value": parsed}, sse_id


def iter_events(
    line_source: Iterable[str],
) -> Iterator[tuple[str, dict[str, Any], str | None]]:
    """Parse an SSE byte stream into ``(event_type, data_dict, sse_id)`` triples.

    Implements the ``text/event-stream`` framing rules from MDN /
    `WHATWG <https://html.spec.whatwg.org/multipage/server-sent-events.html>`_:

    - Frame ends on a blank line.
    - ``event:``, ``data:`` (multi-line concatenated by ``\\n``), and
      ``id:`` lines are recognized; ``retry:`` and unknown fields are
      ignored.
    - A leading single space after the field separator is stripped.
    - Lines starting with ``:`` are comments / keepalives.
    - JSON parse failure on the assembled ``data:`` payload yields a
      synthetic ``parse_error`` event (see :data:`_PARSE_ERROR_EVENT`)
      rather than silently dropping the frame.

    Args:
        line_source: iterable of decoded SSE lines (httpx
            ``Response.iter_lines()`` format — newline already stripped).

    Yields:
        tuples of ``(event_type, data_dict, sse_event_id)`` where
        ``sse_event_id`` is the ``id:`` line value (or ``None`` if absent).
    """
    event: str | None = None
    data_lines: list[str] = []
    sse_id: str | None = None

    def _frame_pending() -> bool:
        return event is not None or bool(data_lines) or sse_id is not None

    for raw_line in line_source:
        line = raw_line.rstrip("\r")
        if line == "":
            if _frame_pending():
                yield from _emit_sse_frame(event, data_lines, sse_id)
                event = None
                data_lines = []
                sse_id = None
            continue
        if line.startswith(":"):
            continue
        if ":" in line:
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
        elif field == "id":
            sse_id = value
        # retry: and unknown fields are ignored per spec

    if _frame_pending():
        yield from _emit_sse_frame(event, data_lines, sse_id)


# ---------------------------------------------------------------------------
# v0.8.8 — 429 / quota-class backoff (T2.1.3, blueprint:
# ``.local/research/v0.8.8_multi_run/quota-config.md`` §2.1 schema, §3
# algorithm, §3.2 ``Retry-After`` parser).
#
# This block is disjoint from T2.1.1's ``create_followup_run`` plumbing so
# the two PRs can land independently (per PLAN.md §4.1 owned-files note).
# Lives at module scope above :class:`CloudCursorClient` because both the
# poller (`daemon/cloud_poller.py`) and the client itself consume it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackoffConfig:
    """Validated ``[cloud.backoff]`` section of ``popolad.toml``.

    Defaults match ``quota-config.md`` §2.1 (500 ms base, 30 s cap, 5 retries,
    ±25 % jitter, ``Retry-After`` honored). The loader in
    ``daemon/main.py::load_popolad_config`` populates this dataclass after
    range / type validation per `quota-config.md` §2.3 (No-Silent-Failures).
    """

    max_retries: int = 5
    base_backoff_ms: int = 500
    max_backoff_ms: int = 30_000
    jitter_pct: int = 25
    honor_retry_after: bool = True


_RETRY_AFTER_HEADER_TRUNC: int = 256
"""Cap on the offending ``Retry-After`` header in a WARN log (per spec §3.2)."""


def _parse_retry_after(raw: str | None) -> int | None:
    """Parse a ``Retry-After`` header into a non-negative millisecond delay.

    RFC 7231 §7.1.3 allows two forms:

    1. ``delta-seconds`` integer (most common — e.g. ``Retry-After: 60``).
    2. HTTP-date (rare — e.g. ``Retry-After: Wed, 21 Oct 2026 07:28:00 GMT``).

    Returns ``None`` and emits a WARN log on a garbled header (No-Silent-
    Failures per ``quota-config.md`` §3.2). Negative deltas (HTTP-dates in
    the past) are clamped to 0 so callers can still sleep ``min(0, server)``
    on the next iteration without a sign check.
    """
    if raw is None:
        return None
    raw_stripped = raw.strip()
    if not raw_stripped:
        return None

    if re.fullmatch(r"[0-9]+", raw_stripped):
        try:
            return int(raw_stripped) * 1000
        except ValueError:
            logger.warning(
                "cursor-cloud Retry-After int parse failed: %r",
                raw_stripped[:_RETRY_AFTER_HEADER_TRUNC],
            )
            return None

    try:
        dt = parsedate_to_datetime(raw_stripped)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "cursor-cloud Retry-After date parse failed (%s): %r",
            type(exc).__name__,
            raw_stripped[:_RETRY_AFTER_HEADER_TRUNC],
        )
        return None
    if dt is None:
        logger.warning(
            "cursor-cloud Retry-After date parse returned None: %r",
            raw_stripped[:_RETRY_AFTER_HEADER_TRUNC],
        )
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta_s = (dt - datetime.now(UTC)).total_seconds()
    return max(0, int(delta_s * 1000))


def _compute_backoff(
    attempt: int,
    cfg: BackoffConfig,
    retry_after_header: str | None,
    *,
    rng: random.Random | None = None,
) -> float:
    """Compute the per-attempt sleep delay in milliseconds.

    Implements ``quota-config.md`` §3.1 verbatim:

    .. code-block:: text

        if cfg.honor_retry_after and Retry-After present and parses:
            return min(server_ms, cfg.max_backoff_ms)
        raw_ms   = cfg.base_backoff_ms * (2 ** attempt)
        capped   = min(raw_ms, cfg.max_backoff_ms)
        jitter_f = 1.0 + uniform(-jitter_pct, +jitter_pct) / 100.0
        return max(0, capped * jitter_f)

    The optional ``rng`` parameter lets tests pin the jitter to a
    deterministic ``random.Random(seed)`` so the schedule pin test is
    reproducible across CI runs.
    """
    if cfg.honor_retry_after and retry_after_header is not None:
        server_ms = _parse_retry_after(retry_after_header)
        if server_ms is not None:
            return float(min(server_ms, cfg.max_backoff_ms))

    raw_ms = cfg.base_backoff_ms * (2**attempt)
    capped = min(raw_ms, cfg.max_backoff_ms)
    if cfg.jitter_pct > 0:
        rand = rng if rng is not None else random
        jitter = float(rand.uniform(-cfg.jitter_pct, cfg.jitter_pct)) / 100.0
        return max(0.0, float(capped) * (1.0 + jitter))
    return float(capped)


def _is_quota_class_409(response: httpx.Response) -> bool:
    """Predicate for a 409 that should be treated as quota / rate-limit class.

    v0.8.8 reserves this hook for forward-compat with a future Cursor
    documented ``code = "quota_exceeded"``. As of `endpoints.md` (retrieved
    2026-05-08), no documented 409 carries quota semantics — the existing
    ``agent_busy`` / ``agent_archived`` / ``run_not_cancellable`` codes are
    all preserved by their existing v0.8.6 catalog handling. The default
    therefore returns ``False`` (i.e. all 409s fall through to
    :func:`_map_http_error`); a single-line tweak here switches the path
    when Cursor publishes the new code without bumping the schema.
    """
    if response.status_code != 409:
        return False
    err_code, _ = _parse_error_body(response)
    return err_code == "quota_exceeded"


class CloudCursorClient:
    """Synchronous ``httpx`` wrapper around ``/v1/agents``.

    Auth: HTTP Basic via ``auth=(api_key, "")``.

    v0.8.6 (T2.1.1) adds :meth:`stream_run` for SSE on
    ``GET .../runs/{runId}/stream``; ``create_followup`` (``POST .../runs``)
    is still reserved for a later milestone.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = CURSOR_API_BASE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not api_key:
            raise ValueError("cursor-cloud: api_key must be non-empty")
        self._api_key: str = api_key
        self._base_url: str = base_url.rstrip("/")
        self._timeout_s: float = timeout_s
        self._client: httpx.Client = httpx.Client(
            base_url=self._base_url,
            auth=(self._api_key, ""),
            timeout=timeout_s,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CloudCursorClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def create_agent(
        self,
        prompt: str,
        model: str,
        repo_url: str | None = None,
        *,
        starting_ref: str = "main",
        auto_create_pr: bool = False,
        work_on_current_branch: bool = False,
        skip_reviewer_request: bool = False,
        pr_url: str | None = None,
        env_vars: dict[str, str] | None = None,
        env: AgentEnv | None = None,
        skip_github_app_preflight: bool = False,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """POST ``/v1/agents`` — launch a cloud agent; returns parsed JSON body.

        v0.10.0 wire schema (per DECISIONS Q-2 + Q-8):

        - Routing now flows via ``env: {type, name?}`` discriminated union
          instead of the legacy ``usePrivateWorker:true + labels.worker:X``
          shape (the gateway rejects the legacy shape with
          ``400 "Unrecognized key(s)"``; PROBE_21/22). Pass ``env=None``
          (the default) for Cursor-managed cloud routing — the gateway
          interprets a missing ``env`` as ``{type:"cloud"}``.
        - ``work_on_current_branch=True`` emits ``workOnCurrentBranch:true``
          (PROBE_29 — accepted by gateway). The historical
          ``autoGenerateBranch:false`` field is rejected by the runtime
          gateway as "Unrecognized key" (PROBE_13/14) and is no longer
          emitted.

        v0.10.0 GitHub-App pre-flight (DECISIONS Q-9): when ``repo_url``
        is supplied AND its host is ``github.com`` AND
        ``skip_github_app_preflight`` is left at its default ``False``,
        this method calls
        :func:`popolaloom.cloud.preflight.check_github_app_installed`
        BEFORE issuing the ``POST /v1/agents`` request. If the response's
        ``items`` list is empty (the Cursor GitHub App is not installed
        on any of the API key's GitHub orgs), the dispatch is refused
        early with :class:`GithubAppMissingError` carrying the SAME
        bilingual hint as the late-catch
        :data:`_ERROR_CATALOG` rule
        ``integration_github_app_branch_not_found`` — operator UX is
        identical regardless of which surface fires.

        Args:
            prompt: Task prompt placed under ``prompt.text``.
            model: Cursor model id (e.g. ``"composer-2"`` / ``"default"``).
            repo_url: Repo URL for ``repos[0].url`` (mutually exclusive
                with ``pr_url``).
            starting_ref: Git ref placed under ``repos[0].startingRef``;
                ignored when ``pr_url`` is set.
            auto_create_pr: Toggles ``autoCreatePR`` on the body.
            work_on_current_branch: When ``True`` emits
                ``workOnCurrentBranch:true`` (the only "use current branch"
                toggle the runtime gateway accepts; Q-8).
            skip_reviewer_request: Emits ``skipReviewerRequest:true`` only
                when paired with ``auto_create_pr=True``.
            pr_url: Mutually exclusive with ``repo_url``; emits
                ``repos[0].prUrl``.
            env_vars: Optional ``envVars`` dict; values must be ``str``.
            env: Optional :class:`AgentEnv` discriminated union. Pass
                ``{type:"machine", name:X}`` for self-hosted-worker
                routing, ``{type:"pool"[, name:X]}`` for Self-Hosted Pool,
                or ``{type:"cloud"}`` to be explicit about the default.
                ``None`` (the default) leaves ``env`` off the payload —
                the gateway treats that as ``{type:"cloud"}``.
            skip_github_app_preflight: Opt-out for the v0.10.0 Q-9
                ``GET /v1/repositories`` pre-flight. Defaults to
                ``False`` (pre-flight ENABLED). Set ``True`` when:
                (a) test suites that mock the dispatch HTTP path want
                a single round-trip and don't need the pre-flight,
                (b) service-account keys that intentionally bypass the
                Cursor GitHub App, or (c) ``pr_url`` dispatches where
                the App-install state is verified out-of-band. The
                late-catch catalog rules
                (``integration_github_app_branch_not_found`` /
                ``pr_resolution_failed``) still produce a friendly hint
                if a downstream 400 fires. (Pre-flight is also a no-op
                for non-``github.com`` hosts — :func:`check_github_app_installed`
                returns ``installed=None`` per A2 AC 3, which this method
                treats as "skip" without raising.)
            timeout_s: Per-call timeout override; ``None`` falls back to
                client default.

        Returns:
            Parsed JSON body — typically
            ``{"agent": {"id": "bc-..."}, "run": {"id": "run-..."}}``.

        Raises:
            ValueError: when both ``repo_url`` and ``pr_url`` are absent.
            GithubAppMissingError: from the v0.10.0 Q-9 pre-flight when
                ``repo_url`` host is ``github.com`` and
                ``GET /v1/repositories`` returns an empty ``items`` list.
            CursorCloudError: any 4xx / 5xx mapped via :func:`_map_http_error`.
        """
        if not pr_url and not repo_url:
            raise ValueError("cursor-cloud: repo_url or pr_url is required for create_agent")

        # v0.10.0 (DECISIONS Q-9) — early-refuse pre-flight for github.com
        # repo URLs. Performs a lightweight ``GET /v1/repositories`` probe
        # to detect the most common operator failure mode (the Cursor
        # GitHub App is not installed on the owning org) BEFORE the heavy
        # ``POST /v1/agents`` call. Refuses with the SAME bilingual hint
        # as the late-catch catalog rule
        # ``integration_github_app_branch_not_found`` so the early refuse
        # and the late catch produce identical operator UX (see PLAN.md
        # C2 AC 4). The pre-flight is a no-op for non-github.com hosts
        # (``installed=None``); the only refuse-trigger is ``installed=False``.
        #
        # Design choice (PLAN.md C2 AC 5): the opt-out is the
        # ``skip_github_app_preflight`` kwarg on this method, NOT a key
        # plumbed through :func:`_normalize_cloud_extra` output. Rationale:
        #   1. The pre-flight is a property of THIS HTTP call (one extra
        #      round trip), not of the user-facing extras grammar — keeping
        #      it in the method signature mirrors the existing
        #      ``timeout_s`` / ``env`` / ``env_vars`` kwargs which are
        #      also adapter-call-shape-only.
        #   2. Tests can opt out by calling ``client.create_agent(...,
        #      skip_github_app_preflight=True)`` directly without round-
        #      tripping through the marker payload.
        #   3. End-to-end CLI opt-out via ``--cli-flag
        #      skip_github_app_preflight=true`` is a follow-up that
        #      requires the supervisor (``daemon/supervisor.py``) to
        #      pluck the value from ``extra`` and forward it to this
        #      kwarg. That's read-only territory for this Wave C2 task
        #      and is documented in the L0 follow-up note.
        #   4. ``pr_url`` dispatches skip the pre-flight (only ``repo_url``
        #      gates it) — the catch path is the new
        #      ``pr_resolution_failed`` catalog rule which already routes
        #      to :class:`GithubAppMissingError` with the same hints.
        if repo_url and not skip_github_app_preflight:
            from popolaloom.cloud.preflight import check_github_app_installed

            preflight_result = check_github_app_installed(self, repo_url)
            if preflight_result.installed is False:
                # Mirror the catalog rule's hint text verbatim so the
                # early refuse is byte-identical to the late catch.
                catalog_entry = _ERROR_CATALOG["integration_github_app_branch_not_found"]
                raise GithubAppMissingError(
                    "cursor-cloud GitHub App pre-flight refused dispatch for "
                    f"{repo_url}: {preflight_result.message}",
                    hint_en=catalog_entry["hint_en"],
                    hint_zh=catalog_entry["hint_zh"],
                    cli_exit=catalog_entry["cli_exit"],
                )

        repo_entry: dict[str, Any] = {}
        if pr_url:
            repo_entry["prUrl"] = pr_url
        else:
            repo_entry["url"] = repo_url
            repo_entry["startingRef"] = starting_ref

        payload: dict[str, Any] = {
            "prompt": {"text": prompt},
            "repos": [repo_entry],
            "autoCreatePR": auto_create_pr,
        }
        payload["model"] = {"id": model}

        if work_on_current_branch:
            payload["workOnCurrentBranch"] = True

        if auto_create_pr and skip_reviewer_request:
            payload["skipReviewerRequest"] = True

        if env_vars:
            payload["envVars"] = dict(env_vars)

        if env is not None:
            payload["env"] = dict(env)

        return self._request_json(
            "POST",
            "/v1/agents",
            json_body=payload,
            timeout_s=timeout_s,
        )

    def get_agent(self, agent_id: str, *, timeout_s: float | None = None) -> dict[str, Any]:
        """GET ``/v1/agents/{id}``."""
        return self._request_json(
            "GET",
            f"/v1/agents/{agent_id}",
            timeout_s=timeout_s,
        )

    def get_run(
        self,
        agent_id: str,
        run_id: str,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """GET ``/v1/agents/{id}/runs/{runId}``."""
        return self._request_json(
            "GET",
            f"/v1/agents/{agent_id}/runs/{run_id}",
            timeout_s=timeout_s,
        )

    def cancel_run(
        self,
        agent_id: str,
        run_id: str,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """POST ``/v1/agents/{id}/runs/{runId}/cancel``."""
        return self._request_json(
            "POST",
            f"/v1/agents/{agent_id}/runs/{run_id}/cancel",
            timeout_s=timeout_s,
        )

    # -----------------------------------------------------------------
    # v0.10.0 — Q-1 / Q-3 informational HTTP surfaces.
    #
    # ``me()`` and ``list_workers()`` are net-add HTTP wrappers used by
    # the CLI pre-flight gates (``cloud worker dispatch``) and by an
    # informational ``api_key_class`` log line. Both route errors through
    # :func:`_map_http_error` via :meth:`_request_json`, so the bilingual
    # catalog hints + ``cli_exit`` codes stay consistent with the rest
    # of the client surface.
    # -----------------------------------------------------------------

    def me(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        """GET ``/v1/me`` — return identity info + observed API-key class.

        v0.10.0 (Q-1) — calls Cursor's ``/v1/me`` and inspects the
        runtime-additive personal-key marker fields
        (``userId | userFirstName | userLastName``) to classify the API
        key class. The OpenAPI ``ApiKeyInfo`` schema only declares
        ``apiKeyName | createdAt | userEmail`` — the trio above is NOT
        in the spec but IS reliably emitted by personal API keys
        (research/01-path-2-live-probe.md §"API key class diagnostics"
        L168-180). Service-account keys carry only the documented trio.

        The detection is **purely informational**. Every code path that
        could route to either key class uses the env-field shape
        unconditionally (Q-2). The ``api_key_class`` value is intended
        for log lines / telemetry only — popola does not branch
        behaviour on it.

        Returns:
            ``{"api_key_class": "personal" | "service_account",
                "user_id": int | None,
                "user_email": str}``

            ``api_key_class`` is ``"personal"`` iff the response
            contains ANY of ``userId``, ``userFirstName``,
            ``userLastName``. ``user_id`` is the parsed integer if
            present, else ``None``. ``user_email`` mirrors the
            ``userEmail`` field (defaults to empty string when absent).

        Raises:
            CursorCloudError: any 4xx / 5xx mapped via
                :func:`_map_http_error` (e.g. ``CursorCloudAuthError``
                on ``401``).
        """
        body = self._request_json("GET", "/v1/me", timeout_s=timeout_s)
        personal_marker_keys = ("userId", "userFirstName", "userLastName")
        api_key_class: Literal["personal", "service_account"] = (
            "personal"
            if any(key in body for key in personal_marker_keys)
            else "service_account"
        )
        raw_user_id = body.get("userId")
        user_id: int | None
        if isinstance(raw_user_id, int):
            user_id = raw_user_id
        elif isinstance(raw_user_id, str) and raw_user_id.strip():
            try:
                user_id = int(raw_user_id)
            except ValueError:
                user_id = None
        else:
            user_id = None
        raw_email = body.get("userEmail", "")
        user_email = raw_email if isinstance(raw_email, str) else ""
        return {
            "api_key_class": api_key_class,
            "user_id": user_id,
            "user_email": user_email,
        }

    def list_workers(
        self,
        *,
        timeout_s: float | None = None,
    ) -> list[WorkerInfo]:
        """GET ``/v0/private-workers`` — list registered self-hosted workers.

        v0.10.0 (Q-3) — calls Cursor's ``/v0/private-workers`` (works
        under personal API keys per probe PROBE_07/44) and converts the
        camelCase wire rows into :class:`WorkerInfo` TypedDicts. The
        wire shape is ``{"workers": [{workerId, name, isInUse,
        activeBcId, repoUrl, userId}, ...]}``; this method returns the
        ``workers`` array verbatim apart from snake_case key remapping.
        Empty / missing ``workers`` returns ``[]``.

        Used by the ``--cloud-target=self-hosted`` pre-flight gate
        (``_enforce_self_hosted_worker_exists`` in
        :mod:`popolaloom.cli.cloud_worker_cmd`) to validate that
        ``--worker-name`` matches a registered worker BEFORE the
        ``POST /v1/agents`` attempt — refusing early with a friendly
        bilingual hint when the worker is missing (per the no-fallback
        contract from DECISIONS Q-7).

        Args:
            timeout_s: Per-call timeout override; ``None`` falls back
                to the client-level default.

        Returns:
            ``list[WorkerInfo]`` — empty list when no workers are
            registered. Each row carries the snake_case keys defined
            on :class:`WorkerInfo` (``worker_id``, ``name``,
            ``is_in_use``, ``active_bc_id``, ``repo_url``, ``user_id``).

        Raises:
            CursorCloudError: any 4xx / 5xx mapped via
                :func:`_map_http_error`. Note: ``GET
                /v0/private-workers/pending-requests`` requires a
                service-account key (PROBE_08), but the listing
                endpoint itself works for personal keys too.
        """
        body = self._request_json("GET", "/v0/private-workers", timeout_s=timeout_s)
        raw_workers = body.get("workers")
        if not isinstance(raw_workers, list):
            return []
        result: list[WorkerInfo] = []
        for row in raw_workers:
            if not isinstance(row, dict):
                logger.warning(
                    "cursor-cloud /v0/private-workers row is not a dict: %r",
                    row,
                )
                continue
            worker: WorkerInfo = {}
            wire_to_local: dict[str, str] = {
                "workerId": "worker_id",
                "name": "name",
                "isInUse": "is_in_use",
                "activeBcId": "active_bc_id",
                "repoUrl": "repo_url",
                "userId": "user_id",
            }
            for wire_key, local_key in wire_to_local.items():
                if wire_key in row:
                    worker[local_key] = row[wire_key]  # type: ignore[literal-required]
            result.append(worker)
        return result

    # -----------------------------------------------------------------
    # v0.8.8 — `popola cloud runs <task>` history listing (T2.4.1)
    # runs-subcommand-spec.md §6.3 — Q-C-1 偏离默认.
    # -----------------------------------------------------------------

    def list_runs(
        self,
        agent_id: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """GET ``/v1/agents/{id}/runs`` — list run history, newest first.

        Per ``runs-subcommand-spec.md`` §1.1 + §6.3 (T2.4.1, Q-C-1
        偏离默认): wraps Cursor's ``List Runs`` REST endpoint verbatim
        and returns the parsed body
        ``{"items": [...], "nextCursor": str | None}``. ``limit`` is
        clamped to ``[1, 100]`` per the official Cursor docs (default
        20, max 100). Errors route through :func:`_map_http_error` so
        the bilingual catalog hints + ``cli_exit`` codes stay aligned
        with sibling methods (``get_agent`` / ``get_run`` / ...).

        The CLI consumer (:mod:`popolaloom.cli.cloud_cmd`) post-
        processes the body to derive ``run_index`` and ``wall_clock_s``
        per spec §3.1 columns 2 + 5; this method does **not** mutate
        the upstream wire shape.

        Args:
            agent_id: Cursor durable ``bc-...`` agent id.
            limit: Page size (default 20, clamped to ``[1, 100]``).
            cursor: Optional pagination cursor from a previous page's
                ``nextCursor``; passed to Cursor REST verbatim.
            timeout_s: Per-call timeout override; ``None`` falls back
                to the client-level default.

        Returns:
            Parsed JSON body — ``{"items": [...], "nextCursor": str | None}``.

        Raises:
            CursorCloudNotFoundError: 404 ``agent_not_found`` (spec §7
                user-locked exit 4 routing happens at the CLI layer
                via DECISIONS.md OQ-1).
            CursorCloudAuthError: 401 / 403 (catalog-mapped to
                ``cli_exit=77`` per DECISIONS.md OQ-2).
            CursorCloudError: any other 4xx / 5xx (full taxonomy in
                :func:`_map_http_error`).
        """
        params: dict[str, Any] = {"limit": max(1, min(100, limit))}
        if cursor is not None:
            params["cursor"] = cursor
        return self._request_json(
            "GET",
            f"/v1/agents/{agent_id}/runs",
            params=params,
            timeout_s=timeout_s,
        )

    # -----------------------------------------------------------------
    # v0.8.8 — multi-run follow-up dispatch (T2.1.1)
    # event-merge-spec.md §1 #1 + §2.3:
    #   * "Send a follow-up prompt to an existing active agent"
    #   * 409 ``agent_busy`` → CursorCloudConflictError; queue path lives
    #     in T2.2.2 (cloud.busy_queued / busy_dispatched).
    # -----------------------------------------------------------------

    def create_followup_run(
        self,
        agent_id: str,
        prompt: str,
        *,
        model: str | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """POST ``/v1/agents/{id}/runs`` — send a follow-up prompt (T2.1.1).

        The new run uses the agent's current conversation and workspace
        state (per Cursor docs ``endpoints.md#create-a-run`` retrieved
        2026-05-08T15:24Z). Only one run per agent may be non-terminal at
        a time: calling this while another run is ``CREATING`` or
        ``RUNNING`` returns ``409 agent_busy``, which :meth:`_request_json`
        maps to :class:`CursorCloudConflictError` via the catalog
        ``conflict_agent_busy`` entry. The async-queue handling for that
        409 lives in T2.2.2 (``daemon/cloud_poller.py`` ``PendingDispatchQueue``);
        this method is the synchronous *primitive* — callers that want
        the queue semantics must compose with the poller.

        Args:
            agent_id: Cursor durable ``bc-...`` agent id.
            prompt: Follow-up prompt body (placed under ``prompt.text``).
            model: Optional Cursor model id (``composer-2`` etc.); when
                ``None`` the upstream API selects the agent's current
                default. Sending an explicit model lets a follow-up flip
                models without rewinding the conversation.
            timeout_s: Per-call timeout override; ``None`` falls back to
                the client-level default.

        Returns:
            Parsed JSON body — typically ``{"id": "run-...", "status":
            "CREATING", ...}``. The new ``run_id`` lives at the top-level
            ``id`` key (per upstream wire shape).

        Raises:
            CursorCloudConflictError: 409 ``agent_busy`` (a prior run is
                still ``CREATING`` / ``RUNNING``); not retryable in
                ``mode = "fail_fast"``.
            CursorCloudNotFoundError: 404 ``agent_not_found`` (agent
                deleted / wrong account).
            CursorCloudError: any other 4xx / 5xx (full taxonomy in
                :func:`_map_http_error`).
        """
        if not prompt:
            raise ValueError("cursor-cloud: prompt must be non-empty for create_followup_run")
        payload: dict[str, Any] = {"prompt": {"text": prompt}}
        if model is not None:
            if not isinstance(model, str) or not model:
                raise ValueError(
                    "cursor-cloud: model must be a non-empty str when provided"
                )
            payload["model"] = {"id": model}
        return self._request_json(
            "POST",
            f"/v1/agents/{agent_id}/runs",
            json_body=payload,
            timeout_s=timeout_s,
        )

    def stream_run(
        self,
        agent_id: str,
        run_id: str,
        *,
        last_event_id: str | None = None,
        timeout_s: float = DEFAULT_STREAM_TIMEOUT_S,
    ) -> Iterator[tuple[str, dict[str, Any], str | None]]:
        """Open an SSE stream and yield typed events (T2.1.1).

        Calls ``GET /v1/agents/{agent_id}/runs/{run_id}/stream`` with
        ``Accept: text/event-stream`` and (optionally) a ``Last-Event-ID``
        resume header. Each yielded tuple is
        ``(event_type, data_dict, sse_event_id_or_None)`` per the §3
        mapping in ``sse-event-schema.md``.

        Error semantics:

        - ``410 stream_expired`` (or message containing it): raises
          :class:`CursorCloudStreamExpiredError` per Q-A-4. Caller MUST
          NOT auto-reconnect — fall back to poll.
        - ``410 invalid_last_event_id``: raises
          :class:`CursorCloudStreamInvalidLastEventIdError`. Caller may
          drop the saved id and retry once without the header.
        - Any other 4xx/5xx: routed through :func:`_map_http_error`.
        - Per-frame JSON parse failure: yielded as a synthetic
          ``parse_error`` event (No-Silent-Failures); the stream
          continues. The caller decides whether to escalate.

        Yields:
            ``(event_type, data_dict, sse_event_id)`` triples; ``data_dict``
            is the JSON-decoded payload (empty dict for heartbeats);
            ``sse_event_id`` is the ``id:`` line value (``None`` if absent).
        """
        url = f"/v1/agents/{agent_id}/runs/{run_id}/stream"
        headers = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = last_event_id

        with self._client.stream(
            "GET",
            url,
            headers=headers,
            timeout=timeout_s,
        ) as response:
            if response.status_code >= 400:
                response.read()
                if response.status_code == 410:
                    err_code, err_msg = _parse_error_body(response)
                    msg_l = (err_msg or "").lower()
                    if err_code == "invalid_last_event_id" or "invalid_last_event_id" in msg_l:
                        raise CursorCloudStreamInvalidLastEventIdError(
                            "cursor-cloud HTTP 410: invalid Last-Event-ID; "
                            "drop the saved id and retry without the header",
                            status_code=410,
                            is_retryable=True,
                        )
                raise _map_http_error(response)

            yield from iter_events(response.iter_lines())

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        # v0.8.8 (T2.4.1) — additive ``params`` kwarg for query-string GETs
        # (e.g. ``GET /v1/agents/{id}/runs?limit=...&cursor=...``); existing
        # callers omit it so behavior is unchanged. Per
        # ``runs-subcommand-spec.md`` §6.3 trailing note.
        timeout: float | httpx.Timeout | None = timeout_s if timeout_s is not None else None
        try:
            response = self._client.request(
                method,
                path,
                json=json_body,
                params=params,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise CursorCloudError(
                f"cursor-cloud request timeout: {exc}",
                status_code=None,
                is_retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise CursorCloudError(
                f"cursor-cloud request failed: {exc}",
                status_code=None,
                is_retryable=True,
            ) from exc

        if response.status_code >= 400:
            logger.warning(
                "cursor-cloud %s %s -> %s",
                method,
                path,
                response.status_code,
            )
            raise _map_http_error(response)

        if not response.content:
            return {}
        return cast(dict[str, Any], response.json())

    # -----------------------------------------------------------------
    # v0.8.8 — quota-aware retry wrapper (T2.1.3)
    # quota-config.md §3 algorithm + §3.4 integration points.
    #
    # Disjoint code block from T2.1.1's ``create_followup_run`` (above).
    # Both ``CloudPollLoop._poll_run_body`` (single-shot ``GET /runs/{id}``)
    # and the future follow-up dispatch path delegate here so the only
    # 429 / quota-class schedule lives in this single helper, eliminating
    # the ad-hoc ``0.5 * 2**attempt`` schedule that v0.8.5–v0.8.7 carried.
    # -----------------------------------------------------------------

    def _retrying_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout_s: float | None = None,
        backoff_config: BackoffConfig | None = None,
        event_log: EventLog | None = None,
        task_id: str | None = None,
        sleep: Any = None,
        rng: random.Random | None = None,
    ) -> dict[str, Any]:
        """``_request_json`` with quota-aware retry per ``quota-config.md`` §3.

        On a ``429`` (or quota-class ``409`` per :func:`_is_quota_class_409`)
        the helper sleeps ``compute_backoff(attempt, cfg, retry_after)`` and
        retries up to :attr:`BackoffConfig.max_retries` times. Other 4xx /
        5xx errors propagate verbatim through :func:`_map_http_error` (i.e.
        the catalog's bilingual hints + ``cli_exit`` unchanged).

        Event semantics (per `quota-config.md` §3.3 + §5.1):

        - ``cloud.queued_quota_exceeded`` fires **once per backoff sequence**
          (on the first 429 / quota-class 409 only — NOT once per attempt).
        - ``cloud.queue_exit`` closes the bracket with one of three outcomes:

          * ``"success"`` — the eventual response is < 400.
          * ``"exhausted"`` — ``max_retries`` reached without success → the
            caller surfaces ``CursorCloudRateLimitError`` (cli_exit=75).
          * ``"cancelled"`` — reserved for the future cancel path (``not yet
            wired in v0.8.8 — placeholder for symmetry).

        Args:
            method: HTTP verb, e.g. ``"GET"``, ``"POST"``.
            path: API path, e.g. ``"/v1/agents"`` (joined to base_url).
            json_body: Optional JSON request body.
            timeout_s: Per-call timeout (None → client default).
            backoff_config: Validated :class:`BackoffConfig`. When ``None``
                falls back to spec defaults (`max_retries=5`, `500 ms` /
                `30 s` cap / ±25 % jitter / honor Retry-After).
            event_log: Optional :class:`EventLog` to record
                ``cloud.queued_quota_exceeded`` / ``cloud.queue_exit``. When
                ``None``, the helper still retries but emits no events.
            task_id: Required when ``event_log`` is provided so each event
                carries the per-task scope.
            sleep: Optional injectable sleep function (defaults to
                :func:`time.sleep`); tests may pass a no-op + a chaperone
                that records the requested durations.
            rng: Optional :class:`random.Random` for deterministic jitter
                (tests may pin via ``random.Random(seed)``).

        Returns:
            Parsed JSON body (same as :meth:`_request_json`).

        Raises:
            CursorCloudRateLimitError: ``max_retries`` reached on a sustained
                429 / quota-class 409. ``cli_exit=75``.
            CursorCloudError: any non-quota error (auth, validation,
                conflict, etc.) — propagates verbatim.
        """
        cfg = backoff_config if backoff_config is not None else BackoffConfig()
        sleeper: Any = sleep if sleep is not None else time.sleep
        attempt = 0
        total_wait_ms: float = 0.0
        emitted_quota_exceeded = False

        while True:
            try:
                response = self._client.request(
                    method,
                    path,
                    json=json_body,
                    timeout=timeout_s if timeout_s is not None else None,
                )
            except httpx.TimeoutException as exc:
                raise CursorCloudError(
                    f"cursor-cloud request timeout: {exc}",
                    status_code=None,
                    is_retryable=True,
                ) from exc
            except httpx.RequestError as exc:
                raise CursorCloudError(
                    f"cursor-cloud request failed: {exc}",
                    status_code=None,
                    is_retryable=True,
                ) from exc

            if response.status_code < 400:
                if attempt > 0 and event_log is not None and task_id is not None:
                    event_log.append(
                        "cloud.queue_exit",
                        {
                            "task_id": task_id,
                            "attempts": attempt,
                            "total_wait_ms": int(total_wait_ms),
                            "outcome": "success",
                        },
                    )
                if not response.content:
                    return {}
                return cast(dict[str, Any], response.json())

            is_429 = response.status_code == 429
            is_quota_409 = _is_quota_class_409(response)
            if is_429 or is_quota_409:
                if attempt >= cfg.max_retries:
                    if event_log is not None and task_id is not None:
                        event_log.append(
                            "cloud.queue_exit",
                            {
                                "task_id": task_id,
                                "attempts": attempt,
                                "total_wait_ms": int(total_wait_ms),
                                "outcome": "exhausted",
                            },
                        )
                    logger.warning(
                        "cursor-cloud %s %s -> %s exhausted after %d attempts",
                        method,
                        path,
                        response.status_code,
                        attempt,
                    )
                    raise _map_http_error(response)

                retry_after_header = response.headers.get("Retry-After")
                if not emitted_quota_exceeded and event_log is not None and task_id is not None:
                    parsed_ms = _parse_retry_after(retry_after_header)
                    event_log.append(
                        "cloud.queued_quota_exceeded",
                        {
                            "task_id": task_id,
                            "status": response.status_code,
                            "retry_after_ms": parsed_ms,
                            "max_retries": cfg.max_retries,
                            "ts": datetime.now(UTC)
                            .isoformat(timespec="milliseconds")
                            .replace("+00:00", "Z"),
                        },
                    )
                    emitted_quota_exceeded = True

                delay_ms = _compute_backoff(attempt, cfg, retry_after_header, rng=rng)
                logger.warning(
                    "cursor-cloud %s %s -> %s retry %d/%d after %.0f ms",
                    method,
                    path,
                    response.status_code,
                    attempt + 1,
                    cfg.max_retries,
                    delay_ms,
                )
                sleeper(delay_ms / 1000.0)
                total_wait_ms += delay_ms
                attempt += 1
                continue

            logger.warning(
                "cursor-cloud %s %s -> %s",
                method,
                path,
                response.status_code,
            )
            raise _map_http_error(response)


class SSEReader:
    """Pump Cursor SSE events into an :class:`EventLog` (append-only).

    The reader is the **fine-grained event appender** in v0.8.6's writer
    contract (``state-source-of-truth.md`` §1.2). It owns:

    - per-session monotonic ``seq`` counter (initialised to 0; written
      verbatim into each ``cloud.sse.*`` envelope);
    - in-memory LRU dedup of ``(run_id, sse_event_id)`` keys with size cap
      :attr:`LRU_MAX` (256, LRU eviction via :class:`OrderedDict`);
    - a per-``task_id`` ``stream_session_id`` (Q-A-OQ-5 default) minted in
      :meth:`__init__` so reconnects can be correlated downstream;
    - a public :meth:`terminal_hint` :class:`threading.Event` handle whose
      semantics are wired by T2.2.2 (the reader itself does not consume it).

    Q-A-8 sole-writer rule: this class **MUST NOT** receive a
    :class:`~popolaloom.daemon.state.StateStore` reference. The constructor
    rejects any collaborator whose class name is ``StateStore`` (defence in
    depth on top of the static type signature). The CI static-grep guard
    from T2.2.2 additionally enforces that no ``StateStore.update(...
    cloud_phase=...)`` call lives outside ``daemon/cloud_poller.py``.
    """

    LRU_MAX: int = 256
    """Max entries in the ``(run_id, sse_event_id)`` LRU dedup window."""

    DEDUP_DROP_FLUSH_EVERY: int = 10
    """Emit a ``cloud.sse.dedup_drop`` summary every N drops + at end of pump."""

    def __init__(
        self,
        client: CloudCursorClient,
        event_log: EventLog,
        task_id: str,
        run_id: str,
        *,
        agent_id: str,
        run_index: int = 0,
    ) -> None:
        # Q-A-8: SSE reader is structurally barred from cloud_phase mutation.
        collaborator_types = {type(arg).__name__ for arg in (event_log, client)}
        assert "StateStore" not in collaborator_types, (
            "SSE reader is structurally barred from cloud_phase mutation (Q-A-8); "
            "a StateStore was passed where an EventLog/CloudCursorClient was expected."
        )
        if "StateStore" in collaborator_types:  # pragma: no cover — defence in depth
            raise TypeError(
                "SSE reader is structurally barred from cloud_phase mutation (Q-A-8); "
                "a StateStore was passed where an EventLog/CloudCursorClient was expected."
            )

        self._client = client
        self._event_log = event_log
        self._task_id = task_id
        self._run_id = run_id
        self._agent_id = agent_id
        # v0.8.8 (T2.1.1): per-run ordinal stamped into every cloud.sse.* envelope
        # under data.run_index (event-merge-spec.md §2.2). Default 0 keeps legacy
        # v0.8.6 single-run callers compatible — they get the implicit run-0
        # identity that v0.8.6 already assumed but never spelled out.
        self._run_index: int = run_index
        # OQ-5 default: stream_session_id is per-task_id, minted on construction.
        self._stream_session_id: str = f"{task_id}-{int(time.monotonic() * 1000)}"
        self._seq: int = 0
        self._lru: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._dedup_drop_count: int = 0
        self._dedup_drop_total: int = 0
        self._dedup_drop_first: str | None = None
        self._dedup_drop_last: str | None = None
        self._last_event_id: str | None = None
        self._terminal_hint_event: threading.Event = threading.Event()

    @property
    def last_event_id(self) -> str | None:
        """The most-recent non-None ``sse_event_id`` observed (None pre-first event).

        Caller may pass this back into :meth:`CloudCursorClient.stream_run`
        as ``last_event_id`` for resume after disconnect.
        """
        return self._last_event_id

    @property
    def stream_session_id(self) -> str:
        """Per-``task_id`` session id minted in ``__init__`` (OQ-5 default)."""
        return self._stream_session_id

    @property
    def seq(self) -> int:
        """Current monotonic seq value (next emitted event uses this then increments)."""
        return self._seq

    @property
    def terminal_hint(self) -> threading.Event:
        """Public read-only handle for T2.2.2 to ``set()`` for poller wakeup.

        Not consumed by the reader itself; T2.2.2 will wire the actual
        SSE↔poller wakeup semantics. Documented per AC (b).
        """
        return self._terminal_hint_event

    @property
    def run_index(self) -> int:
        """Per-run ordinal stamped into every emitted envelope (v0.8.8, T2.1.1).

        ``0`` is the default for legacy v0.8.6 callers / the initial run;
        follow-ups created via :meth:`CloudCursorClient.create_followup_run`
        carry ``1, 2, ...`` per ``event-merge-spec.md`` §2.2.
        """
        return self._run_index

    def _envelope(
        self,
        sse_id: str | None,
        payload: dict[str, Any],
        seq: int,
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # v0.8.8 (T2.1.1) — sextuple identity: stamp run_index into data so
        # downstream consumers (renderer §3.1, replay §4.1, dedup §4.3) can
        # group + sort by (run_index, seq) without a separate lookup.
        # event-merge-spec.md §2.1 + §2.4: the field lives under data.* (not
        # in the CloudEvents extensions block) because it is task-scoped
        # business data, not infra metadata.
        env: dict[str, Any] = {
            "task_id": self._task_id,
            "agent_id": self._agent_id,
            "run_id": self._run_id,
            "run_index": self._run_index,
            "stream_session_id": self._stream_session_id,
            "sse_id": sse_id,
            "seq": seq,
            "payload": payload,
        }
        if extra:
            env.update(extra)
        return env

    def _emit_dedup_summary(self) -> None:
        if self._dedup_drop_count == 0:
            return
        seq = self._seq
        self._seq += 1
        self._event_log.append(
            "cloud.sse.dedup_drop",
            {
                "task_id": self._task_id,
                "agent_id": self._agent_id,
                "run_id": self._run_id,
                "run_index": self._run_index,
                "stream_session_id": self._stream_session_id,
                "sse_id": None,
                "seq": seq,
                "count": self._dedup_drop_count,
                "first_id": self._dedup_drop_first,
                "last_id": self._dedup_drop_last,
                "total_dropped_so_far": self._dedup_drop_total,
            },
        )
        self._dedup_drop_count = 0
        self._dedup_drop_first = None
        self._dedup_drop_last = None

    def _record_dedup(self, sse_id: str) -> None:
        self._dedup_drop_count += 1
        self._dedup_drop_total += 1
        if self._dedup_drop_first is None:
            self._dedup_drop_first = sse_id
        self._dedup_drop_last = sse_id
        if self._dedup_drop_count >= self.DEDUP_DROP_FLUSH_EVERY:
            self._emit_dedup_summary()

    def _emit_parse_error(
        self,
        sse_id: str | None,
        parse_fields: dict[str, Any],
    ) -> None:
        # state-source-of-truth.md §5 failure mode #4 specifies parse_error
        # carries raw_chunk_b64 + error_type at the top level of ``data``;
        # there is no parsed ``payload`` to nest, so we flatten. v0.8.8 adds
        # run_index to keep all cloud.sse.* envelopes sextuple-identifiable
        # (event-merge-spec.md §2.4).
        seq = self._seq
        self._seq += 1
        envelope: dict[str, Any] = {
            "task_id": self._task_id,
            "agent_id": self._agent_id,
            "run_id": self._run_id,
            "run_index": self._run_index,
            "stream_session_id": self._stream_session_id,
            "sse_id": sse_id,
            "seq": seq,
        }
        envelope.update(parse_fields)
        self._event_log.append("cloud.sse.parse_error", envelope)

    def _emit_stream_expired(self) -> None:
        seq = self._seq
        self._seq += 1
        self._event_log.append(
            "cloud.sse.stream_expired",
            self._envelope(
                None,
                {},
                seq,
                extra={"reason": "stream_expired"},
            ),
        )

    def pump(self, stop_event: threading.Event | None = None) -> None:
        """Drive a single ``stream_run`` iteration end-to-end.

        Each frame is dedup'd via the LRU and appended as a
        ``cloud.sse.<event_type>`` envelope carrying the
        ``(task_id, run_id, stream_session_id, sse_id, seq)`` quintuple
        plus the parsed ``payload`` dict. Heartbeats and id-less events
        are emitted unconditionally (no dedup, but ``seq`` still
        advances). Duplicate ids are counted into the next
        ``cloud.sse.dedup_drop`` summary.

        Returns cleanly on:

        - end of the SSE iterator (server closed the stream),
        - ``stop_event.is_set()``,
        - :class:`CursorCloudStreamExpiredError` (also emits a
          ``cloud.sse.stream_expired`` envelope per OQ-6 / Q-A-4),
        - any low-level ``httpx`` protocol/parse error (also emits a
          ``cloud.sse.parse_error`` envelope; No-Silent-Failures).

        ``CursorCloudStreamInvalidLastEventIdError`` is intentionally
        propagated so the caller can drop the saved id and retry without
        the resume header.
        """
        try:
            for event_type, data, sse_id in self._client.stream_run(
                self._agent_id,
                self._run_id,
                last_event_id=self._last_event_id,
            ):
                if stop_event is not None and stop_event.is_set():
                    self._emit_dedup_summary()
                    return

                if event_type == _PARSE_ERROR_EVENT:
                    self._emit_parse_error(sse_id, data)
                    continue

                if sse_id is not None:
                    key = (self._run_id, sse_id)
                    if key in self._lru:
                        self._lru.move_to_end(key)
                        self._record_dedup(sse_id)
                        continue
                    self._lru[key] = None
                    if len(self._lru) > self.LRU_MAX:
                        self._lru.popitem(last=False)
                    self._last_event_id = sse_id

                seq = self._seq
                self._seq += 1
                self._event_log.append(
                    f"cloud.sse.{event_type}",
                    self._envelope(sse_id, data, seq),
                )

            self._emit_dedup_summary()
        except CursorCloudStreamExpiredError:
            self._emit_stream_expired()
            self._emit_dedup_summary()
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            self._emit_parse_error(
                None,
                {
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            self._emit_dedup_summary()


class CursorCloudAdapter:
    """Cursor Cloud Agent adapter — registered as ``cursor-cloud``.

    Per Option α from v0.8.5 research: a sibling adapter alongside
    :class:`~popolaloom.adapters.cursor.CursorAdapter`, selected via
    ``popola dispatch ... --cli=cursor-cloud``.

    Note:
        :meth:`build_command` returns :data:`CLOUD_BUILD_COMMAND_MARKER` plus a
        JSON blob — not a real argv. Supervisor (Stage 2) detects this sentinel
        and calls :class:`CloudCursorClient` instead of subprocess spawn.
    """

    name: str = "cursor-cloud"
    binary: str = ""

    def build_command(
        self,
        prompt: str,
        cwd: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        """Return cloud sentinel argv: marker prefix + JSON(state).

        The JSON object uses sorted keys for stable markers:

        - ``prompt``: task text
        - ``cwd``: string path or null
        - ``extra``: validated cloud ``extra`` dict (defaults merged)

        Optional ``extra`` keys (``--cli-flag`` passthrough):

        - ``repo_url`` (``str``, required unless ``pr_url``) — GitHub repo URL
        - ``starting_ref`` (``str``, default ``\"main\"``) — branch / tag / ref
        - ``model`` (``str``, default ``\"default\"``; v0.10.0 bumped from
          ``\"composer-2\"``) — model id
        - ``auto_create_pr`` (``bool``, default ``False``)
        - ``work_on_current_branch`` (``bool``, default ``False``) — emits
          ``workOnCurrentBranch:true`` (v0.10.0; the legacy
          ``autoGenerateBranch:false`` field is rejected by the gateway)
        - ``skip_reviewer_request`` (``bool``, default ``False``)
        - ``pr_url`` (``str``, optional) — PR URL (``repos[0].prUrl``)
        - ``env_vars`` (``dict[str, str]``, optional)
        - ``cloud_target`` (``str``, optional) — one of
          ``\"self-hosted\"`` / ``\"cursor-managed\"``; v0.10.0 (Q-2 / Q-6)
          symmetric with the ``--cloud-target`` CLI flag.
          ``\"self-hosted\"`` requires ``worker_name`` (or ``machine_name``);
          ``\"cursor-managed\"`` rejects routing knobs.
          ``\"ask-each-time\"`` is rejected at adapter time (it must be
          resolved by the CLI before dispatch).
        - ``pool_name`` (``str``, optional) — emits
          ``env={type:\"pool\", name:X}`` (or ``{type:\"pool\"}`` when
          empty); the v0.10.x current-style spelling for pool routing.
        - ``use_private_worker`` / ``labels`` / ``worker_name`` /
          ``machine_name`` (DEPRECATED, v0.10.0) — legacy v0.9.x
          aliases; translate to ``env={type:\"machine\", name:X}`` and
          emit a single :class:`DeprecationWarning` per call. v1.1+
          will remove the alias path entirely; use ``cloud_target`` +
          ``worker_name`` (which becomes the new style once Wave B3
          ships) or pass ``env`` directly.
        - ``timeout_s`` (``float``, default ``60.0``)
        - ``api_key`` (``str``, optional) — overrides :envvar:`CURSOR_API_KEY`

        Raises:
            ValueError: when both ``repo_url`` and ``pr_url`` are absent, or
                when a present key has an invalid type.
        """
        normalized = _normalize_cloud_extra(extra or {})
        payload = {
            "cwd": str(cwd) if cwd is not None else None,
            "extra": normalized,
            "prompt": prompt,
        }
        encoded = json.dumps(payload, sort_keys=True)
        return [*CLOUD_BUILD_COMMAND_MARKER, encoded]

    def is_available(self) -> bool:
        """True iff a Cursor API key is resolvable.

        Resolves through :func:`popolaloom.credentials.resolve_cursor_api_key`
        so the answer reflects every documented precedence slot
        (explicit override > env var > OS keyring). Backward-compatible
        with the historical ``CURSOR_API_KEY`` env-var path —
        ``resolve_cursor_api_key`` consults the env var as precedence #2.
        """
        from popolaloom.credentials import resolve_cursor_api_key

        return resolve_cursor_api_key() is not None


def basic_auth_header_value(api_key: str) -> str:
    """Return ``Authorization`` header value for Cursor Basic auth (test helper).

    Format: ``Basic base64(f\"{api_key}:\")`` — password empty.
    """
    token = base64.b64encode(f"{api_key}:".encode()).decode("ascii")
    return f"Basic {token}"


def redact_cloud_marker_cmd(cmd: list[str]) -> list[str]:
    """Return a copy of ``cmd`` with any embedded ``api_key`` value redacted.

    v0.9.2: the cursor-cloud marker payload (built by
    :meth:`CursorCloudAdapter.build_command`) is a JSON blob that may
    legitimately contain ``extra.api_key`` (when an operator or test
    passed ``--cli-flag api_key=...``). Persisting that blob into
    ``TaskHandle.cmd``, the NDJSON event log, or the ArkTower SQLite
    row would leak the secret into surfaces ``popola list`` /
    ``popola status`` / ``popola attach`` happily echo back.

    This helper walks the marker payload and replaces the ``api_key``
    string with the redaction placeholder, preserving every other key
    so debug / replay flows keep working. Non-cloud commands and
    malformed payloads pass through unchanged (No Silent Failures —
    nothing to redact, no error to raise).

    Returns a fresh list; the input is not mutated.
    """
    if len(cmd) < 3 or cmd[:2] != CLOUD_BUILD_COMMAND_MARKER:
        return list(cmd)
    raw_payload = cmd[2]
    if not isinstance(raw_payload, str):
        return list(cmd)
    try:
        payload = json.loads(raw_payload)
    except (TypeError, ValueError):
        return list(cmd)
    if not isinstance(payload, dict):
        return list(cmd)
    extra = payload.get("extra")
    if not isinstance(extra, dict) or "api_key" not in extra:
        return list(cmd)
    sanitized_extra = dict(extra)
    sanitized_extra["api_key"] = "<REDACTED:CURSOR_API_KEY>"
    sanitized_payload = dict(payload)
    sanitized_payload["extra"] = sanitized_extra
    redacted_json = json.dumps(sanitized_payload, sort_keys=True)
    return [cmd[0], cmd[1], redacted_json]


_VALID_CLOUD_TARGETS: frozenset[str] = frozenset({
    "self-hosted",
    "cursor-managed",
    "ask-each-time",
})
"""v0.10.0 (Q-2 / Q-6) — valid values for ``--cloud-target`` / ``cloud_target`` extra.

Mirrors :data:`popolaloom.daemon.main.USER_PREF_VALID_DEFAULT_CLOUD_TARGET`
(introduced by Wave B1). ``ask-each-time`` is only valid as a stored
*default* on ``[user_preferences]``; the dispatch path resolves it to a
concrete value before invoking the adapter, so seeing it here is a
caller bug.
"""


_LEGACY_ROUTING_EXTRAS: tuple[str, ...] = (
    "use_private_worker",
    "labels",
    "worker_name",
    "machine_name",
)
"""v0.10.0 (Q-2 / Q-11) — legacy extras that translate to ``env={type:"machine",
name:X}`` and emit a single :class:`DeprecationWarning`.

`pool_name` is intentionally NOT in this list — it is the v0.10.x-current
spelling for ``env={type:"pool", name?:X}`` per AC6.
"""


def _normalize_cloud_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Validate known keys, translate legacy routing extras, return marker dict.

    v0.10.0 (DECISIONS Q-2 / Q-11) — the historical ``use_private_worker``
    / ``labels`` / ``worker_name`` / ``machine_name`` extras are now
    deprecated aliases that translate to the new
    ``env={type:"machine", name:X}`` shape (per :class:`AgentEnv`). When
    ANY of those four legacy keys is present a single
    :class:`DeprecationWarning` is emitted (one per call, never per
    sub-key). The new :data:`_VALID_CLOUD_TARGETS` ``cloud_target`` knob
    layers on top:

    - ``cloud_target="cursor-managed"``: no ``env`` is emitted; the
      gateway defaults to ``{type:"cloud"}``.
    - ``cloud_target="self-hosted"``: requires ``worker_name`` or
      ``machine_name`` (or a ``labels.worker`` / ``labels.machine``
      legacy alias) to also be set; raises :class:`ValueError`
      otherwise.
    - ``cloud_target="ask-each-time"``: rejected at adapter time
      (``ask-each-time`` is only valid as a stored default on
      ``[user_preferences]``; the CLI must resolve it before dispatch).
    - ``pool_name=<X>`` (no ``cloud_target`` needed): emits
      ``env={type:"pool", name:X}``; empty string / ``""`` emits
      ``env={type:"pool"}`` (Self-Hosted Pool default name).

    Output dict shape (v0.10.0):

    - Always: ``auto_create_pr``, ``model``, ``skip_reviewer_request``,
      ``starting_ref``, ``timeout_s``, ``work_on_current_branch``.
    - Conditional: ``repo_url`` / ``pr_url``, ``env_vars``, ``env``
      (the resolved :class:`AgentEnv`), ``cloud_target`` (when set),
      ``api_key``.

    Note: the v0.9.x output keys ``use_private_worker`` and ``labels``
    are NO LONGER EMITTED — every routing decision lives in ``env``
    instead. Callers reading the marker payload (e.g. the daemon
    supervisor) MUST read ``env`` and pass it through to
    :meth:`CloudCursorClient.create_agent`.

    Raises:
        ValueError: for any invalid type, conflicting routing knobs,
            or unsupported ``cloud_target`` value.
    """
    repo_url = extra.get("repo_url")
    pr_url = extra.get("pr_url")
    if repo_url is None and pr_url is None:
        raise ValueError(
            "cursor-cloud: repo_url or pr_url is required in extra "
            "(cloud agents target a GitHub repo or PR)"
        )

    if repo_url is not None and not isinstance(repo_url, str):
        raise ValueError(
            f"cursor-cloud: repo_url must be str, got {type(repo_url).__name__}"
        )
    if pr_url is not None and not isinstance(pr_url, str):
        raise ValueError(f"cursor-cloud: pr_url must be str, got {type(pr_url).__name__}")

    if "starting_ref" in extra:
        starting_ref = extra["starting_ref"]
        if not isinstance(starting_ref, str):
            raise ValueError(
                "cursor-cloud: starting_ref must be str, "
                f"got {type(starting_ref).__name__}"
            )
    else:
        starting_ref = "main"

    if "model" in extra:
        model = extra["model"]
        if not isinstance(model, str):
            raise ValueError(f"cursor-cloud: model must be str, got {type(model).__name__}")
    else:
        # v0.10.0 (AC12) — default model bumped from "composer-2" to
        # "default" so Cursor picks the recommended model for the user's
        # plan rather than pinning to a name that may rotate
        # (research/02-path-1-visibility-probe.md §1 L70-77).
        model = "default"

    for key in ("auto_create_pr", "work_on_current_branch", "skip_reviewer_request"):
        if key in extra and not isinstance(extra[key], bool):
            raise ValueError(
                f"cursor-cloud: {key} must be bool, got {type(extra[key]).__name__}"
            )

    auto_create_pr: bool = bool(extra.get("auto_create_pr", False))
    work_on_current_branch: bool = bool(extra.get("work_on_current_branch", False))
    skip_reviewer_request: bool = bool(extra.get("skip_reviewer_request", False))

    env_vars: dict[str, str] | None = None
    if "env_vars" in extra:
        ev = extra["env_vars"]
        if ev is not None:
            if not isinstance(ev, dict):
                raise ValueError(
                    "cursor-cloud: env_vars must be dict[str, str], "
                    f"got {type(ev).__name__}"
                )
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in ev.items()):
                raise ValueError("cursor-cloud: env_vars must be dict[str, str] only")
            env_vars = dict(ev)

    # ---- v0.10.0 routing translation (AC5 / AC6 / AC7) ------------------
    # 1. Validate the four legacy extras for shape/type so a buggy caller
    #    passing labels=int still gets a precise error.
    # 2. Validate the new cloud_target knob and pool_name.
    # 3. Resolve a single (env_type, env_name) pair from whatever combo
    #    the caller set; raise on conflicts.
    # 4. Emit ONE DeprecationWarning when ANY legacy extra was present
    #    (per implementation hint in PLAN A1).

    use_private_worker_explicit = "use_private_worker" in extra
    if use_private_worker_explicit:
        raw_upw = extra["use_private_worker"]
        if not isinstance(raw_upw, bool):
            raise ValueError(
                "cursor-cloud: use_private_worker must be bool, "
                f"got {type(raw_upw).__name__}"
            )
        use_private_worker_value: bool = raw_upw
    else:
        use_private_worker_value = False

    labels: dict[str, str] = {}
    labels_explicit = "labels" in extra
    if labels_explicit:
        raw_labels = extra["labels"]
        if raw_labels is not None:
            if not isinstance(raw_labels, dict):
                raise ValueError(
                    "cursor-cloud: labels must be dict[str, str], "
                    f"got {type(raw_labels).__name__}"
                )
            if not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in raw_labels.items()
            ):
                raise ValueError("cursor-cloud: labels must be dict[str, str] only")
            labels = dict(raw_labels)

    def _validated_name(key: str) -> str | None:
        if key not in extra:
            return None
        raw = extra[key]
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"cursor-cloud: {key} must be a non-empty str")
        return raw.strip()

    worker_name = _validated_name("worker_name")
    machine_name = _validated_name("machine_name")

    pool_name: str | None = None
    pool_name_explicit = "pool_name" in extra
    if pool_name_explicit:
        raw_pool = extra["pool_name"]
        if not isinstance(raw_pool, str):
            raise ValueError(
                f"cursor-cloud: pool_name must be str, got {type(raw_pool).__name__}"
            )
        pool_name = raw_pool.strip()

    cloud_target: str | None = None
    if "cloud_target" in extra:
        raw_target = extra["cloud_target"]
        if not isinstance(raw_target, str):
            raise ValueError(
                "cursor-cloud: cloud_target must be str, "
                f"got {type(raw_target).__name__}"
            )
        cloud_target = raw_target.strip()
        if cloud_target not in _VALID_CLOUD_TARGETS:
            raise ValueError(
                "cursor-cloud: cloud_target must be one of "
                f"{sorted(_VALID_CLOUD_TARGETS)!r}, got {raw_target!r}"
            )
        if cloud_target == "ask-each-time":
            raise ValueError(
                "cursor-cloud: cloud_target='ask-each-time' is only valid as a "
                "default; resolve to 'self-hosted' or 'cursor-managed' before dispatch"
            )

    # Conflict #1 (legacy v0.9.x semantics carried forward): the caller
    # explicitly typed `use_private_worker=False` AND a routing knob.
    # In v0.9.x this auto-promoted to True with a warning; in v0.10.0 the
    # promotion silently disagrees with the explicit False so we hard-fail.
    has_routing_knob = bool(
        worker_name or machine_name or labels or pool_name_explicit
    )
    if has_routing_knob and use_private_worker_explicit and not use_private_worker_value:
        raise ValueError(
            "cursor-cloud: use_private_worker=false cannot be combined with "
            "labels, worker_name, machine_name, or pool_name; v0.10.0 routes "
            "via env={type, name?} and these knobs are deprecated aliases"
        )

    # Conflict #2 (Q-7 no-fallback contract): cloud_target=cursor-managed
    # is mutually exclusive with any self-hosted routing knob.
    if cloud_target == "cursor-managed" and (
        worker_name is not None
        or machine_name is not None
        or pool_name_explicit
        or labels
    ):
        raise ValueError(
            "cursor-cloud: cloud_target='cursor-managed' is mutually exclusive "
            "with worker_name / machine_name / pool_name / labels (cursor-managed "
            "routes to the Cursor cloud VM, not a self-hosted worker)"
        )

    # Resolve the machine name from the most-specific knob; require unique.
    machine_candidates: list[tuple[str, str]] = []
    if worker_name is not None:
        machine_candidates.append(("worker_name", worker_name))
    if machine_name is not None:
        machine_candidates.append(("machine_name", machine_name))
    legacy_label_worker = labels.get("worker") if labels else None
    if legacy_label_worker is not None:
        machine_candidates.append(("labels[worker]", legacy_label_worker))
    legacy_label_machine = labels.get("machine") if labels else None
    if legacy_label_machine is not None:
        machine_candidates.append(("labels[machine]", legacy_label_machine))

    machine_name_resolved: str | None = None
    if machine_candidates:
        first_source, first_value = machine_candidates[0]
        for source, value in machine_candidates[1:]:
            if value != first_value:
                raise ValueError(
                    f"cursor-cloud: {source}={value!r} conflicts with "
                    f"{first_source}={first_value!r}"
                )
        machine_name_resolved = first_value

    # AC7: cloud_target=self-hosted requires a resolved worker name.
    if cloud_target == "self-hosted" and machine_name_resolved is None:
        raise ValueError(
            "cursor-cloud: cloud_target='self-hosted' requires a worker name "
            "via worker_name=<X> or machine_name=<X>"
        )

    # Conflict #3: pool_name + machine_name_resolved are mutually exclusive
    # (a single dispatch routes to one env-type, not both).
    if pool_name_explicit and machine_name_resolved is not None:
        raise ValueError(
            "cursor-cloud: pool_name is mutually exclusive with worker_name / "
            "machine_name / labels.worker / labels.machine — pick one routing "
            "shape per dispatch"
        )

    # Build the env dict (AgentEnv shape).
    resolved_env: AgentEnv | None = None
    if machine_name_resolved is not None:
        resolved_env = {"type": "machine", "name": machine_name_resolved}
    elif pool_name_explicit:
        # AC6: pool_name=X → {type:"pool", name:X}; pool_name="" → {type:"pool"}.
        resolved_env = {"type": "pool"}
        if pool_name:
            resolved_env["name"] = pool_name
    elif use_private_worker_explicit and use_private_worker_value:
        # Legacy `use_private_worker=true` with no name. The gateway rejects
        # `env={type:"machine"}` without a name (PROBE_49 → 400 "env.name is
        # required when env.type is machine"); fail early with a friendlier
        # message instead of round-tripping to a 400.
        raise ValueError(
            "cursor-cloud: use_private_worker=true is deprecated and requires "
            "either worker_name=<X> or machine_name=<X>; v0.10.0 routes via "
            "env={type:'machine', name:X}"
        )
    # cloud_target=cursor-managed (no other knobs) → leave env=None.
    # cloud_target=None and no routing knobs → leave env=None (gateway default cloud).

    # AC5: emit DeprecationWarning when ANY legacy extra is present (one
    # warning per call regardless of how many legacy keys were used).
    legacy_used = any(key in extra for key in _LEGACY_ROUTING_EXTRAS)
    if legacy_used:
        warnings.warn(
            "cursor-cloud: 'use_private_worker' / 'labels' / 'worker_name' / "
            "'machine_name' extras are deprecated as of v0.10.0; pass "
            "env={'type':'machine','name':<X>} via the new --cloud-target / "
            "--worker-name CLI flags instead. The legacy alias path translates "
            "to env={type:'machine', name:<resolved>} for one minor release; "
            "v1.1+ will remove the alias entirely.",
            DeprecationWarning,
            stacklevel=3,
        )

    if "timeout_s" in extra:
        ts = extra["timeout_s"]
        if not isinstance(ts, (int, float)):
            raise ValueError(
                "cursor-cloud: timeout_s must be int or float, "
                f"got {type(ts).__name__}"
            )
        timeout_s = float(ts)
    else:
        timeout_s = DEFAULT_TIMEOUT_S

    api_key: str | None = None
    if "api_key" in extra and extra["api_key"] is not None:
        raw_key = extra["api_key"]
        if not isinstance(raw_key, str):
            raise ValueError(
                f"cursor-cloud: api_key must be str, got {type(raw_key).__name__}"
            )
        api_key = raw_key

    out: dict[str, Any] = {
        "auto_create_pr": auto_create_pr,
        "model": model,
        "skip_reviewer_request": skip_reviewer_request,
        "starting_ref": starting_ref,
        "timeout_s": timeout_s,
        "work_on_current_branch": work_on_current_branch,
    }
    if repo_url is not None:
        out["repo_url"] = repo_url
    if pr_url is not None:
        out["pr_url"] = pr_url
    if env_vars is not None:
        out["env_vars"] = env_vars
    if resolved_env is not None:
        # Materialize as a plain dict (TypedDict serializes the same shape).
        out["env"] = dict(resolved_env)
    if cloud_target is not None:
        out["cloud_target"] = cloud_target
    if api_key is not None:
        out["api_key"] = api_key
    return out
