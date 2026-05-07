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
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, cast

import httpx

logger = logging.getLogger(__name__)

CLOUD_BUILD_COMMAND_MARKER: list[str] = ["__cloud__", "cursor-cloud"]
CURSOR_API_BASE: str = "https://api.cursor.com"
DEFAULT_TIMEOUT_S: float = 60.0

_CURSOR_API_KEY_ENV: str = "CURSOR_API_KEY"


class CursorCloudError(Exception):
    """Base exception for :class:`CloudCursorClient`."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        is_retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code: int | None = status_code
        self.is_retryable: bool = is_retryable


class CursorCloudAuthError(CursorCloudError):
    """401/403 — credentials invalid or insufficient permission."""


class CursorCloudConflictError(CursorCloudError):
    """409 — e.g. ``agent_busy`` / ``run_not_cancellable``; not retryable."""


def _map_http_error(response: httpx.Response) -> CursorCloudError:
    status = response.status_code
    detail = response.text[:500] if response.text else ""
    msg = f"cursor-cloud HTTP {status}: {detail}" if detail else f"cursor-cloud HTTP {status}"
    if status in (401, 403):
        return CursorCloudAuthError(msg, status_code=status, is_retryable=False)
    if status == 409:
        return CursorCloudConflictError(msg, status_code=status, is_retryable=False)
    if status >= 500:
        return CursorCloudError(msg, status_code=status, is_retryable=True)
    return CursorCloudError(msg, status_code=status, is_retryable=False)


class CloudCursorClient:
    """Synchronous ``httpx`` wrapper around ``/v1/agents``.

    Auth: HTTP Basic via ``auth=(api_key, "")``.

    Note:
        ``stream_run`` (SSE on ``GET .../stream``) and ``create_followup``
        (``POST .../runs``) are **not** implemented here — reserved for v0.8.6.
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
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """POST ``/v1/agents`` — launch a cloud agent; returns parsed JSON body."""
        if not pr_url and not repo_url:
            raise ValueError("cursor-cloud: repo_url or pr_url is required for create_agent")

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
            payload["autoGenerateBranch"] = False

        if auto_create_pr and skip_reviewer_request:
            payload["skipReviewerRequest"] = True

        if env_vars:
            payload["envVars"] = dict(env_vars)

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

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        timeout: float | httpx.Timeout | None = timeout_s if timeout_s is not None else None
        try:
            response = self._client.request(
                method,
                path,
                json=json_body,
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
        - ``model`` (``str``, default ``\"composer-2\"``) — model id
        - ``auto_create_pr`` (``bool``, default ``False``)
        - ``work_on_current_branch`` (``bool``, default ``False``)
        - ``skip_reviewer_request`` (``bool``, default ``False``)
        - ``pr_url`` (``str``, optional) — PR URL (``repos[0].prUrl``)
        - ``env_vars`` (``dict[str, str]``, optional)
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
        """True iff :envvar:`CURSOR_API_KEY` is set and non-empty."""
        key = os.environ.get(_CURSOR_API_KEY_ENV, "")
        return bool(key.strip())


def basic_auth_header_value(api_key: str) -> str:
    """Return ``Authorization`` header value for Cursor Basic auth (test helper).

    Format: ``Basic base64(f\"{api_key}:\")`` — password empty.
    """
    token = base64.b64encode(f"{api_key}:".encode()).decode("ascii")
    return f"Basic {token}"


def _normalize_cloud_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Validate known keys, merge defaults; return JSON-serializable dict."""
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
        model = "composer-2"

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
    if api_key is not None:
        out["api_key"] = api_key
    return out
