"""Pre-flight gates for cloud-cursor dispatch.

This module implements two pure helper functions consumed by the v0.10.0
cloud-dispatch pipeline. Both probe lightweight Cursor REST endpoints to
catch the most common operator failure modes BEFORE the heavy
``POST /v1/agents`` dispatch ever runs.

DECISIONS — verbatim citations from
``.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md``:

- **Q-3 (worker discovery)** — "Research/01 §'Header / undocumented endpoint
  sweep' L135-149 shows ``GET /v0/private-workers`` returns ``200`` under
  the personal API key with the full row
  ``{workerId, name, isInUse, activeBcId, repoUrl, userId}`` — this is the
  *canonical* answer to 'does this name map to a registered worker, and is
  it free to take work?'."

- **Q-9 (GitHub-App caveat handling)** — "Research/01 §'GitHub-App
  branch-validation gotcha' L161-165 + research/02 §'Implications' §6 both
  recommend pre-flight via ``GET /v1/repositories`` (returns
  ``{"items":[]}`` when the App is uninstalled) so the operator sees a
  friendly hint BEFORE the dispatch attempt instead of after."

Consumer wiring (see PLAN.md):

- :func:`check_self_hosted_worker_exists` is consumed by Wave C1's
  ``_enforce_self_hosted_worker_exists`` in ``cli/cloud_worker_cmd.py``.
- :func:`check_github_app_installed` is consumed by Wave C2's
  ``_preflight_github_app_check`` in ``adapters/cursor_cloud.py``.

Hard constraints (PLAN.md A2):

- NO ``httpx`` import — accept a :class:`CloudCursorClient`-typed argument
  and call its public / lightly-private methods.
- NO module-load-time import from ``adapters/cursor_cloud.py`` (uses
  :data:`typing.TYPE_CHECKING` only) so ``cursor_cloud`` can later import
  this module under Wave C2 without circular-import risk.
- All :class:`CursorCloudError` instances raised by the underlying client
  propagate UNCHANGED (No Silent Failures rule, see
  ``AGENTS.md`` workspace rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from popolaloom.adapters.cursor_cloud import CloudCursorClient


@dataclass(frozen=True, slots=True)
class WorkerExistenceResult:
    """Outcome of :func:`check_self_hosted_worker_exists`.

    Attributes:
        found: ``True`` iff a self-hosted worker whose ``name`` field
            equals the requested name was returned by
            :meth:`CloudCursorClient.list_workers`.
        worker: The first matching worker row (TypedDict shape per Wave A1's
            ``WorkerInfo``: ``worker_id``, ``name``, ``is_in_use``,
            ``active_bc_id``, ``repo_url``, ``user_id``). ``None`` when
            ``found`` is ``False``. Stored as ``dict[str, Any]`` so the
            dataclass is import-free at runtime — the underlying value
            is whatever ``client.list_workers`` returns.
        is_in_use: Convenience mirror of ``worker["is_in_use"]``;
            ``False`` when ``found`` is ``False``. Q-3 says the dispatch
            still succeeds when this is ``True`` (the run queues), so
            consumers should treat this as a soft warning, not a hard fail.
        message: Human-readable English summary suitable for log output.
            Bilingual hints are the CLI layer's responsibility (Wave C1).
    """

    found: bool
    worker: dict[str, Any] | None
    is_in_use: bool
    message: str


@dataclass(frozen=True, slots=True)
class GithubAppCheckResult:
    """Outcome of :func:`check_github_app_installed`.

    Attributes:
        installed:
            - ``True`` iff ``GET /v1/repositories`` returned a non-empty
              ``items`` list.
            - ``False`` iff ``items`` is empty (Cursor GitHub App is not
              installed for any of the user's GitHub orgs).
            - ``None`` iff the requested ``repo_url`` host is not
              ``github.com`` — the GitHub-App gate only fires for github.com
              URLs (research/01 §"GitHub-App branch-validation gotcha"
              L161-165). Other hosts (GitLab, Gitea, ...) use a different
              integration path that is out-of-scope for v0.10.0.
        message: Human-readable English summary suitable for log output;
            includes the ``cursor.com/integrations/github`` URL when
            ``installed`` is ``False`` so the message can be surfaced
            verbatim by both the early-refuse and the late-catch paths
            (Q-9 wants identical operator UX).
    """

    installed: bool | None
    message: str


def check_self_hosted_worker_exists(
    client: CloudCursorClient,
    name: str,
) -> WorkerExistenceResult:
    """Resolve a self-hosted worker display ``name`` against the Cursor REST inventory.

    Calls :meth:`CloudCursorClient.list_workers` (which itself wraps
    ``GET /v0/private-workers`` per Wave A1) and looks for a row whose
    ``name`` field equals the requested ``name``. Pure function: no side
    effects, no ``sys.exit``, no logging.

    Args:
        client: A connected :class:`CloudCursorClient` instance.
        name: The display name of the worker (matches ``worker["name"]``,
            NOT the ``worker_id`` UUID). Empty / falsy ``name`` returns a
            ``found=False`` result without performing any HTTP call.

    Returns:
        A :class:`WorkerExistenceResult`. Note that ``found=True,
        is_in_use=True`` is a SOFT signal — Q-3 says the dispatched run
        will queue at the gateway, so the consumer should WARN rather
        than EXIT in that case (per PLAN.md C1 AC 5d).

    Raises:
        popolaloom.adapters.cursor_cloud.CursorCloudError: any HTTP / JSON
            / auth error from the underlying ``client.list_workers()``
            call propagates UNCHANGED (No Silent Failures rule).

    Example:
        >>> # Sketch of expected client.list_workers() shape (TypedDict):
        >>> # [{"worker_id": "uuid-1", "name": "probe-w1",
        >>> #   "is_in_use": False, "active_bc_id": None,
        >>> #   "repo_url": "git.example.com/foo/bar", "user_id": 42}]
        >>> # check_self_hosted_worker_exists(client, "probe-w1") then yields:
        >>> #   .found == True
        >>> #   .worker["worker_id"] == "uuid-1"
        >>> #   .is_in_use == False
        >>> #   .message starts with "worker 'probe-w1' is registered and free"
    """
    if not name:
        return WorkerExistenceResult(
            found=False,
            worker=None,
            is_in_use=False,
            message="empty worker name supplied; skipped /v0/private-workers lookup",
        )

    workers_iter = client.list_workers()
    # WorkerInfo is a TypedDict (snake_case keys); treat it as dict[str, Any]
    # for the membership / sort / repr operations below — TypedDict is dict-
    # compatible at runtime, the cast satisfies strict mypy.
    workers: list[dict[str, Any]] = [dict(w) for w in workers_iter]

    matches: list[dict[str, Any]] = [w for w in workers if w.get("name") == name]
    if not matches:
        registered_names = sorted({w.get("name", "") for w in workers if w.get("name")})
        registered_repr = ", ".join(repr(n) for n in registered_names) or "<none>"
        return WorkerExistenceResult(
            found=False,
            worker=None,
            is_in_use=False,
            message=(
                f"worker {name!r} not found among registered self-hosted workers; "
                f"registered names: [{registered_repr}]"
            ),
        )

    first = matches[0]
    is_in_use = bool(first.get("is_in_use", False))

    duplicate_note = ""
    if len(matches) > 1:
        duplicate_note = (
            f" (NOTE: {len(matches)} registered workers share display name {name!r}; "
            f"using the FIRST match — verify worker_id="
            f"{first.get('worker_id', '<unknown>')!r})"
        )

    if is_in_use:
        active_bc = first.get("active_bc_id") or "<unknown>"
        message = (
            f"worker {name!r} is registered but currently in use "
            f"(active_bc_id={active_bc!r}); the dispatched run will queue"
            f"{duplicate_note}"
        )
    else:
        message = f"worker {name!r} is registered and free to claim a run{duplicate_note}"

    return WorkerExistenceResult(
        found=True,
        worker=first,
        is_in_use=is_in_use,
        message=message,
    )


def check_github_app_installed(
    client: CloudCursorClient,
    repo_url: str,
    *,
    target: str | None = None,
) -> GithubAppCheckResult:
    """Pre-flight the Cursor GitHub-App installation for a github.com repo URL.

    Calls ``GET /v1/repositories`` via the client's ``_request_json`` method
    (PLAN.md A2 hint (a) — a public ``list_repositories`` helper is not yet
    exposed by Wave A1; if it is added later, switch to the public method
    and remove the underscore-prefixed call below). When ``repo_url``'s
    host is NOT ``github.com``, returns ``installed=None`` — the GitHub-App
    gate only fires for github.com URLs (research/01 §"GitHub-App
    branch-validation gotcha" L161-165); other hosts use unrelated
    integrations that are out-of-scope for v0.10.0.

    v1.6.0 (``feedback_for_v1.5.2.md`` constraint #3): when
    ``target == "self-hosted"`` the gate short-circuits to
    ``installed=None`` because the operator already has a registered
    workspace worker that holds the local clone — the upstream
    GitHub-App is irrelevant. The contract is pinned by
    ``tests/cloud/test_preflight.py``: even though the Path-B
    ``cursor-cloud-internal`` transport does not own a
    ``_request_json`` method, callers that route Path-A REST against a
    self-hosted target see the same skip semantics.

    Args:
        client: A connected :class:`CloudCursorClient` instance.
        repo_url: The repository URL the operator is about to dispatch
            against. Both schema-prefixed (``https://github.com/owner/name``)
            and scheme-less (``github.com/owner/name``) forms are accepted —
            :func:`urllib.parse.urlsplit` requires a scheme, so a missing
            scheme is auto-prepended before parsing.
        target: The resolved ``cloud_target`` for the dispatch (Q-7 /
            B3 schema). Pass ``"self-hosted"`` to skip the gate entirely;
            any other value (``"cursor-managed"`` / ``None`` / ``""``)
            preserves the v0.10.0 behaviour.

    Returns:
        A :class:`GithubAppCheckResult`. Per PLAN.md A2 AC 3, ``installed``
        is ``True`` / ``False`` / ``None`` depending on the response shape,
        the URL host, AND the ``target`` value.

    Raises:
        popolaloom.adapters.cursor_cloud.CursorCloudError: any HTTP / JSON
            / auth error from the underlying ``_request_json`` call
            propagates UNCHANGED (No Silent Failures rule).

    Example:
        >>> # When client._request_json("GET", "/v1/repositories") returns
        >>> #   {"items": []}
        >>> # then check_github_app_installed(client, "https://github.com/x/y")
        >>> # returns:
        >>> #   .installed == False
        >>> #   .message contains "https://cursor.com/integrations/github"
        >>> # And check_github_app_installed(client, "https://gitlab.example.com/x/y")
        >>> # returns:
        >>> #   .installed == None  (skipped — not github.com)
        >>> # And check_github_app_installed(client, "https://github.com/x/y",
        >>> #                                target="self-hosted")
        >>> # returns:
        >>> #   .installed == None  (skipped — self-hosted target)
    """
    if target == "self-hosted":
        return GithubAppCheckResult(
            installed=None,
            message=(
                "self-hosted target — GitHub-App preflight skipped "
                "(v1.6.0 feedback_for_v1.5.2 constraint #3: the named "
                "self-hosted worker holds its own workspace clone, so "
                "the upstream GitHub-App is not required)"
            ),
        )

    if not repo_url:
        return GithubAppCheckResult(
            installed=None,
            message="empty repo_url supplied; GitHub-App pre-flight skipped",
        )

    candidate = repo_url.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parts = urlsplit(candidate)
    host = (parts.hostname or "").lower()

    if host != "github.com":
        return GithubAppCheckResult(
            installed=None,
            message=(
                f"repo_url host {host or '<empty>'!r} is not 'github.com'; "
                "GitHub-App pre-flight skipped (other integrations are not "
                "gated by GET /v1/repositories per Q-9)"
            ),
        )

    payload = client._request_json("GET", "/v1/repositories")  # noqa: SLF001

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        from popolaloom.adapters.cursor_cloud import CursorCloudError

        raise CursorCloudError(
            "cursor-cloud GET /v1/repositories returned an unexpected payload shape: "
            f"expected a dict with an 'items' list, got payload of type "
            f"{type(payload).__name__!r}; unable to determine GitHub-App "
            "installation state (No Silent Failures)"
        )

    if len(items) == 0:
        return GithubAppCheckResult(
            installed=False,
            message=(
                "Cursor GitHub App is not installed on any GitHub org for this "
                "API key (GET /v1/repositories returned an empty list). "
                "Install the App at https://cursor.com/integrations/github "
                "before dispatching to a github.com URL, OR use a non-github.com "
                "repo URL (e.g. a self-hosted GitLab/Gitea host) instead."
            ),
        )

    return GithubAppCheckResult(
        installed=True,
        message=(
            f"Cursor GitHub App is installed (GET /v1/repositories returned "
            f"{len(items)} accessible repository entr"
            f"{'y' if len(items) == 1 else 'ies'})"
        ),
    )
