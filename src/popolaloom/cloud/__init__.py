"""Cloud-dispatch helper sub-package — pre-flight gates and friends.

This sub-package consolidates pure helper logic used by the cloud-dispatch
pipeline. It deliberately has NO ``httpx`` import and NO dependency on
``adapters/cursor_cloud.py`` at module-load time (only via
``TYPE_CHECKING``) so it can be imported by both the CLI
(``cli/cloud_worker_cmd.py``, Wave C1) and the adapter
(``adapters/cursor_cloud.py``, Wave C2) without circular-import risk.

Public surface (per PLAN.md A2 AC 1):

- :func:`check_self_hosted_worker_exists`
- :func:`check_github_app_installed`
- :class:`WorkerExistenceResult`
- :class:`GithubAppCheckResult`

See ``.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md``
Q-3 (worker discovery) and Q-9 (GitHub-App caveat handling) for the
authoritative design rationale.
"""

from __future__ import annotations

from popolaloom.cloud.preflight import (
    GithubAppCheckResult,
    WorkerExistenceResult,
    check_github_app_installed,
    check_self_hosted_worker_exists,
)

__all__ = [
    "GithubAppCheckResult",
    "WorkerExistenceResult",
    "check_github_app_installed",
    "check_self_hosted_worker_exists",
]
